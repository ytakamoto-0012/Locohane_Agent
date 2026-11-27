"""表面の解析（src/cad/analysis.py）・位置合わせと偏差（src/cad/align.py）・基準モデル（show_cad_viewer の reference）のテスト。"""

from __future__ import annotations

import base64
import numpy as np
import pytest

from src.cad import align as A
from src.cad.analysis import NORMAL_SCALE, mesh_curvatures, stl_analysis, vertex_normals, weld


def _dec(value: str, dtype: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(value), dtype=np.dtype(dtype).newbyteorder("<"))


def _icosphere(radius: float, levels: int) -> tuple[np.ndarray, np.ndarray]:
    """正二十面体を細かく分けた球（頂点を共有するメッシュ）。"""
    t = (1 + 5**0.5) / 2
    v = [[-1, t, 0], [1, t, 0], [-1, -t, 0], [1, -t, 0], [0, -1, t], [0, 1, t], [0, -1, -t], [0, 1, -t], [t, 0, -1], [t, 0, 1], [-t, 0, -1], [-t, 0, 1]]
    f = [[0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11], [1, 5, 9], [5, 11, 4], [11, 10, 2], [10, 7, 6], [7, 1, 8],
         [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9], [4, 9, 5], [2, 4, 11], [6, 2, 10], [8, 6, 7], [9, 8, 1]]
    verts = [np.array(p, float) / np.linalg.norm(p) for p in v]
    faces = f
    for _ in range(levels):
        cache: dict[tuple[int, int], int] = {}

        def mid(a: int, b: int) -> int:
            key = (min(a, b), max(a, b))
            if key not in cache:
                m = verts[a] + verts[b]
                verts.append(m / np.linalg.norm(m))
                cache[key] = len(verts) - 1
            return cache[key]

        nxt = []
        for a, b, c in faces:
            ab, bc, ca = mid(a, b), mid(b, c), mid(c, a)
            nxt += [[a, ab, ca], [b, bc, ab], [c, ca, bc], [ab, bc, ca]]
        faces = nxt
    return np.array(verts) * radius, np.array(faces, dtype=np.int64)


def _box(sx: float, sy: float, sz: float) -> tuple[np.ndarray, np.ndarray, list[list[int]], list[np.ndarray]]:
    """箱（面ごとに頂点を持つ 6 面・12 三角形）と、面ごとの三角形の範囲・外向きの法線。"""
    c = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * [sx, sy, sz]
    quads = [([0, 3, 2, 1], [0, 0, -1]), ([4, 5, 6, 7], [0, 0, 1]), ([0, 1, 5, 4], [0, -1, 0]), ([2, 3, 7, 6], [0, 1, 0]), ([1, 2, 6, 5], [1, 0, 0]), ([3, 0, 4, 7], [-1, 0, 0])]
    pos, tris, ranges, normals = [], [], [], []
    for q, n in quads:
        base = len(pos)
        pos += [c[i] for i in q]
        ranges.append([len(tris), 2])
        tris += [[base, base + 1, base + 2], [base, base + 2, base + 3]]
        normals.append(np.array(n, float))
    return np.array(pos), np.array(tris, dtype=np.int64), ranges, normals


# ---------------------------------------------------------------------------
# 曲率・法線
# ---------------------------------------------------------------------------


def test_mesh_curvature_of_a_sphere_is_one_over_radius_and_convex_positive() -> None:
    pos, tris = _icosphere(10.0, 4)
    n = vertex_normals(pos, tris)
    assert np.all(np.sum(n * pos, axis=1) > 0)  # 外向き
    k1, k2 = mesh_curvatures(pos, tris, n)
    assert np.median(k1) == pytest.approx(0.1, rel=0.02)
    assert np.median(k2) == pytest.approx(0.1, rel=0.02)
    # 内向きの法線（内側から見た球 = 凹）なら符号が反転する。
    k1i, k2i = mesh_curvatures(pos, tris[:, ::-1], -n)
    assert np.median(k1i) == pytest.approx(-0.1, rel=0.02)


