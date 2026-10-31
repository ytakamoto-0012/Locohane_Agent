"""run_script_background / execute_python_code_background / check_script_job /
stop_script_job の回帰テスト。

設計変更の経緯: 従来は「即座に job_id を返し、LLM自身が check_script_job で
ポーリングする」方式だったが、dispatch_agent と同じ理由（LLMが自分でポーリング
するとその都度トークンを消費する）で、ジョブ完了まで（安全上限まで）ツール
呼び出し内でブロックし、最終結果を1回のLLM往復で直接返す設計に統一された。
安全上限を超えた場合のみ、従来どおり job_id を返すフォールバック経路が残る。
人間向けの進捗表示は cl.Message による直接push（LLM非経由・トークン消費ゼロ、
type="system_message"）に置き換わった（tests/test_tools_dispatch_agent.py と
同じ契約をこのファイルで検証する）。

サブプロセス生成は run_subagent のような注入可能な非同期関数が無いため、
tools.asyncio.create_subprocess_exec を直接モンキーパッチして差し替える。
これは asyncio モジュール自体の属性を書き換えるプロセス全体に効く操作だが、
pytest はテストを直列実行し monkeypatch は各テスト終了時に自動復元するため
安全。実サブプロセスを使う他のテスト（test_tools_python_fs_guard.py 等）と
pytest-xdist 等で並列実行する場合は干渉に注意。
"""

import asyncio
import re

import pytest

from src import tools


class _FakeUserSession:
    def __init__(self, thread_id: str = "thread-1"):
        self._data: dict = {"thread_id": thread_id}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _FakeMessage:
    """tools.cl.Message の差し替え。send()/update() された content と kwargs を記録する。"""

    _sent: list | None = None  # _install_fake_message が差し替える
    _updated: list | None = None  # 同上

    def __init__(self, content: str = "", **kwargs) -> None:
        self.content = content
        self.kwargs = kwargs

    async def send(self) -> None:
        if _FakeMessage._sent is not None:
            _FakeMessage._sent.append((self.content, self.kwargs))

    async def update(self) -> None:
        if _FakeMessage._updated is not None:
            _FakeMessage._updated.append((self.content, self.kwargs))


def _install_fake_message(monkeypatch) -> list:
    sent: list = []
    _FakeMessage._sent = sent
    _FakeMessage._updated = []
    monkeypatch.setattr(tools.cl, "Message", _FakeMessage)
    return sent


def _install_fake_message_with_updates(monkeypatch) -> tuple[list, list]:
    """send()/update() を両方検証したいテスト専用。(sent, updated) を返す。"""
    sent = _install_fake_message(monkeypatch)
    return sent, _FakeMessage._updated


class _FakeStreamReader:
    """asyncio.StreamReader の最小限フェイク。_read_stream_into が呼ぶ readline() のみ実装する。"""

    def __init__(self, lines: list, gate: asyncio.Event):
        self._lines = list(lines)
        self._gate = gate

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if not self._gate.is_set():
            await self._gate.wait()
        return b""


class _RaisingStreamReader(_FakeStreamReader):
    """一定回数の readline() の後に例外を送出するフェイク（異常系テスト用）。"""

    def __init__(self, lines: list, gate: asyncio.Event, raise_after: int = 0):
        super().__init__(lines, gate)
        self._raise_after = raise_after
        self._calls = 0

    async def readline(self) -> bytes:
        self._calls += 1
        if self._calls > self._raise_after:
            raise RuntimeError("boom")
        return await super().readline()


