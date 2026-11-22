"""前の版の形との比較（show_cad_viewer の compare_with）。cadquery が要る（サブプロセスで動く）。

agentcad の diff と同じく、閉じたソリッドどうしなら共通の体積・増えた体積（新 − 旧）・減った体積（旧 − 新）を
B-rep のブール演算で求め、あわせて穴・ボスの増減・ソリッドの数・大きさの変化を数値で返す。絵（ビューアの重ね表示）は
補助で、確かめるのは数値（AI CAD の先行事例で、画像での確認は B-rep の厳密な検査より人の判断と一致しにくかった）。

増えた部分・減った部分は三角形にしてビューア用データの ``diff.added`` / ``diff.removed`` に入れ、ビューアが
緑・赤の半透明で重ねて描く（LLM へ返す概要には入れない）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

import cadquery as cq

from .features import summarize_features
from .formats import format_of
from .mesh_data import FORMAT as VIEWER_FORMAT
from .mesh_data import b64, rnd

# 体積の差がこれ（大きい方の体積に対する割合）より小さければ、ブール演算の誤差とみなして 0 にする。
_VOLUME_NOISE_RATIO = 1e-7


LOAD_CHAIN_MAX = 10


class CompareError(ValueError):
    """比べる前の形が読めない・比べられない形式。"""


def load_previous(path: str | Path, _depth: int = 0) -> tuple[cq.Shape, str]:
    """比べる前の形（CAD ファイルか、show_cad_viewer のビューア用データ）を読む。戻り値は (形, 説明)。

    ビューア用データがほかのビューア用データを指すときはたどる（LOAD_CHAIN_MAX 回まで。自分を指す循環で止まらなくしない）。
    """
    from .export_mesh import load_cad_file

    if _depth >= LOAD_CHAIN_MAX:
        raise CompareError(f"the viewer data refers to other viewer data more than {LOAD_CHAIN_MAX} times")
    path = Path(path)
    fmt = format_of(path)
    if fmt == "VIEWER_JSON":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise CompareError(f"could not read the viewer data {path}: {e}") from e
        if not isinstance(data, dict) or data.get("format") != VIEWER_FORMAT:
            raise CompareError(f"{path} is not viewer data made by show_cad_viewer")
        src = data.get("source")
        if not isinstance(src, dict):
            src = {}
        if src.get("brep") and Path(src["brep"]).is_file():
            return cq.importers.importBrep(src["brep"]).val(), "previous CadQuery result"
        if src.get("path") and Path(src["path"]).is_file():
            return load_previous(src["path"], _depth + 1)
        raise CompareError("the viewer data does not point to a model file that still exists")
    if fmt in (None, "STL", "DXF"):
        raise CompareError(f"cannot compare with {path.suffix or path.name!r}: give a STEP/IGES/BREP/BIN file or viewer data of one")
    parts, _units = load_cad_file(path, fmt)
    shapes = [cq.Shape.cast(p.shape) for p in parts]
    return (shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes)), f"{fmt} {path.name}"


def compare(old: cq.Shape, new: cq.Shape, new_data: dict[str, Any], options: Any, max_triangles: int) -> dict[str, Any]:
    """前の版 old と今の形 new を比べた結果（ビューア用データの "diff"）。"""
    from .export_mesh import Options, Part, shapes_to_viewer_data

    opts = Options(
        linear_deflection_ratio=options.linear_deflection_ratio,
        angular_deflection_deg=options.angular_deflection_deg,
        max_triangles=options.max_triangles,
        selector_hints=False,
    )
    old_data = shapes_to_viewer_data([Part("previous", "previous", old.wrapped)], source={"kind": "compare"}, units="mm", options=opts)
    out: dict[str, Any] = {
        "ids_unchanged": old_data["modelDigest"] == new_data.get("modelDigest"),
        "counts": {
            k: {"before": old_data["stats"].get(k, 0), "after": new_data["stats"].get(k, 0)}
            for k in ("faces", "edges", "vertices")
        },
        "solids": {"before": old_data["check"]["solids"], "after": new_data["check"]["solids"]},
        "size": {"before": old_data["bbox"]["size"], "after": new_data["bbox"]["size"]},
        "features": _feature_changes(old_data.get("features") or [], new_data.get("features") or []),
    }
    warnings: list[str] = []
    if new_data["check"].get("valid") is False and old_data["check"].get("valid") is not False:
        warnings.append("The new shape is not a valid B-rep, but the previous one was.")
    if out["solids"]["after"] != out["solids"]["before"]:
        warnings.append(f"The number of solids changed from {out['solids']['before']} to {out['solids']['after']}.")

    v_old, v_new = old_data["check"].get("volume"), new_data["check"].get("volume")
    if v_old is None or v_new is None:
        out["volume_note"] = "Volumes were not compared: both shapes must be solids."
    else:
        vol = _volume_diff(old, new, float(v_old), float(v_new))
        out["volume"] = vol["summary"]
        if vol.get("error"):
            out["volume_note"] = vol["error"]
        meshes = _diff_meshes(vol, max_triangles, float(np.linalg.norm(new_data["bbox"]["size"]) or 1.0), options)
        out.update(meshes)
        if vol["summary"].get("removed", 0) == 0 and vol["summary"].get("added", 0) == 0 and not vol.get("error"):
            warnings.append("The new shape has exactly the same volume as the previous one: the edit may have had no effect.")
    if warnings:
        out["warnings"] = warnings
    return out


def _volume_diff(old: cq.Shape, new: cq.Shape, v_old: float, v_new: float) -> dict[str, Any]:
    noise = max(abs(v_old), abs(v_new), 1e-12) * _VOLUME_NOISE_RATIO
    summary: dict[str, Any] = {"before": rnd(v_old, 4), "after": rnd(v_new, 4), "change": rnd(v_new - v_old, 4)}
    res: dict[str, Any] = {"summary": summary}
    try:
        added = new.cut(old)
        removed = old.cut(new)
        common = old.intersect(new)
    except Exception as e:  # noqa: BLE001 - OCP の C++ 例外は Python の別の型で来る
        res["error"] = f"Boolean comparison failed ({type(e).__name__}); only the total volumes are given."
        return res

    def volume(s: cq.Shape) -> float:
        try:
            v = float(s.Volume())
        except Exception:  # noqa: BLE001
            return 0.0
        return 0.0 if abs(v) < noise else v

    va, vr, vc = volume(added), volume(removed), volume(common)
    summary.update(common=rnd(vc, 4), added=rnd(va, 4), removed=rnd(vr, 4))
    # 共通 + 増えた分 = 新、共通 + 減った分 = 旧、が成り立たなければブール演算の結果は信用しない。
    tol = max(abs(v_old), abs(v_new)) * 1e-4 + noise
    if abs(vc + va - v_new) > tol or abs(vc + vr - v_old) > tol:
        res["error"] = "The boolean comparison does not add up (common + added != after); the added/removed volumes may be wrong."
    res["added"] = added if va > 0 else None
    res["removed"] = removed if vr > 0 else None
    return res


def _diff_meshes(vol: dict[str, Any], max_triangles: int, diag: float, options: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    budget = max_triangles // 2 if max_triangles > 0 else 0
    for key in ("added", "removed"):
        shape = vol.get(key)
        if shape is None:
            continue
        try:
            verts, tris = shape.tessellate(diag * max(options.linear_deflection_ratio, 1e-4) * 2, math.radians(options.angular_deflection_deg))
        except Exception:  # noqa: BLE001
            continue
        if not tris or (budget and len(tris) > budget):
            continue
        pos = np.array([v.toTuple() for v in verts], dtype=np.float32).reshape(-1, 3)
        idx = np.array(tris, dtype=np.uint32).reshape(-1, 3)
        out[key] = {"positions": b64(pos, "f4"), "indices": b64(idx, "u4")}
    return out


def _feature_changes(before: list[dict], after: list[dict]) -> dict[str, Any]:
    """穴・ボスのグループごとの数の増減（変わったグループだけ）。"""
    b = {_group_key(g): g["count"] for g in summarize_features(before)["groups"]}
    a = {_group_key(g): g["count"] for g in summarize_features(after)["groups"]}
    changes = []
    for key in sorted(set(a) | set(b), key=str):
        if a.get(key, 0) != b.get(key, 0):
            kind, dia, size, through = key
            row: dict[str, Any] = {"type": kind, "diameter": dia, "depth" if kind == "hole" else "length": size}
            if kind == "hole":
                row["through"] = through
            row.update(before=b.get(key, 0), after=a.get(key, 0))
            changes.append(row)
    return {
        "holes": {"before": sum(1 for f in before if f["type"] == "hole"), "after": sum(1 for f in after if f["type"] == "hole")},
        "bosses": {"before": sum(1 for f in before if f["type"] == "boss"), "after": sum(1 for f in after if f["type"] == "boss")},
        "changed_groups": changes[:12],
    }


def _group_key(g: dict) -> tuple:
    return (g["type"], g["diameter"], g.get("depth", g.get("length")), g.get("through"))


def summary_of(diff: dict[str, Any]) -> dict[str, Any]:
    """LLM へ返す比較の概要（重ね表示用のメッシュを除く）。"""
    out = {k: v for k, v in diff.items() if k not in ("added", "removed")}
    out["overlay"] = "shown in the viewer: green = added material, red = removed material" if ("added" in diff or "removed" in diff) else "none"
    return out

