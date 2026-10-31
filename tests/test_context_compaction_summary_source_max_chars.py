"""maybe_compact() が要約対象の切り詰めに context_compaction_summary_source_max_chars
を使い、[context_trim].truncated_max_chars を流用しないことの回帰テスト。

以前は要約対象の ToolMessage を trim_old_tool_messages() で切り詰める際、
[context_trim] 側の（プリフィル短縮目的の小さめの）max_chars をそのまま
流用していた。要約は永続履歴を置き換える恒久操作のため、これでは要約対象の
ツール結果がまとめて情報欠落し、要約が内容の薄いものになりうる問題があった
（大量ファイル処理タスクでファイル名の列挙しか要約に残らない事例）。
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.context_compaction import maybe_compact


@dataclass
class _FakeConfig:
    context_compaction_keep_recent_iterations: int
    context_compaction_prompt_path: Path
    context_trim_truncated_max_chars: int
    context_compaction_summary_source_max_chars: int
    context_compaction_skill_reattach_max_chars_per_skill: int
    context_compaction_skill_reattach_total_max_chars: int


class _CapturingModel:
    """要約LLMの代わり。渡されたプロンプト本文を記録するだけのスタブ。"""

    def __init__(self) -> None:
        self.received_prompt: str | None = None

    async def ainvoke(self, messages):
        self.received_prompt = messages[0].content
        return AIMessage(content="要約結果")


@pytest.mark.asyncio
async def test_summary_source_uses_dedicated_max_chars_not_context_trim(tmp_path) -> None:
    prompt_path = tmp_path / "compaction_prompt.md"
    prompt_path.write_text("以下を要約してください", encoding="utf-8")

    long_content = "A" * 1000
    messages = [
        HumanMessage(content="q1"),
        AIMessage(content="", tool_calls=[{"name": "Read", "args": {}, "id": "a"}]),
        ToolMessage(content=long_content, name="Read", tool_call_id="a"),
        HumanMessage(content="q2"),
        AIMessage(content="ok2"),
    ]

    config = _FakeConfig(
        context_compaction_keep_recent_iterations=1,
        context_compaction_prompt_path=prompt_path,
        # [context_trim] 側は小さい値のままにしておき、これが使われて
        # いないことを検証する。
        context_trim_truncated_max_chars=20,
        context_compaction_summary_source_max_chars=500,
        context_compaction_skill_reattach_max_chars_per_skill=12000,
        context_compaction_skill_reattach_total_max_chars=36000,
    )
    model = _CapturingModel()

    result = await maybe_compact(messages, model, config)

    assert result is not None
    assert model.received_prompt is not None
    # 500文字までは要約LLMへ渡っている（[context_trim]の20文字に切り詰め
    # られていたら、この長さの'A'の並びは含まれない）。
    assert "A" * 500 in model.received_prompt
    assert "A" * 501 not in model.received_prompt
