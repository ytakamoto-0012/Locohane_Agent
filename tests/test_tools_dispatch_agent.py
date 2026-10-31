"""dispatch_agent / check_dispatch_agent_job / stop_dispatch_agent_job の回帰テスト。

設計変更の経緯: 当初は run_script_background と同じ「即座に job_id を返し、
LLM自身が check_dispatch_agent_job でポーリングする」方式で実装したが、
実運用でのフィードバックにより「LLMが自分でポーリングするとその都度トークンを
消費する」問題が判明した。そのため dispatch_agent は現在、ジョブ完了
まで（安全上限まで）ツール呼び出し内でブロックし、最終結果を1回のLLM往復で
直接返す設計に変わっている。安全上限を超えた場合のみ、従来どおり job_id を
返すフォールバック経路が残る。人間向けの進捗表示は cl.Message による直接push
（LLM非経由・トークン消費ゼロ）に置き換わった。

このファイルのテストはこの契約を検証する。
"""

import asyncio
import importlib
import json
import re

import pytest
from langchain_core.messages import HumanMessage

from src import subagent, tools

# tools.write_scratch_note は@toolオブジェクトで上書き済みのため、モジュール自体は
# importlib で sys.modules から直接取得する。
write_scratch_note_module = importlib.import_module("src.tools.write_scratch_note")


class _FakeUserSession:
    def __init__(self, thread_id: str = "thread-1"):
        self._data: dict = {"thread_id": thread_id}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _FakeMessage:
    """tools.cl.Message の差し替え。send()/update() された内容をそれぞれ記録する。"""

    _sent: list | None = None  # _install_fake_message が差し替える
    _updated: list | None = None  # 同上

    def __init__(self, content: str = "", **kwargs) -> None:
        self.content = content

    async def send(self) -> None:
        if _FakeMessage._sent is not None:
            _FakeMessage._sent.append(self.content)

    async def update(self) -> None:
        if _FakeMessage._updated is not None:
            _FakeMessage._updated.append(self.content)


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


def _setup(monkeypatch, tmp_path=None, thread_id: str = "thread-1") -> None:
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", object())
    monkeypatch.setattr(
        tools._state,
        "_AGENT_TYPES",
        {"explore": tools._state.ResolvedAgentType(description="", system_prompt="", tools=[])},
    )
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession(thread_id))
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_SEMAPHORES", {})
    monkeypatch.setattr(tools._dispatch_agent_job, "_DISPATCH_AGENT_JOBS", {})
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_JOB_RETENTION_SECONDS", 1800)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS", 0)
    # 既定は「タイムアウトせずすぐ終わる」テストが安全上限に絶対到達しないよう
    # 十分大きめにしつつ、バグで本当にハングした場合にテストが1800秒待たされ
    # ないよう短めにしておく。安全上限フォールバックを検証するテストでは
    # さらに小さい値へ個別に上書きする。
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 5)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS", 5)
    _install_fake_message(monkeypatch)
    if tmp_path is not None:
        workdir = tmp_path / "workdir"
        workdir.mkdir(exist_ok=True)
        monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)


def _extract_job_id(text: str) -> str:
    m = re.search(r"job_id=(\w+)", text)
    assert m, f"job_id が含まれていません: {text!r}"
    return m.group(1)


_TC_ID_COUNTER = 0


async def _invoke_dispatch_agent(**kwargs):
    """dispatch_agent は InjectedToolCallId（tool_call_id）を持つため、単純な
    引数辞書ではなく ToolCall 形式（name/args/id/type）で呼ぶ必要がある
    （src/tools/dispatch_agent.py 参照。tool_call_id を run_id として使い、
    強制停止時に孤立tool_callから退避先ファイルパスを再計算するため）。
    テスト全体で使う呼び出しラッパー。

    ToolCall形式で ainvoke すると langchain-core が戻り値を ToolMessage へ
    自動ラップする（引数が単純な辞書の場合は生の文字列のまま返る、という
    従来の呼び出し方との違い）。既存テストは戻り値が文字列であることを
    前提にしているため、ここで .content を取り出して統一する。
    """
    global _TC_ID_COUNTER
    _TC_ID_COUNTER += 1
    result = await tools.dispatch_agent.ainvoke(
        {"name": "dispatch_agent", "args": kwargs, "id": f"test-tc-{_TC_ID_COUNTER}", "type": "tool_call"}
    )
    return result.content if hasattr(result, "content") else result


