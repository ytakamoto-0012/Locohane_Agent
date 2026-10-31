"""LLMセッション管理・httpxクライアントのライフサイクル・接続先ルーティング。

[llm].main_url / sub_url が複数件のときの選択ロジック（round_robin / random /
priority_failover）と、そのために必要なセッションID管理・
httpx.AsyncClient のセッション別ブックキーピングをまとめる。
"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import logging
import random
import time
import weakref
from typing import Callable, NamedTuple
from urllib.parse import urlsplit

import httpx
from langchain_openai import ChatOpenAI

from ..config import LLMEndpoint

logger = logging.getLogger(__name__)


# build_model() が生成した httpx.AsyncClient を、生成時点のセッションID（app.py の
# thread_id）ごとに弱参照で分けて保持する。以前はプロセス全体で1つの WeakSet に
# 一括で集めていたが、それだと on_stop（1タブの停止操作）が他タブの実行中
# クライアントまで巻き添えで強制クローズしてしまう不具合があった
# （"Cannot send a request, as the client has been closed" が別タブで発生）。
# セッションごとに分けることで、aclose_active_llm_clients(session_id) が
# 自セッション分のクライアントだけを閉じられるようにする。
# 値の WeakSet は使い捨てクライアント（サブエージェント用等）がGCされれば
# 自然に空になるが、キー（session_id）自体は明示的に forget_session() で
# 消さない限り残る（app.py の @cl.on_chat_end から呼ぶ）。
_active_async_clients: "dict[str, weakref.WeakSet[httpx.AsyncClient]]" = {}

# build_model() が生成する httpx.AsyncClient を、どのセッションに紐づけて
# _active_async_clients へ登録するかを示す。Chainlit は @cl.on_chat_start /
# @cl.on_message / @cl.on_stop のたびに asyncio.create_task() で新しい
# タスクを起こし、各タスクは呼び出し時点の contextvars のコピーを持つ
# （他タブ＝他タスクへ値が漏れることはない）。dispatch_agent 経由の
# サブエージェント（src/subagent.py の run_subagent が asyncio.gather() で
# 並列実行する）にも、子タスク生成時に値がコピーされるため、src/tools.py の
# _IN_SUBAGENT と同様、サブエージェント側のコード変更なしに自動で正しい
# セッションIDへ伝播する。デフォルト None は Chainlit セッションを持たない
# 呼び出し元（evals/ の評価ハーネス等）向け。
_CURRENT_SESSION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("_current_session_id", default=None)

# _CURRENT_SESSION_ID（thread_id）とは別に、実際のブラウザタブ／接続
# （cl.context.session.id）単位の識別子を保持する。_LAST_SELECTED_INDEX の
# キーに使う（下記参照）。_CURRENT_SESSION_ID をそのまま使わない理由:
# 同じ thread_id を複数タブで同時に開く操作（同じ会話を別タブで開く／
# _stop_thread_generating で他タブから停止する等）はこのアプリが正式に
# サポートしており、_active_async_clients はまさにその複数タブ間で
# 意図的に共有する必要があるため thread_id をキーにしているが（上記
# docstring参照）、_LAST_SELECTED_INDEX（「このタブの直近の接続先選択」）
# まで thread_id 単位で共有してしまうと、同じスレッドを開いた別タブの
# build_model() 呼び出しが割り込んで上書きし、role="sub" の
# inherit_from_role 継承が本来とは別タブの接続先を拾ってしまう恐れがある
# （2026-08-26 監査で発見）。tab_id 未指定（evals/ の評価ハーネス等）の
# 場合は従来通り session_id をそのまま使う。
_CURRENT_TAB_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("_current_tab_id", default=None)

# サブエージェント（dispatch_agent）実行中に build_model(role="sub") が優先する
# モデル名（agents/*.md の frontmatter model、または dispatch_agent の model 引数）。
# src/tools/_dispatch_agent_job.py がジョブのランナータスク内で set/reset する
# ため、そのジョブ内の再構築・圧縮用の build_model() 呼び出しにも自動で伝播し、
# 他のジョブ・メインエージェントへは漏れない。None なら通常のルーティング。
_PREFERRED_SUB_MODEL: contextvars.ContextVar[str | None] = contextvars.ContextVar("_preferred_sub_model", default=None)


def set_preferred_sub_model(model: str | None) -> contextvars.Token:
    """これ以降このタスクの build_model(role="sub") が優先するモデル名を設定する。

    Args:
        model: [llm].sub_url の各接続先の model と一致させるモデル名。
            None/空文字なら指定なし（通常のルーティング）。

    Returns:
        reset_preferred_sub_model() に渡すトークン。
    """
    return _PREFERRED_SUB_MODEL.set((model or "").strip() or None)


def reset_preferred_sub_model(token: contextvars.Token) -> None:
    """set_preferred_sub_model() の設定を元に戻す。"""
    _PREFERRED_SUB_MODEL.reset(token)


def get_preferred_sub_model() -> str | None:
    """set_preferred_sub_model() で設定された現在のモデル名を返す（未設定なら None）。"""
    return _PREFERRED_SUB_MODEL.get()


# LLM へのリクエストが空き待ち（[llm].max_concurrent_requests のセマフォ、または
# 接続先の空きスロット待ち）に入った／抜けたことを知らせる先。src/subagent.py の
# run_subagent が自分のタスク内で設定し、進捗表示を「LLMの空き待ち」と
# 「思考中」等で切り替えるのに使う。contextvar なので他のジョブ・メイン
# エージェントの LLM 呼び出しには伝わらない。None なら通知しない。
_LLM_WAIT_LISTENER: contextvars.ContextVar[Callable[[bool], None] | None] = contextvars.ContextVar(
    "_llm_wait_listener", default=None
)


def set_llm_wait_listener(listener: Callable[[bool], None] | None) -> contextvars.Token:
    """これ以降このタスクの LLM 呼び出しの空き待ちを listener(待機中か) で知らせる。

    Returns:
        reset_llm_wait_listener() に渡すトークン。
    """
    return _LLM_WAIT_LISTENER.set(listener)


def reset_llm_wait_listener(token: contextvars.Token) -> None:
    """set_llm_wait_listener() の設定を元に戻す。"""
    _LLM_WAIT_LISTENER.reset(token)


def notify_llm_wait(waiting: bool) -> None:
    """空き待ちに入った（True）／抜けた（False）ことを listener へ知らせる。

    待っていなかった場合にも False を送ってよい（listener 側で状態が変わった
    ときだけ反映する前提）。表示のための通知なので、listener の例外で LLM
    呼び出しを失敗させない。
    """
    listener = _LLM_WAIT_LISTENER.get()
    if listener is None:
        return
    try:
        listener(waiting)
    except Exception:  # noqa: BLE001 - 進捗表示の失敗で生成を止めない
        logger.debug("Failed to notify about waiting for a free LLM", exc_info=True)


def set_current_session(session_id: str | None, *, tab_id: str | None = None) -> None:
    """これ以降このタスク（及びその子タスク）で build_model() が生成する
    httpx.AsyncClient を、どのセッションに紐づけて登録するかを設定する。

    app.py の @cl.on_chat_start・@cl.on_message 冒頭、および _rebuild_graph()
    から呼ぶ。各呼び出しは新しい asyncio.Task（＝ contextvars の独立した
    コピー）の中で行われるため、他タブへ値が漏れる心配はなく、明示的な
    reset も不要（次にこの関数が呼ばれるまで値を保持するだけでよい）。

    Args:
        session_id: このセッションの thread_id（cl.user_session の
            "thread_id"）。_active_async_clients のキーに使う（複数タブで
            同じ thread_id を開いた場合も意図的に共有する。上記docstring
            参照）。
        tab_id: 実際のブラウザタブ／接続の識別子（app.py から渡す場合は
            cl.context.session.id）。_LAST_SELECTED_INDEX のキーに使う
            （_CURRENT_TAB_ID docstring参照）。省略時は session_id を
            そのまま使う（tab_id の概念が無い呼び出し元向けの後方互換）。
    """
    _CURRENT_SESSION_ID.set(session_id)
    _CURRENT_TAB_ID.set(tab_id if tab_id is not None else session_id)


def get_current_session() -> str | None:
    """set_current_session() で設定された現在のセッションID（thread_id）を返す。

    src/tools.py がセッション毎の並列数ガード（_TOOL_CALL_SEMAPHORES /
    _DISPATCH_AGENT_SEMAPHORES）のキーとして流用する。未設定（evals/ の
    評価ハーネス等、Chainlitセッションを持たない呼び出し元）なら None。
    """
    return _CURRENT_SESSION_ID.get()


def forget_session(session_id: str) -> None:
    """セッション終了時（タブを閉じた等）に、_active_async_clients の
    ブックキーピング用エントリだけを片付ける。

    クライアント自体は値の WeakSet が参照を失い次第GCで自然に回収される
    ため、ここでは辞書キー（session_id文字列）がプロセス寿命中ずっと
    残り続けるのを防ぐのが目的。強制クローズは行わない
    （app.py の @cl.on_chat_end 参照: タスクキャンセルを伴わないため、
    孤立した処理が残っている可能性があり、ここで close すると新たな
    エラーを誘発しかねないため）。

    Args:
        session_id: 片付け対象セッションの thread_id。
    """
    _active_async_clients.pop(session_id, None)


async def aclose_active_llm_clients(session_id: str) -> None:
    """指定セッションに紐づく httpx.AsyncClient のみを強制的にクローズする。

    Chainlit の停止ボタンは `session.current_task.cancel()` で実行中タスクへ
    `asyncio.CancelledError` を投げ込むだけで、LLMサーバーへの根底のHTTP接続を
    切断する処理は持たない。`ChatLlamaCpp._astream` の `finally` 節にある
    `agen.aclose()` は、タスクが既にキャンセル済みのコンテキストでは正しく
    完了しない可能性が高く（コメント参照）、接続が生きたままだと llama-server
    側が生成を続けてしまい、停止ボタンを押しても CPU/GPU 使用率が下がらない
    事象につながる（tune-prompt iter27でユーザー報告・調査）。

    app.py の `@cl.on_stop` から、キャンセルされていない別の task コンテキストで
    呼び、明示的に接続を切断する。session_id には停止操作を行った自セッションの
    thread_id を渡すこと。これにより、他タブが使用中のクライアントには一切
    影響しない（以前はプロセス全体で共有される1つの WeakSet を無差別に
    クローズしており、それが他タブの巻き添え停止の原因になっていた）。

    Notes:
        強制クローズした `httpx.AsyncClient` は以降二度と使用できない。
        呼び出し元（app.py の on_stop）はこの直後に必ず自セッションのグラフを
        再構築し、新しい `build_model()` 呼び出しで新しいクライアントに
        差し替えること。差し替えないと、以降そのセッションで LLM 呼び出しが
        恒久的に壊れたままになる。
    """
    clients = _active_async_clients.pop(session_id, None)
    if not clients:
        return
    pending_cancel: asyncio.CancelledError | None = None
    for client in list(clients):
        try:
            await client.aclose()
        except asyncio.CancelledError as exc:
            # このタスク自体へキャンセル要求が届いても、残りのクライアントの
            # close は最後まで試みる（asyncioの定石: 後始末を終えてから
            # 呼び出し元へ伝播する）。ここで即raiseしない。
            pending_cancel = exc
        except Exception:  # noqa: BLE001 - 1件の失敗が残りのcloseを妨げないようにする
            logger.debug("Exception while closing httpx.AsyncClient", exc_info=True)
    if pending_cancel is not None:
        raise pending_cancel


async def aclose_model_client(model: ChatOpenAI) -> None:
    """指定した1つのモデルインスタンスに紐づく httpx.AsyncClient だけを強制クローズする。

    aclose_active_llm_clients(session_id) はセッション内の全クライアントを
    一括で閉じるため、dispatch_agent の並列サブエージェントやメイングラフが
    同じセッションで同時に別のクライアントを使用中だと巻き添えで
    "Cannot send a request, as the client has been closed" を招く
    （src/subagent.py の _invoke_with_loop_retry docstring 参照）。

    要約専用モデル（src/context_compaction.py の maybe_compact 等、
    build_model() を都度その場だけで使い捨てる呼び出し元）のように、
    「このモデルインスタンス1つの接続だけを確実に切断したいが、同じ
    セッションの他のクライアントには触れたくない」場合はこちらを使う。

    build_model() は ChatLlamaCpp(http_async_client=async_client) という
    形で httpx.AsyncClient を明示的に渡しており、langchain_openai は
    それをそのまま model.root_async_client._client として保持する
    （openai.AsyncOpenAI.close() が呼ぶのと同じクライアント。実測で
    `model.root_async_client._client is async_client` を確認済み）。
    ライブラリの非公開属性に依存するため、将来のバージョンアップで
    属性名が変わった場合に備えて例外は握りつぶし、ログのみに留める
    （force closeできなくても、build_model()側でリトライ用に新しい
    クライアントを都度生成する設計のため、呼び出し元のリトライ自体は
    引き続き機能する）。

    Args:
        model: build_model() が返した ChatOpenAI（ChatLlamaCpp）インスタンス。
    """
    client = getattr(getattr(model, "root_async_client", None), "_client", None)
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 - 1回のクローズ失敗でリトライ自体を止めない
        logger.debug("Exception while closing a model-specific client", exc_info=True)


# --- LLM接続先の簡易ルーティング ([llm].main_url / sub_url が複数件のとき) ---
# _LLM_REQUEST_SEMAPHORE 等と同じく、プロセス全体で共有するグローバル可変
# 状態（ロック不要。asyncio単一イベントループ内での逐次的な読み書きのみを
# 想定）。ただし _LAST_SELECTED_INDEX だけは role 単独ではなく
# (role, セッションID) をキーにする。複数タブ・複数ユーザーが同時に
# 接続する運用では、role単独キーだと「直近グローバルに選ばれたindex」が
# 別セッションの選択で上書きされてしまい、mark_last_endpoint_failed() が
# 実際には無関係な接続先をクールダウンしてしまう恐れがあるため
# （priority_failover を複数接続先・並行セッションで使う想定への対応）。
# _CURRENT_SESSION_ID は set_current_session(thread_id) が会話単位で設定する
# contextvar で、build_model() 呼び出し時と、後続の通信エラー検知時
# （app.py の except LLM_CONNECTION_ERRORS）の両方で同一会話中は同じ値になる。
_ROUND_ROBIN_COUNTERS: dict[str, int] = {}
_LAST_SELECTED_INDEX: dict[tuple[str, str], int] = {}
_ENDPOINT_COOLDOWN_UNTIL: dict[tuple[str, int], float] = {}
# priority_failover 戦略で、通信エラーを検知した接続先を一時的に避ける秒数。
_ENDPOINT_FAILOVER_COOLDOWN_SECONDS = 60.0

# --- GET /slots による空き確認を補う、自プロセス側の予約 ---
# build_model() が選んだが、まだ最初のリクエストを送っていない接続先の予約
# （llama-server のルート（_server_key、scheme+netloc）→ トークン→有効期限）。
# main_url と sub_url に同じサーバーを書いた場合も1台として数える。
# GET /slots は「送信済み」のリクエストしか反映しないため、複数の build_model() が
# 同時に同じ空きを見て全員がそこを選ぶ（priority_failover では全員が先頭を
# 優先するため必ず起きる）のを防ぐ。最初のリクエスト送信時（release_slot_reservation）に
# 解除し、送信されないまま捨てられたモデルの分は _SLOT_RESERVATION_TTL_SECONDS で
# 自然に失効させる。
_SLOT_RESERVATIONS: dict[str, dict[int, float]] = {}
_SLOT_RESERVATION_TTL_SECONDS = 30.0
_SLOT_RESERVATION_TOKENS = itertools.count(1)


class SlotCounts(NamedTuple):
    """GET /slots の集計結果。"""

    free: int
    total: int


def _server_key(base_url: str) -> str | None:
    """base_url（"http://host:port/v1" 等）から llama-server のルート（scheme+netloc）を取り出す。"""
    parsed = urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _active_reservations(key: str) -> int:
    """key のサーバーに対する有効な予約数（失効分はここで掃除する）。"""
    reservations = _SLOT_RESERVATIONS.get(key)
    if not reservations:
        return 0
    now = time.time()
    for token in [t for t, expires_at in reservations.items() if expires_at <= now]:
        del reservations[token]
    return len(reservations)


def reserve_slot(endpoint: LLMEndpoint) -> int | None:
    """build_model() が選んだ provider="llama_cpp" の接続先に、最初のリクエスト送信までの予約を入れる。

    Args:
        endpoint: 選ばれた接続先。

    Returns:
        予約トークン（release_slot_reservation に渡す）。llama_cpp 以外・base_url が
        不正な場合は None（予約しない）。
    """
    key = _server_key(endpoint.base_url)
    if endpoint.provider != "llama_cpp" or key is None:
        return None
    token = next(_SLOT_RESERVATION_TOKENS)
    _SLOT_RESERVATIONS.setdefault(key, {})[token] = time.time() + _SLOT_RESERVATION_TTL_SECONDS
    return token


def release_slot_reservation(base_url: str, reservation_token: int | None) -> None:
    """reserve_slot の予約を解除する。

    ChatLlamaCpp が同時実行数ガード（_LLM_REQUEST_SEMAPHORE）の内側、つまり
    実際にサーバーへ送る直前に呼ぶ（以後はサーバー側の GET /slots に反映される）。

    Args:
        base_url: 予約した接続先の base_url。
        reservation_token: reserve_slot の戻り値。None なら何もしない。
    """
    key = _server_key(base_url)
    if key is not None and reservation_token is not None:
        _SLOT_RESERVATIONS.get(key, {}).pop(reservation_token, None)


async def _probe_llama_cpp_slots_available(base_url: str, timeout_seconds: float) -> SlotCounts | None:
    """llama.cpp server の管理API GET /slots を叩き、空きスロット数と総数を返す。

    provider="llama_cpp" の接続先のみが対象（llama-server起動時に --slots が
    有効な場合のみ機能する）。base_url は "http://host:port/v1" のように
    OpenAI互換パス（/v1）付きの形式を想定しているが、/slots はそのパス配下
    ではなくサーバールート直下にあるため、scheme+netlocだけを取り出して
    組み立て直す。

    通信エラー・タイムアウト・想定外のレスポンス形式など、確実な判定が
    できない場合は必ず None を返す（例外は外へ伝播させない）。呼び出し元
    （_select_endpoint_with_slots_probe）は None を「わからない＝空きありとみなす」
    フェイルセーフとして扱う（round_robin/priority_failoverは元々スロット確認なしで即座に
    選んでいたため、確認できない場合は従来動作に寄せて選択を止めない）。

    Args:
        base_url: 接続先の base_url（LLMEndpoint.base_url）。
        timeout_seconds: リクエスト全体のタイムアウト秒数（[llm].
            round_robin_slots_probe_timeout_seconds）。

    Returns:
        SlotCounts(free=待機中のスロット数, total=スロット総数)。
        None: 確認できなかった（通信エラー・想定外のレスポンス形式・スロット0件等）。
    """
    key = _server_key(base_url)
    if key is None:
        return None
    slots_url = f"{key}/slots"
    timeout = httpx.Timeout(timeout_seconds, connect=min(2.0, timeout_seconds))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(slots_url)
            response.raise_for_status()
            slots = response.json()
    except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as exc:
        logger.debug("Checking GET %s failed (treating it as free)", slots_url, exc_info=exc)
        return None
    if not isinstance(slots, list) or not slots:
        return None
    try:
        free = sum(1 for slot in slots if not slot.get("is_processing", True))
    except AttributeError:
        return None
    return SlotCounts(free=free, total=len(slots))


def _endpoint_available_now(endpoint: LLMEndpoint) -> bool:
    """LLMEndpoint.start/end（使用可能時間帯、時間単位・0〜24）に基づき、現在時刻が範囲内かを判定する。

    start/end が両方 None（未指定）の接続先は常に True。start > end の場合は
    日をまたぐ範囲（例: start=22, end=6 なら22:00〜翌6:00）として扱う。

    Args:
        endpoint: 判定対象の接続先。

    Returns:
        現在時刻（ローカルタイム）が使用可能時間帯内なら True。
    """
    if endpoint.start is None and endpoint.end is None:
        return True
    now = time.localtime()
    now_hour = now.tm_hour + now.tm_min / 60.0 + now.tm_sec / 3600.0
    start = endpoint.start if endpoint.start is not None else 0.0
    end = endpoint.end if endpoint.end is not None else 24.0
    if start <= end:
        return start <= now_hour < end
    return now_hour >= start or now_hour < end


def _compute_eligible_indices(endpoints: tuple[LLMEndpoint, ...]) -> list[int]:
    """endpoints のうち現在時刻が start/end の使用可能時間帯内のものだけに絞り込む。

    _endpoint_available_now 参照。絞り込み結果が空になった場合は全件へ
    フォールバックする（config.py の _as_llm_endpoints が最低1件の常時
    使用可能な接続先を要求しているため通常は起こらないが、念のため）。

    Args:
        endpoints: 絞り込み対象の接続先タプル。

    Returns:
        使用可能な接続先の index 一覧（1件以上）。
    """
    eligible = [i for i, e in enumerate(endpoints) if _endpoint_available_now(e)]
    return eligible if eligible else list(range(len(endpoints)))


def _model_matching_indices(endpoints: tuple[LLMEndpoint, ...], model: str | None) -> list[int] | None:
    """endpoints のうち model が一致する接続先の index 一覧を返す。

    model 未指定、または一致する接続先が1件も無い場合は None（＝指定を
    無視して通常のルーティングに従う合図）を返す。

    比較は大文字小文字を区別しない。dispatch_agent の model 引数はLLMが
    ユーザー発話から書き写すため、"qwen3.6_35b-a3b" のように大小が崩れた
    だけで黙って無視されるのを避ける。

    Args:
        endpoints: 選択対象の接続先タプル。
        model: 優先するモデル名（LLMEndpoint.model と前後空白・大文字小文字を無視して比較）。

    Returns:
        一致した index 一覧（1件以上）、または None。
    """
    if not model:
        return None
    wanted = model.strip().casefold()
    matched = [i for i, e in enumerate(endpoints) if (e.model or "").strip().casefold() == wanted]
    return matched or None


def _narrow_to_model(eligible_indices: list[int], model_candidates: list[list[int]]) -> tuple[list[int], int | None]:
    """使用可能な接続先（eligible_indices）を、モデル候補の優先順に絞り込む。

    model_candidates は「指定モデルの接続先」「既定モデル（[llm].sub_default_model）
    の接続先」のように優先順に並べた index 一覧のリスト。先頭から見て、時間帯内の
    接続先が1件以上残る最初の候補で絞り込む。どの候補も全て時間帯外なら、
    指定が無いのと同じく eligible_indices をそのまま返す（「指定モデルが存在
    しない場合は無視」と同じ扱い）。

    Args:
        eligible_indices: _compute_eligible_indices() の結果。
        model_candidates: _model_matching_indices() の結果（None を除いたもの）を
            優先順に並べたリスト。空なら絞り込まない。

    Returns:
        (絞り込み後の index 一覧（1件以上）, 採用した候補の model_candidates 内の
        位置。どの候補も使えなかった／候補が無い場合は None)。
    """
    for position, model_indices in enumerate(model_candidates):
        narrowed = [i for i in eligible_indices if i in model_indices]
        if narrowed:
            return narrowed, position
    return eligible_indices, None


def _round_robin_order(role: str, eligible_indices: list[int], counter_key: str) -> list[int]:
    """round_robin戦略の候補の並び順。呼び出しごとに先頭を1つずつ回す。

    Args:
        role: "main" または "sub"（ログ用。カウンタは counter_key で分ける）。
        eligible_indices: 候補の index 一覧（1件以上）。
        counter_key: _ROUND_ROBIN_COUNTERS のキー。

    Returns:
        先頭から順に試す index 一覧。
    """
    counter = _ROUND_ROBIN_COUNTERS.get(counter_key, 0)
    _ROUND_ROBIN_COUNTERS[counter_key] = counter + 1
    return [eligible_indices[(counter + offset) % len(eligible_indices)] for offset in range(len(eligible_indices))]


def _priority_failover_order(role: str, eligible_indices: list[int]) -> list[int]:
    """priority_failover戦略の候補の並び順。クールダウン中でない接続先を優先順のまま並べる。

    全てクールダウン中の場合は、安全側として候補全件を優先順のまま返す
    （先頭＝使用可能な先頭へフォールバックする従来動作と同じ）。

    Args:
        role: "main" または "sub"（_ENDPOINT_COOLDOWN_UNTIL のキー）。
        eligible_indices: 候補の index 一覧（1件以上、優先順）。

    Returns:
        先頭から順に試す index 一覧。
    """
    now = time.time()
    order = [i for i in eligible_indices if _ENDPOINT_COOLDOWN_UNTIL.get((role, i), 0.0) <= now]
    logger.info(
        "Endpoint selection [priority_failover]: role=%s eligible_indices=%s cooldown_until(all per role)=%s now=%.3f -> order=%s",
        role,
        eligible_indices,
        {k: v for k, v in _ENDPOINT_COOLDOWN_UNTIL.items() if k[0] == role},
        now,
        order or eligible_indices,
    )
    return order or list(eligible_indices)


async def _select_endpoint_with_slots_probe(
    role: str,
    endpoints: tuple[LLMEndpoint, ...],
    strategy: str,
    *,
    probe_timeout_seconds: float,
    busy_poll_interval_seconds: float,
    wait_when_busy: bool = True,
    model_candidates: list[list[int]] | None = None,
) -> int:
    """round_robin / priority_failover 戦略の本体。戦略ごとの並び順で候補を
    先頭から試しつつ、provider="llama_cpp" の接続先だけは選ぶ前に GET /slots で
    空きスロットの有無を確認する。

    並び順は round_robin なら呼び出しごとに先頭を回した順（_round_robin_order）、
    priority_failover ならクールダウン中でない接続先の優先順（_priority_failover_order）。

    空き数からは自プロセスの未送信予約（_SLOT_RESERVATIONS、reserve_slot 参照）を
    差し引く。空きが無い（全スロット生成中）候補はスキップして次点へ回し、
    候補を一巡しても1件も空きが見つからなければ、wait_when_busy=True（既定）なら
    busy_poll_interval_seconds 秒待ってから再試行する（空きが出るまで無期限に
    待機する）。wait_when_busy=False なら待たずに並び順の先頭（order[0]）を
    暫定選択して即座に返す（呼び出し元が「今すぐ何らかの接続先が確定すれば
    よく、生成の予定が無い/未確定の操作」の場合に使う。build_model() 参照）。
    provider="openai_compatible"の接続先は確認を行わず、従来通り即座に選ぶ
    （空き状況が分からないサーバー種別のため）。

    候補（時間帯で使用可能な接続先）と並び順は待機の周回ごとに再計算する。
    空き待ちが長引いて start/end の境界をまたいだり、クールダウンが明けたり
    しても、次の周回では最新の状態に追従する（呼び出し時点の候補一覧を
    固定してしまうと、待機中に使用可能時間帯を外れた接続先を待ち続けたり、
    新しく使用可能になった接続先を見逃したりするため）。

    Args:
        role: "main" または "sub"。
        endpoints: 選択対象の接続先タプル。
        strategy: "round_robin" または "priority_failover"。
        probe_timeout_seconds: [llm].round_robin_slots_probe_timeout_seconds。
        busy_poll_interval_seconds: [llm].round_robin_busy_poll_interval_seconds。
        wait_when_busy: False の場合、全候補ビジー時に待機せず即座に
            フェイルセーフ選択する。
        model_candidates: モデル候補（指定モデル・既定モデルの接続先の index 一覧）を
            優先順に並べたリスト（_narrow_to_model 参照）。指定時は待機の周回ごとに
            時間帯内の接続先が残る最初の候補へ絞り込む（round_robin は順番も
            その候補専用のカウンタで回す）。

    Returns:
        選ばれた接続先の index（endpoints に対する）。
    """
    attempt = 0
    while True:
        eligible_indices, position = _narrow_to_model(_compute_eligible_indices(endpoints), model_candidates or [])
        if strategy == "priority_failover":
            order = _priority_failover_order(role, eligible_indices)
        else:
            counter_key = role if position is None else f"{role}|{','.join(map(str, model_candidates[position]))}"
            order = _round_robin_order(role, eligible_indices, counter_key)
        for index in order:
            endpoint = endpoints[index]
            if endpoint.provider != "llama_cpp":
                logger.info(
                    "Endpoint selection [%s]: role=%s order=%s -> index=%d (reason: chosen without a free-slot check because provider=%s)",
                    strategy,
                    role,
                    order,
                    index,
                    endpoint.provider,
                )
                return index
            counts = await _probe_llama_cpp_slots_available(endpoint.base_url, probe_timeout_seconds)
            if counts is None:
                logger.info(
                    "Endpoint selection [%s]: role=%s order=%s -> index=%d base_url=%s (reason: chosen as a fail-safe because GET /slots could not be checked)",
                    strategy,
                    role,
                    order,
                    index,
                    endpoint.base_url,
                )
                return index
            # 予約数は probe の await 後に読む（同時に選択中の他の build_model() が
            # その間に入れた予約を反映するため）。
            reserved = _active_reservations(_server_key(endpoint.base_url) or "")
            if counts.free - reserved > 0:
                logger.info(
                    "Endpoint selection [%s]: role=%s order=%s -> index=%d base_url=%s (reason: GET /slots free=%d/%d, unsent reservations in this process=%d)",
                    strategy,
                    role,
                    order,
                    index,
                    endpoint.base_url,
                    counts.free,
                    counts.total,
                    reserved,
                )
                return index
            logger.info(
                "Endpoint selection [%s]: role=%s index=%d base_url=%s skipped because it has no free slot (free=%d/%d, unsent reservations in this process=%d)",
                strategy,
                role,
                index,
                endpoint.base_url,
                counts.free,
                counts.total,
                reserved,
            )
        attempt += 1
        if not wait_when_busy:
            index = order[0]
            logger.warning(
                "Endpoint selection [%s]: role=%s no free slot on any candidate %s, but wait_when_busy=False so index=%d is chosen provisionally without waiting",
                strategy,
                role,
                order,
                index,
            )
            return index
        logger.warning(
            "Endpoint selection [%s]: role=%s no free slot on any candidate %s, waiting %.1f seconds (attempt %d; the available time windows are checked again next time)",
            strategy,
            role,
            order,
            busy_poll_interval_seconds,
            attempt,
        )
        notify_llm_wait(True)
        await asyncio.sleep(busy_poll_interval_seconds)


async def _select_endpoint(
    role: str,
    endpoints: tuple[LLMEndpoint, ...],
    strategy: str,
    *,
    inherit_from_role: str | None = None,
    probe_timeout_seconds: float = 3.0,
    busy_poll_interval_seconds: float = 2.0,
    wait_when_busy: bool = True,
    preferred_model: str | None = None,
    fallback_model: str | None = None,
) -> LLMEndpoint:
    """config.ini [llm].main_routing_strategy / sub_routing_strategy に従って接続先を1つ選ぶ。

    選択対象は、まず endpoints のうち現在時刻が start/end の使用可能時間帯内の
    ものだけに絞り込む（_endpoint_available_now 参照）。start/end 未指定の
    接続先は常に対象に含まれるため、config.py の _as_llm_endpoints が最低1件の
    常時使用可能な接続先を要求しており、絞り込み結果が空になることは無い
    （念のため空になった場合は全件にフォールバックする）。

    round_robin/priority_failover戦略でGET /slotsによる空き確認・空き待ち
    （_select_endpoint_with_slots_probe 参照）を行うため async def。
    build_model()からawaitで呼ぶ。

    Args:
        role: "main" または "sub"（ルーティング状態を役割ごとに分けるためのキー）。
        endpoints: config.main_endpoints または config.sub_endpoints。
        strategy: config.main_routing_strategy または config.sub_routing_strategy
            （"round_robin"/"random"/"priority_failover" のいずれか）。
            inherit_from_role が使われた場合は無視される。
        inherit_from_role: 指定時（config.sub_endpoints_inherit_main が True の
            ときの role="sub" 呼び出し）、strategy による独自選択は行わず、
            同一セッションIDで inherit_from_role（"main"）が直近実際に選んだ
            接続先indexをそのまま使う。dispatch_agent は同一 thread_id の
            contextvar をそのまま引き継ぐため（src/subagent.py 参照）、これに
            より「委譲元メインエージェントがこの会話で今使っている接続先」を
            継承できる。まだ inherit_from_role 側の選択が行われていない
            （このセッションでメインエージェントが一度もLLM呼び出しをして
            いない）場合のみ、安全側として通常のロジックへフォールバックする。
        probe_timeout_seconds: [llm].round_robin_slots_probe_timeout_seconds。
            round_robin/priority_failover戦略でGET /slots問い合わせ自体のタイムアウト秒数。
        busy_poll_interval_seconds: [llm].round_robin_busy_poll_interval_seconds。
            round_robin/priority_failover戦略で候補の全接続先に空きスロットが
            無かった場合、再確認までに待機する秒数。
        wait_when_busy: round_robin/priority_failover戦略で候補の全接続先がビジーだった場合に
            空きが出るまで待つか（True、既定）、待たずにフェイルセーフ選択
            するか（False）。build_model() の同名引数を参照。
        preferred_model: 指定時、model が一致する接続先だけを候補にして
            strategy に従って選ぶ（サブエージェントのモデル指定。
            _PREFERRED_SUB_MODEL 参照）。一致する接続先が無い（または全て
            時間帯外の）場合は fallback_model、それも使えなければ指定を無視して
            通常どおり選ぶ。inherit_from_role の継承は、継承する接続先が実際に
            採用したモデルと一致する場合のみ行う。
        fallback_model: preferred_model が未指定、または使えない（一致する
            接続先が無い／全て時間帯外）場合に代わりに使うモデル名
            （[llm].sub_default_model）。扱いは preferred_model と同じ。
            設定値由来のため、一致する接続先が無い・時間帯外のログは INFO に
            留める（名前の誤りは app.py が起動時に1回だけ WARNING を出す。
            時間帯外は時間帯指定の運用上の正常状態のため）。

    Returns:
        選ばれた LLMEndpoint。使用可能な接続先が1件かつ strategy が
        random の場合は常にそれを返す（round_robin/priority_failover は1件しか
        無い場合でも GET /slots による空き確認・待機を行う）。
    """
    # ログ表示・呼び出し元の把握用（会話単位のID）。未設定（サブエージェント
    # 経由でset_current_session未実行など）なら空文字として扱う。
    session_id = _CURRENT_SESSION_ID.get() or ""
    # _LAST_SELECTED_INDEX のキーは session_id（thread_id）ではなく tab_id
    # を使う。同じ thread_id を複数タブで同時に開いた場合でも、「このタブが
    # 直近実際に選んだ接続先」がタブをまたいで上書き・混線しないようにする
    # ため（mark_last_endpoint_failed() 参照。_CURRENT_TAB_ID docstring参照）。
    tab_id = _CURRENT_TAB_ID.get() or session_id
    state_key = (role, tab_id)

    logger.info(
        "Endpoint selection [start]: role=%s strategy=%s session_id=%r inherit_from_role=%r preferred_model=%r fallback_model=%r endpoints_total=%d endpoints=%s",
        role,
        strategy,
        session_id,
        inherit_from_role,
        preferred_model,
        fallback_model,
        len(endpoints),
        [f"[{i}] {e.base_url} model={e.model} start={e.start} end={e.end}" for i, e in enumerate(endpoints)],
    )

    # 優先順に並べたモデル候補（指定モデル → 既定モデル）。一致する接続先が
    # 無いものは除く。model_labels は model_candidates と同じ並びのログ用ラベル。
    model_candidates: list[list[int]] = []
    model_labels: list[str] = []
    for kind, name in (("preferred", preferred_model), ("default", fallback_model)):
        if not name:
            continue
        indices = _model_matching_indices(endpoints, name)
        if indices is None:
            # 指定モデル（LLM・定義ファイル由来）の誤りは WARNING、既定モデル
            # （設定値由来）は app.py が起動時に1回だけ WARNING を出すため INFO。
            logger.log(
                logging.WARNING if kind == "preferred" else logging.INFO,
                "Endpoint selection: role=%s session_id=%r no endpoint for the %s model %r, ignoring this setting",
                role,
                session_id,
                kind,
                name,
            )
        elif indices not in model_candidates:
            model_candidates.append(indices)
            model_labels.append(f"{kind} model {name!r}")

    # round_robin/priority_failover戦略はこのあと _select_endpoint_with_slots_probe 内で待機の
    # 周回ごとに自前で再計算するため、ここでの eligible_indices は
    # 「継承可否・単一候補の早期リターン判定」と下記ログ用のスナップショットに過ぎない。
    time_eligible_indices = _compute_eligible_indices(endpoints)
    eligible_indices, position = _narrow_to_model(time_eligible_indices, model_candidates)
    for skipped in range(len(model_candidates) if position is None else position):
        # 指定モデルの時間帯外は WARNING、既定モデル（時間帯指定の運用上の正常状態）は INFO。
        logger.log(
            logging.WARNING if model_labels[skipped].startswith("preferred") else logging.INFO,
            "Endpoint selection: role=%s session_id=%r all endpoints for %s are outside their time windows, falling back to %s",
            role,
            session_id,
            model_labels[skipped],
            f"routing for the {model_labels[position]}" if position is not None else "normal routing, ignoring the model setting",
        )

    if inherit_from_role is not None:
        inherited_index = _LAST_SELECTED_INDEX.get((inherit_from_role, tab_id))
        if inherited_index is not None and position is not None and inherited_index not in model_candidates[position]:
            logger.info(
                "Endpoint selection: role=%s session_id=%r the last selection of inherit_from_role=%s (index=%d) is not for the %s, so choosing from that model's endpoints instead of inheriting",
                role,
                session_id,
                inherit_from_role,
                inherited_index,
                model_labels[position],
            )
        elif inherited_index is not None and inherited_index < len(endpoints):
            _LAST_SELECTED_INDEX[state_key] = inherited_index
            logger.info(
                "Endpoint selection [result]: role=%s session_id=%r -> index=%d base_url=%s model=%s (reason: inherited the last selection of inherit_from_role=%s)",
                role,
                session_id,
                inherited_index,
                endpoints[inherited_index].base_url,
                endpoints[inherited_index].model,
                inherit_from_role,
            )
            return endpoints[inherited_index]
        else:
            logger.info(
                "Endpoint selection: role=%s session_id=%r no last selection for inherit_from_role=%s, falling back to the normal logic ((%s, %r) is not in _LAST_SELECTED_INDEX)",
                role,
                session_id,
                inherit_from_role,
                inherit_from_role,
                tab_id,
            )

    excluded_indices = [i for i in range(len(endpoints)) if i not in time_eligible_indices]
    if excluded_indices:
        logger.info(
            "Endpoint selection: role=%s session_id=%r indices excluded because they are outside their time windows=%s (check the start/end settings of %s)",
            role,
            session_id,
            excluded_indices,
            [f"[{i}] base_url={endpoints[i].base_url} start={endpoints[i].start} end={endpoints[i].end}" for i in excluded_indices],
        )

    if len(eligible_indices) == 1 and strategy == "random":
        index = eligible_indices[0]
        _LAST_SELECTED_INDEX[state_key] = index
        logger.info(
            "Endpoint selection [result]: role=%s strategy=%s session_id=%r -> index=%d/%d base_url=%s model=%s (reason: only one endpoint is available, so it is chosen regardless of the strategy. endpoints_total=%d)",
            role,
            strategy,
            session_id,
            index,
            len(endpoints),
            endpoints[index].base_url,
            endpoints[index].model,
            len(endpoints),
        )
        return endpoints[index]

    if strategy == "random":
        index = random.choice(eligible_indices)
        logger.info(
            "Endpoint selection [random]: role=%s session_id=%r candidates=%s -> index=%d",
            role,
            session_id,
            eligible_indices,
            index,
        )
    else:  # "round_robin"（既定）/ "priority_failover"
        index = await _select_endpoint_with_slots_probe(
            role,
            endpoints,
            "priority_failover" if strategy == "priority_failover" else "round_robin",
            probe_timeout_seconds=probe_timeout_seconds,
            busy_poll_interval_seconds=busy_poll_interval_seconds,
            wait_when_busy=wait_when_busy,
            model_candidates=model_candidates,
        )
        # 空きスロット待ちをしていた場合はここで抜けたことを知らせる（待っていなければ何も変わらない）。
        notify_llm_wait(False)

    _LAST_SELECTED_INDEX[state_key] = index
    logger.info(
        "Endpoint selection [final result]: role=%s strategy=%s session_id=%r -> index=%d/%d base_url=%s model=%s state_key=%r _LAST_SELECTED_INDEX(all per role)=%s",
        role,
        strategy,
        session_id,
        index,
        len(endpoints),
        endpoints[index].base_url,
        endpoints[index].model,
        state_key,
        {k: v for k, v in _LAST_SELECTED_INDEX.items() if k[0] == role},
    )
    return endpoints[index]


def mark_last_endpoint_failed(role: str) -> None:
    """直近この会話の build_model(config, role) が選んだ接続先を一時的にクールダウンする。

    priority_failover 戦略専用のフィードバックフック。通信エラーを検知した
    呼び出し元（現状は app.py の except LLM_CONNECTION_ERRORS）が、エラーを
    検知した会話のコンテキスト内（set_current_session(thread_id) 済みの状態）
    でここを呼ぶと、次回以降の build_model() 呼び出しで
    _ENDPOINT_FAILOVER_COOLDOWN_SECONDS秒間その接続先を避け、次点の接続先へ
    切り替わる（round_robin/random 戦略では index は記録されるが参照
    されないため実質無視される）。

    _LAST_SELECTED_INDEX は (role, タブID) 単位で管理しているため、
    複数タブ・複数ユーザーが同時に接続していても、他タブの選択に
    巻き込まれず「このタブが直近実際に使っていた接続先」だけを
    クールダウンできる（同じ thread_id を複数タブで開いた場合も含む。
    _CURRENT_TAB_ID docstring参照）。

    Args:
        role: "main" または "sub"。
    """
    tab_id = _CURRENT_TAB_ID.get() or _CURRENT_SESSION_ID.get() or ""
    index = _LAST_SELECTED_INDEX.get((role, tab_id))
    if index is None:
        return
    _ENDPOINT_COOLDOWN_UNTIL[(role, index)] = time.time() + _ENDPOINT_FAILOVER_COOLDOWN_SECONDS
    logger.warning(
        "Detected an LLM connection failure, temporarily avoiding the endpoint (role=%s, index=%d, for %.0f seconds)",
        role,
        index,
        _ENDPOINT_FAILOVER_COOLDOWN_SECONDS,
    )
