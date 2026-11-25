"""開いたサーフェース・メッシュの穴（境界のループ）と、ローカル座標系（ローカル AXIS）の検出。numpy だけで動く（cadquery 不要）。

どちらもビューア用データ（src/cad/mesh_data.py）から求めるので、B-rep（STEP・IGES・BREP・DXF・CadQuery コード）と
STL を同じ処理で扱う。run_cad.py が形をビューア用データにした後に ``attach_topology`` を呼ぶ。

穴（``holeList``・``holes``）:

1. 表示用の三角形の頂点を同じ座標で 1 つにまとめ（許容差の 1/1000 の刻み）、三角形 1 つにしか属さない辺を境界の辺とする。
   B-rep は面ごとに三角形を作るが、隣り合う面のエッジの点は同じ位置なので、つながった面の境目は境界にならない。
2. トポロジートレランス（既定 0.1mm）: 境界の辺のうち、平行に近い別の境界の辺から許容差以内に並ぶものは、隙間が許容差以下の「つながった」境目として除く（縫い合わせていない面どうし等）。
   許容差より細い切れ込み・小さい穴も同じ理由で消える。
3. 残った境界の辺をループにつなぐ（途切れた端どうしが許容差以内なら橋渡しする）。平均の幅（2 × 面積 ÷ 周長）が
   許容差以下のループは隙間として除く。
4. つながった面のまとまり（部分）ごとに、外接箱の最も大きいループを外周として除き、残りを穴とする
   （両端の開いた筒では片方の口が外周、もう片方が穴になる）。
5. 穴の法線はループの面積ベクトル（Newell の方法）を、穴の縁の三角形の向き（面の表側）に合わせたもの。

ローカル AXIS（``localAxes``）: 直線のエッジのうち長さが X・Y・Z の長さ（既定 100・200・300）に許容差以内で合う 3 本が、
1 つの端点を共有して互いに直交するもの。共有する端点を原点、そこから各線の先への向きを X・Y・Z とする
（直交からのずれは Gram-Schmidt で直した正規直交基底にする。左手系なら rightHanded: false）。
"""

from __future__ import annotations

import base64
import math
from typing import Any, Iterable, Sequence

import numpy as np

from .mesh_data import b64, bbox_of, rnd, vec

# 既定のトポロジートレランス（モデルの単位。mm）。
DEFAULT_HOLE_TOLERANCE = 0.1
# 既定のローカル AXIS の X・Y・Z の線の長さと、長さ・端点の位置の許容差、直交の角度の許容差（度）。
DEFAULT_AXIS_LENGTHS = (100.0, 200.0, 300.0)
DEFAULT_AXIS_LENGTH_TOLERANCE = 0.1
DEFAULT_AXIS_ANGLE_TOLERANCE_DEG = 0.5
# 見つけるローカル AXIS の数の上限。
LOCAL_AXES_MAX = 20
# 突き合わせで調べる、辺の上の点の位置（辺の長さに対する割合）。
_SAMPLES = (0.5, 0.1, 0.9, 0.3, 0.7)
# 突き合わせる辺どうしが平行とみなす向きの差。
_PARALLEL_COS = math.cos(math.radians(45))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def attach_topology(data: dict[str, Any], options: dict[str, Any] | None = None) -> None:
    """ビューア用データに穴（holeList・holes）とローカル AXIS（localAxes）を足す（data を書き換える）。

    見つからなければ何も足さない。失敗しても形の表示はできるので、例外は外へ出さず data["topologyError"] に残す。
    """
    options = options or {}
    try:
        tol = float(options.get("hole_tolerance", DEFAULT_HOLE_TOLERANCE))
        if tol > 0:
            holes = find_boundary_holes(data, tol)
            if holes is not None:
                items, geometry = holes
                data["holeList"] = items
                data["holes"] = geometry
                data.setdefault("stats", {})["holes"] = len(items)
        lengths = _axis_lengths(options.get("local_axis_lengths"))
        if lengths:
            axes = find_local_axes(
                data.get("edgeList") or [],
                lengths,
                float(options.get("local_axis_length_tolerance", DEFAULT_AXIS_LENGTH_TOLERANCE)),
                float(options.get("local_axis_angle_tolerance_deg", DEFAULT_AXIS_ANGLE_TOLERANCE_DEG)),
            )
            if axes:
                data["localAxes"] = axes
    except Exception as e:  # noqa: BLE001 - 穴・ローカル AXIS が求まらなくても形は見せる
        data["topologyError"] = f"{type(e).__name__}: {e}"


