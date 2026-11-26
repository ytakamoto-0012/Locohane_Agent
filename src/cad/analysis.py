"""表面の解析（ゼブラ・曲率のカラーマップ）に使う、頂点ごとの法線と主曲率。numpy だけで動く。

ビューア用データ（src/cad/mesh_data.py）の ``analysis`` に入れる::

    "analysis": {
      "normals": <int16 base64、頂点ごとの単位法線 × 32767>,
      "k1": <float32 base64、頂点ごとの最大主曲率>, "k2": <float32 base64、最小主曲率>,
      "method": "exact" | "mesh" | "mixed", "summary": {...}
    }

曲率の符号は「外向きの法線に対して凸 = 正」（球の外面は +1/R、穴の内面は −1/R）。法線は面の表側
（ソリッドなら外側）を向く。B-rep（src/cad/export_mesh.py）は面ごとに OCP で正確な値を求めて ``exact_*`` で渡し、
STL や正確な値が求まらなかった頂点は、三角形の頂点の法線の差から推定する（Rusinkiewicz 2004
"Estimating Curvatures and Their Derivatives on Triangle Meshes"）。

頂点の並びはビューア用データの mesh と同じ（STL は三角形ごとに 3 頂点を持つので、同じ位置の頂点をまとめて
から計算し、元の並びへ戻す）。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .mesh_data import b64, rnd

# 法線を int16 にするときの倍率（ゼブラは法線のわずかな差でも縞が乱れるので、int8 では粗すぎる）。
NORMAL_SCALE = 32767
# STL の表示用の法線で、隣の三角形の向きとこれ以上違う角は折れ目として平らに描く（度）。
CREASE_ANGLE_DEG = 40.0
# 解析のデータを作る頂点数の上限（これを超えるモデルには付けない。ビューアはゼブラ・曲率を出さない）。
MAX_VERTICES = 6_000_000


def weld(positions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """同じ位置の頂点をまとめる。戻り値は (まとめた位置 (u,3), 元の頂点 → まとめた番号 (n,))。"""
    # + 0.0 で -0.0 を 0.0 にする（バイト列で比べるので、そのままだと同じ位置の 0.0 と -0.0 が別の頂点になる）。
    pos = np.ascontiguousarray(positions, dtype=np.float32) + np.float32(0.0)
    if not len(pos):
        return pos.reshape(0, 3), np.zeros(0, dtype=np.int64)
    # 3 つの float32 を 1 つの 12 バイトの値として比べる（np.unique の axis=0 より速い）。
    keys = pos.view(np.dtype((np.void, 12))).ravel()
    _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    return pos[first], inverse.astype(np.int64).ravel()


def face_normals(positions: np.ndarray, tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """三角形の (単位法線 (t,3), 面積 (t,))。潰れた三角形の法線は 0。"""
    p = positions[tris]
    cross = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    norm = np.linalg.norm(cross, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        n = np.where(norm[:, None] > 0, cross / norm[:, None], 0.0)
    return n, norm * 0.5


def vertex_normals(positions: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """頂点の法線（つながる三角形の法線を面積で重み付けして平均）。三角形の無い頂点は 0。"""
    fn, area = face_normals(positions, tris)
    acc = np.zeros((len(positions), 3))
    w = fn * area[:, None]
    for k in range(3):
        for axis in range(3):
            acc[:, axis] += np.bincount(tris[:, k], weights=w[:, axis], minlength=len(positions))
    return _unit(acc)


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, v / n, 0.0)


def _tangent_basis(normals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """法線に垂直な単位ベクトル 2 本（頂点ごと）。"""
    helper = np.where(np.abs(normals[:, :1]) < 0.9, np.array([[1.0, 0.0, 0.0]]), np.array([[0.0, 1.0, 0.0]]))
    u = _unit(np.cross(normals, helper))
    v = np.cross(normals, u)
    return u, v


def _rotate_into(u: np.ndarray, v: np.ndarray, new_n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """u・v の張る平面を new_n に垂直な平面へ回す（Rusinkiewicz の rotate_coord_sys）。"""
    old_n = np.cross(u, v)
    ndot = np.sum(old_n * new_n, axis=1, keepdims=True)
    flip = ndot[:, 0] <= -1.0 + 1e-9
    u = np.where(flip[:, None], -u, u)
    v = np.where(flip[:, None], -v, v)
    ndot = np.where(flip[:, None], -ndot, ndot)
    perp_old = new_n - ndot * old_n
    dperp = (old_n + new_n) / (1.0 + ndot)
    u2 = u - dperp * np.sum(perp_old * u, axis=1, keepdims=True)
    v2 = v - dperp * np.sum(perp_old * v, axis=1, keepdims=True)
    return u2, v2


def mesh_curvatures(positions: np.ndarray, tris: np.ndarray, normals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """三角形のメッシュから頂点ごとの主曲率 (k1 ≥ k2) を推定する（Rusinkiewicz 2004）。

    normals は頂点の単位法線（外向き）。三角形ごとに、3 辺に沿った法線の変化から第 2 基本形式を最小二乗で求め、
    頂点の接平面へ写して面積で重み付けして平均する。三角形の無い頂点は 0。
    """
    n_v = len(positions)
    k1 = np.zeros(n_v)
    k2 = np.zeros(n_v)
    if not len(tris) or not n_v:
        return k1, k2
    p = positions[tris].astype(np.float64)
    nn = normals[tris].astype(np.float64)
    e = np.stack([p[:, 2] - p[:, 1], p[:, 0] - p[:, 2], p[:, 1] - p[:, 0]], axis=1)  # (t,3,3)
    dn = np.stack([nn[:, 2] - nn[:, 1], nn[:, 0] - nn[:, 2], nn[:, 1] - nn[:, 0]], axis=1)
    fu = _unit(e[:, 0])
    fn_raw = np.cross(e[:, 0], e[:, 1])
    area2 = np.linalg.norm(fn_raw, axis=1)
    fn = _unit(fn_raw)
    fv = np.cross(fn, fu)
    eu = np.einsum("tkj,tj->tk", e, fu)
    ev = np.einsum("tkj,tj->tk", e, fv)
    du = np.einsum("tkj,tj->tk", dn, fu)
    dv = np.einsum("tkj,tj->tk", dn, fv)
    # 正規方程式 A^T A x = A^T b（x = [a, b, c]、II = [[a, b], [b, c]]）。
    m = np.zeros((len(tris), 3, 3))
    r = np.zeros((len(tris), 3))
    m[:, 0, 0] = np.sum(eu * eu, axis=1)
    m[:, 0, 1] = np.sum(eu * ev, axis=1)
    m[:, 1, 1] = np.sum(eu * eu + ev * ev, axis=1)
    m[:, 1, 2] = np.sum(eu * ev, axis=1)
    m[:, 2, 2] = np.sum(ev * ev, axis=1)
    m[:, 1, 0] = m[:, 0, 1]
    m[:, 2, 1] = m[:, 1, 2]
    r[:, 0] = np.sum(du * eu, axis=1)
    r[:, 1] = np.sum(du * ev + dv * eu, axis=1)
    r[:, 2] = np.sum(dv * ev, axis=1)
    good = (area2 > 0) & np.all(np.isfinite(m.reshape(len(tris), -1)), axis=1)
    det = np.linalg.det(m)
    good &= np.abs(det) > 1e-30
    x = np.zeros((len(tris), 3))
    if good.any():
        x[good] = np.linalg.solve(m[good], r[good][:, :, None])[:, :, 0]
    w_face = np.where(good, area2 / 6.0, 0.0)  # 三角形の面積の 1/3 を各頂点に

    vu, vv = _tangent_basis(normals.astype(np.float64))
    acc = np.zeros((n_v, 3))
    wsum = np.bincount(tris.ravel(), weights=np.repeat(w_face, 3), minlength=n_v)
    for k in range(3):
        idx = tris[:, k]
        ru, rv = _rotate_into(vu[idx], vv[idx], fn)
        u1 = np.sum(ru * fu, axis=1)
        v1 = np.sum(ru * fv, axis=1)
        u2 = np.sum(rv * fu, axis=1)
        v2 = np.sum(rv * fv, axis=1)
        a, b, c = x[:, 0], x[:, 1], x[:, 2]
        ku = a * u1 * u1 + b * 2 * u1 * v1 + c * v1 * v1
        kuv = a * u1 * u2 + b * (u1 * v2 + u2 * v1) + c * v1 * v2
        kv = a * u2 * u2 + b * 2 * u2 * v2 + c * v2 * v2
        for j, val in enumerate((ku, kuv, kv)):
            acc[:, j] += np.bincount(idx, weights=val * w_face, minlength=n_v)
    with np.errstate(invalid="ignore", divide="ignore"):
        acc = np.where(wsum[:, None] > 0, acc / wsum[:, None], 0.0)
    ku, kuv, kv = acc[:, 0], acc[:, 1], acc[:, 2]
    mean = (ku + kv) / 2
    root = np.sqrt(np.maximum(((ku - kv) / 2) ** 2 + kuv**2, 0.0))
    k1 = mean + root
    k2 = mean - root
    return k1, k2


# crease_normals で一度に扱う頂点の数（頂点ごとに「つながる三角形の数」の 2 乗の表を作るので、メモリーを抑える）。
_CREASE_CHUNK = 50_000


def crease_normals(
    face_n: np.ndarray, face_area: np.ndarray, corner_vertex: np.ndarray, smooth: np.ndarray, crease_deg: float = CREASE_ANGLE_DEG
) -> np.ndarray:
    """三角形の角ごとの表示用の法線（折れ目を残して滑らかにする）。

    角の法線は、その頂点につながる三角形のうち、その角の三角形と向きの差が crease_deg 以内のものの法線を面積で
    平均したもの（箱の角のように向きの違う面が集まる頂点では、面ごとに平らなまま）。corner_vertex は (t,3) の
    角 → まとめた頂点の番号。求まらない角（潰れた三角形）は smooth（頂点の平均の法線）。戻り値は (t,3,3)。
    """
    cos_lim = math.cos(math.radians(crease_deg))
    t = len(face_n)
    cv = corner_vertex.ravel()
    ct = np.repeat(np.arange(t), 3)
    order = np.argsort(cv, kind="stable")
    sv = cv[order]
    starts = np.flatnonzero(np.r_[True, sv[1:] != sv[:-1]]) if len(sv) else np.zeros(0, dtype=np.int64)
    counts = np.diff(np.r_[starts, len(sv)])
    weighted = face_n * face_area[:, None]
    out = np.zeros((len(cv), 3))
    # つながる三角形の数（次数）が同じ頂点をまとめて、次数 × 次数 の向きの比べ合いを一度に計算する。
    for d in np.unique(counts):
        group = starts[counts == d]
        step = max(1, _CREASE_CHUNK * 36 // int(d * d))
        for s in range(0, len(group), step):
            idx = order[group[s : s + step, None] + np.arange(d)]  # (n,d) の角
            tri = ct[idx]
            fn = face_n[tri]
            mask = (np.einsum("nik,njk->nij", fn, fn) >= cos_lim).astype(np.float64)
            out[idx] = np.einsum("nij,njk->nik", mask, weighted[tri])
    out = _unit(out).reshape(t, 3, 3)
    missing = np.linalg.norm(out, axis=2) < 0.5
    if missing.any():
        out[missing] = smooth[corner_vertex][missing]
    return out


def stl_analysis(positions: np.ndarray, indices: np.ndarray) -> dict[str, Any] | None:
    """STL（三角形ごとに 3 頂点を持つメッシュ）の解析のデータ。頂点が多すぎれば None。"""
    n = len(positions)
    if n == 0 or n > MAX_VERTICES:
        return None
    tris = indices.reshape(-1, 3).astype(np.int64)
    upos, inv = weld(positions)
    utris = inv[tris]
    smooth = vertex_normals(upos.astype(np.float64), utris)
    k1u, k2u = mesh_curvatures(upos, utris, smooth)
    fn, area = face_normals(upos.astype(np.float64), utris)
    corner = crease_normals(fn, area, utris, smooth)  # (t,3,3)
    normals = np.zeros((n, 3))
    normals[tris.ravel()] = corner.reshape(-1, 3)
    return pack(normals, k1u[inv], k2u[inv], "mesh")


def mesh_analysis(positions: np.ndarray, indices: np.ndarray, exact_normals: np.ndarray | None = None,
              exact_k: np.ndarray | None = None) -> dict[str, Any] | None:
    """B-rep を三角形にしたメッシュ（面ごとに頂点を持つ）の解析のデータ。

    exact_normals (n,3)・exact_k (n,2) は OCP で求めた正確な値（求まらなかった頂点は NaN）。NaN の頂点は
    メッシュから推定した値で埋める。
    """
    n = len(positions)
    if n == 0 or n > MAX_VERTICES:
        return None
    tris = indices.reshape(-1, 3).astype(np.int64)
    pos = positions.astype(np.float64)
    mesh_n = vertex_normals(pos, tris)
    normals = mesh_n
    if exact_normals is not None and len(exact_normals) == n:
        ok = np.all(np.isfinite(exact_normals), axis=1) & (np.linalg.norm(np.nan_to_num(exact_normals), axis=1) > 0.5)
        normals = np.where(ok[:, None], exact_normals, mesh_n)
    method = "mesh"
    if exact_k is not None and len(exact_k) == n:
        missing = ~np.all(np.isfinite(exact_k), axis=1)
        if not missing.any():
            k1, k2 = exact_k[:, 0], exact_k[:, 1]
            method = "exact"
        else:
            mk1, mk2 = mesh_curvatures(pos, tris, normals)
            k1 = np.where(missing, mk1, exact_k[:, 0])
            k2 = np.where(missing, mk2, exact_k[:, 1])
            method = "exact" if missing.mean() < 0.001 else "mixed"
    else:
        k1, k2 = mesh_curvatures(pos, tris, normals)
    return pack(normals, k1, k2, method)


def pack(normals: np.ndarray, k1: np.ndarray, k2: np.ndarray, method: str) -> dict[str, Any]:
    """ビューア用データの analysis にする（k1 ≥ k2 に並べ直す）。"""
    k1 = np.nan_to_num(np.asarray(k1, dtype=np.float64))
    k2 = np.nan_to_num(np.asarray(k2, dtype=np.float64))
    hi, lo = np.maximum(k1, k2), np.minimum(k1, k2)
    q = np.clip(np.rint(_unit(np.nan_to_num(normals)) * NORMAL_SCALE), -NORMAL_SCALE, NORMAL_SCALE).astype(np.int16)
    return {
        "normals": b64(q, "i2"),
        "k1": b64(hi, "f4"),
        "k2": b64(lo, "f4"),
        "method": method,
        "summary": curvature_summary(hi, lo),
    }


def curvature_summary(k1: np.ndarray, k2: np.ndarray) -> dict[str, Any]:
    """LLM へ返す曲率の概要（外れ値に引きずられないよう分位点で）。"""
    if not len(k1):
        return {}
    absmax = np.maximum(np.abs(k1), np.abs(k2))
    p99 = float(np.percentile(absmax, 99))
    return {
        "max_abs_curvature_p99": rnd(p99, 6),
        "min_radius_p99": rnd(1.0 / p99, 4) if p99 > 1e-12 else None,
        "flat_ratio": rnd(float(np.mean(absmax < 1e-6 + 1e-3 * max(p99, 1e-12))), 4),
        "concave_ratio": rnd(float(np.mean(k2 < -1e-3 * max(p99, 1e-12))), 4),
    }
