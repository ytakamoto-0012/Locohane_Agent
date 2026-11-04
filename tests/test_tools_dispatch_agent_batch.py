"""dispatch_agent_batch（ハーネス側でのグループ分割・並列委譲）の回帰テスト。

低パラメータモデルは「全グループ分の dispatch_agent を1回の応答で並べる」指示を
安定して守れなかった（evals/tuning_log.md の並列発行チューニング参照）。
dispatch_agent_batch はファイル一覧取得・グループ分け・担当ファイル割り当て・
並列実行をハーネス側で行うため、その機械的な部分を検証する。
"""

import asyncio
import sys

import pytest

from src import tools

# パッケージ属性 tools.dispatch_agent_batch は @tool オブジェクトで上書きされているため、
# モジュール本体は sys.modules から取る。
_BATCH_MODULE = sys.modules["src.tools.dispatch_agent_batch"]


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _FakeMessage:
    def __init__(self, content: str = "", **kwargs) -> None:
        self.content = content

    async def send(self) -> None:
        pass

    async def update(self) -> None:
        pass


def _setup(monkeypatch, tmp_path, names: list[str]) -> tuple:
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", object())
    monkeypatch.setattr(
        tools._state,
        "_AGENT_TYPES",
        {"worker": tools._state.ResolvedAgentType(description="", system_prompt="", tools=[])},
    )
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    monkeypatch.setattr(tools.cl, "Message", _FakeMessage)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_SEMAPHORES", {})
    monkeypatch.setattr(tools._dispatch_agent_job, "_DISPATCH_AGENT_JOBS", {})
    monkeypatch.setattr(tools._state, "_SUBAGENT_MAX_ITERATIONS", 100)
    workdir = tmp_path / "workdir"
    images = workdir / "images"
    images.mkdir(parents=True)
    for name in names:
        (images / name).write_bytes(name.encode("utf-8"))  # 中身を変える（同一内容はまとめられるため）
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)
    notes: list[dict] = []

    class _FakeNoteTool:
        def invoke(self, args):
            notes.append(args)
            return "書き込みました"

    monkeypatch.setattr(_BATCH_MODULE, "write_thread_note", _FakeNoteTool())
    return images, notes


_TC_ID_COUNTER = 0


async def _invoke(**kwargs) -> str:
    global _TC_ID_COUNTER
    _TC_ID_COUNTER += 1
    result = await tools.dispatch_agent_batch.ainvoke(
        {"name": "dispatch_agent_batch", "args": kwargs, "id": f"test-batch-{_TC_ID_COUNTER}", "type": "tool_call"}
    )
    return result.content if hasattr(result, "content") else result


def _capture_tasks(monkeypatch) -> list[str]:
    captured: list[str] = []

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        captured.append(task)
        return "処理件数: OK"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)
    return captured


@pytest.mark.asyncio
async def test_batch_splits_files_by_name_into_groups(monkeypatch, tmp_path) -> None:
    names = [f"IMG_{i:03d}.png" for i in range(7)]
    images, notes = _setup(monkeypatch, tmp_path, list(reversed(names)) + ["memo.txt"])
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="画像を解析して md へ書き出す", agent_type="worker", pattern="*.png", path=str(images), group_size=3)

    assert len(captured) == 3
    # 各グループの task には共通指示と、ファイル名順に割り当てた担当ファイルの絶対パスが入る。
    by_group = sorted(captured, key=lambda t: t.split("(group ")[1])
    expected = [names[0:3], names[3:6], names[6:7]]
    for task, group in zip(by_group, expected):
        assert "画像を解析して md へ書き出す" in task
        for name in group:
            assert str((images / name).resolve()) in task
        assert "memo.txt" not in task
    assert "Split the 7 files" in result and "into 3 groups" in result
    assert "done 3" in result
    assert len(notes) == 1 and "dispatch_agent_batch results" in notes[0]["topic"]


@pytest.mark.asyncio
async def test_batch_runs_groups_concurrently_when_max_parallel_allows(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, [f"a{i}.jpg" for i in range(4)])
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_MAX_PARALLEL", 0)
    concurrent = 0
    max_concurrent = 0

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return "ok"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    await _invoke(task="t", agent_type="worker", pattern="*.jpg", path=str(images), group_size=1)

    assert max_concurrent == 4


