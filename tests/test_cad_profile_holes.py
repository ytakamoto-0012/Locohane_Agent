"""丸以外も含む穴の形の認識（src/cad/profile_holes.py）と、その基準穴での位置合わせ（src/cad/align.py）のテスト。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.cad import align as A
from src.cad.features import summarize_features
from src.cad.profile_holes import classify_profile, profile_sdf


def _rot(p: np.ndarray, deg: float, shift: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
    t = math.radians(deg)
    R = np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
    return p @ R.T + np.array(shift)


def _rounded_rect(w: float, h: float, r: float, seg: int = 8) -> np.ndarray:
    """角 R の四角の輪郭（角の弧は seg 分割。r = 0 なら四角）。辺の途中にも点を置く（メッシュの点の代わり）。"""
    a, b = w / 2, h / 2
    if r == 0:
        out = np.array([[a, b], [-a, b], [-a, -b], [a, -b]])
    else:
        pts: list[list[float]] = []
        for cx, cy, start in [(a - r, b - r, 0), (-a + r, b - r, 90), (-a + r, -b + r, 180), (a - r, -b + r, 270)]:
            for i in range(seg + 1):
                t = math.radians(start + 90 * i / seg)
                pts.append([cx + r * math.cos(t), cy + r * math.sin(t)])
        out = np.array(pts)
    # 辺の中点も足す（一直線に並ぶ点は除かれることを確かめる）。
    dense = []
    for i in range(len(out)):
        p, q = out[i], out[(i + 1) % len(out)]
        dense += [p, (p + q) / 2]
    return np.array(dense)


def _regular(n: int, across_corners: float, seg_per_side: int = 1) -> np.ndarray:
    r = across_corners / 2
    pts = []
    for k in range(n):
        p = np.array([r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n)])
        q = np.array([r * math.cos(2 * math.pi * (k + 1) / n), r * math.sin(2 * math.pi * (k + 1) / n)])
        for s in range(seg_per_side):
            pts.append(p + (q - p) * s / seg_per_side)
    return np.array(pts)


# ---------------------------------------------------------------------------
# 輪郭の形
# ---------------------------------------------------------------------------


def test_classify_rectangle_and_rounded_rectangle_in_any_rotation() -> None:
    got = classify_profile(_rot(_rounded_rect(20, 10, 0), 30, (5, -3)))
    assert got["shape"] == "rectangle" and got["width"] == pytest.approx(20) and got["height"] == pytest.approx(10)
    assert np.allclose(got["center2"], [5, -3], atol=1e-6)
    assert abs(abs(float(np.dot(got["x2"], [math.cos(math.radians(30)), math.sin(math.radians(30))]))) - 1) < 1e-6
    got = classify_profile(_rot(_rounded_rect(24, 12, 3, seg=12), -20))
    assert got["shape"] == "roundedRectangle"
    assert got["width"] == pytest.approx(24, abs=1e-3) and got["height"] == pytest.approx(12, abs=1e-3)
    assert got["cornerRadius"] == pytest.approx(3, abs=1e-3) and got["inscribedDiameter"] == pytest.approx(12, abs=1e-3)


@pytest.mark.parametrize("n", [5, 6, 7, 8])
def test_classify_regular_polygons(n: int) -> None:
    got = classify_profile(_rot(_regular(n, 16, seg_per_side=3), 11, (2, 2)))
    assert got["shape"] == "polygon" and got["sides"] == n
    assert got["acrossCorners"] == pytest.approx(16, abs=1e-3)
    assert got["acrossFlats"] == pytest.approx(16 * math.cos(math.pi / n), abs=1e-3)
    assert np.allclose(got["center2"], [2, 2], atol=1e-6)


def test_classify_circle_from_a_fine_polygon_and_other_shapes() -> None:
    got = classify_profile(_regular(48, 10) + 3)
    assert got["shape"] == "circle" and got["diameter"] == pytest.approx(10, abs=1e-3)
    # 辺の長さがそろわない八角形（角を落とした四角）は正多角形でも四角でもない。
    p = np.array([[4, 0], [10, 0], [14, 4], [14, 6], [10, 10], [4, 10], [0, 6], [0, 4]], float)
    assert classify_profile(p)["shape"] == "other"
    # 三角形も対象外。
    assert classify_profile(np.array([[0, 0], [10, 0], [5, 8]], float))["shape"] == "other"


def test_profile_sdf_is_negative_inside_and_matches_the_sides() -> None:
    rect = {"shape": "rectangle", "width": 20, "height": 10}
    assert np.allclose(profile_sdf(rect, np.array([[0, 0], [10, 0], [0, 7], [12, 0]])), [-5, 0, 2, 2])
    rr = {"shape": "roundedRectangle", "width": 20, "height": 10, "cornerRadius": 3}
    corner = np.array([[10 - 3 + 3 / math.sqrt(2), 5 - 3 + 3 / math.sqrt(2)]])
    assert profile_sdf(rr, corner)[0] == pytest.approx(0, abs=1e-9)
    hexagon = {"shape": "polygon", "sides": 6, "acrossFlats": 10 * math.sqrt(3)}
    # 頂点は +x の上（中心から 10）、辺の中点は 30° の向き（中心から 5√3）。
    pts = np.array([[10, 0], [5 * math.sqrt(3) * math.cos(math.pi / 6), 5 * math.sqrt(3) * math.sin(math.pi / 6)]])
    assert np.allclose(profile_sdf(hexagon, pts), 0, atol=1e-9)


# ---------------------------------------------------------------------------
# ソリッド・閉じたメッシュの穴
# ---------------------------------------------------------------------------


def _plate(cq):
    return (
        cq.Workplane("XY").box(120, 60, 10)
        .faces(">Z").workplane().pushPoints([(-45, 0)]).rect(10, 20).cutThruAll()
        .faces(">Z").workplane().pushPoints([(-15, 0)]).sketch().rect(12, 24).vertices().fillet(3).finalize().cutThruAll()
        .faces(">Z").workplane().pushPoints([(15, 0)]).polygon(6, 16).cutThruAll()
        .faces(">Z").workplane().pushPoints([(40, 0)]).hole(6)
        .faces(">Z").workplane().pushPoints([(-45, 22)]).rect(8, 8).cutBlind(-4)
        # 上に立つ四角のボス（穴ではない）。
        .faces(">Z").workplane().pushPoints([(40, 20)]).rect(6, 6).extrude(5)
    )


def _by_shape(features: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in features:
        out.setdefault(f.get("shape", "circle"), []).append(f)
    return out


def _check_plate_features(features: list[dict]) -> None:
    holes = [f for f in features if f["type"] == "hole"]
    assert len(holes) == 5
    s = _by_shape(holes)
    rect = sorted(s["rectangle"], key=lambda f: f["width"])
    assert rect[0]["width"] == pytest.approx(8) and rect[0]["through"] is False and rect[0]["depth"] == pytest.approx(4)
    assert rect[1]["width"] == pytest.approx(20) and rect[1]["height"] == pytest.approx(10) and rect[1]["through"] is True
    assert np.allclose(rect[1]["axisStart"], [-45, 0, -5], atol=1e-3) and np.allclose(rect[1]["axisEnd"], [-45, 0, 5], atol=1e-3)
    assert abs(rect[1]["xDirection"][1]) == pytest.approx(1)  # 長い辺（20）は y の向き
    rr = s["roundedRectangle"][0]
    assert (rr["width"], rr["height"], rr["cornerRadius"]) == pytest.approx((24, 12, 3), abs=1e-3)
    hexagon = s["polygon"][0]
    assert hexagon["sides"] == 6 and hexagon["acrossCorners"] == pytest.approx(16, abs=1e-3) and hexagon["depth"] == pytest.approx(10)
    assert s["circle"][0]["diameter"] == pytest.approx(6, abs=1e-3)
    assert len({f["id"] for f in features}) == len(features)


def test_solid_holes_of_every_shape_are_recognized_once() -> None:
    cq = pytest.importorskip("cadquery")
    from src.cad.export_mesh import Options, shape_to_viewer_data

    data = shape_to_viewer_data(_plate(cq).val(), Options(), {"kind": "code"})
    _check_plate_features(data["features"])
    # 丸穴は円柱面の認識（shape なし）のまま、重ねて出さない。
    assert [f for f in data["features"] if f["type"] == "hole" and "shape" not in f][0]["diameter"] == pytest.approx(6)
    # 壁の面に穴の ID が付く。
    hexagon = _by_shape(data["features"])["polygon"][0]
    assert all(data["faces"][i]["feature"] == hexagon["id"] for i in hexagon["faces"])


def test_closed_mesh_holes_of_every_shape_are_recognized(tmp_path) -> None:
    cq = pytest.importorskip("cadquery")
    from src.cad.mesh_data import stl_to_viewer_data

    path = tmp_path / "plate.stl"
    cq.exporters.export(_plate(cq), str(path), tolerance=0.01, angularTolerance=0.1)
    data = stl_to_viewer_data(path, max_triangles=1_000_000)
    _check_plate_features(data["features"])


def test_summary_groups_holes_by_shape_and_size() -> None:
    feats = [
        {"type": "hole", "diameter": 5.0, "depth": 10.0, "through": True},
        {"type": "hole", "shape": "rectangle", "width": 20.0, "height": 10.0, "depth": 10.0, "through": True},
        {"type": "hole", "shape": "rectangle", "width": 20.0, "height": 10.0, "depth": 10.0, "through": True},
        {"type": "hole", "shape": "polygon", "sides": 6, "acrossFlats": 13.8564, "depth": 10.0, "through": True},
    ]
    groups = summarize_features(feats)["groups"]
    rect = next(g for g in groups if g.get("shape") == "rectangle")
    assert rect["count"] == 2 and rect["width"] == 20.0 and rect["height"] == 10.0
    assert next(g for g in groups if "shape" not in g)["diameter"] == 5.0
    assert next(g for g in groups if g.get("shape") == "polygon")["sides"] == 6


# ---------------------------------------------------------------------------
# 丸以外の基準穴での位置合わせ
# ---------------------------------------------------------------------------


def _plate_with_profile_holes(outlines: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """z=0 の板（100×100）と、板から下へ深さ 10 の穴の壁（outlines の輪郭）。板の三角形は穴の上も覆う（試験用）。"""
    pos = [[0, 0, 0], [100, 0, 0], [100, 100, 0], [0, 100, 0]]
    tris = [[0, 1, 2], [0, 2, 3]]
    for out in outlines:
        base = len(pos)
        k = len(out)
        for x, y in out:
            pos += [[x, y, 0], [x, y, -10]]
        for i in range(k):
            j = (i + 1) % k
            t0, b0, t1, b1 = base + 2 * i, base + 2 * i + 1, base + 2 * j, base + 2 * j + 1
            tris += [[t0, b0, b1], [t0, b1, t1]]
    return np.array(pos, float), np.array(tris, dtype=np.int64)


def _dense(outline: np.ndarray, per_side: int = 12) -> np.ndarray:
    pts = []
    for i in range(len(outline)):
        p, q = outline[i], outline[(i + 1) % len(outline)]
        pts += [p + (q - p) * s / per_side for s in range(per_side)]
    return np.array(pts)


def _profile_hole(T_true: np.ndarray, c: tuple[float, float], xdir: list[float], profile: dict, pin: float) -> dict:
    center = A.apply(np.linalg.inv(T_true), np.array([[c[0], c[1], 0.0]]))[0]
    R = np.linalg.inv(T_true)[:3, :3]
    return {
        "type": "hole",
        "center": center.tolist(),
        "axis": (R @ [0, 0, 1]).tolist(),
        "diameter": 1.0,  # 形があれば内接円の直径に置き換わる
        "pin": pin,
        "profile": {**profile, "xDirection": (R @ np.array(xdir, float)).tolist()},
    }


def _point(T_true: np.ndarray, p: list[float]) -> dict:
    return {"type": "point", "point": A.apply(np.linalg.inv(T_true), np.array([p], float))[0].tolist()}


def test_fit_reference_profile_finds_rect_and_hexagon_centers() -> None:
    rect = _dense(_rot(np.array([[10, 5], [-10, 5], [-10, -5], [10, -5]], float), 0, (30, 40)))
    hexagon = _dense(_regular(6, 16) + [70, 40])
    pos, tris = _plate_with_profile_holes([rect, hexagon])
    index = A.SurfaceIndex(pos, tris)
    rp = {"shape": "rectangle", "width": 20, "height": 10}
    c = A.fit_reference_profile(index, np.array([31.0, 39.2, 0]), np.array([0, 0, 1.0]), np.array([1.0, 0, 0]), rp)
    assert c is not None and np.allclose(c, [30, 40, 0], atol=0.02)
    hp = {"shape": "polygon", "sides": 6, "acrossFlats": 8 * math.sqrt(3)}
    c = A.fit_reference_profile(index, np.array([71.0, 41.0, 0]), np.array([0, 0, 1.0]), np.array([1.0, 0, 0]), hp)
    assert c is not None and np.allclose(c, [70, 40, 0], atol=0.02)
    assert A.fit_reference_profile(index, np.array([50.0, 85, 0]), np.array([0, 0, 1.0]), np.array([1.0, 0, 0]), rp) is None


def test_datum_holes_of_rect_and_hexagon_recover_the_position() -> None:
    rect = _dense(np.array([[10, 5], [-10, 5], [-10, -5], [10, -5]], float) + [30, 40])
    hexagon = _dense(_regular(6, 16) + [75, 40])
    pos, tris = _plate_with_profile_holes([rect, hexagon])
    index = A.SurfaceIndex(pos, tris)
    T_true = np.eye(4)
    T_true[:3, :3] = A.rotation(np.radians([0, 0, 1.0]))
    T_true[:3, 3] = [0.8, -0.6, 0.2]
    target = A.TargetMesh(A.apply(np.linalg.inv(T_true), pos), tris, [[0, len(tris)]])
    rp = {"shape": "rectangle", "width": 20, "height": 10}
    hp = {"shape": "polygon", "sides": 6, "acrossFlats": 8 * math.sqrt(3)}
    datums = {
        "A": _point(T_true, [50, 85, 0]),
        # ピンが穴の一番狭い向き（10・対辺）と同じ太さ: 四角は長い向きにだけ動け、六角は中心に決まる。
        "B": _profile_hole(T_true, (75, 40), [1, 0, 0], hp, 8 * math.sqrt(3)),
        "C": _profile_hole(T_true, (30, 40), [1, 0, 0], rp, 10.0),
    }
    T, info = A.align(target, index, "datum", start=np.eye(4), datums=datums, pad_radius=15)
    assert info["datums"]["B"]["shape"] == "polygon" and info["datums"]["B"]["offset"] < 1e-2
    assert info["datums"]["C"]["shape"] == "rectangle" and info["datums"]["C"]["diameter"] == pytest.approx(10)
    assert np.allclose(T, T_true, atol=5e-3)


def test_rect_datum_hole_leaves_the_long_direction_free() -> None:
    rect = _dense(np.array([[10, 5], [-10, 5], [-10, -5], [10, -5]], float) + [30, 40])
    pos, tris = _plate_with_profile_holes([rect])
    index = A.SurfaceIndex(pos, tris)
    T_true = np.eye(4)
    T_true[:3, 3] = [3.0, 2.0, 0]  # 長い向き（x）に 3、短い向き（y）に 2 ずれた位置から
    target = A.TargetMesh(A.apply(np.linalg.inv(T_true), pos), tris, [[0, len(tris)]])
    rp = {"shape": "rectangle", "width": 20, "height": 10}
    datums = {"A": _point(T_true, [60, 80, 0]), "B": _profile_hole(T_true, (30, 40), [1, 0, 0], rp, 6.0)}
    T, info = A.align(target, index, "datum", start=np.eye(4), datums=datums, pad_radius=15)
    # ピン ⌀6 は四角 20×10 の中で x に ±7、y に ±2 動ける: 対象の穴（基準の座標で (27, 38)）は、y は縁（2）に当たる
    # ところまでで、x の 3 は隙間の中なので、どちらもそのまま。
    moved = A.apply(T, A.apply(np.linalg.inv(T_true), np.array([[30.0, 40, 0]])))[0]
    assert moved[0] - 30 == pytest.approx(-3.0, abs=0.05)
    assert moved[1] - 40 == pytest.approx(-2.0, abs=0.05)
    # 短い向きに 3 ずれていれば、縁がピンに当たる 2 まで押し戻す。
    T_true[:3, 3] = [3.0, 3.0, 0]
    target = A.TargetMesh(A.apply(np.linalg.inv(T_true), pos), tris, [[0, len(tris)]])
    datums = {"A": _point(T_true, [60, 80, 0]), "B": _profile_hole(T_true, (30, 40), [1, 0, 0], rp, 6.0)}
    T, info = A.align(target, index, "datum", start=np.eye(4), datums=datums, pad_radius=15)
    moved = A.apply(T, A.apply(np.linalg.inv(T_true), np.array([[30.0, 40, 0]])))[0]
    # A と B だけでは z まわりの回転が決まらないので、x は隙間（±7）の中にあることだけを見る。
    assert -7 < moved[0] - 30 < 0
    assert moved[1] - 40 == pytest.approx(-2.0, abs=0.05)
    assert info["datums"]["B"]["clearance"] == pytest.approx(2.0)