class _FakeProcess:
    """asyncio.subprocess.Process の最小限フェイク。_run_background_job が使う
    stdout/stderr/wait()/kill()/returncode のみ実装する。

    stdout/stderr/wait() は同じ asyncio.Event を共有し、finish()/kill() が
    それを set() することで「プロセス終了」を一斉に表現する（実プロセスの
    パイプEOF・wait()解決が同時に起きる挙動を模する）。
    """

    def __init__(self, stdout_lines=(), stderr_lines=(), exit_code: int = 0, started_running: bool = False):
        self._gate = asyncio.Event()
        self._exit_code = exit_code
        self.stdout = _FakeStreamReader(list(stdout_lines), self._gate)
        self.stderr = _FakeStreamReader(list(stderr_lines), self._gate)
        self.returncode: int | None = None
        self.killed = False
        if not started_running:
            self._gate.set()

    async def wait(self) -> int:
        await self._gate.wait()
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9
        self._gate.set()

    def finish(self, exit_code: int = 0) -> None:
        """テストからジョブの「完了」を明示的にトリガーする。"""
        self._exit_code = exit_code
        self._gate.set()


def _install_fake_subprocess(monkeypatch, fake_process: "_FakeProcess") -> dict:
    captured: dict = {}

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return fake_process

    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    return captured


def _stub_prepare_script_execution(monkeypatch, cmd: list, workdir) -> None:
    def _fake(skill_name, script_filename, script_args=None):
        return cmd, workdir

    monkeypatch.setattr(tools._script_job, "_prepare_script_execution", _fake)


def _setup(monkeypatch, tmp_path, thread_id: str = "thread-1") -> None:
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession(thread_id))
    monkeypatch.setattr(tools._script_job, "_BACKGROUND_JOBS", {})
    monkeypatch.setattr(tools._state, "_CODE_EXEC_ENABLED", True)
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_MAX_RUNTIME_SECONDS", 5)
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_JOB_RETENTION_SECONDS", 1800)
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS", 0)
    # 既定は「タイムアウトせずすぐ終わる」テストが安全上限に絶対到達しないよう
    # 十分大きめにしつつ、バグで本当にハングした場合にテストが長時間待たされ
    # ないよう短めにしておく（tests/test_tools_dispatch_agent.py と同じ方針）。
    # 安全上限フォールバックを検証するテストではさらに小さい値へ個別に上書きする。
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 5)
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS", 5)
    _install_fake_message(monkeypatch)
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)


def _extract_job_id(text: str) -> str:
    m = re.search(r"job_id=(\w+)", text)
    assert m, f"job_id が含まれていません: {text!r}"
    return m.group(1)


async def _wait_until_job_registered(timeout: float = 2.0) -> "tools._BackgroundJob":
    elapsed = 0.0
    step = 0.01
    while not tools._script_job._BACKGROUND_JOBS and elapsed < timeout:
        await asyncio.sleep(step)
        elapsed += step
    assert tools._script_job._BACKGROUND_JOBS, "ジョブが登録されませんでした"
    return next(iter(tools._script_job._BACKGROUND_JOBS.values()))


@pytest.mark.asyncio
async def test_run_script_background_normal_completion_returns_final_result_directly(monkeypatch, tmp_path) -> None:
    """安全上限内に終わる通常ケースでは、1回の呼び出しで最終結果がそのまま返る。"""
    _setup(monkeypatch, tmp_path)
    workdir = tools._state._DEFAULT_WORKDIR
    _stub_prepare_script_execution(monkeypatch, ["python", "count.py"], workdir)
    fake_process = _FakeProcess(stdout_lines=[b"hello\n"], exit_code=0)
    _install_fake_subprocess(monkeypatch, fake_process)

    result = await tools.run_script_background.ainvoke({"skill_name": "demo", "script_filename": "count.py"})

    assert "job_id=" not in result
    assert "[exit code] 0" in result
    assert "[stdout]\nhello" in result
    assert tools._script_job._BACKGROUND_JOBS == {}


@pytest.mark.asyncio
async def test_execute_python_code_background_normal_completion_registers_output_files(monkeypatch, tmp_path) -> None:
    """完了時の新規/更新ファイル自動登録が、フォールバックを経ない直接完了経路でも動く。"""
    _setup(monkeypatch, tmp_path)
    tools.cl.user_session.set("plan_approved", True)
    fake_process = _FakeProcess(started_running=True)
    _install_fake_subprocess(monkeypatch, fake_process)

    task = asyncio.create_task(tools.execute_python_code_background.ainvoke({"code": "print('hi')"}))
    job = await _wait_until_job_registered()
    tmp_py_path = job.tmp_path
    assert tmp_py_path is not None and tmp_py_path.is_file()

    # 実コードは実行されない（サブプロセスをフェイクしているため）ので、
    # コードが書き出したはずのファイルをテストから直接模倣する。
    (job.workdir / "output.txt").write_text("result", encoding="utf-8")

    fake_process.finish(0)
    result = await task

    assert "job_id=" not in result
    assert "[Created/updated files]" in result
    assert not tmp_py_path.exists()  # 完了時に一時.pyファイルが削除される