def test_stl_analysis_welds_vertices_and_keeps_creases_flat() -> None:
    pos, tris, _ranges, normals = _box(10, 20, 30)
    soup = pos[tris].reshape(-1, 3)  # STL のように三角形ごとに 3 頂点
    upos, inv = weld(soup)
    assert len(upos) == 8 and len(inv) == len(soup)
    a = stl_analysis(soup, np.arange(len(soup), dtype=np.uint32))
    assert a is not None and a["method"] == "mesh"
    n = _dec(a["normals"], "i2").reshape(-1, 3) / NORMAL_SCALE
    # 箱の角は折れ目なので、表示用の法線はそれぞれの面の向きのまま（平均でぼかさない）。
    expect = np.repeat(np.array(normals), 6, axis=0)
    assert np.allclose(n, expect, atol=1e-4)


def test_cad_curvature_is_exact_and_signed_by_face_orientation() -> None:
    cq = pytest.importorskip("cadquery")
    from src.cad.export_mesh import Options, shape_to_viewer_data

    shape = cq.Workplane().box(40, 40, 10).faces(">Z").workplane().hole(10).edges("|Z").fillet(3)
    data = shape_to_viewer_data(shape.val(), Options(), {"kind": "code"})
    a = data["analysis"]
    assert a["method"] == "exact"
    k1, k2 = _dec(a["k1"], "f4"), _dec(a["k2"], "f4")
    kinds = {f["geomType"] for f in data["faces"]}
    assert {"PLANE", "CYLINDER"} <= kinds
    # 外側の丸め R3 は凸（+1/3）、穴 R5 の内面は凹（-1/5）、平面は 0。
    assert np.isclose(k1.max(), 1 / 3, atol=1e-6)
    assert np.isclose(k2.min(), -1 / 5, atol=1e-6)
    # 法線は面の表側（ソリッドの外側）を向く: 上面の頂点は +Z。
    n = _dec(a["normals"], "i2").reshape(-1, 3) / NORMAL_SCALE
    idx = _dec(data["mesh"]["indices"], "u4").reshape(-1, 3)
    top = next(f for f in data["faces"] if f["geomType"] == "PLANE" and np.allclose(f.get("normal", [0, 0, 0]), [0, 0, 1]))
    s, c = data["mesh"]["faceRanges"][top["id"]]
    assert np.allclose(n[np.unique(idx[s : s + c])], [0, 0, 1], atol=1e-4)
    # 解析を止めると付かない（比べる前の形・基準モデル）。
    assert "analysis" not in shape_to_viewer_data(shape.val(), Options(surface_analysis=False), {"kind": "code"})


# ---------------------------------------------------------------------------
# 最近点・偏差・位置合わせ
# ---------------------------------------------------------------------------


def test_closest_point_on_triangle_regions() -> None:
    a = np.array([[0.0, 0, 0]] * 4)
    b = np.array([[1.0, 0, 0]] * 4)
    c = np.array([[0.0, 1, 0]] * 4)
    p = np.array([[0.2, 0.2, 1], [-1, -1, 0], [0.5, -1, 0], [1, 1, 0]])
    out = A.closest_on_triangles(p, a, b, c)
    assert np.allclose(out, [[0.2, 0.2, 0], [0, 0, 0], [0.5, 0, 0], [0.5, 0.5, 0]])


