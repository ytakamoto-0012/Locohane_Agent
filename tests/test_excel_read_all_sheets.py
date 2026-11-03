"""read_excel.py の `--all-sheets`（全シートを1回で読む）の回帰テスト。

シート一覧 → シートごとに1回ずつ、と呼ぶと小さいブックでも呼び出しがシート数+1回になり、
explore の反復がその分増えていた（本番ログ 2026-11-03: 7シートのブックを1回に1シートずつ読んだ）。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import openpyxl

_SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "excel-read" / "scripts" / "read_excel.py"


def _make_book(tmp_path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "年度別比較"
    ws["A1"] = "項目"
    ws["B1"] = "2025年度"
    ws["A2"] = "予算"
    ws["B2"] = 40000
    ws2 = wb.create_sheet("当日の流れ")
    for i in range(1, 6):
        ws2[f"A{i}"] = f"手順{i}"
    path = tmp_path / "book.xlsx"
    wb.save(path)
    return path


def _run(tmp_path, *args) -> subprocess.CompletedProcess:
    env = {**os.environ, "AGENT_DEFAULT_WORKDIR": str(tmp_path), "AGENT_THREAD_ID": "t"}
    return subprocess.run([sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, encoding="utf-8", env=env)


def test_all_sheets_reads_every_sheet_in_one_call(tmp_path) -> None:
    path = _make_book(tmp_path)

    proc = _run(tmp_path, str(path), "--all-sheets", "--limit", "3", "--query-json", '[{"op": "list_images"}]')

    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["mode"] == "all_sheets"
    by_name = {s["sheet"]: s for s in summary["sheets"]}
    assert by_name["年度別比較"]["truncated"] is False
    # --limit は各シートに効き、続きのあるシートは truncated で分かる。
    assert by_name["当日の流れ"] == {
        "sheet": "当日の流れ", "total_rows": 5, "total_columns": 1, "end_row": 3, "truncated": True, "query_results_count": 1,
    }
    full = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    first = full["sheets"][0]
    assert first["sheet"] == "年度別比較"
    assert first["rows"][1][1] == {"ref": "B2", "value": 40000}
    assert first["query_results"][0]["op"] == "list_images"


def test_all_sheets_rejects_sheet_and_columns(tmp_path) -> None:
    path = _make_book(tmp_path)

    proc = _run(tmp_path, str(path), "--all-sheets", "--sheet", "0")

    assert proc.returncode == 1
    assert "--all-sheets cannot be combined" in proc.stderr


def test_text_path_has_one_line_per_row_without_empty_cells(tmp_path) -> None:
    """result_path の JSON は1セルが数行で、小さいブックでも千行を超えて Read を何回にも分けていた。

    中身を読むための text_path は1行1行・空セル省略で、続きのあるシートはその読み方も示す。
    """
    path = _make_book(tmp_path)

    proc = _run(tmp_path, str(path), "--all-sheets", "--limit", "3")

    assert proc.returncode == 0, proc.stderr
    text = Path(json.loads(proc.stdout)["text_path"]).read_text(encoding="utf-8")
    assert "## 年度別比較 (rows 1-2 of 2)" in text
    assert "A1=項目 | B1=2025年度" in text
    assert "A2=予算 | B2=40000" in text
    assert "## 当日の流れ (rows 1-3 of 5)" in text
    assert '(rows 4-5 not read; read them with --sheet "当日の流れ" --offset 3)' in text


def test_single_sheet_mode_also_writes_text_path(tmp_path) -> None:
    path = _make_book(tmp_path)

    proc = _run(tmp_path, str(path), "--sheet", "年度別比較")

    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert "A2=予算 | B2=40000" in Path(summary["text_path"]).read_text(encoding="utf-8")


def _make_book_with_chartsheet(tmp_path) -> Path:
    from openpyxl.chart import BarChart, Reference

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "データ"
    for i, v in enumerate([3, 5, 2], start=1):
        ws[f"A{i}"] = v
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=1, min_row=1, max_row=3))
    cs = wb.create_chartsheet("グラフ")
    cs.add_chart(chart)
    path = tmp_path / "chart.xlsx"
    wb.save(path)
    return path


def test_chartsheet_does_not_crash_sheet_list_or_all_sheets(tmp_path) -> None:
    """グラフだけのシート（Chartsheet）があると max_row 等が無く AttributeError で落ちていた。"""
    path = _make_book_with_chartsheet(tmp_path)

    listed = _run(tmp_path, str(path))
    assert listed.returncode == 0, listed.stderr
    listed_full = json.loads(Path(json.loads(listed.stdout)["result_path"]).read_text(encoding="utf-8"))
    sheets = {s["name"]: s for s in listed_full["sheets"]}
    assert sheets["グラフ"]["chartsheet"] is True
    assert sheets["データ"]["max_row"] == 3

    proc = _run(tmp_path, str(path), "--all-sheets")
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    by_name = {s["sheet"]: s for s in summary["sheets"]}
    assert by_name["グラフ"]["chartsheet"] is True
    assert by_name["データ"]["total_rows"] == 3
    assert "chart-only sheet" in Path(summary["text_path"]).read_text(encoding="utf-8")


def test_all_sheets_loads_the_workbook_once(tmp_path, monkeypatch) -> None:
    """シートごとにブック全体を読み直すと、大きいブックでスクリプトの時間切れになる。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("read_excel_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SCRIPT.parent))
    calls = []
    real_load = openpyxl.load_workbook
    monkeypatch.setattr(openpyxl, "load_workbook", lambda *a, **k: calls.append(1) or real_load(*a, **k))

    result = module._read_all_sheets(_make_book(tmp_path), ".xlsx", 0, 100, False, None, True)

    assert [s["sheet"] for s in result["sheets"]] == ["年度別比較", "当日の流れ"]
    assert len(calls) == 1
