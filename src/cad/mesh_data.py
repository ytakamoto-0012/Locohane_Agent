"""ビューア用データ（JSON）の組み立て。numpy だけで動く（cadquery 不要）。

データの形（``FORMAT``。frontend/src/components/cad/cadData.ts と一致させる）:

```
{
  "format": "locohane-cad-viewer/1",
  "source": {"kind": "file" | "code", "format": "STEP" | ..., "path": "..."},
  "units": "mm" | "unknown",
  "bbox": {"min": [x,y,z], "max": [x,y,z], "size": [x,y,z]},
  "mesh":     {"positions": <float32 base64>, "indices": <uint32 base64>,
               "colors": <uint8 RGB base64> | null, "faceRanges": [[先頭の三角形, 三角形数], ...]},
  "edges":    {"positions": <float32 base64、線分ごとに 2 点>, "ranges": [[先頭の線分, 線分数], ...]},
  "vertices": {"positions": <float32 base64>},
  "faces": [面の情報, ...], "edgeList": [エッジの情報, ...], "vertexList": [頂点の情報, ...],
  "stats": {...},
  // 開いた形の面の中の穴とローカル AXIS（src/cad/topology.py。見つかったときだけ）。
  "holes": {"positions": <float32 base64、線分ごとに 2 点>, "ranges": [[先頭の線分, 線分数], ...], "tolerance": ...},
  "holeList": [穴の情報, ...], "outers": {"positions", "ranges"}, "outerList": [外周の情報, ...],
  "localAxes": [{"origin", "x", "y", "z", ...}, ...],
  // ゼブラ・曲率の表示に使う頂点ごとの法線と主曲率（src/cad/analysis.py。[cad_viewer] surface_analysis）。
  "analysis": {"normals": <int16 base64>, "k1": <float32 base64>, "k2": <float32 base64>, "method", "summary"},
  // 比較モデル ② ③ ④ と、その頂点ごとの ① からの偏差（show_cad_viewer の compare_models。src/cad/reference.py）。
  "comparisons": [{"positions", "indices", "source", "triangles", "bbox", "chordTolerance",
                   "deviation": {"values": <float32 base64>, "transform": 4×4（そのモデル → ①）, "alignment", "tolerance", "stats"}}
                  | {"source", "error"}, ...],
  "baseSurface": {"positions", "indices", "chordTolerance"}  // 偏差を測る ① の細かい三角形（① が CAD のとき。ブラウザへは送らない）
}
```

面 i の三角形は mesh の三角形 faceRanges[i] の範囲に並び、エッジ j の線分は edges の ranges[j] の
範囲に並ぶ（ビューアは範囲で描き分け・当たり判定をする）。各情報の "id" は配列の位置と同じ。
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .stl_reader import group_planar_regions, read_stl

FORMAT = "locohane-cad-viewer/1"


class TooManyTrianglesError(ValueError):
    """三角形の数が [cad_viewer] max_triangles を超えた。"""


def b64(arr: np.ndarray, dtype: str) -> str:
    """配列を指定の型のリトルエンディアンのバイト列にして base64 にする。"""
    return base64.b64encode(np.ascontiguousarray(arr, dtype=np.dtype(dtype).newbyteorder("<")).tobytes()).decode("ascii")


def rnd(value: float, digits: int = 6) -> float:
    """情報に載せる数値を丸める（LLM へ返す文字数を抑える。-0.0 は 0.0 にする）。"""
    r = round(float(value), digits)
    return 0.0 if r == 0 else r


def vec(values: Sequence[float], digits: int = 6) -> list[float]:
    return [rnd(v, digits) for v in values]


def hex_color(rgb: Sequence[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(c) for c in rgb))


def bbox_of(points: np.ndarray) -> dict[str, list[float]]:
    lo, hi = points.min(axis=0), points.max(axis=0)
    return {"min": vec(lo), "max": vec(hi), "size": vec(hi - lo)}


def build_viewer_data(
    *,
    source: dict[str, Any],
    units: str,
    positions: np.ndarray,
    indices: np.ndarray,
    face_ranges: list[list[int]],
    colors: np.ndarray | None,
    edge_positions: np.ndarray,
    edge_ranges: list[list[int]],
    vertex_positions: np.ndarray,
    faces: list[dict],
    edges: list[dict],
    vertices: list[dict],
) -> dict[str, Any]:
    """各配列をまとめてビューア用データにする。"""
    all_points = [p for p in (positions, edge_positions, vertex_positions) if len(p)]
    bbox = bbox_of(np.concatenate(all_points)) if all_points else {"min": [0, 0, 0], "max": [0, 0, 0], "size": [0, 0, 0]}
    return {
        "format": FORMAT,
        "source": source,
        "units": units,
        "bbox": bbox,
        "mesh": {
            "positions": b64(positions, "f4"),
            "indices": b64(indices, "u4"),
            "colors": b64(colors, "u1") if colors is not None else None,
            "faceRanges": face_ranges,
        },
        "edges": {"positions": b64(edge_positions, "f4"), "ranges": edge_ranges},
        "vertices": {"positions": b64(vertex_positions, "f4")},
        "faces": faces,
        "edgeList": edges,
        "vertexList": vertices,
        "stats": {
            "faces": len(faces),
            "edges": len(edges),
            "vertices": len(vertices),
            "triangles": int(len(indices)),
        },
    }


def stl_to_viewer_data(path: str | Path, *, max_triangles: int, region_angle_deg: float = 1.0, analysis: bool = True) -> dict[str, Any]:
    """STL を読み、平面領域を「面」、領域の境目を「エッジ」にしたビューア用データを返す。

    STL は B-rep（面・エッジ・頂点の位相）を持たないため、頂点は選べない。単位も持たないので
    units は "unknown"。analysis が真なら、ゼブラ・曲率の表示に使う法線と主曲率（src/cad/analysis.py）も付ける。

    Raises:
        StlError: STL として読めない場合。
        TooManyTrianglesError: 三角形の数が max_triangles を超えた場合。
    """
    stl = read_stl(path)
    n = len(stl.triangles)
    if max_triangles > 0 and n > max_triangles:
        raise TooManyTrianglesError(f"The STL file has {n} triangles, more than [cad_viewer] max_triangles ({max_triangles}).")
    groups = group_planar_regions(stl.triangles, stl.colors, region_angle_deg)

    # 三角形を領域の順に並べ替える（面ごとに連続した範囲で描くため）。
    order = np.argsort(groups.region, kind="stable")
    tris = stl.triangles[order]
    region = groups.region[order]
    normals = groups.normals[order]
    areas = groups.areas[order]
    region_count = int(region.max()) + 1 if n else 0
    counts = np.bincount(region, minlength=region_count)
    starts = np.r_[0, np.cumsum(counts)[:-1]]

    positions = tris.reshape(-1, 3).astype(np.float32)
    indices = np.arange(n * 3, dtype=np.uint32).reshape(n, 3)
    colors = None
    tri_colors = stl.colors[order] if stl.colors is not None else None
    tri_colored = stl.colored[order] if stl.colored is not None else None
    if tri_colors is not None:
        colors = np.repeat(tri_colors, 3, axis=0)

    faces: list[dict] = []
    centroids = tris.mean(axis=1)
    for r in range(region_count):
        s, c = int(starts[r]), int(counts[r])
        a = areas[s : s + c]
        total = float(a.sum())
        weights = a / total if total > 0 else np.full(c, 1.0 / c)
        center = (centroids[s : s + c] * weights[:, None]).sum(axis=0)
        normal = (normals[s : s + c] * a[:, None]).sum(axis=0)
        norm = float(np.linalg.norm(normal))
        normal = normal / norm if norm > 0 else normal
        info: dict[str, Any] = {
            "id": r,
            "kind": "face",
            "geomType": "PLANE",
            "area": rnd(total),
            "center": vec(center),
            "normal": vec(normal),
            "planeOffset": rnd(float(normal @ center)),
            "triangles": c,
            "bbox": bbox_of(tris[s : s + c].reshape(-1, 3)),
        }
        if tri_colors is not None:
            info["color"] = hex_color(tri_colors[s]) if tri_colored is None or bool(tri_colored[s]) else None
        faces.append(info)
    if faces:
        # 各軸で中心が最も大きい・小さい面に、CadQuery のセレクタ風の目印を添える。
        _add_extreme_hints(faces)

    edge_segments: list[np.ndarray] = []
    edge_ranges: list[list[int]] = []
    edges: list[dict] = []
    seg_total = 0
    for (ra, rb), pairs in sorted(groups.boundaries.items()):
        pts = groups.vertices[np.array(pairs)]  # (k, 2, 3)
        lengths = np.linalg.norm(pts[:, 1] - pts[:, 0], axis=1)
        edge_segments.append(pts.reshape(-1, 3))
        edge_ranges.append([seg_total, len(pairs)])
        seg_total += len(pairs)
        edges.append(
            {
                "id": len(edges),
                "kind": "edge",
                "geomType": "MESH_BOUNDARY",
                "length": rnd(float(lengths.sum())),
                "segments": len(pairs),
                "faces": [ra] if rb < 0 else [ra, rb],
                "openBoundary": rb < 0,
                "bbox": bbox_of(pts.reshape(-1, 3)),
            }
        )
    edge_positions = np.concatenate(edge_segments).astype(np.float32) if edge_segments else np.zeros((0, 3), np.float32)

    source = {"kind": "file", "format": "STL", "path": str(path), "binary": stl.binary}
    if stl.color_format:
        source["colorFormat"] = stl.color_format
    face_ranges = [[int(s), int(c)] for s, c in zip(starts, counts)]
    # 閉じた形の穴（丸・四角・角 R の四角・正多角形。src/cad/profile_holes.py）。壁の面に feature が付く。
    from .profile_holes import recognize_profile_holes

    features = recognize_profile_holes(positions, indices, face_ranges, faces, [])
    data = build_viewer_data(
        source=source,
        units="unknown",
        positions=positions,
        indices=indices,
        face_ranges=face_ranges,
        colors=colors,
        edge_positions=edge_positions,
        edge_ranges=edge_ranges,
        vertex_positions=np.zeros((0, 3), np.float32),
        faces=faces,
        edges=edges,
        vertices=[],
    )
    if features:
        data["features"] = features
    if analysis:
        from .analysis import stl_analysis

        result = stl_analysis(positions, indices)
        if result:
            data["analysis"] = result
    return data


_AXES = ("X", "Y", "Z")


def _add_extreme_hints(faces: list[dict]) -> None:
    """各軸で中心が最大・最小の面がただ 1 つなら、その面に ">Z" 等の印を付ける（STL 用）。

    B-rep では CadQuery のセレクタを実際に当てて求める（export_mesh.py）。STL には CadQuery の
    形が無いため、同じ意味の目印だけを中心座標から付ける（"meshHints"。セレクタとしては使えない）。
    """
    centers = np.array([f["center"] for f in faces])
    for axis, name in enumerate(_AXES):
        col = centers[:, axis]
        tol = max(1e-9, float(col.max() - col.min()) * 1e-6)
        for sign, pick in ((">", col.max()), ("<", col.min())):
            hits = np.flatnonzero(np.abs(col - pick) <= tol)
            if len(hits) == 1:
                faces[int(hits[0])].setdefault("meshHints", []).append(f"{sign}{name}")


def apply_view_edits(
    data: dict[str, Any],
    *,
    face_colors: dict[int, str | None],
    part_colors: dict[int, str | None],
    hidden_parts: Sequence[int] = (),
    visible: dict[str, bool] | None = None,
    color_changes: Sequence[dict[str, Any]] = (),
    saved_at: str = "",
    history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ビューアで変えた色・表示を適用したビューア用データ（元の data は変えない）。

    色は面の情報（faces[i].color・parts[i].color）と三角形の頂点色（mesh.colors）の両方に入れるので、
    書き出した JSON を show_cad_viewer の file_path に渡すと、変えた色のまま表示される。部品の色は
    その部品の面にも付ける（面ごとに変えた色が優先）。色が None は色を外す（ビューアは色の無い面を
    テーマの既定色で描くので、頂点色はそのままでよい）。隠した部品と種類ごとの表示は "viewState" に、
    変更の一覧は "edits" に残す。history はビューアの「元に戻す」「やり直す」の履歴（viewState.history。
    開き直したビューアが続きから戻せるように。frontend の editHistory.ts）。
    """
    out = json.loads(json.dumps(data))
    parts = out.get("parts") or []
    for pi, color in part_colors.items():
        if 0 <= pi < len(parts):
            parts[pi]["color"] = color
    faces = out.get("faces") or []
    new_face_colors: dict[int, str] = {}
    for fi, face in enumerate(faces):
        if fi in face_colors:
            color = face_colors[fi]
        elif face.get("part") is not None and int(face["part"]) in part_colors:
            color = part_colors[int(face["part"])]
        else:
            continue
        face["color"] = color
        if color is not None:
            new_face_colors[fi] = color
    mesh = out.get("mesh") or {}
    if new_face_colors and mesh.get("positions") and mesh.get("indices"):
        n_vertices = len(base64.b64decode(mesh["positions"])) // 12
        indices = np.frombuffer(base64.b64decode(mesh["indices"]), dtype="<u4")
        if mesh.get("colors"):
            colors = np.frombuffer(base64.b64decode(mesh["colors"]), dtype=np.uint8).reshape(-1, 3).copy()
        else:
            colors = np.full((n_vertices, 3), 180, dtype=np.uint8)
        for fi, color in new_face_colors.items():
            start, count = mesh["faceRanges"][fi]
            if not count:
                continue
            verts = indices[start * 3 : (start + count) * 3]
            colors[int(verts.min()) : int(verts.max()) + 1] = [int(color[k : k + 2], 16) for k in (1, 3, 5)]
        mesh["colors"] = b64(colors, "u1")
    out["viewState"] = {"hiddenParts": sorted({int(p) for p in hidden_parts}), "visible": dict(visible or {})}
    if history:
        out["viewState"]["history"] = history
    previous = (data.get("edits") or {}).get("color_changes") or []
    face_parts: dict[int, int] = {}
    for fi, face in enumerate(faces):
        try:
            face_parts[fi] = int(face["part"])
        except (KeyError, TypeError, ValueError):
            continue
    out["edits"] = {"saved_at": saved_at, "color_changes": merge_color_changes(previous, color_changes, face_parts)}
    return out


