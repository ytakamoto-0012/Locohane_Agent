"""比較モデル（show_cad_viewer の compare_models）を読み、① 基準モデルへの位置合わせと偏差をビューア用データに付ける。

① はビューアに出すモデル（file_path・code）で、② ③ ④（最大 MAX_COMPARISONS 個）はそれと比べるモデル。偏差は
② ③ ④ それぞれの各頂点から ① の表面までの符号付き距離（① の外向きの法線の側 = 肉が多いが正）で、② ③ ④ の側に塗る。
位置合わせも ② ③ ④ を ① に合わせる（① の座標は動かさないので、選択・計測の座標は変わらない）。

サブプロセス（src/cad/run_cad.py）で動く。比較モデルが STL・ビューア用データなら numpy だけで読み、CAD（STEP 等）なら
cadquery で三角形に分ける。① が CAD（元のファイル、CadQuery のコードなら隣の .brep）なら、偏差の誤差を抑えるため
① も表示用より細かく分けた三角形（[cad_viewer].reference_deflection_ratio）を偏差の表面に使い、baseSurface に残す。

ビューア用データに足すもの（src/cad/mesh_data.py の docstring）::

    "comparisons": [
        {"positions": <float32 base64>, "indices": <uint32 base64>, "source": {"format", "path"},
         "triangles": 数, "bbox": {...}, "chordTolerance": 三角形の弦の許容差（CAD のとき）,
         "deviation": {"values": <float32 base64、このモデルの頂点ごとの符号付き距離>, "transform": 4×4（このモデル → ①）,
                       "alignment": {"method", "rms", ...}, "stats": {...}, "tolerance": 公差}}
        | {"source": {"path"}, "error": "読めなかった理由"},
        ...  # ② ③ ④ の順
    ],
    "baseSurface": {"positions", "indices", "chordTolerance"}  # ① を細かく分けた三角形（① が CAD のときだけ。ブラウザへは送らない）
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


# 一度に比べられるモデルの数（② ③ ④）。
MAX_COMPARISONS = 3
# 細かく三角形に分け直せる ① の形式。
_CAD_FORMATS = ("STEP", "IGES", "BREP", "BIN")
# ② ③ ④ の印（LLM へ返す概要・注意で使う）。
MARKS = ("②", "③", "④")


def _base_cad_file(data: dict[str, Any], data_path: str | Path | None) -> Path | None:
    """① を細かく分け直すための CAD ファイル（元のファイル。CadQuery のコードならビューア用データの隣の .brep）。"""
    src = data.get("source") if isinstance(data.get("source"), dict) else {}
    path = src.get("path")
    if src.get("kind") == "file" and isinstance(path, str) and path:
        p = Path(path)
        if p.is_file() and format_of(p) in _CAD_FORMATS:
            return p
    if data_path is not None:
        brep = Path(data_path).with_suffix(".brep")
        if brep.is_file():
            return brep
    return None


def base_surface(data: dict[str, Any], data_path: str | Path | None, raw: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, float | None]:
    """偏差を測る ① の表面の三角形（CAD なら細かく分け直したもの、それ以外は表示用の mesh）と弦の許容差。"""
    cad = _base_cad_file(data, data_path)
    if cad is not None:
        try:
            ref = load_reference(
                cad,
                deflection_ratio=float(raw.get("reference_deflection_ratio") or 0.0002),
                angular_deg=min(float(raw.get("angular_deflection_deg") or 20.0), 10.0),
                max_triangles=int(raw.get("max_triangles", 1_000_000)),
            )
            return ref["positions"], ref["indices"], ref["chordTolerance"]
        except Exception:  # noqa: BLE001 - 分け直せなければ表示用の三角形で測る（OCP の C++ 例外は別の型で来る）
            pass
    pos, idx = mesh_arrays(data)
    return pos, idx, None


def base_index(data: dict[str, Any]) -> SurfaceIndex:
    """ビューア用データの ① の表面の索引（baseSurface があればそれ、無ければ表示用の mesh）。"""
    surf = data.get("baseSurface")
    if isinstance(surf, dict) and surf.get("positions") and surf.get("indices"):
        pos, idx = mesh_arrays({"mesh": surf})
    else:
        pos, idx = mesh_arrays(data)
    return SurfaceIndex(pos, idx)


def comparison_mesh(entry: dict[str, Any]) -> TargetMesh:
    """比較モデル（comparisons の 1 つ）の三角形（位置を合わせる側・偏差を塗る側）。"""
    pos, idx = mesh_arrays({"mesh": entry})
    return TargetMesh(pos, idx, [])


def _comparison(path: str, index: SurfaceIndex, raw: dict[str, Any], method: str, tolerance: float) -> dict[str, Any]:
    ref = load_reference(
        path,
        deflection_ratio=float(raw.get("reference_deflection_ratio") or 0.0002),
        angular_deg=min(float(raw.get("angular_deflection_deg") or 20.0), 10.0),
        max_triangles=int(raw.get("max_triangles", 1_000_000)),
    )
    target = TargetMesh(ref["positions"], ref["indices"], [])
    T, info = align(target, index, method if method in ("none", "bestfit") else "none")
    values = deviations(target, index, T)
    pos = ref["positions"].astype(np.float32)
    out: dict[str, Any] = {
        "positions": b64(pos, "f4"),
        "indices": b64(ref["indices"].astype(np.uint32), "u4"),
        "source": ref["source"],
        "triangles": int(len(ref["indices"])),
        "bbox": bbox_of(pos),
    }
    if ref["chordTolerance"] is not None:
        out["chordTolerance"] = rnd(ref["chordTolerance"], 6)
    out["deviation"] = {
        "values": b64(values, "f4"),
        "transform": describe_transform(T)["matrix"],
        "alignment": info,
        "tolerance": rnd(tolerance, 6),
        "stats": deviation_stats(values, target.vertex_weights(), tolerance),
    }
    return out


def _error_text(e: BaseException) -> str:
    if isinstance(e, ImportError):
        return f"cadquery is not installed ({e}), so a CAD model cannot be read for the comparison (an STL works)."
    if isinstance(e, (ReferenceModelError, AlignError, TooManyTrianglesError, OSError)):
        return str(e)
    return f"could not compare with the base model ({type(e).__name__}: {e})"


def attach_comparisons(data: dict[str, Any], raw: dict[str, Any], data_path: str | Path | None = None) -> None:
    """options の compare_models（② ③ ④）があれば、それぞれを ① に合わせた位置と偏差を data["comparisons"] に付ける。

    比較モデルが読めなくても形の表示はできるので、失敗はそのモデルの {"source", "error"} にして続ける（② ③ ④ の番号は保つ）。
    """
    paths = [str(p) for p in (raw.get("compare_models") or []) if p][:MAX_COMPARISONS]
    # 前の版の基準モデル（① の偏差）は向きが逆なので使わない。
    data.pop("reference", None)
    data.pop("deviation", None)
    data.pop("comparisons", None)
    data.pop("baseSurface", None)
    if not paths:
        return
    method = str(raw.get("align") or "none")
    tolerance = float(raw.get("deviation_tolerance") or 0.1)
    try:
        pos, idx, chord = base_surface(data, data_path, raw)
        index = SurfaceIndex(pos, idx)
    except Exception as e:  # noqa: BLE001 - ① の三角形が無い等。どの比較もできない
        msg = "the base model ① has no faces (triangles) to measure the deviation against" if isinstance(e, AlignError) else _error_text(e)
        data["comparisons"] = [{"source": {"path": p}, "error": msg} for p in paths]
        return
    if chord is not None:
        data["baseSurface"] = {
            "positions": b64(pos.astype(np.float32), "f4"),
            "indices": b64(idx.astype(np.uint32), "u4"),
            "chordTolerance": rnd(chord, 6),
        }
    out: list[dict[str, Any]] = []
    for p in paths:
        try:
            out.append(_comparison(p, index, raw, method, tolerance))
        except Exception as e:  # noqa: BLE001 - OCP の C++ 例外は Python の別の型で来る
            out.append({"source": {"path": p}, "error": _error_text(e)})
    data["comparisons"] = out