def _axis_lengths(raw: Any) -> tuple[float, float, float] | None:
    if raw is None:
        return DEFAULT_AXIS_LENGTHS
    if isinstance(raw, str):
        raw = [x for x in raw.replace(";", ",").split(",") if x.strip()]
    try:
        vals = tuple(float(x) for x in raw)
    except (TypeError, ValueError):
        return None
    if len(vals) != 3 or any(v <= 0 for v in vals):
        return None
    return vals  # type: ignore[return-value]


def _decode(value: Any, dtype: str, width: int) -> np.ndarray:
    if not value:
        return np.zeros((0, width), dtype=np.dtype(dtype))
    raw = base64.b64decode(value) if isinstance(value, str) else bytes(value)
    return np.frombuffer(raw, dtype=np.dtype(dtype).newbyteorder("<")).reshape(-1, width)


# ---------------------------------------------------------------------------
# 穴
# ---------------------------------------------------------------------------


def find_boundary_holes(data: dict[str, Any], tolerance: float) -> tuple[list[dict], dict[str, Any]] | None:
    """ビューア用データの三角形から、開いた形の穴を求める。境界が無い（閉じた形・三角形が無い）なら None。"""
    mesh = data.get("mesh") or {}
    positions = _decode(mesh.get("positions"), "f4", 3).astype(np.float64)
    indices = _decode(mesh.get("indices"), "u4", 3).astype(np.int64)
    if not len(indices):
        return None
    tri_face = _triangle_faces(mesh.get("faceRanges") or [], len(indices))
    tris = _Boundary(positions, indices, tolerance)
    if not len(tris.a):
        return None
    tris.match_within_tolerance()
    loops, open_chains = tris.loops()
    faces = data.get("faces") or []
    edges = data.get("edgeList") or []
    is_brep = (data.get("source") or {}).get("format") != "STL"

    tri_normals = _triangle_normals(positions, indices)
    candidates: list[dict[str, Any]] = []
    gaps = 0
    for pts, loop_tris in loops:
        info = _loop_info(pts, loop_tris, tri_normals, tri_face)
        if info is None:
            continue
        if info["_width"] <= tolerance:
            gaps += 1
            continue
        candidates.append(info)
    if not candidates and not open_chains and not gaps:
        return None

    # つながった面のまとまり（部分）ごとに、外接箱の最も大きいループを外周とする。
    comp = _face_components(len(faces), edges, tris.matched_face_pairs(tri_face))
    outer: dict[int, int] = {}
    for i, c in enumerate(candidates):
        key = int(comp[c["_faces"][0]]) if c["_faces"] and len(comp) else -1
        c["_component"] = key
        j = outer.get(key)
        if j is None or (c["_diag"], c["perimeter"]) > (candidates[j]["_diag"], candidates[j]["perimeter"]):
            outer[key] = i
    outer_ids = set(outer.values())
    holes = [c for i, c in enumerate(candidates) if i not in outer_ids]

    if is_brep:
        _attach_brep_edges(holes, faces, edges, data.get("edges") or {})

    items: list[dict[str, Any]] = []
    seg_chunks: list[np.ndarray] = []
    ranges: list[list[int]] = []
    seg_total = 0
    for i, c in enumerate(holes):
        pts = c.pop("_points")
        segs = np.stack([pts, np.roll(pts, -1, axis=0)], axis=1).reshape(-1, 3)
        seg_chunks.append(segs)
        ranges.append([seg_total, len(pts)])
        seg_total += len(pts)
        item = {"id": i, "kind": "hole", "geomType": "BOUNDARY_LOOP"}
        item.update({k: v for k, v in c.items() if not k.startswith("_")})
        item["faces"] = c["_faces"]
        if "_edges" in c:
            item["edges"] = c["_edges"]
        parts = sorted({int(faces[f]["part"]) for f in c["_faces"] if f < len(faces) and isinstance(faces[f].get("part"), int)})
        if len(parts) == 1:
            item["part"] = parts[0]
        items.append(item)
    geometry = {
        "positions": b64(np.concatenate(seg_chunks) if seg_chunks else np.zeros((0, 3)), "f4"),
        "ranges": ranges,
        "tolerance": rnd(tolerance),
        "outerLoops": len(outer_ids),
        "openChains": open_chains,
        "gapLoops": gaps,
    }
    return items, geometry