@pytest.mark.asyncio
async def test_normal_completion_returns_final_result_directly(monkeypatch, tmp_path) -> None:
    """安全上限内に終わる通常ケースでは、1回の呼び出しで最終結果がそのまま返る。"""
    _setup(monkeypatch, tmp_path=tmp_path)
    tools.cl.user_session.set("main_agent_tool_guard_call_count", {"Glob": 1})

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        return f"done:{task.splitlines()[-1]}"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    result = await _invoke_dispatch_agent(task="investigate", agent_type="explore")

    assert result == "done:investigate"
    assert "job_id=" not in result
    assert tools._dispatch_agent_job._DISPATCH_AGENT_JOBS == {}  # 完了時点でレジストリから取り除かれている
    # 安全上限フォールバックを経ない通常完了では、同一ターン内での
    # 複数回delegateを妨げないよう従来通りガードカウンタがリセットされる。
    assert tools.cl.user_session.get("main_agent_tool_guard_call_count") is None


@pytest.mark.asyncio
async def test_background_dispatch_injects_work_dir_hint_into_task(monkeypatch, tmp_path) -> None:
    """実際の作業ディレクトリをtaskの先頭に事実として与える。"""
    _setup(monkeypatch, tmp_path=tmp_path)
    captured: dict = {}

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        captured["task"] = task
        return "ok"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    await _invoke_dispatch_agent(task="investigate", agent_type="explore")

    # work_dir未設定時、_resolve_workdir()はdefault_workdir自体ではなく
    # スレッド専用フォルダ（_resolve_exec_workdir()）を返す（2026-08-29修正、
    # 別スレッドのデータを拾わないための分離）。
    expected_work_dir = tools._workdir._resolve_exec_workdir()
    expected_info = json.dumps({"absolute_path": str(expected_work_dir)}, ensure_ascii=False)
    assert captured["task"] == f"[Working directory]\n{expected_info}\n\ninvestigate"


@pytest.mark.asyncio
async def test_safety_cap_fallback_returns_job_id_and_keeps_job_running(monkeypatch, tmp_path) -> None:
    """安全上限を超えるとjob_idを返してターンを終えるが、ジョブ自体は動き続ける（asyncio.shield）。"""
    _setup(monkeypatch, tmp_path=tmp_path)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    gate = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        await gate.wait()
        return "done"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    started = await _invoke_dispatch_agent(task="t", agent_type="explore")
    assert "job_id=" in started
    job_id = _extract_job_id(started)

    job = tools._dispatch_agent_job._DISPATCH_AGENT_JOBS[job_id]
    assert job.status == "running"
    assert not job.runner_task.done()  # shieldにより安全上限超過でもキャンセルされていない

    gate.set()
    await job.runner_task
    result = await tools.check_dispatch_agent_job.ainvoke({"job_id": job_id})
    assert result == "done"


@pytest.mark.asyncio
async def test_fallback_completion_does_not_reset_main_agent_tool_guard(monkeypatch, tmp_path) -> None:
    """安全上限フォールバック後にジョブが完了しても、別ターンのツールガード
    カウンタを横からリセットしない（cross-turnレースの回帰確認）。"""
    _setup(monkeypatch, tmp_path=tmp_path)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    gate = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        await gate.wait()
        return "done"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    started = await _invoke_dispatch_agent(task="t", agent_type="explore")
    assert "job_id=" in started
    job_id = _extract_job_id(started)
    job = tools._dispatch_agent_job._DISPATCH_AGENT_JOBS[job_id]

    # フォールバック発生後、同一セッションで別の新しいターンが始まり、
    # 既にツールガード上限まで積んでいる状態を模す。
    tools.cl.user_session.set("main_agent_tool_guard_call_count", {"Glob": 1})

    gate.set()
    await job.runner_task

    assert job.turn_still_waiting is False
    # 旧ジョブの完了によって新ターン側のカウンタが横から消されていないこと。
    assert tools.cl.user_session.get("main_agent_tool_guard_call_count") == {"Glob": 1}


