"""会話履歴が長くなった際に、古い部分をLLM自身に要約させて圧縮する。

ClaudeCode の compact 相当。src/context_trim.py が「今回のLLM呼び出しへの
入力だけを縮める・永続履歴（checkpointer上のメッセージ）は書き換えない」
方針であるのに対し、こちらは永続履歴自体を書き換える恒久的な圧縮であり、
会話が長引くほどプリフィル遅延・トークン量の両方に効く。

app.py の on_message から、そのターンの astream_events ループが完全に
終了した後（進行中のグラフ実行と aupdate_state が競合しないタイミング）に
呼ばれる想定。
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from .config import Config
from .context_trim import (
    COMPACTION_KEPT_KEY,
    find_iteration_cut_index,
    find_last_user_message_cut_index,
    last_ai_total_tokens,
    latest_skill_content_indices,
    trim_old_tool_messages,
)
from .images import IMAGE_REFS_KEY
from .skills import skill_content_name
from .llm import (
    LLM_CONNECTION_ERRORS,
    ThinkingLoopDetected,
    aclose_model_client,
    build_model,
    mark_last_endpoint_failed,
)

logger = logging.getLogger(__name__)


CompactionUsageListener = Callable[[str, str, dict], Awaitable[None]]
# app.py が登録する、圧縮処理のトークン使用量をセッションの会話累計へ加算する関数。
# app.py を直接 import すると `chainlit run` が app.py を別モジュールとして再実行して
# 壊れるため、src/plan_persist.py と同じ登録方式にしている。
_usage_listener: CompactionUsageListener | None = None


def register_compaction_usage_listener(fn: CompactionUsageListener | None) -> None:
    global _usage_listener
    _usage_listener = fn


def _log_compaction_usage(kind: str, role: str, response) -> dict | None:
    """圧縮処理自身のLLM呼び出しのトークン使用量をログへ残し、usage を返す。

    これらの呼び出しはグラフ外の ainvoke のため app.py の on_chat_model_end
    （「トークン使用量 thread_id=...」行）では捕捉されない。設定ダッシュボードの
    トークン推移グラフ（admin/monitor.py の token_history）がこの行を読んで累計へ
    加算する。thread_id はログのフォーマット側（[thread=...]）で付く。
    """
    usage = getattr(response, "usage_metadata", None)
    # [llm].track_token_usage=false なら None。dict 以外（テストのスタブ等）も記録しない
    # （ここで例外を出すと呼び出し元の except に捕まり、要約自体が失敗扱いになる）。
    if not isinstance(usage, dict) or not usage:
        return None
    logger.info(
        "compaction token usage kind=%s role=%s call(in=%d,out=%d,total=%d)",
        kind,
        role,
        usage.get("input_tokens", 0) or 0,
        usage.get("output_tokens", 0) or 0,
        usage.get("total_tokens", 0) or 0,
    )
    return usage


async def _record_compaction_usage(kind: str, role: str, response) -> None:
    """ログへ残した上で、登録済みのリスナー（app.py）でセッションの会話累計へ加算する。

    集計の失敗で要約・圧縮そのものを失敗させないよう、リスナーの例外は握りつぶす。
    """
    usage = _log_compaction_usage(kind, role, response)
    if usage is None or _usage_listener is None:
        return
    try:
        await _usage_listener(kind, role, usage)
    except Exception:  # noqa: BLE001 - 付帯の集計で圧縮を止めない
        logger.warning("Failed to tally compaction token usage", exc_info=True)


_SUMMARY_HEADER = "[Automatic summary: part of the earlier conversation was summarized for context compaction. Continue the work based on it]\n"
_PLAN_STATUS_HEADER = "[Approved plan (highest priority task). Added mechanically by the code, independent of the summary]\n"
_THREAD_NOTE_STATUS_HEADER = (
    "[Current state of the thread notes. Added mechanically by the code, independent of the summary. "
    "If you need concrete facts (values, counts, locations, etc.) not in the summary, "
    "read the topic names listed here with read_thread_note as they are]\n"
)
_SKILL_REATTACH_HEADER = (
    "[Skill bodies loaded with read_skill before compaction. Added mechanically by the code, independent of the summary. "
    "The bodies here do not need to be reloaded]\n"
)
# 要約メッセージ内の再添付セクションの区切り（再添付は要約メッセージの末尾に置く）。
_SKILL_REATTACH_SEPARATOR = "\n\n" + _SKILL_REATTACH_HEADER
_OMITTED_SKILLS_PREFIX = "The following skills were also loaded, but their bodies are omitted (reload with read_skill if needed): "
_REATTACHED_BLOCK_SPLIT_RE = re.compile(r'\n\n(?=<skill_content name=")')
_PINNED_INSTRUCTION_HEADER = "[The task given by the delegator (verbatim). Added mechanically by the code, independent of the summary]\n"
_PRE_NOTE_MARKER = "[Context compaction is approaching]"
_LOOP_NUDGE_TEXT = "The previous summary got stuck repeating the same content and was stopped. Calm down and summarize the conversation history concisely once more."


def maybe_append_precompact_note_nudge(messages: list[BaseMessage], config: Config) -> list[BaseMessage]:
    """圧縮（要約）が近づいたら、write_thread_noteへの書き出しを促すメッセージを末尾へ足す。

    src/main_token_guard.py の maybe_append_token_guard と同じ考え方
    （今回のLLM呼び出しへの入力にだけ差し込み、state・checkpointer上の
    永続履歴は書き換えない）。maybe_compact() による要約は永続履歴を
    書き換える恒久的な操作であり、要約LLMの精度次第で古い会話中の具体的な
    事実（値・件数・該当箇所等）が薄まって失われうる。要約対象から外れる
    前に、そうした事実を write_thread_note（ファイルへの追記であり要約の
    影響を受けない）へ退避させる機会をモデルに与える狙い。

    Args:
        messages: 今回のモデル呼び出しへ渡す予定のメッセージ列
            （context_trim 適用後のものを想定）。書き換えない。
        config: context_compaction_pre_note_* を含むアプリ設定。

    Returns:
        閾値に達していれば末尾に HumanMessage を1件足した新しいリスト。
        達していない場合・無効化されている場合、または直近
        keep_recent_iterations 反復以内に write_thread_note が既に呼ばれて
        いる場合は、引数の messages をそのまま返す。
    """
    if not config.context_compaction_enabled or config.context_compaction_pre_note_threshold <= 0:
        return messages
    total = last_ai_total_tokens(messages)
    if total is None or total < config.context_compaction_pre_note_threshold:
        return messages
    if _write_thread_note_called_recently(messages, config.context_compaction_keep_recent_iterations):
        # 直近で既に書き出し済みなら、同じ facts を書かせるためだけの
        # 再ナッジは無意味かつ有害（ナッジ自体・再書き込み自体がトークンを
        # 消費し、閾値超過が続く限り毎ターン再発火して無限ループ状態になる）。
        return messages

    logger.warning(
        "Main agent token usage reached the context compaction warning threshold (%d) (last response: %d tokens). Prompting it to write to write_thread_note",
        config.context_compaction_pre_note_threshold,
        total,
    )
    return [*messages, HumanMessage(content=f"{_PRE_NOTE_MARKER}\n{config.context_compaction_pre_note_warning_text}")]


def _write_thread_note_called_recently(messages: list[BaseMessage], keep_recent_iterations: int) -> bool:
    """直近 keep_recent_iterations 反復以内に write_thread_note が
    呼ばれていれば True を返す。

    「直近何反復か」の切り出しには find_iteration_cut_index を使う
    （境界より後ろが keep_recent_iterations 分の直近範囲）。単純に「前回の
    ナッジ以降」で判定しないのは、ナッジ自体が永続履歴へ書き込まれない
    一時的な差し込みメッセージであり、状態として覚えておく場所が無いため
    （src/main_token_guard.py の maybe_append_token_guard と同じ、今回の
    呼び出し限りの差し込み方針）。keep_recent_iterations はどのみち圧縮時に
    丸ごと残る範囲を決めている値であり、その範囲内に書き出し済みなら
    再度書かせても得るものが無い。
    """
    cut_index = find_iteration_cut_index(messages, keep_recent_iterations)
    recent = messages[cut_index:] if cut_index is not None else messages
    return any(
        isinstance(m, AIMessage) and any(tc.get("name") == "write_thread_note" for tc in (m.tool_calls or []))
        for m in recent
    )


def _find_compaction_cut_index(messages: list[BaseMessage], keep_recent_iterations: int) -> int | None:
    """圧縮（要約）で「ここより前を要約対象にする」境界を決める。

    基本は find_iteration_cut_index（直近 keep_recent_iterations 反復を
    丸ごと保持）だが、圧縮は永続履歴を要約で置き換える恒久的な操作のため、
    直近のユーザー発話まで要約に飲まれないよう
    find_last_user_message_cut_index を上限として被せる。

    ただしこの上限をそのまま適用すると、ユーザー発話が履歴の先頭近くに
    しか無いケース（1ターン内でLLM呼び出しを何十回も繰り返す長時間タスク。
    サブエージェントの典型形）で境界が0へ張り付き、圧縮の機会が一度も
    来なくなる（コンテキスト上限に張り付いたまま停止する、まさに圧縮で
    避けたい状態）。そのため上限は「0でない＝要約対象が残る」場合にのみ
    適用する。

    Args:
        messages: 圧縮対象の会話履歴全体。
        keep_recent_iterations: 丸ごと保持する直近の反復数。

    Returns:
        `messages[:戻り値]` が要約対象、それ以降が保持対象になる境界値。
        圧縮しても縮まらない・安全な境界が無い場合は None。
    """
    cut_index = find_iteration_cut_index(messages, keep_recent_iterations)
    if cut_index is None:
        return None
    protected = find_last_user_message_cut_index(messages)
    if protected and protected < cut_index:
        return protected
    return cut_index


def is_compaction_blocked_by_missing_note(
    messages: list[BaseMessage], config: Config, skip_count: int
) -> bool:
    """圧縮条件（閾値）を満たしていても、write_thread_note未呼び出しのため
    今回は圧縮を見送るべきかを判定する。

    maybe_append_precompact_note_nudge による事前の注意喚起はあくまで促す
    だけで、LLMが無視し続けても should_compact() が True である限り圧縮
    （永続履歴の要約）自体は強制的に発火してしまう。それでは要約に含まれ
    なかった古い会話中の具体的な事実が復元不能なまま失われうるため、
    write_thread_noteへの書き出しを圧縮の前提条件にする。

    ただしLLMが最後まで書き出さないケースに備え、見送った回数が
    context_compaction_require_note_max_skips に達したら記録なしでも
    圧縮を強制する（さもないとコンテキスト上限に張り付いたまま停止する
    従来の問題が再発するため。src/main_token_guard.py docstring参照）。

    Args:
        messages: 圧縮対象の会話履歴全体。
        config: context_compaction_require_note_max_skips /
            context_compaction_keep_recent_iterations を含むアプリ設定。
        skip_count: 直近で連続して見送った回数（呼び出し元が保持・更新する。
            この関数自体は状態を持たない）。

    Returns:
        見送るべきなら True（呼び出し元は skip_count を +1 し、今回は
        圧縮を実行しない）。直近で書き出し済み、または見送り回数が
        require_note_max_skips に達していれば False（圧縮してよい。
        呼び出し元は skip_count を 0 へリセットする）。
    """
    if _write_thread_note_called_recently(messages, config.context_compaction_keep_recent_iterations):
        return False
    max_skips = config.context_compaction_require_note_max_skips
    if max_skips > 0 and skip_count >= max_skips:
        return False
    return True


async def force_write_thread_note(
    messages: list[BaseMessage], model, config: Config
) -> tuple[AIMessage, list[ToolMessage]] | None:
    """write_thread_note が未呼び出しのまま圧縮閾値に達した際、見送る代わりに
    その場でLLMへ write_thread_note の実行を強制する。

    is_compaction_blocked_by_missing_note による「見送り」は、ユーザーの
    次発話でLLMがナッジに応じてくれるのを待つだけであり、無視され続けると
    require_note_max_skips 回まで見送った末に記録なしで圧縮が強制される
    （事実退避の機会が一度も無いまま要約されうる）。この関数は、その場で
    write_thread_note 以外のツールを使えなくした上でモデルを1回呼び出し、
    確実に書き出させる。

    実現方法: OpenAI互換の tool_choice で特定関数名を固定する方式
    （{"type":"function","function":{"name":"write_thread_note"}}）は
    llama-server（本アプリの前提バックエンド）では無視されることを実機で
    確認済み。一方 tool_choice="required" は「bind_tools に渡したツールの
    どれかを必ず呼ぶ」という強制として機能するため、ツール一覧を
    write_thread_note 1件だけに絞った上で tool_choice="required" にする
    ことで、実質的に特定ツールの強制呼び出しを実現する。

    Args:
        messages: 現在の会話履歴全体（圧縮対象を含む）。
        model: build_model() が返す素のモデル（未 bind_tools でよい。
            この関数の中で write_thread_note のみへ bind_tools し直す）。
        config: context_compaction_keep_recent_iterations /
            context_compaction_summary_source_max_chars /
            context_compaction_pre_note_warning_text を含むアプリ設定。

    Returns:
        書き出しに成功した場合、実際に永続履歴へ追記すべき
        (AIMessage, ToolMessageのリスト) のペア。ToolMessage は
        AIMessage.tool_calls の**全件**に対応する（1件でも欠けると
        OpenAI互換APIが以降のリクエストを拒否するため。モデルが
        write_thread_note をトピック別に複数回呼ぶことがある）。
        LLM呼び出し自体の失敗、tool_calls が空、または全ての呼び出しが
        topic/content 引数の不足・書き込み失敗で1件も書けなかった場合は
        None（呼び出し元は従来の見送り処理へフォールバックする）。
    """
    from .tools.thread_notes import write_thread_note

    cut_index = find_iteration_cut_index(messages, config.context_compaction_keep_recent_iterations)
    old_messages = messages[:cut_index] if cut_index else messages
    trimmed_old = _without_reattached_skills(
        trim_old_tool_messages(
            old_messages,
            keep_recent_iterations=0,
            max_chars=config.context_compaction_summary_source_max_chars,
            protect_skill_content=False,
        )
    )
    text = _messages_to_text(trimmed_old)
    prompt = (
        f"{config.context_compaction_pre_note_warning_text}\n\n"
        "---\n\n# Conversation history (old parts that may be lost by the summary)\n\n" + text
    )

    # bind_tools も try の内側に置く: ここは「見送る代わりの追加の試み」で
    # あり、失敗しても呼び出し元（app.py / run_subagent）は従来どおり見送りへ
    # フォールバックできればよい。bind_tools の失敗で本編のターンごと落とす
    # 価値は無い。
    try:
        bound_model = model.bind_tools([write_thread_note], tool_choice="required")
        response = await bound_model.ainvoke([HumanMessage(content=prompt)])
        await _record_compaction_usage("thread_note", "-", response)
    except ThinkingLoopDetected:
        # maybe_compact の except ThinkingLoopDetected と同じ理由で、この
        # モデルインスタンス専用のクライアントを無条件に強制クローズする
        # （閉じないまま return すると、ストリームの後始末が終わらず
        # llama-server側の生成が続き、次のLLM呼び出しが応答ヘッダー待ちで
        # ハングし続ける）。ここはリトライせず見送りへフォールバックするが、
        # 後始末だけは必ず行う。クローズ対象は bind_tools 後の
        # RunnableBinding ではなく素の model（httpx.AsyncClient を保持して
        # いるのはこちら）。
        await aclose_model_client(model)
        logger.exception("Forced write_thread_note failed: loop detected in the summarizer LLM")
        return None
    except Exception:
        logger.exception("Forced write_thread_note failed (LLM call error)")
        return None

    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        logger.warning("Forced write_thread_note returned a response with empty tool_calls")
        return None

    # tool_choice="required" でツールを1件に絞っていても、モデルが
    # write_thread_note を複数回（トピック別に）呼ぶことはありうる。response を
    # そのまま永続履歴へ追記する以上、**全ての tool_call に ToolMessage を
    # 返さないと対応の取れない tool_call が残り**、次回以降のLLM呼び出しが
    # OpenAI互換APIのバリデーションで落ち続ける。引数不足・実行失敗の場合も
    # エラー文言の ToolMessage を返して対応を取る。
    tool_messages: list[ToolMessage] = []
    written_topics: list[str] = []
    for call in tool_calls:
        args = call.get("args") or {}
        topic = args.get("topic")
        content = args.get("content")
        if not topic or not content:
            logger.warning("Forced write_thread_note was missing topic/content arguments: %r", args)
            result_text = "Error: nothing was written because topic/content was missing."
        else:
            try:
                result_text = await write_thread_note.ainvoke({"topic": topic, "content": content})
            except Exception:
                logger.exception("Forced write_thread_note failed to run the tool itself")
                result_text = "Error: failed to write to the thread note."
            else:
                written_topics.append(topic)
        tool_messages.append(ToolMessage(content=result_text, tool_call_id=call.get("id", "")))

    if not written_topics:
        # 1件も書けていない。履歴に無意味なやり取りを残さず、従来の見送りへ
        # フォールバックする（呼び出し元は skip_count を進める）。
        return None

    logger.warning("Forced write_thread_note succeeded (topics=%r)", written_topics)
    return response, tool_messages


def should_compact(
    cumulative_usage: dict | None,
    last_usage: dict | None,
    message_count: int,
    config: Config,
) -> bool:
    """メインエージェントの累積トークン使用量と直近1回分の使用量から、圧縮を検討すべきか判定する。

    2つの独立した条件のOR判定になっている:

    1. 累積条件: 直近1回のLLM呼び出し分だけで判定すると、context_trim による
       送信ペイロード削減の影響で閾値未満に収まり続け、圧縮が長期間発火しない
       まま永続履歴（state["messages"]）だけが肥大化しうる。そのため、会話
       全体を通じたメインエージェントの累積トークン量（サブエージェント呼び出し
       分は含まない）でも判定する。
    2. 単発条件: 会話全体の累積は低くても、1ターンで巨大なツール結果や
       ファイル内容を一気に積むなどして、単発のリクエストがモデルの
       context window上限に迫るケースがある。これは累積条件では捉えられない
       ため、直近1回の total_tokens も別途見る。

    Args:
        cumulative_usage: app.py が保持する token_usage_cumulative_main
            （{"input","output","total"} を持つ累積集計辞書）。track_token_usage=false
            等で一度も集計されていない場合は None。
        last_usage: on_chat_model_end で得た直近1回分の usage_metadata
            （"total_tokens" キーを持つ dict）。取得できなかった場合は None。
        message_count: 現在の会話履歴（state["messages"]）の件数。
        config: context_compaction_* 設定を含むアプリ設定。

    Returns:
        圧縮を試みるべきなら True。
    """
    if not config.context_compaction_enabled:
        return False
    if message_count < config.context_compaction_min_messages_to_compact:
        return False
    cumulative_total = (cumulative_usage or {}).get("total", 0) or 0
    if cumulative_total >= config.context_compaction_token_threshold:
        return True
    last_total = (last_usage or {}).get("total_tokens", 0) or 0
    return last_total >= config.context_compaction_single_request_token_threshold


def _render_reattached_skills(
    old_messages: list[BaseMessage],
    kept_messages: list[BaseMessage],
    *,
    max_chars_per_skill: int,
    total_max_chars: int,
) -> str:
    """要約で消える read_skill 結果（スキル本文）を、要約の後ろへ再添付する文字列にする。

    plan_status / thread_note_status と同じく、要約LLMの精度に依存させず
    原文を機械的に残す。スキル本文は作業全体を通じて守る手順書で、要約で
    薄まると手順が欠けたまま作業が続くため（ClaudeCode の compaction も
    起動済みスキルを要約の後ろへ再添付している）。

    対象は old_messages 内の各スキルの最新の read_skill 結果。2回目以降の
    圧縮では、前回の要約メッセージに再添付済みの本文（名前だけ列挙した分を
    含む）も old_messages 側に入るため引き継ぐ（read_skill の ToolMessage
    だけを見ると、圧縮が重なるたびにスキル本文が消える）。kept_messages
    側に同じスキルの結果があればそちらが残るため除く。新しく読んだスキルから
    順に詰め、1件あたり max_chars_per_skill・合計 total_max_chars を超える分は
    切り詰める。合計に収まらなかったスキルは名前だけ列挙し再読込を促す。

    Returns:
        再添付する文字列。対象が無ければ空文字列。
    """
    kept_names = set(latest_skill_content_indices(kept_messages))
    # {スキル名: (新しさの順序キー, 本文 or None（名前だけ列挙済み）)}
    latest: dict[str, tuple[tuple[int, int], str | None]] = {}
    for i, m in enumerate(old_messages):
        if isinstance(m, HumanMessage):
            blocks, omitted_before = _parse_reattached_skills(m.content)
            # 再添付セクション内は新しいスキルが先頭。
            for j, (name, content) in enumerate(blocks):
                latest[name] = ((i, -j), content)
            for name in omitted_before:
                latest.setdefault(name, ((i, -len(blocks)), None))
    for name, i in latest_skill_content_indices(old_messages).items():
        latest[name] = ((i, 0), old_messages[i].content)
    ordered = sorted(
        ((order, name, content) for name, (order, content) in latest.items() if name not in kept_names),
        reverse=True,
    )
    parts: list[str] = []
    omitted: list[str] = []
    remaining = total_max_chars
    for _, name, content in ordered:
        if content is None:
            omitted.append(name)
            continue
        if len(content) > max_chars_per_skill:
            content = (
                content[:max_chars_per_skill]
                + f"\n...[only the first {max_chars_per_skill} characters of the body of skill '{name}'. Reload with read_skill if you need the rest]"
            )
        if len(content) > remaining:
            # 合計の残り枠に合わせて本文の切れ端だけを残しても手順として役に立たないため、名前だけにする。
            omitted.append(name)
            continue
        parts.append(content)
        remaining -= len(content)
    if omitted:
        parts.append(_OMITTED_SKILLS_PREFIX + ", ".join(omitted))
    return "\n\n".join(parts)


def _parse_reattached_skills(content: object) -> tuple[list[tuple[str, str]], list[str]]:
    """前回の圧縮で要約メッセージへ再添付したスキル本文を取り出す。

    Returns:
        ([(スキル名, 本文), ...], [名前だけ列挙したスキル名, ...])。
        再添付セクションが無ければ ([], [])。
    """
    if not isinstance(content, str):
        return [], []
    _, sep, section = content.partition(_SKILL_REATTACH_SEPARATOR)
    if not sep:
        return [], []
    omitted: list[str] = []
    if section.startswith(_OMITTED_SKILLS_PREFIX):
        section, omitted_text = "", section[len(_OMITTED_SKILLS_PREFIX) :]
        omitted = omitted_text.split(", ")
    else:
        section, _, omitted_text = section.partition("\n\n" + _OMITTED_SKILLS_PREFIX)
        omitted = omitted_text.split(", ") if omitted_text else []
    blocks: list[tuple[str, str]] = []
    for block in _REATTACHED_BLOCK_SPLIT_RE.split(section):
        name = skill_content_name(block)
        if name:
            blocks.append((name, block))
    return blocks, [name for name in omitted if name]


def _without_reattached_skills(messages: list[BaseMessage]) -> list[BaseMessage]:
    """要約メッセージ末尾の再添付スキル本文を除いたメッセージ列を返す（要約LLMへ渡す用）。"""
    result: list[BaseMessage] = []
    for m in messages:
        if isinstance(m, HumanMessage) and isinstance(m.content, str) and _SKILL_REATTACH_SEPARATOR in m.content:
            m = m.model_copy(update={"content": m.content.partition(_SKILL_REATTACH_SEPARATOR)[0]})
        result.append(m)
    return result


def content_to_text(content: object, image_refs: list[str] | None = None) -> str:
    """メッセージの content をテキストにする。画像等の非テキスト部分は参照に置き換える。

    analyze_image が履歴へ足す画像付き HumanMessage は content がリスト
    （base64 の image_url を含む）で、str() するとbase64がそのまま要約LLMへ
    渡り、1回の要約リクエストが1000万トークン規模になってコンテキスト長
    超過で必ず失敗していた（2026-10-01、006 レシピ画像ケースで発覚）。

    Args:
        content: メッセージの content。
        image_refs: content 内の画像ブロックと同じ順の参照（`@N 絶対パス` 等。
            メッセージの additional_kwargs[images.IMAGE_REFS_KEY]）。画像は
            `[画像: <参照>]` になり、参照が無い画像は `[画像]` になる。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    refs = iter(image_refs or [])
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            ref = next(refs, None)
            parts.append(f"[image: {ref}]" if ref else "[image]")
    return "\n".join(parts)