def test_deviation_is_signed_outside_positive_including_edges_and_corners() -> None:
    pos, tris, ranges, normals = _box(100, 60, 20)
    index = A.SurfaceIndex(pos, tris)
    vnorm = np.repeat(np.array(normals), 4, axis=0)
    # 面の頂点を外へずらす: 角・稜線の上の点も、隣の面の三角形でなく自分の面で符号が決まる（+）。
    target = A.TargetMesh(pos + 0.3 * vnorm, tris, ranges)
    assert np.allclose(A.deviations(target, index, np.eye(4)), 0.3, atol=1e-5)
    # 面の中の点を内へずらす（角の点を内へずらすと隣の面の上に載るので、面の中心で確かめる）: −。
    centers = np.array([pos[tris[s : s + c].ravel()].mean(axis=0) for s, c in ranges])
    d, _near, _n = index.closest(centers - 0.3 * np.array(normals))
    assert np.allclose(d, -0.3, atol=1e-5)
    stats = A.deviation_stats(np.array([0.0, 0.05, 0.2, -0.2]), np.array([1.0, 1, 1, 1]), 0.1)
    assert stats["within_tolerance_ratio"] == 0.5 and stats["above_ratio"] == 0.25 and stats["below_ratio"] == 0.25


def _moved(T: np.ndarray, pos: np.ndarray) -> np.ndarray:
    return A.apply(np.linalg.inv(T), pos)


def test_bestfit_recovers_a_rigid_move() -> None:
    pos, tris = _icosphere(1.0, 3)
    pos = pos * [30, 15, 8]  # 3 軸の長さが違う楕円体（主軸で粗く合わせられる）
    index = A.SurfaceIndex(pos, tris)
    T_true = np.eye(4)
    T_true[:3, :3] = A.rotation(np.radians([10, -5, 40]))
    T_true[:3, 3] = [4, -2, 3]
    target = A.TargetMesh(_moved(T_true, pos), tris, [[0, len(tris)]])
    T, info = A.align(target, index, "bestfit")
    assert info["method"] == "bestfit" and info["rms"] < 1e-3
    d = A.deviations(target, index, T)
    assert np.abs(d).max() < 1e-2


def _pt(T_true: np.ndarray, p: list[float]) -> dict:
    """基準の座標の点 p を、対象（T_true で動かした形）の上の点のデータムにする。"""
    return {"type": "point", "point": _moved(T_true, np.array([p], float))[0].tolist()}


def test_point_datums_fix_dof_in_priority_order() -> None:
    pos, tris, ranges, _normals = _box(100, 60, 20)
    index = A.SurfaceIndex(pos, tris)
    # 対象の底面（A）だけを 0.5 下げた（反った）形: データム A で合わせると底面が基準に合い、上面が +0.5 ずれる。
    target_pos = pos.copy()
    bottom = np.arange(4)  # 底面の 4 頂点（_box の 1 つめの面）
    target_pos[bottom, 2] -= 0.5
    T_true = np.eye(4)
    T_true[:3, 3] = [0.3, -0.2, 0.4]
    target = A.TargetMesh(_moved(T_true, target_pos), tris, ranges)
    datums = {"A": _pt(T_true, [50, 30, -0.5]), "B": _pt(T_true, [50, 0, 10]), "C": _pt(T_true, [0, 30, 10])}
    T, info = A.align(target, index, "datum", start=np.eye(4), datums=datums, pad_radius=8)
    assert info["method"] == "datum" and info["datum_mode"] == "priority"
    assert info["datums"]["A"]["dof"] == 3 and info["datums"]["A"]["type"] == "point"
    d = A.deviations(target, index, T)
    top = target.triangles[ranges[1][0] : ranges[1][0] + 2].ravel()
    assert np.allclose(d[bottom], 0, atol=1e-4)
    assert np.allclose(d[top], 0.5, atol=1e-3)
    with pytest.raises(A.AlignError):
        A.align(target, index, "datum", start=np.eye(4), datums={})
    with pytest.raises(A.AlignError):
        A.align(target, index, "faces", start=np.eye(4), faces=[])


def test_point_datum_pad_uses_only_the_surface_connected_to_the_point() -> None:
    pos, tris, ranges, _normals = _box(100, 60, 2)  # 厚さ 2 の板
    target = A.TargetMesh(pos, tris, ranges)
    # 板の真ん中: 半径 5 の中には裏面（距離 2）も入るが、上面とは半径の外の側面を通じてしかつながらないので入らない。
    pts = target.pad_points(np.array([50.0, 30, 2]), 5.0, 500, np.random.default_rng(0))
    assert len(pts) > 100 and np.allclose(pts[:, 2], 2.0)
    assert np.all(np.linalg.norm(pts - [50, 30, 2], axis=1) <= 5 + 1e-9)


