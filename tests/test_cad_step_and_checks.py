"""3D ビューアの形の検査・穴の認識・前の版との比較・STEP の PMI と属性の読み書きのテスト。

P21 の解析（src/cad/step_p21.py）と概要（step_summary.py）は標準ライブラリだけなので常に確かめる。
形を使うもの（features / check_shape / compare / step_xcaf）は cadquery が入っているときだけ確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cad import step_p21
from src.cad.step_summary import summarize_step

# ---------------------------------------------------------------------------
# P21（cadquery 不要）
# ---------------------------------------------------------------------------

_P21 = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('test model'),'2;1');
FILE_NAME('part.stp','2026-01-01T00:00:00',('user_a'),('org_a'),'pre 1','\\X2\\30C630B930C8\\X0\\ CAD','none');
FILE_SCHEMA(('AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF { 1 0 10303 442 4 1 4 }'));
ENDSEC;
DATA;
#1=PRODUCT('PN-1','\\X2\\90E854C1\\X0\\A','desc; with semicolon',(#2));
#2=PRODUCT_CONTEXT('',#3,'mechanical');
#3=APPLICATION_CONTEXT('managed model based 3d engineering');
#4=PRODUCT_DEFINITION_FORMATION('','',#1);
#5=PRODUCT_DEFINITION('design','',#4,#6);
#6=PRODUCT_DEFINITION_CONTEXT('part definition',#3,'design');
#10=PROPERTY_DEFINITION('user defined attributes','',#5);
#11=PROPERTY_DEFINITION_REPRESENTATION(#10,#12);
#12=REPRESENTATION('',(#13),#14);
#13=DESCRIPTIVE_REPRESENTATION_ITEM('\\X2\\67508CEA\\X0\\','SS400');
#14=REPRESENTATION_CONTEXT('','');
#20=PROPERTY_DEFINITION('user defined attributes','',#5);
#21=PROPERTY_DEFINITION_REPRESENTATION(#20,#22);
#22=REPRESENTATION('',(#23),#14);
#23=VALUE_REPRESENTATION_ITEM('mass',MASS_MEASURE(1.5));
#30=PROPERTY_DEFINITION('geometric validation property','volume',#5);
#31=PROPERTY_DEFINITION_REPRESENTATION(#30,#32);
#32=REPRESENTATION('volume',(#33),#14);
#33=(MEASURE_REPRESENTATION_ITEM()MEASURE_WITH_UNIT(VOLUME_MEASURE(1000.),#99)REPRESENTATION_ITEM('volume')VOLUME_MEASURE_WITH_UNIT());
#40=PROPERTY_DEFINITION('material property','material name',#5);
#41=PROPERTY_DEFINITION_REPRESENTATION(#40,#42);
#42=REPRESENTATION('material name',(#43),#14);
#43=DESCRIPTIVE_REPRESENTATION_ITEM('Steel','SS400');
ENDSEC;
END-ISO-10303-21;
"""


def test_p21_strings_are_decoded_and_encoded() -> None:
    assert step_p21.decode_string("\\X2\\90E854C1\\X0\\A-1") == "部品A-1"
    assert step_p21.decode_string("it''s") == "it's"
    assert step_p21.decode_string("\\X\\E9t\\S\\a") == "été" or step_p21.decode_string("\\X\\E9") == "é"
    for text in ("部品A-1", "𠀋 x", "a'b\\c", "表面処理: 黒染め"):
        enc = step_p21.encode_string(text)
        assert enc.isascii()
        assert step_p21.decode_string(enc[1:-1]) == text


def test_p21_header_gives_the_application_protocol_and_edition() -> None:
    h = step_p21.read_header(_P21)
    assert h["ap"] == "AP242" and h["edition"] == "AP242 ed3 (2022)"
    assert h["originating_system"] == "テスト CAD"
    assert h["author"] == ["user_a"] and h["organization"] == ["org_a"]
    assert step_p21.schema_info(["AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }"])["ap"] == "AP214"
    assert step_p21.schema_info(["CONFIG_CONTROL_DESIGN"])["ap"] == "AP203"


def test_p21_products_have_attributes_material_and_validation() -> None:
    [prod] = step_p21.read_products(_P21)
    assert prod["product_id"] == "PN-1" and prod["name"] == "部品A" and prod["description"] == "desc; with semicolon"
    assert prod["attributes"] == [{"name": "材質", "value": "SS400"}, {"name": "mass", "value": 1.5, "type": "mass_measure"}]
    assert prod["material"] == [{"name": "Steel", "value": "SS400"}]
    assert prod["validation"]["volume"] == 1000.0


