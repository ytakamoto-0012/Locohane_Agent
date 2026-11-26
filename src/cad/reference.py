"""基準モデル（show_cad_viewer の reference）を読み、位置合わせと偏差をビューア用データに付ける。

サブプロセス（src/cad/run_cad.py）で動く。基準が STL・ビューア用データなら numpy だけで読み、CAD（STEP 等）なら
cadquery で三角形に分ける（偏差の誤差を抑えるため、表示用より細かく。[cad_viewer].reference_deflection_ratio）。

ビューア用データに足すもの（src/cad/mesh_data.py の docstring）::

    "reference": {"positions": <float32 base64>, "indices": <uint32 base64>, "source": {"format", "path"},
                  "triangles": 数, "bbox": {...}, "chordTolerance": 三角形の弦の許容差（CAD のとき）},
    "deviation": {"values": <float32 base64、対象の頂点ごとの符号付き距離>, "transform": 4×4,
                  "alignment": {"method", "rms", ...}, "stats": {...}, "tolerance": 公差}
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .align import AlignError, SurfaceIndex, TargetMesh, align, deviation_stats, deviations, describe_transform
from .formats import format_of
from .mesh_data import FORMAT as VIEWER_FORMAT
from .mesh_data import TooManyTrianglesError, b64, bbox_of, rnd


class ReferenceModelError(ValueError):
    """基準モデルが読めない。"""


def _decode(value: str, dtype: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(value), dtype=np.dtype(dtype).newbyteorder("<"))


def mesh_arrays(data: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """ビューア用データの mesh の (頂点 (n,3) float64, 三角形 (t,3) int64)。"""
    mesh = data.get("mesh") or {}
    if not mesh.get("positions") or not mesh.get("indices"):
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    pos = _decode(mesh["positions"], "f4").reshape(-1, 3).astype(np.float64)
    idx = _decode(mesh["indices"], "u4").reshape(-1, 3).astype(np.int64)
    return pos, idx


def target_mesh(data: dict[str, Any]) -> TargetMesh:
    pos, idx = mesh_arrays(data)
    return TargetMesh(pos, idx, (data.get("mesh") or {}).get("faceRanges") or [])


def load_reference(path: str | Path, *, deflection_ratio: float, angular_deg: float, max_triangles: int) -> dict[str, Any]:
    """基準モデルを三角形にする。戻り値は positions (n,3)・indices (t,3)・source・chordTolerance。"""
    path = Path(path)
    fmt = format_of(path)
    if fmt is None:
        raise ReferenceModelError(f"unsupported file type {path.suffix!r} for the reference model")
    chord: float | None = None
    if fmt == "STL":
        from .stl_reader import read_stl

        stl = read_stl(path)
        if max_triangles > 0 and len(stl.triangles) > max_triangles:
            raise TooManyTrianglesError(
                f"The reference STL has {len(stl.triangles)} triangles, more than [cad_viewer] max_triangles ({max_triangles})."
            )
        pos = stl.triangles.reshape(-1, 3).astype(np.float64)
        idx = np.arange(len(pos), dtype=np.int64).reshape(-1, 3)
    elif fmt == "VIEWER_JSON":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ReferenceModelError(f"could not read the viewer data {path}: {e}") from e
        if not isinstance(data, dict) or data.get("format") != VIEWER_FORMAT:
            raise ReferenceModelError(f"{path} is not viewer data made by show_cad_viewer")
        pos, idx = mesh_arrays(data)
        src = data.get("source") if isinstance(data.get("source"), dict) else {}
        fmt = f"VIEWER_JSON ({src.get('format', '?')})"
    elif fmt == "DXF":
        raise ReferenceModelError("a DXF (2D) file cannot be a reference model: give a 3D CAD file or an STL")
    else:
        pos, idx, chord = _tessellate_cad(path, fmt, deflection_ratio, angular_deg, max_triangles)
    if not len(idx):
        raise ReferenceModelError("the reference model has no faces (triangles) to measure deviation against")
    return {"positions": pos, "indices": idx, "source": {"format": fmt, "path": str(path)}, "chordTolerance": chord}


def _tessellate_cad(path: Path, fmt: str, ratio: float, angular_deg: float, max_triangles: int) -> tuple[np.ndarray, np.ndarray, float]:
    """CAD ファイルを細かく三角形に分ける（三角形が上限を超えたら粗くする。3 回まで）。"""
    import cadquery as cq
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_FACE

    from .export_mesh import _diag, _index_map, _triangulate_face, load_cad_file

    parts, _units = load_cad_file(path, fmt)
    compound = cq.Compound.makeCompound([cq.Shape.cast(p.shape) for p in parts]).wrapped
    face_map = _index_map(compound, TopAbs_FACE)
    diag = _diag(compound, exact=face_map.Extent() <= 2000)
    lin = diag * ratio
    ang = math.radians(angular_deg)
    for _attempt in range(4):
        BRepMesh_IncrementalMesh(compound, lin, False, ang, True)
        meshes = [_triangulate_face(face_map.FindKey(i)) for i in range(1, face_map.Extent() + 1)]
        total = sum(len(t) for _, t in meshes)
        if max_triangles <= 0 or total <= max_triangles:
            break
        lin *= 3
        BRepTools.Clean_s(compound)
    else:
        raise TooManyTrianglesError(
            f"The reference model needs {total} triangles even with a coarse tessellation, more than [cad_viewer] max_triangles ({max_triangles})."
        )
    pos_list, idx_list, offset = [], [], 0
    for nodes, tris in meshes:
        if len(tris):
            pos_list.append(nodes)
            idx_list.append(tris + offset)
            offset += len(nodes)
    if not pos_list:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), lin
    return np.concatenate(pos_list), np.concatenate(idx_list), lin


def attach_reference(data: dict[str, Any], raw: dict[str, Any]) -> None:
    """options の reference があれば、基準モデルと（align に応じて位置を合わせた）偏差を data に付ける。

    基準が読めなくても形の表示はできるので、失敗は data["deviation"] = {"error": ...} にして続ける。
    """
    ref_path = raw.get("reference")
    if not ref_path:
        return
    method = str(raw.get("align") or "none")
    tolerance = float(raw.get("deviation_tolerance") or 0.1)
    try:
        ref = load_reference(
            ref_path,
            deflection_ratio=float(raw.get("reference_deflection_ratio") or 0.0002),
            angular_deg=min(float(raw.get("angular_deflection_deg") or 20.0), 10.0),
            max_triangles=int(raw.get("max_triangles", 1_000_000)),
        )
        index = SurfaceIndex(ref["positions"], ref["indices"])
        target = target_mesh(data)
        if not len(target.triangles):
            raise ReferenceModelError("the model has no faces (triangles) to compare with the reference")
        T, info = align(target, index, method if method in ("none", "bestfit") else "none")
        values = deviations(target, index, T)
    except ImportError as e:
        data["deviation"] = {"error": f"cadquery is not installed ({e}), so a CAD reference model cannot be read (an STL reference works)."}
        return
    except (ReferenceModelError, AlignError, TooManyTrianglesError, OSError) as e:
        data["deviation"] = {"error": str(e)}
        return
    except Exception as e:  # noqa: BLE001 - OCP の C++ 例外は Python の別の型で来る
        data["deviation"] = {"error": f"could not compare with the reference model ({type(e).__name__}: {e})"}
        return
    pos = ref["positions"].astype(np.float32)
    data["reference"] = {
        "positions": b64(pos, "f4"),
        "indices": b64(ref["indices"].astype(np.uint32), "u4"),
        "source": ref["source"],
        "triangles": int(len(ref["indices"])),
        "bbox": bbox_of(pos),
    }
    if ref["chordTolerance"] is not None:
        data["reference"]["chordTolerance"] = rnd(ref["chordTolerance"], 6)
    data["deviation"] = {
        "values": b64(values, "f4"),
        "transform": describe_transform(T)["matrix"],
        "alignment": info,
        "tolerance": rnd(tolerance, 6),
        "stats": deviation_stats(values, target.vertex_weights(), tolerance),
    }
