"""会話履歴中の古い ToolMessage / AIMessage を切り詰め、LLMへの入力を抑える。

長いReActループ（ファイル読み込み等のツール呼び出しを繰り返すタスク）では、
サイズ上限のないツール実行結果（例: OCR結果のMarkdown全文）が ToolMessage
としてそのまま会話履歴に蓄積し続け、llama.cppへのプロンプトプリフィルが
極端に遅くなる（本番ログで100秒以上の遅延を実測）。

src/graph.py の prebuilt 実装（pre_model_hook）・handwritten 実装
（call_model 直前）の両方から呼ばれる共通ロジック。ToolMessage は1件も
削除しない（LangGraph の create_react_agent が要求する「AIMessage.tool_calls
と対応する ToolMessage が揃っていること」という不変条件を壊さないため）。
content だけを短縮したコピーに差し替えることで、checkpointer 上の永続履歴
には手を付けず、今回のLLM呼び出しへの入力だけを縮める。
"""

from __future__ import annotations

import copy

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from .skills import skill_content_name

_MARKER_TEMPLATE = (
    "\n...[truncated: {original_len} chars total, first {limit} chars shown. "
    "Full text preserved in conversation history. "
    "To read the rest, re-run the tool with different offset/limit parameters]"
)

# Read/Glob/Grep/json_query/analyze_image は src/tools.py の
# _check_file_tools_duplicate / 個別の重複ガードにより「同一引数での
# 再呼び出しは上限回数まで」しか許されない（結果が変わらない読み取り専用
# ツールのため）。ガードのエラー文言は「会話履歴にある前回の実行結果を
# 参照してください」と案内するが、その前回結果が一般的な max_chars で
# 切り詰められていると、実際にはモデルへ渡っていない分を参照させることに
# なり、案内が機能しない。そのためこれらのツール名の ToolMessage だけは
# 別枠の guarded_tool_max_chars を使えるようにする（ツール名は @tool の
# 明示指定と一致させること）。
_DUPLICATE_GUARD_TOOL_NAMES = frozenset({"Read", "Glob", "Grep", "json_query", "analyze_image"})


def latest_skill_content_indices(messages: list[BaseMessage]) -> dict[str, int]:
    """スキル名ごとに、最後に読まれた read_skill 結果（ToolMessage）の位置を返す。

    read_skill の成功結果は `<skill_content name="...">` で始まる
    （src/skills.py の wrap_skill_content）。エラー応答はこの形式でないため
    対象外になる。

    Returns:
        {スキル名: messages 内のインデックス}。
    """
    latest: dict[str, int] = {}
    for i, m in enumerate(messages):
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == "read_skill":
            name = skill_content_name(m.content)
            if name:
                latest[name] = i
    return latest


