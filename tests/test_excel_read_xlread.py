"""excel-read スキルの補助モジュール xlread のテスト。"""

import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import PatternFill

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "excel-read" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from xlread import read_table  # noqa: E402


def _book(tmp_path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "週間"
    ws["A1"] = "2027年 週間予定"
    ws.merge_cells("A1:C1")
    for col, value in zip("ABC", ["月", "週", "内容"]):
        ws[f"{col}2"] = value
    body = [["4月", "W1", "準備"], [None, "W2", "行事"], ["5月", "W1", None]]
    for i, row in enumerate(body, start=3):
        for col, value in zip("ABC", row):
            ws[f"{col}{i}"] = value
    ws.merge_cells("A3:A4")
    ws["A4"].fill = PatternFill(fill_type="solid", start_color="DDEBF7", end_color="DDEBF7")
    ws["B4"].fill = PatternFill(fill_type="solid", start_color="DDEBF7", end_color="DDEBF7")
    ws["A6"] = "注記: 週は月曜始まり"
    ws.merge_cells("A6:C6")
    path = tmp_path / "t.xlsx"
    wb.save(path)
    return path


def test_read_table_finds_the_header_and_fills_merged_values(tmp_path):
    rows = read_table(_book(tmp_path), "週間")
    assert [r["_row"] for r in rows] == [3, 4, 5]
    assert [r["月"] for r in rows] == ["4月", "4月", "5月"]
    assert rows[2]["内容"] is None
    assert rows[0]["_fill"] is None


def test_read_table_stops_at_a_note_merged_across_the_table(tmp_path):
    path = _book(tmp_path)
    wb = openpyxl.load_workbook(path)
    ws = wb["週間"]
    ws.unmerge_cells("A6:C6")
    ws["A6"] = None
    ws["A5"].value = "5月"
    ws.insert_rows(6)
    ws["A6"] = "前提: 推定"
    ws.merge_cells("A6:C6")
    wb.save(path)
    rows = read_table(path, 0, header_row=2)
    assert [r["_row"] for r in rows] == [3, 4, 5]


def test_read_table_numbers_duplicate_headers(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["備考", "備考", "x"])
    ws.append([1, 2, 3])
    path = tmp_path / "d.xlsx"
    wb.save(path)
    assert read_table(path)[0] == {"備考": 1, "備考_2": 2, "x": 3, "_row": 2, "_fill": None}
