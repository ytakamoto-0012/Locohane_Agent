"""maybe_compact() 後にトリム（context_trim）が解除されることの回帰テスト。

is_trigger_reached() は「一度でも閾値に達したら以後トリムを継続する」判定
だが、圧縮で保持される直近の AIMessage の複製は圧縮前の（大きな）
usage_metadata を持つため、そのままでは圧縮後もトリムが続いてしまう。
maybe_compact() は複製に COMPACTION_KEPT_KEY を付け、判定から除外させる。
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src import tools
from src.context_compaction import maybe_compact
from src.context_trim import COMPACTION_KEPT_KEY, is_trigger_reached


@dataclass
class _FakeConfig:
    context_compaction_keep_recent_iterations: int
    context_compaction_prompt_path: Path
    context_trim_truncated_max_chars: int
    context_compaction_summary_source_max_chars: int
    context_compaction_skill_reattach_max_chars_per_skill: int
    context_compaction_skill_reattach_total_max_chars: int


class _SummaryModel:
    async def ainvoke(self, messages):
        return AIMessage(content="要約結果")


class _FakeUserSession:
    def get(self, key, default=None):
        return default


def _ai_with_usage(content: str, total_tokens: int) -> AIMessage:
    return AIMessage(
        content=content,
        usage_metadata={"input_tokens": total_tokens - 10, "output_tokens": 10, "total_tokens": total_tokens},
    )


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


@pytest.mark.asyncio
async def test_trim_is_released_after_compaction(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    original_kept = _ai_with_usage("ok2", 150_000)
    messages = [
        HumanMessage(content="q1"),
        _ai_with_usage("ok1", 120_000),
        HumanMessage(content="q2"),
        original_kept,
    ]
    assert is_trigger_reached(messages, 100_000) is True

    result = await maybe_compact(messages, _SummaryModel(), _config(tmp_path))

    assert result is not None
    kept_ai = [m for m in result if isinstance(m, AIMessage)]
    assert kept_ai, "直近の AIMessage は保持される想定"
    assert all(m.response_metadata.get(COMPACTION_KEPT_KEY) for m in kept_ai)
    # 圧縮直後はトリム解除、圧縮後の呼び出しが閾値に達したら再びトリム。
    assert is_trigger_reached(result, 100_000) is False
    assert is_trigger_reached([*result, _ai_with_usage("ok3", 100_000)], 100_000) is True
    # 元のメッセージ（checkpointer上の既存state）は書き換えない。
    assert COMPACTION_KEPT_KEY not in original_kept.response_metadata
