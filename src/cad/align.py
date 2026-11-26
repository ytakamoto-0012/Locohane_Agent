"""2 つのモデルの位置合わせと偏差（符号付き距離）。numpy と scipy（cKDTree）で動く（cadquery は要らない）。

show_cad_viewer の reference（基準モデル）で使う。ビューアに出しているモデル（対象。スキャンした STL 等）を
基準モデル（CAD の公称形状等）へ合わせる剛体の変換 T（4×4、対象の座標 → 基準の座標）を求め、対象の各頂点
p について、T·p から基準モデルの表面までの符号付き距離（基準の外向きの法線の側が正 = 肉が多い）を求める。

位置合わせのしかた:

- ``bestfit``: 対象全体を基準へ最小二乗で合わせる（point-to-plane の ICP。初めの位置は今の位置と、重心と主軸を
  合わせた 4 通りを試して、合いの良いものから詰める）。
- ``faces``: 選んだ面だけを使って合わせる（部分ベストフィット）。今の位置から詰める。
- ``datum``: 基準（データム）A・B・C の順に、各段で決める自由度の数を A=3・B=2・C=1（既定。3-2-1 の位置決め）に
  限って合わせる。先の段が決めた向きは後の段で動かさない（優先度付きの最小二乗）。検査具の当て面（A）・ピン穴（B）・
  長穴（C）で押さえたときの位置に当たる。
- ``none``: 合わせない（今の位置のまま偏差だけ求める）。

ビューアは座標を動かさずに基準モデルの方を T⁻¹ で動かして重ねる（選択・計測の座標が変わらないように）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from .mesh_data import rnd

# 基準モデルの表面に置く見本の点の間隔（基準の対角線に対する割合）と、見本の点の数の上限。
SAMPLE_SPACING_RATIO = 1 / 400
MAX_SAMPLES = 4_000_000
# 最近点の候補にする見本の点の数（その点が載る三角形を候補にして、正確な距離を比べる）。
NEIGHBOURS = 8
# 一度に最近点を求める点の数（メモリーを抑える）。
QUERY_CHUNK = 200_000
# ICP に使う対象の点の数（面積に比例してランダムに取る）。
ICP_POINTS = 20_000
COARSE_POINTS = 2_000
COARSE_ITERATIONS = 15
ICP_ITERATIONS = 60
# データムの既定の自由度（3-2-1）。
DATUM_DOF = {"A": 3, "B": 2, "C": 1}
METHODS = ("none", "bestfit", "faces", "datum")


class AlignError(ValueError):
    """位置合わせができない（点が足りない・基準モデルが空など）。"""


# ---------------------------------------------------------------------------
# 三角形への最近点
# ---------------------------------------------------------------------------


def closest_on_triangles(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """点 p (q,3) から三角形 (a,b,c)（それぞれ (q,3)）への最近点 (q,3)（Ericson, Real-Time Collision Detection 5.1.5）。"""
    ab, ac, ap = b - a, c - a, p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    vc = d1 * d4 - d3 * d2
    vb = d5 * d2 - d1 * d6
    va = d3 * d6 - d5 * d4
    with np.errstate(divide="ignore", invalid="ignore"):
        t_ab = d1 / (d1 - d3)
        t_ac = d2 / (d2 - d6)
        t_bc = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        denom = 1.0 / (va + vb + vc)
        v_in = vb * denom
        w_in = vc * denom
    conds = [
        (d1 <= 0) & (d2 <= 0),
        (d3 >= 0) & (d4 <= d3),
        (vc <= 0) & (d1 >= 0) & (d3 <= 0),
        (d6 >= 0) & (d5 <= d6),
        (vb <= 0) & (d2 >= 0) & (d6 <= 0),
        (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0),
    ]
    choices = [
        a,
        b,
        a + np.nan_to_num(t_ab)[:, None] * ab,
        c,
        a + np.nan_to_num(t_ac)[:, None] * ac,
        b + np.nan_to_num(t_bc)[:, None] * (c - b),
    ]
    inside = a + np.nan_to_num(v_in)[:, None] * ab + np.nan_to_num(w_in)[:, None] * ac
    out = inside.copy()
    # 先の条件ほど優先（np.select と同じ）。後ろから上書きする。
    for cond, choice in reversed(list(zip(conds, choices))):
        out = np.where(cond[:, None], choice, out)
    return out


class SurfaceIndex:
    """三角形のメッシュ（基準モデル）の表面への最近点を引く索引。"""

    def __init__(self, positions: np.ndarray, triangles: np.ndarray):
        from scipy.spatial import cKDTree

        pos = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
        tris = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
        if not len(tris) or not len(pos):
            raise AlignError("the reference model has no triangles")
        p = pos[tris]
        cross = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
        area2 = np.linalg.norm(cross, axis=1)
        keep = area2 > 0
        if not keep.any():
            raise AlignError("the reference model has only degenerate triangles")
        self.a, self.b, self.c = p[keep, 0], p[keep, 1], p[keep, 2]
        self.normals = cross[keep] / area2[keep, None]
        self.areas = area2[keep] / 2
        # 角の頂点の擬似法線（同じ位置の頂点をまとめ、つながる三角形の法線を面積で平均）。最近点が稜線・角の上にあって
        # 三角形の法線では符号が決まらないときに使う。
        from .analysis import vertex_normals, weld

        upos, inv = weld(pos.astype(np.float32))
        corner = inv[tris[keep]]
        vn = vertex_normals(upos.astype(np.float64), corner)
        self.corner_normals = vn[corner]  # (t,3,3)
        lo, hi = pos.min(axis=0), pos.max(axis=0)
        self.diag = float(np.linalg.norm(hi - lo)) or 1.0
        self.bbox = (lo, hi)
        samples, owner = self._samples()
        self.samples = samples
        self.owner = owner
        self.tree = cKDTree(samples)

    def _samples(self) -> tuple[np.ndarray, np.ndarray]:
        """三角形の上の見本の点（大きな三角形ほど多く）と、その点が載る三角形の番号。"""
        edge = np.max(
            np.stack(
                [
                    np.linalg.norm(self.b - self.a, axis=1),
                    np.linalg.norm(self.c - self.b, axis=1),
                    np.linalg.norm(self.a - self.c, axis=1),
                ]
            ),
            axis=0,
        )
        spacing = self.diag * SAMPLE_SPACING_RATIO
        for _ in range(8):
            n_sub = np.clip(np.ceil(edge / spacing), 1, 48).astype(np.int64)
            count = int(np.sum(np.where(n_sub == 1, 4, (n_sub + 1) * (n_sub + 2) // 2)))
            if count <= MAX_SAMPLES:
                break
            spacing *= 1.6
        pts: list[np.ndarray] = []
        own: list[np.ndarray] = []
        ids = np.arange(len(self.a))
        for n in np.unique(n_sub):
            sel = ids[n_sub == n]
            if n == 1:
                bary = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [1 / 3, 1 / 3, 1 / 3]])
            else:
                ii, jj = np.meshgrid(np.arange(n + 1), np.arange(n + 1), indexing="ij")
                m = (ii + jj) <= n
                u, v = ii[m] / n, jj[m] / n
                bary = np.stack([1 - u - v, u, v], axis=1)
            a, b, c = self.a[sel], self.b[sel], self.c[sel]
            block = bary[None, :, 0, None] * a[:, None] + bary[None, :, 1, None] * b[:, None] + bary[None, :, 2, None] * c[:, None]
            pts.append(block.reshape(-1, 3))
            own.append(np.repeat(sel, len(bary)))
        return np.concatenate(pts), np.concatenate(own)

    def closest(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """点 (q,3) の (符号付き距離 (q,), 最近点 (q,3), 最近点の三角形の外向きの法線 (q,3))。"""
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        dist = np.empty(len(pts))
        near = np.empty((len(pts), 3))
        normal = np.empty((len(pts), 3))
        k = min(NEIGHBOURS, len(self.samples))
        for s in range(0, len(pts), QUERY_CHUNK):
            q = pts[s : s + QUERY_CHUNK]
            _, idx = self.tree.query(q, k=k, workers=-1)
            idx = idx.reshape(len(q), k)
            cand = self.owner[idx]  # (m,k)
            flat = cand.ravel()
            qq = np.repeat(q, k, axis=0)
            cp = closest_on_triangles(qq, self.a[flat], self.b[flat], self.c[flat])
            diff_all = (qq - cp).reshape(len(q), k, 3)
            d2 = np.sum(diff_all**2, axis=2)
            dmin = d2.min(axis=1, keepdims=True)
            # 最近点が稜線・角の上にあると、同じ距離の三角形が複数ある。そのうち、ずれの向きと法線が最もそろうもの
            # （稜線の隣の面は向きが直交して符号が決まらない）を選ぶ。
            tie = d2 <= dmin * (1 + 1e-6) + (self.diag * 1e-9) ** 2
            align_score = np.abs(np.einsum("mkj,mkj->mk", diff_all, self.normals[cand])) / np.sqrt(np.maximum(d2, 1e-300))
            best = np.argmax(np.where(tie, align_score + 1.0, -1.0), axis=1)
            rows = np.arange(len(q))
            tri = cand[rows, best]
            cpb = cp.reshape(len(q), k, 3)[rows, best]
            n = self.normals[tri]
            diff = q - cpb
            d = np.sqrt(d2[rows, best])
            dot = np.einsum("ij,ij->i", diff, n)
            sign = np.sign(dot)
            # ずれの向きが三角形の面に沿う（最近点が稜線・角で、隣の面の三角形が候補に入らなかった）ときは、
            # 最近点に一番近い角の擬似法線で符号を決める。
            vague = np.abs(dot) < 0.3 * d
            if vague.any():
                vi = np.flatnonzero(vague)
                t_v = tri[vi]
                corners = np.stack([self.a[t_v], self.b[t_v], self.c[t_v]], axis=1)
                nearest = np.argmin(np.sum((corners - cpb[vi, None]) ** 2, axis=2), axis=1)
                pn = self.corner_normals[t_v, nearest]
                sign[vi] = np.sign(np.einsum("ij,ij->i", diff[vi], pn))
            # 面の上にほぼ載る点は符号を付けない（0）。
            dist[s : s + len(q)] = np.where(d > self.diag * 1e-12, sign * d, 0.0)
            near[s : s + len(q)] = cpb
            normal[s : s + len(q)] = n
        return dist, near, normal

    def sample_points(self, n: int, rng: np.random.Generator) -> np.ndarray:
        """表面から面積に比例してランダムに取った点（主軸を求めるため）。"""
        return sample_triangles(self.a, self.b, self.c, self.areas, n, rng)


def sample_triangles(a: np.ndarray, b: np.ndarray, c: np.ndarray, areas: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    """三角形 (a,b,c) の上に、面積に比例して一様に n 点取る。"""
    total = float(areas.sum())
    if n <= 0 or total <= 0:
        return np.zeros((0, 3))
    tri = rng.choice(len(areas), size=n, p=areas / total)
    r1 = np.sqrt(rng.random(n))
    r2 = rng.random(n)
    return (1 - r1)[:, None] * a[tri] + (r1 * (1 - r2))[:, None] * b[tri] + (r1 * r2)[:, None] * c[tri]


# ---------------------------------------------------------------------------
# 対象のメッシュ
# ---------------------------------------------------------------------------


@dataclass
class TargetMesh:
    """位置合わせする側（ビューアに出しているモデル）の三角形と、面ごとの三角形の範囲。"""

    positions: np.ndarray  # (n,3) ビューア用データの頂点の並び
    triangles: np.ndarray  # (t,3)
    face_ranges: Sequence[Sequence[int]]

    def __post_init__(self) -> None:
        self.positions = np.asarray(self.positions, dtype=np.float64).reshape(-1, 3)
        self.triangles = np.asarray(self.triangles, dtype=np.int64).reshape(-1, 3)
        p = self.positions[self.triangles]
        self.areas = np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1) / 2

    def triangles_of_faces(self, faces: Sequence[int]) -> np.ndarray:
        out: list[np.ndarray] = []
        for f in faces:
            if 0 <= f < len(self.face_ranges):
                s, c = int(self.face_ranges[f][0]), int(self.face_ranges[f][1])
                if c > 0:
                    out.append(np.arange(s, s + c))
        return np.concatenate(out) if out else np.zeros(0, dtype=np.int64)

    def sample(self, n: int, rng: np.random.Generator, triangles: np.ndarray | None = None) -> np.ndarray:
        tris = np.arange(len(self.triangles)) if triangles is None else triangles
        if not len(tris):
            return np.zeros((0, 3))
        p = self.positions[self.triangles[tris]]
        return sample_triangles(p[:, 0], p[:, 1], p[:, 2], self.areas[tris], n, rng)

    def vertex_weights(self) -> np.ndarray:
        """頂点ごとの重み（つながる三角形の面積の 1/3 の和）。偏差の統計を面積で重み付けするため。"""
        w = np.repeat(self.areas / 3, 3)
        return np.bincount(self.triangles.ravel(), weights=w, minlength=len(self.positions))


# ---------------------------------------------------------------------------
# 変換
# ---------------------------------------------------------------------------


def apply(T: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return pts @ T[:3, :3].T + T[:3, 3]


def rotation(omega: np.ndarray) -> np.ndarray:
    """回転ベクトル omega（向き = 軸、長さ = 角度 rad）の回転行列（Rodrigues）。"""
    theta = float(np.linalg.norm(omega))
    if theta < 1e-15:
        return np.eye(3)
    k = omega / theta
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(theta) * K + (1 - math.cos(theta)) * (K @ K)


def _increment(omega: np.ndarray, t: np.ndarray, center: np.ndarray) -> np.ndarray:
    """点 q を R(q − center) + center + t に動かす 4×4。"""
    R = rotation(omega)
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = center - R @ center + t
    return M


def as_matrix(raw: Any) -> np.ndarray | None:
    """4×4 の変換（入れ子のリスト）を検査して返す。壊れていれば None。"""
    try:
        T = np.array(raw, dtype=np.float64).reshape(4, 4)
    except (TypeError, ValueError):
        return None
    if not np.all(np.isfinite(T)) or not np.allclose(T[3], [0, 0, 0, 1], atol=1e-9):
        return None
    R = T[:3, :3]
    if not np.allclose(R @ R.T, np.eye(3), atol=1e-5) or np.linalg.det(R) < 0:
        return None
    return T


def describe_transform(T: np.ndarray) -> dict[str, Any]:
    """変換の要約（移動量と回転角・回転軸。LLM・ビューアに見せる）。"""
    R = T[:3, :3]
    angle = math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(R) - 1) / 2))))
    axis = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
    n = float(np.linalg.norm(axis))
    out: dict[str, Any] = {
        "matrix": [[rnd(v, 9) for v in row] for row in T],
        "translation": [rnd(v, 6) for v in T[:3, 3]],
        "rotation_deg": rnd(angle, 6),
    }
    if n > 1e-12:
        out["rotation_axis"] = [rnd(v, 6) for v in axis / n]
    return out


# ---------------------------------------------------------------------------
# ICP（point-to-plane、優先度付き）
# ---------------------------------------------------------------------------


def _solve_prioritized(rows: list[tuple[np.ndarray, np.ndarray, int]], scale: float) -> np.ndarray:
    """優先度付きの最小二乗。rows は段ごとの (ヤコビアン (n,6), 残差 (n,), 決める自由度の上限)。

    先の段で決めた向き（6 次元の変化の向き）は後の段で動かさない。戻り値は [ω (rad), t]（6,）。
    ヤコビアンの回転の列は scale（モデルの大きさ）で割って単位をそろえてある（未知数は ω·scale）。
    """
    Q = np.eye(6)
    x = np.zeros(6)
    for J, r, cap in rows:
        if Q.shape[1] == 0 or not len(r):
            continue
        res = r - J @ x
        M = J @ Q
        H = M.T @ M
        lam, V = np.linalg.eigh(H)
        order = np.argsort(lam)[::-1]
        lam, V = lam[order], V[:, order]
        if lam[0] <= 0:
            continue
        k = int(min(cap, Q.shape[1], np.sum(lam > lam[0] * 1e-6)))
        if k <= 0:
            continue
        Vk = V[:, :k]
        y = Vk @ ((Vk.T @ (M.T @ res)) / lam[:k])
        x = x + Q @ y
        Q = Q @ V[:, k:]
    omega = x[:3] / scale
    return np.concatenate([omega, x[3:]])


def _trim(d: np.ndarray, floor: float) -> np.ndarray:
    """外れた対応（測定の抜け・治具の写り込み等）を外す重み（0 か 1）。"""
    med = float(np.median(np.abs(d))) if len(d) else 0.0
    limit = max(3.0 * 1.4826 * med, floor)
    return (np.abs(d) <= limit).astype(np.float64)


def icp(
    index: SurfaceIndex,
    groups: list[tuple[np.ndarray, int]],
    T0: np.ndarray,
    iterations: int = ICP_ITERATIONS,
) -> tuple[np.ndarray, dict[str, Any]]:
    """groups（段ごとの (対象の点 (n,3)、決める自由度)）を基準の表面へ合わせる変換と、合いの指標。"""
    T = T0.copy()
    scale = index.diag / 2
    floor = index.diag * 1e-4
    it = 0
    for it in range(1, iterations + 1):
        rows = []
        all_q = [apply(T, pts) for pts, _ in groups]
        center = np.concatenate(all_q).mean(axis=0)
        for q, (_pts, cap) in zip(all_q, groups):
            d, _near, n = index.closest(q)
            w = _trim(d, floor)
            if w.sum() < 3:
                continue
            # 未知数は [ω·scale, t]（回転と移動の単位をそろえる）。n·(ω × a) = (a × n)·(ω·scale) / scale。
            arm = np.cross(q - center, n) / scale
            J = np.hstack([arm, n]) * w[:, None]
            rows.append((J, -d * w, cap))
        if not rows:
            raise AlignError("too few points correspond to the reference surface")
        step = _solve_prioritized(rows, scale)
        T = _increment(step[:3], step[3:], center) @ T
        if float(np.linalg.norm(step[:3])) * scale + float(np.linalg.norm(step[3:])) < index.diag * 1e-8:
            break
    pts = np.concatenate([apply(T, p) for p, _ in groups])
    d, _near, _n = index.closest(pts)
    inl = _trim(d, floor) > 0
    return T, {
        "iterations": it,
        "rms": rnd(float(np.sqrt(np.mean(d[inl] ** 2))) if inl.any() else float("nan"), 6),
        "inlier_ratio": rnd(float(inl.mean()), 4),
        "points": int(len(d)),
    }


def _pca(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    c = points.mean(axis=0)
    _, vecs = np.linalg.eigh(np.cov((points - c).T))
    return c, vecs[:, ::-1]


def coarse_candidates(source: np.ndarray, index: SurfaceIndex, rng: np.random.Generator) -> list[np.ndarray]:
    """重心と主軸を合わせた初めの位置の候補（主軸の向きの取り方 4 通り）。"""
    ref = index.sample_points(max(len(source), 2000), rng)
    cs, Es = _pca(source)
    cr, Er = _pca(ref)
    # eigh の固有ベクトルの並びは右手系とは限らない。両方を右手系にそろえないと、下の 4 通り（符号の積が +1）が
    # すべて鏡映（det < 0）になって捨てられ、初めの位置の候補が 1 つも無くなる。
    if np.linalg.det(Es) < 0:
        Es[:, 2] *= -1
    if np.linalg.det(Er) < 0:
        Er[:, 2] *= -1
    out = []
    for signs in ((1, 1, 1), (1, -1, -1), (-1, 1, -1), (-1, -1, 1)):
        R = Er @ np.diag(signs) @ Es.T
        if np.linalg.det(R) < 0:
            continue
        T = np.eye(4)
        T[:3, :3] = R
        T[:3, 3] = cr - R @ cs
        out.append(T)
    return out


def align(
    target: TargetMesh,
    index: SurfaceIndex,
    method: str,
    *,
    start: np.ndarray | None = None,
    faces: Sequence[int] = (),
    datums: dict[str, Sequence[int]] | None = None,
    datum_dof: dict[str, int] | None = None,
    seed: int = 0,
) -> tuple[np.ndarray, dict[str, Any]]:
    """位置合わせの変換（対象 → 基準）と、その記録（方法・合いの指標・使った面）。"""
    rng = np.random.default_rng(seed)
    T0 = np.eye(4) if start is None else start
    if method == "none":
        return T0, {"method": "none"}
    if method == "bestfit":
        src = target.sample(ICP_POINTS, rng)
        if len(src) < 6:
            raise AlignError("the model has no triangles to align")
        small = src[rng.choice(len(src), size=min(COARSE_POINTS, len(src)), replace=False)]
        best_T, best_score = T0, math.inf
        for cand in [T0, *coarse_candidates(src, index, rng)]:
            try:
                T, info = icp(index, [(small, 6)], cand, iterations=COARSE_ITERATIONS)
            except AlignError:
                continue
            d, _n, _m = index.closest(apply(T, small))
            score = float(np.median(np.abs(d)))
            if score < best_score:
                best_T, best_score = T, score
        T, info = icp(index, [(src, 6)], best_T)
        return T, {"method": "bestfit", **info}
    if method == "faces":
        tris = target.triangles_of_faces(faces)
        src = target.sample(ICP_POINTS, rng, tris)
        if len(src) < 6:
            raise AlignError("select faces of the model to align with (best fit on the selected faces)")
        T, info = icp(index, [(src, 6)], T0)
        return T, {"method": "faces", "faces": sorted({int(f) for f in faces})[:200], "face_count": len(set(faces)), **info}
    if method == "datum":
        dof = {**DATUM_DOF, **(datum_dof or {})}
        groups: list[tuple[np.ndarray, int]] = []
        used: dict[str, Any] = {}
        for name in ("A", "B", "C"):
            fs = list((datums or {}).get(name) or [])
            if not fs:
                continue
            cap = int(max(0, min(6, dof.get(name, 0))))
            pts = target.sample(max(ICP_POINTS // 3, 500), rng, target.triangles_of_faces(fs))
            if len(pts) < 3 or cap == 0:
                continue
            groups.append((pts, cap))
            used[name] = {"faces": sorted({int(f) for f in fs})[:200], "dof": cap}
        if not groups:
            raise AlignError("set the datum faces (A, and B / C if needed) before aligning by datums")
        T, info = icp(index, groups, T0)
        return T, {"method": "datum", "datums": used, **info}
    raise AlignError(f"unknown alignment method {method!r}")


# ---------------------------------------------------------------------------
# 偏差
# ---------------------------------------------------------------------------


def deviations(target: TargetMesh, index: SurfaceIndex, T: np.ndarray) -> np.ndarray:
    """対象の各頂点（ビューア用データの並び）の、基準の表面までの符号付き距離（float32）。"""
    from .analysis import weld

    upos, inv = weld(target.positions.astype(np.float32))
    d, _near, _n = index.closest(apply(T, upos.astype(np.float64)))
    return d[inv].astype(np.float32)


def deviation_stats(values: np.ndarray, weights: np.ndarray | None = None, tolerance: float | None = None) -> dict[str, Any]:
    """偏差の統計（面積で重み付け）。tolerance があれば ±tolerance に入る面積の割合も。"""
    v = np.asarray(values, dtype=np.float64)
    if not len(v):
        return {}
    w = np.ones_like(v) if weights is None or len(weights) != len(v) else np.asarray(weights, dtype=np.float64)
    if w.sum() <= 0:
        w = np.ones_like(v)
    mean = float(np.average(v, weights=w))
    out: dict[str, Any] = {
        "min": rnd(float(v.min()), 6),
        "max": rnd(float(v.max()), 6),
        "mean": rnd(mean, 6),
        "std": rnd(float(np.sqrt(np.average((v - mean) ** 2, weights=w))), 6),
        "rms": rnd(float(np.sqrt(np.average(v**2, weights=w))), 6),
        "abs_p95": rnd(float(_weighted_quantile(np.abs(v), w, 0.95)), 6),
    }
    if tolerance is not None and tolerance > 0:
        out["tolerance"] = rnd(tolerance, 6)
        out["within_tolerance_ratio"] = rnd(float(w[np.abs(v) <= tolerance].sum() / w.sum()), 4)
        out["above_ratio"] = rnd(float(w[v > tolerance].sum() / w.sum()), 4)
        out["below_ratio"] = rnd(float(w[v < -tolerance].sum() / w.sum()), 4)
    return out


def _weighted_quantile(v: np.ndarray, w: np.ndarray, q: float) -> float:
    order = np.argsort(v)
    cw = np.cumsum(w[order])
    i = int(np.searchsorted(cw, q * cw[-1]))
    return float(v[order][min(i, len(v) - 1)])


def face_deviation_stats(target: TargetMesh, values: np.ndarray, faces: Sequence[int], tolerance: float | None = None) -> dict[str, Any]:
    """選んだ面ごとの偏差の統計（面の頂点の値を面積で重み付け）。"""
    weights = target.vertex_weights()
    out: dict[str, Any] = {}
    for f in faces:
        tris = target.triangles_of_faces([f])
        if not len(tris):
            continue
        verts = np.unique(target.triangles[tris].ravel())
        out[str(int(f))] = deviation_stats(values[verts], weights[verts], tolerance)
    return out
