"""trim_old_tool_messages/trim_old_ai_messages がターン単位で保持することの回帰テスト。

件数ベースの keep_recent だと、1回のAIMessageが並列発行した複数の
tool_calls に対応するToolMessage群のうち、直近何件かだけが保持され残りが
切り詰められる、という分断が起きうる（同じラウンドトリップの一部だけが
欠落する）。keep_recent_iterations（src.context_trim.find_iteration_cut_index による
ラウンドトリップ境界判定）はこれを防ぎ、1回のラウンドトリップは常に
丸ごと同じ側（保持/切り詰め）に入ることを保証する。
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.context_trim import trim_old_tool_messages


def test_parallel_tool_calls_in_same_round_trip_are_not_split() -> None:
    long_content = "x" * 1000
    # 1回のAIMessageが5件のtool_callsを並列発行し、5件のToolMessageが返る、
    # という直近ラウンドトリップ。
    call_ids = [f"call-{i}" for i in range(5)]
    messages = [
        HumanMessage(content="q1"),
        AIMessage(content="", tool_calls=[{"name": "Read", "args": {}, "id": cid} for cid in call_ids]),
        *[ToolMessage(content=long_content, name="Read", tool_call_id=cid) for cid in call_ids],
    ]

    # 件数ベースの旧実装なら keep_recent=3 等で後半3件だけが保持され、
    # 前半2件が切り詰められて分断が起きていた。ターン単位では「直近1
    # ターン全体」が単位のため、5件とも同じ側（保持）に入る。
    result = trim_old_tool_messages(messages, keep_recent_iterations=1, max_chars=10)

    tool_messages = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 5
    assert all(m.content == long_content for m in tool_messages)


def test_parallel_tool_calls_in_old_round_trip_are_trimmed_together() -> None:
    long_content = "y" * 1000
    old_call_ids = [f"old-{i}" for i in range(4)]
    messages = [
        HumanMessage(content="q1"),
        AIMessage(content="", tool_calls=[{"name": "Read", "args": {}, "id": cid} for cid in old_call_ids]),
        *[ToolMessage(content=long_content, name="Read", tool_call_id=cid) for cid in old_call_ids],
        HumanMessage(content="q2"),
        AIMessage(content="", tool_calls=[{"name": "Read", "args": {}, "id": "recent"}]),
        ToolMessage(content=long_content, name="Read", tool_call_id="recent"),
    ]

    result = trim_old_tool_messages(messages, keep_recent_iterations=1, max_chars=10)

    old_tool_messages = [m for m in result if isinstance(m, ToolMessage) and m.tool_call_id.startswith("old-")]
    recent_tool_message = next(m for m in result if isinstance(m, ToolMessage) and m.tool_call_id == "recent")
    # 古いターンの並列tool_calls群は、一部だけでなく全件が揃って切り詰められる。
    assert len(old_tool_messages) == 4
    assert all(m.content != long_content and len(m.content) < 1000 for m in old_tool_messages)
    # 直近ターンは全文保持。
    assert recent_tool_message.content == long_content
