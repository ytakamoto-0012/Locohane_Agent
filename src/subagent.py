"""サブエージェント（タスク委譲）用の独立した ReAct ループ。

tools.py の dispatch_agent ツールから呼ばれる。LangGraph の StateGraph は
使わず、graph.py の _build_handwritten_graph（call_model → should_continue
→ ToolNode → call_model … のループ）と同じ流れを async 関数の中で再現する。

意図的に tools.py / graph.py を import しない（呼び出し側から tools /
system_prompt / config を引数で受け取る汎用エンジンにすることで、
tools.py → subagent.py → tools.py という循環 import を避けている）。

model.ainvoke() / tool.ainvoke() はいずれも RunnableConfig を明示的に渡さない
ため、親グラフの astream_events には内部の呼び出しが一切伝播しない。
これにより、サブエージェント内部の思考過程・ツール呼び出しは Chainlit の
UI には表示されず、ログファイルにのみ記録される（コンテキスト節約が目的）。

async 化している理由: run_script が実行前にユーザー承認を求める async ツール
になったため（tools.py 参照）。dispatch_agent 自身も async ツールであり、
ImageAwareToolNode.ainvoke から同一イベントループ上で直接 await されるので、
ここで ainvoke に統一しても新規スレッド/イベントループのブリッジは不要。
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Callable
from dataclasses import replace

from chainlit.context import ChainlitContextException
from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool


from .config import Config
from .context_compaction import (
    force_write_thread_note,
    is_compaction_blocked_by_missing_note,
    maybe_append_precompact_note_nudge,
    maybe_compact,
    message_content_to_text,
    should_compact,
)
from .context_trim import is_trigger_reached, trim_old_ai_messages, trim_old_tool_messages
from .images import image_followup_message
from .tool_errors import looks_like_error
from .tool_args_guard import find_unknown_tool_args, unknown_tool_args_error
from .tool_loop_guard import detect_tool_call_loop, find_leaked_tool_markup, leaked_tool_markup_error
from .llm import (
    LLM_CONNECTION_ERRORS,
    ThinkingLoopDetected,
    ToolCallLoopDetected,
    aclose_model_client,
    build_model,
    describe_current_task,
    enable_thinking_control,
    mark_last_endpoint_failed,
    pick_loop_nudge_message,
    reset_llm_wait_listener,
    set_llm_wait_listener,
    tool_loop_nudge_text,
)

logger = logging.getLogger(__name__)

_EMPTY_RESPONSE_NUDGE_TEXT = (
    "[System notice: your previous response had neither a tool call nor text. "
    "Either summarize what you have found so far as your answer, or make the additional tool calls you need]"
)

# on_cancelled（会話履歴の緊急退避）が呼ばれる理由。スクラッチノートの見出しに
# そのまま出るため、後からログを読む人が原因を取り違えないよう区別する。
RESCUE_REASON_CANCELLED = "forced stop by the stop button or similar"
RESCUE_REASON_TOKEN_GUARD = "stopped because token usage reached the limit"
# run_subagent の on_activity へ渡す「今している動作」は (種別, *引数) のタプル。
# 表示文言は src/tools/_dispatch_agent_job.py の format_activity が表示言語に合わせて組み立てる。
Activity = tuple[str, ...]
ACTIVITY_THINKING: Activity = ("thinking",)
ACTIVITY_COMPACTING: Activity = ("compacting",)
# LLM の空き待ち中に渡す動作。
_LLM_WAIT_ACTIVITY: Activity = ("llm_wait",)

# 会話履歴圧縮・トークン閾値注意メッセージ注入の直後1手に限り、tool_calls無し
# の最終応答を無検査で受理しない（2026-08-23 issue: 圧縮直後にモデルが
# タスクと無関係な内容（例:「1+1=2」等の存在しない質問への回答）を幻覚し、
# dispatch_agentがそれを正常完了として扱ってそれまでの作業結果（VBAデータ
# 移行）を丸ごと握りつぶした事象への対処）。このしきい値未満の短い最終応答は
# 疑わしいとみなし1回だけ再試行する。
_POST_COMPACTION_SUSPICIOUS_RESPONSE_MIN_LENGTH = 80
_POST_COMPACTION_SUSPICIOUS_RESPONSE_NUDGE_TEXT = (
    "[System notice: your previous response was extremely short right after "
    "conversation history compaction / a token usage warning, and looks unrelated to the original task. "
    "Based on the work so far (the results of your tool calls), answer the original task again]"
)


async def _run_one_tool_call(call: dict, tools_by_name: dict[str, BaseTool]) -> tuple[ToolMessage, HumanMessage | None]:
    """1件の tool_call を実行し、(ToolMessage, followup) を返す。例外は送出しない。"""
    tool_obj = tools_by_name.get(call["name"])
    leaked_key = find_leaked_tool_markup(call.get("args"))
    unknown_args = find_unknown_tool_args(tool_obj, call.get("args"))
    if tool_obj is None:
        tool_message = ToolMessage(
            content=f"Error: unknown tool: {call['name']}",
            tool_call_id=call["id"],
        )
    elif leaked_key is not None:
        tool_message = ToolMessage(
            content=leaked_tool_markup_error(call["name"], leaked_key),
            tool_call_id=call["id"],
            # name が無いと、繰り返した場合のループ通知（ToolLoop.tool_names）が空になる
            name=call["name"],
            status="error",
        )
    elif unknown_args:
        tool_message = ToolMessage(
            content=unknown_tool_args_error(tool_obj, unknown_args, call.get("args")),
            tool_call_id=call["id"],
            name=call["name"],
            status="error",
        )
    else:
        try:
            # ToolCall dict（"name"/"args"/"id"/"type" を持つ）をそのまま
            # ainvoke に渡す。call["args"] のみを渡すと BaseTool 側で
            # tool_call_id が None のまま扱われ、response_format=
            # "content_and_artifact" のツール（view_image 等）の artifact
            # が握りつぶされてしまうため、ToolCall 形式を維持する。
            tool_message = await tool_obj.ainvoke(call)
        except Exception as e:  # noqa: BLE001 - ツール異常はエラー文字列化してループ継続
            logger.exception("subagent tool error: %s", call["name"])
            tool_message = ToolMessage(
                content=f"Error: tool execution failed: {e}\n{traceback.format_exc()}",
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )
    # execute_python_*系ツールは成功時もWARNING（スキル開発アイデアのシグナルとして記録）
    # それ以外でも戻り値がエラーを示す場合はWARNING（src/tool_errors.py）
    content = str(tool_message.content)
    is_execute_python = call["name"].startswith("execute_python_")
    has_error = looks_like_error(content)

    if is_execute_python or has_error:
        logger.warning(
            "subagent tool=%s args=%s -> %s",
            call["name"],
            call["args"],
            content[:500],
        )
    else:
        logger.info(
            "subagent tool=%s args=%s -> %s",
            call["name"],
            call["args"],
            content[:500],
        )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("subagent tool=%s args=%r -> %r", call["name"], call["args"], tool_message.content)
    followup = image_followup_message(getattr(tool_message, "artifact", None))
    return tool_message, followup


async def _invoke_with_loop_retry(model, messages: list, config: Config, tools: list[BaseTool]):
    """model.ainvoke() を呼び、反復ループを検知したら注意メッセージを注入して再試行する。

    ThinkingLoopDetected（src/llm.py の ChatLlamaCpp がストリーム中に検知して
    送出する）を捕捉し、config.thinking_loop_guard_nudge_messages から選んだ
    メッセージを一時的に追加した**ローカルコピー**で再試行する。サブエージェントの
    会話履歴（呼び出し元の messages）はローカルなPythonリストでLangGraphの
    チェックポイントを一切使わないため、成功しても失敗しても nudge が
    messages 本体に残ることはない（後始末が不要）。

    ループ検知のたびに、app.py の on_message と同様にモデルを再構築してから
    リトライする。壊れたクライアントのまま再試行すると、応答ヘッダーが
    返らず無期限にハングする恐れがある（本番incident・2026-07-20参照。
    src/llm.py の ThinkingLoopDetected docstring 参照）。

    再構築は exc.client_broken の真偽に関わらず無条件で行う。
    client_broken はストリームの後始末(aclose)が例外を送出した場合のみ
    真になるが、httpcoreのTraceフックがクローズ失敗をログするだけで
    再raiseしないケースがあり、その場合client_brokenが立たないまま壊れた
    クライアントを使い回して同種のハングが再発したため（2026-07-21）。

    ただし aclose_active_llm_clients()（on_stop専用）は呼ばない。
    src/llm.py の _active_async_clients は現在セッションID別の辞書に
    なっており、thread_id を指定すれば他タブを巻き添えにせず自セッション
    分だけを強制クローズできる。しかしここ（同一セッション内のリトライ
    経路）ではそれでも呼ばない: dispatch_agentは並列サブエージェント実行
    が既知の機能として存在し、同じセッション内で並行実行中の別サブ
    エージェントやメイングラフが使用中のクライアントまで巻き添えで閉じて
    しまい、"Cannot send a request, as the client has been closed" という
    別の実害を招くため（2026-07-21、app.py側の同種修正で実際に発生し
    確認済み）。代わりに aclose_model_client(current_model) で「この
    モデルインスタンス1つの接続だけ」を exc.client_broken の真偽に
    関わらず無条件で強制クローズする（context_compaction.py の
    maybe_compact と同じパターン）。単に参照を手放して build_model() で
    新しいクライアントに差し替えるだけでは、旧クライアントのストリーム
    後始末(aclose)自体が失敗・タイムアウトして接続が生きたまま
    llama-server側の生成が終わらずに残り続けることがある（2026-08-26
    ユーザー報告: サブエージェント実行中にループガードが発火すると
    「接続は切れるがLLMの生成は止まらない」）。

    なお build_model() が生成するhttpx.AsyncClientは、呼び出し時点で
    src/llm.py の set_current_session() により設定されているセッションID
    へ自動的に紐づく（asyncio.create_task/gatherが子タスクへcontextvarを
    コピーする性質による。src/tools.py の _IN_SUBAGENT と同型）。そのため
    dispatch_agent → run_subagent 経由のこの再構築は、呼び出し元セッション
    を意識するコードを一切書かなくても、正しいセッションのクライアント
    集合へ登録される。

    Args:
        model: build_model() が構築した（bind_tools 済みの）モデル。
        messages: これまでの会話履歴（変更しない）。
        config: thinking_loop_guard_nudge_messages/_max_retries を含むアプリ設定。
        tools: ループ検知時にモデルを再構築する際 bind_tools に渡すツール一覧。

    Returns:
        (model.ainvoke() が返す AIMessage, 実際に使用したモデルインスタンス)の
        タプル。呼び出し元は次回イテレーション以降、このモデルを使い続けること
        （ループ検知によりモデルが再構築された場合、元の model はもう使えない）。

    Raises:
        ThinkingLoopDetected: max_retries 回再試行してもなお反復ループが
            解消しなかった場合。
    """
    max_retries = config.thinking_loop_guard_max_retries
    local_messages = messages
    current_model = model
    for attempt in range(max_retries + 1):
        try:
            response = await current_model.ainvoke(local_messages)
            return response, current_model
        except ThinkingLoopDetected as exc:
            # 諦める（raise）か再試行するかに関わらず、このモデルインスタンス
            # 専用のクライアントだけを無条件で強制クローズする。この分岐より
            # 後（if attempt >= max_retries の raise の後）に置くと、リトライ
            # 予算を使い切った最後の1回だけ後始末されずに終わり、ストリームの
            # 後始末自体が失敗してllama-server側の生成が終わらないまま
            # 残り続ける（docstring参照）。
            await aclose_model_client(current_model)
            if attempt >= max_retries:
                raise
            logger.warning(
                "subagent: detected an LLM response loop (retry %d): recent text=%r [%s]",
                attempt + 1,
                exc.snippet,
                describe_current_task(),
            )
            # app.py の on_message は astream_events を消費して「思考中」Stepを
            # 表示するが、このリトライはここで完結し例外を呼び出し元へ伝播
            # しないため、on_message 側の except ThinkingLoopDetected（自グラフ
            # 自身のループ検知用）は発火しない。対策しないと、打ち切られた
            # 直前の試行分の思考Stepが「停止」バッジ無しで残ったまま、リトライ
            # 後の新しいトークンが同じStepへ継ぎ足されてしまう（2026-08-21
            # ユーザー報告）。astream_events はcontextvar経由でツール内部の
            # 呼び出しも同じストリームへ伝播させる（_resolve_parent_id参照）
            # ため、この adispatch_custom_event もそのストリームに乗り、
            # on_message 側で該当Stepを明示的にクローズ&リセットできる。
            # 呼び出し元にRunnableConfigの親run_idが無い経路（単体テスト等）
            # では失敗しうるが、UI通知はあくまで付加的なものでありリトライ
            # 自体を止めてはならないため、失敗しても握りつぶす。
            try:
                await adispatch_custom_event("subagent_loop_retry", {"snippet": exc.snippet})
            except Exception:  # noqa: BLE001 - UI通知の失敗でリトライ自体を止めない
                logger.debug("Failed to emit the subagent_loop_retry event", exc_info=True)
            current_model = enable_thinking_control(await build_model(config, role="sub"), config, "sub").bind_tools(tools)
            logger.warning(
                "subagent: rebuilt the LLM model before retrying (client_broken=%s) [%s]",
                exc.client_broken,
                describe_current_task(),
            )
            text = pick_loop_nudge_message(config.thinking_loop_guard_nudge_messages, attempt)
            local_messages = [*messages, HumanMessage(content=text)]
        except asyncio.CancelledError as exc:
            # サブエージェントがキャンセルされた場合、握りつぶさず
            # 診断ログを出してから再送出する（従来は捕捉していなかった）。
            logger.warning(
                "subagent: detected asyncio.CancelledError [%s, cause=%r]",
                describe_current_task(),
                repr(exc.__cause__),
            )
            raise
    raise AssertionError("unreachable")  # pragma: no cover


async def _invoke_with_empty_response_retry(model, messages: list, config: Config, tools: list[BaseTool]):
    """_invoke_with_loop_retry を呼び、tool_calls も本文も空の応答を検知したら再試行する。

    llama.cpp サーバーがまれに tool_calls・content ともに空の AIMessage を
    返すことがある（本番incident・2026-07-23、`explore` サブエージェントが
    8回目の反復で空応答を返し、run_subagent がそれを「正常終了」として
    空文字列をそのまま委譲元へ返し、それまでの調査結果が丸ごと失われた）。
    run_subagent は「tool_calls が空 = 最終回答」とみなす設計のため、この
    チェックを挟まないと同じ事象を無検査で通してしまう。

    リトライ時に注入する nudge メッセージはローカルコピーにのみ追加し、
    呼び出し元の messages 本体は変更しない（_invoke_with_loop_retry と
    同じ方針。失敗した試行を永続履歴に残さない）。

    Args:
        model: build_model() が構築した（bind_tools 済みの）モデル。
        messages: これまでの会話履歴（変更しない）。
        config: subagent_empty_response_max_retries を含むアプリ設定。
        tools: _invoke_with_loop_retry がループ検知時にモデルを再構築する
            際、bind_tools に渡す。

    Returns:
        (最終的な AIMessage, 使用したモデルインスタンス, 空応答のまま
        リトライ上限に達したかどうかの bool) のタプル。3番目が True の
        場合、返される AIMessage は空応答のままなので、呼び出し元は
        これを正常終了として扱ってはならない。
    """
    max_retries = config.subagent_empty_response_max_retries
    local_messages = messages
    current_model = model
    for attempt in range(max_retries + 1):
        response, current_model = await _invoke_with_loop_retry(current_model, local_messages, config, tools)
        tool_calls = getattr(response, "tool_calls", None)
        if tool_calls or str(response.content).strip():
            return response, current_model, False
        if attempt >= max_retries:
            return response, current_model, True
        logger.warning(
            "subagent: detected an empty response (no tool_calls, no text), retrying (%d/%d)",
            attempt + 1,
            max_retries,
        )
        local_messages = [
            *local_messages,
            response,
            HumanMessage(content=_EMPTY_RESPONSE_NUDGE_TEXT),
        ]
    raise AssertionError("unreachable")  # pragma: no cover


async def _invoke_with_timeout_retry(
    model, messages: list, config: Config, tools: list[BaseTool], max_retries: int
):
    """_invoke_with_empty_response_retry を呼び、LLM呼び出しタイムアウトを検知したら再試行する。

    run_subagent は元々、1回でも TimeoutError/LLM_CONNECTION_ERRORS が起きると
    その反復までの内容を要約して即座に打ち切っていた。しかし dispatch_agent の
    ように長時間動くジョブがバックグラウンドタスクとして裏側で動き続ける実行では、
    数百件中のたった1回の一時的なタイムアウト（llama-serverの瞬間的な混雑等）
    で残り全件分の進捗を諦めてしまうのは本末転倒。max_retries>0 の場合のみ、
    _invoke_with_loop_retry と同じ「モデルを再構築してから同じ内容で再試行する」
    パターンで温存する。

    max_retries=0（既定）では従来どおり初回のタイムアウトで例外がそのまま
    呼び出し元（run_subagent）へ伝播する。この既定値は明示的に retries を
    指定しない呼び出し元向けの安全側デフォルトであり、dispatch_agent は
    常に非ゼロの値を明示的に渡す（下記 llm_timeout_max_retries 参照）。

    Args:
        model: build_model() が構築した（bind_tools 済みの）モデル。
        messages: これまでの会話履歴（変更しない）。
        config: LLM 接続情報を含むアプリ設定。
        tools: モデル再構築時に bind_tools へ渡す。
        max_retries: タイムアウト時に再試行する最大回数。

    Returns:
        _invoke_with_empty_response_retry と同じ
        (AIMessage, 使用したモデルインスタンス, 空応答のままリトライ上限に
        達したかどうかの bool) のタプル。

    Raises:
        TimeoutError, LLM_CONNECTION_ERRORS: max_retries 回再試行してもなお
            タイムアウト/接続エラーが解消しなかった場合。openai SDK が
            httpx のread timeoutを openai.APITimeoutError（openai.APIConnectionError
            のサブクラス）へラップして再送出するケースがあり、単純な
            (TimeoutError, httpx.TimeoutException) では取りこぼすため
            LLM_CONNECTION_ERRORS（src/llm.py）を合わせて捕捉する
            （本番incident・2026-08-21: このラップにより本リトライが
            一度も発動せず、dispatch_agent が生のトレースバックで
            即失敗した）。
    """
    current_model = model
    for attempt in range(max_retries + 1):
        try:
            return await _invoke_with_empty_response_retry(current_model, messages, config, tools)
        except (TimeoutError, *LLM_CONNECTION_ERRORS) as exc:
            if attempt >= max_retries:
                raise
            logger.warning(
                "dispatch_agent: LLM call timed out, retrying (%d/%d): %s",
                attempt + 1,
                max_retries,
                exc,
            )
            # [subagent].sub_routing_strategy=priority_failover の場合、直近
            # 使用した接続先を一時的にクールダウンし、次の build_model() で
            # 次点の接続先へ切り替わるようにする（app.py の
            # except LLM_CONNECTION_ERRORS と同じフック。他戦略では実質
            # 無視される。src/llm.py の mark_last_endpoint_failed 参照）。
            mark_last_endpoint_failed("sub")
            current_model = enable_thinking_control(await build_model(config, role="sub"), config, "sub").bind_tools(tools)
    raise AssertionError("unreachable")  # pragma: no cover


def _extract_total_tokens(response: AIMessage) -> int | None:
    """AIMessage.usage_metadata から total_tokens を取り出す。

    config.track_token_usage=False の場合や、サーバーが usage を返さない
    場合は usage_metadata が None になりうる（src/llm.py build_model 参照）。

    Args:
        response: model.ainvoke() が返した AIMessage。

    Returns:
        total_tokens の値。usage_metadata が無い/キーが無ければ None。
    """
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return None
    return usage.get("total_tokens")


_TRUNCATION_PREFIX = "[Sub-agent: "


def is_truncated_result(content: object) -> bool:
    """dispatch_agent の戻り値が run_subagent の打ち切りによるものかを判定する。

    _build_truncation_message() が生成した文字列（max_iterations到達・
    空応答連続・トークン閾値超過・LLMタイムアウトのいずれか）かどうかを
    先頭プレフィックスで判定する。app.py の on_tool_end から、打ち切りを
    UI（stopped_reasonバッジ）とユーザー通知に反映するために使う。
    """
    return isinstance(content, str) and content.startswith(_TRUNCATION_PREFIX)


# LLM呼び出しのタイムアウト・通信エラーが再試行後も続いた場合の打ち切り理由。
_LLM_UNREACHABLE_REASON = "the LLM call timed out"


def is_llm_unreachable_result(content: object) -> bool:
    """打ち切りの理由が、再試行後も続いたLLMのタイムアウト・通信エラーかを判定する。

    dispatch_agent_batch が、LLMサーバーに届かない状態で残りのグループを
    起動し続けないよう判定するために使う。
    """
    return isinstance(content, str) and content.startswith(f"{_TRUNCATION_PREFIX}{_LLM_UNREACHABLE_REASON}")


def _build_truncation_message(reason: str, messages: list) -> str:
    """打ち切り時の共通メッセージを組み立てる（反復上限・トークン閾値超過で共用）。

    Args:
        reason: 打ち切り理由の文言（例: "最大反復回数(50)に達した"、
            "トークン使用量が上限(55000トークン)に達した"）。文末に
            「ため打ち切りました」が続く前提で渡すこと。
        messages: これまでの会話履歴。打ち切り直前の最後のメッセージ
            （AIMessageなら content）と、_collect_tool_results_summary()
            によるツール結果要約の抽出に使う。

    Returns:
        打ち切り理由に続けて、直前のAIMessage.content（あれば）と、
        収集済みツール結果要約（あれば）を連結した文字列。
    """
    last = messages[-1]
    last_text = last.content if isinstance(last, AIMessage) else ""
    parts = [f"{_TRUNCATION_PREFIX}stopped because {reason}]"]
    if last_text:
        parts.append(last_text)
    collected = _collect_tool_results_summary(messages)
    if collected:
        parts.append(
            "[Tool results collected so far (summarizing/organizing is unfinished because of the stop). "
            "The delegator should decide based on this whether further investigation is needed]\n" + collected
        )
    return "\n\n".join(parts)


def _build_llm_input(messages: list, config: Config) -> list:
    """会話履歴からLLM入力を組み立てる。[context_trim.subagent]（未設定の
    項目は [context_trim] を継承）が有効かつ trigger_total_tokens の閾値に
    達していれば、graph.py の pre_model_hook / call_model と同じロジックで
    古い ToolMessage/AIMessage を間引く（Claude Codeがメイン会話・
    サブエージェントを区別せず同一のコンテキスト管理を適用するのに倣い、
    同じ関数をサブエージェントのローカル履歴にも適用する。ただし設定値は
    サブエージェント専用の context_trim_subagent_* を使う）。

    呼び出し元の messages 本体は書き換えない（トリム結果はこの関数呼び出し
    1回分のLLM入力としてのみ使う）。
    """
    if not config.context_trim_subagent_enabled or not is_trigger_reached(
        messages, config.context_trim_subagent_trigger_total_tokens
    ):
        return messages
    trimmed = trim_old_tool_messages(
        messages,
        keep_recent_iterations=config.context_trim_subagent_keep_recent_tool_iterations,
        max_chars=config.context_trim_subagent_truncated_max_chars,
        guarded_tool_max_chars=config.context_trim_subagent_duplicate_guard_tool_max_chars,
    )
    if config.context_trim_subagent_ai_messages:
        trimmed = trim_old_ai_messages(
            trimmed,
            keep_recent_iterations=config.context_trim_subagent_keep_recent_ai_iterations,
            max_chars=config.context_trim_subagent_truncated_max_chars,
        )
    return trimmed


def _subagent_compaction_config(config: Config) -> Config:
    """should_compact() / maybe_compact()（src.context_compaction）は Config を
    丸ごと受け取り context_compaction_* 属性を参照する設計のため、
    context_compaction_* をサブエージェント専用の context_compaction_subagent_*
    の値に差し替えたビューを作って渡す（他のフィールドは元の config のまま）。
    """
    return replace(
        config,
        context_compaction_enabled=config.context_compaction_subagent_enabled,
        context_compaction_token_threshold=config.context_compaction_subagent_token_threshold,
        context_compaction_single_request_token_threshold=(
            config.context_compaction_subagent_single_request_token_threshold
        ),
        context_compaction_keep_recent_iterations=config.context_compaction_subagent_keep_recent_iterations,
        context_compaction_min_messages_to_compact=(
            config.context_compaction_subagent_min_messages_to_compact
        ),
        context_compaction_prompt_path=config.context_compaction_subagent_prompt_path,
        context_compaction_summary_source_max_chars=(
            config.context_compaction_subagent_summary_source_max_chars
        ),
        context_compaction_pre_note_threshold=config.context_compaction_subagent_pre_note_threshold,
        context_compaction_pre_note_warning_text=config.context_compaction_subagent_pre_note_warning_text,
        context_compaction_require_note_max_skips=(
            config.context_compaction_subagent_require_note_max_skips
        ),
    )


async def run_subagent(
    task: str,
    tools: list[BaseTool],
    system_prompt: str,
    config: Config,
    max_iterations: int,
    on_iteration: Callable[[int, int], None] | None = None,
    llm_timeout_max_retries: int = 0,
    on_cancelled: Callable[[list, str], None] | None = None,
    on_activity: Callable[[Activity], None] | None = None,
) -> str:
    """独立した ReAct ループでタスクを処理し、最終回答のテキストのみを返す。

    tool_calls が空になった時点の AIMessage.content を返す。ただし content も
    空（tool_calls無し・本文も空のLLM異常応答）だった場合は
    _invoke_with_empty_response_retry により config.subagent_empty_response_max_retries
    回まで再試行し、それでも解消しなければ正常終了として空文字列を返さず、
    max_iterations 到達時と同じ要約フォーマットで打ち切る（本番incident・
    2026-07-23: 空応答を無検査で正常終了扱いし、それまでの調査結果が
    丸ごと失われた事象への対処）。max_iterations に達してもループが
    終わらない場合も、その時点までの最後の応答に打ち切りである旨を
    明記して返す。ツール実行時の例外はここで捕捉してエラー文字列化し
    ループを継続する（例外を外へ送出しない）。

    config.subagent_token_guard_enabled が True（かつ track_token_usage が
    True）の場合、応答の usage_metadata.total_tokens を監視し、
    subagent_token_guard_soft_threshold 到達時に一度だけ注意メッセージを
    注入する。subagent_token_guard_hard_threshold 到達はソフト警告発行の
    前提を問わず単独で判定し（急速なトークン爆発でソフト警告を飛び越えて
    一気にhardへ到達した場合でもその場で捕捉するため。以前はソフト警告
    発行済みであることが前提の判定だったため、この飛び越えケースで
    ガードが効かずサブエージェントが打ち切られない事象があった）、
    到達時は on_cancelled が渡されていればそれも呼び出して会話履歴を
    スクラッチノートへ退避してから、max_iterations 到達時と同じ要約
    フォーマットで打ち切る（LLM自身は自分のトークン使用量を認識できない
    ため、コード側で判定する）。
    上記ソフト警告が発動しない場合のみ、[context_compaction.subagent]の
    pre_note_threshold到達を判定し write_thread_note を促す（メインエージェント
    側のsrc/graph.pyと同じ排他方針。詳細はmaybe_append_precompact_note_nudge参照）。

    Args:
        task: サブエージェントに委譲するタスクの説明文。
        tools: サブエージェントが使えるツールのリスト（dispatch_agent
            自身は含めないこと。含めると再帰委譲が可能になってしまう）。
        system_prompt: サブエージェント用のシステムプロンプト全文。
        config: LLM 接続情報を含むアプリ設定。build_model 経由でモデル構築に使う。
        max_iterations: agent→tools の遷移を許す最大回数。
        on_iteration: 各反復で応答を得るたびに `(iteration, max_iterations)` を
            渡して呼ぶ同期コールバック（省略可）。dispatch_agent が
            進捗表示（現在の反復回数）をジョブオブジェクトへ書き込むために使う。
            戻り値は無視する。例外は送出しない前提（呼び出し元の責任）。
        llm_timeout_max_retries: LLM呼び出しが [llm].request_timeout_seconds/
            stream_chunk_timeout_seconds に達した場合、モデルを再構築してから
            同じ反復を再試行する最大回数。既定0＝明示的に指定しない呼び出し元
            向けの安全側デフォルト（初回のタイムアウトで即座に打ち切る）。
            dispatch_agent は人間がターン内でリアルタイムに待つ間もジョブ自体は
            バックグラウンドタスクとして動き続けるため、より大きい値を渡して
            耐性を上げる（_invoke_with_timeout_retry 参照）。
        on_cancelled: 停止ボタン等による asyncio.CancelledError 検知時、
            または subagent_token_guard_hard_threshold 到達時に呼ぶ同期
            コールバック（省略可）。その時点までの messages（会話履歴）と、
            呼ばれた理由（RESCUE_REASON_CANCELLED / RESCUE_REASON_TOKEN_GUARD）
            の2引数を渡す。dispatch_agent がこの内容をスクラッチノートへ
            緊急退避するために使う（_dispatch_agent_job.py 参照）。戻り値は
            無視する。CancelledError検知時は例外を送出しても伝播を妨げない。
            hard_threshold到達時はCancelledErrorを使わず通常のreturnで
            打ち切るため、このコールバック自体の例外はここで捕捉して
            握りつぶし、打ち切り処理を継続する。
        on_activity: 今している動作が変わるたびに、その表示名（"思考中"・
            "ツール: スクリプト実行、Grep"・"会話履歴の圧縮中"・"LLMの空き待ち"）を
            渡して呼ぶ同期コールバック（省略可）。ツールの表示名は UI のStepバッジと
            同じ対応表（src/tool_labels.py）から引く。"LLMの空き待ち" は
            [llm].max_concurrent_requests の枠や接続先の空きスロットを待っている間
            （src/llm/routing.py の notify_llm_wait）で、抜けたら元の表示に戻す。
            dispatch_agent が進捗表示へ「今何をしているか」を出すために使う。
            戻り値は無視する。例外は送出しない前提（呼び出し元の責任）。

    Returns:
        サブエージェントの最終回答テキスト。
    """
    # このセッションのユーザーのドラフトスキル名を read_skill の選択肢へ足す
    # （src/tools/session_tools.py。メインエージェントと同じ範囲だけが見える）。
    from .tools.session_tools import apply_session_tool_overrides  # noqa: PLC0415 (src.tools が本モジュールを import するため循環を避ける)

    tools = apply_session_tool_overrides(tools)
    tools_by_name = {t.name: t for t in tools}
    messages: list = [SystemMessage(content=system_prompt), HumanMessage(content=task)]

    logger.info("dispatch_agent start: task=%r max_iterations=%d", task, max_iterations)

    token_guard_enabled = config.subagent_token_guard_enabled and config.track_token_usage
    soft_warning_issued = False
    # [context_compaction.subagent]（未設定の項目は[context_compaction]を継承）を
    # 使ってサブエージェントのローカル履歴にも適用する
    # （Claude Code方式。src/context_compaction.py 参照）。
    # トークン使用量が取得できない場合（track_token_usage=false）は
    # should_compact() が常にFalseを返すため実質無効化される。
    compaction_enabled = config.context_compaction_subagent_enabled and config.track_token_usage
    compaction_config = _subagent_compaction_config(config)
    cumulative_tokens_sub = 0
    # is_compaction_blocked_by_missing_note が見送りを判定するたびに増える
    # カウンタ（require_note_max_skips到達で記録なしでも圧縮を強制する）。
    note_skip_count = 0
    # 会話圧縮・トークン閾値注意メッセージ注入の直後1手だけ、tool_calls無しの
    # 最終応答を無検査で受理しない（2026-08-23 issue対応）。前の反復の末尾で
    # 圧縮/nudgeが起きたら次の反復の頭でTrueになり、その反復の判定を終えたら
    # 次回に持ち越さないよう毎回リセットする。
    just_compacted_or_nudged = False
    hallucination_retry_used = False
    # 全く同じ応答（ツール呼び出し）を繰り返すループ（src/tool_loop_guard.py）を検知して
    # 注意メッセージを入れた回数。thinking_loop_guard_max_retries を超えたら打ち切る。
    tool_loop_retries = 0

    # on_activity へ最後に渡した表示と、LLMの空き待ち中か。空き待ちから抜けたら
    # 待つ前の表示（"思考中"・"会話履歴の圧縮中" 等）へ戻す。
    activity: Activity = ()
    llm_waiting = False

    def _set_activity(name: Activity) -> None:
        nonlocal activity, llm_waiting
        activity, llm_waiting = name, False
        if on_activity is not None:
            on_activity(name)

    def _on_llm_wait(waiting: bool) -> None:
        nonlocal llm_waiting
        if on_activity is None or waiting == llm_waiting:
            return
        llm_waiting = waiting
        on_activity(_LLM_WAIT_ACTIVITY if waiting else activity)

    # contextvar なので、このサブエージェントの中の LLM 呼び出し（再構築・圧縮の
    # 要約・ツール内部の呼び出しを含む）だけが対象になる。
    wait_listener_token = set_llm_wait_listener(_on_llm_wait if on_activity is not None else None)
    try:
        model = enable_thinking_control(await build_model(config, role="sub"), config, "sub").bind_tools(tools)
    except BaseException:
        reset_llm_wait_listener(wait_listener_token)
        raise
    try:
        for iteration in range(1, max_iterations + 1):
            check_final_answer_strictly = just_compacted_or_nudged
            just_compacted_or_nudged = False
            tool_loop = detect_tool_call_loop(messages, config)
            if tool_loop is not None:
                if tool_loop_retries >= config.thinking_loop_guard_max_retries:
                    logger.warning(
                        "dispatch_agent: stopping because the same tool call loop did not resolve (iter=%d): %s",
                        iteration,
                        tool_loop.detail(),
                    )
                    return _build_truncation_message(
                        f"the exact same tool call ({', '.join(tool_loop.tool_names)}) kept repeating "
                        f"and {tool_loop_retries} warnings did not resolve it",
                        messages,
                    )
                logger.warning(
                    "dispatch_agent: detected a loop of the same tool call (%d): %s",
                    tool_loop_retries + 1,
                    tool_loop.detail(),
                )
                # 繰り返した応答を残すと、注意しても同じ tool_call を書き写し続ける
                # （src/tool_loop_guard.py 冒頭参照）。
                del messages[tool_loop.start_index :]
                messages.append(
                    HumanMessage(
                        content=tool_loop_nudge_text(
                            config.tool_loop_guard_nudge_messages,
                            tool_loop_retries,
                            ToolCallLoopDetected(tool_loop.detail(removed=True)),
                        )
                    )
                )
                tool_loop_retries += 1
            llm_input = _build_llm_input(messages, config)
            _set_activity(ACTIVITY_THINKING)
            try:
                response, model, empty_retries_exhausted = await _invoke_with_timeout_retry(
                    model, llm_input, config, tools, llm_timeout_max_retries
                )
            except (TimeoutError, *LLM_CONNECTION_ERRORS) as exc:
                logger.warning(
                    "dispatch_agent: stopping because the LLM call timed out (iter=%d): %s",
                    iteration,
                    exc,
                )
                return _build_truncation_message(f"{_LLM_UNREACHABLE_REASON}({exc})", messages)
            except ThinkingLoopDetected as exc:
                # thinking_loop_guard_max_retries回再試行してもなお反復ループが
                # 解消しなかった場合（_invoke_with_loop_retry参照）。以前はここで
                # 捕捉せず素通りさせていたため、run_subagentの外まで例外が
                # 伝播してjob.status="error"となり、通信エラー時とは非対称に
                # 会話履歴が一切引き継がれずに失われていた（2026-09-09 実運用
                # で確認）。通信エラーと同じ「打ち切りメッセージとして正常
                # returnする」扱いに揃える。_invoke_with_loop_retry が既に
                # aclose_model_client でこのモデルインスタンスをクローズ済み
                # のため、ここでの追加クローズ処理は不要。
                logger.warning(
                    "dispatch_agent: stopping because the repeated LLM response loop did not resolve (iter=%d): %s",
                    iteration,
                    exc.snippet,
                )
                return _build_truncation_message(
                    f"a repetition loop in the LLM response was detected (recent text: {exc.snippet!r})", messages
                )
            messages.append(response)
            logger.info("subagent iter=%d ai=%r", iteration, str(response.content)[:500])
            if on_iteration is not None:
                on_iteration(iteration, max_iterations)

            tool_calls = getattr(response, "tool_calls", None)
            if not tool_calls:
                if empty_retries_exhausted:
                    logger.warning(
                        "dispatch_agent: stopping after %d empty responses in a row (iter=%d)",
                        config.subagent_empty_response_max_retries + 1,
                        iteration,
                    )
                    return _build_truncation_message(
                        f"the LLM returned an empty response {config.subagent_empty_response_max_retries + 1} " "times in a row",
                        messages,
                    )
                content_text = str(response.content).strip()
                if (
                    check_final_answer_strictly
                    and not hallucination_retry_used
                    and len(content_text) < _POST_COMPACTION_SUSPICIOUS_RESPONSE_MIN_LENGTH
                ):
                    # 会話圧縮/トークン閾値注意の直後1手にもかかわらず極端に短い
                    # 最終応答は、タスクと無関係な内容を幻覚している疑いがある
                    # （実例: 圧縮直後に存在しない「1+1=?」への回答を返し、
                    # dispatch_agentがそれを正常完了として扱ってそれまでの作業結果
                    # を握りつぶした。issue/20260823_021924参照）。無限リトライは
                    # せず1回だけやり直しを促す。
                    hallucination_retry_used = True
                    logger.warning(
                        "dispatch_agent: detected an extremely short final response (%d chars) right after compaction/token threshold warning, retrying (iter=%d)",
                        len(content_text),
                        iteration,
                    )
                    messages.append(HumanMessage(content=_POST_COMPACTION_SUSPICIOUS_RESPONSE_NUDGE_TEXT))
                    continue
                logger.info("dispatch_agent finished normally: completed in %d iterations", iteration)
                return response.content

            total_tokens = _extract_total_tokens(response) if config.track_token_usage else None
            if total_tokens is not None:
                cumulative_tokens_sub += total_tokens

            if (
                token_guard_enabled
                and total_tokens is not None
                and total_tokens >= config.subagent_token_guard_hard_threshold
            ):
                logger.warning(
                    "dispatch_agent: stopping because token usage reached the threshold (%d) (iter=%d, total_tokens=%d)",
                    config.subagent_token_guard_hard_threshold,
                    iteration,
                    total_tokens,
                )
                if on_cancelled is not None:
                    try:
                        on_cancelled(messages, RESCUE_REASON_TOKEN_GUARD)
                    except Exception:  # noqa: BLE001 - 退避処理の失敗で打ち切り自体を妨げない
                        logger.exception("subagent: failed to run the rescue callback on reaching token_guard hard_threshold")
                return _build_truncation_message(
                    "token usage reached the limit " f"({config.subagent_token_guard_hard_threshold} tokens)",
                    messages,
                )

            # 並列の同名呼び出しは1つにまとめる（dict.fromkeys で順序を保つ）。
            _set_activity(("tools", *dict.fromkeys(call["name"] for call in tool_calls)))
            results = await asyncio.gather(*(_run_one_tool_call(call, tools_by_name) for call in tool_calls))
            for tool_message, followup in results:
                messages.append(tool_message)
                if followup is not None:
                    messages.append(followup)

            if compaction_enabled and should_compact(
                {"total": cumulative_tokens_sub},
                {"total_tokens": total_tokens},
                len(messages),
                compaction_config,
            ):
                blocked = is_compaction_blocked_by_missing_note(messages, compaction_config, note_skip_count)
                # 圧縮用モデルはツール未bindの素のインスタンスを使う（本編の model は
                # bind_tools 済みで、要約専用の呼び出しにツール定義を含める必要が
                # 無いため。src/context_compaction.py の maybe_compact docstring参照）。
                # write_thread_note の強制実行にも同じインスタンスを使い回す
                # （force_write_thread_note が内部で write_thread_note のみへ
                # bind_tools し直す。bind_tools は新しい Runnable を返すだけで
                # 元のインスタンスは素のまま）。
                summary_model = None
                if blocked:
                    # 見送るだけだと、require_note_max_skips 回まで見送った末に
                    # 記録なしで圧縮が強制される（事実退避の機会が一度も無いまま
                    # 要約されうる）。メインエージェント側（app.py の
                    # _run_context_compaction）と同じく、見送る前にその場で
                    # write_thread_note の実行を強制する。サブエージェントは
                    # 「委譲元へ返すのは要約、具体的な事実は thread note へ」と
                    # いう前提で動くため、ここでの退避はメイン側以上に重要。
                    _set_activity(("tools", "write_thread_note"))
                    summary_model = await build_model(config, role="sub")
                    # messages[0]（SystemMessage）は要約元テキストに含めない
                    # （maybe_compact へ messages[1:] を渡すのと同じ理由。下記参照）。
                    forced = await force_write_thread_note(messages[1:], summary_model, compaction_config)
                    if forced is None:
                        note_skip_count += 1
                        logger.warning(
                            "subagent: forced write_thread_note failed, postponing compaction (iter=%d, skip_count=%d)",
                            iteration,
                            note_skip_count,
                        )
                    else:
                        forced_ai, forced_tools = forced
                        # AIMessage と、その tool_calls 全件に対応する ToolMessage を
                        # 揃えて追記する（1件でも欠けるとOpenAI互換APIが以降の
                        # リクエストを拒否する）。
                        messages = [*messages, forced_ai, *forced_tools]
                        note_skip_count = 0
                        blocked = False
                        logger.warning(
                            "subagent: compacting after forcing write_thread_note (iter=%d)",
                            iteration,
                        )
                if not blocked:
                    # note_skip_count のリセットは実際に圧縮が完了した場合のみ行う
                    # （maybe_compactがNoneを返す＝要約LLM呼び出しの失敗・ループ検知
                    # 予算切れ等の場合はリセットしない。ここでリセットしてしまうと、
                    # 猶予を使い切って強制圧縮に踏み切った直後に要約が失敗したとき、
                    # 安全弁がまた最初から猶予を積み直すことになり骨抜きになる）。
                    # 見送る場合（blocked のまま）は圧縮しないため、ここで初めて表示する。
                    _set_activity(ACTIVITY_COMPACTING)
                    if summary_model is None:
                        summary_model = await build_model(config, role="sub")
                    # messages[0] は run_subagent 開始時に積んだ SystemMessage。graph.py の
                    # メインエージェントは system_prompt を state["messages"] に含めず
                    # call_model 側で毎回付け足す構造のため要約対象から自然に外れるが、
                    # サブエージェントの messages はローカルリストの先頭に SystemMessage を
                    # 保持する構造が異なる。除外せずに渡すと要約で先頭が切り捨てられた際に
                    # サブエージェントが以後システムプロンプト（役割・ツール方針等）を
                    # 失ってしまうため、常に保持対象として明示的に除外してから渡す。
                    # 一方 messages[1] の task（委譲時の指示）は要約対象に含まれる
                    # ため、pinned_instruction で原文を要約結果の先頭へ毎回付け直す
                    # （2回目以降の圧縮では前回の要約＝task付きが messages[1] に来るが、
                    # 付け直すのは常に元の task 原文なので重複・劣化しない）。
                    new_tail = await maybe_compact(
                        messages[1:], summary_model, compaction_config, role="sub", pinned_instruction=task
                    )
                    if new_tail is not None:
                        logger.info(
                            "subagent: compacted the conversation history (iter=%d) [%s]",
                            iteration,
                            describe_current_task(),
                        )
                        messages = [messages[0], *new_tail]
                        cumulative_tokens_sub = 0
                        note_skip_count = 0
                        just_compacted_or_nudged = True
                        # app.py の圧縮成功パスと同じく重複ガードの履歴もリセットする。
                        # 漏れていたため、圧縮で消えた画像の読み直しが analyze_image の
                        # 重複ガードに拒否され処理が欠落した（2026-10-01、006）。
                        # tools → subagent の循環 import を避けるため関数内で import する。
                        # chainlit 文脈外（テスト等）では cl.user_session が例外を送出する
                        # ため、リセットの失敗で圧縮自体を壊さないよう握りつぶす。
                        from .tools import reset_call_history_guards_after_compaction

                        try:
                            reset_call_history_guards_after_compaction()
                        except ChainlitContextException:
                            pass

            if (
                token_guard_enabled
                and not soft_warning_issued
                and total_tokens is not None
                and total_tokens >= config.subagent_token_guard_soft_threshold
            ):
                messages.append(HumanMessage(content=config.subagent_token_guard_soft_warning_text))
                soft_warning_issued = True
                just_compacted_or_nudged = True
                logger.warning(
                    "subagent: token usage is approaching the threshold (%d), injecting a warning message (iter=%d, total_tokens=%d)",
                    config.subagent_token_guard_soft_threshold,
                    iteration,
                    total_tokens,
                )
            else:
                # token_guardのソフト警告（会話をこれ以上進めず引継ぎを促す）と
                # 同時に条件を満たす場合はそちらを優先し、pre_noteは差し込まない
                # （src/graph.py の call_model/pre_model_hook と同じ排他方針。
                # 「これ以上調べるな」と「write_thread_noteを呼べ」が矛盾するため。
                # 元々この関数はsrc/context_compaction.pyに実装済みだったが、
                # subagent.pyから一度も呼ばれておらず
                # [context_compaction.subagent].pre_note_thresholdが死んだ設定に
                # なっていた実装漏れを修正）。
                before_pre_note = messages
                messages = maybe_append_precompact_note_nudge(messages, compaction_config)
                if messages is not before_pre_note:
                    just_compacted_or_nudged = True

        logger.warning("dispatch_agent: stopping because the maximum number of iterations (%d) was reached", max_iterations)
        return _build_truncation_message(f"the maximum number of iterations ({max_iterations}) was reached", messages)
    except asyncio.CancelledError:
        # 停止ボタン等でこのサブエージェントが強制終了された場合、ここまでの
        # messages（ローカル変数のためこの関数を抜けると失われる）を
        # on_cancelled 経由で呼び出し元へ退避させてから再送出する。
        # dispatch_agent → run_subagent という一方向の依存を保つため
        # （モジュールdocstring参照）、退避先の決定・書き込みは呼び出し元
        # （_dispatch_agent_job.py）の責務とし、ここではコールバックを
        # 呼ぶだけに留める。
        if on_cancelled is not None:
            try:
                on_cancelled(messages, RESCUE_REASON_CANCELLED)
            except Exception:  # noqa: BLE001 - 退避処理の失敗でCancelledErrorの伝播を妨げない
                logger.exception("subagent: failed to run the on_cancelled callback")
        raise
    finally:
        reset_llm_wait_listener(wait_listener_token)


def dump_messages_for_cancelled_rescue(messages: list, reason: str = RESCUE_REASON_CANCELLED) -> str:
    """打ち切り時、会話履歴を緊急退避用に整形する。

    run_subagent の on_cancelled コールバックから渡された messages
    （SystemMessage/HumanMessage/AIMessage/ToolMessageの列）を、要約せず
    ほぼそのままMarkdown化する。_collect_tool_results_summary は打ち切り時に
    委譲元へ引き継ぐための軽量要約だが、こちらは予期しない中断からの人間に
    よる調査・復旧が目的のため、tool_calls の引数を含め情報量を優先する。
    SystemMessage（システムプロンプト本体）は調査・復旧に不要な上に長大な
    ため除外する。

    Args:
        messages: 打ち切られた時点の会話履歴。
        reason: 退避の理由（見出しに出す）。停止ボタン等による
            CancelledError と token_guard の hard_threshold 到達では原因が
            全く異なるため、後からログを読む人が取り違えないよう区別する。
            RESCUE_REASON_* を使う。

    Returns:
        メッセージごとに "### <型名>" の見出しとcontentを並べたMarkdown文字列
        （tool_callsがあれば併記）。messagesが空なら空文字列。
    """
    if not messages:
        return ""
    parts = [f"[Emergency save of the sub-agent conversation history: {reason}]"]
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        role = type(m).__name__
        # 画像付きメッセージの base64 をそのまま退避しない（content_to_text 参照）。
        content = message_content_to_text(m)
        parts.append(f"### {role}\n{content}")
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            parts.append(f"tool_calls: {tool_calls}")
    return "\n\n".join(parts)


_TOOL_RESULT_SNIPPET_LIMIT = 1500
# snippets 全体（結合後）の上限文字数。大量ファイルの調査中に打ち切られた
# 場合、個々のスニペットを1500字に切り詰めても件数が多ければ合計は
# 際限なく増える（実例: 30件超のファイルを読んだ後に打ち切られ、合計
# 数万字が呼び出し元へ返り、呼び出し元自身のトークン上限を圧迫した）。
# 委譲による分離効果を保つため、直近の結果を優先して全体もここで切る。
_TOOL_RESULT_TOTAL_LIMIT = 6000


def _collect_tool_results_summary(messages: list) -> str:
    """反復上限打ち切り時に、それまでの ToolMessage 内容を軽量に要約する。

    実データ規模の探索（例: 100件超のファイル一覧取得）で反復上限に達すると、
    せっかく収集できていた生データが呼び出し元に一切引き継がれず丸ごと
    破棄される事象が確認された（tune-prompt調査、2026-07-18）。ここでは
    完全な集約ロジックは持たず、各 ToolMessage の内容を一定文字数で
    切り詰めて列挙するだけに留める（呼び出し元の会話がさらに肥大化し
    すぎないようにするため）。結合後の合計も _TOOL_RESULT_TOTAL_LIMIT で
    切り詰め、直近の結果（打ち切り直前の状況に近く、続きの判断に有用）を
    優先して残す。

    view_image の ToolMessage.content は「画像を読み込みました: <path>」
    という固定文言のみで、画像から実際に読み取った内容は、その直後に
    続く AIMessage（画像を見た直後にモデルが生成する説明文）にしか
    含まれない。ToolMessage だけを集めると、反復上限で打ち切られた際に
    この説明が丸ごと失われ、呼び出し元（メインエージェント）が
    「サブエージェントの結果は使えない」と判断して同じファイルを自力で
    再確認せざるを得なくなる副作用が大きいことが確認された
    （tune-prompt iter27、021ケース: evals/tuning_log.md参照）。そのため、
    連続する ToolMessage 群の直後に続く最初の AIMessage.content
    （モデル自身の解釈・説明）も1回だけ併記する。

    Args:
        messages: これまでの会話履歴（SystemMessage/HumanMessage/AIMessage/
            ToolMessage の列）。

    Returns:
        "- ツール=<name>: <内容（省略あり）>" の行に、直後の AIMessage の
        説明があれば "  → モデルの解釈: <内容（省略あり）>" を続けて
        改行で連結した文字列。ToolMessage が1件も無ければ空文字列。
    """
    snippets = []
    n = len(messages)
    i = 0
    while i < n:
        if not isinstance(messages[i], ToolMessage):
            i += 1
            continue
        # 並列 tool_calls 分の連続する ToolMessage をまとめて処理する。
        while i < n and isinstance(messages[i], ToolMessage):
            content = str(messages[i].content)
            if len(content) > _TOOL_RESULT_SNIPPET_LIMIT:
                content = content[:_TOOL_RESULT_SNIPPET_LIMIT] + "...[truncated]"
            name = getattr(messages[i], "name", None) or "?"
            snippets.append(f"- tool={name}: {content}")
            i += 1
        # このツール群の直後（view_image の画像 followup である
        # HumanMessage は飛ばす）に続く最初の AIMessage があれば、
        # モデル自身の解釈として1回だけ併記する。
        for j in range(i, n):
            nxt = messages[j]
            if isinstance(nxt, ToolMessage):
                break
            if isinstance(nxt, AIMessage):
                ai_content = str(nxt.content).strip()
                if ai_content:
                    if len(ai_content) > _TOOL_RESULT_SNIPPET_LIMIT:
                        ai_content = ai_content[:_TOOL_RESULT_SNIPPET_LIMIT] + "...[truncated]"
                    snippets.append(f"  -> model's interpretation: {ai_content}")
                break
    joined = "\n".join(snippets)
    if len(joined) > _TOOL_RESULT_TOTAL_LIMIT:
        joined = "(too many results, first part omitted. showing recent results only)\n...\n" + joined[-_TOOL_RESULT_TOTAL_LIMIT:]
    return joined