@pytest.mark.asyncio
async def test_safety_cap_fallback_returns_job_id_and_keeps_job_running(monkeypatch, tmp_path) -> None:
    """安全上限を超えるとjob_idを返してターンを終えるが、ジョブ自体は動き続ける（asyncio.shield）。"""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    workdir = tools._state._DEFAULT_WORKDIR
    _stub_prepare_script_execution(monkeypatch, ["python", "count.py"], workdir)
    fake_process = _FakeProcess(started_running=True)
    _install_fake_subprocess(monkeypatch, fake_process)

    started = await tools.run_script_background.ainvoke({"skill_name": "demo", "script_filename": "count.py"})
    assert "job_id=" in started
    job_id = _extract_job_id(started)

    job = tools._script_job._BACKGROUND_JOBS[job_id]
    assert job.status == "running"
    assert not job.runner_task.done()  # shieldにより安全上限超過でもキャンセルされていない

    fake_process.finish(0)
    await job.runner_task
    result = await tools.check_script_job.ainvoke({"job_id": job_id})
    assert "[exit code] 0" in result


@pytest.mark.asyncio
async def test_progress_is_pushed_without_llm_and_stops_after_completion(monkeypatch, tmp_path) -> None:
    """進捗pushはLLMを介さず cl.Message で type="system_message" として直接送られ、
    ジョブ完了後は止まる。会話が「実行中です」で埋まらないよう、1回目のみ
    .send() で新規メッセージを作り、2回目以降は同じメッセージを .update() で
    書き換える（新規メッセージを積み増さない）（2026-09-07 ユーザー報告）。"""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS", 0.02)
    sent, updated = _install_fake_message_with_updates(monkeypatch)
    workdir = tools._state._DEFAULT_WORKDIR
    _stub_prepare_script_execution(monkeypatch, ["python", "count.py"], workdir)
    fake_process = _FakeProcess(started_running=True)
    _install_fake_subprocess(monkeypatch, fake_process)

    async def _release_after_updates():
        # 1回目の .send() の後、.update() が実際に届くのを待ってから終わらせる（固定時間だけ待つと、
        # Windows のタイマーの粒度（約15.6ms）で .update() の前に終わることがある。test_tools_dispatch_agent と同じ）。
        deadline = asyncio.get_running_loop().time() + 5
        while not updated and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        fake_process.finish(0)

    release_task = asyncio.create_task(_release_after_updates())
    result = await tools.run_script_background.ainvoke({"skill_name": "demo", "script_filename": "count.py"})
    await release_task

    assert "job_id=" not in result
    # 新規メッセージ(.send())は1回だけ、以降は同一メッセージの更新(.update())になる。
    assert len(sent) == 1
    assert len(updated) >= 1
    assert all(kwargs.get("type") == "system_message" for _, kwargs in sent)
    assert any("elapsed" in content for content, _ in updated)

    updated_count_after_completion = len(updated)
    await asyncio.sleep(0.06)  # push間隔を跨いでも増えないことを確認
    assert len(sent) == 1
    assert len(updated) == updated_count_after_completion


