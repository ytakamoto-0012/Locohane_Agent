"""3D ビューア用データ（src/cad/）のテスト。

STL（ASCII / バイナリ / 色付き 2 方式）は numpy だけで動くので常に確かめる。STEP・IGES・BREP・BIN・DXF と
CadQuery コードは cadquery が入っているときだけ確かめる（無ければ skip）。
"""

from __future__ import annotations

import base64
import json
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from src.cad.formats import format_of
from src.cad.mesh_data import FORMAT, TooManyTrianglesError, stl_to_viewer_data
from src.cad.stl_reader import StlError, read_stl

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_CAD = PROJECT_ROOT / "src" / "cad" / "run_cad.py"

# 1 辺 10 の立方体（三角形 12 個、各面 2 個ずつ、面の順は -Z, +Z, -Y, +Y, +X, -X）。
_CUBE_V = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * 10
_CUBE_QUADS = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [2, 3, 7, 6], [1, 2, 6, 5], [3, 0, 4, 7]]
CUBE = _CUBE_V[np.array([t for q in _CUBE_QUADS for t in ([q[0], q[1], q[2]], [q[0], q[2], q[3]])])]


def _write_binary_stl(path: Path, triangles: np.ndarray, attrs: list[int], header: bytes = b"") -> Path:
    with path.open("wb") as f:
        f.write(header.ljust(80, b" "))
        f.write(struct.pack("<I", len(triangles)))
        for tri, attr in zip(triangles, attrs):
            f.write(struct.pack("<3f", 0, 0, 0))
            f.write(struct.pack("<9f", *tri.ravel()))
            f.write(struct.pack("<H", attr))
    return path


def _write_ascii_stl(path: Path, triangles: np.ndarray) -> Path:
    lines = ["solid cube"]
    for tri in triangles:
        lines += [" facet normal 0 0 0", "  outer loop", *(f"   vertex {a} {b} {c}" for a, b, c in tri), "  endloop", " endfacet"]
    lines.append("endsolid cube")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _floats(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype="<f4")


# ---------------------------------------------------------------------------
# STL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["binary", "ascii"])
def test_stl_cube_has_six_planar_faces_and_twelve_edges(tmp_path, kind) -> None:
    if kind == "binary":
        path = _write_binary_stl(tmp_path / "cube.stl", CUBE, [0] * 12)
    else:
        path = _write_ascii_stl(tmp_path / "cube.stl", CUBE)
    data = stl_to_viewer_data(path, max_triangles=1000)
    assert data["format"] == FORMAT
    assert data["stats"] == {"faces": 6, "edges": 12, "vertices": 0, "triangles": 12}
    assert data["source"]["binary"] is (kind == "binary")
    assert data["mesh"]["colors"] is None
    # 面ごとの三角形の範囲が全体を隙間なく覆う。
    ranges = data["mesh"]["faceRanges"]
    assert [r[0] for r in ranges] == [sum(c for _, c in ranges[:i]) for i in range(len(ranges))]
    assert sum(c for _, c in ranges) == 12
    for face in data["faces"]:
        assert face["geomType"] == "PLANE"
        assert face["area"] == pytest.approx(100.0)
        assert np.linalg.norm(face["normal"]) == pytest.approx(1.0)
    top = [f for f in data["faces"] if f["normal"] == [0.0, 0.0, 1.0]]
    assert len(top) == 1 and top[0]["meshHints"] == [">Z"] and top[0]["center"] == [5.0, 5.0, 10.0]
    # エッジは隣り合う 2 面の境目。
    assert all(len(e["faces"]) == 2 and e["length"] == pytest.approx(10.0) for e in data["edgeList"])
    assert len(_floats(data["edges"]["positions"])) == 12 * 2 * 3


def test_viscam_colored_stl_splits_a_plane_by_color(tmp_path) -> None:
    red = (1 << 15) | (31 << 10)
    blue = (1 << 15) | 31
    path = _write_binary_stl(tmp_path / "c.stl", CUBE, [red, blue] + [red] * 10)
    stl = read_stl(path)
    assert stl.color_format == "viscam"
    assert tuple(stl.colors[0]) == (255, 0, 0) and tuple(stl.colors[1]) == (0, 0, 255)
    data = stl_to_viewer_data(path, max_triangles=1000)
    # 底面（-Z）は 2 色に分かれているので 2 つの面になる。
    assert data["stats"]["faces"] == 7
    assert sorted({f["color"] for f in data["faces"]}) == ["#0000ff", "#ff0000"]
    assert data["source"]["colorFormat"] == "viscam"
    colors = base64.b64decode(data["mesh"]["colors"])
    assert len(colors) == 12 * 3 * 3  # 三角形ごとに 3 頂点 × RGB


