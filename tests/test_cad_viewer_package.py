"""ブラウザへ送るビューア用データのパッケージ（src/cad/viewer_package.py）のテスト。"""

from __future__ import annotations

import base64
import json
import struct

import numpy as np
import pytest

from src.cad import viewer_package
from src.cad.viewer_package import PackageError, read_package, write_package


def _b64(arr: np.ndarray) -> str:
    return base64.b64encode(arr.tobytes()).decode("ascii")


def _sample(n_faces: int = 5) -> dict:
    return {
        "format": "locohane-cad-viewer/1",
        "source": {"kind": "file", "format": "STL", "path": "C:/data/a.stl"},
        "units": "unknown",
        "mesh": {
            "positions": _b64(np.arange(9, dtype="<f4")),
            "indices": _b64(np.arange(3, dtype="<u4")),
            "colors": None,
            "faceRanges": [[0, 1]],
        },
        "edges": {"positions": _b64(np.arange(6, dtype="<f4")), "ranges": [[0, 1]]},
        "vertices": {"positions": ""},
        "faces": [{"id": i, "kind": "face", "name": "面"} for i in range(n_faces)],
        "edgeList": [{"id": 0, "kind": "edge"}],
        "vertexList": [],
        "diff": {"added": {"positions": _b64(np.ones(9, dtype="<f4")), "indices": _b64(np.arange(3, dtype="<u4"))}, "volume": 1.5},
        "stats": {"faces": n_faces, "triangles": 1},
    }


def test_round_trip_restores_the_viewer_data_and_splits_lists(tmp_path, monkeypatch):
    # 一覧は JSON_CHUNK_ITEMS 件ずつの塊に分かれ、読み戻すと元どおりにつながる。
    monkeypatch.setattr(viewer_package, "JSON_CHUNK_ITEMS", 2)
    data = _sample(5)
    original = json.loads(json.dumps(data))
    path = tmp_path / "a.lcvd"
    size = write_package(data, path)
    assert size == path.stat().st_size
    assert read_package(path) == original
    # 元の dict は書き換えない（LLM へ返す・保存に使う data のまま）。
    assert data == original

    raw = path.read_bytes()
    assert raw[:4] == b"LCVD"
    _, header_len = struct.unpack_from("<II", raw, 4)
    header = json.loads(raw[12 : 12 + header_len])
    # 大きな項目はヘッダーに残さない。
    assert "faces" not in header["data"] and header["data"]["mesh"]["positions"] is None
    face_chunks = [b for b in header["blobs"] if b["path"] == ["faces"]]
    assert len(face_chunks) == 3
    # バイト列は 8 の倍数の位置から始まる（ブラウザで Float32Array として直接見る）。
    for blob in header["blobs"]:
        assert blob["offset"] % 8 == 0 and blob["offset"] >= 12 + header_len
    mesh_blob = next(b for b in header["blobs"] if b["path"] == ["mesh", "positions"])
    values = np.frombuffer(raw, dtype="<f4", count=9, offset=mesh_blob["offset"])
    assert values.tolist() == list(range(9))
    # 無い色（null）はバイト列にせず null のまま。空の文字列（頂点の無い STL）は長さ 0 のバイト列になる。
    assert not any(b["path"] == ["mesh", "colors"] for b in header["blobs"])
    assert next(b for b in header["blobs"] if b["path"] == ["vertices", "positions"])["length"] == 0


def test_empty_lists_are_kept(tmp_path):
    data = _sample(0)
    path = tmp_path / "b.lcvd"
    write_package(data, path)
    back = read_package(path)
    assert back["faces"] == [] and back["vertexList"] == []


def test_large_offsets_keep_the_header_consistent(tmp_path, monkeypatch):
    # ヘッダーの長さが位置の桁に影響しても、位置とヘッダーが食い違わない。
    monkeypatch.setattr(viewer_package, "JSON_CHUNK_ITEMS", 1)
    data = _sample(3000)
    path = tmp_path / "c.lcvd"
    write_package(data, path)
    assert read_package(path)["faces"] == data["faces"]


def test_not_a_package_is_rejected(tmp_path):
    path = tmp_path / "x.lcvd"
    path.write_bytes(b"{}")
    with pytest.raises(PackageError):
        read_package(path)


def test_no_temporary_file_is_left(tmp_path):
    write_package(_sample(), tmp_path / "d.lcvd")
    assert [p.name for p in tmp_path.iterdir()] == ["d.lcvd"]


def test_comparisons_are_bytes_and_base_surface_is_not_sent(tmp_path) -> None:
    data = _sample()
    cmp = {
        "positions": _b64(np.arange(9, dtype="<f4") + 1),
        "indices": _b64(np.arange(3, dtype="<u4")),
        "source": {"format": "STL", "path": "C:/data/b.stl"},
        "deviation": {"values": _b64(np.array([0.1, 0.2, 0.3], dtype="<f4")), "transform": np.eye(4).tolist()},
    }
    data["comparisons"] = [cmp, {"source": {"path": "C:/data/c.stl"}, "error": "bad"}]
    data["baseSurface"] = {"positions": _b64(np.zeros(9, dtype="<f4")), "indices": _b64(np.arange(3, dtype="<u4"))}
    path = tmp_path / "p.lcvd"
    write_package(data, path)
    raw = path.read_bytes()
    header = json.loads(raw[12 : 12 + struct.unpack_from("<I", raw, 8)[0]].decode("utf-8"))
    paths = [b["path"] for b in header["blobs"] if b["type"] == "bytes"]
    assert ["comparisons", 0, "positions"] in paths and ["comparisons", 0, "deviation", "values"] in paths
    assert "baseSurface" not in header["data"] and header["data"]["comparisons"][1]["error"] == "bad"
    back = read_package(path)
    assert back["comparisons"][0]["positions"] == cmp["positions"]
    assert back["comparisons"][0]["deviation"]["values"] == cmp["deviation"]["values"]
    # 元のデータは書き換えない。
    assert data["comparisons"][0]["positions"] == cmp["positions"]