@pytest.mark.asyncio
async def test_cross_session_access_is_rejected(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path, thread_id="session-a")
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    workdir = tools._state._DEFAULT_WORKDIR
    _stub_prepare_script_execution(monkeypatch, ["python", "count.py"], workdir)
    fake_process = _FakeProcess(started_running=True)
    _install_fake_subprocess(monkeypatch, fake_process)

    started = await tools.run_script_background.ainvoke({"skill_name": "demo", "script_filename": "count.py"})
    job_id = _extract_job_id(started)

    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession("session-b"))
    check_result = await tools.check_script_job.ainvoke({"job_id": job_id})
    assert "does not belong to the current session" in check_result
    stop_result = await tools.stop_script_job.ainvoke({"job_id": job_id})
    assert "does not belong to the current session" in stop_result

    fake_process.finish(0)
    await tools._script_job._BACKGROUND_JOBS[job_id].runner_task


@pytest.mark.asyncio
async def test_poll_rate_limiting_rejects_too_soon(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS", 60)
    workdir = tools._state._DEFAULT_WORKDIR
    _stub_prepare_script_execution(monkeypatch, ["python", "count.py"], workdir)
    fake_process = _FakeProcess(started_running=True)
    _install_fake_subprocess(monkeypatch, fake_process)

    started = await tools.run_script_background.ainvoke({"skill_name": "demo", "script_filename": "count.py"})
    job_id = _extract_job_id(started)

    first = await tools.check_script_job.ainvoke({"job_id": job_id})
    assert "Running" in first

    second = await tools.check_script_job.ainvoke({"job_id": job_id})
    assert "Checked too soon" in second
    assert job_id in second

    fake_process.finish(0)
    await tools._script_job._BACKGROUND_JOBS[job_id].runner_task


@pytest.mark.asyncio
async def test_stop_script_job_cancels_running_job_cleanly(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    workdir = tools._state._DEFAULT_WORKDIR
    _stub_prepare_script_execution(monkeypatch, ["python", "count.py"], workdir)
    fake_process = _FakeProcess(started_running=True)
    _install_fake_subprocess(monkeypatch, fake_process)

    started = await tools.run_script_background.ainvoke({"skill_name": "demo", "script_filename": "count.py"})
    job_id = _extract_job_id(started)
    job = tools._script_job._BACKGROUND_JOBS[job_id]

    result = await tools.stop_script_job.ainvoke({"job_id": job_id})

    assert result.startswith("Forcibly stopped")
    assert job.status == "killed"
    assert fake_process.killed is True
    assert job_id not in tools._script_job._BACKGROUND_JOBS


@pytest.mark.asyncio
async def test_cancel_background_script_jobs_for_thread_stops_running_job(monkeypatch, tmp_path) -> None:
    """app.py の on_stop / _stop_thread_generating が使う
    cancel_background_script_jobs_for_thread の回帰テスト。

    停止ボタン押下時、session.current_task.cancel() はメイングラフのタスクにしか
    届かず、asyncio.shield() で保護された run_script_background/
    execute_python_code_background の job.runner_task はそれだけでは止まらない
    （dispatch_agentのcancel_dispatch_agent_jobs_for_threadと同じ理由。
    2026-08-26レビューで発見: 停止ボタンがdispatch_agentしか強制終了しない
    不具合の回帰防止）。stop_script_job ツールと同じ強制終了を、job_idを
    知らない停止ボタン経路からも thread_id 指定で一括適用できることを検証する。
    """
    _setup(monkeypatch, tmp_path, thread_id="thread-1")
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    workdir = tools._state._DEFAULT_WORKDIR
    _stub_prepare_script_execution(monkeypatch, ["python", "count.py"], workdir)
    fake_process = _FakeProcess(started_running=True)
    _install_fake_subprocess(monkeypatch, fake_process)

    started = await tools.run_script_background.ainvoke({"skill_name": "demo", "script_filename": "count.py"})
    job_id = _extract_job_id(started)
    job = tools._script_job._BACKGROUND_JOBS[job_id]

    result = await tools.cancel_background_script_jobs_for_thread("thread-1")

    assert result is True
    assert job.status == "killed"
    assert fake_process.killed is True
    assert job_id not in tools._script_job._BACKGROUND_JOBS


@pytest.mark.asyncio
async def test_cancel_background_script_jobs_for_thread_ignores_other_threads(monkeypatch, tmp_path) -> None:
    """thread_id が一致しないジョブには一切触れない（他セッションを巻き添えにしない）。"""
    _setup(monkeypatch, tmp_path, thread_id="thread-1")
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    workdir = tools._state._DEFAULT_WORKDIR
    _stub_prepare_script_execution(monkeypatch, ["python", "count.py"], workdir)
    fake_process = _FakeProcess(started_running=True)
    _install_fake_subprocess(monkeypatch, fake_process)

    started = await tools.run_script_background.ainvoke({"skill_name": "demo", "script_filename": "count.py"})
    job_id = _extract_job_id(started)
    job = tools._script_job._BACKGROUND_JOBS[job_id]

    result = await tools.cancel_background_script_jobs_for_thread("thread-other")

    assert result is False
    assert job.status == "running"
    assert fake_process.killed is False
    assert job_id in tools._script_job._BACKGROUND_JOBS

    fake_process.finish(0)
    await job.runner_task


@pytest.mark.asyncio
async def test_exception_inside_job_is_returned_as_error_not_lost(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path)
    workdir = tools._state._DEFAULT_WORKDIR
    _stub_prepare_script_execution(monkeypatch, ["python", "count.py"], workdir)
    fake_process = _FakeProcess(exit_code=0)
    fake_process.stdout = _RaisingStreamReader([], fake_process._gate, raise_after=0)
    _install_fake_subprocess(monkeypatch, fake_process)

    result = await tools.run_script_background.ainvoke({"skill_name": "demo", "script_filename": "count.py"})

    assert result.startswith("Error: a problem occurred during the background run: boom")
    assert "Traceback (most recent call last)" in result
    assert tools._script_job._BACKGROUND_JOBS == {}


@pytest.mark.asyncio
async def test_timeout_kills_process_and_returns_timeout_prefixed_result(monkeypatch, tmp_path) -> None:
    """_SCRIPT_BACKGROUND_MAX_RUNTIME_SECONDS（既存の強制終了ロジック）が
    インライン待機の統合後も変わらず機能することの回帰確認。"""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(tools._state, "_SCRIPT_BACKGROUND_MAX_RUNTIME_SECONDS", 0.05)
    workdir = tools._state._DEFAULT_WORKDIR
    _stub_prepare_script_execution(monkeypatch, ["python", "count.py"], workdir)
    fake_process = _FakeProcess(started_running=True)
    _install_fake_subprocess(monkeypatch, fake_process)

    result = await tools.run_script_background.ainvoke({"skill_name": "demo", "script_filename": "count.py"})

    assert "job_id=" not in result
    assert "seconds and was forcibly stopped" in result
    assert fake_process.killed is True
    assert tools._script_job._BACKGROUND_JOBS == {}


@pytest.mark.asyncio
async def test_run_script_background_unknown_skill_creates_no_job(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(tools._state, "_SKILLS_ROOTS", [tmp_path])  # 実在するがスキルが無いルート
    captured = _install_fake_subprocess(monkeypatch, _FakeProcess())

    result = await tools.run_script_background.ainvoke({"skill_name": "no-such-skill", "script_filename": "x.py"})

    assert result.startswith("Error:")
    assert tools._script_job._BACKGROUND_JOBS == {}
    assert "cmd" not in captured


@pytest.mark.asyncio
async def test_execute_python_code_background_without_plan_approval_creates_no_job(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path)
    captured = _install_fake_subprocess(monkeypatch, _FakeProcess())

    result = await tools.execute_python_code_background.ainvoke({"code": "print(1)"})

    assert result.startswith("Error:")
    assert tools._script_job._BACKGROUND_JOBS == {}
    assert "cmd" not in captured


@pytest.mark.asyncio
async def test_execute_python_code_background_empty_code_creates_no_job(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path)
    tools.cl.user_session.set("plan_approved", True)
    captured = _install_fake_subprocess(monkeypatch, _FakeProcess())

    result = await tools.execute_python_code_background.ainvoke({"code": "   "})

    assert result.startswith("Error:")
    assert tools._script_job._BACKGROUND_JOBS == {}
    assert "cmd" not in captured
