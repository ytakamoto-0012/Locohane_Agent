"""埋め込み（embedding）を推論サーバーの /v1/embeddings で計算する。"""

from __future__ import annotations

from array import array
import base64
from collections import deque
from contextlib import contextmanager
import logging
import random
import sys
import threading
import time
from typing import Callable, Literal, NamedTuple
from urllib.parse import urlsplit

import httpx

from ..config import Config, LLMEndpoint

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """埋め込みを計算できなかった（接続できない・応答が不正等）。"""


class EmbeddingStopped(EmbeddingError):
    """再試行を待っている間に should_stop() が True を返した（取り込みの「停止」）。"""


# 接続先の選び方（[vector_db].embedding_routing_strategy）のための状態。管理ツールのプロセスでは
# 複数インスタンスの設定が混在するため、接続先の base_url（の並び）をキーにする。
_routing_lock = threading.Lock()
_round_robin_counters: dict[tuple[str, ...], int] = {}
# priority_failover で、エラーが返った接続先を後回しにする期限（base_url → time.monotonic()）。
_failed_until: dict[str, float] = {}
_FAILOVER_COOLDOWN_SECONDS = 60.0
# このプロセスから送信中（空きを確かめて選んでから応答が返るまで）のリクエスト数（サーバーのルート → 件数）。
# 複数の取り込みスレッドが同時に GET /slots で同じ空きを見て、全員がそこへ送るのを防ぐ（_has_room 参照）。
_inflight: dict[str, int] = {}
# _release() の通知（_routing_lock を使う）と、その回数（空きを確かめている間に送信が返ったかの判定に使う）。
_routing_cond = threading.Condition(_routing_lock)
_releases = 0
# 空きを待っている埋め込みの待ち行列（接続先の base_url の並び → 待ち始めた順の目印）。_choose_order 参照。
_waiters: dict[tuple[str, ...], deque[object]] = {}
# 全接続先が埋まっているときに確かめ直す間隔の上限（秒）。[llm].round_robin_busy_poll_interval_seconds は
# チャットの長い生成に合わせた値のため、1回が短い埋め込みではこれより長く待たない。
_BUSY_POLL_MAX_SECONDS = 0.5
# encoding_format = base64 に対応していない（400・422 を返した）接続先の base_url。以後は float で受け取る。
_float_only: set[str] = set()
# 再試行を待つ間に should_stop() を確かめる間隔（秒）。
_STOP_POLL_SECONDS = 0.5
# 再試行の待ちに使う sleep（テストで差し替える）。
_sleep = time.sleep
# open_client() の keep-alive の接続数に、同時に送るリクエストの数へ足す分（GET /slots で空きを確かめる接続の分）。
_KEEPALIVE_MARGIN = 8


def embedding_model(cfg: Config) -> str:
    """設定中の埋め込みモデル名（コレクションに記録し、検索時の一致確認に使う）。

    Raises:
        EmbeddingError: [vector_db].embedding_url が未設定の場合。
    """
    if not cfg.vector_db_embedding_endpoints:
        raise EmbeddingError("[vector_db].embedding_url is not set.")
    return cfg.vector_db_embedding_endpoints[0].model


def open_client(cfg: Config, *, transport: httpx.BaseTransport | None = None, concurrency: int = 0) -> httpx.Client:
    """埋め込みサーバー用の httpx クライアント（呼び出し側で close する）。

    取り込みのように何度も埋め込むときは、これを1つ作って embed_texts・embed_images の client に渡し、
    接続を使い回す。Windows では接続先が localhost だと新しく接続するたびに IPv6（::1）を試してから
    IPv4 へ戻るため約 2 秒かかり、ファイルごとにクライアントを作り直すとその分だけ遅くなる。
    httpx.Client はスレッド間で共有できる。

    concurrency は同時に送るリクエストの数（取り込みの ingest_embed_requests）。使い終えた接続を閉じずに残す数
    （keep-alive）をこれに合わせる。httpx の既定（20本）のままだと、それを超えて同時に送った分の接続が毎回
    閉じて作り直され、閉じた接続（TIME_WAIT）がたまって Windows のソケットのバッファーが尽きる（WinError 10055）。
    """
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=max(concurrency + _KEEPALIVE_MARGIN, 20))
    return httpx.Client(
        timeout=httpx.Timeout(cfg.vector_db_embedding_timeout_seconds, connect=10.0), transport=transport, limits=limits
    )


