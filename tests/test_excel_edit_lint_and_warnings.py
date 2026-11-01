"""excel-edit の保存後チェック・op警告・自動折り返しと、excel-read の番地付き出力の回帰テスト。

背景（2026-11-01）: 6シートの執行計画xlsxの作成で、検証役（verifier）が
桁区切りの不揃い・折り返し漏れ・同じ値の未結合（と違う値をまたいだ結合）を
NGにし、修正と再検証の往復に45分以上かかった。どれも機械的に分かる不備のため、
edit_excel.py が保存直後に `warnings` として作業役へ返すようにした。
あわせて、修正役が read_excel の `rows[i][j]` から番地を手作業で逆算して
長考・取り違えをしていたため、各セルに `ref`（番地）と `merged`（結合範囲）を付けた。
"""

import json
import subprocess
import sys
from pathlib import Path

import openpyxl

_ROOT = Path(__file__).resolve().parent.parent
_EDIT_DIR = _ROOT / "skills" / "excel-edit" / "scripts"
_READ_DIR = _ROOT / "skills" / "excel-read" / "scripts"
if str(_EDIT_DIR) not in sys.path:
    sys.path.insert(0, str(_EDIT_DIR))

from _lint import lint_sheet, sheet_summary  # noqa: E402
from _ops import apply_op, pop_op_warnings  # noqa: E402

_LONG_TEXT = "反省点：" + "あ" * 40  # 表示幅 88（自動調整の上限60を超える）


def _wb():
    wb = openpyxl.Workbook()
    wb.active.title = "Sheet1"
    pop_op_warnings()
    return wb, wb.active


def test_set_style_valign_keeps_existing_wrap_and_align():
    wb, ws = _wb()
    apply_op(wb, {"op": "set_cell", "sheet": "Sheet1", "cell": "A1", "value": "x", "style": {"wrap_text": True, "align": "left"}})
    apply_op(wb, {"op": "set_style", "sheet": "Sheet1", "range": "A1", "style": {"valign": "center"}})

    alignment = ws["A1"].alignment
    assert alignment.vertical == "center"
    assert alignment.wrap_text is True
    assert alignment.horizontal == "left"


def test_set_range_wraps_text_longer_than_autofit_limit():
    wb, ws = _wb()
    apply_op(wb, {"op": "set_range", "sheet": "Sheet1", "start_cell": "A1", "rows": [["項目", "内容"], ["短い", "短い"], ["長い", _LONG_TEXT]], "header_style": {}})

    assert ws.column_dimensions["B"].width == 60
    assert ws["B3"].alignment.wrap_text is True
    assert not ws["B2"].alignment.wrap_text


def test_set_column_width_allows_up_to_255_and_warns_when_clamped():
    wb, ws = _wb()
    apply_op(wb, {"op": "set_column_width", "sheet": "Sheet1", "column": "C", "width": 75})
    assert ws.column_dimensions["C"].width == 75
    assert pop_op_warnings() == []

    apply_op(wb, {"op": "set_column_width", "sheet": "Sheet1", "column": "C", "width": 300})
    assert ws.column_dimensions["C"].width == 255
    warnings = pop_op_warnings()
    assert len(warnings) == 1 and "clamped to 255" in warnings[0]


def test_merge_cells_warns_only_when_different_values_are_discarded():
    wb, ws = _wb()
    for row, value in enumerate(["役員", "役員", "役員"], start=1):
        ws.cell(row=row, column=1, value=value)
    for row, value in enumerate(["10月〜", "11月", "11月"], start=1):
        ws.cell(row=row, column=2, value=value)

    apply_op(wb, {"op": "merge_cells", "sheet": "Sheet1", "range": "A1:A3"})
    assert pop_op_warnings() == []

    apply_op(wb, {"op": "merge_cells", "sheet": "Sheet1", "range": "B1:B3"})
    warnings = pop_op_warnings()
    assert len(warnings) == 1
    assert "B2='11月'" in warnings[0] and "B3='11月'" in warnings[0]


def test_lint_reports_overflow_number_format_and_unmerged_runs():
    wb, ws = _wb()
    ws.column_dimensions["C"].width = 20
    rows = [
        ["時期", "単価", "メモ"],
        ["11月", 1200, _LONG_TEXT],
        ["11月", 300, "短い"],
        ["12月", "=B2+B3", "短い"],
    ]
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=value)
    ws["B2"].number_format = "#,##0"

    warnings = lint_sheet(ws)
    joined = "\n".join(warnings)
    assert "cut off" in joined and "C2" in joined
    assert "column B" in joined and "B3:B4 are General" in joined
    assert "A2:A3='11月'" in joined


def test_lint_is_quiet_for_clean_sheet():
    wb, ws = _wb()
    apply_op(wb, {"op": "set_range", "sheet": "Sheet1", "start_cell": "A1", "rows": [["時期", "単価"], ["11月", 1200], ["11月", 300], ["12月", 50]], "header_style": {}})
    apply_op(wb, {"op": "set_style", "sheet": "Sheet1", "range": "B2:B4", "style": {"number_format": "#,##0"}})
    apply_op(wb, {"op": "merge_cells", "sheet": "Sheet1", "range": "A2:A3"})

    assert lint_sheet(ws) == []
    summary = sheet_summary(ws)
    assert summary["merged_cells"] == ["A2:A3"]
    assert summary["freeze_panes"] == "A2"


def test_edit_excel_returns_warnings_and_summaries_for_touched_sheets_only(tmp_path):
    path = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "untouched"
    wb.active["A1"] = "同じ"
    wb.active["A2"] = "同じ"
    wb.save(path)

    ops = [
        {"op": "add_sheet", "name": "work"},
        {"op": "set_range", "sheet": "work", "start_cell": "A1", "rows": [["担当"], ["役員"], ["役員"]], "header_style": {}},
        {"op": "rename_sheet", "name": "work", "new_name": "renamed"},
    ]
    proc = subprocess.run(
        [sys.executable, str(_EDIT_DIR / "edit_excel.py"), str(path), "--ops-json", json.dumps(ops, ensure_ascii=False)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert [s["sheet"] for s in result["sheet_summaries"]] == ["renamed"]
    assert any("renamed: consecutive rows with the same value" in w for w in result["warnings"])
    assert not any(w.startswith("untouched") for w in result["warnings"])


def test_read_excel_cells_carry_ref_and_merged(tmp_path):
    if str(_READ_DIR) not in sys.path:
        sys.path.insert(0, str(_READ_DIR))
    from read_excel import _read_xlsx

    path = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for r, row in enumerate([["時期", "やること"], ["11月", "a"], [None, "b"], ["12月", "c"]], start=1):
        for c, value in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=value)
    ws.merge_cells("A2:A3")
    wb.save(path)

    result = _read_xlsx(path, sheet_arg="Sheet1", offset=1, limit=2, data_only=False, columns="A")
    assert result["rows"] == [
        [{"ref": "A2", "value": "11月", "merged": "A2:A3"}],
        [{"ref": "A3", "value": None, "merged": "A2:A3"}],
    ]