def test_p21_attributes_can_be_added_and_read_back() -> None:
    text, n = step_p21.add_attributes(_P21, {"部品A": [{"name": "表面処理", "value": "黒染め"}, {"name": "qty", "value": 2}], "missing": [{"name": "x", "value": 1}]})
    assert n == 2
    [prod] = step_p21.read_products(text)
    assert {"name": "表面処理", "value": "黒染め"} in prod["attributes"]
    assert {"name": "qty", "value": 2, "type": "count_measure"} in prod["attributes"]
    assert text.isascii()  # ASCII 以外は \X2\ で書く


def test_p21_real_numbers_are_written_in_the_valid_form() -> None:
    """ISO 10303-21 の REAL は小数点が必須（Python の repr の 1e-05 をそのまま書くと厳格な読み取りが拒否する）。"""
    text, n = step_p21.add_attributes(_P21, {"部品A": [{"name": "tol", "value": 0.00001}, {"name": "big", "value": 1e20}]})
    assert n == 2
    assert "REAL(1.E-05)" in text and "REAL(1.E+20)" in text
    [prod] = step_p21.read_products(text)
    values = {a["name"]: a["value"] for a in prod["attributes"]}
    assert values["tol"] == 1e-05 and values["big"] == 1e20


def test_p21_truncated_hex_escape_is_kept_literally() -> None:
    """文字列の末尾で 1 桁だけ切れた \\X\\ は文字として残す（1 桁のまま制御文字として読んでいた）。"""
    assert step_p21.decode_string("name\\X\\E") == "name\\X\\E"
    assert step_p21.decode_string("x\\X\\E9y") == "xéy"


def test_p21_header_keeps_the_original_author_and_system() -> None:
    written = _P21.replace("('user_a'),('org_a'),'pre 1','\\X2\\30C630B930C8\\X0\\ CAD'", "('Author'),('Open CASCADE'),'occt','Open CASCADE 7.9'")
    out = step_p21.set_header(written, step_p21.read_header(_P21), "Locohane")
    h = step_p21.read_header(out)
    assert h["author"] == ["user_a"] and h["originating_system"] == "テスト CAD" and h["preprocessor_version"] == "Locohane"


def test_step_summary_is_bounded() -> None:
    pmi = {"dimensions": [{"id": f"dim:{i}", "text": f"⌀{i}"} for i in range(50)]}
    out = summarize_step({"header": {"ap": "AP242"}, "products": step_p21.read_products(_P21), "pmi": pmi})
    assert out["pmi"] == {"dimensions": 50}
    assert len(out["pmi_texts"]) == 20 and out["pmi_more"] == 30
    assert out["attributes"] == {"products": 1, "count": 2, "names": ["材質", "mass"]}


# ---------------------------------------------------------------------------
# 形の検査・穴の認識・比較（cadquery）
# ---------------------------------------------------------------------------


def _plate(cq):
    return (
        cq.Workplane("XY")
        .box(80, 60, 10)
        .faces(">Z").workplane().rarray(60, 40, 2, 2).hole(5)
        .faces(">Z").workplane().center(0, 0).hole(8, depth=4)
        .faces("<Z").workplane().circle(6).extrude(15)
        .edges("|Z and >X").fillet(2)
    )


def test_holes_and_bosses_are_recognized() -> None:
    cq = pytest.importorskip("cadquery")
    from src.cad.export_mesh import Options, shape_to_viewer_data
    from src.cad.features import summarize_features

    data = shape_to_viewer_data(_plate(cq).val(), Options(), {"kind": "code"})
    s = summarize_features(data["features"])
    assert s["holes"] == 5 and s["bosses"] == 1  # フィレット（部分的な円柱）は数えない
    rows = {(g["type"], g["diameter"], g.get("through")): g for g in s["groups"]}
    assert rows[("hole", 5.0, True)]["count"] == 4 and rows[("hole", 5.0, True)]["depth"] == 10.0
    assert rows[("hole", 8.0, False)]["depth"] == 4.0
    assert rows[("boss", 12.0, None)]["length"] == 15.0
    hole_faces = [f for f in data["faces"] if f.get("feature", "").startswith("hole:")]
    assert hole_faces and all(f["concave"] for f in hole_faces)
    assert data["check"]["solids"] == 1 and data["check"]["valid"] and data["check"]["openEdges"] == 0
    assert "warnings" not in data["check"]
    assert len(data["modelDigest"]) == 16