@pytest.mark.asyncio
async def test_progress_is_pushed_without_llm_and_stops_after_completion(monkeypatch, tmp_path) -> None:
    """進捗pushはLLMを介さず cl.Message で直接送られ、ジョブ完了後は止まる。
    会話が「実行中です」で埋まらないよう、1回目のみ .send() で新規メッセージを
    作り、2回目以降は同じメッセージを .update() で書き換える
    （新規メッセージを積み増さない）（2026-09-07 ユーザー報告）。"""
    _setup(monkeypatch, tmp_path=tmp_path)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS", 0.02)
    sent, updated = _install_fake_message_with_updates(monkeypatch)
    gate = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, on_iteration=None, **kwargs):
        if on_iteration is not None:
            on_iteration(1, max_iterations)
        await gate.wait()
        return "done"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    async def _release_after_updates():
        # 1回目の .send() の後、.update() が実際に届くのを待ってからジョブを終わらせる。
        # 固定時間（以前は 0.08 秒）だけ待つと、Windows ではタイマーの粒度（約15.6ms）のため
        # asyncio.sleep(0.02) が約31msかかり、.update() の前にジョブが終わってしまうことがあった。
        deadline = asyncio.get_running_loop().time() + 5
        while not updated and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        gate.set()

    release_task = asyncio.create_task(_release_after_updates())
    result = await _invoke_dispatch_agent(task="t", agent_type="explore")
    await release_task

    assert result == "done"
    # 新規メッセージ(.send())は1回だけ、以降は同一メッセージの更新(.update())になる。
    assert len(sent) == 1
    assert len(updated) >= 1
    assert any("反復 1/" in msg for msg in updated)
    assert any("経過" in msg for msg in updated)

    updated_count_after_completion = len(updated)
    await asyncio.sleep(0.06)  # push間隔を跨いでも増えないことを確認
    assert len(sent) == 1
    assert len(updated) == updated_count_after_completion


@pytest.mark.asyncio
async def test_cross_session_access_is_rejected(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path=tmp_path, thread_id="session-a")
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    gate = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        await gate.wait()
        return "done"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)
    started = await _invoke_dispatch_agent(task="t", agent_type="explore")
    job_id = _extract_job_id(started)

    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession("session-b"))
    check_result = await tools.check_dispatch_agent_job.ainvoke({"job_id": job_id})
    assert "does not belong to the current session" in check_result
    stop_result = await tools.stop_dispatch_agent_job.ainvoke({"job_id": job_id})
    assert "does not belong to the current session" in stop_result

    gate.set()
    await tools._dispatch_agent_job._DISPATCH_AGENT_JOBS[job_id].runner_task


@pytest.mark.asyncio
async def test_poll_rate_limiting_rejects_too_soon(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path=tmp_path)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS", 60)
    gate = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        await gate.wait()
        return "done"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)
    started = await _invoke_dispatch_agent(task="t", agent_type="explore")
    job_id = _extract_job_id(started)

    first = await tools.check_dispatch_agent_job.ainvoke({"job_id": job_id})
    assert "Running" in first

    second = await tools.check_dispatch_agent_job.ainvoke({"job_id": job_id})
    assert "Checked too soon" in second
    assert job_id in second

    gate.set()
    await tools._dispatch_agent_job._DISPATCH_AGENT_JOBS[job_id].runner_task


@pytest.mark.asyncio
async def test_stop_cancels_running_job_cleanly(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path=tmp_path)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    started_running = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        started_running.set()
        await asyncio.sleep(1000)  # stop_dispatch_agent_job にキャンセルされる想定

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)
    started = await _invoke_dispatch_agent(task="t", agent_type="explore")
    job_id = _extract_job_id(started)
    await started_running.wait()

    job = tools._dispatch_agent_job._DISPATCH_AGENT_JOBS[job_id]
    result = await tools.stop_dispatch_agent_job.ainvoke({"job_id": job_id})

    assert "Forcibly stopped" in result
    assert job.status == "killed"
    assert job.runner_task.cancelled() or job.runner_task.done()
    assert job_id not in tools._dispatch_agent_job._DISPATCH_AGENT_JOBS