@pytest.mark.asyncio
async def test_batch_brace_pattern_matches_case_insensitively_sorted(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, ["b.JPG", "a.png", "c.heic"])
    captured = _capture_tasks(monkeypatch)

    await _invoke(task="t", agent_type="worker", pattern="*.{jpg,png}", path=str(images), group_size=10)

    assert len(captured) == 1
    task = captured[0]
    assert task.index("a.png") < task.index("b.JPG")
    assert "c.heic" not in task


@pytest.mark.asyncio
async def test_batch_returns_error_without_jobs_when_no_files(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, ["a.txt"])
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images))

    assert result.startswith("Error:")
    assert captured == []
    assert tools._dispatch_agent_job._DISPATCH_AGENT_JOBS == {}


@pytest.mark.asyncio
async def test_batch_rejects_unknown_agent_type(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, ["a.png"])

    result = await _invoke(task="t", agent_type="nope", pattern="*.png", path=str(images))

    assert result.startswith("Error: unknown agent_type")


@pytest.mark.asyncio
async def test_batch_truncates_long_group_results_in_return_value(monkeypatch, tmp_path) -> None:
    images, notes = _setup(monkeypatch, tmp_path, ["a.png"])

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        return "長い結果" * 500

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images))

    assert "... (rest omitted)" in result
    assert len(result) < 1500
    # 全文は thread note 側へ保存される。
    assert "長い結果" * 500 in notes[0]["content"]


@pytest.mark.asyncio
async def test_batch_clamps_group_size_to_max_iterations(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, [f"f{i}.png" for i in range(5)])
    monkeypatch.setattr(tools._state, "_SUBAGENT_MAX_ITERATIONS", 2)
    captured = _capture_tasks(monkeypatch)

    await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images), group_size=50)

    assert len(captured) == 3


@pytest.mark.asyncio
async def test_batch_absolute_pattern_returns_error_instead_of_raising(monkeypatch, tmp_path) -> None:
    """Path.glob は絶対パスの pattern を NotImplementedError で拒否する。ToolNode の既定
    エラーハンドラはこれを再送出してターン全体を落とすため、エラー文字列で返すこと。"""
    images, _ = _setup(monkeypatch, tmp_path, ["a.png"])
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", pattern=str(images / "*.png"), path=str(images))

    assert result.startswith("Error: Invalid pattern")
    assert "pass the folder in path" in result
    assert captured == []


@pytest.mark.asyncio
async def test_batch_rejects_too_many_groups_without_starting_jobs(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, [f"f{i}.png" for i in range(4)])
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BATCH_MAX_GROUPS", 3)
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images), group_size=1)

    assert result.startswith("Error:") and "4 groups" in result
    assert captured == []
    assert tools._dispatch_agent_job._DISPATCH_AGENT_JOBS == {}


@pytest.mark.asyncio
async def test_batch_max_groups_zero_means_unlimited(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, [f"f{i}.png" for i in range(4)])
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BATCH_MAX_GROUPS", 0)
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images), group_size=1)

    assert not result.startswith("Error:")
    assert len(captured) == 4


@pytest.mark.asyncio
async def test_batch_lists_files_only_for_failed_groups(monkeypatch, tmp_path) -> None:
    images, notes = _setup(monkeypatch, tmp_path, ["a.png", "b.png"])

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        if "a.png" in task:
            raise RuntimeError("boom")
        return "処理件数: 1"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images), group_size=1)

    failed, ok = result.split("### ")[1:3]
    ok = ok.split("\n\n")[0]  # 末尾の thread note 案内・再委任案内を除いた、グループ2のブロックだけ
    assert failed.startswith("Group 1") and ": error" in failed
    # 再委任でLLMがファイル名を推測しないよう、失敗グループには担当ファイルの絶対パスが載る。
    assert f"- {(images / 'a.png').resolve()}" in failed
    assert ok.startswith("Group 2") and ": done" in ok
    assert "Assigned files" not in ok
    assert "re-delegate with dispatch_agent" in result
    # thread note には全グループの担当ファイルが残る。
    assert str((images / "b.png").resolve()) in notes[0]["content"]