def trim_old_tool_messages(
    messages: list[BaseMessage],
    *,
    keep_recent_iterations: int,
    max_chars: int,
    guarded_tool_max_chars: int | None = None,
    protect_skill_content: bool = True,
) -> list[BaseMessage]:
    """直近 keep_recent_iterations 反復分の ToolMessage は全文保持し、それより
    古いものは content を先頭 max_chars 文字に切り詰める。

    HumanMessage 内の画像（analyze_image の画像フォローアップ）は切り詰めない。
    古い画像を消すと、読み取った内容を書き出す前だった場合にモデルが再読込を
    試み、analyze_image の重複ガードに拒否されて処理が欠落する（2026-10-01、
    006 レシピ画像ケースで80枚が未処理になった）。画像による肥大化は
    圧縮予告（write_thread_note への書き出し）→圧縮の流れで扱う。

    「1反復」の数え方と境界の選び方は find_iteration_cut_index() を参照
    （ReActループ1周＝AIMessage 1件を単位に数え、切断位置は必ず安全な
    切断点から選ぶ）。単純な ToolMessage 件数指定にしないのは、1回の
    AIMessage が並列発行した複数 tool_calls に対応する ToolMessage 群の
    うち一部だけが保持され残りが切り詰められる、という分断が起きうる
    ため（同じ反復は丸ごと同じ側に入ることを保証するため）。

    Args:
        messages: state["messages"]（元の全履歴。書き換えない）。
        keep_recent_iterations: 全文保持する直近の反復数
            （ReActループ1周＝LLM呼び出し1回）。
        max_chars: 切り詰め後に残す本文の最大文字数（マーカー文言は含まない）。
            _DUPLICATE_GUARD_TOOL_NAMES に含まれないツールの ToolMessage に
            適用する。
        guarded_tool_max_chars: _DUPLICATE_GUARD_TOOL_NAMES に含まれる
            ツール（Read/Glob/Grep/json_query/analyze_image）の ToolMessage
            にだけ適用する切り詰め文字数。None の場合は max_chars を使う
            （従来どおりの挙動）。
        protect_skill_content: True の場合、スキル名ごとに最後に読まれた
            read_skill 結果は切り詰めない。スキル本文は作業全体を通じて守る
            手順書で、先頭だけ残すと手順が欠けたまま作業が続くため
            （同じスキルの古い再読込分は従来どおり切り詰める）。
            context_compaction が要約対象を作る際は False を渡す
            （本文は要約させず、要約の後ろへ別途再添付するため）。

    Returns:
        content だけ差し替えたコピーを含むメッセージ列。書き換え不要な
        メッセージは元のオブジェクトをそのまま含む。
    """
    cut_index = find_iteration_cut_index(messages, keep_recent_iterations)
    keep_from = cut_index or 0
    protected = set(latest_skill_content_indices(messages).values()) if protect_skill_content else set()

    result: list[BaseMessage] = []
    for i, m in enumerate(messages):
        if i >= keep_from or i in protected or not isinstance(m, ToolMessage) or not isinstance(m.content, str):
            result.append(m)
            continue
        limit = max_chars
        if guarded_tool_max_chars is not None and getattr(m, "name", None) in _DUPLICATE_GUARD_TOOL_NAMES:
            limit = guarded_tool_max_chars
        if len(m.content) <= limit:
            result.append(m)
            continue
        marker = _MARKER_TEMPLATE.format(original_len=len(m.content), limit=limit)
        result.append(m.model_copy(update={"content": m.content[:limit] + marker}))
    return result


def _truncate(text: str, max_chars: int) -> str | None:
    """max_chars を超える文字列を切り詰める。切り詰め不要なら None を返す。"""
    if len(text) <= max_chars:
        return None
    return text[:max_chars] + _MARKER_TEMPLATE.format(original_len=len(text), limit=max_chars)


def _trim_tool_call_args(tool_calls: list[dict], max_chars: int) -> list[dict] | None:
    """tool_calls の args に含まれる長い文字列値だけを切り詰める。

    `id`/`name`、および tool_calls の件数は一切変更しない。ToolMessage との
    対応は `tool_call_id` で取られるため、args の中身だけを縮めるぶんには
    「AIMessage.tool_calls と対応する ToolMessage が揃っていること」という
    LangGraph の不変条件を壊さない。

    Args:
        tool_calls: AIMessage.tool_calls（LangChain 正規化済みの dict のリスト）。
        max_chars: 切り詰め後に残す文字数。

    Returns:
        切り詰めが発生した場合のみ新しいリスト。1件も切り詰めなければ None。
    """
    changed = False
    new_calls: list[dict] = []
    for call in tool_calls:
        args = call.get("args")
        if not isinstance(args, dict):
            new_calls.append(call)
            continue
        new_args = None
        for key, value in args.items():
            if not isinstance(value, str):
                continue
            truncated = _truncate(value, max_chars)
            if truncated is None:
                continue
            if new_args is None:
                new_args = copy.copy(args)
            new_args[key] = truncated
        if new_args is None:
            new_calls.append(call)
            continue
        new_call = copy.copy(call)
        new_call["args"] = new_args
        new_calls.append(new_call)
        changed = True
    return new_calls if changed else None


