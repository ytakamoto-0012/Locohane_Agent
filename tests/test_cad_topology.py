"""開いた形の穴（トポロジートレランス）とローカル AXIS の検出（src/cad/topology.py）と、show_cad_viewer での扱い。

B-rep の確認は cadquery が入っているときだけ（無ければ skip）。
"""

from __future__ import annotations

import json
import math
import struct

import numpy as np
import pytest

from src.cad.mesh_data import stl_to_viewer_data
from src.cad.topology import attach_topology, find_local_axes, to_local_point, to_local_vector
from src.cad.viewer_package import read_package, write_package
from src.tools import cad_viewer


def _plate(n: int, size: float, *, z: float = 0.0, x0: float = 0.0, holes=(), flip: bool = False) -> list[np.ndarray]:
    """size 角の板を n×n の升目の三角形で作る。holes の (x, y, r) の円に中心が入る升目は抜く。"""
    h = size / n
    tris = []
    for i in range(n):
        for j in range(n):
            cx, cy = x0 + (i + 0.5) * h, (j + 0.5) * h
            if any((cx - hx) ** 2 + (cy - hy) ** 2 < r * r for hx, hy, r in holes):
                continue

            def p(a: int, b: int) -> list[float]:
                return [x0 + a * h, b * h, z]

            t1, t2 = [p(i, j), p(i + 1, j), p(i + 1, j + 1)], [p(i, j), p(i + 1, j + 1), p(i, j + 1)]
            if flip:
                t1, t2 = t1[::-1], t2[::-1]
            tris += [np.array(t1), np.array(t2)]
    return tris