@pytest.mark.asyncio
async def test_cancel_dispatch_agent_jobs_for_thread_stops_running_job(monkeypatch, tmp_path) -> None:
    """app.py の on_stop / _stop_thread_generating が使う cancel_dispatch_agent_jobs_for_thread の回帰テスト。

    停止ボタン押下時、session.current_task.cancel() はメイングラフのタスクにしか
    届かず、asyncio.shield() で保護された dispatch_agent の job.runner_task は
    それだけでは止まらない（2026-08-26 ユーザー報告）。stop_dispatch_agent_job
    ツールと同じ強制終了を、job_idを知らない停止ボタン経路からも thread_id 指定で
    一括適用できることを検証する。
    """
    _setup(monkeypatch, tmp_path=tmp_path, thread_id="thread-1")
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    started_running = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        started_running.set()
        await asyncio.sleep(1000)  # cancel_dispatch_agent_jobs_for_thread にキャンセルされる想定

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)
    started = await _invoke_dispatch_agent(task="t", agent_type="explore")
    job_id = _extract_job_id(started)
    await started_running.wait()

    job = tools._dispatch_agent_job._DISPATCH_AGENT_JOBS[job_id]
    result = await tools.cancel_dispatch_agent_jobs_for_thread("thread-1")

    assert result is True
    assert job.status == "killed"
    assert job.turn_still_waiting is False
    assert job.runner_task.cancelled() or job.runner_task.done()
    assert job_id not in tools._dispatch_agent_job._DISPATCH_AGENT_JOBS


@pytest.mark.asyncio
async def test_cancel_dispatch_agent_jobs_for_thread_cancels_concurrently(monkeypatch, tmp_path) -> None:
    """同一スレッドで複数のdispatch_agentジョブが並行実行中でも、1件ずつ直列に
    cancel()→awaitするのではなく全件へ先にcancel()を発行してから並行して
    後始末を待つことを検証する（2026-08-26レビュー: 直列だと停止ボタンの
    反応が同時実行ジョブ数に比例して遅延する不具合の回帰防止）。

    各ジョブのrunner_taskがCancelledErrorを実際に受け取るまでの間、
    asyncio.Event で「まだ後始末中」を可視化し、2件目の後始末が始まった
    時点で1件目がまだ完了していない（＝直列待ちではなく並行して進んでいる）
    ことを確認する。
    """
    _setup(monkeypatch, tmp_path=tmp_path, thread_id="thread-1")
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_MAX_PARALLEL", 2)

    cleanup_started = [asyncio.Event(), asyncio.Event()]
    both_cleanups_started = asyncio.Event()
    started_running = [asyncio.Event(), asyncio.Event()]

    def _make_fake_run_subagent(idx: int):
        async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
            started_running[idx].set()
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
                cleanup_started[idx].set()
                if all(e.is_set() for e in cleanup_started):
                    both_cleanups_started.set()
                # 後始末が並行して進んでいることを検証するため、もう一方の
                # 後始末開始を待ってから実際にキャンセルを完了させる。
                await asyncio.wait_for(both_cleanups_started.wait(), timeout=5)
                raise

        return fake_run_subagent

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", _make_fake_run_subagent(0))
    started_0 = await _invoke_dispatch_agent(task="t0", agent_type="explore")
    job_id_0 = _extract_job_id(started_0)
    await started_running[0].wait()

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", _make_fake_run_subagent(1))
    started_1 = await _invoke_dispatch_agent(task="t1", agent_type="explore")
    job_id_1 = _extract_job_id(started_1)
    await started_running[1].wait()

    result = await asyncio.wait_for(tools.cancel_dispatch_agent_jobs_for_thread("thread-1"), timeout=5)

    assert result is True
    assert job_id_0 not in tools._dispatch_agent_job._DISPATCH_AGENT_JOBS
    assert job_id_1 not in tools._dispatch_agent_job._DISPATCH_AGENT_JOBS


@pytest.mark.asyncio
async def test_cancel_dispatch_agent_jobs_for_thread_ignores_other_threads(monkeypatch, tmp_path) -> None:
    """thread_id が一致しないジョブには一切触れない（他セッションを巻き添えにしない）。"""
    _setup(monkeypatch, tmp_path=tmp_path, thread_id="thread-1")
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    started_running = asyncio.Event()
    gate = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        started_running.set()
        await gate.wait()
        return "ok"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)
    started = await _invoke_dispatch_agent(task="t", agent_type="explore")
    job_id = _extract_job_id(started)
    await started_running.wait()

    job = tools._dispatch_agent_job._DISPATCH_AGENT_JOBS[job_id]
    await tools.cancel_dispatch_agent_jobs_for_thread("thread-other")

    assert job.status == "running"
    assert not job.runner_task.done()
    assert job_id in tools._dispatch_agent_job._DISPATCH_AGENT_JOBS

    gate.set()
    await job.runner_task