def trim_old_ai_messages(
    messages: list[BaseMessage], *, keep_recent_iterations: int, max_chars: int
) -> list[BaseMessage]:
    """直近 keep_recent_iterations 反復分の AIMessage は全文保持し、それより
    古いものは content と tool_calls の引数を先頭 max_chars 文字に切り詰め、
    thinking（additional_kwargs["reasoning_content"]）を取り除く。

    境界の決め方は trim_old_tool_messages() と同じ find_iteration_cut_index()
    を使う（keep_recent_iterations はこちらの独自の値を渡せるため、tool側と
    ai側で異なる反復数を指定できる）。

    trim_old_tool_messages() は ToolMessage しか見ないため、モデル自身が
    `execute_python_code` の `code` 引数へファイル本文を書き写すような使い方を
    すると、ツール結果側だけを絞っても入力が膨らみ続ける（実測: 大量ファイル
    処理で1リクエストあたり24,833→128,000トークンまで単調増加し、コンテキスト
    上限に張り付いて処理が停止した）。その経路を塞ぐための関数。

    thinking は [llm].reasoning_preserve=true の場合のみ ChatLlamaCpp が
    リクエストへ載せ直すが、そのままだと古い反復の thinking まで全量送られ
    続けるため、本文と同じく保持範囲外では落とす（false の場合はもともと
    送られないので影響しない）。

    Args:
        messages: state["messages"]（元の全履歴。書き換えない）。
        keep_recent_iterations: 全文保持する直近の反復数
            （ReActループ1周＝LLM呼び出し1回）。
        max_chars: 切り詰め後に残す本文の最大文字数（マーカー文言は含まない）。

    Returns:
        content / tool_calls.args / additional_kwargs だけ差し替えたコピーを
        含むメッセージ列。
        書き換え不要なメッセージは元のオブジェクトをそのまま含む。
    """
    cut_index = find_iteration_cut_index(messages, keep_recent_iterations)
    keep_from = cut_index or 0

    result: list[BaseMessage] = []
    for i, m in enumerate(messages):
        if i >= keep_from or not isinstance(m, AIMessage):
            result.append(m)
            continue
        update: dict = {}
        if isinstance(m.content, str):
            truncated = _truncate(m.content, max_chars)
            if truncated is not None:
                update["content"] = truncated
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            new_calls = _trim_tool_call_args(tool_calls, max_chars)
            if new_calls is not None:
                update["tool_calls"] = new_calls
        if "reasoning_content" in m.additional_kwargs:
            update["additional_kwargs"] = {k: v for k, v in m.additional_kwargs.items() if k != "reasoning_content"}
        result.append(m.model_copy(update=update) if update else m)
    return result


def _safe_cut_points(messages: list[BaseMessage]) -> list[int]:
    """`messages[:境界]` が自己完結する（未処理の tool_call を含まない）
    スライス境界を、昇順で列挙する。

    先頭から走査し、各インデックス i で「発行済み tool_call id の集合」と
    「返却済み ToolMessage id の集合」が一致している（＝未処理のツール
    呼び出しが無い）状態になった時点の**スライス境界 i+1**を候補にする。
    境界を message[i] の直後、つまり i+1 にするのが重要で、安全になった
    直後の message[i] 自身（多くの場合は直前の ToolMessage）を境界に
    そのまま使うと、その ToolMessage だけが `messages[:境界]` から漏れて
    対応する AIMessage.tool_calls だけが残る、という壊れ方をする。

    HumanMessage の位置をそのまま境界にできないのは、analyze_image の画像
    フォローアップ（_with_image_followups）やループガードの nudge が
    ツール往復の途中に HumanMessage を挿入するため。LangGraph は tool_call を
    1件ずつ tools ノードへ渡すので、ToolMessage(a) → HumanMessage(画像) →
    ToolMessage(b) という並びが起こりうる。そこで切ると ToolMessage(b) だけが
    対応する AIMessage を失い、OpenAI 互換 API がエラーを返す。

    Returns:
        昇順のスライス境界。先頭（境界0、何も含まない）は自明に安全なため
        常に含む。
    """
    issued_ids: set[str] = set()
    done_ids: set[str] = set()
    points: list[int] = [0]

    for i, m in enumerate(messages):
        # ToolMessage が返ってきた → 対応する tool_call が完了
        if isinstance(m, ToolMessage):
            done_ids.add(m.tool_call_id)
        # AIMessage が tool_calls を発行 → 未完了としてマーク
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                issued_ids.add(tc.get("id", ""))
        # 現在の位置で未処理の tool_call が無い → message[i] を含めた境界 i+1 が安全
        if issued_ids == done_ids:
            points.append(i + 1)
    return points


