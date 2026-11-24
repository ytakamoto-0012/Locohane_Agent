"""ビューア用データを、ブラウザへ送る形（パッケージ）にする・読み戻す。

ビューア用データ（src/cad/mesh_data.py）をそのまま 1 本の JSON で送ると、曲面の多い STL では
三角形 100 万個で 500MB を超え、ブラウザが 1 本の文字列として扱える長さ（約 5.4 億文字）に届く。
そこで、ブラウザへは次の形のファイルを HTTP（app.py の GET /locohane/cad/data/{id}）で渡す。

    b"LCVD" | u32 版 | u32 ヘッダーの長さ | ヘッダー（UTF-8 の JSON） | 塊…

- ヘッダー: {"data": 大きな項目を除いたビューア用データ, "blobs": [{"path", "type", "offset", "length"}, ...]}
- type "bytes": base64 だった配列（三角形の座標・番号・色、エッジ・頂点の座標、比較の重ね表示）を
  復号したそのままのバイト列。offset は 8 の倍数（ブラウザで Float32Array 等として直接見るため）。
- type "json": 要素の一覧（faces・edgeList・vertexList・parts）を JSON_CHUNK_ITEMS 件ずつに分けた JSON の配列。
  同じ path の塊を順につなげると元の一覧になる。

LLM が読む viewer_data（`json_query` で読む JSON ファイル）はこれまでどおりで、このパッケージは
ブラウザへ送るためだけに作る（frontend/src/components/cad/cadData.ts の parseCadPackage が読む）。
数値はすべてリトルエンディアン。
"""

from __future__ import annotations

import base64
import json
import os
import struct
from pathlib import Path
from typing import Any

MAGIC = b"LCVD"
VERSION = 1
_ALIGN = 8
# 1 つの JSON の塊に入れる要素の数（1 要素は数百バイト〜数 KB なので、塊は数 MB〜数十 MB に収まる）。
JSON_CHUNK_ITEMS = 20000
# 一覧（JSON の塊に分ける）のキー。
LIST_KEYS = ("faces", "edgeList", "vertexList", "parts")
# base64 の配列（バイト列にする）の場所。
BYTES_PATHS = (
    ("mesh", "positions"),
    ("mesh", "indices"),
    ("mesh", "colors"),
    ("edges", "positions"),
    ("vertices", "positions"),
    ("diff", "added", "positions"),
    ("diff", "added", "indices"),
    ("diff", "removed", "positions"),
    ("diff", "removed", "indices"),
)


class PackageError(ValueError):
    """パッケージとして読めない。"""


def _get(data: dict, path: tuple[str, ...]) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _copy_without(data: dict) -> dict:
    """大きな項目（塊にするもの）を除いた浅いコピー（入れ子の dict は、除く項目がある分だけコピーする）。"""
    out = {k: v for k, v in data.items() if k not in LIST_KEYS}
    for path in BYTES_PATHS:
        if not isinstance(_get(data, path), str):
            continue
        node = out
        for key in path[:-1]:
            node[key] = dict(node[key])
            node = node[key]
        node[path[-1]] = None
    return out


def write_package(data: dict, path: Path) -> int:
    """data をパッケージにして path へ書く（同じフォルダの一時ファイルに書いてから置き換える）。書いた大きさを返す。"""
    blobs: list[dict[str, Any]] = []
    payloads: list[bytes] = []
    for bpath in BYTES_PATHS:
        value = _get(data, bpath)
        if isinstance(value, str):
            blobs.append({"path": list(bpath), "type": "bytes"})
            payloads.append(base64.b64decode(value))
    for key in LIST_KEYS:
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for start in range(0, max(len(items), 1), JSON_CHUNK_ITEMS):
            blobs.append({"path": [key], "type": "json"})
            payloads.append(json.dumps(items[start : start + JSON_CHUNK_ITEMS], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    base = _copy_without(data)
    # ヘッダーの長さで塊の位置が変わるので、ヘッダーの長さが変わらなくなるまで組み直す
    # （位置は増える一方なので、桁が増えなくなった時点で収まる）。
    header_len = 0
    for _ in range(10):
        offset = _pad(12 + header_len)
        for blob, payload in zip(blobs, payloads):
            blob["offset"], blob["length"] = offset, len(payload)
            offset = _pad(offset + len(payload))
        header = json.dumps({"data": base, "blobs": blobs}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(header) == header_len:
            break
        header_len = len(header)
    else:  # pragma: no cover - 桁が増えるのは高々数回
        raise PackageError("could not lay out the package header")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with tmp.open("wb") as f:
            f.write(MAGIC + struct.pack("<II", VERSION, len(header)) + header)
            pos = 12 + len(header)
            for blob, payload in zip(blobs, payloads):
                f.write(b"\0" * (blob["offset"] - pos))
                f.write(payload)
                pos = blob["offset"] + len(payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path.stat().st_size


def _pad(n: int) -> int:
    return (n + _ALIGN - 1) // _ALIGN * _ALIGN


def read_package(path: Path) -> dict:
    """パッケージを元のビューア用データ（base64 の配列を含む dict）に戻す（テスト・確認用）。"""
    raw = Path(path).read_bytes()
    if len(raw) < 12 or raw[:4] != MAGIC:
        raise PackageError("not a 3D viewer package")
    version, header_len = struct.unpack_from("<II", raw, 4)
    if version != VERSION:
        raise PackageError(f"unsupported package version {version}")
    header = json.loads(raw[12 : 12 + header_len].decode("utf-8"))
    data = header["data"]
    for blob in header["blobs"]:
        chunk = raw[blob["offset"] : blob["offset"] + blob["length"]]
        keys = blob["path"]
        node = data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        if blob["type"] == "bytes":
            node[keys[-1]] = base64.b64encode(chunk).decode("ascii")
        else:
            node.setdefault(keys[-1], []).extend(json.loads(chunk.decode("utf-8")))
    return data
