"""ベクターDB のテスト用の偽物（埋め込みサーバー・Qdrant サーバー）と設定の作り方。

pytest は各テストファイルのディレクトリを sys.path に足すため、
tests/ 配下のテストから `import _vector_db_fakes` で使える。
"""

from __future__ import annotations

import base64
import dataclasses
import json
import math
import struct
from pathlib import Path

import httpx

from src.config import LLMEndpoint, load_config

DIM = 32


def fake_vector(text: str) -> list[float]:
    """文字コードを DIM 個の箱に数えた正規化ベクター（同じ文字を多く含むほど近くなる）。"""
    v = [0.0] * DIM
    for ch in text:
        if not ch.isspace():
            v[ord(ch) % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _input_text(item) -> str:
    """入力1件を、ベクターを作る元の文字列にする。

    画像入力（{"content": [{"type": "image_url", ...}]}）は、データ URL の中身を UTF-8 として読む
    （テストでは「画像」の代わりに文字列のバイト列を渡し、画像の内容に応じたベクターにする）。
    """
    if isinstance(item, str):
        return item
    url = item["content"][0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    return base64.b64decode(url.split(",", 1)[1]).decode("utf-8", errors="ignore")


class FakeEmbeddingServer:
    """/v1/embeddings の偽物。受け取った入力を requests に記録する。"""

    def __init__(self, *, fail_hosts: tuple[str, ...] = (), two_dimensional: bool = False, reject_base64: bool = False):
        self.requests: list[dict] = []
        self.fail_hosts = fail_hosts
        self.two_dimensional = two_dimensional
        # True なら encoding_format（base64）に対応していないサーバーとして 400 を返す。
        self.reject_base64 = reject_base64

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in self.fail_hosts:
            raise httpx.ConnectError("refused", request=request)
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), **body})
        if self.reject_base64 and "encoding_format" in body:
            return httpx.Response(400, json={"error": "unknown field encoding_format"})
        data = []
        for i, item in enumerate(body["input"]):
            vec = fake_vector(_input_text(item))
            if body.get("encoding_format") == "base64":
                data.append({"index": i, "embedding": base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")})
                continue
            data.append({"index": i, "embedding": [vec, vec] if self.two_dimensional else vec})
        return httpx.Response(200, json={"data": data, "model": body["model"]})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeQdrant:
    """Qdrant REST API のうち QdrantStore が使う操作だけを再現する偽物。"""

    def __init__(self):
        self.collections: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        # PATCH /collections/{name} と POST .../points/query で受け取った本文。
        self.patches: list[tuple[str, dict]] = []
        self.queries: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        parts = path.strip("/").split("/")
        body = json.loads(request.content) if request.content else {}
        if parts[0] != "collections" or len(parts) < 2:
            return httpx.Response(404, json={"status": {"error": "not found"}})
        name = parts[1]
        rest = parts[2:]
        coll = self.collections.get(name)
        if not rest:
            if request.method == "GET":
                if coll is None:
                    return httpx.Response(404, json={"status": {"error": "Not found"}})
                return httpx.Response(200, json={"result": {"config": coll["config"]}})
            if request.method == "PUT":
                # GET で返す形（実際の Qdrant と同じく、作成時の optimizers_config は optimizer_config で返す）。
                config = {
                    "params": {"vectors": dict(body["vectors"]), "on_disk_payload": body.get("on_disk_payload", True)},
                    "hnsw_config": {"m": 16, "ef_construct": 100, "on_disk": False, **body.get("hnsw_config", {})},
                    "optimizer_config": {"default_segment_number": 0, **body.get("optimizers_config", {})},
                    "quantization_config": body.get("quantization_config"),
                }
                self.collections[name] = {"size": body["vectors"]["size"], "distance": body["vectors"]["distance"], "points": {}, "config": config}
                return httpx.Response(200, json={"result": True})
            if request.method == "PATCH":
                if coll is None:
                    return httpx.Response(404, json={"status": {"error": "Not found"}})
                self.patches.append((name, body))
                config = coll["config"]
                config["params"]["vectors"].update(body.get("vectors", {}).get("", {}))
                config["params"].update(body.get("params", {}))
                config["hnsw_config"].update(body.get("hnsw_config", {}))
                config["optimizer_config"].update(body.get("optimizers_config", {}))
                if "quantization_config" in body:
                    config["quantization_config"] = body["quantization_config"]
                return httpx.Response(200, json={"result": True})
            if request.method == "DELETE":
                self.collections.pop(name, None)
                return httpx.Response(200, json={"result": coll is not None})
        if coll is None:
            return httpx.Response(404, json={"status": {"error": "Not found"}})
        if rest == ["index"]:
            return httpx.Response(200, json={"result": {}})
        if rest == ["points"] and request.method == "PUT":
            self._upsert(coll, body["points"])
            return httpx.Response(200, json={"result": {}})
        if rest == ["points"] and request.method == "POST":
            # ID を指定した取り出し（無い ID は返さない。順序は保証しない実際の Qdrant に合わせて逆順で返す）。
            found = [{"id": i, **coll["points"][i]} for i in body["ids"] if i in coll["points"]]
            return httpx.Response(200, json={"result": found[::-1]})
        if rest == ["points", "delete"]:
            self._delete(coll, body["filter"])
            return httpx.Response(200, json={"result": {}})
        if rest == ["points", "batch"] and request.method == "POST":
            # 実際の Qdrant と同じく、operations を順に適用する（削除と追加の順序が保たれる）。
            for op in body["operations"]:
                if "upsert" in op:
                    self._upsert(coll, op["upsert"]["points"])
                else:
                    self._delete(coll, op["delete"]["filter"])
            return httpx.Response(200, json={"result": [{"status": "completed"}] * len(body["operations"])})
        if rest == ["points", "scroll"]:
            # source_path の一致だけの filter と、整数の offset でのページ送り。
            value = body["filter"]["must"][0]["match"]["value"]
            matched = [{"id": k, **p} for k, p in coll["points"].items() if p["payload"]["source_path"] == value]
            start = int(body.get("offset") or 0)
            page = matched[start : start + body["limit"]]
            next_offset = start + body["limit"] if start + body["limit"] < len(matched) else None
            return httpx.Response(200, json={"result": {"points": page, "next_page_offset": next_offset}})
        if rest == ["points", "query"]:
            self.queries.append(body)
            q = body["query"]
            # Euclid・Manhattan は距離（小さいほど近い）を返し、昇順に並べる（実際の Qdrant と同じ）。
            metric = {
                "Euclid": lambda v: sum((a - b) ** 2 for a, b in zip(q, v)) ** 0.5,
                "Manhattan": lambda v: sum(abs(a - b) for a, b in zip(q, v)),
            }.get(coll["distance"], lambda v: sum(a * b for a, b in zip(q, v)))
            scored = [{"id": k, "score": metric(p["vector"]), "payload": p["payload"]} for k, p in coll["points"].items()]
            scored.sort(key=lambda s: s["score"], reverse=coll["distance"] not in ("Euclid", "Manhattan"))
            return httpx.Response(200, json={"result": {"points": scored[: body["limit"]]}})
        return httpx.Response(400, json={"status": {"error": f"unexpected {request.method} {path}"}})

    @staticmethod
    def _upsert(coll: dict, points: list[dict]) -> None:
        for p in points:
            assert len(p["vector"]) == coll["size"]
            coll["points"][p["id"]] = p

    @staticmethod
    def _delete(coll: dict, filter_: dict) -> None:
        value = filter_["must"][0]["match"]["value"]
        coll["points"] = {k: p for k, p in coll["points"].items() if p["payload"]["source_path"] != value}

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def make_config(tmp_path: Path, **changes):
    """[vector_db] を有効にした設定（データは tmp_path 配下）。"""
    cfg = load_config()
    defaults = {
        "vector_db_enabled": True,
        "vector_db_dir": tmp_path / "vector_db",
        "vector_db_embedding_endpoints": (LLMEndpoint(base_url="http://embed/v1", api_key="k", model="fake-embed"),),
        "vector_db_embedding_batch_size": 16,
        "vector_db_embedding_routing_strategy": "priority_failover",
        "vector_db_embedding_max_retries": 3,
        "vector_db_search_embedding_max_retries": 2,
        "vector_db_embedding_retry_interval_seconds": 10.0,
        # base64 は専用のテストで確かめる（既存のテストの偽物のサーバーは数値の配列で返す）。
        "vector_db_embedding_encoding_format": "float",
        "vector_db_ingest_processes": 0,
        "vector_db_min_score": 0.0,
        "vector_db_image_min_score": 0.0,
        "vector_db_path_min_score": 0.0,
        "vector_db_rrf_weight_text": 1.0,
        "vector_db_rrf_weight_keyword": 1.0,
        "vector_db_rrf_weight_image": 1.0,
        "vector_db_rrf_weight_path_keyword": 1.0,
        "vector_db_chunk_size": 200,
        "vector_db_chunk_overlap": 20,
        "vector_db_qdrant_url": "http://qdrant:6333",
        "vector_db_qdrant_collection_prefix": "locohane_test_",
    }
    defaults.update(changes)
    return dataclasses.replace(cfg, **defaults)