def _largest_safe_cut_point_upto(safe_points: list[int], limit: int) -> int | None:
    """safe_points のうち limit 以下で最大のものを返す（0 の場合は None）。"""
    cut_index = None
    for boundary in safe_points:
        if boundary > limit:
            break
        cut_index = boundary
    return cut_index if cut_index else None


def find_iteration_cut_index(messages: list[BaseMessage], keep_recent_iterations: int) -> int | None:
    """安全な切断点のうち、末尾から keep_recent_iterations 回目の反復
    （AIMessage）の直前の切断点（スライス境界）を返す。

    src/context_compaction.py の要約対象切り出しと、このモジュール
    （trim_old_tool_messages / trim_old_ai_messages）の「直近何反復分を
    全文保持するか」判定の両方で共有する。

    ここでいう「1反復」は ReActループの1周（model.ainvoke 1回＝AIMessage
    1件と、それに対応する ToolMessage 群）を指す。メインエージェントの
    agent→tools 遷移1回、サブエージェント（run_subagent）の1 iteration が
    そのまま単位になる。

    数える対象を AIMessage にするのが要点:

    - HumanMessage の個数（＝ユーザーターン数）では数えられない。
      analyze_image の画像フォローアップや各種 nudge（ループガード、
      token_guard のソフト警告、幻覚リトライ）が HumanMessage として
      履歴に積まれるため、「ユーザーが何回発話したか」とはずれる。
      注入が増えるほどカウントが汚染され、保持範囲が意図せず広がって
      トリム・圧縮が効かなくなる。
    - 安全な切断点の個数でも数えられない。SystemMessage や注入
      HumanMessage の位置でも（未処理の tool_call が無いため）境界は
      立つので、1反復あたり1〜2個と履歴の中身次第でぶれる。

    一方、切断位置そのものは必ず安全な切断点（_safe_cut_points）から選ぶ
    ため、1回の AIMessage が並列発行した複数 tool_calls に対応する
    ToolMessage 群が「一部だけ保持・一部だけ切り詰め」に分断されることは
    ない（同じ反復は丸ごと同じ側に入る）。

    Args:
        messages: 現在の会話履歴全体。
        keep_recent_iterations: 丸ごと保持する直近の反復数。0 以下を渡すと
            「全反復を古い側にしてよい」という意味になり、末尾の安全な
            切断点を返す（要約対象そのものを切り詰める用途で使う）。

    Returns:
        `messages[:戻り値]` が古い側、それ以降が直近側になる境界値。
        古い側が空になる・安全な境界が無い場合は None。
    """
    safe_points = _safe_cut_points(messages)
    ai_indices = [i for i, m in enumerate(messages) if isinstance(m, AIMessage)]
    target_idx = len(ai_indices) - keep_recent_iterations  # 古い側に入れてよい最後の反復の次
    if target_idx < 0:
        # 反復数そのものが keep_recent_iterations に満たない
        # （＝全反復が保持対象）。切るものが無い。
        return None
    # target_idx == len(ai_indices) は keep_recent_iterations <= 0 のケース。
    # ai_indices の範囲外になるため、境界をメッセージ列の末尾扱いにする。
    target_ai_index = ai_indices[target_idx] if target_idx < len(ai_indices) else len(messages)
    return _largest_safe_cut_point_upto(safe_points, target_ai_index)


