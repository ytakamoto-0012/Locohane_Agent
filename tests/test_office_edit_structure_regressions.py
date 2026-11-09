"""docx-edit / pptx-edit / pptx-create の構造まわりの回帰テスト（2026-11-10 のコード点検で見つかった不具合）。"""

import json
import subprocess
import sys
from pathlib import Path

import docx
from PIL import Image
from pptx import Presentation
from pptx.oxml.ns import qn as pptx_qn
from pptx.util import Cm

_SKILLS = Path(__file__).resolve().parent.parent / "skills"
_EDIT_DOCX = _SKILLS / "docx-edit" / "scripts" / "edit_docx.py"
_EDIT_PPTX = _SKILLS / "pptx-edit" / "scripts" / "edit_pptx.py"
_CREATE_PPTX = _SKILLS / "pptx-create" / "scripts" / "create_pptx.py"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _run(args, cwd):
    proc = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, encoding="utf-8", cwd=cwd)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_docx_insert_image_after_paragraph_followed_by_table(tmp_path):
    """直後に表がある段落の後ろを指定すると、画像が表の後ろに入っていた。"""
    Image.new("RGB", (40, 20), (0, 0, 200)).save(tmp_path / "i.png")
    d = docx.Document()
    d.add_paragraph("P0")
    d.add_table(rows=1, cols=1).cell(0, 0).text = "T"
    d.add_paragraph("P1")
    d.save(tmp_path / "a.docx")
    ops = [{"op": "insert_image", "image_path": str(tmp_path / "i.png"), "after_index": 0}]
    _run([_EDIT_DOCX, tmp_path / "a.docx", "--ops-json", json.dumps(ops)], tmp_path)
    body = [el.tag.split("}")[1] for el in docx.Document(tmp_path / "a.docx").element.body]
    assert body[:3] == ["p", "p", "tbl"]
    second = docx.Document(tmp_path / "a.docx").element.body[1]
    assert second.findall(".//" + "{http://schemas.openxmlformats.org/drawingml/2006/main}blip")


def test_docx_find_replace_reaches_tables_and_headers(tmp_path):
    d = docx.Document()
    d.add_paragraph("旧社名")
    d.add_table(rows=1, cols=1).cell(0, 0).text = "旧社名 様"
    d.sections[0].header.paragraphs[0].text = "旧社名"
    d.save(tmp_path / "a.docx")
    ops = [{"op": "find_replace", "old_text": "旧社名", "new_text": "新社名"}]
    result = _run([_EDIT_DOCX, tmp_path / "a.docx", "--ops-json", json.dumps(ops, ensure_ascii=False)], tmp_path)
    assert result["op_results"][0]["replaced_count"] == 3
    saved = docx.Document(tmp_path / "a.docx")
    assert saved.tables[0].cell(0, 0).text == "新社名 様"
    assert saved.sections[0].header.paragraphs[0].text == "新社名"


def test_docx_styles_keep_schema_order_and_do_not_duplicate(tmp_path):
    d = docx.Document()
    p = d.add_paragraph("x")
    p.alignment = 1
    d.add_table(rows=1, cols=1).cell(0, 0).text = "h"
    d.save(tmp_path / "a.docx")
    ops = [
        {"op": "set_paragraph_style", "index": 0, "fill_color": "FFEEAA", "border_color": "FF0000"},
        {"op": "set_paragraph_style", "index": 0, "fill_color": "AAEEFF"},
        {"op": "set_table_style", "table_index": 0, "theme": "forest"},
        {"op": "set_table_style", "table_index": 0, "theme": "navy"},
    ]
    _run([_EDIT_DOCX, tmp_path / "a.docx", "--ops-json", json.dumps(ops)], tmp_path)
    saved = docx.Document(tmp_path / "a.docx")
    assert [c.tag.split("}")[1] for c in saved.paragraphs[0]._p.pPr] == ["pBdr", "shd", "jc"]
    assert saved.paragraphs[0]._p.pPr.find(_W + "shd").get(_W + "fill") == "AAEEFF"
    assert len(saved.tables[0].cell(0, 0)._tc.tcPr.findall(_W + "shd")) == 1


def test_pptx_duplicate_slide_relinks_hyperlinks_and_pictures(tmp_path):
    """XML だけをコピーし、ハイパーリンクの r:id が新しいスライドに無いまま残っていた。"""
    Image.new("RGB", (40, 20), (200, 0, 0)).save(tmp_path / "i.png")
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    box = slide.shapes.add_textbox(Cm(1), Cm(5), Cm(5), Cm(2))
    run = box.text_frame.paragraphs[0].add_run()
    run.text = "link"
    run.hyperlink.address = "https://example.com"
    slide.shapes.add_picture(str(tmp_path / "i.png"), Cm(8), Cm(5))
    prs.save(tmp_path / "a.pptx")
    data = {"operations": [{"op": "duplicate_slide", "slide": 1}]}
    _run([_EDIT_PPTX, tmp_path / "a.pptx", tmp_path / "b.pptx", "--data", json.dumps(data)], tmp_path)
    copy = Presentation(tmp_path / "b.pptx").slides[1]
    refs = [v for el in copy._element.iter() for k, v in el.attrib.items() if k.startswith("{" + pptx_qn("r:id").split("}")[0][1:] + "}")]
    assert refs and all(r in copy.part.rels for r in refs)
    assert any(rel.is_external and rel.target_ref == "https://example.com" for rel in copy.part.rels.values())


def test_pptx_bullets_given_as_string_are_lines_not_characters(tmp_path):
    data = {"slides": [{"layout": "content", "title": "t", "bullets": "一行目\n二行目"}]}
    _run([_CREATE_PPTX, tmp_path / "a.pptx", "--data", json.dumps(data, ensure_ascii=False)], tmp_path)
    body = Presentation(tmp_path / "a.pptx").slides[0].placeholders[1].text_frame
    assert [p.text for p in body.paragraphs] == ["一行目", "二行目"]
    edit = {"operations": [{"op": "set_text", "slide": 1, "shape_index": 1, "bullets": "A\nB"}]}
    _run([_EDIT_PPTX, tmp_path / "a.pptx", tmp_path / "b.pptx", "--data", json.dumps(edit)], tmp_path)
    body = Presentation(tmp_path / "b.pptx").slides[0].shapes[1].text_frame
    assert [p.text for p in body.paragraphs] == ["A", "B"]