@pytest.mark.asyncio
async def test_batch_timeout_returns_completed_results_and_pending_job_ids(monkeypatch, tmp_path) -> None:
    """安全上限に達しても、それまでに完了したグループの結果を捨てないこと。"""
    images, notes = _setup(monkeypatch, tmp_path, ["a.png", "b.png"])
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 0.2)
    release = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        if "b.png" in task:
            await release.wait()
            return "遅いグループ"
        return "速いグループ"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images), group_size=1)

    jobs = tools._dispatch_agent_job._DISPATCH_AGENT_JOBS
    assert len(jobs) == 1
    (pending_id, pending_job), = jobs.items()
    assert "速いグループ" in result
    assert "done 1" in result and "running 1" in result
    assert f"job_id={pending_id}" in result
    assert "check_dispatch_agent_job" in result
    assert f"- {(images / 'b.png').resolve()}" in result
    assert len(notes) == 1 and "速いグループ" in notes[0]["content"]
    release.set()
    await pending_job.runner_task
    assert pending_job.status == "completed"


@pytest.mark.asyncio
async def test_batch_pushes_single_aggregated_progress_message(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, [f"f{i}.png" for i in range(3)])
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_MAX_PARALLEL", 1)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS", 0.01)
    sent: list = []
    contents: list[str] = []

    class _RecordingMessage(_FakeMessage):
        async def send(self) -> None:
            sent.append(self)
            contents.append(self.content)

        async def update(self) -> None:
            contents.append(self.content)

    monkeypatch.setattr(tools.cl, "Message", _RecordingMessage)

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        await asyncio.sleep(0.05)
        return "ok"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images), group_size=1)

    # グループごとの個別メッセージは出さず、1件のメッセージを書き換え続ける。
    assert len(sent) == 1
    assert any("順番待ち 2" in c for c in contents)


def test_single_job_progress_shows_waiting_until_semaphore_acquired(monkeypatch) -> None:
    monkeypatch.setattr(tools._dispatch_agent_job, "_scratch_notes_path_for_run", lambda run_id: __import__("pathlib").Path("__nonexistent__"))
    job = tools._dispatch_agent_job._DispatchAgentJob(
        thread_id="t", run_id="r", agent_type="worker", task_preview="", started_at=0.0,
        status="running", result=None, error_message=None, max_iterations=10,
    )

    assert tools._dispatch_agent_job._format_dispatch_agent_progress(job, "j").startswith("Waiting in line")
    job.run_started_at = __import__("time").monotonic()
    assert tools._dispatch_agent_job._format_dispatch_agent_progress(job, "j").startswith("Running (elapsed 0 s")


def test_progress_shows_current_activity(monkeypatch) -> None:
    """単体・一括とも、進捗に今している動作（Stepバッジ名）を出す。"""
    monkeypatch.setattr(tools._dispatch_agent_job, "_scratch_notes_path_for_run", lambda run_id: __import__("pathlib").Path("__nonexistent__"))
    job = tools._dispatch_agent_job._DispatchAgentJob(
        thread_id="t", run_id="r", agent_type="worker", task_preview="", started_at=0.0,
        status="running", result=None, error_message=None, max_iterations=10,
        run_started_at=__import__("time").monotonic(),
    )

    assert " [Tool" not in tools._dispatch_agent_job._format_dispatch_agent_progress(job, "j")
    job.current_activity = ("tools", "Read")
    assert "[Tool: Read]" in tools._dispatch_agent_job._format_dispatch_agent_progress(job, "j")

    job.runner_task = type("_T", (), {"done": lambda self: False})()
    # 一括の進捗は画面専用で、セッションの表示言語（セッション外は既定の日本語）で出す。
    assert "【ツール: Read】" in _BATCH_MODULE._format_batch_progress([("j", job)], 0.0)


def _activity_job():
    return tools._dispatch_agent_job._DispatchAgentJob(
        thread_id="t", run_id="r", agent_type="worker", task_preview="", started_at=0.0,
        status="running", result=None, error_message=None, max_iterations=10,
    )


