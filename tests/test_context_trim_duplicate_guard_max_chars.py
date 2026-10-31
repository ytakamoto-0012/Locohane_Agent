"""trim_old_tool_messages() の guarded_tool_max_chars 引数の回帰テスト。

Read/Glob/Grep/json_query/analyze_image は同一引数での再呼び出しに上限が
あり（src.tools の _check_file_tools_duplicate 等）、上限到達時のエラーは
「会話履歴にある前回の実行結果を参照してください」と案内する。だが前回の
ToolMessage が一般的な max_chars（プリフィル短縮目的の小さめの値）で
切り詰められていると、実際にはモデルへ渡っていない分を参照させることに
なり案内が機能しない。guarded_tool_max_chars はこれらのツールの
ToolMessage にだけ別枠の（通常より大きい）切り詰め文字数を適用する。

trim_old_tool_messages() は keep_recent_iterations（find_iteration_cut_index による
ラウンドトリップ境界判定）を使うため、各 ToolMessage には対応する
AIMessage.tool_calls を含めた現実的な構造で組み立てる（対応するAIMessage
が無いToolMessage単体だと「未処理のtool_callが無い」状態を検出できず、
安全な切断点が一つも見つからなくなる）。
"""

from langchain_core.messages import AIMessage, ToolMessage

from src.context_trim import trim_old_tool_messages


def _round_trip(name: str, content: str, call_id: str) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}]),
        ToolMessage(content=content, name=name, tool_call_id=call_id),
    ]


def test_guarded_tool_uses_its_own_larger_limit() -> None:
    long_content = "x" * 1000
    messages = [
        *_round_trip("Read", long_content, "call-1"),
        *_round_trip("execute_python_code", long_content, "call-2"),
    ]

    result = trim_old_tool_messages(
        messages, keep_recent_iterations=0, max_chars=50, guarded_tool_max_chars=500
    )

    read_msg, exec_msg = result[1], result[3]
    assert read_msg.content.startswith("x" * 500)
    assert not read_msg.content.startswith("x" * 501)
    assert exec_msg.content.startswith("x" * 50)
    assert not exec_msg.content.startswith("x" * 51)


def test_guarded_tool_names_cover_all_duplicate_guarded_tools() -> None:
    long_content = "y" * 100
    names = ["Read", "Glob", "Grep", "json_query", "analyze_image"]
    messages = []
    for i, name in enumerate(names):
        messages.extend(_round_trip(name, long_content, f"call-{i}"))

    result = trim_old_tool_messages(messages, keep_recent_iterations=0, max_chars=10, guarded_tool_max_chars=80)

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    for original_name, trimmed in zip(names, tool_messages):
        assert trimmed.content.startswith("y" * 80), original_name
        assert not trimmed.content.startswith("y" * 81), original_name


def test_none_guarded_tool_max_chars_falls_back_to_generic_max_chars() -> None:
    long_content = "z" * 1000
    messages = _round_trip("Read", long_content, "call-1")

    result = trim_old_tool_messages(messages, keep_recent_iterations=0, max_chars=30, guarded_tool_max_chars=None)

    tool_msg = result[1]
    assert tool_msg.content.startswith("z" * 30)
    assert not tool_msg.content.startswith("z" * 31)