def _triangle_faces(face_ranges: Sequence[Sequence[int]], n_tris: int) -> np.ndarray:
    out = np.full(n_tris, -1, dtype=np.int64)
    for fid, (start, count) in enumerate(face_ranges):
        out[int(start) : int(start) + int(count)] = fid
    return out


def _triangle_normals(positions: np.ndarray, indices: np.ndarray) -> np.ndarray:
    """三角形ごとの面積ベクトル（向きは三角形の回り方。大きさは面積の 2 倍）。"""
    p = positions[indices]
    return np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])


class _Boundary:
    """三角形の境界の辺（同じ座標の頂点をまとめた後で三角形 1 つにしか属さない辺）と、その突き合わせ・つなぎ。"""

    def __init__(self, positions: np.ndarray, indices: np.ndarray, tolerance: float) -> None:
        self.tol = tolerance
        quantum = tolerance * 1e-3
        keys = np.floor(positions / quantum + 0.5).astype(np.int64)
        _, inverse = np.unique(keys, axis=0, return_inverse=True)
        inverse = inverse.reshape(-1)
        n_vertices = int(inverse.max()) + 1 if len(inverse) else 0
        counts = np.bincount(inverse, minlength=n_vertices).astype(np.float64)
        self.points = np.stack([np.bincount(inverse, weights=positions[:, k], minlength=n_vertices) for k in range(3)], axis=1) / counts[:, None]
        tri = inverse[indices]
        ok = (tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])
        tri_ids = np.flatnonzero(ok)
        tri = tri[ok]
        a = tri[:, [0, 1, 2]].ravel()
        b = tri[:, [1, 2, 0]].ravel()
        owner = np.repeat(tri_ids, 3)
        lo, hi = np.minimum(a, b), np.maximum(a, b)
        key = lo * max(n_vertices, 1) + hi
        _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
        boundary = cnt[inv.reshape(-1)] == 1
        self.a = a[boundary]
        self.b = b[boundary]
        self.tri = owner[boundary]
        self.alive = np.ones(len(self.a), dtype=bool)
        self.partner = np.full(len(self.a), -1, dtype=np.int64)

    # --- 突き合わせ（トポロジートレランス） ---

    def match_within_tolerance(self) -> None:
        """別の境界の辺から許容差以内に並ぶ境界の辺を除く（隙間が許容差以下の境目）。

        辺の上の数点（_SAMPLES）のどれもが、平行に近い別の境界の辺へ、その辺の内側で垂線を下ろせて、
        その長さが許容差以内なら除く。同じ縁で続く隣の辺は、点から下ろした垂線がその辺の外に出るので相手にならない
        （細かく分けた小さな円の縁でも）。
        """
        n = len(self.a)
        if n < 2:
            return
        tol = self.tol
        pa, pb = self.points[self.a], self.points[self.b]
        seg = pb - pa
        length = np.linalg.norm(seg, axis=1)
        direction = np.divide(seg, length[:, None], out=np.zeros_like(seg), where=length[:, None] > 0)
        cell = max(tol * 4, float(np.median(length)))
        grid: dict[tuple[int, int, int], list[int]] = {}
        lo = np.floor((np.minimum(pa, pb) - tol) / cell).astype(np.int64)
        hi = np.floor((np.maximum(pa, pb) + tol) / cell).astype(np.int64)
        for i in range(n):
            x0, y0, z0 = lo[i].tolist()
            x1, y1, z1 = hi[i].tolist()
            # 許容差に比べて非常に長い辺（大きな平面の縁など）は、入れる升目を始点の側に限る。
            x1, y1, z1 = min(x1, x0 + 15), min(y1, y0 + 15), min(z1, z0 + 15)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    for z in range(z0, z1 + 1):
                        grid.setdefault((x, y, z), []).append(i)
        tol2 = tol * tol

        def covering(i: int, p: np.ndarray) -> int:
            """p から許容差以内で、内側に垂線を下ろせる平行に近い別の辺（無ければ -1）。"""
            for j in grid.get(tuple(np.floor(p / cell).astype(np.int64).tolist()), ()):  # type: ignore[arg-type]
                if j == i or abs(float(direction[i] @ direction[j])) < _PARALLEL_COS:
                    continue
                d = seg[j]
                ll = float(d @ d)
                if ll <= 0:
                    continue
                t = float((p - pa[j]) @ d) / ll
                if t < 0.0 or t > 1.0:
                    continue
                q = pa[j] + d * t - p
                if float(q @ q) <= tol2:
                    return j
            return -1

        for i in range(n):
            if length[i] <= 0:
                continue
            partner = -1
            for k in _SAMPLES:
                j = covering(i, pa[i] + seg[i] * k)
                if j < 0:
                    partner = -1
                    break
                if partner < 0:
                    partner = j
            if partner >= 0:
                self.alive[i] = False
                self.partner[i] = partner

    def matched_face_pairs(self, tri_face: np.ndarray) -> list[tuple[int, int]]:
        """突き合わせで除いた辺の、両側の三角形が属する面の組（つながった面のまとまりを求めるのに使う）。"""
        out = []
        for i in np.flatnonzero(self.partner >= 0).tolist():
            fa, fb = int(tri_face[self.tri[i]]), int(tri_face[self.tri[self.partner[i]]])
            if fa >= 0 and fb >= 0 and fa != fb:
                out.append((fa, fb))
        return out

    # --- ループ ---

    def loops(self) -> tuple[list[tuple[np.ndarray, list[int]]], int]:
        """残った境界の辺をループにつなぐ。戻り値は ([(点の並び, 縁の三角形), ...], 閉じなかった線の数)。"""
        live = np.flatnonzero(self.alive).tolist()
        out_edges: dict[int, list[int]] = {}
        in_edges: dict[int, list[int]] = {}
        for i in live:
            out_edges.setdefault(int(self.a[i]), []).append(i)
            in_edges.setdefault(int(self.b[i]), []).append(i)
        used: set[int] = set()
        chains: list[tuple[list[int], list[int]]] = []  # (頂点の並び, 辺の並び)
        for start in live:
            if start in used:
                continue
            used.add(start)
            verts = [int(self.a[start]), int(self.b[start])]
            path = [start]
            while verts[-1] != verts[0]:
                cur = verts[-1]
                nxt = next((e for e in out_edges.get(cur, ()) if e not in used), None)
                flip = False
                if nxt is None:
                    # 三角形の回り方がそろっていないメッシュでは、逆向きの辺でつなぐ。
                    nxt = next((e for e in in_edges.get(cur, ()) if e not in used), None)
                    flip = True
                if nxt is None:
                    break
                used.add(nxt)
                path.append(nxt)
                verts.append(int(self.a[nxt]) if flip else int(self.b[nxt]))
            chains.append((verts, path))
        closed = [c for c in chains if len(c[0]) > 3 and c[0][-1] == c[0][0]]
        opened = [c for c in chains if not (len(c[0]) > 1 and c[0][-1] == c[0][0])]
        closed += self._bridge(opened)
        open_count = sum(1 for c in opened if c[0])
        loops = [(self.points[np.array(v[:-1])], [int(self.tri[e]) for e in p]) for v, p in closed if len(v) > 3]
        return loops, open_count

    def _bridge(self, opened: list[tuple[list[int], list[int]]]) -> list[tuple[list[int], list[int]]]:
        """途切れた線の端どうしが許容差以内なら橋渡しして、閉じたループにする（opened から外す）。"""
        if not opened:
            return []
        tol = self.tol
        closed: list[tuple[list[int], list[int]]] = []
        changed = True
        while changed and opened:
            changed = False
            for i, (vi, pi) in enumerate(opened):
                if not vi:
                    continue
                head, tail = self.points[vi[0]], self.points[vi[-1]]
                if len(vi) > 3 and float(np.linalg.norm(head - tail)) <= tol:
                    closed.append((vi + [vi[0]], pi))
                    opened[i] = ([], [])
                    changed = True
                    continue
                best = None
                for j, (vj, _pj) in enumerate(opened):
                    if j == i or not vj:
                        continue
                    for rev in (False, True):
                        start = self.points[vj[-1] if rev else vj[0]]
                        d = float(np.linalg.norm(tail - start))
                        if d <= tol and (best is None or d < best[0]):
                            best = (d, j, rev)
                if best is not None:
                    _, j, rev = best
                    vj, pj = opened[j]
                    opened[i] = (vi + (vj[::-1] if rev else vj), pi + pj)
                    opened[j] = ([], [])
                    changed = True
        opened[:] = [c for c in opened if c[0]]
        return closed