def _plate_with_holes(holes: list[tuple[float, float]], r: float = 5.0) -> tuple[np.ndarray, np.ndarray]:
    """z=0 の板（100×100）と、板から下へ深さ 10 の穴の壁（中心 holes、半径 r）。板の三角形は穴の上も覆う（試験用）。"""
    pos = [[0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0]]
    tris = [[0, 1, 2], [0, 2, 3]]
    seg = 64
    for cx, cy in holes:
        base = len(pos)
        for i in range(seg):
            a = 2 * np.pi * i / seg
            pos += [[cx + r * np.cos(a), cy + r * np.sin(a), 0], [cx + r * np.cos(a), cy + r * np.sin(a), -10]]
        for i in range(seg):
            j = (i + 1) % seg
            t0, b0, t1, b1 = base + 2 * i, base + 2 * i + 1, base + 2 * j, base + 2 * j + 1
            tris += [[t0, b0, b1], [t0, b1, t1]]
    return np.array(pos, float), np.array(tris, dtype=np.int64)


def _hole(T_true: np.ndarray, c: tuple[float, float], dia: float, pin: float) -> dict:
    center = _moved(T_true, np.array([[c[0], c[1], 0.0]]))[0]
    axis = np.linalg.inv(T_true)[:3, :3] @ [0, 0, 1]
    return {"type": "hole", "center": center.tolist(), "axis": axis.tolist(), "diameter": dia, "pin": pin}


def test_fit_reference_hole_finds_the_hole_center_and_radius() -> None:
    pos, tris = _plate_with_holes([(30, 40)])
    index = A.SurfaceIndex(pos, tris)
    got = A.fit_reference_hole(index, np.array([31.5, 39.0, 0]), np.array([0, 0, 1.0]), 5.0)
    assert got is not None
    c, r = got
    assert np.allclose(c, [30, 40, 0], atol=0.05) and r == pytest.approx(5.0, abs=0.05)
    # 穴の無いところでは見つからない。
    assert A.fit_reference_hole(index, np.array([70.0, 70, 0]), np.array([0, 0, 1.0]), 5.0) is None


def test_two_datum_holes_with_round_pins_recover_the_position() -> None:
    pos, tris = _plate_with_holes([(30, 40), (80, 40)])
    index = A.SurfaceIndex(pos, tris)
    T_true = np.eye(4)
    T_true[:3, :3] = A.rotation(np.radians([0, 0, 1.5]))
    T_true[:3, 3] = [1.2, -0.8, 0.3]
    target = A.TargetMesh(_moved(T_true, pos), tris, [[0, len(tris)]])
    datums = {
        "A": _pt(T_true, [55, 80, 0]),
        "B": _hole(T_true, (30, 40), 10.0, 10.0),  # 隙間なしのピン: 穴の中心をピンの軸に合わせる
        "C": _hole(T_true, (80, 40), 10.0, 10.0),
    }
    T, info = A.align(target, index, "datum", start=np.eye(4), datums=datums, pad_radius=15)
    assert info["datums"]["B"]["type"] == "hole" and info["datums"]["B"]["offset"] < 1e-3
    assert np.allclose(T, T_true, atol=1e-3)
    with pytest.raises(A.AlignError):  # 基準穴は 2 本まで
        A.align(target, index, "datum", start=np.eye(4), datums={**datums, "D": _hole(T_true, (30, 40), 10.0, 10.0)})