def _write_stl(path, tris) -> None:
    with path.open("wb") as f:
        f.write(b"\0" * 80)
        f.write(struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<12fH", 0, 0, 0, *np.asarray(t).ravel(), 0))


def _stl_data(tmp_path, tris, **options):
    path = tmp_path / "m.stl"
    _write_stl(path, tris)
    data = stl_to_viewer_data(path, max_triangles=0)
    attach_topology(data, options)
    assert "topologyError" not in data
    return data


def test_holes_in_an_open_stl_plate_have_center_normal_and_diameter(tmp_path) -> None:
    top = _plate(50, 100, holes=[(20, 20, 5), (70, 30, 8)])
    # 下向きの面の板（三角形の回り方が逆）にも穴を 1 つ。法線は面の表側（-Z）を向く。
    bottom = [t + [0, 0, 10] for t in _plate(40, 100, holes=[(40, 40, 7)], flip=True)]
    data = _stl_data(tmp_path, top + bottom)
    holes = sorted(data["holeList"], key=lambda h: (h["center"][2], h["center"][0]))
    assert len(holes) == 3
    assert data["holes"]["outerLoops"] == 2  # 板ごとの外周は穴にしない
    assert data["stats"]["holes"] == 3
    # 外周は別の一覧（選べる種類「外周」）に、穴と同じ形の情報と線分で入る。
    outers = data["outerList"]
    assert len(outers) == 2 and data["stats"]["outers"] == 2
    assert {o["kind"] for o in outers} == {"outer"}
    assert sorted(round(o["perimeter"]) for o in outers) == [400, 400]
    assert len(data["outers"]["ranges"]) == 2
    a, b, c = holes
    assert a["center"][:2] == pytest.approx([20, 20], abs=0.5) and a["normal"] == pytest.approx([0, 0, 1])
    assert b["center"][:2] == pytest.approx([70, 30], abs=0.5)
    assert c["center"][2] == pytest.approx(10) and c["normal"] == pytest.approx([0, 0, -1])
    assert a["kind"] == "hole" and a["faces"] and a["diameter"] > 0 and a["perimeter"] > 0
    # 穴の縁の線分（ビューア用）は holes.ranges の範囲に並ぶ。
    assert len(data["holes"]["ranges"]) == 3 and all(n >= 3 for _, n in data["holes"]["ranges"])


@pytest.mark.parametrize(("gap", "outer"), [(0.0, 1), (0.05, 1), (0.5, 2)])
def test_gaps_up_to_the_topology_tolerance_count_as_connected(tmp_path, gap, outer) -> None:
    # 左右の板の境目の分け方が違う（点が合わない）ので、境目は許容差で突き合わせないとつながらない。
    left = _plate(25, 50, holes=[(20, 20, 5)])
    right = [t + [0, 0, 0] for t in _plate(17, 50, x0=50 + gap)]
    data = _stl_data(tmp_path, left + right, hole_tolerance=0.1)
    # 0.1 以下の隙間は 1 つの面のまとまり（外周 1 つ）、それより広ければ 2 つ。どちらでも穴は 1 つだけ。
    assert len(data["holeList"]) == 1
    assert data["holes"]["outerLoops"] == outer
    assert len(data["outerList"]) == outer
    assert data["holes"]["openChains"] == 0


def test_long_border_edge_is_matched_along_its_whole_length(tmp_path) -> None:
    # 左の板は三角形 2 つ（境目は長さ 50 の辺 1 本）、右の板は細かい升目。長い辺の中ほどでも向かいの辺と突き合わせる。
    left = [np.array([[0, 0, 0], [50, 0, 0], [50, 50, 0]], float), np.array([[0, 0, 0], [50, 50, 0], [0, 50, 0]], float)]
    right = _plate(50, 50, x0=50.05, holes=[(75, 25, 5)])
    data = _stl_data(tmp_path, left + right, hole_tolerance=0.1)
    assert len(data["holeList"]) == 1
    assert data["holes"]["outerLoops"] == 1


def test_diagonal_border_edges_are_matched_across_grid_cell_corners() -> None:
    # 斜めの向きに置いた 2 枚の帯が、許容差（0.1）より狭い隙間（0.05）で向かい合う。辺が升目の角を斜めに横切ると、
    # 辺の端の点のまわりだけを升目に入れていたとき、辺の中ほどの点から相手が見つからず境目が残っていた。
    from src.cad.topology import _Boundary

    rng = np.random.default_rng(1)
    for _ in range(40):
        d = rng.normal(size=3)
        d /= np.linalg.norm(d)
        side = np.cross(d, rng.normal(size=3))
        side /= np.linalg.norm(side)
        o = rng.uniform(-50, 50, size=3)
        pts, tris = [], []
        for k in range(4):
            a, b = o + d * 10.0 * k, o + d * 10.0 * (k + 1)
            base = len(pts)
            pts += [a, b, a - side * 3, b - side * 3, a + side * 0.05, b + side * 0.05, a + side * 3.05, b + side * 3.05]
            tris += [[base, base + 1, base + 2], [base + 1, base + 3, base + 2], [base + 4, base + 6, base + 5], [base + 5, base + 6, base + 7]]
        bd = _Boundary(np.array(pts), np.array(tris), 0.1)
        before = int(bd.alive.sum())
        bd.match_within_tolerance()
        # 向かい合う縁の辺（帯ごとに 4 本、2 枚で 8 本）がすべて突き合わさる。
        assert before - int(bd.alive.sum()) == 8


def test_hole_smaller_than_the_tolerance_is_not_a_hole(tmp_path) -> None:
    # 升目 0.02 の板に、0.06 角の穴（許容差 0.1 より小さい）。
    data = _stl_data(tmp_path, _plate(50, 1, holes=[(0.5, 0.5, 0.03)]), hole_tolerance=0.1)
    assert not data.get("holeList")
    data = _stl_data(tmp_path, _plate(50, 1, holes=[(0.5, 0.5, 0.03)]), hole_tolerance=0.001)
    assert len(data["holeList"]) == 1


def test_closed_mesh_has_no_holes(tmp_path) -> None:
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * 10
    quads = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [2, 3, 7, 6], [1, 2, 6, 5], [3, 0, 4, 7]]
    cube = v[np.array([t for q in quads for t in ([q[0], q[1], q[2]], [q[0], q[2], q[3]])])]
    data = _stl_data(tmp_path, list(cube))
    assert "holeList" not in data and "holes" not in data


def _faces_data(face_tris: list[list[np.ndarray]], parts: list[int] | None = None, **options) -> dict:
    """面ごとの三角形から、B-rep と同じ形（面ごとに頂点を持ち、面の中では頂点を共有する）のビューア用データを作る。

    B-rep のエッジの共有（edgeList の faces）は入れない（縫い合わせていない面どうし）。parts は面ごとの部品の番号。
    """
    from src.cad.mesh_data import b64

    positions, indices, ranges, faces = [], [], [], []
    base = tri_start = 0
    for fid, tris in enumerate(face_tris):
        pts = np.asarray(tris, float).reshape(-1, 3)
        uniq, inv = np.unique(np.round(pts, 9), axis=0, return_inverse=True)
        positions.append(uniq)
        indices.append(inv.reshape(-1, 3) + base)
        ranges.append([tri_start, len(tris)])
        faces.append({"id": fid, "kind": "face", "geomType": "PLANE"})
        if parts is not None:
            faces[-1]["part"] = parts[fid]
        base += len(uniq)
        tri_start += len(tris)
    data = {
        "source": {"kind": "file", "format": "STEP"},
        "mesh": {
            "positions": b64(np.concatenate(positions), "f4"),
            "indices": b64(np.concatenate(indices), "u4"),
            "colors": None,
            "faceRanges": ranges,
        },
        "edges": {"positions": b64(np.zeros((0, 3)), "f4"), "ranges": []},
        "faces": faces,
        "edgeList": [],
        "stats": {},
    }
    attach_topology(data, options)
    assert "topologyError" not in data
    return data


