"""ソリッド・閉じたメッシュの穴の形（丸・四角・角 R の四角・正 5〜8 角形・その他）の認識。numpy だけで動く（cadquery 不要）。

ビューア用データの三角形と面（面 i の三角形は faceRanges[i] の範囲）から求めるので、B-rep（STEP 等）と STL を同じ処理で扱う。
src/cad/features.py（円柱面からの丸穴・ボス）を補うもので、B-rep の丸穴（円柱面に ``feature`` が付いた穴）は重ねて出さない。

1. 三角形の頂点を同じ座標で 1 つにまとめ、面ごとに「その面の三角形 1 つだけに属する辺」（面の縁）を求める。
   面の中の穴の数はオイラー数（頂点 − 辺 + 三角形 = 1 − 穴の数）で分かるので、穴のある平らな面だけを調べる。
2. 平らな面の縁をループにつなぎ、面積が最大のものを外周、残りを穴の口（内側の輪郭）とする。
3. 口の辺の向こうの面（壁）から、口の法線（＝穴の軸）に平行な面（三角形の法線が軸にほぼ直交する面）をたどって壁をまとめる。
   壁の外接の範囲が口の輪郭からはみ出す（形の外側の面につながった）なら穴としない。壁の法線が軸の方を向いていれば穴
   （外を向いていればボスなので出さない）。面取り・テーパーのある口（壁が軸に平行でない）は扱わない。
4. 壁の先の面のうち、口の輪郭の中に収まるものは底（止まり穴）。無ければ通し穴。深さは壁の軸方向の範囲。
   通し穴は両方の口から同じ壁が見つかるので、壁の組で 1 つにまとめる。
5. 口の輪郭（平面の 2 次元の点列。一直線に並ぶ点は除く）の形を当てる:
   - 正多角形（polygon）: 点が 5〜8 個で、中心からの距離と辺の長さがそろう。sides・acrossFlats（対辺）・acrossCorners（対角）。
   - 丸（circle）: 点が 9 個以上で円に乗る。diameter。
   - 四角（rectangle）・角 R の四角（roundedRectangle）: 最小の外接長方形に沿い、角の点から R を求めて輪郭に合うか確かめる。
     width ≥ height・cornerRadius。
   - どれでもなければ other（width・height は最小の外接長方形）。
   xDirection は形の x の向き（長方形は長い辺、正多角形は中心から頂点の 1 つへ）。inscribedDiameter は中心に置ける
   一番大きい円の直径（基準穴の丸ピンの太さの上限）。

profile_sdf は、形の中心と x の向きを原点・x 軸にした平面での符号付き距離（内側が負）。src/cad/align.py の基準穴で使う。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .mesh_data import rnd, vec

# 認識する形と、正多角形の辺の数の範囲。
SHAPES = ("circle", "rectangle", "roundedRectangle", "polygon", "other")
POLYGON_SIDES = range(5, 9)
# 基準穴の丸ピンの当て方を形で決められるもの（other は内接円の丸穴として扱う）。
SDF_SHAPES = ("circle", "rectangle", "roundedRectangle", "polygon")
# 平らな面とみなす、三角形の法線と面の法線のなす角の上限（度）。
_PLANE_COS = math.cos(math.radians(2.0))
# 壁とみなす、三角形の法線と穴の軸のなす角の 90° からのずれの上限（度）。
_WALL_SIN = math.sin(math.radians(1.5))
# 一直線に並ぶとみなす、隣り合う 2 辺の折れ角の正弦の上限。
_COLLINEAR_SIN = 2e-3
# 輪郭の点が形に乗るとみなす許容差（輪郭の大きさに対する割合）。頂点は曲線の上の点なので厳しく、辺の中点は弦の分だけ緩く。
_VERTEX_TOL = 2e-3
_MIDPOINT_TOL = 0.02


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def recognize_profile_holes(
    positions: np.ndarray, indices: np.ndarray, face_ranges: list[list[int]], faces: list[dict], features: list[dict]
) -> list[dict]:
    """穴の一覧を返し、壁の面の情報に ``feature`` を付ける（faces を書き換える）。

    features は先に見つけた穴・ボス（src/cad/features.py）。ID の番号はその続きにし、その壁を使う穴は出さない。
    認識できなくても形の表示はできるので、失敗したら何も足さない。
    """
    try:
        return _recognize(positions, indices, face_ranges, faces, features)
    except Exception:  # noqa: BLE001 - 穴が求まらなくても形は見せる
        for f in faces:
            if isinstance(f.get("feature"), str) and f["feature"] not in {x.get("id") for x in features}:
                del f["feature"]
        return []


def _recognize(
    positions: np.ndarray, indices: np.ndarray, face_ranges: list[list[int]], faces: list[dict], features: list[dict]
) -> list[dict]:
    pos = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    idx = np.asarray(indices, dtype=np.int64).reshape(-1, 3)
    if not len(idx) or not faces or len(face_ranges) != len(faces):
        return []
    mesh = _Mesh(pos, idx, face_ranges)
    counter = sum(1 for f in features if f.get("type") == "hole")
    out: list[dict] = []
    seen: set[frozenset[int]] = set()
    for fid in mesh.candidate_faces():
        for hole in mesh.holes_of(fid, faces):
            walls = frozenset(hole["faces"])
            if walls in seen:
                continue
            seen.add(walls)
            hole["id"] = f"hole:{counter}"
            counter += 1
            for w in walls:
                faces[w]["feature"] = hole["id"]
            out.append(hole)
    return out


def closed_shape_loops(
    positions: np.ndarray, indices: np.ndarray, face_ranges: list[list[int]], faces: list[dict], hole_ids: set[str]
) -> tuple[list[dict], list[dict]]:
    """閉じた形（ソリッド・閉じたメッシュ）の、穴の口と外周のループ（src/cad/topology.py の穴・外周の一覧に足す）。

    穴の口: 平らな面の内側の輪郭のうち、向こうの面が穴（hole_ids の feature を持つ面）の壁のもの（通し穴は両方の口）。
    外周: 穴の口を持つ平らな面の外側の輪郭。どちらも {"points": (k,3) の順に並ぶ点, "face": 輪郭の面, "walls": 壁の面,
    "feature": 穴の ID}（外周は walls・feature なし）。
    """
    pos = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    idx = np.asarray(indices, dtype=np.int64).reshape(-1, 3)
    if not len(idx) or not hole_ids or len(face_ranges) != len(faces):
        return [], []
    mesh = _Mesh(pos, idx, face_ranges)
    holes: list[dict] = []
    outers: list[dict] = []
    for f in mesh.candidate_faces():
        n = mesh.face_normal(f)
        if not float(np.linalg.norm(n)) or not mesh.is_planar(f, n):
            continue
        loops = mesh.loops_of(f)
        if len(loops) < 2:
            continue
        u, v = _plane_basis(n)
        basis = np.stack([u, v], axis=1)
        areas = [abs(_shoelace((mesh.points[vs] - mesh.points[loops[0][0][0]]) @ basis)) for vs, _ in loops]
        outer = int(np.argmax(areas))
        found = False
        for i, (verts, others) in enumerate(loops):
            if i == outer:
                continue
            walls = sorted({o for o in others if o >= 0 and o != f})
            feats = {faces[w].get("feature") for w in walls} & hole_ids
            if len(feats) == 1:
                holes.append({"points": mesh.points[verts], "face": f, "walls": walls, "feature": feats.pop()})
                found = True
        if found:
            outers.append({"points": mesh.points[loops[outer][0]], "face": f})
    return holes, outers


# ---------------------------------------------------------------------------
# メッシュ（頂点をまとめ、面の縁・面のつながりを求める）
# ---------------------------------------------------------------------------


class _Mesh:
    def __init__(self, pos: np.ndarray, idx: np.ndarray, face_ranges: list[list[int]]) -> None:
        lo, hi = pos.min(axis=0), pos.max(axis=0)
        self.diag = float(np.linalg.norm(hi - lo)) or 1.0
        q = self.diag * 1e-6
        _, vid = np.unique(np.round(pos / q).astype(np.int64), axis=0, return_inverse=True)
        vid = vid.reshape(-1)
        self.points = np.zeros((int(vid.max()) + 1, 3))
        self.points[vid] = pos
        tri_face = np.full(len(idx), -1, dtype=np.int64)
        for f, (s, c) in enumerate(face_ranges):
            tri_face[int(s) : int(s) + int(c)] = f
        tv = vid[idx]
        ok = (tv[:, 0] != tv[:, 1]) & (tv[:, 1] != tv[:, 2]) & (tv[:, 2] != tv[:, 0]) & (tri_face >= 0)
        self.tri_face = tri_face
        p0, p1, p2 = pos[idx[:, 0]], pos[idx[:, 1]], pos[idx[:, 2]]
        cross = np.cross(p1 - p0, p2 - p0)
        area2 = np.linalg.norm(cross, axis=1)
        self.tri_normal = np.where(area2[:, None] > 0, cross / np.where(area2 > 0, area2, 1)[:, None], 0.0)
        self.tri_area = area2 / 2
        self.tri_center = (p0 + p1 + p2) / 3
        self.face_ranges = [(int(s), int(c)) for s, c in face_ranges]
        n_faces = len(face_ranges)
        self.n_faces = n_faces

        good = np.nonzero(ok)[0]
        tvg = tv[good]
        ef = np.repeat(tri_face[good], 3)
        a = tvg[:, [0, 1, 2]].reshape(-1)
        b = tvg[:, [1, 2, 0]].reshape(-1)
        nv = np.int64(len(self.points))
        key = np.minimum(a, b) * nv + np.maximum(a, b)
        # 面の縁: 同じ面の中で 1 回しか出ない辺。
        pair = np.stack([ef, key], axis=1)
        uniq, inv, cnt = np.unique(pair, axis=0, return_inverse=True, return_counts=True)
        inv = inv.reshape(-1)
        border = cnt[inv] == 1
        self.b_face, self.b_a, self.b_b, self.b_key = ef[border], a[border], b[border], key[border]
        # 縁の辺の向こうの面（同じ位置の辺を持つ別の面）。
        order = np.argsort(self.b_key, kind="stable")
        ks, fs = self.b_key[order], self.b_face[order]
        other = np.full(len(ks), -1, dtype=np.int64)
        same = (ks[1:] == ks[:-1]) & (fs[1:] != fs[:-1])
        i = np.nonzero(same)[0]
        other[i], other[i + 1] = fs[i + 1], fs[i]
        self.b_other = np.empty_like(other)
        self.b_other[order] = other
        # 面ごとの縁の辺（CSR）と、隣り合う面（CSR）。
        self.b_order = np.argsort(self.b_face, kind="stable")
        self.b_ptr = np.searchsorted(self.b_face[self.b_order], np.arange(n_faces + 1))
        pa = np.concatenate([fs[i], fs[i + 1]])
        pb = np.concatenate([fs[i + 1], fs[i]])
        adj = np.unique(np.stack([pa, pb], axis=1), axis=0) if len(pa) else np.zeros((0, 2), dtype=np.int64)
        self.adj = adj[:, 1]
        self.adj_ptr = np.searchsorted(adj[:, 0], np.arange(n_faces + 1))
        # オイラー数から、面の中の穴の数。
        uv = np.unique(np.stack([np.repeat(tri_face[good], 3), tvg.reshape(-1)], axis=1), axis=0)
        v_count = np.bincount(uv[:, 0], minlength=n_faces)
        e_count = np.bincount(uniq[:, 0], minlength=n_faces)
        t_count = np.bincount(tri_face[good], minlength=n_faces)
        self.inner_loops = 1 - (v_count - e_count + t_count)

    def tris(self, f: int) -> slice:
        s, c = self.face_ranges[f]
        return slice(s, s + c)

    def neighbors(self, f: int) -> np.ndarray:
        return self.adj[self.adj_ptr[f] : self.adj_ptr[f + 1]]

    def face_normal(self, f: int) -> np.ndarray:
        sl = self.tris(f)
        n = (self.tri_normal[sl] * self.tri_area[sl, None]).sum(axis=0)
        norm = float(np.linalg.norm(n))
        return n / norm if norm > 0 else n

    def is_planar(self, f: int, n: np.ndarray) -> bool:
        sl = self.tris(f)
        nn = self.tri_normal[sl][self.tri_area[sl] > 0]
        return bool(len(nn)) and float((nn @ n).min()) > _PLANE_COS

    def is_wall(self, f: int, axis: np.ndarray) -> bool:
        sl = self.tris(f)
        nn = self.tri_normal[sl][self.tri_area[sl] > 0]
        return bool(len(nn)) and float(np.abs(nn @ axis).max()) < _WALL_SIN

    def face_points(self, faces: set[int] | list[int]) -> np.ndarray:
        """面の縁の点（壁・底の範囲を見るため）。"""
        ids = [self.b_order[self.b_ptr[f] : self.b_ptr[f + 1]] for f in faces]
        if not ids:
            return np.zeros((0, 3))
        sel = np.concatenate(ids)
        return self.points[np.unique(np.concatenate([self.b_a[sel], self.b_b[sel]]))]

    def candidate_faces(self) -> list[int]:
        return [int(f) for f in np.nonzero(self.inner_loops >= 1)[0]]

    def loops_of(self, f: int) -> list[tuple[list[int], list[int]]]:
        """面 f の縁のループ（(頂点の並び, 各辺の向こうの面)）。"""
        sel = self.b_order[self.b_ptr[f] : self.b_ptr[f + 1]]
        nxt: dict[int, list[tuple[int, int]]] = {}
        for e in sel:
            nxt.setdefault(int(self.b_a[e]), []).append((int(self.b_b[e]), int(self.b_other[e])))
        loops = []
        while nxt:
            start = next(iter(nxt))
            verts, others = [start], []
            cur = start
            while True:
                outs = nxt.get(cur)
                if not outs:
                    break
                b, o = outs.pop()
                if not outs:
                    del nxt[cur]
                others.append(o)
                if b == start:
                    break
                verts.append(b)
                cur = b
            if len(verts) >= 3 and len(others) == len(verts):
                loops.append((verts, others))
        return loops

    def holes_of(self, f: int, faces: list[dict]) -> list[dict]:
        n = self.face_normal(f)
        if not float(np.linalg.norm(n)) or not self.is_planar(f, n):
            return []
        loops = self.loops_of(f)
        if len(loops) < 2:
            return []
        u, v = _plane_basis(n)
        origin = self.points[loops[0][0][0]]
        areas = []
        for verts, _ in loops:
            p2 = (self.points[verts] - origin) @ np.stack([u, v], axis=1)
            areas.append(abs(_shoelace(p2)))
        outer = int(np.argmax(areas))
        out = []
        for i, (verts, others) in enumerate(loops):
            if i == outer or areas[i] <= 0:
                continue
            hole = self._hole_from_loop(f, n, u, v, verts, others, faces)
            if hole is not None:
                out.append(hole)
        return out

    def _hole_from_loop(
        self, f: int, n: np.ndarray, u: np.ndarray, v: np.ndarray, verts: list[int], others: list[int], faces: list[dict]
    ) -> dict | None:
        if any(o < 0 for o in others):
            return None
        start = set(others) - {f}
        if not start or not all(self.is_wall(w, n) for w in start):
            return None
        # 軸に平行な面をたどって壁をまとめる。
        walls = set(start)
        stack = list(start)
        while stack:
            cur = stack.pop()
            for nb in self.neighbors(cur):
                nb = int(nb)
                if nb != f and nb not in walls and self.is_wall(nb, n):
                    walls.add(nb)
                    stack.append(nb)
        if any(faces[w].get("feature") for w in walls):
            return None  # B-rep の丸穴（src/cad/features.py）
        pts3 = self.points[verts]
        origin = pts3.mean(axis=0)
        basis = np.stack([u, v], axis=1)
        p2 = (pts3 - origin) @ basis
        size = float(np.ptp(p2, axis=0).max())
        if size <= 0:
            return None
        tol = max(size * _VERTEX_TOL, self.diag * 1e-6)
        lo, hi = p2.min(axis=0) - tol * 5, p2.max(axis=0) + tol * 5
        wall_pts = self.face_points(walls)
        w2 = (wall_pts - origin) @ basis
        if len(w2) and (np.any(w2 < lo) or np.any(w2 > hi)):
            return None  # 壁が口の輪郭の外へ広がる（形の外側の面につながった）
        # 壁の法線が軸の方を向いていれば穴（外を向いていればボス）。
        tri = np.concatenate([np.arange(*self.tris(w).indices(len(self.tri_face))) for w in walls])
        rel = self.tri_center[tri] - origin
        radial = rel - np.outer(rel @ n, n)
        if float(((radial * self.tri_normal[tri]).sum(axis=1) * self.tri_area[tri]).sum()) >= 0:
            return None
        t = (wall_pts - origin) @ n
        t0, t1 = float(min(t.min(), 0.0)), float(max(t.max(), 0.0))
        depth = t1 - t0
        if depth <= tol:
            return None
        # 壁の先の面のうち、口の輪郭の中に収まるものが底。
        ends = {int(nb) for w in walls for nb in self.neighbors(w)} - walls - {f}
        caps = []
        for e in ends:
            e2 = (self.face_points([e]) - origin) @ basis
            if len(e2) and np.all(e2 >= lo) and np.all(e2 <= hi):
                caps.append(e)
        profile = classify_profile(p2)
        c2 = np.asarray(profile.pop("center2"))
        x2 = np.asarray(profile.pop("x2"))
        center = origin + basis @ c2
        axis = _canonical_axis(n)
        hole: dict[str, Any] = {"type": "hole", **profile}
        hole.update(
            depth=rnd(depth, 4),
            axisDirection=vec(axis),
            axisStart=vec(center + n * t0),
            axisEnd=vec(center + n * t1),
            xDirection=vec(basis @ x2),
            faces=sorted(walls),
            through=not caps,
        )
        if caps:
            hole["bottomFaces"] = sorted(caps)
        return hole


# ---------------------------------------------------------------------------
# 輪郭の形
# ---------------------------------------------------------------------------


def classify_profile(points: np.ndarray) -> dict[str, Any]:
    """平面の閉じた点列（(k,2)、順に並ぶ）の形。center2（形の中心）・x2（x の向き）と、形ごとの大きさを返す。"""
    p = _simplify(np.asarray(points, dtype=np.float64))
    size = float(np.ptp(p, axis=0).max()) if len(p) else 0.0
    scale = float(np.abs(p).max()) if len(p) else 0.0
    tol = max(size * _VERTEX_TOL, scale * 4e-7)
    tol_mid = size * _MIDPOINT_TOL
    mids = (p + np.roll(p, -1, axis=0)) / 2
    k = len(p)

    if k in POLYGON_SIDES:
        c = p.mean(axis=0)
        r = np.linalg.norm(p - c, axis=1)
        side = np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1)
        if np.ptp(r) <= tol and np.ptp(side) <= 2 * tol:
            rr = float(r.mean())
            x = (p[0] - c) / np.linalg.norm(p[0] - c)
            flats = 2 * rr * math.cos(math.pi / k)
            return {
                "shape": "polygon",
                "sides": k,
                "acrossFlats": rnd(flats, 4),
                "acrossCorners": rnd(2 * rr, 4),
                "sideLength": rnd(float(side.mean()), 4),
                "inscribedDiameter": rnd(flats, 4),
                "center2": c,
                "x2": x,
            }

    if k >= 9:
        c, r = _fit_circle(p)
        if r > 0 and np.abs(np.linalg.norm(p - c, axis=1) - r).max() <= tol and np.abs(np.linalg.norm(mids - c, axis=1) - r).max() <= tol_mid:
            return {"shape": "circle", "diameter": rnd(2 * r, 4), "inscribedDiameter": rnd(2 * r, 4), "center2": c, "x2": np.array([1.0, 0.0])}

    c, x, w, h = _min_area_rect(p)
    y = np.array([-x[1], x[0]])
    local = np.stack([(p - c) @ x, (p - c) @ y], axis=1)
    a, b = w / 2, h / 2
    du = a - np.abs(local[:, 0])
    dv = b - np.abs(local[:, 1])
    corner = (du > tol) & (dv > tol)
    radius = float(np.median(du[corner] + dv[corner] + np.sqrt(2 * du[corner] * dv[corner]))) if corner.any() else 0.0
    if radius <= min(a, b) + tol:
        radius = min(radius, min(a, b))
        prof = {"shape": "roundedRectangle", "width": w, "height": h, "cornerRadius": radius}
        lm = np.stack([(mids - c) @ x, (mids - c) @ y], axis=1)
        if np.abs(profile_sdf(prof, local)).max() <= 2 * tol and np.abs(profile_sdf(prof, lm)).max() <= tol_mid:
            out: dict[str, Any] = {"shape": "rectangle" if radius <= tol else "roundedRectangle", "width": rnd(w, 4), "height": rnd(h, 4)}
            if radius > tol:
                out["cornerRadius"] = rnd(radius, 4)
            out.update(inscribedDiameter=rnd(h, 4), center2=c, x2=x)
            return out

    # どの形にも合わない: 外接長方形の大きさと、中心に置ける一番大きい円。
    seg_a, seg_b = p, np.roll(p, -1, axis=0)
    return {
        "shape": "other",
        "width": rnd(w, 4),
        "height": rnd(h, 4),
        "inscribedDiameter": rnd(2 * float(_point_segment_distance(c, seg_a, seg_b).min()), 4),
        "center2": c,
        "x2": x,
    }


def profile_sdf(profile: dict[str, Any], xy: np.ndarray) -> np.ndarray:
    """形の中心を原点、xDirection を x 軸にした平面の点 (k,2) の符号付き距離（内側が負）。

    正多角形は頂点の 1 つが +x の上にある向き。外側の角の近くでは距離を少し小さく見積もる（内側は正確）。
    """
    xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    shape = profile.get("shape")
    if shape == "circle":
        return np.linalg.norm(xy, axis=1) - float(profile["diameter"]) / 2
    if shape in ("rectangle", "roundedRectangle"):
        a, b = float(profile["width"]) / 2, float(profile["height"]) / 2
        r = min(float(profile.get("cornerRadius") or 0.0), a, b)
        q = np.abs(xy) - np.array([a - r, b - r])
        return np.linalg.norm(np.maximum(q, 0.0), axis=1) + np.minimum(q.max(axis=1), 0.0) - r
    if shape == "polygon":
        n = int(profile["sides"])
        apothem = float(profile["acrossFlats"]) / 2
        ang = math.pi / n + 2 * math.pi * np.arange(n) / n
        normals = np.stack([np.cos(ang), np.sin(ang)], axis=1)
        return (xy @ normals.T).max(axis=1) - apothem
    raise ValueError(f"no distance for the hole shape {shape!r}")


def profile_inradius(profile: dict[str, Any]) -> float:
    """中心に置ける一番大きい円の半径。"""
    shape = profile.get("shape")
    if shape == "circle":
        return float(profile["diameter"]) / 2
    if shape in ("rectangle", "roundedRectangle"):
        return min(float(profile["width"]), float(profile["height"])) / 2
    if shape == "polygon":
        return float(profile["acrossFlats"]) / 2
    return float(profile.get("inscribedDiameter") or 0.0) / 2


def profile_circumradius(profile: dict[str, Any]) -> float:
    """中心から一番遠い輪郭の点までの距離。"""
    shape = profile.get("shape")
    if shape == "circle":
        return float(profile["diameter"]) / 2
    if shape in ("rectangle", "roundedRectangle"):
        a, b = float(profile["width"]) / 2, float(profile["height"]) / 2
        r = min(float(profile.get("cornerRadius") or 0.0), a, b)
        return math.hypot(a - r, b - r) + r
    if shape == "polygon":
        return float(profile["acrossFlats"]) / 2 / math.cos(math.pi / int(profile["sides"]))
    return float(profile.get("inscribedDiameter") or 0.0) / 2


def profile_size_fields(feature: dict[str, Any]) -> dict[str, Any]:
    """穴の形と大きさの項目（要約・比較のまとめに使う）。丸は shape を付けず diameter だけ。"""
    shape = feature.get("shape", "circle")
    if shape == "circle":
        return {"diameter": feature.get("diameter")}
    keys = {
        "rectangle": ("width", "height"),
        "roundedRectangle": ("width", "height", "cornerRadius"),
        "polygon": ("sides", "acrossFlats"),
        "other": ("width", "height"),
    }.get(shape, ())
    return {"shape": shape, **{k: feature.get(k) for k in keys}}


# ---------------------------------------------------------------------------
# 幾何の小道具
# ---------------------------------------------------------------------------


def _plane_basis(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    h = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
    u = np.cross(n, h)
    u /= np.linalg.norm(u)
    return u, np.cross(n, u)


def _canonical_axis(d: np.ndarray) -> np.ndarray:
    for c in d:
        if abs(c) > 1e-9:
            return d if c > 0 else -d
    return d


def _shoelace(p: np.ndarray) -> float:
    x, y = p[:, 0], p[:, 1]
    return float((x * np.roll(y, -1) - np.roll(x, -1) * y).sum() / 2)


def _simplify(p: np.ndarray) -> np.ndarray:
    """重なる点と、一直線に並ぶ点を除く。"""
    for _ in range(len(p)):
        if len(p) < 3:
            return p
        d = np.roll(p, -1, axis=0) - p
        keep = np.linalg.norm(d, axis=1) > 0
        if not keep.all():
            p = p[keep]
            continue
        prev = np.roll(d, 1, axis=0)
        cross = prev[:, 0] * d[:, 1] - prev[:, 1] * d[:, 0]
        sin = np.abs(cross) / (np.linalg.norm(prev, axis=1) * np.linalg.norm(d, axis=1))
        straight = (sin < _COLLINEAR_SIN) & ((prev * d).sum(axis=1) > 0)
        if not straight.any():
            return p
        p = p[~straight]
    return p


def _fit_circle(p: np.ndarray) -> tuple[np.ndarray, float]:
    x, y = p[:, 0], p[:, 1]
    M = np.stack([x, y, np.ones_like(x)], axis=1)
    sol, *_ = np.linalg.lstsq(M, -(x**2 + y**2), rcond=None)
    c = np.array([-sol[0] / 2, -sol[1] / 2])
    r2 = float(c @ c - sol[2])
    return c, math.sqrt(r2) if r2 > 0 else 0.0


def _cross2(a: np.ndarray, b: np.ndarray) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _convex_hull(p: np.ndarray) -> np.ndarray:
    pts = np.unique(p, axis=0)
    if len(pts) < 3:
        return pts

    def half(points: np.ndarray) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        for q in points:
            while len(out) >= 2 and _cross2(out[-1] - out[-2], q - out[-2]) <= 0:
                out.pop()
            out.append(q)
        return out

    lower, upper = half(pts), half(pts[::-1])
    return np.array(lower[:-1] + upper[:-1])


def _min_area_rect(p: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
    """最小の外接長方形の (中心, 長い辺の向き, 長い辺, 短い辺)。"""
    hull = _convex_hull(p)
    if len(hull) < 3:
        lo, hi = p.min(axis=0), p.max(axis=0)
        return (lo + hi) / 2, np.array([1.0, 0.0]), float(hi[0] - lo[0]), float(hi[1] - lo[1])
    edges = np.roll(hull, -1, axis=0) - hull
    lens = np.linalg.norm(edges, axis=1)
    dirs = edges[lens > 0] / lens[lens > 0, None]
    best = None
    for d in dirs:
        nrm = np.array([-d[1], d[0]])
        s, t = hull @ d, hull @ nrm
        area = float(np.ptp(s) * np.ptp(t))
        if best is None or area < best[0] - 1e-12 * max(area, 1.0):
            best = (area, d, nrm, s, t)
    _, d, nrm, s, t = best  # type: ignore[misc]
    c = d * (s.min() + s.max()) / 2 + nrm * (t.min() + t.max()) / 2
    w, h = float(np.ptp(s)), float(np.ptp(t))
    if h > w:
        w, h, d = h, w, nrm
    # 向きの符号をそろえる（最初の 0 でない成分を正）。
    d = d if (d[0] > 1e-12 or (abs(d[0]) <= 1e-12 and d[1] > 0)) else -d
    return c, d, w, h


def _point_segment_distance(c: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    t = np.clip(((c - a) * ab).sum(axis=1) / np.maximum((ab * ab).sum(axis=1), 1e-300), 0.0, 1.0)
    return np.linalg.norm(a + ab * t[:, None] - c, axis=1)
