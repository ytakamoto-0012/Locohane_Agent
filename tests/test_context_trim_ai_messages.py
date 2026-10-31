"""src/context_trim.py の trim_old_ai_messages() の回帰テスト。

trim_old_tool_messages() は ToolMessage しか見ないため、モデルが
execute_python_code の code 引数へファイル本文を書き写すような使い方をすると、
ツール結果側だけを絞っても1リクエストあたりの入力が膨らみ続ける（実測: 大量
ファイル処理で24,833→128,000トークンまで単調増加しコンテキスト上限に張り付いて
処理が停止した）。trim_old_ai_messages() はこの経路を塞ぐために追加した。

tool_calls の id/name・件数を変更しないことが最重要の不変条件（ToolMessage との
対応が壊れると LangGraph 側で例外になる）。
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.context_trim import trim_old_ai_messages


def _ai_with_tool_call(content: str, code: str, call_id: str) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[
            {"name": "execute_python_code", "args": {"code": code}, "id": call_id},
        ],
    )


def test_old_ai_message_content_and_args_are_truncated() -> None:
    long_code = "x = 1\n" * 1000
    messages = [
        HumanMessage(content="go"),
        _ai_with_tool_call("thinking..." * 500, long_code, "call-1"),
        ToolMessage(content="ok", tool_call_id="call-1"),
        HumanMessage(content="次"),
        _ai_with_tool_call("short", "print(1)", "call-2"),
        ToolMessage(content="ok", tool_call_id="call-2"),
    ]

    result = trim_old_ai_messages(messages, keep_recent_iterations=0, max_chars=50)

    old_ai = result[1]
    assert isinstance(old_ai, AIMessage)
    assert len(old_ai.content) < len("thinking..." * 500)
    assert len(old_ai.tool_calls[0]["args"]["code"]) < len(long_code)


def test_tool_call_ids_and_names_and_count_are_unchanged() -> None:
    long_code = "y = 2\n" * 1000
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "execute_python_code", "args": {"code": long_code}, "id": "a"},
                {"name": "Read", "args": {"file_path": "@1"}, "id": "b"},
            ],
        ),
        ToolMessage(content="ok", tool_call_id="a"),
        ToolMessage(content="ok", tool_call_id="b"),
    ]

    result = trim_old_ai_messages(messages, keep_recent_iterations=0, max_chars=10)

    trimmed_calls = result[0].tool_calls
    assert len(trimmed_calls) == 2
    assert [c["id"] for c in trimmed_calls] == ["a", "b"]
    assert [c["name"] for c in trimmed_calls] == ["execute_python_code", "Read"]
    # 短い引数（@1）は切り詰め対象外のまま。
    assert trimmed_calls[1]["args"]["file_path"] == "@1"


def test_recent_ai_messages_are_kept_verbatim() -> None:
    long_code = "z = 3\n" * 1000
    ai_1 = _ai_with_tool_call("old" * 200, long_code, "call-1")
    ai_2 = _ai_with_tool_call("recent" * 200, long_code, "call-2")
    messages = [
        ai_1,
        ToolMessage(content="ok", tool_call_id="call-1"),
        ai_2,
        ToolMessage(content="ok", tool_call_id="call-2"),
    ]

    result = trim_old_ai_messages(messages, keep_recent_iterations=1, max_chars=20)

    assert result[0] is not ai_1
    assert len(result[0].content) < len(ai_1.content)
    assert result[2] is ai_2
    assert result[2].tool_calls[0]["args"]["code"] == long_code


def test_original_messages_are_not_mutated() -> None:
    long_code = "w = 4\n" * 1000
    original = _ai_with_tool_call("body" * 200, long_code, "call-1")
    messages = [original, ToolMessage(content="ok", tool_call_id="call-1")]

    trim_old_ai_messages(messages, keep_recent_iterations=0, max_chars=10)

    assert original.content == "body" * 200
    assert original.tool_calls[0]["args"]["code"] == long_code
