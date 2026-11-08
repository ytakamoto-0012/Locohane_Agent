"""xlkit（excel-edit スキル）の保存直前の点検（lint）・自動折り返しと、excel-read の番地付き出力の回帰テスト。

背景（2026-11-01）: 6シートの執行計画xlsxの作成で、検証役（verifier）が
桁区切りの不揃い・折り返し漏れ・同じ値の未結合（と違う値をまたいだ結合）を
NGにし、修正と再検証の往復に45分以上かかった。どれも機械的に分かる不備のため、
保存直後に `warnings` として作業役へ返すようにした。
あわせて、修正役が read_excel の `rows[i][j]` から番地を手作業で逆算して
長考・取り違えをしていたため、各セルに `ref`（番地）と `merged`（結合範囲）を付けた。
"""

import sys
from pathlib import Path

import openpyxl

_ROOT = Path(__file__).resolve().parent.parent
_EDIT_DIR = _ROOT / "skills" / "excel-edit" / "scripts"
_READ_DIR = _ROOT / "skills" / "excel-read" / "scripts"
if str(_EDIT_DIR) not in sys.path:
    sys.path.insert(0, str(_EDIT_DIR))

from _lint import lint_sheet, sheet_summary  # noqa: E402
from xlkit import merge_same, new_workbook, write_note, write_table  # noqa: E402

_LONG_TEXT = "反省点：" + "あ" * 40  # 表示幅 88（自動調整の上限60を超える）


def test_write_table_wraps_text_longer_than_autofit_limit():
    wb = new_workbook()
    ws = wb.create_sheet("S")
    t = write_table(ws, [["項目", "内容"], ["短い", "短い"], ["長い", _LONG_TEXT]])

    assert ws.column_dimensions["B"].width == 60
    assert ws.cell(row=t.last_row, column=2).alignment.wrap_text is True
    assert not ws.cell(row=t.first_row, column=2).alignment.wrap_text
    # write_tableは折り返しだけ付ける。折返し行の高さはsave（finalize）で決まる
    # （test_excel_edit_xlkit.py側で確認）。write_noteはタイトル・注記行の高さを見る。
    assert ws.row_dimensions[t.last_row].height is None


def test_write_table_title_and_note_are_merged_and_tall_enough():
    wb = new_workbook()
    ws = wb.create_sheet("S")
    t = write_table(ws, [["項目", "金額"], ["ケーキ", 12870]], title="前提：参加者30人・予算40,000円" * 3)
    note_row = t.last_row + 2
    write_note(ws, note_row, "凡例: 金額は税抜き", last_col=t.last_col)

    assert ws["A1"].value.startswith("前提")
    assert ws["A1"].font.bold and ws["A1"].alignment.wrap_text
    assert "A1:B1" in {str(r) for r in ws.merged_cells.ranges}
    assert ws.row_dimensions[1].height > 15
    assert ws["A2"].value == "項目" and ws["A2"].font.bold  # 見出しは2行目
    assert ws["A2"].fill.fgColor.rgb.endswith("1F4E78")
    assert ws.freeze_panes == "A3"
    assert f"A{note_row}:B{note_row}" in {str(r) for r in ws.merged_cells.ranges}
    assert ws.cell(row=note_row, column=1).alignment.wrap_text
    assert lint_sheet(ws) == []


def test_lint_reports_overflow_number_format_and_unmerged_runs():
    wb = new_workbook()
    ws = wb.create_sheet("S")
    ws.column_dimensions["C"].width = 20
    rows = [
        ["時期", "単価", "メモ"],
        ["11月", 1200, _LONG_TEXT],
        ["11月", 300, "短い"],
        ["12月", "=B2+B3", "短い"],
        ["12月", 50, "短い"],
    ]
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=value)
    ws["B2"].number_format = "#,##0"
    ws.merge_cells("A4:A5")  # A列はグループ列（他のグループが結合済み）

    warnings = lint_sheet(ws)
    joined = "\n".join(warnings)
    assert "cut off" in joined and "C2" in joined
    assert "column B" in joined and "B3:B5 are General" in joined
    assert "A2:A3='11月'" in joined
    # 結合の無い列（行ごとに独立した値）の同じ値の連続は拾わない
    assert "C3:C5" not in joined


def test_lint_is_quiet_for_clean_sheet():
    wb = new_workbook()
    ws = wb.create_sheet("S")
    t = write_table(
        ws,
        [["時期", "単価"], ["11月", 1200], ["11月", 300], ["12月", 50]],
        "A1",
        number_formats={1: "#,##0"},
    )
    merge_same(ws, "A", t.first_row, t.first_row + 1)

    assert lint_sheet(ws) == []
    summary = sheet_summary(ws)
    assert summary["merged_cells"] == [f"A{t.first_row}:A{t.first_row + 1}"]
    assert summary["freeze_panes"] == f"A{t.first_row}"


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
