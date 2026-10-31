"""サブエージェントの圧縮で委譲時の指示（task）が失われないことの回帰テスト。

背景: run_subagent は SystemMessage（messages[0]）だけを圧縮対象から除外して
いたため、messages[1] の task は要約に飲まれていた。task は履歴の先頭にしか
無く _find_compaction_cut_index の直近ユーザー発話保護も効かないため、
出力形式・調査範囲・禁止事項等の指示が要約LLMの書きぶり次第で薄まり、
圧縮を重ねるほど劣化していた。maybe_compact の pinned_instruction で原文を
要約結果の先頭へ機械的に付け直す。
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from src import subagent, tools
from src.context_compaction import maybe_compact

_TASK = "src/ 配下の TODO を全件列挙し、ファイルパスと行番号の表で返すこと。ファイルは編集しないこと。"


@dataclass
class _FakeConfig:
    context_compaction_keep_recent_iterations: int
    context_compaction_prompt_path: Path
    context_trim_truncated_max_chars: int
    context_compaction_summary_source_max_chars: int
    context_compaction_skill_reattach_max_chars_per_skill: int
    context_compaction_skill_reattach_total_max_chars: int


class _LossySummaryModel:
    """要約LLMの代わり。task の具体的な指示に一切触れない要約を返す。"""

    async def ainvoke(self, messages):
        return AIMessage(content="- TODO を調べている")


class _FakeUserSession:
    def get(self, key, default=None):
        return default


def _config(tmp_path: Path) -> _FakeConfig:
    prompt_path = tmp_path / "compaction_prompt.md"
    prompt_path.write_text("以下を要約してください", encoding="utf-8")
    return _FakeConfig(
        context_compaction_keep_recent_iterations=1,
        context_compaction_prompt_path=prompt_path,
        context_trim_truncated_max_chars=2000,
        context_compaction_summary_source_max_chars=2000,
        context_compaction_skill_reattach_max_chars_per_skill=12000,
        context_compaction_skill_reattach_total_max_chars=36000,
    )


def _iteration(call_id: str) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": "Grep", "args": {}, "id": call_id}]),
        ToolMessage(content="結果", tool_call_id=call_id),
    ]


@pytest.mark.asyncio
async def test_pinned_instruction_survives_repeated_compaction(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    config = _config(tmp_path)

    history = [HumanMessage(content=_TASK), *_iteration("c1"), *_iteration("c2")]
    first = await maybe_compact(history, _LossySummaryModel(), config, role="sub", pinned_instruction=_TASK)
    assert first is not None
    assert _TASK in first[0].content

    # 2回目の圧縮では前回の要約（task付き）が先頭に来る。付け直すのは常に元の
    # task 原文なので、原文が1回だけ含まれる状態が保たれる。
    history = [*first, *_iteration("c3"), *_iteration("c4")]
    second = await maybe_compact(history, _LossySummaryModel(), config, role="sub", pinned_instruction=_TASK)
    assert second is not None
    assert second[0].content.count(_TASK) == 1
    assert second[0].content.startswith("[The task given by the delegator")


@pytest.mark.asyncio
async def test_no_pinned_instruction_appends_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())

    history = [HumanMessage(content=_TASK), *_iteration("c1"), *_iteration("c2")]
    result = await maybe_compact(history, _LossySummaryModel(), _config(tmp_path))

    assert result is not None
    assert "委譲元から指示されたタスク" not in result[0].content


@dataclass
class _SubagentConfig:
    thinking_loop_guard_max_retries: int = 0
    # 同じツール呼び出しのループ検知（src/tool_loop_guard.py）。このテストの対象外のため無効。
    tool_loop_guard_enabled: bool = False
    tool_loop_guard_max_repeats: int = 3
    tool_loop_guard_exclude_tools: tuple = ()
    tool_loop_guard_nudge_messages: tuple = ()
    subagent_empty_response_max_retries: int = 0
    subagent_token_guard_enabled: bool = False
    track_token_usage: bool = True
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
    context_compaction_subagent_enabled: bool = True
    context_compaction_subagent_token_threshold: int = 0
    context_compaction_subagent_single_request_token_threshold: int = 0
    context_compaction_subagent_keep_recent_iterations: int = 3
    context_compaction_subagent_min_messages_to_compact: int = 0
    context_compaction_subagent_prompt_path: str | None = None
    context_compaction_subagent_summary_source_max_chars: int = 0
    context_compaction_subagent_pre_note_threshold: int = 0
    context_compaction_subagent_pre_note_warning_text: str = ""
    context_compaction_require_note_max_skips: int = 0
    context_compaction_subagent_require_note_max_skips: int = 0
    subagent_token_guard_soft_threshold: int = 999999999
    subagent_token_guard_hard_threshold: int = 999999999
    subagent_token_guard_soft_warning_text: str = ""


class _ToolCallThenFinalModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            # write_thread_note も呼ばせて is_compaction_blocked_by_missing_note に見送られないようにする
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "dummy_tool", "args": {}, "id": "call-1"},
                    {"name": "write_thread_note", "args": {"topic": "t", "content": "c"}, "id": "call-2"},
                ],
            )
        return AIMessage(content="完了しました")


@tool
def dummy_tool() -> str:
    """テスト用の何もしないツール。"""
    return "ok"


@pytest.mark.asyncio
async def test_run_subagent_pins_task_on_compaction(monkeypatch) -> None:
    fake_model = _ToolCallThenFinalModel()

    async def fake_build_model(config, role):
        return fake_model

    calls = {"should_compact": 0}

    def fake_should_compact(cumulative_usage, last_usage, message_count, config):
        calls["should_compact"] += 1
        return calls["should_compact"] == 1

    captured = {}

    async def fake_maybe_compact(messages, model, config, *, role="sub", pinned_instruction=None):
        captured["pinned_instruction"] = pinned_instruction
        return [HumanMessage(content="[要約]圧縮済み")]

    monkeypatch.setattr(subagent, "build_model", fake_build_model)
    monkeypatch.setattr(subagent, "should_compact", fake_should_compact)
    monkeypatch.setattr(subagent, "maybe_compact", fake_maybe_compact)

    result = await subagent.run_subagent(
        task=_TASK,
        tools=[dummy_tool],
        system_prompt="sp",
        config=_SubagentConfig(),
        max_iterations=5,
    )

    assert result == "完了しました"
    assert captured["pinned_instruction"] == _TASK
