"""dispatch_agent の _task_with_plan_hint の回帰テスト。

背景: 本番インシデントで、create_plan の detail_markdown（プロース）は
1ファイル成果物を約束していたが、実際に dispatch_agent(agent_type="worker")
へ渡された task 文には「月間版を作って」としか書かれておらず、委譲された
workerは計画全体が1ファイルを約束していることを知る術が無かった。
_task_with_work_dir_hint と同じ「委譲元の書き起こしを信用せず、
cl.user_session の ground truth を機械的に注入する」パターンを計画にも
適用し、委譲元がtask文に計画全体を書き忘れてもサブエージェントが常に
計画全体・現在位置を認識できることを確認する。
"""

import importlib
import json

import pytest

from src import tools

# tools.dispatch_agent は@toolオブジェクトで上書き済みのため、モジュール自体は
# importlib で sys.modules から直接取得する。
dispatch_agent_module = importlib.import_module("src.tools.dispatch_agent")


class _FakeUserSession:
    def __init__(self, data: dict | None = None):
        self._data: dict = {"thread_id": "thread-1", **(data or {})}

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


def _setup(monkeypatch, tmp_path, plan: list | None = None) -> None:
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", object())
    monkeypatch.setattr(
        tools._state,
        "_AGENT_TYPES",
        {"explore": tools._state.ResolvedAgentType(description="", system_prompt="", tools=[])},
    )
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"plan": plan} if plan is not None else {}))
    monkeypatch.setattr(tools.cl, "Message", _FakeMessage)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_SEMAPHORES", {})
    monkeypatch.setattr(tools._dispatch_agent_job, "_DISPATCH_AGENT_JOBS", {})
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_JOB_RETENTION_SECONDS", 1800)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS", 5)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS", 5)
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)


def test_task_with_plan_hint_prepends_plan_status_when_plan_exists(monkeypatch, tmp_path) -> None:
    plan = [
        {"content": "annual_schedule.pptxを1ファイルで作る", "activeForm": "作成中", "status": "in_progress"},
    ]
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"plan": plan}))

    hinted = dispatch_agent_module._task_with_plan_hint("月間版を作って")

    assert hinted.startswith("[Plan (in progress, highest priority task)]")
    # in_progress ステップは content ではなく activeForm が表示される（_render_plan の仕様）。
    assert "作成中" in hinted
    assert hinted.endswith("\n\n月間版を作って")


def test_task_with_plan_hint_passthrough_when_no_plan(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({}))

    hinted = dispatch_agent_module._task_with_plan_hint("月間版を作って")

    assert hinted == "月間版を作って"


@pytest.mark.asyncio
async def test_dispatch_agent_injects_plan_hint_into_task_reaching_subagent(monkeypatch, tmp_path) -> None:
    plan = [
        {"content": "annual_schedule.pptxを1ファイルで作る", "activeForm": "作成中", "status": "pending"},
    ]
    _setup(monkeypatch, tmp_path, plan=plan)
    captured: dict = {}

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        captured["task"] = task
        return "ok"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    await tools.dispatch_agent.ainvoke(
        {
            "name": "dispatch_agent",
            "args": {"task": "月間版を作って", "agent_type": "explore"},
            "id": "test-tc-1",
            "type": "tool_call",
        }
    )

    assert "annual_schedule.pptxを1ファイルで作る" in captured["task"]
    assert captured["task"].endswith("月間版を作って")


@pytest.mark.asyncio
async def test_dispatch_agent_task_unchanged_by_plan_hint_when_no_plan(monkeypatch, tmp_path) -> None:
    _setup(monkeypatch, tmp_path, plan=None)
    captured: dict = {}

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        captured["task"] = task
        return "ok"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    await tools.dispatch_agent.ainvoke(
        {
            "name": "dispatch_agent",
            "args": {"task": "investigate", "agent_type": "explore"},
            "id": "test-tc-2",
            "type": "tool_call",
        }
    )

    # work_dir未設定時、_resolve_workdir()はdefault_workdir自体ではなく
    # スレッド専用フォルダ（_resolve_exec_workdir()）を返す（2026-08-29修正、
    # 別スレッドのデータを拾わないための分離）。
    expected_work_dir = tools._workdir._resolve_exec_workdir()
    expected_info = json.dumps({"absolute_path": str(expected_work_dir)}, ensure_ascii=False)
    assert captured["task"] == f"[Working directory]\n{expected_info}\n\ninvestigate"