# shared_client() のクライアント（タイムアウト秒ごと）。プロセスが終わるまで閉じない。
_shared_clients: dict[float, httpx.Client] = {}
_shared_lock = threading.Lock()


def shared_client(cfg: Config) -> httpx.Client:
    """プロセス内で使い回す埋め込みサーバー用のクライアント（検索のように1件ずつ何度も埋め込む用。close しない）。

    検索のたびに open_client() で作り直すと、localhost では毎回約 2 秒かかる（open_client 参照）。
    """
    timeout = cfg.vector_db_embedding_timeout_seconds
    with _shared_lock:
        client = _shared_clients.get(timeout)
        if client is None or client.is_closed:
            client = _shared_clients[timeout] = open_client(cfg)
        return client


@contextmanager
def _client_scope(cfg: Config, client: httpx.Client | None, transport: httpx.BaseTransport | None):
    """client が渡されていればそれを（閉じずに）、無ければこの呼び出しの間だけのクライアントを使う。"""
    if client is not None:
        yield client
        return
    with open_client(cfg, transport=transport) as own:
        yield own


def embed_texts(
    cfg: Config,
    texts: list[str],
    *,
    kind: Literal["document", "query"],
    transport: httpx.BaseTransport | None = None,
    client: httpx.Client | None = None,
    should_stop: Callable[[], bool] | None = None,
    wait_when_busy: bool | None = None,
) -> list[list[float]]:
    """texts の埋め込みベクターを入力と同じ順で返す。

    [vector_db].embedding_batch_size 件ずつまとめて送り、kind に応じて
    query_prefix / document_prefix を先頭に付ける。接続先の選び方とエラー時の
    切り替え・再試行は _embed_batch 参照（再試行の回数は kind="document" なら
    embedding_max_retries、kind="query" なら search_embedding_max_retries）。

    Args:
        cfg: 設定。
        texts: 埋め込む文字列。
        kind: "document"（取り込み時）または "query"（検索時）。
        transport: テスト用に差し替える httpx のトランスポート（client を渡したときは使わない）。
        client: open_client() で作ったクライアント。省略時はこの呼び出しの間だけのものを作る。
        should_stop: 再試行を待つ間に確かめ、True を返したら EmbeddingStopped を投げてやめる。
        wait_when_busy: 全接続先が埋まっているときに空きを待つか（_choose_order）。省略時は kind="document"
            （取り込み）なら待ち、"query"（検索）なら待たない。止める手段の無い呼び出し（管理アシスタントの索引等）は
            False を渡すこと。

    Raises:
        EmbeddingError: どの接続先からも埋め込みを得られなかった場合
            （EmbeddingStopped は再試行の待ち中に止められた場合）。
    """
    endpoints = cfg.vector_db_embedding_endpoints
    if not endpoints:
        raise EmbeddingError("[vector_db].embedding_url is not set.")
    prefix = cfg.vector_db_query_prefix if kind == "query" else cfg.vector_db_document_prefix
    inputs = [prefix + t for t in texts]
    vectors: list[list[float]] = []
    batch_size = cfg.vector_db_embedding_batch_size
    # 検索は止める手段が無いため、無制限（embedding_max_retries = 0）にはしない。
    max_retries = cfg.vector_db_search_embedding_max_retries if kind == "query" else (cfg.vector_db_embedding_max_retries or None)
    # 検索は AI の応答を待たせるため、接続先が埋まっていても空きを待たずに送る。
    if wait_when_busy is None:
        wait_when_busy = kind == "document"
    with _client_scope(cfg, client, transport) as http:
        for start in range(0, len(inputs), batch_size):
            vectors.extend(_embed_batch(cfg, http, endpoints, inputs[start : start + batch_size], max_retries, should_stop, wait_when_busy))
    return vectors


