"""Qdrant（REST API を httpx で直接呼ぶ）によるベクターの保存先。

Qdrant 上のコレクション名は [vector_db].qdrant_collection_prefix + Locohane のコレクション名。
接頭辞の付いたコレクション以外には触れない（同じ Qdrant サーバーを他の用途と共有できる）。
コレクションの距離関数は [vector_db].distance（作るときに決まり、後から変えるには再構築が要る）。
索引・量子化・ディスク配置は [vector_db].qdrant_preset（QDRANT_PRESETS。既存のコレクションにも
apply_preset() で反映でき、埋め込み直しは要らない）。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterator

import httpx

from .store import DISTANCE_METRICS, ChunkRecord, Hit, StoreError, StoreStopped, distance_to_score

logger = logging.getLogger(__name__)

# ポイント ID（UUID）を source_path とチャンク番号から決めるための名前空間。
_POINT_NAMESPACE = uuid.UUID("6b7f2f0e-3c1d-4f5a-9a51-1f0c2d7e8b90")
_UPSERT_BATCH = 256
# 検索（store.open_store の shared=True）で送り直す回数の上限と間隔の上限（秒）。検索は止める手段が無く、
# [vector_db].qdrant_max_retries = -1（無制限）のままだと AI の応答が返らなくなるため。
SEARCH_MAX_RETRIES = 1
SEARCH_RETRY_INTERVAL_SECONDS = 1.0
# 検索の1リクエストのタイムアウトの上限（秒）。[vector_db].qdrant_timeout_seconds は取り込みの書き込みの反映待ち
# （wait=true）に合わせて長くしてあるため、検索ではこれより長く待たない。
SEARCH_TIMEOUT_SECONDS = 30.0
# 接続（TCP の確立）のタイムアウトの上限（秒）。Qdrant のホストが落ちて応答が無いときに、読み取りの長いタイムアウトまで
# 待たずに失敗させる（送り直しの判断を早くする）。
CONNECT_TIMEOUT_SECONDS = 10.0
# 送り直しを待つ間に should_stop() を確かめる間隔（秒）。
_STOP_POLL_SECONDS = 0.5
# 送り直しの待ちに使う sleep（テストで差し替える）。
_sleep = time.sleep


@dataclass(frozen=True)
class QdrantPreset:
    """[vector_db].qdrant_preset の1つ分（想定するチャンク件数の規模ごとの Qdrant の設定）。

    どのプリセットも、ベクターを int8 にスカラー量子化した複製を RAM に置いて検索し
    （上位の候補は元のベクターで再採点する）、チャンク本文等の payload はディスクに置く。

    m / ef_construct: HNSW 索引のつながりの数・作るときの探索幅（大きいほど件数が多くても
        取りこぼしにくいが、索引の作成が遅くメモリーも増える）。
    hnsw_ef: 検索時の探索幅（大きいほど正確で遅い）。
    vectors_on_disk: 元の（float32 の）ベクターをディスク（mmap）に置くか。量子化した複製は RAM に置く。
    segments: コレクションを分けるセグメント数の目安（並列に検索する単位。CPU のコア数程度）。
    oversampling: 量子化した複製で top_k の何倍を候補にして、元のベクターで再採点するか。
    """

    m: int
    ef_construct: int
    hnsw_ef: int
    vectors_on_disk: bool
    segments: int
    oversampling: float


# config.ini の qdrant_preset の説明・README_DETAIL.md「Qdrant を使う」の表と一致させる。
# キーは src/config.py の VECTOR_DB_QDRANT_PRESETS（none 以外）。
QDRANT_PRESETS: dict[str, QdrantPreset] = {
    "1m": QdrantPreset(m=16, ef_construct=128, hnsw_ef=128, vectors_on_disk=False, segments=4, oversampling=1.5),
    "2m": QdrantPreset(m=16, ef_construct=200, hnsw_ef=160, vectors_on_disk=True, segments=6, oversampling=2.0),
    "3m": QdrantPreset(m=24, ef_construct=256, hnsw_ef=200, vectors_on_disk=True, segments=8, oversampling=2.0),
    "4m": QdrantPreset(m=32, ef_construct=256, hnsw_ef=256, vectors_on_disk=True, segments=8, oversampling=2.5),
    "5m": QdrantPreset(m=32, ef_construct=320, hnsw_ef=288, vectors_on_disk=True, segments=10, oversampling=2.5),
    "6m": QdrantPreset(m=40, ef_construct=320, hnsw_ef=320, vectors_on_disk=True, segments=10, oversampling=2.5),
    "7m": QdrantPreset(m=40, ef_construct=384, hnsw_ef=352, vectors_on_disk=True, segments=12, oversampling=3.0),
    "8m": QdrantPreset(m=48, ef_construct=384, hnsw_ef=384, vectors_on_disk=True, segments=12, oversampling=3.0),
    "9m": QdrantPreset(m=48, ef_construct=448, hnsw_ef=416, vectors_on_disk=True, segments=16, oversampling=3.0),
    "10m": QdrantPreset(m=64, ef_construct=512, hnsw_ef=448, vectors_on_disk=True, segments=16, oversampling=3.0),
}
_SCALAR_QUANTIZATION = {"scalar": {"type": "int8", "quantile": 0.99, "always_ram": True}}


def _preset_config(preset: QdrantPreset) -> dict:
    """コレクションの作成・更新（PATCH）で共通に送る、ベクター以外の設定。"""
    return {
        "hnsw_config": {"m": preset.m, "ef_construct": preset.ef_construct, "on_disk": False},
        "optimizers_config": {"default_segment_number": preset.segments},
        "quantization_config": _SCALAR_QUANTIZATION,
    }


def _preset_matches(info: dict, preset: QdrantPreset) -> bool:
    """GET /collections/{name} の設定が preset と同じか（違えば PATCH で更新する）。"""
    config = info.get("result", {}).get("config", {})
    params = config.get("params", {})
    vectors = params.get("vectors", {})
    hnsw = config.get("hnsw_config") or {}
    optimizer = config.get("optimizer_config") or {}
    return (
        isinstance(vectors, dict)
        and bool(vectors.get("on_disk", False)) == preset.vectors_on_disk
        and hnsw.get("m") == preset.m
        and hnsw.get("ef_construct") == preset.ef_construct
        and optimizer.get("default_segment_number") == preset.segments
        and config.get("quantization_config") == _SCALAR_QUANTIZATION
        and bool(params.get("on_disk_payload", False))
    )


# shared_client() のクライアント（接続先・API キー・タイムアウトごと）。プロセスが終わるまで閉じない。
_shared_clients: dict[tuple[str, str, float], httpx.Client] = {}
_shared_lock = threading.Lock()


def _new_client(url: str, api_key: str, timeout: float, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    headers = {"api-key": api_key} if api_key else {}
    timeouts = httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT_SECONDS))
    return httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=timeouts, transport=transport)


def shared_client(url: str, api_key: str, timeout: float) -> httpx.Client:
    """プロセス内で使い回す Qdrant 用のクライアント（検索のように何度も開く用。close しない）。

    検索のたびに作り直すと、Windows で接続先が localhost だと毎回 IPv6（::1）を試してから
    IPv4 へ戻るため約 2 秒かかる（embeddings.open_client と同じ）。
    """
    key = (url.rstrip("/"), api_key, timeout)
    with _shared_lock:
        client = _shared_clients.get(key)
        if client is None or client.is_closed:
            client = _shared_clients[key] = _new_client(url, api_key, timeout)
        return client


def _delete_operation(source_path: str) -> dict:
    return {"delete": {"filter": {"must": [{"key": "source_path", "match": {"value": source_path}}]}}}


def _upsert_operations(source_path: str, start: int, chunks: list[ChunkRecord], vectors: list[list[float]]) -> list[dict]:
    """chunks を start 番目からのポイントにし、_UPSERT_BATCH 件ずつの upsert にする。

    vectors の各ベクターは list でなくてもよい（取り込みは array('d') で持つ。JSON にするため list にする）。
    """
    points = [
        {
            "id": str(uuid.uuid5(_POINT_NAMESPACE, f"{source_path}#{i}")),
            "vector": vector if isinstance(vector, list) else list(vector),
            "payload": {"source_path": source_path, "locator": chunk.locator, "text": chunk.text},
        }
        for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True), start)
    ]
    return [{"upsert": {"points": points[i : i + _UPSERT_BATCH]}} for i in range(0, len(points), _UPSERT_BATCH)]


def _chunks_of(points: list[dict]) -> list[ChunkRecord]:
    return [
        ChunkRecord(locator=str((p.get("payload") or {}).get("locator", "")), text=str((p.get("payload") or {}).get("text", "")))
        for p in points
    ]


def _vectors_of(points: list[dict]) -> list[list[float]]:
    return [[float(x) for x in p.get("vector") or []] for p in points]


def _retryable_status(status: int) -> bool:
    """時間を置いて送り直す意味のある HTTP ステータスか（5xx・408・429。それ以外の 4xx は要求・設定の誤り）。"""
    return status >= 500 or status in (408, 429)


def _point_count(operation: dict) -> int:
    return len(operation["upsert"]["points"]) if "upsert" in operation else 0


def _collection_distance(info: dict) -> str | None:
    vectors = info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
    return vectors.get("distance") if isinstance(vectors, dict) else None


class QdrantStore:
    def __init__(
        self,
        url: str,
        *,
        prefix: str,
        api_key: str = "",
        timeout: float = 30,
        distance: str = "Cosine",
        preset: str = "none",
        transport: httpx.BaseTransport | None = None,
        shared: bool = False,
        max_retries: int = 0,
        retry_interval: float = 10.0,
        should_stop: Callable[[], bool] | None = None,
    ):
        """shared=True なら shared_client() を使い、close() でも閉じない（transport は使わない）。

        preset は [vector_db].qdrant_preset（QDRANT_PRESETS のキー。"none" なら Qdrant の既定のまま）。
        max_retries・retry_interval は送り直しの回数（-1 は無制限、0 は送り直さない）と間隔（秒）。_request 参照。
        should_stop は送り直しを待つ間に確かめ、True を返したら StoreStopped を投げる。
        """
        self._max_retries = max_retries
        self._retry_interval = retry_interval
        self._should_stop = should_stop
        if not url:
            raise StoreError("[vector_db].qdrant_url is not set.")
        self._owns_client = not shared
        self._client = shared_client(url, api_key, timeout) if shared else _new_client(url, api_key, timeout, transport)
        self._prefix = prefix
        self._distance = distance
        self._preset = QDRANT_PRESETS.get(preset)
        # 検索したコレクションの距離関数（作ったときの設定。今の [vector_db].distance と違うことがある）。
        self._distances: dict[str, str | None] = {}
        # get_source() で source_path の索引を確かめた Qdrant 上のコレクション名。
        self._indexed: set[str] = set()
        # begin_batch() から commit() までにためた更新（Qdrant 上のコレクション名 → points/batch の operations）。
        self._batching = False
        self._pending: dict[str, list[dict]] = {}
        self._pending_points = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _name(self, name: str) -> str:
        return self._prefix + name

    def _request(self, method: str, path: str, *, allow_404: bool = False, **kwargs) -> dict | None:
        """Qdrant へ1リクエストを送る。

        接続できない・接続を切られた・タイムアウトした（httpx.TransportError）・5xx・408・429 が返ったときは、
        retry_interval 秒待って同じリクエストを送り直す（max_retries 回まで。-1 なら成功するまで）。
        送り直しても結果が変わらないよう、更新は冪等なものだけにしている（ポイントの ID は source_path と連番から決まり、
        削除は source_path の絞り込み、コレクションの作成は ensure_collection が作成済みを確かめる）。
        それ以外の 4xx は要求・設定の誤りのため、送り直さずに StoreError を投げる。
        """
        retries = 0
        while True:
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as e:
                error = f"Cannot connect to Qdrant ({self._client.base_url}, {method} {path}): {type(e).__name__}: {e}"
            else:
                if allow_404 and response.status_code == 404:
                    return None
                if response.status_code < 400:
                    return response.json()
                error = f"Qdrant returned an error ({method} {path}, HTTP {response.status_code}): {response.text[:300]}"
                if not _retryable_status(response.status_code):
                    raise StoreError(error)
            if 0 <= self._max_retries <= retries:
                raise StoreError(error + (f" (retried {retries} times)" if retries else ""))
            retries += 1
            logger.warning(
                "qdrant: %s. Retrying in %g seconds (%d/%s).",
                error, self._retry_interval, retries, "unlimited" if self._max_retries < 0 else self._max_retries,
            )
            self._wait_retry()

    def _wait_retry(self) -> None:
        """retry_interval 秒待つ。待つ間に should_stop() が True を返したら StoreStopped を投げる。"""
        remaining = self._retry_interval
        while True:
            if self._should_stop is not None and self._should_stop():
                raise StoreStopped("Stopped retrying the request to Qdrant because a stop was requested.")
            if remaining <= 0:
                return
            step = min(remaining, _STOP_POLL_SECONDS)
            _sleep(step)
            remaining -= step

    def ensure_collection(self, name: str, dim: int) -> None:
        qname = self._name(name)
        info = self._request("GET", f"/collections/{qname}", allow_404=True)
        if info is None:
            body: dict = {"vectors": {"size": dim, "distance": self._distance}}
            if self._preset is not None:
                body["vectors"]["on_disk"] = self._preset.vectors_on_disk
                body.update(_preset_config(self._preset), on_disk_payload=True)
            try:
                self._request("PUT", f"/collections/{qname}", json=body)
            except StoreStopped:
                raise
            except StoreError:
                # タイムアウト・切断の後に送り直した作成は、1回目で作れていれば「既にある」エラーになる。
                # 作れていたなら下で次元・距離関数を確かめて続け、無ければ元のエラーのまま。
                info = self._request("GET", f"/collections/{qname}", allow_404=True)
                if info is None:
                    raise
            # source_path での削除（再取り込み・ファイル削除時）を速くするための索引（作成済みでも送ってよい）。
            self._request(
                "PUT", f"/collections/{qname}/index", params={"wait": "true"}, json={"field_name": "source_path", "field_schema": "keyword"}
            )
            if info is None:
                return
        vectors = info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
        size = vectors.get("size") if isinstance(vectors, dict) else None
        if size is not None and size != dim:
            raise StoreError(f"The vector dimension ({size}) of Qdrant collection {qname} differs from the embedding dimension ({dim}). Rebuild it.")
        distance = _collection_distance(info)
        if distance is not None and distance != self._distance:
            raise StoreError(
                f"The distance ({distance}) of Qdrant collection {qname} differs from [vector_db].distance ({self._distance}). Rebuild it."
            )
        self._apply_preset(qname, info)

    def apply_preset(self, name: str) -> None:
        """既存のコレクション name の設定を [vector_db].qdrant_preset に合わせる（無い・preset が none なら何もしない）。

        索引・量子化は Qdrant が裏で作り直す（埋め込み直しは要らない。作り直す間も検索はできる）。
        """
        if self._preset is None:
            return
        qname = self._name(name)
        info = self._request("GET", f"/collections/{qname}", allow_404=True)
        if info is not None:
            self._apply_preset(qname, info)

    def _apply_preset(self, qname: str, info: dict) -> None:
        if self._preset is None or _preset_matches(info, self._preset):
            return
        # 名前の無いベクター（このストアは名前を付けない）の設定は "" の名前で更新する。
        body = {"vectors": {"": {"on_disk": self._preset.vectors_on_disk}}, **_preset_config(self._preset), "params": {"on_disk_payload": True}}
        self._request("PATCH", f"/collections/{qname}", json=body)

    def drop_collection(self, name: str) -> None:
        self._distances.pop(name, None)
        self._pending.pop(self._name(name), None)
        self._request("DELETE", f"/collections/{self._name(name)}", allow_404=True)

    # --- 更新（削除・追加）は POST /collections/{name}/points/batch でまとめて送る ---

    def begin_batch(self) -> None:
        """以後の delete_source・replace_source を commit() までためて、まとめて送る（取り込み用）。

        ためたポイントが _UPSERT_BATCH 件を超えたら commit() を待たずに送る。同じコレクションへの更新は
        ためた順に送るため、削除と追加の順序は変わらない。
        """
        self._batching = True

    def commit(self) -> None:
        """ためた更新を送り、反映されるまで待つ（wait=true）。"""
        pending, self._pending, self._pending_points = self._pending, {}, 0
        for qname, operations in pending.items():
            self._send_operations(qname, operations)

    def rollback(self) -> None:
        """まだ送っていない更新を捨てる（既に送った分は戻らない）。"""
        self._pending, self._pending_points = {}, 0

    def end_batch(self) -> None:
        self.commit()
        self._batching = False

    def _queue(self, name: str, operations: list[dict]) -> None:
        qname = self._name(name)
        if not self._batching:
            self._send_operations(qname, operations)
            return
        self._pending.setdefault(qname, []).extend(operations)
        self._pending_points += sum(_point_count(op) for op in operations)
        if self._pending_points >= _UPSERT_BATCH:
            self.commit()

    def _send_operations(self, qname: str, operations: list[dict]) -> None:
        """operations を、1リクエストあたりのポイントが _UPSERT_BATCH 件程度になるよう分けて順に送る。

        削除だけのリクエストはコレクションが無ければ何もしない（404 を無視する）。
        """
        group: list[dict] = []
        count = 0
        for op in operations:
            n = _point_count(op)
            if group and count + n > _UPSERT_BATCH:
                self._send_batch(qname, group)
                group, count = [], 0
            group.append(op)
            count += n
        if group:
            self._send_batch(qname, group)

    def _send_batch(self, qname: str, operations: list[dict]) -> None:
        only_deletes = all("delete" in op for op in operations)
        self._request(
            "POST", f"/collections/{qname}/points/batch", allow_404=only_deletes, params={"wait": "true"}, json={"operations": operations}
        )

    def delete_source(self, name: str, source_path: str) -> None:
        self._queue(name, [_delete_operation(source_path)])

    def replace_source(self, name: str, source_path: str, chunks: list[ChunkRecord], vectors: list[list[float]]) -> None:
        self._queue(name, [_delete_operation(source_path)] + _upsert_operations(source_path, 0, chunks, vectors))

    def append_source(self, name: str, source_path: str, start: int, chunks: list[ChunkRecord], vectors: list[list[float]]) -> None:
        # ポイントの ID は replace_source と同じく "<source_path>#<連番>" から作る（get_source で元の順に戻せる）。
        self._queue(name, _upsert_operations(source_path, start, chunks, vectors))

    def dim(self, name: str) -> int | None:
        info = self._request("GET", f"/collections/{self._name(name)}", allow_404=True)
        if info is None:
            return None
        vectors = info.get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
        size = vectors.get("size") if isinstance(vectors, dict) else None
        return int(size) if size is not None else None

    def get_source(self, name: str, source_path: str) -> tuple[list[ChunkRecord], list[list[float]]]:
        """source_path のチャンクとベクターを、replace_source() で入れた順に返す（コレクションが無ければ空）。"""
        qname = self._name(name)
        if qname not in self._indexed:
            # source_path の索引の無い古いコレクションで、ファイルごとの絞り込みが全件の走査にならないよう作る（あれば何もしない）。
            self._request(
                "PUT", f"/collections/{qname}/index", allow_404=True, params={"wait": "true"},
                json={"field_name": "source_path", "field_schema": "keyword"},
            )
            self._indexed.add(qname)
        points: list[dict] = []
        offset = None
        while True:
            body: dict = {
                "filter": {"must": [{"key": "source_path", "match": {"value": source_path}}]},
                "limit": _UPSERT_BATCH,
                "with_payload": True,
                "with_vector": True,
            }
            if offset is not None:
                body["offset"] = offset
            result = self._request("POST", f"/collections/{qname}/points/scroll", allow_404=True, json=body)
            if result is None:
                return [], []
            points.extend(result.get("result", {}).get("points", []))
            offset = result.get("result", {}).get("next_page_offset")
            if offset is None:
                break
        # ポイントの ID は "<source_path>#<連番>" から作る（replace_source）ため、ID から元の順番に戻せる。
        order = {str(uuid.uuid5(_POINT_NAMESPACE, f"{source_path}#{i}")): i for i in range(len(points))}
        points.sort(key=lambda p: order.get(str(p.get("id")), len(points)))
        return _chunks_of(points), _vectors_of(points)

    def iter_source(self, name: str, source_path: str) -> Iterator[tuple[list[ChunkRecord], list[list[float]]]]:
        """source_path のチャンクとベクターを、入れた順に _UPSERT_BATCH 件ずつ返す（コレクションが無ければ何も返さない）。

        ポイントの ID は "<source_path>#<連番>" から決まる（replace_source・append_source）ため、連番の順に
        ID を指定して取り出す（scroll で全件を読んでから並べ替えない）。返った件数が求めた数より少なければ終わり。
        """
        qname = self._name(name)
        start = 0
        while True:
            ids = [str(uuid.uuid5(_POINT_NAMESPACE, f"{source_path}#{i}")) for i in range(start, start + _UPSERT_BATCH)]
            result = self._request(
                "POST", f"/collections/{qname}/points", allow_404=True, json={"ids": ids, "with_payload": True, "with_vector": True}
            )
            if result is None:
                return
            order = {point_id: i for i, point_id in enumerate(ids)}
            points = sorted(result.get("result") or [], key=lambda p: order.get(str(p.get("id")), len(ids)))
            if points:
                yield _chunks_of(points), _vectors_of(points)
            if len(points) < len(ids):
                return
            start += _UPSERT_BATCH

    def _search_distance(self, name: str) -> str | None:
        """コレクション name の距離関数（無ければ None）。同じストアでは1回だけ問い合わせる。"""
        if name not in self._distances:
            info = self._request("GET", f"/collections/{self._name(name)}", allow_404=True)
            self._distances[name] = None if info is None else _collection_distance(info)
        return self._distances[name]

    def _query_body(self, vector: list[float], top_k: int) -> dict:
        body: dict = {"query": vector, "limit": int(top_k), "with_payload": True}
        if self._preset is not None:
            # 量子化していないコレクション（まだ apply_preset していないもの）では quantization は無視される。
            body["params"] = {"hnsw_ef": self._preset.hnsw_ef, "quantization": {"rescore": True, "oversampling": self._preset.oversampling}}
        return body

    def search(self, name: str, vector: list[float], top_k: int) -> list[Hit]:
        """Hit.score は、Cosine・Dot では Qdrant の値そのもの、Euclid・Manhattan では 1 / (1 + 距離)。"""
        distance = self._search_distance(name)
        if distance is None:
            return []
        body = self._request(
            "POST",
            f"/collections/{self._name(name)}/points/query",
            allow_404=True,
            json=self._query_body(vector, top_k),
        )
        if body is None:
            return []
        hits = []
        for point in body.get("result", {}).get("points", []):
            payload = point.get("payload") or {}
            score = float(point.get("score", 0.0))
            if distance in DISTANCE_METRICS:
                score = distance_to_score(score)
            hits.append(
                Hit(
                    collection=name,
                    score=score,
                    source_path=str(payload.get("source_path", "")),
                    locator=str(payload.get("locator", "")),
                    text=str(payload.get("text", "")),
                )
            )
        return hits