@pytest.mark.asyncio
async def test_exception_inside_job_is_returned_as_error_not_lost(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path=tmp_path)

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)
    result = await _invoke_dispatch_agent(task="t", agent_type="explore")

    assert result.startswith("Error: the sub-agent run failed: boom\n")
    assert "Traceback (most recent call last)" in result
    assert tools._dispatch_agent_job._DISPATCH_AGENT_JOBS == {}


@pytest.mark.asyncio
async def test_exception_with_empty_str_falls_back_to_type_name(monkeypatch, tmp_path) -> None:
    """本番incident・2026-08-08: str(e) が空文字列になる例外（asyncio.TimeoutError() 等）で
    「エラー: サブエージェントの実行に失敗しました: 」と原因が一切分からない文言に
    なっていた事象の回帰テスト（issue/20260808_022438_dispatch_agent_background_failure.md）。
    """
    _setup(monkeypatch, tmp_path=tmp_path)

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        raise TimeoutError()  # str(TimeoutError()) == ""

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)
    result = await _invoke_dispatch_agent(task="t", agent_type="explore")

    assert result.startswith("Error: the sub-agent run failed: TimeoutError\n")
    assert "Traceback (most recent call last)" in result


def test_scratch_notes_path_for_run_is_isolated_per_run(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path=tmp_path)
    path_a = write_scratch_note_module._scratch_notes_path_for_run("run-a")
    path_b = write_scratch_note_module._scratch_notes_path_for_run("run-b")
    assert path_a != path_b
    assert "run-a" in path_a.name
    assert "run-b" in path_b.name


def test_sanitize_run_id_keeps_safe_characters_unchanged() -> None:
    """実測されるtool_call_id（英数字のみ）はそのまま通ること。"""
    assert write_scratch_note_module.sanitize_run_id("URn3f6oVdPomA1pBa78USJgyNqniZzWT") == "URn3f6oVdPomA1pBa78USJgyNqniZzWT"


def test_sanitize_run_id_replaces_unsafe_characters() -> None:
    """パス区切り文字等、ファイル名として不適切な文字は保証されないフォーマット
    （生成元のLLM/サーバー実装依存）のため防御的に置換する。"""
    assert write_scratch_note_module.sanitize_run_id("../etc/passwd") == "___etc_passwd"
    assert write_scratch_note_module.sanitize_run_id("a b\\c") == "a_b_c"


def test_sanitize_run_id_empty_string_falls_back_to_unknown() -> None:
    assert write_scratch_note_module.sanitize_run_id("") == "unknown"