@pytest.mark.asyncio
async def test_progress_push_wakes_early_when_activity_changes(monkeypatch) -> None:
    """2回目以降の進捗pushは、動作が変わったら push 間隔を待たずに（最短間隔は空けて）書き換える。"""
    job_module = tools._dispatch_agent_job
    monkeypatch.setattr(job_module, "_ACTIVITY_PUSH_MIN_INTERVAL_SECONDS", 0.01)
    idle, changed = _activity_job(), _activity_job()
    changed.activity_changed.set()

    await asyncio.wait_for(job_module._wait_next_progress_push([idle, changed], 30, early_on_activity=True), timeout=5)

    # 動作が変わらなければ push 間隔まで待つ。
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(job_module._wait_next_progress_push([idle], 30, early_on_activity=True), timeout=0.2)


@pytest.mark.asyncio
async def test_first_progress_push_ignores_activity_changes(monkeypatch) -> None:
    """最初のメッセージを送るまでは、動作が変わっても push 間隔を待つ（すぐ終わるジョブに進捗を出さない）。"""
    job_module = tools._dispatch_agent_job
    monkeypatch.setattr(job_module, "_ACTIVITY_PUSH_MIN_INTERVAL_SECONDS", 0.01)
    job = _activity_job()
    job.activity_changed.set()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(job_module._wait_next_progress_push([job], 30, early_on_activity=False), timeout=0.2)


@pytest.mark.asyncio
async def test_batch_subfolder_pattern_collects_nested_files(monkeypatch, tmp_path) -> None:
    """年度フォルダ/ocr_md のような入れ子構成でも pattern に階層を含めれば対象にできる
    （本番で "**/ocr_md/*.md" が直下のみの探索で0件エラーになった件の回帰テスト）。"""
    images, _ = _setup(monkeypatch, tmp_path, [])
    root = images.parent
    for year in ("2024", "2023"):
        ocr = root / year / "ocr_md"
        ocr.mkdir(parents=True)
        (ocr / f"{year}_a.md").write_text(year, encoding="utf-8")
        (root / year / "photo.jpg").write_bytes(year.encode("utf-8"))
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", pattern="**/ocr_md/*.md", path=str(root), group_size=1)

    assert len(captured) == 2
    assert all("ocr_md" in task and "photo.jpg" not in task for task in captured)
    # 相対パス順（2023 → 2024）でグループ分けされ、見出しにも相対パスが出る。
    assert result.index("2023/ocr_md/2023_a.md") < result.index("2024/ocr_md/2024_a.md")

    captured.clear()
    result = await _invoke(task="t", agent_type="worker", pattern="*/ocr_md/*.md", path=str(root), group_size=10)
    assert len(captured) == 1 and "2023_a.md" in captured[0] and "2024_a.md" in captured[0]


@pytest.mark.asyncio
async def test_batch_pattern_cannot_escape_path_with_parent_dir(monkeypatch, tmp_path) -> None:
    """pathlib の glob は ".." をそのまま辿るため、path の外のファイルが対象に入らないこと。"""
    images, _ = _setup(monkeypatch, tmp_path, ["a.png"])
    outside = images.parent / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"x")
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", pattern="../outside/*.png", path=str(images))
    assert result.startswith("Error:")
    assert captured == []

    result = await _invoke(task="t", agent_type="worker", pattern="{*.png,../outside/*.png}", path=str(images))
    assert len(captured) == 1
    assert "a.png" in captured[0] and "secret.png" not in captured[0]


@pytest.mark.asyncio
async def test_batch_plain_pattern_stays_direct_children_only_with_hint(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, [])
    (images / "sub").mkdir()
    (images / "sub" / "a.png").write_bytes(b"x")
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images))

    assert result.startswith("Error:") and "**/*.jpg" in result
    assert captured == []