def test_viscam_uncolored_triangles_have_no_face_color(tmp_path) -> None:
    red = (1 << 15) | (31 << 10)
    path = _write_binary_stl(tmp_path / "c.stl", CUBE, [red, red] + [0] * 10)
    data = stl_to_viewer_data(path, max_triangles=1000)
    assert sorted(str(f["color"]) for f in data["faces"]) == ["#ff0000"] + ["None"] * 5


def test_magics_colored_stl_uses_header_default_color(tmp_path) -> None:
    # 1 個目だけ赤（bit15=0、bit0-4 が赤）、残りは既定色（ヘッダーの COLOR= の緑）。
    path = _write_binary_stl(tmp_path / "m.stl", CUBE, [31] + [1 << 15] * 11, header=b"COLOR=\x00\xff\x00\xff")
    stl = read_stl(path)
    assert stl.color_format == "magics"
    assert tuple(stl.colors[0]) == (255, 0, 0)
    assert tuple(stl.colors[1]) == (0, 255, 0)
    data = stl_to_viewer_data(path, max_triangles=1000)
    assert data["stats"]["faces"] == 7
    assert {f["color"] for f in data["faces"]} == {"#ff0000", "#00ff00"}


def test_binary_stl_starting_with_solid_is_read_as_binary(tmp_path) -> None:
    path = _write_binary_stl(tmp_path / "s.stl", CUBE, [0] * 12, header=b"solid exported by some CAD")
    assert read_stl(path).binary is True


def test_stl_over_max_triangles_is_an_error(tmp_path) -> None:
    path = _write_binary_stl(tmp_path / "cube.stl", CUBE, [0] * 12)
    with pytest.raises(TooManyTrianglesError):
        stl_to_viewer_data(path, max_triangles=11)


def test_not_an_stl_is_an_error(tmp_path) -> None:
    path = tmp_path / "x.stl"
    path.write_bytes(b"hello")
    with pytest.raises(StlError):
        read_stl(path)


def test_format_of_extensions() -> None:
    assert format_of("A.STEP") == "STEP"
    assert format_of("a.igs") == "IGES"
    assert format_of("a.brp") == "BREP"
    assert format_of("a.stl") == "STL"
    assert format_of("a.obj") is None


