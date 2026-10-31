"""src/context_trim.py の is_trigger_reached() / last_ai_total_tokens() の回帰テスト。

Claude API の context editing（clear_tool_uses_20250919）の trigger.value に
倣い、[context_trim].trigger_total_tokens 未満のうちはトリムを発動しない
挙動を追加した（変更経緯: 「発動閾値を実装したい。ClaudeAPI方式がいいです」）。
"""

from langchain_core.messages import AIMessage, HumanMessage

from src.context_trim import COMPACTION_KEPT_KEY, is_trigger_reached, last_ai_total_tokens


def _ai_with_usage(total_tokens: int) -> AIMessage:
    msg = AIMessage(content="ok")
    msg.usage_metadata = {
        "input_tokens": total_tokens - 10,
        "output_tokens": 10,
        "total_tokens": total_tokens,
    }
    return msg


def test_zero_threshold_always_triggers() -> None:
    messages = [HumanMessage(content="hi")]

    assert is_trigger_reached(messages, 0) is True


def test_below_threshold_does_not_trigger() -> None:
    messages = [HumanMessage(content="hi"), _ai_with_usage(500)]

    assert is_trigger_reached(messages, 1000) is False


def test_at_or_above_threshold_triggers() -> None:
    messages = [HumanMessage(content="hi"), _ai_with_usage(1000)]

    assert is_trigger_reached(messages, 1000) is True

    messages_over = [HumanMessage(content="hi"), _ai_with_usage(1500)]
    assert is_trigger_reached(messages_over, 1000) is True


def test_missing_usage_metadata_does_not_trigger_when_threshold_positive() -> None:
    # track_token_usage=false 等で usage_metadata が無い場合、閾値未到達
    # とみなし発動しない（main_token_guard.maybe_append_token_guard と同じ
    # 安全側判断）。
    messages = [HumanMessage(content="hi"), AIMessage(content="ok")]

    assert is_trigger_reached(messages, 1000) is False


def test_stays_triggered_after_trimmed_request_drops_below_threshold() -> None:
    # トリムで入力が閾値未満に下がっても解除しない（解除→再トリムの往復で
    # llama-server のプレフィックスキャッシュが毎回外れるのを防ぐ）。
    messages = [
        HumanMessage(content="hi"),
        _ai_with_usage(1200),
        HumanMessage(content="次"),
        _ai_with_usage(800),
    ]

    assert is_trigger_reached(messages, 1000) is True


def test_compaction_kept_copy_is_ignored() -> None:
    # 圧縮で保持された AIMessage の複製は圧縮前の usage_metadata を持つため、
    # 判定から除外する（圧縮後はトリムを解除する）。
    kept = _ai_with_usage(1500)
    kept.response_metadata = {COMPACTION_KEPT_KEY: True}
    messages = [HumanMessage(content="[自動要約]..."), kept, _ai_with_usage(600)]

    assert is_trigger_reached(messages, 1000) is False

    messages.append(_ai_with_usage(1100))
    assert is_trigger_reached(messages, 1000) is True


def test_last_ai_total_tokens_returns_most_recent_value() -> None:
    messages = [_ai_with_usage(100), HumanMessage(content="次"), _ai_with_usage(2000)]

    assert last_ai_total_tokens(messages) == 2000
