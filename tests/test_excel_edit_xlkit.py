"""excel-edit スキルの補助モジュール xlkit のテスト。"""

import json
import sys
from pathlib import Path

import openpyxl
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "excel-edit" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from xlkit import merge_same, new_workbook, open_workbook, save, write_table  # noqa: E402


@pytest.fixture(autouse=True)
def _without_path_memory(monkeypatch):
    monkeypatch.delenv("AGENT_SRC_DIR", raising=False)


def _rgb(cell) -> str | None:
    if cell.fill.fill_type != "solid":
        return None
    return str(cell.fill.fgColor.rgb)[-6:]


def test_write_table_title_header_body_and_row_fills(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_SRC_DIR", raising=False)
    wb = new_workbook()
    assert wb.sheetnames == []
    ws = wb.create_sheet("週間")
    body = [["4月", "第1週", "打ち合わせ"], ["4月", "第2週", "行事"], ["4月", "第3週", "片付け"], ["5月", "第1週", "-"]]
    t = write_table(ws, [["月", "週", "種別"]] + body, "A1", title="2026年 予定", row_fills=["E1D5E7", "DAEEF3", "E2EFDA", None])
    assert (t.title_row, t.header_row, t.first_row, t.last_row) == (1, 2, 3, 6)
    assert "A1:C1" in {str(r) for r in ws.merged_cells.ranges}
    assert ws["A2"].font.bold and _rgb(ws["A2"]) == "1F4E78"
    # 明示した色は縞模様で上書きされない
    assert [_rgb(ws.cell(row=r, column=1)) for r in range(3, 7)] == ["E1D5E7", "DAEEF3", "E2EFDA", "F2F2F2"]
    assert ws["B4"].border.top.style == "thin"
    assert ws.freeze_panes == "A3"
    # 全角は2文字として列幅を取る
    assert ws.column_dimensions["C"].width >= 12

    merged = merge_same(ws, "A", t.first_row, t.last_row)
    assert merged == ["A3:A5"]
    assert ws["A3"].alignment.vertical == "center"
    assert ws["A6"].alignment.vertical == "center"  # 1行だけのグループも縦中央

    out = tmp_path / "s.xlsx"
    result = save(wb, out, new=True)
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["path"] == str(out.resolve()) == result["path"]
    assert "backup" not in result
    assert openpyxl.load_workbook(out)["週間"]["C3"].value == "打ち合わせ"


def test_row_fills_length_mismatch_raises():
    ws = new_workbook().create_sheet("S")
    with pytest.raises(ValueError, match="row_fills"):
        write_table(ws, [["a"], [1], [2]], row_fills=["FFFFFF"])


def test_save_new_refuses_existing_and_edit_makes_backup(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_SRC_DIR", raising=False)
    out = tmp_path / "b.xlsx"
    wb = new_workbook()
    wb.create_sheet("S")["A1"] = 1
    save(wb, out, new=True)
    with pytest.raises(FileExistsError):
        save(wb, out, new=True)
    wb2 = open_workbook(out)
    wb2["S"]["A1"] = 2
    result = save(wb2, out)
    assert Path(result["backup"]).exists()
    assert openpyxl.load_workbook(out)["S"]["A1"].value == 2


def test_save_warns_about_unmerged_runs_in_group_column(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_SRC_DIR", raising=False)
    wb = new_workbook()
    ws = wb.create_sheet("S")
    t = write_table(ws, [["月", "x"], ["4月", 1], ["4月", 2], ["5月", 3], ["5月", 4]])
    ws.merge_cells(f"A{t.first_row}:A{t.first_row + 1}")
    result = save(wb, tmp_path / "w.xlsx", new=True)
    assert any("not merged" in w for w in result.get("warnings", []))


def test_save_rejects_new_xlsm_and_empty_workbook(tmp_path):
    wb = new_workbook()
    with pytest.raises(ValueError, match="no sheets"):
        save(wb, tmp_path / "e.xlsx", new=True)
    wb.create_sheet("S")
    with pytest.raises(ValueError, match="xlsm"):
        save(wb, tmp_path / "m.xlsm", new=True)


def test_write_note_merges_wraps_and_sets_height():
    from xlkit import write_note

    ws = new_workbook().create_sheet("S")
    t = write_table(ws, [["月", "内容", "備考"], ["4月", "a", "b"]])
    long_text = "前提: 準備・段取り・片付け・打ち合わせの時期は過去記録に無いため、一般的な基準による仮定です。" * 2
    write_note(ws, t.last_row + 2, long_text, last_col=t.last_col)
    row = t.last_row + 2
    assert f"A{row}:C{row}" in {str(r) for r in ws.merged_cells.ranges}
    assert ws.cell(row=row, column=1).alignment.wrap_text
    assert ws.row_dimensions[row].height > 15


def test_save_sets_cjk_font_for_new_workbook_with_japanese_text(tmp_path):
    wb = new_workbook()
    ws = wb.create_sheet("月別")
    write_table(ws, [["月", "予定"], ["4月", "打ち合わせ"]])
    save(wb, tmp_path / "jp.xlsx", new=True)

    saved = openpyxl.load_workbook(tmp_path / "jp.xlsx")["月別"]
    assert saved["A1"].font.name == "Yu Gothic"
    assert saved["B2"].font.name == "Yu Gothic"


def test_save_keeps_calibri_for_new_workbook_without_japanese_text(tmp_path):
    wb = new_workbook()
    ws = wb.create_sheet("S")
    write_table(ws, [["month", "plan"], ["April", "meeting"]])
    save(wb, tmp_path / "en.xlsx", new=True)

    saved = openpyxl.load_workbook(tmp_path / "en.xlsx")["S"]
    assert saved["A1"].font.name == "Calibri"


def test_save_names_fonts_of_existing_workbook_with_the_sheet_dominant_name(tmp_path):
    path = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = "既存"
    ws["A1"].font = openpyxl.styles.Font(name="Meiryo")
    wb.save(path)

    wb2 = open_workbook(path)
    ws2 = wb2["S"]
    ws2["A2"] = "追記"  # openpyxlが作った名前なしのフォント
    save(wb2, path)

    saved = openpyxl.load_workbook(path)["S"]
    assert saved["A1"].font.name == "Meiryo"
    assert saved["A2"].font.name == "Meiryo"


def test_save_sets_heights_of_wrapped_data_rows(tmp_path):
    wb = new_workbook()
    ws = wb.create_sheet("S")
    long_text = "反省点：" + "あ" * 40  # 表示幅88（自動列幅の上限60を超える）
    t = write_table(ws, [["項目", "内容"], ["短い", "短い"], ["長い", long_text]])
    wrapped_row = t.last_row
    save(wb, tmp_path / "h.xlsx", new=True)

    saved = openpyxl.load_workbook(tmp_path / "h.xlsx")["S"]
    assert saved.cell(row=wrapped_row, column=2).alignment.wrap_text
    assert saved.row_dimensions[wrapped_row].height > 15


def test_save_keeps_an_explicit_row_height(tmp_path):
    wb = new_workbook()
    ws = wb.create_sheet("S")
    t = write_table(ws, [["項目", "内容"], ["a", "b"], ["c", "長テキスト長テキスト長テキスト長テキスト長テキスト長テキスト"]])
    wrapped_row = t.last_row
    ws.row_dimensions[wrapped_row].height = 20
    save(wb, tmp_path / "h2.xlsx", new=True)

    saved = openpyxl.load_workbook(tmp_path / "h2.xlsx")["S"]
    assert saved.row_dimensions[wrapped_row].height == 20