def _loop_info(pts: np.ndarray, loop_tris: list[int], tri_normals: np.ndarray, tri_face: np.ndarray) -> dict[str, Any] | None:
    """ループ 1 つの形の情報（中心・法線・面積・周長・直径・平面からのずれ・外接箱・縁の面）。"""
    if len(pts) < 3:
        return None
    nxt = np.roll(pts, -1, axis=0)
    seg_len = np.linalg.norm(nxt - pts, axis=1)
    perimeter = float(seg_len.sum())
    if perimeter <= 0:
        return None
    center = (((pts + nxt) / 2) * seg_len[:, None]).sum(axis=0) / perimeter
    area_vec = 0.5 * np.cross(pts - center, nxt - center).sum(axis=0)
    area = float(np.linalg.norm(area_vec))
    surf = tri_normals[np.array(loop_tris)].sum(axis=0) if loop_tris else np.zeros(3)
    surf_norm = float(np.linalg.norm(surf))
    if area > 1e-12 * perimeter * perimeter:
        normal = area_vec / area
        if surf_norm > 0 and float(normal @ surf) < 0:
            normal = -normal
    elif surf_norm > 0:
        normal = surf / surf_norm
    else:
        return None
    rel = pts - center
    off = rel @ normal
    in_plane = rel - np.outer(off, normal)
    radii = np.linalg.norm(in_plane, axis=1)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    faces = sorted({int(tri_face[t]) for t in loop_tris if tri_face[t] >= 0})
    return {
        "center": vec(center),
        "normal": vec(normal),
        "area": rnd(area),
        "perimeter": rnd(perimeter),
        "diameter": rnd(2 * float(radii.mean())),
        "circularity": rnd(4 * math.pi * area / (perimeter * perimeter), 4),
        "planarity": rnd(float(np.abs(off).max())),
        "bbox": bbox_of(pts),
        "segments": len(pts),
        "_points": pts,
        "_faces": faces,
        "_width": 2 * area / perimeter,
        "_diag": float(np.linalg.norm(hi - lo)),
    }