def test_run_cad_cli_reads_stl_without_cadquery_import(tmp_path) -> None:
    path = _write_binary_stl(tmp_path / "cube.stl", CUBE, [0] * 12)
    out = tmp_path / "out.json"
    proc = subprocess.run([sys.executable, str(RUN_CAD), "--file", str(path), "--out", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "CAD_VIEWER_OK" in proc.stdout
    assert json.loads(out.read_text(encoding="utf-8"))["stats"]["faces"] == 6


def test_run_cad_cli_rejects_unsupported_extension(tmp_path) -> None:
    path = tmp_path / "a.obj"
    path.write_text("v 0 0 0\n", encoding="ascii")
    proc = subprocess.run([sys.executable, str(RUN_CAD), "--file", str(path), "--out", str(tmp_path / "o.json")], capture_output=True, text=True)
    assert proc.returncode == 2
    assert "unsupported file type '.obj'" in proc.stderr


# ---------------------------------------------------------------------------
# B-rep（cadquery が要る）
# ---------------------------------------------------------------------------


@pytest.fixture
def cq():
    return pytest.importorskip("cadquery")


def _box_with_hole(cq):
    return cq.Workplane().box(100, 60, 20).faces(">Z").workplane().hole(10)


def test_box_with_hole_faces_edges_vertices_and_selectors(cq) -> None:
    from src.cad.export_mesh import Options, shape_to_viewer_data

    wp = _box_with_hole(cq)
    shape = wp.val()
    data = shape_to_viewer_data(shape, Options(), {"kind": "code", "format": "CADQUERY"})
    assert data["stats"]["faces"] == 7
    assert data["stats"]["edges"] == 15
    assert data["stats"]["vertices"] == 10
    # ID は CadQuery の faces().vals() の順と同じ。
    areas = [round(f.Area(), 3) for f in cq.Workplane().add(shape).faces().vals()]
    assert [round(f["area"], 3) for f in data["faces"]] == areas
    top = next(f for f in data["faces"] if ">Z" in f.get("selectors", []))
    assert top["geomType"] == "PLANE" and top["normal"] == [0.0, 0.0, 1.0]
    # セレクタ候補は、実際に当てるとその面だけを選ぶ。
    picked = cq.Workplane().add(shape).faces(">Z").vals()
    assert len(picked) == 1 and round(picked[0].Area(), 3) == round(top["area"], 3)
    cyl = [f for f in data["faces"] if f["geomType"] == "CYLINDER"]
    assert len(cyl) == 1 and cyl[0]["radius"] == pytest.approx(5.0) and "%CYLINDER" in cyl[0]["selectors"]
    circles = [e for e in data["edgeList"] if e["geomType"] == "CIRCLE"]
    assert len(circles) == 2 and all(e["radius"] == pytest.approx(5.0) for e in circles)
    # 円筒の継ぎ目は同じ面に 2 回属する（seam）。それ以外のエッジは 2 つの面の境目。
    seams = [e for e in data["edgeList"] if e.get("seam")]
    assert len(seams) == 1 and seams[0]["faces"] == [cyl[0]["id"]]
    assert all(len(e["faces"]) == 2 for e in data["edgeList"] if not e.get("seam"))
    assert all(len(v["edges"]) >= 2 for v in data["vertexList"])
    assert not any(e.get("free") for e in data["edgeList"])


def test_free_curves_and_points_are_marked_free(cq) -> None:
    from src.cad.export_mesh import Options, shape_to_viewer_data, to_shape

    shape = to_shape([cq.Workplane().box(1, 1, 1), cq.Edge.makeLine(cq.Vector(3, 0, 0), cq.Vector(4, 0, 0)), cq.Vector(5, 5, 5)])
    data = shape_to_viewer_data(shape, Options(), {"kind": "code", "format": "CADQUERY"})
    free_edges = [e for e in data["edgeList"] if e.get("free")]
    free_vertices = [v for v in data["vertexList"] if v.get("free")]
    assert len(free_edges) == 1 and free_edges[0]["geomType"] == "LINE" and free_edges[0]["faces"] == []
    assert [v["point"] for v in free_vertices] == [[5.0, 5.0, 5.0]]


@pytest.mark.parametrize("ext", ["step", "brep", "bin", "iges"])
def test_cad_files_round_trip(cq, tmp_path, ext) -> None:
    from src.cad.export_mesh import Options, cad_file_to_viewer_data

    shape = _box_with_hole(cq).val()
    path = tmp_path / f"part.{ext}"
    if ext == "step":
        cq.exporters.export(shape, str(path))
    elif ext == "brep":
        shape.exportBrep(str(path))
    elif ext == "bin":
        shape.exportBin(str(path))
    else:
        from OCP.IGESControl import IGESControl_Writer

        writer = IGESControl_Writer()
        writer.AddShape(shape.wrapped)
        writer.ComputeModel()
        assert writer.Write(str(path))
    data = cad_file_to_viewer_data(path, format_of(path), Options())
    assert data["stats"]["faces"] == 7
    assert data["source"]["format"] == format_of(path)
    if ext != "iges":
        assert data["stats"]["edges"] == 15
    else:
        # IGES は面が別々に入っているので縫い合わせる。縫わないと 1 本のエッジが 2 面に別々に入る（30 本）。
        assert data["stats"]["edges"] < 30


def test_step_assembly_keeps_parts_names_and_colors(cq, tmp_path) -> None:
    from src.cad.export_mesh import Options, cad_file_to_viewer_data

    assy = cq.Assembly(name="Assy")
    assy.add(cq.Workplane().box(1, 1, 1), name="Cube", color=cq.Color("red"))
    assy.add(cq.Workplane().sphere(0.5), name="Ball", loc=cq.Location((3, 0, 0)), color=cq.Color(0, 0, 1))
    path = tmp_path / "assy.step"
    assy.export(str(path))
    data = cad_file_to_viewer_data(path, "STEP", Options())
    parts = {p["name"]: p for p in data["parts"]}
    assert set(parts) == {"Cube", "Ball"}
    assert parts["Cube"]["path"] == "Assy/Cube" and parts["Cube"]["color"] == "#ff0000"
    assert parts["Ball"]["color"] == "#0000ff"
    assert len(parts["Cube"]["faces"]) == 6 and len(parts["Ball"]["faces"]) == 1
    # 配置（loc）が反映されている。
    assert parts["Ball"]["bbox"]["min"][0] == pytest.approx(2.5, abs=0.01)
    ball_face = data["faces"][parts["Ball"]["faces"][0]]
    assert ball_face["part"] == parts["Ball"]["id"] and ball_face["color"] == "#0000ff"
    assert data["mesh"]["colors"] is not None


def test_dxf_layers_become_parts_with_faces_and_free_curves(cq, tmp_path) -> None:
    ezdxf = pytest.importorskip("ezdxf")
    from src.cad.export_mesh import Options, cad_file_to_viewer_data

    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 2), (0, 2)], close=True, dxfattribs={"layer": "OUTLINE"})
    msp.add_line((10, 0), (12, 0), dxfattribs={"layer": "GUIDE"})
    msp.add_point((20, 20), dxfattribs={"layer": "GUIDE"})
    path = tmp_path / "plan.dxf"
    doc.saveas(path)
    data = cad_file_to_viewer_data(path, "DXF", Options())
    assert {p["name"] for p in data["parts"]} == {"OUTLINE", "GUIDE"}
    assert data["stats"]["faces"] == 1 and data["faces"][0]["area"] == pytest.approx(8.0)
    assert [e["length"] for e in data["edgeList"] if e.get("free")] == [pytest.approx(2.0)]
    assert [v["point"][:2] for v in data["vertexList"] if v.get("free")] == [[20.0, 20.0]]
    # 単独の点も部品（レイヤー）を持つ（サイドバーでレイヤーを隠したときに一緒に隠すため）。
    guide = next(p["id"] for p in data["parts"] if p["name"] == "GUIDE")
    assert all(v["part"] == guide for v in data["vertexList"] if v.get("free"))