def embed_images(
    cfg: Config,
    pngs: list[bytes],
    *,
    transport: httpx.BaseTransport | None = None,
    client: httpx.Client | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[list[float]]:
    """PNG 画像の埋め込みベクターを入力と同じ順で返す（[vector_db].image_input = true の取り込み用）。

    llama-server の /v1/embeddings の画像入力（--mmproj 付きで起動したマルチモーダルの
    埋め込みモデル）の形式 {"content": [{"type": "image_url", ...}]} で送る。
    画像はデータが大きく、llama-server では同じリクエスト内の画像入力の結果が
    揺れる不具合も報告されているため、embedding_batch_size によらず1枚ずつ送る。
    document_prefix はテキスト用のため付けない。transport・client・should_stop は embed_texts と同じ。

    Raises:
        EmbeddingError: どの接続先からも埋め込みを得られなかった場合
            （埋め込みモデルが画像入力に対応していない場合を含む）。
    """
    endpoints = cfg.vector_db_embedding_endpoints
    if not endpoints:
        raise EmbeddingError("[vector_db].embedding_url is not set.")
    vectors: list[list[float]] = []
    with _client_scope(cfg, client, transport) as http:
        for png in pngs:
            url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            batch = [{"content": [{"type": "image_url", "image_url": {"url": url}}]}]
            vectors.extend(_embed_batch(cfg, http, endpoints, batch, cfg.vector_db_embedding_max_retries or None, should_stop, True))
    return vectors


def _endpoint_order(cfg: Config, endpoints: tuple[LLMEndpoint, ...]) -> list[LLMEndpoint]:
    """[vector_db].embedding_routing_strategy に従い、1回のリクエストで試す接続先の順を返す。

    - round_robin: リクエストごとに先頭を1つずつ回す（並列の取り込みで負荷を分ける）。
    - random: リクエストごとに無作為に並べる。
    - priority_failover: 書いた順。直近 _FAILOVER_COOLDOWN_SECONDS 秒以内にエラーが返った接続先は後回しにする。

    どの戦略でも全接続先を並べ、先頭でエラーが返れば次を試す（_embed_batch）。
    """
    if len(endpoints) <= 1:
        return list(endpoints)
    strategy = cfg.vector_db_embedding_routing_strategy
    if strategy == "round_robin":
        key = tuple(e.base_url for e in endpoints)
        with _routing_lock:
            counter = _round_robin_counters.get(key, 0)
            _round_robin_counters[key] = counter + 1
        offset = counter % len(endpoints)
        return list(endpoints[offset:] + endpoints[:offset])
    if strategy == "random":
        return random.sample(endpoints, len(endpoints))
    now = time.monotonic()
    with _routing_lock:
        cooling = {e.base_url for e in endpoints if _failed_until.get(e.base_url, 0.0) > now}
    return [e for e in endpoints if e.base_url not in cooling] + [e for e in endpoints if e.base_url in cooling]


class _Load(NamedTuple):
    """接続先の空き状況。total が None なら同時に受けられる数は分からない（vLLM）。"""

    free: int
    total: int | None


def _server_key(base_url: str) -> str | None:
    """base_url（"http://host:port/v1" 等）からサーバーのルート（scheme+netloc）を取り出す。"""
    parsed = urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _probe_load(cfg: Config, client: httpx.Client, endpoint: LLMEndpoint) -> _Load | None:
    """provider="llama_cpp" は GET /slots、"vllm" は GET /metrics で接続先の空き状況を確かめる。

    - llama_cpp: is_processing でないスロットの数と、スロットの総数。
    - vllm: スロットの考え方が無いため、vllm:num_requests_waiting（スケジューラの待ち行列。
      データ並列なら全エンジンの合計）が 0 なら空き1・1以上なら空き0とする。

    タイムアウトは [llm].round_robin_slots_probe_timeout_seconds。確かめられなかった場合
    （接続できない・形式が違う・対象外の provider）は None（呼び出し元は空きとみなす）。
    """
    key = _server_key(endpoint.base_url)
    if key is None or endpoint.provider not in ("llama_cpp", "vllm"):
        return None
    probe_timeout = cfg.round_robin_slots_probe_timeout_seconds
    timeout = httpx.Timeout(probe_timeout, connect=min(2.0, probe_timeout))
    url = f"{key}/slots" if endpoint.provider == "llama_cpp" else f"{key}/metrics"
    try:
        response = client.get(url, timeout=timeout)
        response.raise_for_status()
        if endpoint.provider == "llama_cpp":
            slots = response.json()
            if not isinstance(slots, list) or not slots:
                return None
            return _Load(free=sum(1 for slot in slots if not slot.get("is_processing", True)), total=len(slots))
        waiting: float | None = None
        for line in response.text.splitlines():
            if line.split("{", 1)[0].split(" ", 1)[0] == "vllm:num_requests_waiting":
                waiting = (waiting or 0.0) + float(line.rsplit(None, 1)[-1])
        return None if waiting is None else _Load(free=0 if waiting > 0 else 1, total=None)
    except (httpx.TransportError, httpx.HTTPStatusError, ValueError, AttributeError) as e:
        logger.debug("embeddings: checking GET %s failed (treating it as free): %s", url, e)
        return None


def _has_room(load: _Load, inflight: int) -> bool:
    """空きがあるか。llama_cpp はこのプロセスから送信中の数を総数から引いた残りでも抑える
    （GET /slots に送信直前・直後のリクエストがまだ反映されていない分を補う）。"""
    if load.total is None:
        return load.free > 0
    return min(load.free, load.total - inflight) > 0


def _claim(key: str | None) -> None:
    if key is not None:
        with _routing_lock:
            _inflight[key] = _inflight.get(key, 0) + 1


def _release(key: str | None) -> None:
    """送信中の数を減らし、空きを待っている埋め込み（_choose_order）にすぐ確かめ直させる。"""
    global _releases
    if key is not None:
        with _routing_lock:
            _inflight[key] = max(0, _inflight.get(key, 0) - 1)
            _releases += 1
            _routing_cond.notify_all()


def _cond_wait(seconds: float) -> None:
    """_routing_lock を持った状態で、_release()・待ち行列の変化の知らせを最大 seconds 秒待つ（テストで差し替える）。"""
    _routing_cond.wait(seconds)


def _try_choose(
    cfg: Config, client: httpx.Client, endpoints: tuple[LLMEndpoint, ...]
) -> tuple[list[LLMEndpoint], bool] | None:
    """_endpoint_order() の順に空きを確かめ（_probe_load）、空きのある最初の接続先を先頭にした順を返す
    （確かめられない接続先・その他の provider は確かめずに選ぶ）。全接続先が埋まっていれば None。

    2つ目の戻り値は、先頭の接続先を送信中として数え済み（_claim）か。
    """
    order = _endpoint_order(cfg, endpoints)
    for i, endpoint in enumerate(order):
        key = _server_key(endpoint.base_url)
        load = _probe_load(cfg, client, endpoint)
        with _routing_lock:
            inflight = _inflight.get(key, 0) if key is not None else 0
            chosen = load is None or _has_room(load, inflight)
            if chosen and key is not None:
                _inflight[key] = inflight + 1
        if chosen:
            return [endpoint] + order[:i] + order[i + 1 :], key is not None
        logger.debug(
            "embeddings: %s has no free slot (%s, sending from this process=%d), trying the next endpoint",
            endpoint.base_url,
            load,
            inflight,
        )
    return None


def _choose_order(
    cfg: Config,
    client: httpx.Client,
    endpoints: tuple[LLMEndpoint, ...],
    wait_when_busy: bool,
    should_stop: Callable[[], bool] | None,
) -> tuple[list[LLMEndpoint], bool]:
    """1回のリクエストで試す接続先の順を決める（src/llm/routing.py の _select_endpoint_with_slots_probe と同じ考え方）。

    round_robin / priority_failover では、_try_choose() で空きのある接続先を選ぶ。全接続先が埋まっていれば、
    wait_when_busy なら空きが出るまで待ち（should_stop() が True なら EmbeddingStopped）、そうでなければ
    待たずに並び順の先頭を使う。random は確かめない。

    空きを待つ埋め込みは、同じ接続先の組み合わせごとに待ち始めた順に並べ（_waiters）、先頭だけが空きを確かめる
    （後から来たリクエストが、先に待っていたものを追い越して空きを取り続けないため。追い越されたファイルは、
    設定ダッシュボードで「進んでいない」と表示されていた）。先頭は、このプロセスの送信が返ったら（_release）すぐに、
    ほかのプロセスの負荷で埋まっている間は [llm].round_robin_busy_poll_interval_seconds 秒（最大
    _BUSY_POLL_MAX_SECONDS 秒。埋め込みは1回が短いため）ごとに確かめ直す。

    選んだ接続先を先頭に、残りをエラー時の切り替え先として並べて返す。2つ目の戻り値は、
    先頭の接続先を送信中として数え済み（_claim）か。
    """
    if cfg.vector_db_embedding_routing_strategy == "random":
        return _endpoint_order(cfg, endpoints), False
    if not wait_when_busy:
        chosen = _try_choose(cfg, client, endpoints)
        return chosen if chosen is not None else (_endpoint_order(cfg, endpoints), False)
    queue_key = tuple(e.base_url for e in endpoints)
    ticket = object()
    with _routing_lock:
        queue = _waiters.setdefault(queue_key, deque())
        queue.append(ticket)
    interval = min(cfg.round_robin_busy_poll_interval_seconds, _BUSY_POLL_MAX_SECONDS)
    waited = False
    try:
        while True:
            if should_stop is not None and should_stop():
                raise EmbeddingStopped("Stopped waiting for a free slot of the embedding server because a stop was requested.")
            with _routing_lock:
                if queue[0] is not ticket:
                    # 先に待っているものが空きを取るまで待つ（取ったら、または止めたら知らせが来る）。
                    _cond_wait(_STOP_POLL_SECONDS)
                    continue
                releases = _releases
            chosen = _try_choose(cfg, client, endpoints)
            if chosen is not None:
                if waited:
                    logger.debug("embeddings: got a free slot after waiting")
                return chosen
            if not waited:
                logger.info("embeddings: no free slot on any endpoint, waiting for one (checking every %g seconds)", interval)
                waited = True
            with _routing_lock:
                # 確かめている間に送信が返っていれば、待たずにすぐ確かめ直す。
                if releases == _releases:
                    _cond_wait(interval)
    finally:
        with _routing_lock:
            queue.remove(ticket)
            if not queue and _waiters.get(queue_key) is queue:
                del _waiters[queue_key]
            _routing_cond.notify_all()


def _mark_failed(endpoint: LLMEndpoint) -> None:
    """endpoint を priority_failover で一時的に後回しにする（他の戦略では参照しない）。"""
    with _routing_lock:
        _failed_until[endpoint.base_url] = time.monotonic() + _FAILOVER_COOLDOWN_SECONDS


def _retryable_status(status: int) -> bool:
    """時間を置いて試し直す意味のある HTTP ステータスか（4xx は 408・429 以外は入力・設定の誤りとみなす）。"""
    return not (400 <= status < 500) or status in (408, 429)


def _wait_retry(seconds: float, should_stop: Callable[[], bool] | None) -> None:
    """seconds 秒待つ。待つ間に should_stop() が True を返したら EmbeddingStopped を投げる。"""
    remaining = seconds
    while True:
        if should_stop is not None and should_stop():
            raise EmbeddingStopped("Stopped retrying the embeddings because a stop was requested.")
        if remaining <= 0:
            return
        step = min(remaining, _STOP_POLL_SECONDS)
        _sleep(step)
        remaining -= step


def _embed_batch(
    cfg: Config,
    client: httpx.Client,
    endpoints: tuple[LLMEndpoint, ...],
    batch: list[str | dict],
    max_retries: int | None,
    should_stop: Callable[[], bool] | None,
    wait_when_busy: bool,
) -> list[list[float]]:
    """batch を埋め込む。エラーが返れば次の接続先へ切り替え、全接続先でエラーなら時間を置いて試し直す。

    最初に送る接続先は _choose_order() が空き状況を見て決める（wait_when_busy はそちら参照）。
    接続できない・HTTP 200 以外が返ったら、_choose_order() の順で次の接続先を試す。全接続先で
    エラーになったら [vector_db].embedding_retry_interval_seconds 秒待って全接続先を試し直し、これを
    max_retries 回まで（None なら成功するまで無制限に、0 なら試し直さずに）繰り返す。ただし、すべてのエラーが
    408・429 以外の 4xx（入力・設定の誤りで、待っても直らない）なら試し直さない。
    応答の形が不正な場合（_parse_response）は設定の誤りのため、切り替えずにすぐ投げる。
    """
    retries = 0
    while True:
        errors: list[str] = []
        retryable = False
        order, claimed = _choose_order(cfg, client, endpoints, wait_when_busy, should_stop)
        for n, endpoint in enumerate(order):
            url = endpoint.base_url.rstrip("/") + "/embeddings"
            key = _server_key(endpoint.base_url)
            if not (n == 0 and claimed):
                _claim(key)
            try:
                response = _post(cfg, client, endpoint, url, batch)
            except httpx.TransportError as e:
                logger.warning("embeddings: cannot connect to %s: %s", url, e)
                errors.append(f"Cannot connect to {url} ({e})")
                retryable = True
                _mark_failed(endpoint)
                continue
            finally:
                _release(key)
            if response.status_code != 200:
                logger.warning("embeddings: %s returned an error (HTTP %d): %s", url, response.status_code, response.text[:300])
                errors.append(f"{url} returned an error (HTTP {response.status_code}): {response.text[:300]}")
                if _retryable_status(response.status_code):
                    retryable = True
                    _mark_failed(endpoint)
                continue
            return _parse_response(url, response.json(), len(batch))
        detail = " / ".join(errors)
        if not retryable or (max_retries is not None and retries >= max_retries):
            suffix = f" (retried {retries} times)" if retries else ""
            raise EmbeddingError(f"Could not compute the embeddings{suffix}: {detail}")
        retries += 1
        interval = cfg.vector_db_embedding_retry_interval_seconds
        logger.warning(
            "embeddings: every endpoint returned an error. Retrying in %g seconds (%d/%s): %s",
            interval,
            retries,
            "unlimited" if max_retries is None else max_retries,
            detail,
        )
        _wait_retry(interval, should_stop)


def _post(cfg: Config, client: httpx.Client, endpoint: LLMEndpoint, url: str, batch: list[str | dict]) -> httpx.Response:
    """endpoint へ batch を送る。

    [vector_db].embedding_encoding_format = base64 なら encoding_format を付けて送り、接続先が 400・422 を返したら
    （encoding_format に対応していない）、その接続先を以後 float で受け取ることにして同じリクエストを送り直す。
    """
    headers = {"Authorization": f"Bearer {endpoint.api_key}"}
    body: dict = {"model": endpoint.model, "input": batch}
    if cfg.vector_db_embedding_encoding_format != "base64" or endpoint.base_url in _float_only:
        return client.post(url, json=body, headers=headers)
    response = client.post(url, json={**body, "encoding_format": "base64"}, headers=headers)
    if response.status_code in (400, 422):
        logger.warning(
            "embeddings: %s did not accept encoding_format=base64 (HTTP %d). Using float for this endpoint from now on: %s",
            url,
            response.status_code,
            response.text[:300],
        )
        with _routing_lock:
            _float_only.add(endpoint.base_url)
        response = client.post(url, json=body, headers=headers)
    return response


def _decode_base64_vector(text: str) -> list[float]:
    """encoding_format = base64 の埋め込み（リトルエンディアンの float32 の並び）をベクターにする。"""
    raw = base64.b64decode(text)
    if not raw or len(raw) % 4:
        raise ValueError("The base64 length is not a sequence of float32")
    values = array("f")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tolist()


def _parse_response(url: str, body: dict, expected: int) -> list[list[float]]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != expected:
        raise EmbeddingError(f"data in the response from {url} does not match the number of inputs ({expected}).")
    ordered = sorted(data, key=lambda item: item.get("index", 0))
    vectors = []
    for item in ordered:
        embedding = item.get("embedding")
        if isinstance(embedding, str):
            # encoding_format = base64 の応答（対応していない接続先は無視して数値の配列で返すため、どちらも受け付ける）。
            try:
                vectors.append(_decode_base64_vector(embedding))
            except ValueError as e:
                raise EmbeddingError(f"Cannot read the base64 embeddings in the response from {url}: {e}") from e
            continue
        # llama-server は --pooling none のとき トークンごとのベクター（2次元）を返す。検索には使えない。
        if not isinstance(embedding, list) or not embedding or isinstance(embedding[0], (list, str)):
            raise EmbeddingError(f"The response from {url} is not a one-dimensional vector (start llama-server with --pooling other than none).")
        vectors.append(list(map(float, embedding)))
    return vectors
