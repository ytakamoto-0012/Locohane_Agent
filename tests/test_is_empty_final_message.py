"""is_empty_final_message の判定ロジックの回帰テスト。

2026-08-10 issue: サーバー高負荷下でLLMが content='…'（記号のみ）を返し、
単純な空文字列判定（content.strip()）では「空ではない」と誤判定されて
EMPTY_RESPONSE_NUDGEが発火しなかった。意味のある文字（英数字・各言語の
文字）を一つも含まない場合も「空」とみなすよう修正した際の回帰防止テスト。
"""

from langchain_core.messages import AIMessage, HumanMessage

from src.graph import is_empty_final_message


def test_empty_string_is_empty() -> None:
    assert is_empty_final_message([AIMessage(content="")]) is True


def test_whitespace_only_is_empty() -> None:
    assert is_empty_final_message([AIMessage(content="   \n\t  ")]) is True


def test_ellipsis_only_is_empty() -> None:
    """本番incidentの再現: 三点リーダー1文字のみの応答。"""
    assert is_empty_final_message([AIMessage(content="…")]) is True


def test_punctuation_only_is_empty() -> None:
    assert is_empty_final_message([AIMessage(content="...")]) is True
    assert is_empty_final_message([AIMessage(content="。")]) is True
    assert is_empty_final_message([AIMessage(content="-")]) is True


def test_meaningful_japanese_content_is_not_empty() -> None:
    assert is_empty_final_message([AIMessage(content="完了しました。")]) is False


def test_meaningful_short_english_content_is_not_empty() -> None:
    assert is_empty_final_message([AIMessage(content="OK")]) is False


def test_message_with_tool_calls_is_not_empty() -> None:
    msg = AIMessage(content="…", tool_calls=[{"name": "Read", "args": {}, "id": "1", "type": "tool_call"}])
    assert is_empty_final_message([msg]) is False


def test_non_ai_message_is_not_empty() -> None:
    assert is_empty_final_message([HumanMessage(content="")]) is False


def test_empty_messages_list_is_not_empty() -> None:
    assert is_empty_final_message([]) is False