def test_dxf_layer_settings_are_kept(cq, tmp_path) -> None:
    ezdxf = pytest.importorskip("ezdxf")
    from src.cad.export_mesh import Options, cad_file_to_viewer_data

    doc = ezdxf.new()
    doc.layers.add("RED", color=1, linetype="DASHED")
    hidden = doc.layers.add("HIDDEN", color=5)
    hidden.off()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 2), (0, 2)], close=True, dxfattribs={"layer": "RED"})
    msp.add_line((10, 0), (12, 0), dxfattribs={"layer": "HIDDEN"})
    path = tmp_path / "layers.dxf"
    doc.saveas(path)
    data = cad_file_to_viewer_data(path, "DXF", Options())
    parts = {p["name"]: p for p in data["parts"]}
    red = parts["RED"]["layer"]
    assert red["color"] == "#ff0000" and red["aci"] == 1 and red["linetype"] == "DASHED"
    assert not red["off"] and not red["frozen"]
    assert parts["RED"]["color"] == "#ff0000"
    # 面はレイヤーの色で描く。
    assert data["faces"][0]["color"] == "#ff0000"
    assert parts["HIDDEN"]["layer"]["off"] is True


def test_dxf_single_layer_is_kept_as_a_part(cq, tmp_path) -> None:
    ezdxf = pytest.importorskip("ezdxf")
    from src.cad.export_mesh import Options, cad_file_to_viewer_data

    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (1, 0))
    path = tmp_path / "one.dxf"
    doc.saveas(path)
    data = cad_file_to_viewer_data(path, "DXF", Options())
    assert [p["name"] for p in data["parts"]] == ["0"]
    # 色 7（白/黒）は背景で変わるので、色を付けない。
    assert data["parts"][0]["layer"]["color"] is None and "color" not in data["parts"][0]


def test_run_cad_code_prologue_and_epilogue(cq, tmp_path) -> None:
    from src.cad.run_cad import CODE_EPILOGUE, CODE_PROLOGUE

    out = tmp_path / "out.json"
    code = "import cadquery as cq\nshow_object(cq.Workplane().box(1, 2, 3))\n"
    script = tmp_path / "s.py"
    script.write_text(CODE_PROLOGUE.format(src_root=str(PROJECT_ROOT)) + code + CODE_EPILOGUE.format(out_path=str(out), options={}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["bbox"]["size"] == [1.0, 2.0, 3.0]

    script.write_text(CODE_PROLOGUE.format(src_root=str(PROJECT_ROOT)) + "x = 1\n" + CODE_EPILOGUE.format(out_path=str(out), options={}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, cwd=str(tmp_path))
    assert proc.returncode == 2
    assert "`result`" in proc.stderr
