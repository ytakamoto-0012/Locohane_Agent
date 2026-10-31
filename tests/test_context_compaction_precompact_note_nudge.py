"""src/context_compaction.py の maybe_append_precompact_note_nudge() の回帰テスト。

コンテキスト圧縮（要約）は永続履歴を書き換える恒久的な操作であり、要約LLMの
精度次第で古い会話中の具体的な事実が薄まって失われうる。圧縮が実際に発火する
前に、write_thread_noteへの書き出しを促す注意メッセージを1回だけ差し込む
（src/main_token_guard.py の maybe_append_token_guard と同じ設計）。
"""

from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.context_compaction import (
    _PRE_NOTE_MARKER,
    is_compaction_blocked_by_missing_note,
    maybe_append_precompact_note_nudge,
)


@dataclass
class _FakeConfig:
    context_compaction_enabled: bool = True
    context_compaction_pre_note_threshold: int = 1000
    context_compaction_pre_note_warning_text: str = "write_thread_noteへ書き出してください"
    context_compaction_keep_recent_iterations: int = 3
    context_compaction_require_note_max_skips: int = 0


def _ai_with_usage(total_tokens: int) -> AIMessage:
    msg = AIMessage(content="ok")
    msg.usage_metadata = {
        "input_tokens": total_tokens - 10,
        "output_tokens": 10,
        "total_tokens": total_tokens,
    }
    return msg


def test_no_injection_when_below_threshold() -> None:
    config = _FakeConfig(context_compaction_pre_note_threshold=1000)
    messages = [HumanMessage(content="hi"), _ai_with_usage(500)]

    result = maybe_append_precompact_note_nudge(messages, config)

    assert result == messages


def test_injects_nudge_when_threshold_reached() -> None:
    config = _FakeConfig(context_compaction_pre_note_threshold=1000)
    messages = [HumanMessage(content="hi"), _ai_with_usage(1500)]

    result = maybe_append_precompact_note_nudge(messages, config)

    assert len(result) == len(messages) + 1
    added = result[-1]
    assert isinstance(added, HumanMessage)
    assert _PRE_NOTE_MARKER in added.content
    assert "write_thread_noteへ書き出してください" in added.content


def test_original_messages_are_not_mutated() -> None:
    config = _FakeConfig(context_compaction_pre_note_threshold=1000)
    messages = [HumanMessage(content="hi"), _ai_with_usage(1500)]
    original_length = len(messages)

    maybe_append_precompact_note_nudge(messages, config)

    assert len(messages) == original_length


def test_disabled_compaction_never_injects() -> None:
    config = _FakeConfig(context_compaction_enabled=False, context_compaction_pre_note_threshold=1000)
    messages = [HumanMessage(content="hi"), _ai_with_usage(999999)]

    result = maybe_append_precompact_note_nudge(messages, config)

    assert result == messages


def test_zero_threshold_disables_nudge() -> None:
    config = _FakeConfig(context_compaction_pre_note_threshold=0)
    messages = [HumanMessage(content="hi"), _ai_with_usage(999999)]

    result = maybe_append_precompact_note_nudge(messages, config)

    assert result == messages


def test_no_ai_message_never_injects() -> None:
    config = _FakeConfig(context_compaction_pre_note_threshold=1000)
    messages = [HumanMessage(content="hi")]

    result = maybe_append_precompact_note_nudge(messages, config)

    assert result == messages


def _write_thread_note_call(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "write_thread_note", "args": {"topic": "t", "content": "c"}, "id": call_id}],
    )


def test_no_injection_when_write_thread_note_called_within_recent_turns() -> None:
    """直近ターン内で write_thread_note 済みなら、閾値超過でも再ナッジしない（無限ループ回避）。"""
    config = _FakeConfig(context_compaction_pre_note_threshold=1000, context_compaction_keep_recent_iterations=3)
    messages = [
        HumanMessage(content="turn1"),
        _write_thread_note_call("call-1"),
        ToolMessage(content="ok", tool_call_id="call-1"),
        _ai_with_usage(1500),
    ]

    result = maybe_append_precompact_note_nudge(messages, config)

    assert result == messages


def test_injects_again_once_write_thread_note_call_falls_outside_recent_turns() -> None:
    """write_thread_note 済みでも、それが keep_recent_iterations より前なら再ナッジする。"""
    config = _FakeConfig(context_compaction_pre_note_threshold=1000, context_compaction_keep_recent_iterations=2)
    messages = [
        HumanMessage(content="turn1"),
        _write_thread_note_call("call-1"),
        ToolMessage(content="ok", tool_call_id="call-1"),
        HumanMessage(content="turn2"),
        AIMessage(content="a"),
        HumanMessage(content="turn3"),
        AIMessage(content="b"),
        HumanMessage(content="turn4"),
        _ai_with_usage(1500),
    ]

    result = maybe_append_precompact_note_nudge(messages, config)

    assert len(result) == len(messages) + 1
    assert _PRE_NOTE_MARKER in result[-1].content


def test_blocked_when_note_not_called_and_max_skips_zero() -> None:
    """require_note_max_skips=0（無期限に待つ）なら、write_thread_note未呼び出しの
    間はskip_countがいくつであっても常にブロックする。"""
    config = _FakeConfig(context_compaction_require_note_max_skips=0)
    messages = [HumanMessage(content="turn1"), _ai_with_usage(1500)]

    assert is_compaction_blocked_by_missing_note(messages, config, skip_count=0) is True
    assert is_compaction_blocked_by_missing_note(messages, config, skip_count=999) is True


def test_not_blocked_when_note_called_recently() -> None:
    """直近keep_recent_iterations反復以内にwrite_thread_noteが呼ばれていればブロックしない。"""
    config = _FakeConfig(context_compaction_require_note_max_skips=0)
    messages = [
        HumanMessage(content="turn1"),
        _write_thread_note_call("call-1"),
        ToolMessage(content="ok", tool_call_id="call-1"),
        _ai_with_usage(1500),
    ]

    assert is_compaction_blocked_by_missing_note(messages, config, skip_count=0) is False


def test_forced_unblock_once_max_skips_reached() -> None:
    """未呼び出しのまま見送った回数がrequire_note_max_skipsに達したら、記録が
    無くても圧縮を強制するためブロックを解除する（安全弁）。"""
    config = _FakeConfig(context_compaction_require_note_max_skips=2)
    messages = [HumanMessage(content="turn1"), _ai_with_usage(1500)]

    assert is_compaction_blocked_by_missing_note(messages, config, skip_count=0) is True
    assert is_compaction_blocked_by_missing_note(messages, config, skip_count=1) is True
    assert is_compaction_blocked_by_missing_note(messages, config, skip_count=2) is False
