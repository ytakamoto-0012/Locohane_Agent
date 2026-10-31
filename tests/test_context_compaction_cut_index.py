"""_find_compaction_cut_index()（圧縮の要約対象境界）の回帰テスト。

圧縮は永続履歴を要約で置き換える恒久的な操作のため、trim と同じ反復単位
（find_iteration_cut_index）だけで切ると、直近のユーザー発話まで要約に
飲まれて原文が残らないことがある。そこで「末尾の HumanMessage 以降は必ず
原文のまま残す」上限を被せる。

ただしこの上限をそのまま適用すると、ユーザー発話が履歴の先頭近くにしか
無いケース（1ターン内でLLM呼び出しを何十回も繰り返す長時間タスク。
サブエージェントの典型形）で境界が0へ張り付き、圧縮の機会が一度も来なく
なる — コンテキスト上限に張り付いたまま停止する、まさに圧縮で避けたい
状態になる。そのため上限は「適用しても要約対象が残る」場合にのみ効かせる。
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.context_compaction import _find_compaction_cut_index
from src.context_trim import find_iteration_cut_index


def _round_trip(call_id: str) -> list:
    return [
        AIMessage(content="t", tool_calls=[{"name": "Read", "args": {}, "id": call_id}]),
        ToolMessage(content="r", tool_call_id=call_id),
    ]


def _messages_with_two_user_turns() -> list:
    messages: list = [HumanMessage(content="q1")]
    for i in range(5):
        messages += _round_trip(f"a{i}")
    messages.append(HumanMessage(content="q2"))
    for i in range(2):
        messages += _round_trip(f"b{i}")
    return messages


def test_last_user_message_is_never_summarized_away() -> None:
    """反復数だけで切ると直近のユーザー発話が要約対象に入ってしまう場合、
    その発話の直前で止めること。"""
    messages = _messages_with_two_user_turns()
    last_human_index = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))

    # 反復単位だけなら q2 より後ろ（＝q2を要約対象に含む位置）で切ってしまう。
    assert find_iteration_cut_index(messages, 1) > last_human_index

    cut_index = _find_compaction_cut_index(messages, 1)

    assert cut_index is not None
    assert cut_index <= last_human_index
    # q2 が保持side（原文のまま）に残ること。
    assert any(isinstance(m, HumanMessage) and m.content == "q2" for m in messages[cut_index:])


def test_iteration_boundary_wins_when_already_earlier() -> None:
    """反復単位の境界が既に末尾HumanMessageより手前なら、上限は何もしない
    （保持範囲を不必要に広げて圧縮量を減らさない）。"""
    messages = _messages_with_two_user_turns()

    assert _find_compaction_cut_index(messages, 3) == find_iteration_cut_index(messages, 3)


def test_protection_is_skipped_when_it_would_block_compaction_entirely() -> None:
    """ユーザー発話が先頭にしか無い長時間タスクでは、上限を適用すると要約対象が
    空になり圧縮の機会が永久に来ない。この場合は上限を適用しないこと。

    これは旧「ユーザーターン単位」実装で実際に起きていた退行の再発防止
    （1ターンで何十回もLLM呼び出しを繰り返す間、圧縮が一度も発火せず
    コンテキスト上限に張り付いた）。
    """
    messages: list = [HumanMessage(content="q1")]
    for i in range(8):
        messages += _round_trip(f"x{i}")

    cut_index = _find_compaction_cut_index(messages, 3)

    assert cut_index is not None
    assert cut_index > 0
    assert messages[:cut_index]  # 要約対象が空でない
    assert cut_index == find_iteration_cut_index(messages, 3)


def test_returns_none_when_iteration_cut_is_none() -> None:
    """反復数が足りず切るものが無い場合は、上限の有無に関わらず None。"""
    messages: list = [HumanMessage(content="q1"), AIMessage(content="final")]

    assert _find_compaction_cut_index(messages, 3) is None