def _tile(i: int, j: int, size: float = 10.0, n: int = 10, holes=()) -> list[np.ndarray]:
    """(i, j) 番目の size 角の板（升目 n×n）。"""
    return [t + [i * size, j * size, 0] for t in _plate(n, size, holes=holes)]


def test_hole_in_a_face_joined_only_by_shared_points_stays_a_hole() -> None:
    # 3×3 の面（縫い合わせていないが、境目の点は同じ位置）の真ん中の面に穴。以前は真ん中の面だけで 1 つのまとまりになり、
    # その穴が「まとまりの外周」になっていた。
    faces = [_tile(i, j, holes=[(5, 5, 2)] if (i, j) == (1, 1) else ()) for i in range(3) for j in range(3)]
    data = _faces_data(faces)
    assert len(data["holeList"]) == 1 and len(data["outerList"]) == 1
    assert data["holeList"][0]["center"][:2] == pytest.approx([15, 15], abs=0.5)
    assert round(data["outerList"][0]["perimeter"]) == 120
    # 9 つの面は 1 つのまとまり（「連続する面」の選択に使う）。
    assert data["faceComponents"] == [0] * 9


def test_outer_that_does_not_close_does_not_turn_a_hole_into_the_outer() -> None:
    # 6 面の箱の上の面に穴が 1 つだけある形（外周が無い）。以前は境界のループが穴 1 つだけなので、それが外周になっていた。
    sq = _plate(10, 10, holes=[(5, 5, 2)])
    def place(tris, f):
        return [np.array([f(p) for p in t]) for t in tris]
    full = _plate(10, 10)
    faces = [
        place(sq, lambda p: [p[0], p[1], 10]),
        place(full, lambda p: [p[1], p[0], 0]),
        place(full, lambda p: [p[0], 0, p[1]]),
        place(full, lambda p: [p[1], 10, p[0]]),
        place(full, lambda p: [0, p[1], p[0]]),
        place(full, lambda p: [10, p[0], p[1]]),
    ]
    data = _faces_data(faces)
    assert len(data["holeList"]) == 1
    assert not data["outerList"]
    assert data["holeList"][0]["center"] == pytest.approx([5, 5, 10], abs=0.5)


@pytest.mark.parametrize("offset", [0.0, 0.02])
def test_two_overlapping_copies_of_a_surface_each_keep_outer_and_holes(offset) -> None:
    # フィレット無しのデータとフィレット後のデータが重なっているような、ほぼ同じ面が 2 枚ある開いたサーフェース。
    # 以前は縁がすべて突き合わされて、外周も穴も見つからなかった。
    a = _plate(20, 40, holes=[(10, 10, 3), (30, 25, 4)])
    b = [t + [offset, 0, 0] for t in _plate(16, 40, holes=[(10, 10, 3), (30, 25, 4)])]
    data = _faces_data([a, b])
    assert len(data["outerList"]) == 2
    assert len(data["holeList"]) == 4
    assert data["faceComponents"] == [0, 1]


def test_patch_of_another_part_on_a_face_seam_keeps_each_outer() -> None:
    # 部品 0 の 2 つの面（左右に並ぶ）の右の面に、別の部品 1 の面（ラベルのようなパッチ）がそっくり重なっている。
    # 境目の位置には辺が 3 本（左の面・右の面・パッチ）あり、以前はパッチと左の面が組になって右の面の縁が境界に残り、
    # 部品 0 の外周が崩れていた（パッチの縁へ乗り移る・パッチと 1 つのまとまりになる）。
    left, right, patch = _tile(0, 0), _tile(1, 0), _tile(1, 0)
    data = _faces_data([left, patch, right], parts=[0, 1, 0])
    outers = sorted(data["outerList"], key=lambda o: -o["perimeter"])
    assert [round(o["perimeter"]) for o in outers] == [60, 40]
    assert outers[0]["faces"] == [0, 2] and outers[0]["part"] == 0
    assert outers[1]["faces"] == [1] and outers[1]["part"] == 1
    assert not data["holeList"]
    assert data["faceComponents"] == [0, 1, 0]