def test_separate_solids_are_warned() -> None:
    cq = pytest.importorskip("cadquery")
    from src.cad.export_mesh import Options, shape_to_viewer_data

    two = cq.Workplane().box(10, 10, 10).union(cq.Workplane().box(5, 5, 5).translate((20, 0, 0)))
    check = shape_to_viewer_data(two.val(), Options(), {"kind": "code"})["check"]
    assert check["solids"] == 2 and any("separate solids" in w for w in check["warnings"])


def test_compare_reports_removed_volume_and_new_holes() -> None:
    cq = pytest.importorskip("cadquery")
    from src.cad.compare import compare
    from src.cad.export_mesh import Options, shape_to_viewer_data

    old = _plate(cq)
    new = old.faces(">Z").workplane().center(20, 0).hole(3)
    data = shape_to_viewer_data(new.val(), Options(), {"kind": "code"})
    diff = compare(old.val(), new.val(), data, Options(), 500_000)
    vol = diff["volume"]
    assert vol["added"] == 0 and vol["removed"] == pytest.approx(3.14159265 * 1.5**2 * 10, rel=1e-3)
    assert vol["common"] + vol["removed"] == pytest.approx(vol["before"], rel=1e-6)
    assert diff["features"]["holes"] == {"before": 5, "after": 6}
    assert diff["features"]["changed_groups"] == [{"type": "hole", "diameter": 3.0, "depth": 10.0, "through": True, "before": 0, "after": 1}]
    assert diff["ids_unchanged"] is False and "removed" in diff and "added" not in diff

    same = compare(old.val(), old.val(), shape_to_viewer_data(old.val(), Options(), {"kind": "code"}), Options(), 500_000)
    assert same["ids_unchanged"] is True
    assert any("no effect" in w for w in same["warnings"])


def test_feature_changes_counts_groups_beyond_the_summary_cap() -> None:
    """要約の groups の上限（SUMMARY_MAX_GROUPS）を超えるグループも、変わっていなければ増減に出さない。"""
    pytest.importorskip("cadquery")
    from src.cad.compare import _feature_changes
    from src.cad.features import SUMMARY_MAX_GROUPS

    # 直径の違う穴のグループを上限より多く作る。数の多い順で並ぶので、1 個ずつのグループは上限で切られる。
    before = [{"type": "hole", "diameter": 1.0, "depth": 5.0, "through": True}] * 3
    before += [{"type": "hole", "diameter": 2.0 + i, "depth": 5.0, "through": True} for i in range(SUMMARY_MAX_GROUPS + 3)]
    after = before + [{"type": "hole", "diameter": 1.0, "depth": 5.0, "through": True}]
    changes = _feature_changes(before, after)
    assert changes["changed_groups"] == [{"type": "hole", "diameter": 1.0, "depth": 5.0, "through": True, "before": 3, "after": 4}]
    # 上限の外のグループが 1 個増えたことも分かる。
    tail = before[-1]
    grown = _feature_changes(before, before + [tail])["changed_groups"]
    assert grown == [{**tail, "before": 1, "after": 2}]


# ---------------------------------------------------------------------------
# STEP の PMI・属性（cadquery）
# ---------------------------------------------------------------------------