@pytest.mark.asyncio
async def test_batch_runs_each_group_as_named_child_runnable(monkeypatch, tmp_path) -> None:
    """各グループは BATCH_GROUP_RUN_NAME の子runとして実行され、表示名が metadata に載る
    （app.py がこれを見てグループごとの中間Stepを作る）。"""
    from langchain_core.callbacks import AsyncCallbackHandler

    images, _ = _setup(monkeypatch, tmp_path, [f"f{i}.png" for i in range(3)])
    _capture_tasks(monkeypatch)
    started: list[tuple[str, dict]] = []

    class _Recorder(AsyncCallbackHandler):
        async def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
            if kwargs.get("name") == _BATCH_MODULE.BATCH_GROUP_RUN_NAME:
                started.append((kwargs["name"], metadata or {}))

    await tools.dispatch_agent_batch.ainvoke(
        {
            "name": "dispatch_agent_batch",
            "args": {"task": "t", "agent_type": "worker", "pattern": "*.png", "path": str(images), "group_size": 2},
            "id": "test-batch-runnable",
            "type": "tool_call",
        },
        config={"callbacks": [_Recorder()]},
    )

    labels = sorted(meta["batch_group_label"] for _, meta in started)
    assert labels == ["SUB: worker (group 1/2, 2 files)", "SUB: worker (group 2/2, 1 files)"]


@pytest.mark.asyncio
async def test_batch_missing_folder_lists_workdir_folders(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path, ["a.png"])
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path="no_such_folder")

    assert result.startswith("Error: target folder not found")
    assert "Folders directly under the working directory" in result and "images" in result
    assert captured == []


_UNREACHABLE_RESULT = "[Sub-agent: the LLM call timed out(ConnectError), so it was cut off]"


@pytest.mark.asyncio
async def test_batch_stops_starting_groups_after_llm_unreachable(monkeypatch, tmp_path) -> None:
    """LLMに届かず打ち切られたグループが出たら、順番待ちのグループは起動せず「未実行」で返す。"""
    images, _ = _setup(monkeypatch, tmp_path, [f"f{i}.png" for i in range(3)])
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_MAX_PARALLEL", 1)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BATCH_STOP_AFTER_UNREACHABLE_GROUPS", 1)
    started: list[str] = []

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        started.append(task)
        return _UNREACHABLE_RESULT

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images), group_size=1)

    assert len(started) == 1
    assert "truncated 1" in result and "not started 2" in result
    assert "ask the user to check the state of the LLM server" in result
    # 未実行のグループも担当ファイルを返す（再委任用）。
    assert f"- {(images / 'f2.png').resolve()}" in result
    assert tools._dispatch_agent_job._DISPATCH_AGENT_JOBS == {}


@pytest.mark.asyncio
async def test_batch_unreachable_guard_disabled_with_zero(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, [f"f{i}.png" for i in range(3)])
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_MAX_PARALLEL", 1)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BATCH_STOP_AFTER_UNREACHABLE_GROUPS", 0)
    started: list[str] = []

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        started.append(task)
        return _UNREACHABLE_RESULT

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images), group_size=1)

    assert len(started) == 3
    assert "truncated 3)" in result
    assert "state of the LLM server" not in result


@pytest.mark.asyncio
async def test_batch_saves_interrupted_note_on_cancel(monkeypatch, tmp_path) -> None:
    """停止で中断しても、完了済みグループの結果と未完了グループの担当ファイルを thread note に残す。"""
    images, notes = _setup(monkeypatch, tmp_path, ["a.png", "b.png", "c.png"])
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_MAX_PARALLEL", 1)
    b_started = asyncio.Event()

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        if "a.png" in task:
            return "グループ1の処理結果"
        b_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)
    tool_call_id = "test-batch-cancel"
    call = asyncio.create_task(
        tools.dispatch_agent_batch.ainvoke(
            {
                "name": "dispatch_agent_batch",
                "args": {"task": "t", "agent_type": "worker", "pattern": "*.png", "path": str(images), "group_size": 1},
                "id": tool_call_id,
                "type": "tool_call",
            }
        )
    )
    await asyncio.wait_for(b_started.wait(), timeout=5)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert len(notes) == 1
    note = notes[0]
    assert note["topic"] == _BATCH_MODULE.interrupted_note_topic(tool_call_id)
    content = note["content"]
    assert "done 1" in content and "killed 1" in content and "not started 1" in content
    assert "グループ1の処理結果" in content
    assert str((images / "c.png").resolve()) in content
    assert tools._dispatch_agent_job._DISPATCH_AGENT_JOBS == {}


# --- files 指定（前段の explore が見つけたファイルを少数ずつ割り振る） ---
# 1つの explore が検索から全ファイルの読み込みまでを抱えてコンテキストが膨らみ、
# 1回の応答ごとに遅くなっていた本番実例（2026-11-03）への対応。