def test_datum_hole_pin_with_clearance_stops_at_the_pin() -> None:
    pos, tris = _plate_with_holes([(30, 40)])
    index = A.SurfaceIndex(pos, tris)
    T_true = np.eye(4)
    T_true[:3, 3] = [2.0, 0, 0]  # 穴が x に 2 ずれた位置から
    target = A.TargetMesh(_moved(T_true, pos), tris, [[0, len(tris)]])
    datums = {"A": _pt(T_true, [60, 80, 0]), "B": _hole(T_true, (30, 40), 10.0, 9.0)}  # 直径 9 のピン: 隙間 0.5
    T, info = A.align(target, index, "datum", start=np.eye(4), datums=datums, pad_radius=15)
    hole = info["datums"]["B"]
    assert hole["clearance"] == pytest.approx(0.5) and hole["offset"] == pytest.approx(0.5, abs=0.02)
    # 隙間の中なら動かさない。
    T_true[:3, 3] = [0.3, 0, 0]
    target = A.TargetMesh(_moved(T_true, pos), tris, [[0, len(tris)]])
    datums = {"A": _pt(T_true, [60, 80, 0]), "B": _hole(T_true, (30, 40), 10.0, 9.0)}
    T, info = A.align(target, index, "datum", start=np.eye(4), datums=datums, pad_radius=15)
    assert info["datums"]["B"]["offset"] == pytest.approx(0.3, abs=0.02)


def test_datum_d_by_priority_or_average() -> None:
    pos, tris, ranges, _normals = _box(100, 60, 20)
    index = A.SurfaceIndex(pos, tris)
    target_pos = pos.copy()
    target_pos[np.arange(4), 2] -= 0.5  # 底面が反っている
    target = A.TargetMesh(target_pos, tris, ranges)
    datums = {
        "A": {"type": "point", "point": [50, 30, -0.5]},
        "B": {"type": "point", "point": [50, 0, 10]},
        "C": {"type": "point", "point": [0, 30, 10]},
        "D": {"type": "point", "point": [50, 30, 20]},  # 上面
    }
    T, info = A.align(target, index, "datum", start=np.eye(4), datums=datums, pad_radius=8)
    # 優先: D は最後の段（C の 1 自由度）にだけ効くので、A で決めた高さ（底面が基準に合う）は変わらない。
    assert info["datums"]["D"]["weight"] == pytest.approx(A.DATUM_D_WEIGHT)
    d = A.deviations(target, index, T)
    assert np.allclose(d[np.arange(4)], 0, atol=1e-3)
    # 平均: 底面と上面の両方に同じ重みで当てるので、反りを上下に分け合う。
    T, info = A.align(target, index, "datum", start=np.eye(4), datums=datums, pad_radius=8, datum_mode="average")
    assert info["datum_mode"] == "average"
    d = A.deviations(target, index, T)
    assert 0.05 < float(np.abs(d[np.arange(4)]).mean()) < 0.45


def test_as_matrix_rejects_non_rigid_or_broken_transforms() -> None:
    assert A.as_matrix(np.eye(4).tolist()) is not None
    assert A.as_matrix([[2, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]) is None
    assert A.as_matrix([[1, 0, 0]]) is None
    assert A.as_matrix("x") is None


def test_coarse_candidates_are_rotations_whatever_the_eigenvector_handedness() -> None:
    # eigh の固有ベクトルの並びが左手系だと、符号の積が +1 の 4 通りがすべて鏡映になり候補が 0 個になっていた
    pos, tris, _ranges, _normals = _box(10, 5, 2)
    index = A.SurfaceIndex(pos, tris)
    rng = np.random.default_rng(0)
    src = index.sample_points(3000, rng)
    for flip in (np.diag([1.0, 1.0, 1.0]), np.diag([1.0, 1.0, -1.0])):
        cands = A.coarse_candidates(src @ flip, index, rng)
        assert len(cands) == 4
        for T in cands:
            assert np.linalg.det(T[:3, :3]) > 0


def test_weld_treats_negative_zero_as_the_same_vertex() -> None:
    pos = np.array([[0.0, 1.0, 2.0], [-0.0, 1.0, 2.0], [1.0, -0.0, 0.0], [1.0, 0.0, -0.0]], dtype=np.float32)
    upos, inv = weld(pos)
    assert len(upos) == 2
    assert inv[0] == inv[1] and inv[2] == inv[3]