def _face_components(n_faces: int, edges: list[dict], extra_pairs: list[tuple[int, int]]) -> np.ndarray:
    """面どうしのつながり（エッジを共有する・許容差以内で突き合わせた）でまとめた、面ごとのまとまりの番号。"""
    parent = list(range(n_faces))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        if 0 <= x < n_faces and 0 <= y < n_faces:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[ry] = rx

    for e in edges:
        fs = e.get("faces") or []
        for f in fs[1:]:
            union(int(fs[0]), int(f))
    for x, y in extra_pairs:
        union(x, y)
    return np.array([find(i) for i in range(n_faces)], dtype=np.int64)


def _attach_brep_edges(holes: list[dict], faces: list[dict], edges: list[dict], edge_geom: dict[str, Any]) -> None:
    """B-rep の穴に、その縁になっているエッジ（面 1 つにしか属さないエッジ）の ID を付ける。"""
    if not holes or not edges:
        return
    seg = _decode(edge_geom.get("positions"), "f4", 3).astype(np.float64)
    ranges = edge_geom.get("ranges") or []
    by_face: dict[int, list[int]] = {}
    for hi, h in enumerate(holes):
        for f in h["_faces"]:
            by_face.setdefault(f, []).append(hi)
    found: dict[int, list[int]] = {}
    for eid, e in enumerate(edges):
        fs = e.get("faces") or []
        if len(fs) != 1 or e.get("seam") or e.get("free") or eid >= len(ranges):
            continue
        cand = by_face.get(int(fs[0]))
        if not cand:
            continue
        start, count = ranges[eid]
        if not count:
            continue
        pts = seg[int(start) * 2 : (int(start) + int(count)) * 2]
        sample = pts[len(pts) // 2]
        best = None
        for hi in cand:
            d = float(np.min(np.linalg.norm(holes[hi]["_points"] - sample, axis=1)))
            if best is None or d < best[0]:
                best = (d, hi)
        if best is not None:
            found.setdefault(best[1], []).append(eid)
    for hi, ids in found.items():
        holes[hi]["_edges"] = sorted(ids)


# ---------------------------------------------------------------------------
# ローカル AXIS
# ---------------------------------------------------------------------------


def find_local_axes(
    edges: list[dict],
    lengths: Sequence[float] = DEFAULT_AXIS_LENGTHS,
    length_tolerance: float = DEFAULT_AXIS_LENGTH_TOLERANCE,
    angle_tolerance_deg: float = DEFAULT_AXIS_ANGLE_TOLERANCE_DEG,
) -> list[dict[str, Any]]:
    """長さ lengths（X・Y・Z の順）の直線のエッジ 3 本が 1 つの端点を共有して直交するものを、ローカル座標系として返す。"""
    tol = max(float(length_tolerance), 1e-9)
    sin_tol = math.sin(math.radians(max(float(angle_tolerance_deg), 0.0)))
    cands: list[list[tuple[int, np.ndarray, np.ndarray]]] = [[], [], []]
    for e in edges:
        if e.get("geomType") != "LINE" or "start" not in e or "end" not in e:
            continue
        s, t = np.asarray(e["start"], float), np.asarray(e["end"], float)
        length = float(np.linalg.norm(t - s))
        for k, target in enumerate(lengths):
            if abs(length - float(target)) <= tol:
                cands[k].append((int(e["id"]), s, t))
    if not all(cands):
        return []

    def ends(items: list[tuple[int, np.ndarray, np.ndarray]]) -> dict[tuple[int, int, int], list[tuple[int, np.ndarray, np.ndarray]]]:
        grid: dict[tuple[int, int, int], list[tuple[int, np.ndarray, np.ndarray]]] = {}
        for eid, s, t in items:
            for p, q in ((s, t), (t, s)):
                key = tuple(np.floor(p / tol).astype(np.int64).tolist())
                grid.setdefault(key, []).append((eid, p, q))  # type: ignore[arg-type]
        return grid

    y_ends, z_ends = ends(cands[1]), ends(cands[2])

    def near(grid, p: np.ndarray) -> Iterable[tuple[int, np.ndarray, np.ndarray]]:
        c = np.floor(p / tol).astype(np.int64)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for item in grid.get((int(c[0] + dx), int(c[1] + dy), int(c[2] + dz)), ()):
                        if float(np.linalg.norm(item[1] - p)) <= tol:
                            yield item

    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int]] = set()
    for ex, s, t in cands[0]:
        for p, q in ((s, t), (t, s)):
            dx = _unit(q - p)
            for ey, py, qy in near(y_ends, p):
                if ey == ex:
                    continue
                dy = _unit(qy - py)
                if abs(float(dx @ dy)) > sin_tol:
                    continue
                for ez, pz, qz in near(z_ends, p):
                    if ez in (ex, ey):
                        continue
                    dz = _unit(qz - pz)
                    if abs(float(dx @ dz)) > sin_tol or abs(float(dy @ dz)) > sin_tol:
                        continue
                    key = (ex, ey, ez)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(_axis_info(len(out), (p + py + pz) / 3, dx, dy, dz, key))
                    if len(out) >= LOCAL_AXES_MAX:
                        return out
    return out


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _axis_info(idx: int, origin: np.ndarray, dx: np.ndarray, dy: np.ndarray, dz: np.ndarray, edge_ids: tuple[int, int, int]) -> dict[str, Any]:
    right = float(np.cross(dx, dy) @ dz) > 0
    x = dx
    y = _unit(dy - x * float(dy @ x))
    z = np.cross(x, y) if right else -np.cross(x, y)
    worst = max(
        abs(90 - math.degrees(math.acos(max(-1.0, min(1.0, float(a @ b))))))
        for a, b in ((dx, dy), (dx, dz), (dy, dz))
    )
    info: dict[str, Any] = {
        "id": idx,
        # 座標の変換に使うので、ほかの値（6 桁）より細かく持つ（100mm 先で 1e-5 の誤差にならないように）。
        "origin": vec(origin, 9),
        "x": vec(x, 9),
        "y": vec(y, 9),
        "z": vec(z, 9),
        "edges": list(edge_ids),
        "rightHanded": right,
        "orthogonalityErrorDeg": rnd(worst, 4),
    }
    return info


def to_local_point(axis: dict[str, Any], p: Sequence[float]) -> list[float]:
    """ワールド座標の点をローカル座標にする（local = [x·(p−o), y·(p−o), z·(p−o)]）。"""
    d = np.asarray(p, float) - np.asarray(axis["origin"], float)
    return vec([float(np.asarray(axis[k], float) @ d) for k in ("x", "y", "z")])


def to_local_vector(axis: dict[str, Any], v: Sequence[float]) -> list[float]:
    """ワールド座標の向き（法線・軸・差）をローカル座標にする。"""
    w = np.asarray(v, float)
    return vec([float(np.asarray(axis[k], float) @ w) for k in ("x", "y", "z")])