def _pmi_step(path: Path, schema_id: int = 5) -> Path:
    """穴に ⌀10 +0.1/-0.05 の寸法を付け、レイヤーと属性を持つ板の STEP を書く。

    幾何公差・データムは、OCCT が書ける組み方が限られる（データム系の組み立てが要る）ため、実際の CAD が書いた
    STEP（NIST の PMI テストモデル）で手で確かめた。ここでは寸法と面の対応だけを確かめる。
    """
    cq = pytest.importorskip("cadquery")
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDimTolObjects import XCAFDimTolObjects_DimensionObject, XCAFDimTolObjects_DimensionType
    from OCP.XCAFDoc import XCAFDoc_Dimension, XCAFDoc_DocumentTool

    from src.cad.step_xcaf import write_xcaf

    shape = cq.Workplane().box(40, 30, 10).faces(">Z").workplane().hole(10).val()
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    lab = st.AddShape(shape.wrapped, False)
    TDataStd_Name.Set_s(lab, TCollection_ExtendedString("plate"))
    XCAFDoc_DocumentTool.LayerTool_s(doc.Main()).SetLayer(lab, TCollection_ExtendedString("BODY"))
    dt = XCAFDoc_DocumentTool.DimTolTool_s(doc.Main())
    cyl = next(f for f in shape.Faces() if f.geomType() == "CYLINDER")
    seq = TDF_LabelSequence()
    seq.Append(st.AddSubShape(lab, cyl.wrapped))
    dl = dt.AddDimension()
    dt.SetDimension(seq, TDF_LabelSequence(), dl)
    dim = XCAFDimTolObjects_DimensionObject()
    dim.SetType(XCAFDimTolObjects_DimensionType.XCAFDimTolObjects_DimensionType_Size_Diameter)
    dim.SetValue(10.0)
    dim.SetUpperTolValue(0.1)
    dim.SetLowerTolValue(-0.05)
    XCAFDoc_Dimension.Set_s(dl).SetObject(dim)
    write_xcaf(doc, path, schema_id)
    text, enc = step_p21.read_text(path)
    text, _n = step_p21.add_attributes(text, {"plate": [{"name": "品番", "value": "PN-0001"}, {"name": "質量", "value": 1.25, "type": "mass_measure"}]})
    path.write_bytes(text.encode(enc))
    return path


def test_step_pmi_attributes_and_layers_are_read(tmp_path) -> None:
    pytest.importorskip("cadquery")
    from src.cad.export_mesh import Options, cad_file_to_viewer_data

    data = cad_file_to_viewer_data(_pmi_step(tmp_path / "plate.step"), "STEP", Options())
    step = data["step"]
    assert step["header"]["ap"] == "AP242"
    [dim] = step["pmi"]["dimensions"]
    assert dim["type"] == "Size_Diameter" and dim["value"] == 10.0 and dim["text"] == "⌀10 +0.1/-0.05"
    cyl = dim["faces"][0]
    assert data["faces"][cyl]["geomType"] == "CYLINDER" and "dim:0" in data["faces"][cyl]["pmi"]
    assert step["products"][0]["attributes"][0] == {"name": "品番", "value": "PN-0001"}
    assert [lay["name"] for lay in step["layers"]] == ["BODY"]
    s = summarize_step(step)
    assert s["pmi"] == {"dimensions": 1} and s["pmi_texts"] == ["⌀10 +0.1/-0.05"]
    assert "pmi_units_note" not in step  # mm のファイルは換算しない


def test_step_save_keeps_pmi_attributes_and_header(tmp_path) -> None:
    pytest.importorskip("cadquery")
    from src.cad.export_mesh import Options, cad_file_to_viewer_data
    from src.cad.export_model import export_model
    from src.cad.mesh_data import apply_view_edits

    src = _pmi_step(tmp_path / "plate.step")
    data = cad_file_to_viewer_data(src, "STEP", Options())
    plain = next(f["id"] for f in data["faces"] if not f.get("pmi"))
    edited = apply_view_edits(data, face_colors={plain: "#ff0000"}, part_colors={}, color_changes=[{"kind": "face", "id": plain, "from": None, "to": "#ff0000"}])
    edited["step"]["products"][0]["attributes"].append({"name": "表面処理", "value": "黒染め"})

    edited["exportOptions"] = {"step_write_schema": "auto"}
    out = tmp_path / "saved.step"
    notes = export_model(edited, out)
    assert "Written as STEP AP242." in notes and any(n.startswith("PMI kept") for n in notes)
    back = cad_file_to_viewer_data(out, "STEP", Options())
    assert back["step"]["pmi"]["dimensions"][0]["text"] == "⌀10 +0.1/-0.05"
    assert [a["name"] for a in back["step"]["products"][0]["attributes"]] == ["品番", "質量", "表面処理"]
    assert back["faces"][plain]["color"] == "#ff0000"
    assert back["step"]["header"]["preprocessor_version"].startswith("Locohane")

    edited["exportOptions"] = {"step_write_schema": "AP214"}
    out214 = tmp_path / "saved214.step"
    notes = export_model(edited, out214)
    assert any("not written in AP214" in n for n in notes)
    back = cad_file_to_viewer_data(out214, "STEP", Options())
    assert back["step"]["header"]["ap"] == "AP214" and "pmi" not in back["step"]
    assert len(back["step"]["products"][0]["attributes"]) == 3  # 属性は AP214 でも残る