def _make_scattered_files(tmp_path) -> list:
    paths = []
    for folder, name in [("2019", "p20.md"), ("2024", "IMG_1.md"), ("2019", "p6.md"), ("2025", "IMG_2.md"), ("2020", "IMG_3.md")]:
        p = tmp_path / "workdir" / "Datas" / folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(p), encoding="utf-8")
        paths.append(p)
    return paths


@pytest.mark.asyncio
async def test_batch_files_splits_listed_files_in_given_order(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path, [])
    paths = _make_scattered_files(tmp_path)
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="クリスマス会の日程を確認する", agent_type="worker", files=[str(p) for p in paths], group_size=2)

    assert len(captured) == 3
    by_group = sorted(captured, key=lambda t: t.split("(group ")[1])
    # 並びはファイル名順に並べ替えず、渡された順のまま2件ずつ割り振る。
    for task, group in zip(by_group, [paths[0:2], paths[2:4], paths[4:5]]):
        assert "クリスマス会の日程を確認する" in task
        for p in group:
            assert str(p.resolve()) in task
        # 確認した事実と出典を返させる（件数だけ返させない）。
        assert "facts you confirmed with their source" in task
        assert "the number written" not in task
    assert "Split the 5 listed files into 3 groups" in result


@pytest.mark.asyncio
async def test_batch_files_accepts_relative_paths_and_drops_duplicates(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path, [])
    paths = _make_scattered_files(tmp_path)
    captured = _capture_tasks(monkeypatch)

    await _invoke(task="t", agent_type="worker", files=["Datas/2019/p20.md", str(paths[0]), " "], group_size=2)

    assert len(captured) == 1
    # 相対パスはスレッド専用フォルダ（default_workdir のシード写し）側に解決されうるが、
    # 中身が同じなので1件だけ割り振られる。
    assert captured[0].count("p20.md") == 1


@pytest.mark.asyncio
async def test_batch_files_skips_missing_file_and_reports_it(monkeypatch, tmp_path) -> None:
    """ナレッジの索引は実ファイルより古いことがあるため、消えたファイルは飛ばして残りを割り振る（本番ログ 2026-11-03）。"""
    _setup(monkeypatch, tmp_path, [])
    paths = _make_scattered_files(tmp_path)
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", files=[str(paths[0]), str(tmp_path / "nope.md")])

    assert len(captured) == 1
    assert str(paths[0].resolve()) in captured[0]
    assert "nope.md" not in captured[0]
    assert "could not be opened and were not assigned" in result and "nope.md" in result


@pytest.mark.asyncio
async def test_batch_files_all_missing_returns_error_without_jobs(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path, [])
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", files=[str(tmp_path / "nope.md")])

    assert result.startswith("Error: none of the listed files could be opened")
    assert captured == []
    assert tools._dispatch_agent_job._DISPATCH_AGENT_JOBS == {}


@pytest.mark.asyncio
async def test_batch_requires_exactly_one_of_pattern_and_files(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, ["a.png"])
    paths = _make_scattered_files(tmp_path)
    captured = _capture_tasks(monkeypatch)

    both = await _invoke(task="t", agent_type="worker", pattern="*.png", path=str(images), files=[str(paths[0])])
    neither = await _invoke(task="t", agent_type="worker")

    assert both.startswith("Error: pass either pattern")
    assert neither.startswith("Error: pass pattern")
    assert captured == []


@pytest.mark.asyncio
async def test_batch_files_returns_longer_group_answers(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path, [])
    paths = _make_scattered_files(tmp_path)

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        return "日程は12月14日" + "あ" * 1000

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    result = await _invoke(task="t", agent_type="worker", files=[str(paths[0])])

    # 答えそのものが入るため、pattern 指定時の400字では切らない。
    assert "あ" * 1000 in result
    assert "... (rest omitted)" not in result


@pytest.mark.asyncio
async def test_batch_files_default_group_size_is_lookup_files_per_agent(monkeypatch, tmp_path) -> None:
    """files 指定で group_size を省くと [subagent].lookup_files_per_agent 件ずつ（15件ずつにしない）。"""
    _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", type("Cfg", (), {"subagent_lookup_files_per_agent": 2})())
    paths = _make_scattered_files(tmp_path)
    captured = _capture_tasks(monkeypatch)

    await _invoke(task="t", agent_type="worker", files=[str(p) for p in paths])

    assert len(captured) == 3


