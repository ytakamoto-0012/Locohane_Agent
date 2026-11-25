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
    assert data["holes"]["openChains"] == 0


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
    out = json.loads(cad_viewer._format_result(data, [("hole", 0)], "viewer.json", [], measurements=[m], local_axis=axis))
    assert out["selected_by_kind"] == {"hole": 1}
    assert out["local_axis"]["origin"] == [10, 0, 0] and "local =" in out["local_axis"]["note"]
    assert out["measurements"][0]["local"]["delta"] == pytest.approx([5, 0, 0])
    assert out["measurements"][0]["local"]["points"][1] == pytest.approx([5, 0, 0])
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