def test_step_assembly_save_drops_hidden_parts(tmp_path) -> None:
    cq = pytest.importorskip("cadquery")
    from src.cad.export_mesh import Options, cad_file_to_viewer_data
    from src.cad.export_model import export_model
    from src.cad.mesh_data import apply_view_edits

    assy = cq.Assembly(name="asm")
    assy.add(cq.Workplane().box(10, 10, 10), name="部品a", color=cq.Color(1, 0, 0, 1))
    assy.add(cq.Workplane().sphere(3), name="b", loc=cq.Location((0, 20, 0)))
    src = tmp_path / "asm.step"
    assy.export(str(src))
    data = cad_file_to_viewer_data(src, "STEP", Options())
    assert [p["name"] for p in data["parts"]] == ["部品a", "b"]
    edited = apply_view_edits(data, face_colors={}, part_colors={0: "#00ff00"}, hidden_parts=[1], color_changes=[{"kind": "part", "id": 0, "from": "#ff0000", "to": "#00ff00"}])
    edited["exportOptions"] = {"step_write_schema": "auto"}
    out = tmp_path / "saved.step"
    export_model(edited, out)
    back = cad_file_to_viewer_data(out, "STEP", Options())
    names = [p["name"] for p in back.get("parts") or []] or [back["step"]["products"][0]["name"]]
    assert "b" not in names and back["faces"][0].get("color") == "#00ff00"
    assert json.dumps(back["step"]["header"]).count("Locohane") == 1


def test_single_part_step_save_keeps_the_body_color_of_unchanged_faces(tmp_path) -> None:
    """部品の一覧が無い（アセンブリでない）STEP で 1 面だけ色を変えて保存しても、ほかの面の色は消えない。"""
    cq = pytest.importorskip("cadquery")
    from src.cad.export_mesh import Options, cad_file_to_viewer_data
    from src.cad.export_model import export_model
    from src.cad.mesh_data import apply_view_edits

    assy = cq.Assembly(name="one")
    assy.add(cq.Workplane().box(10, 10, 10), name="body", color=cq.Color(1, 0, 0, 1))
    src = tmp_path / "one.step"
    assy.export(str(src))
    data = cad_file_to_viewer_data(src, "STEP", Options())
    assert not data.get("parts")
    edited = apply_view_edits(data, face_colors={1: "#00ff00"}, part_colors={}, color_changes=[{"kind": "face", "id": 1, "from": "#ff0000", "to": "#00ff00"}])
    edited["exportOptions"] = {"step_write_schema": "auto"}
    out = tmp_path / "saved.step"
    export_model(edited, out)
    back = cad_file_to_viewer_data(out, "STEP", Options())
    colors = [f.get("color") for f in back["faces"]]
    assert colors[1] == "#00ff00"
    assert all(c == "#ff0000" for i, c in enumerate(colors) if i != 1)


def test_reopened_data_part_recolor_wins_over_an_earlier_face_color(tmp_path) -> None:
    """前の回に面の色を変えたデータを開き直し、その部品の色を変えて保存すると、面はビューアと同じ部品の色になる。"""
    cq = pytest.importorskip("cadquery")
    from src.cad.export_mesh import Options, cad_file_to_viewer_data
    from src.cad.export_model import export_model
    from src.cad.mesh_data import apply_view_edits

    assy = cq.Assembly(name="asm")
    assy.add(cq.Workplane().box(10, 10, 10), name="a", color=cq.Color(1, 0, 0, 1))
    assy.add(cq.Workplane().sphere(3), name="b", loc=cq.Location((0, 20, 0)))
    src = tmp_path / "asm.step"
    assy.export(str(src))
    data = cad_file_to_viewer_data(src, "STEP", Options())
    face = next(i for i, f in enumerate(data["faces"]) if f.get("part") == 0)
    first = apply_view_edits(data, face_colors={face: "#0000ff"}, part_colors={}, color_changes=[{"kind": "face", "id": face, "from": "#ff0000", "to": "#0000ff"}])
    second = apply_view_edits(first, face_colors={}, part_colors={0: "#00ff00"}, color_changes=[{"kind": "part", "id": 0, "from": "#ff0000", "to": "#00ff00"}])
    assert second["faces"][face]["color"] == "#00ff00"
    assert all(c["kind"] != "face" for c in second["edits"]["color_changes"])
    second["exportOptions"] = {"step_write_schema": "auto"}
    out = tmp_path / "saved.step"
    export_model(second, out)
    back = cad_file_to_viewer_data(out, "STEP", Options())
    assert back["faces"][face].get("color") == "#00ff00"