@pytest.mark.asyncio
async def test_batch_pattern_default_group_size_is_15(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path, [f"{i:02d}.png" for i in range(20)])
    captured = _capture_tasks(monkeypatch)

    await _invoke(task="t", agent_type="worker", pattern="*.png", path="images")

    assert len(captured) == 2


@pytest.mark.asyncio
async def test_batch_pattern_keeps_files_with_identical_content(monkeypatch, tmp_path) -> None:
    """pattern の一括処理はファイルごとに出力を作ることがあるため、中身が同じでも省かない。"""
    images, _ = _setup(monkeypatch, tmp_path, [])
    for name in ("a.png", "b.png", "empty1.png", "empty2.png"):
        (images / name).write_bytes(b"" if name.startswith("empty") else b"same")
    captured = _capture_tasks(monkeypatch)

    result = await _invoke(task="t", agent_type="worker", pattern="*.png", path="images", group_size=10)

    assert len(captured) == 1
    assert all(n in captured[0] for n in ("a.png", "b.png", "empty1.png", "empty2.png"))
    assert "identical copies" not in result


@pytest.mark.asyncio
async def test_batch_files_leaves_out_identical_copies_but_not_empty_files(monkeypatch, tmp_path) -> None:
    images, _ = _setup(monkeypatch, tmp_path, [])
    (images / "a.png").write_bytes(b"same")
    (images / "copy_of_a.png").write_bytes(b"same")
    (images / "empty1.png").write_bytes(b"")
    (images / "empty2.png").write_bytes(b"")
    captured = _capture_tasks(monkeypatch)

    names = ["a.png", "copy_of_a.png", "empty1.png", "empty2.png"]
    result = await _invoke(task="t", agent_type="worker", files=[str(images / n) for n in names], group_size=10)

    assert len(captured) == 1
    assert "copy_of_a.png" not in captured[0].split("[Assigned files", 1)[1]
    assert "empty1.png" in captured[0] and "empty2.png" in captured[0]
    assert "1 identical copies left out" in result


def test_list_target_files_pattern_forms_and_junction_loop(tmp_path, monkeypatch) -> None:
    """サブフォルダを含むパターンは、ジャンクションの循環に入らず、pathlib の glob と同じ一致になる。"""
    import os
    import subprocess
    import sys

    from src import skill_drafts

    monkeypatch.setattr(skill_drafts, "current_draft_user", lambda: None)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)
    _list_target_files = sys.modules["src.tools.dispatch_agent_batch"]._list_target_files

    (tmp_path / "2021" / "ocr_md").mkdir(parents=True)
    (tmp_path / "2021" / "ocr_md" / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "2021" / "b.jpg").write_text("b", encoding="utf-8")
    (tmp_path / "top.jpg").write_text("t", encoding="utf-8")
    loop = tmp_path / "2021" / "loop"
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(loop), str(tmp_path)], check=True, capture_output=True)
    try:
        names = lambda pattern: [p.relative_to(tmp_path.resolve()).as_posix() for p in _list_target_files(tmp_path, pattern)]  # noqa: E731
        assert names("**/*.jpg") == ["2021/b.jpg", "top.jpg"]
        assert names("*/ocr_md/*.md") == ["2021/ocr_md/a.md"]
        assert names("**/ocr_md/*.MD") == ["2021/ocr_md/a.md"] if os.name == "nt" else True
    finally:
        if loop.exists():
            os.rmdir(loop)


def test_list_target_files_stops_at_time_limit(tmp_path, monkeypatch) -> None:
    import sys

    from src import skill_drafts
    from src.tools._file_tools_limits import FileToolsLimits

    batch = sys.modules["src.tools.dispatch_agent_batch"]
    monkeypatch.setattr(skill_drafts, "current_draft_user", lambda: None)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.jpg").write_text("a", encoding="utf-8")
    monkeypatch.setattr(batch, "current_limits", lambda: FileToolsLimits(search_time_limit_seconds=0))
    with pytest.raises(ValueError, match="longer than"):
        batch._list_target_files(tmp_path, "**/*.jpg")
