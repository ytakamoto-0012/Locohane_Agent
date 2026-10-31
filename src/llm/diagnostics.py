"""LLM呼び出しの診断用ロギング（コールバック・cancel scope監視・タスク診断）。

chat_model.py の ChatLlamaCpp / build_model() にアタッチして使う。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)


# --- P0-3: cancel scope breakage watcher ---
# httpcore が "Attempted to exit cancel scope in a different task" を
# DEBUG でログするが、再raiseしないため app.py の client_broken フラグ
# に影響しない。このフィルタが検知したら src.llm 側で WARNING を出し、
# 直近の検知時刻/回数を公開関数で参照できるようにする。
class _CancelScopeBreakageWatcher(logging.Filter):
    """httpcore の cancel scope task 不一致ログを検知して WARNING へ格上げする。"""

    PATTERN = "Attempted to exit cancel scope in a different task"

    def __init__(self) -> None:
        super().__init__()
        self._hits: list[float] = []

    def filter(self, record: logging.LogRecord) -> bool:
        if self.PATTERN in record.getMessage():
            import time

            now = time.time()
            self._hits.append(now)
            # 直近60秒間のヒット数を保持
            self._hits[:] = [t for t in self._hits if now - t < 60]
            logger.warning(
                "Detected httpcore cancel scope breakage (%d times in the last 60 seconds). The stream was likely cleaned up in another task",
                len(self._hits),
            )
        return True


_cancel_scope_watcher = _CancelScopeBreakageWatcher()


def _register_cancel_scope_watcher() -> None:
    """httpcore ロガーに cancel scope breakage フィルタを登録する（冪等）。

    app.py の _setup() から呼ぶ。複数回呼んでも重複登録しない。
    """
    if _cancel_scope_watcher not in logging.getLogger("httpcore").filters:
        logging.getLogger("httpcore").addFilter(_cancel_scope_watcher)


def recent_cancel_scope_breakage(within_seconds: float = 60.0) -> int:
    """直近 within_seconds 秒以内に検知された cancel scope breakage の回数を返す。

    0 を返す場合は検知なし（ただしフィルタ自体が未登録の場合は常に0）。
    app.py の for ループ先頭（新しい astream_events 開始時）で呼び、
    リトライ経路で何が起きたかを診断する。
    """
    now = time.time()
    return sum(1 for t in _cancel_scope_watcher._hits if now - t < within_seconds)


class _DebugResponseLogger(BaseCallbackHandler):
    """LLM応答本文・thinking（reasoning_content）・tool_callsを DEBUG レベルで記録する。

    build_model() が構築するモデルインスタンスに常時アタッチする（コストは
    logger.debug() 自体の isEnabledFor チェックのみで、config.ini の
    [log].level が "debug" 以外のときはほぼゼロオーバーヘッド）。
    on_llm_end はチャットモデルでも呼ばれ、streaming=True の場合も最終的に
    集約されたメッセージ1件を持つ LLMResult が渡される。graph.py（メイングラフ、
    handwritten/prebuilt いずれの実装でも）・subagent.py（dispatch_agent内部の
    ReAct ループ）の両方が build_model() 経由でモデルを作るため、この1箇所で
    両方のLLM呼び出しを網羅できる。
    """

    def on_llm_end(self, response, **kwargs: Any) -> None:  # noqa: ANN001
        if not logger.isEnabledFor(logging.DEBUG):
            return
        for generation in response.generations:
            for gen in generation:
                message = getattr(gen, "message", None)
                content = getattr(message, "content", None) if message is not None else gen.text
                reasoning = None
                tool_calls = None
                if message is not None:
                    reasoning = (getattr(message, "additional_kwargs", None) or {}).get("reasoning_content")
                    tool_calls = getattr(message, "tool_calls", None)
                logger.debug(
                    "LLM response: content=%r reasoning_content=%r tool_calls=%r",
                    content,
                    reasoning,
                    tool_calls,
                )


class _RequestEndpointLogger(BaseCallbackHandler):
    """実際にHTTPリクエストが送信される瞬間（on_chat_model_start/on_llm_start）に、
    このモデルインスタンスが向いている接続先を記録する診断用コールバック。

    _select_endpoint() のログは「選択した」時点の記録に過ぎず、選択結果が
    実際に ChatLlamaCpp インスタンスへ正しく渡り、かつそのインスタンスが
    使い回される限りその接続先へ送られ続けているかは別途確認が必要
    （build_model() は role="main" の場合グラフ構築時に1回しか呼ばれず、
    以降のターンは同じモデルインスタンスを使い回す。src/graph.py 参照）。
    build_model()呼び出し1回につき固定の base_url/model を持つ単純な
    クロージャのため、on_llm_start/on_chat_model_start のたびに同じ値を
    ログへ出すだけだが、「このセッションのこの役割で、いつ・何回、実際に
    リクエストが飛んだか」を base_url と突き合わせて時系列で追えるように
    する（会話が続く間ずっと同じ接続先しか使われていないことの直接証拠、
    または想定外に接続先が変わっていないことの確認に使う）。
    """

    def __init__(self, role: str, session_id: str, base_url: str, model: str) -> None:
        self._role = role
        self._session_id = session_id
        self._base_url = base_url
        self._model = model

    def _log(self, run_id: Any) -> None:  # noqa: ANN401
        logger.info(
            "Endpoint used [request sent]: role=%s session_id=%r base_url=%s model=%s run_id=%s",
            self._role,
            self._session_id,
            self._base_url,
            self._model,
            run_id,
        )

    def on_llm_start(self, serialized: dict, prompts: list[str], *, run_id: Any = None, **kwargs: Any) -> None:  # noqa: ANN401
        self._log(run_id)

    def on_chat_model_start(self, serialized: dict, messages: list, *, run_id: Any = None, **kwargs: Any) -> None:  # noqa: ANN401
        self._log(run_id)


def describe_current_task(now: float | None = None) -> str:
    """現在の asyncio.Task の診断情報を1文字列にまとめる。

    task name, task id, cancelling() 回数, getattr で安全に取り出した
    _must_cancel 私有属性（3.11 の uncancel() がリセットしないため
    孤立キャンセルの直接証拠になりうる）, cancelled() 等を出力する。
    呼び出し元は末尾に [diag] task=... の形でログに付加する。

    イベントループが未起動の同期コンテキストでは "task=NONE thread=<id>"
    を返す。threading.get_ident() を付加することで、同期経路のログでも
    どのスレッド由来か事后に相関を取れるようにする。

    Args:
        now: 経過ms計算用の基準時刻（None なら time.time() を使う）。

    Returns:
        "name=<name> id=<id> cancelling=<n> must_cancel=<mc> cancelled=<c>"
        形式の文字列。task が None の場合は "task=NONE thread=<id>"。
    """
    if now is None:
        now = time.time()
    try:
        task = asyncio.current_task()
    except RuntimeError:
        # イベントループ未起動（同期コンテキスト）
        return f"task=NONE thread={threading.get_ident()}"
    if task is None:
        return f"task=NONE thread={threading.get_ident()}"
    parts = [
        f"name={task.get_name()!r}",
        f"id={id(task)}",
        f"cancelling={task.cancelling()}",
        f"cancelled={task.cancelled()}",
    ]
    # Python 3.11 の asyncio.Task には _must_cancel 私有属性があり、
    # uncancel() 後も残るため「未処理のキャンセル要求」の直接証拠になりうる。
    # getattr で防御的にアクセスする（存在しないバージョンでも安全）。
    mc = getattr(task, "_must_cancel", None)
    if mc is not None:
        parts.append(f"must_cancel={mc}")
    elapsed = now - getattr(task, "_started_at", now)
    parts.append(f"elapsed_ms={elapsed * 1000:.0f}")
    return " ".join(parts)


def _log_first_chunk_latency(elapsed_seconds: float, *, sync: bool, never_received: bool = False) -> None:
    """ストリーム開始から初回チャンク受信までの経過時間を DEBUG ログで記録する。

    常に DEBUG レベルのみで出力する（WARNING 閾値はここに持たせない）。
    正常に初回チャンクを受信した場合は "初回チャンクまで%dms" を出す。
    ストリーム終了までに一度もチャンクを受信しなかった場合は
    "初回チャンクを一度も受信せずストリーム終了" を出す。

    WARNING 閾値ベースの異常判定は、リトライ文脈を知っている app.py 側
    で行う（通常の初回リクエストの長い prefill との誤検知を避けるため）。

    Args:
        elapsed_seconds: ストリーム開始から現在までの経過秒数。
        sync: True なら同期経路(_stream)、False なら非同期経路(_astream)。
        never_received: True の場合、初回チャンク未受信であることを示すログを出力。
    """
    elapsed_ms = elapsed_seconds * 1000
    path = "sync" if sync else "async"
    if never_received:
        logger.debug(
            "Stream ended without receiving any chunk [%s, elapsed_ms=%.0f]",
            path,
            elapsed_ms,
        )
    else:
        logger.debug(
            "%.0fms until the first chunk [%s]",
            elapsed_ms,
            path,
        )
