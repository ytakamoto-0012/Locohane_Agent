"""ChatLlamaCpp（llama.cpp向けChatOpenAI拡張）と build_model()（組み立て入口）。

_LLM_REQUEST_SEMAPHORE は ChatLlamaCpp._stream/_astream/_agenerate が毎呼び出し
読み取る可変グローバルで、init_llm_concurrency() が再代入する。再代入を
他モジュールから見えるようにするため（`from module import name` は再代入を
追従しない）、この2つは同じファイルに置く。
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from typing import Any, Literal

import httpx
from langchain_openai import ChatOpenAI

from ..config import Config
from .dialect import _warn_once, build_extra_body, extract_reasoning, history_reasoning_keys, last_user_turn_index
from .diagnostics import (
    _DebugResponseLogger,
    _log_first_chunk_latency,
    _RequestEndpointLogger,
    describe_current_task,
)
from .loop_guard import ThinkingLoopDetected, _ThinkingLoopDetector, feed_chunk
from .routing import (
    _active_async_clients,
    _CURRENT_SESSION_ID,
    _select_endpoint,
    get_preferred_sub_model,
    notify_llm_wait,
    release_slot_reservation,
    reserve_slot,
)
from .thinking_control import (
    ThinkingControlSettings,
    apply_level,
    previous_step_stats,
    select_level,
    settings_from_config,
)
from .undeclared_tool_call import declared_tool_names, recover_undeclared_tool_calls

logger = logging.getLogger(__name__)

# --- LLM 同時実行数ガード ---
# llama-server への実 HTTP リクエスト総数をガードするセマフォ。
# [llm].max_concurrent_requests に応じて init_llm_concurrency() が
# 再設定する。None はガード無効（無制限）を表す。既定 Semaphore(1) は
# init_llm_concurrency() 未実行時（テスト等）の安全側フォールバック。
_LLM_REQUEST_SEMAPHORE: "asyncio.Semaphore | None" = asyncio.Semaphore(1)


def init_llm_concurrency(max_concurrent_requests: int) -> None:
    """llama-server への同時リクエスト数上限を初期化する。

    Args:
        max_concurrent_requests: 同時実行数上限。1 以上: Semaphore(N) で
            ガードする。0 以下: ガードを無効化（None に設定）する。
    """
    global _LLM_REQUEST_SEMAPHORE
    _LLM_REQUEST_SEMAPHORE = asyncio.Semaphore(max_concurrent_requests) if max_concurrent_requests > 0 else None


class ChatLlamaCpp(ChatOpenAI):
    """llama.cpp server / vLLM 向けの ChatOpenAI 拡張。

    推論サーバーは <think> ブロックを OpenAI 非標準のフィールドで返す
    （llama-server は reasoning_content、vLLM 0.20以降は reasoning）。
    ChatOpenAI はこの拡張フィールドを読み捨てる仕様（本家 docstring 参照）
    のため、ここでどちらのフィールドも additional_kwargs["reasoning_content"]
    に正規化して拾い上げ、UI 側（app.py）で思考過程として表示できるように
    する（dialect.extract_reasoning 参照）。

    あわせて、ストリーミング中の応答（thinking/本文）が反復ループに陥って
    いないかを _ThinkingLoopDetector で監視し、検知したら ThinkingLoopDetected
    を送出して生成を打ち切る（loop_guard_* フィールド、config.ini の
    [thinking_loop_guard] 由来、build_model() が注入する）。
    """

    loop_guard_enabled: bool = True
    loop_guard_window_chars: int = 600
    loop_guard_check_interval_chars: int = 150
    loop_guard_confirm_count: int = 2
    loop_guard_max_history_chars: int = 4000
    loop_guard_match_ratio_threshold: float = 0.2
    # False なら思考（reasoning_content）をループ検知の対象から外し、本文だけを
    # 監視する（[thinking_loop_guard].target = content_only、build_model() が注入する）。
    loop_guard_include_reasoning: bool = True
    # False なら本文（content）をループ検知の対象から外し、思考だけを監視する
    # （[thinking_loop_guard].target = thinking_only、build_model() が注入する）。
    loop_guard_include_content: bool = True
    # True なら履歴の AIMessage.additional_kwargs["reasoning_content"] を
    # リクエストの assistant メッセージへ載せ直す（[llm].reasoning_preserve
    # 由来、build_model() が注入する）。
    preserve_reasoning_content: bool = False
    # 送信先の LLMEndpoint.provider。載せ直す際のキー名の決定に使う
    # （dialect.history_reasoning_keys 参照、build_model() が注入する）。
    reasoning_dialect: str = "llama_cpp"
    # 設定されていれば、送信直前のメッセージ列から思考レベルを選んで extra_body を
    # 差し替える（[thinking_control]、enable_thinking_control() が注入する）。
    # None のモデル（要約・圧縮用など）は従来通り。
    thinking_control: ThinkingControlSettings | None = None
    # ログ用の役割名（"main" / "sub"、enable_thinking_control() が注入する）。
    thinking_control_role: str = ""
    # 接続先選択時に入れた空きスロットの予約（routing.reserve_slot、build_model() が
    # 注入する）。最初のリクエスト送信時に _release_slot_reservation が解除する。
    slot_reservation_token: int | None = None

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any) -> dict:
        """思考レベルの差し替えと、reasoning_preserve 有効時の履歴の thinking の載せ直しを行う。

        thinking_control が設定されていれば、メッセージ列から思考レベルを選び
        payload["extra_body"] を差し替える（src/llm/thinking_control.py 参照）。

        reasoning_preserve について: langchain_openai の _convert_message_to_dict
        は additional_kwargs の reasoning_content を読み捨てるため、サーバー側で
        preserve を有効にしても過去の thinking が届かず効果が出ない。親が作った
        payload["messages"] は入力メッセージと1対1で並ぶので、同じ位置の
        AIMessage から補う。キー名は送信先の方言に合わせる（llama-server は
        reasoning_content、vLLM は reasoning しか読まない）。

        載せ直すのは最後のユーザー発話（dialect.mark_user_turn の目印付き）
        より後の assistant メッセージだけ。今のツールループ内の thinking の
        連続性は保ちつつ、過去のユーザーターンの thinking を送り続けて
        コンテキストを消費しないようにする。目印付きが無い履歴（目印導入前の
        スレッド・サブエージェント）は全件載せ直す。
        """
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self.thinking_control is None and not self.preserve_reasoning_content:
            return payload
        messages = self._convert_input(input_).to_messages()
        if self.thinking_control is not None:
            self._apply_thinking_control(payload, messages)
        if not self.preserve_reasoning_content:
            return payload
        payload_messages = payload.get("messages")
        if not isinstance(payload_messages, list) or len(payload_messages) != len(messages):
            return payload
        start = last_user_turn_index(messages) or 0
        for message, message_dict in zip(messages[start:], payload_messages[start:]):
            if message_dict.get("role") != "assistant":
                continue
            reasoning = extract_reasoning(message.additional_kwargs)
            if reasoning:
                for key in history_reasoning_keys(self.reasoning_dialect):
                    message_dict[key] = reasoning
        return payload

    def _apply_thinking_control(self, payload: dict, messages: list) -> None:
        """思考レベルを選んで payload["extra_body"] を差し替え、判定をログに残す。"""
        settings = self.thinking_control
        level, reason, prev_level = select_level(messages, settings)
        payload["extra_body"] = apply_level(payload.get("extra_body"), level, settings, self.reasoning_dialect)
        stats = previous_step_stats(messages, settings)
        if stats is None:
            prev = "none"
        else:
            prev = (
                f"level={prev_level} reasoning_chars={stats['reasoning_chars']} "
                f"truncated={stats['truncated']} leaked={stats['leaked']}"
            )
        logger.info(
            "thinking_control: role=%s level=%s reason=%s prev(%s) [%s]",
            self.thinking_control_role,
            level,
            reason,
            prev,
            describe_current_task(),
        )

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> Any:
        generation_chunk = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if generation_chunk is None:
            return generation_chunk
        choices = chunk.get("choices") or []
        if choices:
            reasoning = extract_reasoning(choices[0].get("delta"))
            if reasoning:
                generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk

    def _create_chat_result(self, response: Any, generation_info: dict | None = None) -> Any:
        """非ストリーム応答の thinking も additional_kwargs["reasoning_content"] に拾い上げる。

        現状は streaming=True 固定のため通らないが、_agenerate 経由で
        stream=False が使われた場合に thinking が失われないようにする。
        """
        result = super()._create_chat_result(response, generation_info)
        response_dict = response if isinstance(response, dict) else response.model_dump(warnings=False)
        for generation, choice in zip(result.generations, response_dict.get("choices") or []):
            reasoning = extract_reasoning(choice.get("message"))
            if reasoning:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _make_loop_detector(self) -> _ThinkingLoopDetector:
        return _ThinkingLoopDetector(
            self.loop_guard_window_chars,
            self.loop_guard_check_interval_chars,
            self.loop_guard_confirm_count,
            self.loop_guard_max_history_chars,
            self.loop_guard_match_ratio_threshold,
        )

    def _stream(self, *args: Any, **kwargs: Any) -> Any:
        if _LLM_REQUEST_SEMAPHORE is not None:
            logger.warning("The synchronous stream path (_stream) was called. This path is not guarded by the semaphore.")
        if not self.loop_guard_enabled:
            yield from super()._stream(*args, **kwargs)
            return
        detector = self._make_loop_detector()
        stream_started_at = time.time()
        inner = super()._stream(*args, **kwargs)
        first_chunk_seen = False
        try:
            for chunk in inner:
                if feed_chunk(
                    detector,
                    chunk,
                    include_reasoning=self.loop_guard_include_reasoning,
                    include_content=self.loop_guard_include_content,
                ):
                    snippet = detector.snippet()
                    logger.warning(
                        "Detected an LLM response loop, stopping generation (recent text: %r)",
                        snippet,
                    )
                    raise ThinkingLoopDetected("Stopped because the LLM response fell into a repetition loop", snippet=snippet)
                if not first_chunk_seen:
                    first_chunk_seen = True
                    _log_first_chunk_latency(time.time() - stream_started_at, sync=True)
                yield chunk
        finally:
            try:
                inner.close()
            except Exception:  # noqa: BLE001 - 過剰ログを避ける
                logger.debug("Exception while cleaning up the stream (close)", exc_info=True)
            if not first_chunk_seen:
                _log_first_chunk_latency(time.time() - stream_started_at, sync=True, never_received=True)

    async def _astream(self, *args: Any, **kwargs: Any) -> Any:
        """llama-server への HTTP リクエストを _LLM_REQUEST_SEMAPHORE でガードする。

        本体処理（ループ検知・finally節）は _astream_guarded に分離し、
        この関数自体はセマフォ取得の薄いラッパーとして振る舞う。
        本文に書かれた未宣言ツールの呼び出しは tool_call に組み直す
        （src/llm/undeclared_tool_call.py 参照）。
        """
        declared = declared_tool_names(kwargs.get("tools"))
        sem = _LLM_REQUEST_SEMAPHORE
        if sem is None:
            self._release_slot_reservation()
            async for chunk in recover_undeclared_tool_calls(self._astream_guarded(*args, **kwargs), declared):
                yield chunk
            return
        if sem.locked():
            logger.debug("No free slot, waiting (llm concurrent guard)")
            notify_llm_wait(True)
        async with sem:
            notify_llm_wait(False)
            self._release_slot_reservation()
            async for chunk in recover_undeclared_tool_calls(self._astream_guarded(*args, **kwargs), declared):
                yield chunk

    def _release_slot_reservation(self) -> None:
        """接続先選択時の予約を、最初のリクエスト送信直前に解除する（routing.release_slot_reservation）。"""
        token, self.slot_reservation_token = self.slot_reservation_token, None
        release_slot_reservation(self.openai_api_base or "", token)

    async def _astream_guarded(self, *args: Any, **kwargs: Any) -> Any:
        """_astream の本体（ループ検知・finally節）。

        _astream からセマフォの内側で呼ばれる。
        """
        if not self.loop_guard_enabled:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
            return
        detector = self._make_loop_detector()
        stream_started_at = time.time()
        agen = super()._astream(*args, **kwargs)
        # finally節でaclose失敗時にclient_brokenを立てるため、raiseした
        # ThinkingLoopDetectedへの参照をローカル変数として保持しておく
        # （finally節からraise済みの例外オブジェクトへ直接アクセスする
        # 標準の手段が無いため、参照を生かしておく必要がある）。
        loop_exc: ThinkingLoopDetected | None = None
        diag_start = None
        first_chunk_seen = False
        try:
            async for chunk in agen:
                if feed_chunk(
                    detector,
                    chunk,
                    include_reasoning=self.loop_guard_include_reasoning,
                    include_content=self.loop_guard_include_content,
                ):
                    snippet = detector.snippet()
                    diag = describe_current_task(diag_start)
                    logger.warning(
                        "Detected an LLM response loop, stopping generation (recent text: %r) [%s]",
                        snippet,
                        diag,
                    )
                    loop_exc = ThinkingLoopDetected("Stopped because the LLM response fell into a repetition loop", snippet=snippet)
                    raise loop_exc
                if not first_chunk_seen:
                    first_chunk_seen = True
                    # ストリーム開始からこの最初のチャンク受信までの経過時間
                    # （TTFT）をこの場で記録する。finally節まで遅延させると
                    # 「ストリーム終了時刻 - 最初のチャンク受信時刻」という
                    # 別の値（実質的な生成時間）を記録してしまうバグになる。
                    _log_first_chunk_latency(time.time() - stream_started_at, sync=False)
                yield chunk
                # detector.feed()は同期的でCPUバウンドな処理（difflibでの
                # 文字列比較）を含む。Chainlitは単一プロセス・単一イベント
                # ループで全セッションを捌くため、ここで一度制御を返して
                # おかないと、他セッションのソケットイベント（承認ボタンの
                # クリック等）の処理が後回しにされ得る。
                await asyncio.sleep(0)
        finally:
            if not first_chunk_seen:
                _log_first_chunk_latency(time.time() - stream_started_at, sync=False, never_received=True)
            try:
                # 停止ボタン等でこの _astream 自体が既にキャンセル済みの
                # タスク内にいる場合、ここでの await も即座に再キャンセル
                # されうる（asyncio の一般的な挙動）。asyncio.shield() で
                # 別taskとして保護すると、cancel scope が前提とする
                # 「開いたのと同じtaskで閉じる」制約を破るため採用しない。
                # 従来 asyncio.wait_for() でタイムアウトのみ付与していたが、
                # wait_for は渡されたコルーチンを ensure_future で別task に
                # ラップして実行するため、shield と同型の task 不一致を
                # 引き起こしうる（agen.aclose() が巻き戻す先の anyio
                # CancelScope は「開いたのと同じtaskでしか閉じられない」
                # 制約を持つ）。asyncio.timeout() は現在のtaskに直接
                # タイムアウトキャンセルを注入し、新規taskを生成しないため、
                # 同一task制約を破らずにタイムアウトを維持できる。
                diag_start = time.time()
                async with asyncio.timeout(5.0):
                    await agen.aclose()
                diag_elapsed = (time.time() - diag_start) * 1000
                logger.debug(
                    "Stream cleanup (aclose) finished [%s, elapsed_ms=%.0f]",
                    describe_current_task(diag_start),
                    diag_elapsed,
                )
            except TimeoutError:
                diag_elapsed = (time.time() - diag_start) * 1000
                if loop_exc is not None:
                    loop_exc.client_broken = True
                    logger.warning(
                        "Stream cleanup (aclose) timed out (5 seconds). The httpx.AsyncClient is likely broken; the client must be recreated before the caller retries [%s, elapsed_ms=%.0f]",
                        describe_current_task(diag_start),
                        diag_elapsed,
                    )
                else:
                    logger.debug(
                        "Stream cleanup (aclose) timed out (5 seconds) [%s, elapsed_ms=%.0f]",
                        describe_current_task(diag_start),
                        diag_elapsed,
                    )
            except Exception:
                diag_elapsed = (time.time() - diag_start) * 1000
                if loop_exc is not None:
                    loop_exc.client_broken = True
                    logger.warning(
                        "Stream cleanup (aclose) failed. The httpx.AsyncClient may be broken, so the client must be recreated before the caller retries [%s, elapsed_ms=%.0f]",
                        describe_current_task(diag_start),
                        diag_elapsed,
                    )
                else:
                    logger.debug(
                        "Exception while cleaning up the stream [%s, elapsed_ms=%.0f]",
                        describe_current_task(diag_start),
                        diag_elapsed,
                    )

    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        """llama-server への HTTP リクエストを _LLM_REQUEST_SEMAPHORE でガードする。

        現状は stream=True 常に設定されているため到達しないが、将来
        stream=False が明示指定された場合の保険として追加する。
        """
        sem = _LLM_REQUEST_SEMAPHORE
        if sem is None:
            self._release_slot_reservation()
            return await super()._agenerate(*args, **kwargs)
        if sem.locked():
            logger.debug("No free slot, waiting (llm concurrent guard)")
            notify_llm_wait(True)
        async with sem:
            notify_llm_wait(False)
            self._release_slot_reservation()
            return await super()._agenerate(*args, **kwargs)

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        """同期生成パスはセマフォ二重管理を行わず、警告ログのみ出力する。

        項目2の修正後は呼び出し箇所がなくなるため、想定外に到達した場合に
        気づけるよう警告ログを出力して親の処理をそのまま返す。
        """
        if _LLM_REQUEST_SEMAPHORE is not None:
            logger.warning("The synchronous generate path (_generate) was called. This path is not guarded by the semaphore.")
        return super()._generate(*args, **kwargs)


async def build_model(
    config: Config,
    role: Literal["main", "sub"] = "main",
    *,
    wait_when_busy: bool = True,
) -> ChatOpenAI:
    """llama.cpp server の OpenAI 互換エンドポイントに繋ぐ ChatOpenAI を作る。

    Ollama 固有の API・ライブラリは使わず、langchain-openai の ChatOpenAI
    を llama.cpp の OpenAI 互換サーバーへ向けて構築する。

    top_p / max_tokens / frequency_penalty / presence_penalty は ChatOpenAI が
    ネイティブに持つフィールドとしてそのまま渡す。top_k / repeat_penalty /
    DRY サンプラー系（dry_*）は OpenAI 標準 API には無い llama.cpp 拡張
    パラメータのため、model_kwargs では openai SDK の Completions.create() が
    未知のキーワード引数として例外を起こしてしまう。extra_body（openai SDK の
    正式な予約引数）に載せることで、HTTP リクエストボディのトップレベルに
    マージされ llama-server に届く。拡張パラメータの名前・有無は推論サーバー
    ごとに異なるため、選ばれた接続先の provider に応じて
    dialect.build_extra_body() が組み立てる。

    Args:
        config: main_endpoints/main_routing_strategy（role="main"時）または
            sub_endpoints/sub_routing_strategy（role="sub"時）/ temperature /
            top_p / top_k / repeat_penalty / frequency_penalty / presence_penalty /
            max_tokens / dry_multiplier / dry_base / dry_allowed_length /
            dry_penalty_last_n / dry_sequence_breakers / enable_thinking /
            reasoning_format / reasoning_budget / reasoning_budget_message /
            reasoning_effort / reasoning_preserve / track_token_usage / request_timeout_seconds / thinking_loop_guard_*
            を含むアプリ設定。
            未指定（None）の項目はリクエストに含めず llama-server 側の
            デフォルトに委ねる。thinking_loop_guard_* は ChatLlamaCpp の
            loop_guard_* フィールドへ渡され、ストリーミング中の反復ループ検知
            （ThinkingLoopDetected）に使う。
        role: "main"（メインエージェント、既定）または "sub"（サブエージェント
            ／dispatch_agent）。どちらの接続先リスト・ルーティング戦略を
            使うかを切り替える（_select_endpoint() 参照）。ただし
            config.sub_endpoints_inherit_main が True（[llm].sub_url 未指定）
            の場合、role="sub" は sub_routing_strategy を使わず、同一
            セッションでメインエージェントが直近実際に使った接続先を
            そのまま継承する。role="sub" で set_preferred_sub_model() により
            モデル名が指定されている場合は、その model の接続先だけから選ぶ
            （一致する接続先が無ければ指定を無視。_select_endpoint() 参照）。
            指定が無い・使えない場合は config.sub_default_model
            （[llm].sub_default_model）を同じ扱いで使う。
        wait_when_busy: main_routing_strategy/sub_routing_strategy=round_robin/priority_failover
            かつ provider="llama_cpp" の接続先が全てビジーだった場合、空きが
            出るまで待つか（True、既定）。False の場合は待たずにフェイル
            セーフ選択する。build_model() はグラフ構築時にしか呼ばれず
            （実際の生成のたびには呼ばれない）、構築直後に生成が続くとは
            限らない呼び出し元（スレッド再開・新規セッション開始等、直後に
            メッセージ送信するとは限らない操作）でTrueのままだと、無関係な
            他セッションの生成中に長時間ブロックしてしまう
            （2026-08-26 ユーザー報告: 生成中に会話履歴を切り替えると
            ロードが極端に遅くなる。app.py の on_chat_start/on_chat_resume
            はFalseを渡す）。直後に生成が確定している呼び出し元
            （ThinkingLoopDetected・接続エラー時の再構築など）は既定のTrue
            のままにする。

    Returns:
        streaming=True で構築された ChatLlamaCpp インスタンス（未 bind_tools）。
        config.track_token_usage が True の場合、stream_usage=True も
        設定される（base_url を指定した ChatOpenAI は既定で usage 取得が
        無効化されるため、明示的に有効化する必要がある）。

    Notes:
        内部で生成する httpx.AsyncClient は、呼び出し時点の
        set_current_session() で設定されたセッションIDに紐づけて
        _active_async_clients へ登録される。これにより
        aclose_active_llm_clients(session_id) が自セッション分だけを
        強制クローズできる。
    """
    endpoints = config.main_endpoints if role == "main" else config.sub_endpoints
    routing_strategy = config.main_routing_strategy if role == "main" else config.sub_routing_strategy
    inherit_from_role = "main" if role == "sub" and config.sub_endpoints_inherit_main else None
    endpoint = await _select_endpoint(
        role,
        endpoints,
        routing_strategy,
        inherit_from_role=inherit_from_role,
        probe_timeout_seconds=config.round_robin_slots_probe_timeout_seconds,
        busy_poll_interval_seconds=config.round_robin_busy_poll_interval_seconds,
        wait_when_busy=wait_when_busy,
        preferred_model=get_preferred_sub_model() if role == "sub" else None,
        # 委譲ごとの指定（frontmatter model / dispatch_agent の model 引数）が
        # 無い・使えない場合は [llm].sub_default_model を使う。sub_url に軽量・
        # 高性能モデルを混在させたとき、未指定（または指定名の誤り）の委譲が
        # 無作為にどちらかへ振られるのを防ぐ。
        fallback_model=config.sub_default_model if role == "sub" else None,
    )
    # 選択（GET /slots の確認）から最初のリクエスト送信までの間に、同時に
    # 選択中の他の build_model() が同じ空きを選ばないよう予約する。選択直後で
    # await を挟まないこと（挟むと予約前に他の選択が割り込む）。直後に生成する
    # とは限らない wait_when_busy=False の呼び出し元では予約しない（使われない
    # 予約が失効まで他の選択を次点へ追いやるため）。
    slot_reservation_token = reserve_slot(endpoint) if wait_when_busy else None
    logger.info(
        "build_model() called: role=%s routing_strategy=%s session_id=%r -> base_url=%s model=%s [diag] %s",
        role,
        routing_strategy,
        _CURRENT_SESSION_ID.get(),
        endpoint.base_url,
        endpoint.model,
        describe_current_task(),
    )

    extra_body = build_extra_body(config, endpoint.provider)

    # keep-alive接続を無効化する: 思考ループ検知時にストリームを正しく
    # クローズできなかった場合（cancel scopeのtask不一致等）、壊れた接続が
    # コネクションプールに残り続け、次のリクエストがそれを再利用して
    # 応答ヘッダー待ちのまま無限にフリーズすることがある（実際に発生した
    # インシデントより）。ローカルのllama-server相手のためTCP再確立の
    # コストは無視できるレベルなので、毎回新規接続にして安全側に倒す。
    no_keepalive_limits = httpx.Limits(max_keepalive_connections=0)

    # 応答待ちタイムアウトを明示する: httpxのデフォルトは無制限のため、
    # 上記のkeep-alive無効化だけではカバーできないケース（新規接続を
    # 張った後でも、サーバー側に残った旧ストリームの影響で応答ヘッダーが
    # 返らない場合）に、クライアント側が無期限に待ち続けてしまう
    # （本番incident・2026-07-20: ThinkingLoopDetected後のaclose失敗で
    # クライアント内部状態が壊れ、次のリトライが応答ヘッダー待ちのまま
    # 7分11秒間ハングした事例を確認）。read/write/poolは
    # config.request_timeout_seconds（config.ini [llm].request_timeout_seconds）
    # を使う。read timeoutはチャンク間のアイドル時間の上限であり、生成が
    # 続く限り新しいチャンクのたびにリセットされるため、長い思考・長い
    # max_tokens生成そのものは妨げない。connectのみ短い固定値にし、
    # llama-server自体が未起動等の接続失敗は素早く検知する（ローカル
    # サーバー相手のため通常は一瞬で確立できるはず）。
    llm_timeout = httpx.Timeout(config.request_timeout_seconds, connect=10.0)
    async_client = httpx.AsyncClient(limits=no_keepalive_limits, timeout=llm_timeout)
    session_id = _CURRENT_SESSION_ID.get()
    _active_async_clients.setdefault(session_id, weakref.WeakSet()).add(async_client)

    # なぜ request_timeout を明示する必要があるか:
    # langchain_openai の ChatOpenAI は内部属性 self.timeout を持っており、
    # 既定値は None。openai SDK は毎リクエスト build_request(timeout=...) で
    # self.timeout を httpx リクエストに渡す。self.timeout が None だと、
    # httpx.AsyncClient に設定した httpx.Timeout が「無視される」のではなく、
    # openai SDK 側で「明示的な無制限指定」として扱われ、connect/read/write/pool
    # が全て無制限になる（実ログで connect_tcp.started ... timeout=None を
    # 確認済み）。build_model() は httpx.Timeout オブジェクトを llm_timeout として
    # 作っているが、ChatOpenAI.request_timeout を未設定だったため、この上書きを
    # 止めるには ChatLlamaCpp 構築時に request_timeout=llm_timeout を渡す必要が
    # ある（2026-07-20, 2026-07-21, 2026-07-28 の各 incident で確認された
    # 構造的な欠陥）。
    return ChatLlamaCpp(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,  # llama.cpp は認証不要のためダミー値
        model=endpoint.model,
        temperature=config.temperature,
        top_p=config.top_p,
        max_tokens=config.max_tokens,
        frequency_penalty=config.frequency_penalty,
        presence_penalty=config.presence_penalty,
        extra_body=extra_body or None,
        http_client=httpx.Client(limits=no_keepalive_limits, timeout=llm_timeout),
        http_async_client=async_client,
        request_timeout=llm_timeout,
        # openai SDKは既定でmax_retries=2の自動リトライを持つが、これは
        # request_timeout_seconds分のハングを我々のコードから見えないまま
        # 最大3倍まで増幅させてしまう（本番incident・2026-07-20:
        # ThinkingLoopDetected後のリトライがcancel scope破損済みクライアント
        # 上でハングした際、SDK内部リトライにより8分近く沈黙し、
        # thinking_loop_guard/client_broken側の回復ロジックが発動する前に
        # 停止ボタンでの強制終了が必要になった）。1回の試行を確実に
        # request_timeout_seconds以内で失敗させ、アプリ側の
        # リトライ・クライアント再構築ロジックに委ねるため無効化する。
        max_retries=0,
        streaming=True,
        stream_usage=config.track_token_usage,
        stream_chunk_timeout=config.stream_chunk_timeout_seconds,
        callbacks=[
            _DebugResponseLogger(),
            _RequestEndpointLogger(role, session_id or "", endpoint.base_url, endpoint.model),
        ],
        loop_guard_enabled=config.thinking_loop_guard_enabled,
        loop_guard_window_chars=config.thinking_loop_guard_window_chars,
        loop_guard_check_interval_chars=config.thinking_loop_guard_check_interval_chars,
        loop_guard_confirm_count=config.thinking_loop_guard_confirm_count,
        loop_guard_max_history_chars=config.thinking_loop_guard_max_history_chars,
        loop_guard_match_ratio_threshold=config.thinking_loop_guard_match_ratio_threshold,
        loop_guard_include_reasoning=config.thinking_loop_guard_target != "content_only",
        loop_guard_include_content=config.thinking_loop_guard_target != "thinking_only",
        preserve_reasoning_content=bool(config.reasoning_preserve),
        reasoning_dialect=endpoint.provider,
        slot_reservation_token=slot_reservation_token,
    )


def enable_thinking_control(model: Any, config: Config, role: Literal["main", "sub"]) -> Any:
    """エージェントのループ用モデルに、ステップごとの思考レベル切り替えを有効化する。

    build_model() 自体は要約・圧縮用の呼び出しとも共用のため、ここで対象を
    ループ（src/graph.py・src/subagent.py）だけに限定する。build_model() の
    戻り値をそのまま渡し、bind_tools() はこの後に行うこと。

    次をすべて満たすときだけ設定を注入する（満たさなければ何もしない）:
    - model が ChatLlamaCpp（テスト用の偽モデル等は対象外）
    - [thinking_control].enabled が true
    - role に対応する apply_to_main / apply_to_sub が true
    - [llm] 側で思考が無効になっていない（enable_thinking=false /
      reasoning_effort=none なら、レベルを上げても思考させられないため警告して無効）

    Args:
        model: build_model() が返したモデル。
        config: アプリ設定。
        role: "main" / "sub"。

    Returns:
        渡された model（設定を注入した場合も同じインスタンス）。
    """
    if not isinstance(model, ChatLlamaCpp) or not config.thinking_control_enabled:
        return model
    applies = config.thinking_control_apply_to_main if role == "main" else config.thinking_control_apply_to_sub
    if not applies:
        return model
    if config.enable_thinking is False or config.reasoning_effort == "none":
        _warn_once(
            ("*", "thinking_control"),
            "[thinking_control].enabled=true, but thinking is disabled on the [llm] side "
            "(enable_thinking=false / reasoning_effort=none), so the thinking level is not switched",
        )
        return model
    model.thinking_control = settings_from_config(config)
    model.thinking_control_role = role
    return model
