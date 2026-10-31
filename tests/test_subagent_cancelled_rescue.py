"""run_subagent の on_cancelled コールバック・dump_messages_for_cancelled_rescue の回帰テスト。

背景: 停止ボタン等でdispatch_agent実行中のサブエージェントを強制終了すると、
run_subagent のローカル変数 messages（会話履歴）は asyncio.CancelledError と
ともに完全に破棄されていた。既存の stop_dispatch_agent_job ツール（LLM自身が
明示的に呼ぶ経路）は write_scratch_note の内容を返す救済策を持つが、停止
ボタン（on_stop → cancel_dispatch_agent_jobs_for_thread）経由の強制終了には
この救済が無かった。on_cancelled 引数を追加し、CancelledError検知時にここ
までの会話履歴を呼び出し元（_dispatch_agent_job.py）へ渡せるようにする。
"""

import asyncio
from dataclasses import dataclass

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src import subagent


@dataclass
class _FakeConfig:
    """test_subagent_timeout_retry.py の _FakeConfig と同じ理由づけ。"""

    thinking_loop_guard_max_retries: int = 0
    # 同じツール呼び出しのループ検知（src/tool_loop_guard.py）。このテストの対象外のため無効。
    tool_loop_guard_enabled: bool = False
    tool_loop_guard_max_repeats: int = 3
    tool_loop_guard_exclude_tools: tuple = ()
    tool_loop_guard_nudge_messages: tuple = ()
    subagent_empty_response_max_retries: int = 0
    subagent_token_guard_enabled: bool = False
    track_token_usage: bool = False
    context_trim_subagent_enabled: bool = False
    context_compaction_enabled: bool = False
    context_compaction_token_threshold: int = 0
    context_compaction_single_request_token_threshold: int = 0
    context_compaction_keep_recent_iterations: int = 0
    context_compaction_min_messages_to_compact: int = 0
    context_compaction_prompt_path: str | None = None
    context_compaction_summary_source_max_chars: int = 0
    context_compaction_skill_reattach_max_chars_per_skill: int = 12000
    context_compaction_skill_reattach_total_max_chars: int = 36000
    context_compaction_pre_note_threshold: int = 0
    context_compaction_pre_note_warning_text: str = ""
    context_compaction_require_note_max_skips: int = 0
    context_compaction_subagent_enabled: bool = False
    context_compaction_subagent_token_threshold: int = 0
    context_compaction_subagent_single_request_token_threshold: int = 0
    context_compaction_subagent_keep_recent_iterations: int = 0
    context_compaction_subagent_min_messages_to_compact: int = 0
    context_compaction_subagent_prompt_path: str | None = None
    context_compaction_subagent_summary_source_max_chars: int = 0
    context_compaction_subagent_pre_note_threshold: int = 0
    context_compaction_subagent_pre_note_warning_text: str = ""
    context_compaction_subagent_require_note_max_skips: int = 0


class _CancellingModel:
    """1回目の ainvoke() は正常応答（tool_call）を返し、2回目で CancelledError を送出する。"""

    def __init__(self) -> None:
        self._calls = 0

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        self._calls += 1
        if self._calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "noop", "args": {}, "id": "tc-1", "type": "tool_call"}],
            )
        raise asyncio.CancelledError()


async def _fake_build_model(config, role):
    return _CancellingModel()


async def _noop_tool_ainvoke(call):
    return ToolMessage(content="ok", tool_call_id=call["id"])


class _FakeToolObj:
    name = "noop"

    async def ainvoke(self, call):
        return await _noop_tool_ainvoke(call)


@pytest.mark.asyncio
async def test_on_cancelled_receives_messages_and_cancelled_error_propagates(monkeypatch) -> None:
    monkeypatch.setattr(subagent, "build_model", _fake_build_model)

    captured: list = []
    captured_reasons: list[str] = []

    def _on_cancelled(messages: list, reason: str) -> None:
        captured.append(list(messages))
        captured_reasons.append(reason)

    with pytest.raises(asyncio.CancelledError):
        await subagent.run_subagent(
            task="t",
            tools=[_FakeToolObj()],
            system_prompt="sp",
            config=_FakeConfig(),
            max_iterations=5,
            on_cancelled=_on_cancelled,
        )

    assert len(captured) == 1
    rescued_messages = captured[0]
    # SystemMessage + HumanMessage(task) + AIMessage(tool_call) + ToolMessage(noop結果)
    # までは積まれた状態でキャンセルされているはず。
    assert any(isinstance(m, ToolMessage) and m.content == "ok" for m in rescued_messages)
    assert any(isinstance(m, HumanMessage) and m.content == "t" for m in rescued_messages)
    assert captured_reasons == [subagent.RESCUE_REASON_CANCELLED]


@pytest.mark.asyncio
async def test_cancelled_error_propagates_without_on_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(subagent, "build_model", _fake_build_model)

    with pytest.raises(asyncio.CancelledError):
        await subagent.run_subagent(
            task="t",
            tools=[_FakeToolObj()],
            system_prompt="sp",
            config=_FakeConfig(),
            max_iterations=5,
        )


@pytest.mark.asyncio
async def test_on_cancelled_exception_does_not_suppress_cancelled_error(monkeypatch) -> None:
    """退避コールバック自体が失敗しても、CancelledErrorの伝播を妨げてはならない。"""
    monkeypatch.setattr(subagent, "build_model", _fake_build_model)

    def _broken_on_cancelled(messages: list, reason: str) -> None:
        raise RuntimeError("scratch note write failed")

    with pytest.raises(asyncio.CancelledError):
        await subagent.run_subagent(
            task="t",
            tools=[_FakeToolObj()],
            system_prompt="sp",
            config=_FakeConfig(),
            max_iterations=5,
            on_cancelled=_broken_on_cancelled,
        )


def test_dump_messages_for_cancelled_rescue_empty_returns_empty_string() -> None:
    assert subagent.dump_messages_for_cancelled_rescue([]) == ""


def test_dump_messages_for_cancelled_rescue_includes_role_and_content() -> None:
    messages = [
        HumanMessage(content="task-content"),
        AIMessage(
            content="",
            tool_calls=[{"name": "read_skill", "args": {"skill_name": "x"}, "id": "tc-1", "type": "tool_call"}],
        ),
        ToolMessage(content="tool-result", tool_call_id="tc-1"),
    ]

    dump = subagent.dump_messages_for_cancelled_rescue(messages)

    assert "task-content" in dump
    assert "tool-result" in dump
    assert "read_skill" in dump
    assert "HumanMessage" in dump
    assert "ToolMessage" in dump
