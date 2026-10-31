"""src/main_token_guard.py の回帰テスト。

メインエージェントの1リクエストあたりのトークン量が閾値に達しても、
context_trim（切り詰め）だけでは処理を止める手段が無い。実測では大量
ファイル処理タスクで24,833→128,000トークンまで単調増加し、コンテキスト
上限に張り付いたまま無言で処理が止まった。maybe_append_token_guard() は
その手前で「ユーザーへの状況報告＋新しいチャットへの引継ぎプロンプト」を
出させるための注意メッセージを1回だけ差し込む。
"""

from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage

from src.main_token_guard import GUARD_MARKER, maybe_append_token_guard


@dataclass
class _FakeConfig:
    graph_token_guard_enabled: bool = True
    graph_token_guard_soft_threshold: int = 1000
    graph_handoff_prompt_path: object = None


def _ai_with_usage(total_tokens: int) -> AIMessage:
    msg = AIMessage(content="ok")
    msg.usage_metadata = {
        "input_tokens": total_tokens - 10,
        "output_tokens": 10,
        "total_tokens": total_tokens,
    }
    return msg


def test_no_injection_when_below_threshold(tmp_path) -> None:
    prompt_path = tmp_path / "handoff.md"
    prompt_path.write_text("引継ぎ手順", encoding="utf-8")
    config = _FakeConfig(graph_token_guard_soft_threshold=1000, graph_handoff_prompt_path=prompt_path)
    messages = [HumanMessage(content="hi"), _ai_with_usage(500)]

    result = maybe_append_token_guard(messages, config)

    assert result == messages


def test_injects_handoff_message_once_when_threshold_reached(tmp_path) -> None:
    prompt_path = tmp_path / "handoff.md"
    prompt_path.write_text("引継ぎ手順の本文", encoding="utf-8")
    config = _FakeConfig(graph_token_guard_soft_threshold=1000, graph_handoff_prompt_path=prompt_path)
    messages = [HumanMessage(content="hi"), _ai_with_usage(1500)]

    result = maybe_append_token_guard(messages, config)

    assert len(result) == len(messages) + 1
    added = result[-1]
    assert isinstance(added, HumanMessage)
    assert GUARD_MARKER in added.content
    assert "引継ぎ手順の本文" in added.content


def test_original_messages_are_not_mutated(tmp_path) -> None:
    prompt_path = tmp_path / "handoff.md"
    prompt_path.write_text("引継ぎ手順", encoding="utf-8")
    config = _FakeConfig(graph_token_guard_soft_threshold=1000, graph_handoff_prompt_path=prompt_path)
    messages = [HumanMessage(content="hi"), _ai_with_usage(1500)]
    original_length = len(messages)

    maybe_append_token_guard(messages, config)

    # state（永続履歴）は書き換えない。呼び出しのたびに毎回その場の入力へだけ
    # 1件差し込む設計なので、元のリストは変化しない。
    assert len(messages) == original_length


def test_ignores_human_messages_after_last_ai_message(tmp_path) -> None:
    prompt_path = tmp_path / "handoff.md"
    prompt_path.write_text("引継ぎ手順", encoding="utf-8")
    config = _FakeConfig(graph_token_guard_soft_threshold=1000, graph_handoff_prompt_path=prompt_path)
    # 直近の AIMessage が閾値超でも、その後ろに HumanMessage が続く場合は
    # 「直近の応答」として正しくそのAIMessageのusageを見ること。
    messages = [
        HumanMessage(content="hi"),
        _ai_with_usage(1500),
        HumanMessage(content="tool result follow-up"),
    ]

    result = maybe_append_token_guard(messages, config)

    assert len(result) == len(messages) + 1
    assert GUARD_MARKER in result[-1].content


def test_disabled_guard_never_injects(tmp_path) -> None:
    prompt_path = tmp_path / "handoff.md"
    prompt_path.write_text("引継ぎ手順", encoding="utf-8")
    config = _FakeConfig(
        graph_token_guard_enabled=False,
        graph_token_guard_soft_threshold=1000,
        graph_handoff_prompt_path=prompt_path,
    )
    messages = [HumanMessage(content="hi"), _ai_with_usage(999999)]

    result = maybe_append_token_guard(messages, config)

    assert result == messages


def test_missing_prompt_file_does_not_raise(tmp_path) -> None:
    config = _FakeConfig(
        graph_token_guard_soft_threshold=1000,
        graph_handoff_prompt_path=tmp_path / "does_not_exist.md",
    )
    messages = [HumanMessage(content="hi"), _ai_with_usage(1500)]

    result = maybe_append_token_guard(messages, config)

    assert result == messages