def find_last_user_message_cut_index(messages: list[BaseMessage]) -> int | None:
    """末尾の HumanMessage 以降を必ず直近側へ残すための、安全な切断点を返す。

    src/context_compaction.py が要約対象の上限として使う。圧縮は永続履歴を
    要約で置き換える恒久的な操作のため、反復数だけで切ると直近のユーザー
    発話そのものが原文のまま残らない（要約LLMの書きぶり次第で指示内容が
    薄まる）ことがありうる。その下限として使う。

    注入 HumanMessage（画像フォローアップ・各種 nudge）と本来のユーザー
    発話は区別できないため、ここでは区別しない。注入が末尾に来ている場合は
    保護範囲が狭まるだけで、壊れる方向には働かない。

    Returns:
        末尾の HumanMessage の位置以下で最大の安全な切断点。HumanMessage が
        無い、または境界が0にしかならない場合は None。
    """
    last_human_index = None
    for i, m in enumerate(messages):
        if isinstance(m, HumanMessage):
            last_human_index = i
    if last_human_index is None:
        return None
    return _largest_safe_cut_point_upto(_safe_cut_points(messages), last_human_index)


def last_ai_total_tokens(messages: list[BaseMessage]) -> int | None:
    """直近の AIMessage の usage_metadata から total_tokens を取り出す。

    src/main_token_guard.py の閾値判定と同一の取得元（末尾から最初に見つかった
    AIMessage の usage_metadata）を、is_trigger_reached() と共有するために
    ここへ置く。

    Args:
        messages: 会話履歴。末尾から最初に見つかった AIMessage を見る。

    Returns:
        total_tokens。AIMessage が無い、または usage_metadata を取得できない
        （config.ini [llm].track_token_usage=false 等）場合は None。
    """
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        usage = getattr(message, "usage_metadata", None)
        if not isinstance(usage, dict):
            return None
        total = usage.get("total_tokens")
        return int(total) if total else None
    return None


# maybe_compact()（src/context_compaction.py）が圧縮時に丸ごと保持した
# AIMessage の複製へ付ける response_metadata のキー。複製の usage_metadata は
# 圧縮前の（大きな）値のままのため、is_trigger_reached() はこの印の付いた
# AIMessage を判定から除外する。response_metadata は langchain-openai が
# リクエストへ含めないため、LLM への入力には影響しない。
COMPACTION_KEPT_KEY = "locohane_compaction_kept"


def is_trigger_reached(messages: list[BaseMessage], trigger_total_tokens: int) -> bool:
    """トリムを発動すべきか判定する（Claude API の context editing
    （clear_tool_uses_20250919）の trigger.value 相当）。

    一度でも閾値に達したら、以後その会話ではトリムを継続する（履歴内の
    いずれかの AIMessage の total_tokens が閾値以上なら True）。直近1件だけで
    判定すると、トリムで入力が閾値未満に下がる→次の呼び出しでトリム解除→
    閾値超過→再トリム、と呼び出しごとに入力の途中が入れ替わり、llama-server の
    プレフィックスキャッシュが毎回外れて全量プリフィルし直しになる
    （2026-09-30 のログで、10万トークン規模の読み直しに1回約3分かかっていた）。
    トリム後の入力は古い側から順に確定していくため、継続していればキャッシュが効く。

    コンテキスト圧縮（maybe_compact）が走ると圧縮前のメッセージは履歴から
    消えるため、トリムも自然に解除される。圧縮時に保持された AIMessage の
    複製（COMPACTION_KEPT_KEY 付き）は圧縮前の usage_metadata を持つため除外する。

    Args:
        messages: 判定対象の会話履歴（トリム前）。
        trigger_total_tokens: いずれかの AIMessage の total_tokens がこの値以上に
            なって初めてトリムを発動する閾値。0以下を指定すると、常に発動する
            （この閾値機能が無かった旧来の挙動と同じ）。

    Returns:
        トリムを発動すべきなら True。track_token_usage=false 等で
        total_tokens を取得できない場合は、閾値未到達とみなし False を返す
        （src/main_token_guard.py の maybe_append_token_guard と同じ安全側判断）。
    """
    if trigger_total_tokens <= 0:
        return True
    for message in messages:
        if not isinstance(message, AIMessage) or message.response_metadata.get(COMPACTION_KEPT_KEY):
            continue
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, dict) and (usage.get("total_tokens") or 0) >= trigger_total_tokens:
            return True
    return False