def message_content_to_text(message: BaseMessage) -> str:
    """content_to_text() に、メッセージが持つ画像の参照を渡して呼ぶ。"""
    return content_to_text(message.content, message.additional_kwargs.get(IMAGE_REFS_KEY))


def _messages_to_text(messages: list[BaseMessage]) -> str:
    """要約対象メッセージ列を、要約LLMへ渡すプレーンテキストへ変換する。"""
    lines = []
    for m in messages:
        role = getattr(m, "type", m.__class__.__name__)
        content = message_content_to_text(m)
        if not content.strip():
            continue
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


async def maybe_compact(
    messages: list[BaseMessage],
    model,
    config: Config,
    *,
    role: Literal["main", "sub"] = "main",
    pinned_instruction: str | None = None,
) -> list[BaseMessage] | None:
    """必要なら古い会話履歴を要約し、状態更新用のメッセージ列を返す。

    呼び出し元は、返り値が None でなければ次のように使うこと（1回の
    aupdate_state 呼び出しで完結させ、途中の中間状態を作らないこと。
    RemoveMessageによる全削除と要約結果の追加を2回に分けて呼ぶと、
    その間に別の処理が会話履歴を読みに行った場合に不整合な中間状態
    （メッセージが一時的に空）を観測しうるため）:

        new_messages = await maybe_compact(messages, model, config, role="main")
        if new_messages is not None:
            await graph.aupdate_state(
                config,
                {"messages": [RemoveMessage(id=m.id) for m in messages] + new_messages},
            )

    要約LLM呼び出しが通信エラー（LLM_CONNECTION_ERRORS）またはループ検知
    （ThinkingLoopDetected）で失敗した場合、app.py の on_message /
    src/subagent.py の run_subagent と同じ方針で接続先を切り替えつつ
    モデルを再構築してリトライする（下記 Notes 参照）。両方の予算を
    使い切った場合のみ、従来通り None を返して今回の圧縮をスキップする。

    Args:
        messages: 現在の会話履歴全体（state["messages"]）。
        model: 要約に使うモデル（build_model() の素のインスタンスでよい。
            ツールは不要）。リトライが発生した場合、このインスタンスは
            以降使われなくなる（新しいインスタンスに差し替わる）。
        config: context_compaction_* 設定を含むアプリ設定。
        role: "main"（app.py の要約呼び出し）または "sub"（dispatch_agent
            内の要約呼び出し）。接続先の再選択・リトライ回数上限（main:
            graph_connection_error_max_retries / sub:
            subagent_background_llm_timeout_max_retries）・クライアントの
            強制クローズ方針を build_model() のロールごとの接続先設定に
            合わせるために使う。
        pinned_instruction: 要約LLMに頼らず、要約結果の先頭へ原文のまま
            機械的に付与する指示（src/subagent.py が dispatch_agent の task を
            渡す）。サブエージェントの task は履歴の先頭にしか無いため
            _find_compaction_cut_index の直近ユーザー発話保護が効かず、
            省略すると出力形式・調査範囲・禁止事項等の委譲時の指示が要約で
            薄まり、圧縮を重ねるほど劣化していく。

    Returns:
        要約が実行された場合、「要約結果のHumanMessage」+「直近ターンの
        メッセージ（新しいidを振った複製）」のリスト。圧縮不要、または
        要約LLM呼び出しに失敗した場合は None（呼び出し元は何もしない）。

    Notes:
        ThinkingLoopDetected発生時は、そのモデルインスタンス専用の
        httpx.AsyncClientのみを aclose_model_client() で強制クローズする
        （aclose_active_llm_clients()は同一セッションの他クライアント
        － 並行実行中の別サブエージェントやメイングラフ － まで巻き添えで
        閉じてしまうため、要約専用のこの経路では使わない。src/subagent.py
        の _invoke_with_loop_retry と同じ理由）。これにより、ストリームの
        後始末(aclose)自体が失敗・タイムアウトして接続が生きたまま
        llama-server側の生成が終わらない状態
        （ThinkingLoopDetected.client_broken=True）
        でも、次のリトライ・次のターンのLLM呼び出しが応答ヘッダー待ちで
        ハングし続けることを防ぐ。
    """
    cut_index = _find_compaction_cut_index(messages, config.context_compaction_keep_recent_iterations)
    if cut_index is None:
        return None

    old_messages = messages[:cut_index]
    kept_messages = messages[cut_index:]
    if not old_messages:
        return None

    # 要約対象自体が長大だと要約プロンプト自体のプリフィルが遅くなるため、
    # context_trim と同様の切り詰めを要約対象にも適用してから渡す
    # （keep_recent_iterations=0: 要約対象内では「直近だから全文保持」は意味を持たない）。
    # ただし max_chars は [context_trim] のものを流用せず、要約専用の
    # context_compaction_summary_source_max_chars を使う。要約は永続履歴を
    # 置き換える恒久的な操作のため、プリフィル短縮目的の[context_trim]と
    # 同じ小さめの値を使うと、要約対象のツール結果がまとめて情報欠落し、
    # 要約が内容の薄いものになりうる（例: 大量ファイル処理タスクで
    # ファイル名の列挙しか残らない）。
    # スキル本文は要約させず（要約LLMに手順を薄められないよう）、下記の
    # _render_reattached_skills で原文のまま要約の後ろへ再添付する。
    # 前回の圧縮で要約メッセージへ再添付したスキル本文も同様に要約させない。
    trimmed_old = _without_reattached_skills(
        trim_old_tool_messages(
            old_messages,
            keep_recent_iterations=0,
            max_chars=config.context_compaction_summary_source_max_chars,
            protect_skill_content=False,
        )
    )
    text = _messages_to_text(trimmed_old)
    if not text.strip():
        return None

    try:
        prompt = config.context_compaction_prompt_path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("Failed to load the summary prompt: %s", config.context_compaction_prompt_path)
        return None

    summary_prompt = prompt + "\n\n---\n\n# Conversation history to summarize\n\n" + text
    local_input: list[BaseMessage] = [HumanMessage(content=summary_prompt)]
    current_model = model
    connection_attempt = 0
    loop_attempt = 0
    response = None
    while True:
        try:
            response = await current_model.ainvoke(local_input)
            await _record_compaction_usage("summary", role, response)
            break
        except LLM_CONNECTION_ERRORS as exc:
            # config属性へのアクセスをexcept節内に留めているのは、テスト用の
            # 簡易Configスタブ（retry関連フィールドを持たない）が、通信エラー・
            # ループ検知いずれも起きない成功系のテストで壊れないようにするため。
            connection_max_retries = (
                config.graph_connection_error_max_retries
                if role == "main"
                else config.subagent_background_llm_timeout_max_retries
            )
            if connection_attempt >= connection_max_retries:
                # 要約自体の失敗で本編の会話を壊さないよう、失敗時は元の履歴のまま続行する。
                logger.exception("Automatic conversation summary failed with a communication error. Skipping compaction this time")
                return None
            connection_attempt += 1
            logger.warning(
                "Summarizer LLM call hit a communication error, switching endpoints and retrying (%d/%d, role=%s): %s",
                connection_attempt,
                connection_max_retries,
                role,
                exc,
            )
            # main_routing_strategy/sub_routing_strategy=priority_failover の
            # 場合のみ次点の接続先へ切り替わる（他戦略では実質無視される。
            # app.py の except LLM_CONNECTION_ERRORS と同じフック）。
            mark_last_endpoint_failed(role)
            current_model = await build_model(config, role=role)
        except ThinkingLoopDetected as exc:
            # このモデルインスタンス専用のクライアントだけを、リトライするか
            # 諦めるかに関わらず無条件で強制クローズする（client_broken の
            # 真偽にも関わらない。理由は _invoke_with_loop_retry と同じ:
            # httpcoreのTraceフックがクローズ失敗をログするだけで再raiseせず
            # client_broken が立たないケースがあるため）。ここを諦める分岐の
            # 前に置かないと、リトライ予算を使い切った最後の1回だけ後始末
            # されずに終わり、ストリームの後始末自体が失敗してllama-server側
            # の生成が終わらないまま（client_broken=True）次のLLM呼び出しが
            # 応答ヘッダー待ちでハングし続ける（ユーザー報告の疑いに対応）。
            await aclose_model_client(current_model)
            loop_max_retries = config.thinking_loop_guard_max_retries
            if loop_attempt >= loop_max_retries:
                logger.warning(
                    "Summarizer LLM response kept looping after %d retries, skipping compaction this time",
                    loop_max_retries,
                )
                return None
            loop_attempt += 1
            logger.warning(
                "Detected a loop in the summarizer LLM response, retrying (%d/%d, client_broken=%s): %r",
                loop_attempt,
                loop_max_retries,
                exc.client_broken,
                exc.snippet,
            )
            current_model = await build_model(config, role=role)
            local_input = [HumanMessage(content=summary_prompt), HumanMessage(content=_LOOP_NUDGE_TEXT)]
        except Exception:
            # 要約自体の失敗で本編の会話を壊さないよう、失敗時は元の履歴のまま続行する。
            logger.exception("Automatic conversation summary failed. Skipping compaction this time")
            return None

    summary_text = response.content if isinstance(response.content, str) else str(response.content)
    if not summary_text.strip():
        logger.warning("Automatic conversation summary was empty. Skipping compaction this time")
        return None

    summary_content = _SUMMARY_HEADER + summary_text
    if pinned_instruction:
        summary_content = _PINNED_INSTRUCTION_HEADER + pinned_instruction + "\n\n" + summary_content
    # 要約LLMの読み取り精度に依存せず、圧縮のたびに100%正確な最新の計画状態・
    # thread noteの状態を機械的に追記する（要約対象の tool_calls 引数は
    # _messages_to_text に含まれず要約LLMからは元々見えないため、要約結果に
    # 計画・thread noteの存在が反映される保証が無い）。
    # tools.py からの import はモジュール先頭ではなくここで遅延させる: tools.py は
    # 起動時に `from .subagent import run_subagent` を行っており、subagent.py が
    # このモジュール（context_compaction）を（context_trim と同様の位置づけで）
    # サブエージェントにも使い回すために import すると、
    # tools.py → subagent.py → context_compaction.py → tools.py という循環
    # importになり ImportError になる（subagent.py 冒頭のコメント参照）。
    # 実際に呼ばれるのはアプリ起動が完了しモジュール初期化が済んだ後のため、
    # 関数内 import なら安全。
    from .tools import current_plan_status_text, thread_note_status_text

    plan_status = current_plan_status_text()
    if plan_status:
        summary_content += "\n\n" + _PLAN_STATUS_HEADER + plan_status
    note_status = thread_note_status_text()
    if note_status:
        summary_content += "\n\n" + _THREAD_NOTE_STATUS_HEADER + note_status
    reattached_skills = _render_reattached_skills(
        old_messages,
        kept_messages,
        max_chars_per_skill=config.context_compaction_skill_reattach_max_chars_per_skill,
        total_max_chars=config.context_compaction_skill_reattach_total_max_chars,
    )
    if reattached_skills:
        summary_content += "\n\n" + _SKILL_REATTACH_HEADER + reattached_skills
    summary_message = HumanMessage(content=summary_content)
    # kept_messages は同一の aupdate_state 呼び出し内で RemoveMessage と
    # 競合しないよう、新しい id を振った複製にする（add_messages リデューサは
    # 既存stateに無いidのメッセージを渡された順に末尾へ追記する）。
    # AIMessage の複製には COMPACTION_KEPT_KEY を付け、圧縮前の usage_metadata で
    # トリムが継続しないようにする（context_trim.is_trigger_reached 参照）。
    kept_copies = [
        m.model_copy(
            update={
                "id": str(uuid.uuid4()),
                "response_metadata": {**m.response_metadata, COMPACTION_KEPT_KEY: True},
            }
        )
        if isinstance(m, AIMessage)
        else m.model_copy(update={"id": str(uuid.uuid4())})
        for m in kept_messages
    ]

    logger.warning(
        "Compacted the conversation history: %d messages -> 1 summary + last %d",
        len(old_messages),
        len(kept_messages),
    )
    return [summary_message, *kept_copies]
