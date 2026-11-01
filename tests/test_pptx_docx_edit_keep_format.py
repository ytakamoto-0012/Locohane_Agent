"""pptx-edit のテキスト差し替え時の書式保持・保存後チェックと、docx-edit の run をまたぐ find_replace の回帰テスト。

背景（2026-11-01）: excel-edit で「既存の書式が巻き添えで消える」「機械的に分かる不備を
検証役に回している」ことが修正往復の主因だったため、pptx/docx の同種の問題を洗い出した。
- python-pptx の `text_frame.text`/`cell.text` への代入は段落ごと作り直すため、
  表の見出しセルの白文字・太字・中央揃え等が差し替えで黙って消えていた。
- docx-edit の find_replace は run 単位の検索だけで、段落に見えている文字列でも
  run をまたぐと `old_text not found` になり、old_text を短くして試し直す往復が起きていた。

docx-edit と excel-edit はどちらも `_ops.py` を持ち、同一プロセスで import すると
sys.modules のキャッシュが衝突するため、スクリプトは別プロセスで実行する。
"""

import json
import subprocess
import sys
from pathlib import Path

from docx import Document
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Cm, Inches, Pt

_ROOT = Path(__file__).resolve().parent.parent
_EDIT_PPTX = _ROOT / "skills" / "pptx-edit" / "scripts" / "edit_pptx.py"
_EDIT_DOCX = _ROOT / "skills" / "docx-edit" / "scripts" / "edit_docx.py"


def _run(args: list[str]) -> dict:
    proc = subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _make_pptx(path: Path) -> None:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    for c in range(2):
        cell = table.cell(0, c)
        cell.text = f"見出し{c}"
        para = cell.text_frame.paragraphs[0]
        para.alignment = PP_ALIGN.CENTER
        run = para.runs[0]
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        run.font.bold = True
        run.font.size = Pt(20)
    box = slide.shapes.add_textbox(Inches(1), Inches(4), Inches(3), Inches(1)).text_frame
    box.text = "箇条"
    box.paragraphs[0].runs[0].font.size = Pt(28)
    # スライド右端からはみ出す図形（保存後チェックの対象）
    slide.shapes.add_textbox(prs.slide_width - Cm(2), Inches(5), Cm(5), Cm(1))
    prs.save(str(path))


def test_pptx_text_replacement_keeps_run_and_paragraph_format(tmp_path):
    src, out = tmp_path / "in.pptx", tmp_path / "out.pptx"
    _make_pptx(src)
    data = {
        "operations": [
            {"op": "set_table", "slide": 1, "shape_index": 1, "headers": ["新A", "新B"], "rows": [["1", "2"]]},
            {"op": "set_table_cell", "slide": 1, "shape_index": 1, "row": 0, "col": 1, "text": "新C"},
            {"op": "set_text", "slide": 1, "shape_index": 2, "bullets": ["一つ目", "二つ目"]},
        ]
    }
    result = _run([str(_EDIT_PPTX), str(src), str(out), "--data", json.dumps(data, ensure_ascii=False)])

    slide = Presentation(str(out)).slides[0]
    for c, expected in enumerate(["新A", "新C"]):
        para = slide.shapes[1].table.cell(0, c).text_frame.paragraphs[0]
        run = para.runs[0]
        assert run.text == expected
        assert run.font.color.rgb == RGBColor(0xFF, 0xFF, 0xFF)
        assert run.font.bold is True
        assert run.font.size == Pt(20)
        assert para.alignment == PP_ALIGN.CENTER
    bullets = slide.shapes[2].text_frame.paragraphs
    assert [p.runs[0].text for p in bullets] == ["一つ目", "二つ目"]
    assert all(p.runs[0].font.size == Pt(28) for p in bullets)

    assert any("slide 1: shape_index 3" in w and "right edge" in w for w in result["warnings"])
    assert [s["slide"] for s in result["slide_summaries"]] == [1]
    assert result["slide_summaries"][0]["shapes"][1]["table_dims"] == {"rows": 2, "cols": 2}


def _make_docx(path: Path) -> None:
    doc = Document()
    para = doc.add_paragraph()
    para.add_run("請求先は")
    para.add_run("株式会").bold = True
    para.add_run("社Aです。株式会社Aへ送付。")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "表の中の文言"
    doc.save(str(path))


def test_docx_find_replace_crosses_runs_and_keeps_start_run_format(tmp_path):
    path = tmp_path / "doc.docx"
    _make_docx(path)
    ops = [{"op": "find_replace", "old_text": "株式会社A", "new_text": "株式会社B"}]
    result = _run([str(_EDIT_DOCX), str(path), "--ops-json", json.dumps(ops, ensure_ascii=False)])

    assert result["op_results"][0] == {"op": "find_replace", "replaced_count": 2, "cross_run_count": 1}
    para = Document(str(path)).paragraphs[0]
    assert para.text == "請求先は株式会社Bです。株式会社Bへ送付。"
    assert [r.text for r in para.runs] == ["請求先は", "株式会社B", "です。株式会社Bへ送付。"]
    assert para.runs[1].bold is True


def test_docx_find_replace_not_found_points_to_where_text_appears(tmp_path):
    path = tmp_path / "doc.docx"
    _make_docx(path)

    ops = [{"op": "find_replace", "old_text": "表の中", "new_text": "x"}]
    proc = subprocess.run(
        [sys.executable, str(_EDIT_DOCX), str(path), "--ops-json", json.dumps(ops, ensure_ascii=False)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 1
    assert "table(s) [0]" in proc.stderr

    ops = [{"op": "find_replace", "old_text": "株式会社A", "new_text": "x", "track_changes": True, "occurrence": "first"}]
    proc = subprocess.run(
        [sys.executable, str(_EDIT_DOCX), str(path), "--ops-json", json.dumps(ops, ensure_ascii=False)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    # 2つ目の一致（1つのrunに収まっている）は変更履歴付きで置換できる
    assert proc.returncode == 0, proc.stderr
