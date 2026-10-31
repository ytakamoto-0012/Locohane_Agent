"""src/context_trim.py の find_iteration_cut_index() の回帰テスト。

context_compaction.py の要約対象切り出しと、context_trim.py の
trim_old_tool_messages/trim_old_ai_messages（直近何反復分を全文保持
するか）の両方で共有される境界計算ロジック。

「1反復」は ReActループ1周＝LLM呼び出し1回（AIMessage 1件とそれに対応する
ToolMessage群）。数える対象を AIMessage にしているのは、過去2つの実装が
どちらも別のものを数えて破綻したため:

1. HumanMessage の個数（＝ユーザーターン数）で数えた実装。analyze_image の
   画像フォローアップや各種 nudge が HumanMessage として履歴に積まれるため
   カウントが汚染され、注入が増えるほど保持範囲が広がってトリムが効かなく
   なった（keep と同数のHumanMessageが溜まると境界が0へ張り付き、トリムが
   完全に無効化された）。
2. 安全な切断点の個数で数えたフォールバック。SystemMessage や注入
   HumanMessage の位置でも境界が立つため、1反復あたり1〜2個と履歴の中身
   次第でぶれ、keep=5 と指定しても実際は3〜4反復しか残らなかった。

切断位置そのものは必ず安全な切断点（未処理の tool_call が無い位置）から
選ぶ。その際の off-by-one: 「未処理のtool_callが無くなった位置」の message
自身のインデックスをそのままスライス境界に使うと、その位置の ToolMessage
自体がスライスから漏れる（境界は message index + 1 でなければならない）。
このテストはその境界計算も含めて検証する。
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.context_trim import find_iteration_cut_index


def _tool_call(call_id: str) -> dict:
    return {"name": "x", "args": {}, "id": call_id}


def test_does_not_cut_between_tool_call_and_its_delayed_response() -> None:
    # ToolMessage(a) → HumanMessage(画像) → ToolMessage(b) という並びを含む。
    # b の応答が返るまでの間（インデックス2,3,4）は絶対に切断点にしてはならない。
    messages = [
        HumanMessage(content="q1"),
        AIMessage(content="", tool_calls=[_tool_call("a"), _tool_call("b")]),
        ToolMessage(content="ra", tool_call_id="a"),
        HumanMessage(content="image"),
        ToolMessage(content="rb", tool_call_id="b"),
        AIMessage(content="final1"),
        HumanMessage(content="q2"),
        AIMessage(content="final2"),
    ]

    # AIMessage は index 1, 5, 7 の3件。直近2反復を残すと index 5 以降が保持side。
    cut_index = find_iteration_cut_index(messages, keep_recent_iterations=2)

    assert cut_index == 5
    assert cut_index not in (2, 3, 4)
    old_messages = messages[:cut_index]
    # 両方の tool_call が old_messages 内で自己完結していること
    # （AIMessage の tool_calls と対応する ToolMessage が両方含まれる）。
    old_tool_call_ids = {
        tc["id"] for m in old_messages if isinstance(m, AIMessage) for tc in (m.tool_calls or [])
    }
    old_response_ids = {m.tool_call_id for m in old_messages if isinstance(m, ToolMessage)}
    assert old_tool_call_ids == old_response_ids


def test_cut_point_found_mid_turn_with_single_human_message() -> None:
    # ユーザー発言が1件しかない（1ターン継続中）が、その中で3回の完結した
    # ツール往復がある。ユーザー発話回数に関係なく、反復単位で切断点を
    # 見つけられること。
    messages = [
        HumanMessage(content="start"),
        AIMessage(content="", tool_calls=[_tool_call("a1")]),
        ToolMessage(content="r1", tool_call_id="a1"),
        AIMessage(content="", tool_calls=[_tool_call("a2")]),
        ToolMessage(content="r2", tool_call_id="a2"),
        AIMessage(content="", tool_calls=[_tool_call("a3")]),
        ToolMessage(content="r3", tool_call_id="a3"),
    ]

    cut_index = find_iteration_cut_index(messages, keep_recent_iterations=2)

    assert cut_index is not None
    old_messages = messages[:cut_index]
    kept_messages = messages[cut_index:]
    assert old_messages == messages[:3]  # 最初の1往復（a1）だけが要約対象
    # kept_messages 側の tool_calls も自己完結していること。
    kept_tool_call_ids = {
        tc["id"] for m in kept_messages if isinstance(m, AIMessage) for tc in (m.tool_calls or [])
    }
    kept_response_ids = {m.tool_call_id for m in kept_messages if isinstance(m, ToolMessage)}
    assert kept_tool_call_ids == kept_response_ids


def test_returns_none_when_only_pending_tool_call_exists() -> None:
    # まだ応答が返っていない tool_call だけの会話は、安全に切り取れる
    # 完結した往復が無いため None を返す。
    messages = [
        HumanMessage(content="q1"),
        AIMessage(content="", tool_calls=[_tool_call("a")]),
    ]

    assert find_iteration_cut_index(messages, keep_recent_iterations=2) is None


def test_returns_none_when_not_enough_history_to_compact() -> None:
    messages = [HumanMessage(content="q1"), AIMessage(content="final")]

    assert find_iteration_cut_index(messages, keep_recent_iterations=2) is None


def test_keep_recent_iterations_zero_does_not_raise_and_cuts_everything() -> None:
    """keep_recent_iterations=0（保持すべき直近の反復が1つも無い）は
    len(ai_indices) - 0 == len(ai_indices) が ai_indices の範囲外を指すため、
    素朴に実装するとIndexErrorになる（旧ユーザーターン版で
    is_compaction_blocked_by_missing_note 経由で実際に踏み抜いた回帰）。
    0は「全反復を要約対象にしてよい」という意味として扱い、安全な切断点の
    うち最大のものを返す（maybe_compact が要約対象そのものを切り詰める際に
    この値を使う）。
    """
    messages = [
        HumanMessage(content="q1"),
        AIMessage(content="", tool_calls=[_tool_call("a")]),
        ToolMessage(content="ra", tool_call_id="a"),
    ]

    cut_index = find_iteration_cut_index(messages, keep_recent_iterations=0)

    assert cut_index == 3
    assert messages[cut_index:] == []


def test_injected_human_messages_do_not_shrink_the_trim_range() -> None:
    """注入HumanMessage（画像フォローアップ・各種nudge）が何件混ざっても、
    保持範囲は反復数だけで決まること。

    旧実装（HumanMessageの個数ベース）では、注入が keep_recent と同数まで
    溜まった時点で境界が先頭付近へ張り付き、トリム・圧縮が事実上無効化
    されていた（実測: サブエージェント既定keep=5・40往復・画像フォロー
    アップ5件で、ToolMessage 200万文字のうち195万文字が無トリムのまま
    LLMへ送られた）。
    """
    messages: list = [SystemMessage(content="SYS"), HumanMessage(content="task")]
    for i in range(10):
        messages.append(AIMessage(content="t", tool_calls=[_tool_call(f"c{i}")]))
        messages.append(ToolMessage(content="r", tool_call_id=f"c{i}"))
        if i % 3 == 0:
            # analyze_image のフォローアップ相当（ツール往復の直後に入る）
            messages.append(HumanMessage(content="[画像フォローアップ]"))

    cut_index = find_iteration_cut_index(messages, keep_recent_iterations=3)

    assert cut_index is not None
    # 直近3反復（AIMessage 3件）だけが保持side に入ること。
    assert sum(1 for m in messages[cut_index:] if isinstance(m, AIMessage)) == 3
    # 保持side の tool_calls が自己完結していること。
    kept = messages[cut_index:]
    kept_call_ids = {tc["id"] for m in kept if isinstance(m, AIMessage) for tc in (m.tool_calls or [])}
    kept_response_ids = {m.tool_call_id for m in kept if isinstance(m, ToolMessage)}
    assert kept_call_ids == kept_response_ids


def test_larger_keep_never_keeps_less() -> None:
    """keep_recent_iterations を増やすほど保持範囲が単調に広がること
    （cut_index が単調に小さくなること）。

    旧実装はユーザーターン単位と往復単位で「1単位」の意味が切り替わる
    ため非単調だった（実測: keep=3 でトリム無効、keep=4 で往復4回ぶんだけ
    保持、と保持量が逆転した）。
    """
    messages: list = [SystemMessage(content="SYS"), HumanMessage(content="task")]
    for i in range(12):
        messages.append(AIMessage(content="t", tool_calls=[_tool_call(f"c{i}")]))
        messages.append(ToolMessage(content="r", tool_call_id=f"c{i}"))
        if i % 4 == 0:
            messages.append(HumanMessage(content="[nudge]"))

    cut_indices = []
    for keep in range(1, 12):
        cut = find_iteration_cut_index(messages, keep_recent_iterations=keep)
        cut_indices.append(len(messages) if cut is None else cut)
    assert cut_indices == sorted(cut_indices, reverse=True)
