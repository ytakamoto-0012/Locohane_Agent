"""excel-edit が保存直前にフォント名と折り返し行の高さを確定させる処理（finalize_sheets）のテスト。

背景（2026-11-05 evals 001）: 内容は全項目 OK なのに、行の高さ未設定（レンダリングで
折り返しが切れる）とフォント名未設定（日本語が Calibri）だけで verifier が NG にし、
worker と verifier の2周目に約17分かかった。
"""

import json
import subprocess
import sys
from pathlib import Path

import openpyxl

_ROOT = Path(__file__).resolve().parent.parent
_EDIT_DIR = _ROOT / "skills" / "excel-edit" / "scripts"
if str(_EDIT_DIR) not in sys.path:
    sys.path.insert(0, str(_EDIT_DIR))

from _ops import apply_op, finalize_sheets, pop_op_warnings  # noqa: E402


def _wb():
    wb = openpyxl.Workbook()
    wb.active.title = "Sheet1"
    pop_op_warnings()
    return wb, wb.active


def test_wrapped_row_gets_height_and_explicit_height_is_kept():
    wb, ws = _wb()
    long_text = "備考：" + "あ" * 50
    rows = [["月", "備考"], ["1月", long_text], ["2月", "短い"], ["3月", long_text]]
    apply_op(wb, {"op": "set_range", "sheet": "Sheet1", "start_cell": "A1", "rows": rows, "header_style": {}})
    apply_op(wb, {"op": "set_row_height", "sheet": "Sheet1", "row": 4, "height": 20})
    finalize_sheets(wb, [ws], new_workbook=True)
    assert ws.row_dimensions[2].height is not None and ws.row_dimensions[2].height > 15
    assert ws.row_dimensions[3].height is None
    assert ws.row_dimensions[4].height == 20


def test_merged_title_uses_merged_width_and_vertical_merge_is_ignored():
    wb, ws = _wb()
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 10
    ws["A1"] = "あ" * 9  # 幅 18：A:B 結合（20）なら1行に収まる
    apply_op(wb, {"op": "set_style", "sheet": "Sheet1", "range": "A1", "style": {"wrap_text": True}})
    ws.merge_cells("A1:B1")
    ws["A2"] = "あ" * 30
    apply_op(wb, {"op": "set_style", "sheet": "Sheet1", "range": "A2", "style": {"wrap_text": True}})
    ws.merge_cells("A2:A4")
    finalize_sheets(wb, [ws], new_workbook=False)
    assert ws.row_dimensions[1].height is None
    assert ws.row_dimensions[2].height is None


def test_new_workbook_with_japanese_gets_cjk_font_on_styled_cells():
    wb, ws = _wb()
    apply_op(wb, {"op": "set_range", "sheet": "Sheet1", "start_cell": "A1", "rows": [["月", "行事"], ["1月", "文化祭"]], "header_style": {}})
    finalize_sheets(wb, [ws], new_workbook=True)
    assert ws["A1"].font.name == "Yu Gothic" and ws["A1"].font.bold
    assert ws["B2"].font.name == "Yu Gothic"


def test_existing_workbook_keeps_named_fonts_and_names_only_unnamed_ones():
    wb, ws = _wb()
    ws["A1"] = "既存"
    ws["A1"].font = openpyxl.styles.Font(name="Meiryo", size=10)
    ws["A2"] = "既存2"
    ws["A2"].font = openpyxl.styles.Font(name="Meiryo", size=10)
    apply_op(wb, {"op": "set_cell", "sheet": "Sheet1", "cell": "A3", "value": "追加", "style": {"bold": True}})
    finalize_sheets(wb, [ws], new_workbook=False)
    assert ws["A1"].font.name == "Meiryo"
    assert ws["A3"].font.name == "Meiryo" and ws["A3"].font.bold and ws["A3"].font.size == 11


def test_english_new_workbook_keeps_calibri(tmp_path):
    wb, ws = _wb()
    apply_op(wb, {"op": "set_range", "sheet": "Sheet1", "start_cell": "A1", "rows": [["Month", "Event"], ["Jan", "Fair"]], "header_style": {}})
    finalize_sheets(wb, [ws], new_workbook=True)
    assert ws["A1"].font.name == "Calibri"


def test_edit_excel_cli_applies_finalize(tmp_path):
    out = tmp_path / "a.xlsx"
    ops = [
        {"op": "add_sheet", "name": "予定"},
        {"op": "set_range", "sheet": "予定", "start_cell": "A1", "rows": [["月", "備考"], ["1月", "い" * 80]], "header_style": {}},
    ]
    res = subprocess.run(
        [sys.executable, str(_EDIT_DIR / "edit_excel.py"), str(out), "--new", "--ops-json", json.dumps(ops, ensure_ascii=False)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert res.returncode == 0, res.stderr
    ws = openpyxl.load_workbook(out)["予定"]
    assert ws["B2"].font.name == "Yu Gothic"
    assert ws.row_dimensions[2].height and ws.row_dimensions[2].height > 15