def test_sewing_closes_gaps_between_faces_but_keeps_small_holes_in_a_face() -> None:
    # 左右の板の間に 0.3 の面離れ。左の板には ⌀0.6 ほどの小さい穴。
    left = _plate(50, 5, holes=[(2.5, 2.5, 0.3)])
    right = [t + [5.3, 0, 0] for t in _plate(40, 5)]
    plain = _faces_data([left, right], hole_tolerance=0.1)
    assert len(plain["outerList"]) == 2 and len(plain["holeList"]) == 1
    assert plain["faceComponents"] == [0, 1]
    sewn = _faces_data([left, right], hole_tolerance=0.1, sew_tolerance=0.5)
    assert len(sewn["outerList"]) == 1 and len(sewn["holeList"]) == 1
    assert sewn["faceComponents"] == [0, 0]
    assert sewn["holes"]["sewTolerance"] == 0.5 and sewn["holes"]["sewnEdges"] > 0
    # 縫合トレランスを変えて求め直せる（前の結果は消える）。
    from src.cad.topology import apply_holes

    apply_holes(sewn, 0.1, 0.0)
    assert len(sewn["outerList"]) == 2 and sewn["holes"]["sewTolerance"] == 0


def test_closed_shape_still_has_face_components(tmp_path) -> None:
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * 10
    quads = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [2, 3, 7, 6], [1, 2, 6, 5], [3, 0, 4, 7]]
    faces = [[v[[q[0], q[1], q[2]]], v[[q[0], q[2], q[3]]]] for q in quads]
    data = _faces_data(faces)
    assert "holeList" not in data
    assert data["faceComponents"] == [0] * 6


def _lines(origin, x, y, z, lengths=(100, 200, 300), reverse_y=False):
    o = np.asarray(origin, float)
    edges = []
    for k, (d, length) in enumerate(zip((x, y, z), lengths)):
        s, e = o, o + np.asarray(d, float) * length
        if k == 1 and reverse_y:
            s, e = e, s
        edges.append({"id": k, "kind": "edge", "geomType": "LINE", "start": s.tolist(), "end": e.tolist(), "length": length})
    return edges


def test_local_axis_from_three_perpendicular_lines() -> None:
    a = math.radians(30)
    x, y, z = [math.cos(a), math.sin(a), 0], [-math.sin(a), math.cos(a), 0], [0, 0, 1]
    # 線の向き（始点・終点）は問わない。ほかの直線があっても組を探す。
    edges = _lines([10, 20, 30], x, y, z, reverse_y=True) + [
        {"id": 3, "kind": "edge", "geomType": "LINE", "start": [0, 0, 0], "end": [100, 0, 0], "length": 100}
    ]
    axes = find_local_axes(edges)
    assert len(axes) == 1
    ax = axes[0]
    assert ax["origin"] == pytest.approx([10, 20, 30])
    assert ax["x"] == pytest.approx(x) and ax["y"] == pytest.approx(y) and ax["z"] == pytest.approx(z)
    assert ax["rightHanded"] is True and ax["edges"] == [0, 1, 2]
    assert to_local_point(ax, [10 + 100 * x[0], 20 + 100 * x[1], 30]) == pytest.approx([100, 0, 0], abs=1e-5)
    assert to_local_vector(ax, [0, 0, 1]) == pytest.approx([0, 0, 1])


def test_local_axis_needs_the_right_lengths_and_right_angles() -> None:
    assert find_local_axes(_lines([0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], lengths=(100, 200, 250))) == []
    tilted = [0, math.cos(math.radians(80)), math.sin(math.radians(80))]
    assert find_local_axes(_lines([0, 0, 0], [1, 0, 0], tilted, [0, 0, 1])) == []
    # 左手系（Z が X × Y と逆向き）は見つけるが rightHanded: false。
    left = find_local_axes(_lines([0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, -1]))
    assert len(left) == 1 and left[0]["rightHanded"] is False and left[0]["z"] == pytest.approx([0, 0, -1])
    # 長さの設定を変えられる。
    assert len(find_local_axes(_lines([0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], lengths=(10, 20, 30)), (10, 20, 30))) == 1


def test_package_round_trips_the_holes(tmp_path) -> None:
    data = _stl_data(tmp_path, _plate(20, 100, holes=[(50, 50, 10)]))
    write_package(data, tmp_path / "p.lcvd")
    back = read_package(tmp_path / "p.lcvd")
    assert back["holeList"] == data["holeList"]
    assert back["holes"]["positions"] == data["holes"]["positions"]
    assert back["outerList"] == data["outerList"]
    assert back["outers"]["positions"] == data["outers"]["positions"]