def merge_color_changes(
    previous: Sequence[dict[str, Any]],
    current: Sequence[dict[str, Any]],
    face_parts: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """前回までに保存した色の変更（開き直したビューア用データの edits.color_changes）に、今回の変更を重ねる。

    同じ面・部品は、最初の from（元のファイルの色）と最後の to を残し、元の色に戻ったものは外す。開き直した
    データを書き出すとき、前の回に変えた色も変更として扱われるように（step_xcaf.export_step_xcaf 等は
    edits.color_changes を見て色を付ける）。face_parts（面の番号 → 部品の番号）を渡すと、今回色を変えた
    部品の面について前の回の面の色の変更を捨てる（ビューアはその面を新しい部品の色で描くので。今回その面の
    色も変えていれば今回の変更が残る）。
    """
    current = [c for c in current if isinstance(c, dict)]
    recolored_parts: set[int] = set()
    current_faces: set[int] = set()
    for c in current:
        try:
            if c.get("kind") == "part":
                recolored_parts.add(int(c["id"]))
            elif c.get("kind") == "face":
                current_faces.add(int(c["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    if face_parts and recolored_parts:
        kept = []
        for c in previous:
            try:
                fid = int(c["id"]) if isinstance(c, dict) and c.get("kind") == "face" else None
            except (KeyError, TypeError, ValueError):
                fid = None
            if fid is not None and fid not in current_faces and face_parts.get(fid) in recolored_parts:
                continue
            kept.append(c)
        previous = kept
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    for c in [*previous, *current]:
        if not isinstance(c, dict) or c.get("kind") not in ("face", "part"):
            continue
        try:
            key = (str(c["kind"]), int(c["id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if key in merged:
            merged[key] = {**merged[key], "to": c.get("to")}
        else:
            merged[key] = dict(c)
    return [c for c in merged.values() if (c.get("from") or "").lower() != (c.get("to") or "").lower()]


def write_viewer_json(data: dict[str, Any], out_path: str | Path) -> None:
    """ビューア用データを JSON で書く（数値の NaN・無限大は null にする）。"""
    Path(out_path).write_text(json.dumps(_finite(data), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value