@pytest.mark.asyncio
async def test_cancel_via_stop_button_rescues_conversation_to_scratch_note(monkeypatch, tmp_path) -> None:
    """停止ボタン経由(cancel_dispatch_agent_jobs_for_thread)の強制終了時、
    run_subagentのon_cancelledコールバックに渡された会話履歴が
    write_scratch_noteと同じ_scratch_notes_<run_id>.mdへ退避されることを検証する。

    停止ボタンによる強制終了ではサブエージェントのmessages（ローカル変数）が
    完全に破棄されていた問題への対処（_make_rescue_on_cancelled参照）。
    """
    _setup(monkeypatch, tmp_path=tmp_path, thread_id="thread-1")
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.05)
    started_running = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, on_cancelled=None, **kwargs):
        started_running.set()
        try:
            await asyncio.sleep(1000)
        except asyncio.CancelledError:
            if on_cancelled is not None:
                on_cancelled([HumanMessage(content="rescued-task-marker")], subagent.RESCUE_REASON_CANCELLED)
            raise

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)
    started = await _invoke_dispatch_agent(task="t", agent_type="explore")
    job_id = _extract_job_id(started)
    await started_running.wait()

    run_id = tools._dispatch_agent_job._DISPATCH_AGENT_JOBS[job_id].run_id
    result = await tools.cancel_dispatch_agent_jobs_for_thread("thread-1")

    assert result is True
    note_path = tools._dispatch_agent_job._scratch_notes_path_for_run(run_id)
    assert note_path.is_file()
    assert "rescued-task-marker" in note_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cancelling_caller_task_directly_still_rescues_before_propagating(monkeypatch, tmp_path) -> None:
    """メイングラフのタスク自体がキャンセルされる経路（session.current_task.cancel()）
    でも、緊急退避が完了してからCancelledErrorが呼び出し元へ伝播することを検証する。

    実運用のレース: 停止ボタン押下時、Chainlitのstop()ハンドラは
    session.current_task.cancel()（メイングラフのタスク＝dispatch_agentを
    呼び出しているタスクそのもの）を呼んだ"後"にon_stop()経由で
    cancel_dispatch_agent_jobs_for_thread()（job.runner_task自身を個別に
    cancel()する）を呼ぶ。単一イベントループ上ではこの2つのcancel()は
    スケジュールされる順序が固定されており、"session.current_task.cancel()"
    の方が"job.runner_task.cancel()"より確実に先に配送される。

    このテストは cancel_dispatch_agent_jobs_for_thread を一切呼ばず、
    dispatch_agent(...) をawaitしている外側のタスク自体を直接cancel()する
    （asyncio.wait_for(asyncio.shield(job.runner_task), ...) の待機点へ
    CancelledErrorが届く、実運用と同じ経路）。dispatch_agent側がjob.runner_task
    自身のキャンセル・後始末（緊急退避書き込み含む）を待たずにそのまま
    CancelledErrorを伝播させてしまうと、app.py側の孤立tool_call検出
    （退避ファイルの存在確認）がまだファイルが作られていない状態で走ってしまう
    （2026-09-09 実測で確認したレース。src/tools/dispatch_agent.py の
    except asyncio.CancelledError 参照）。
    """
    _setup(monkeypatch, tmp_path=tmp_path, thread_id="thread-1")
    # wait_for自体のタイムアウトでは発火させたくないため、テストが現実的な
    # 時間で終わる範囲で十分大きくしておく。
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 5)
    started_running = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, on_cancelled=None, **kwargs):
        started_running.set()
        try:
            await asyncio.sleep(1000)
        except asyncio.CancelledError:
            if on_cancelled is not None:
                on_cancelled([HumanMessage(content="rescued-task-marker")], subagent.RESCUE_REASON_CANCELLED)
            raise

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    outer_task = asyncio.create_task(_invoke_dispatch_agent(task="t", agent_type="explore"))
    await started_running.wait()
    await asyncio.sleep(0)  # job.runner_taskの起動・run_subagent呼び出しが完了するのを待つ

    job = next(iter(tools._dispatch_agent_job._DISPATCH_AGENT_JOBS.values()))
    run_id = job.run_id

    # cancel_dispatch_agent_jobs_for_thread は呼ばない。session.current_task
    # に相当する外側のタスクだけを直接cancel()し、job.runner_task自身は
    # ここでは一切触れない（実運用の順序どおり、こちらのcancel()が先に届く）。
    outer_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer_task

    # outer_taskのCancelledErrorが呼び出し元まで伝播した"時点で"、既に
    # job.runner_task自身の後始末（緊急退避）が完了していること。
    note_path = tools._dispatch_agent_job._scratch_notes_path_for_run(run_id)
    assert note_path.is_file()
    assert "rescued-task-marker" in note_path.read_text(encoding="utf-8")
    assert job.runner_task.cancelled() or job.runner_task.done()


def test_dispatch_agent_family_is_base_only_not_subagent() -> None:
    assert tools.dispatch_agent in tools.registry._BASE_TOOLS
    assert tools.check_dispatch_agent_job in tools.registry._BASE_TOOLS
    assert tools.stop_dispatch_agent_job in tools.registry._BASE_TOOLS
    assert tools.dispatch_agent not in tools.registry._SUBAGENT_TOOLS
    assert tools.check_dispatch_agent_job not in tools.registry._SUBAGENT_TOOLS
    assert tools.stop_dispatch_agent_job not in tools.registry._SUBAGENT_TOOLS