def test_selected_holes_and_measurements_get_local_values(tmp_path, monkeypatch) -> None:
    import src.tools as tools

    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", None)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", None)

    class _Session(dict):
        def set(self, key, value):
            self[key] = value

    monkeypatch.setattr(tools.cl, "user_session", _Session(thread_id="topology-thread"))
    axis = {"id": 0, "origin": [10, 0, 0], "x": [0, 1, 0], "y": [-1, 0, 0], "z": [0, 0, 1], "rightHanded": True}
    data = {
        "source": {"format": "STL"},
        "faces": [{"id": 0}],
        "holeList": [{"id": 0, "kind": "hole", "center": [10, 5, 0], "normal": [1, 0, 0], "diameter": 4.0}],
        "holes": {"tolerance": 0.1, "outerLoops": 1},
        "localAxes": [axis],
    }
    assert cad_viewer._available_kinds(data, None) == ["face", "hole"]
    assert cad_viewer._valid_local_axis(data, 0) is axis
    assert cad_viewer._valid_local_axis(data, 1) is None and cad_viewer._valid_local_axis(data, True) is None
    m = {"type": "distance", "distance": 5.0, "delta": [0, 5, 0], "points": [[10, 0, 0], [10, 5, 0]]}
    # 点とエッジの法線距離の垂線の足（foot）もローカル座標にする。
    pe = {"type": "point_edge_distance", "distance": 5.0, "foot": [10, 5, 0], "delta": [0, 5, 0], "points": [[10, 0, 0], [10, 5, 0]]}
    measurements = cad_viewer._valid_measurements(data, [m, pe])
    out = json.loads(cad_viewer._format_result(data, [("hole", 0)], "viewer.json", [], measurements=measurements, local_axis=axis))
    assert out["selected_by_kind"] == {"hole": 1}
    assert out["local_axis"]["origin"] == [10, 0, 0] and "local =" in out["local_axis"]["note"]
    assert out["measurements"][0]["local"]["delta"] == pytest.approx([5, 0, 0])
    assert out["measurements"][0]["local"]["points"][1] == pytest.approx([5, 0, 0])
    assert out["measurements"][1]["local"]["foot"] == pytest.approx([5, 0, 0])
    assert out["model"]["holes"]["count"] == 1 and out["model"]["holes"]["groups"][0]["diameter"] == 4.0
    detail = json.loads(open(out["selection_file"], encoding="utf-8").read())
    hole = detail["selected"][0]
    assert hole["kind"] == "hole" and hole["local"]["center"] == pytest.approx([5, 0, 0])
    assert hole["local"]["normal"] == pytest.approx([0, -1, 0])
    assert detail["local_axis"]["id"] == 0


# ---------------------------------------------------------------------------
# B-rep（cadquery が要る）
# ---------------------------------------------------------------------------


def test_brep_open_shell_holes_and_local_axis() -> None:
    cq = pytest.importorskip("cadquery")
    from src.cad.export_mesh import Options, Part, shapes_to_viewer_data

    box = cq.Workplane().box(100, 100, 50).faces(">Z").workplane().pushPoints([(20, 20), (-20, -20)]).hole(10)
    solid = box.val()
    # 上の面と穴の円柱面を除いた、開いたサーフェース（底面に穴が 2 つ）。
    faces = [f for f in solid.Faces() if f.geomType() != "CYLINDER" and not (f.geomType() == "PLANE" and abs(f.Center().z - 25) < 1e-6)]
    shell = cq.Shell.makeShell(faces)
    o = cq.Vector(10, 20, 30)
    lines = [cq.Edge.makeLine(o, o + cq.Vector(100, 0, 0)), cq.Edge.makeLine(o, o + cq.Vector(0, 200, 0)), cq.Edge.makeLine(o, o + cq.Vector(0, 0, 300))]
    comp = cq.Compound.makeCompound([shell, *lines])
    data = shapes_to_viewer_data([Part("m", "m", comp.wrapped)], source={"kind": "code", "format": "CADQUERY"}, units="mm", options=Options())
    attach_topology(data, {})
    assert "topologyError" not in data
    holes = data["holeList"]
    assert len(holes) == 2 and data["holes"]["outerLoops"] == 1
    for h in holes:
        assert h["diameter"] == pytest.approx(10, abs=0.05)
        assert h["normal"] == pytest.approx([0, 0, -1])  # 底面の外向き
        # B-rep では穴の縁の円のエッジの ID も付く。
        assert h["edges"] and all(data["edgeList"][e]["geomType"] == "CIRCLE" for e in h["edges"])
    axes = data["localAxes"]
    assert len(axes) == 1 and axes[0]["origin"] == pytest.approx([10, 20, 30])
