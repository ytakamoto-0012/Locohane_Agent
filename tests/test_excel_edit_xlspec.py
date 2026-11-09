"""excel-edit スキルの補助モジュール xlspec のテスト。"""

import sys
import textwrap
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "excel-edit" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from xlkit import new_workbook, save, write_table  # noqa: E402
from xlspec import records, run_checks  # noqa: E402

_NOTES = """## 2021 activities
<!-- 2026-11-07 08:55:00 by explore -->
Cards are text images.

```records
{"date": "2021-01-24", "name": "節分祭", "participants": 27, "source": "2021/photo_01.png"}
{"date": "2021-04-25", "name": "川遊び", "participants": 18, "source": "2021/photo_04.png"},
```

## 2022 activities
<!-- 2026-11-07 08:56:00 by explore -->
```records
{"date": "2022-11-16", "name": null, "participants": 28, "source": "2022/photo_04.png"}
```

## Rules
<!-- 2026-11-07 09:00:00 by main -->
weeks start on Monday
"""


def _notes(tmp_path, text=_NOTES) -> Path:
    path = tmp_path / "_thread_notes.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_records_reads_every_block_in_order_with_its_topic(tmp_path):
    rows = records(notes_path=_notes(tmp_path))
    assert [r["name"] for r in rows] == ["節分祭", "川遊び", None]
    assert rows[2]["_topic"] == "2022 activities"
    assert [r["date"] for r in records("2022 activities", notes_path=_notes(tmp_path))] == ["2022-11-16"]


def test_records_skips_a_bad_line_and_reports_it(tmp_path, capsys):
    bad = _notes(tmp_path, _NOTES.replace('"participants": 27', "participants: 27"))
    rows = records(notes_path=bad)
    assert [r["name"] for r in rows] == ["川遊び", None]
    report = capsys.readouterr().out
    assert '"skipped_lines": 1' in report and "2021 activities" in report
    with pytest.raises(ValueError, match="No ```records block"):
        records("Rules", notes_path=_notes(tmp_path))


def test_records_splits_objects_on_one_line_and_merges_identical_lines(tmp_path, capsys):
    text = _NOTES.replace(
        '{"date": "2022-11-16", "name": null, "participants": 28, "source": "2022/photo_04.png"}',
        '{"date": "2022-11-16", "participants": 28, "source": "2022/photo_04.png"}{"date": "2022-12-15", "name": "夏祭り", "source": "2022/photo_01.png"}\n'
        '{"date": "2022-11-16", "participants": 28, "source": "2022/photo_04.png"}',
    )
    rows = records("2022 activities", notes_path=_notes(tmp_path, text))
    assert [r["date"] for r in rows] == ["2022-11-16", "2022-12-15"]
    assert "name" not in rows[0]
    assert '"duplicate_lines_merged": 1' in capsys.readouterr().out


def test_run_checks_separates_failed_requirements_from_broken_checks(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_SRC_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    _notes(tmp_path)
    (tmp_path / "rules.py").write_text("def month_label(date):\n    return f\"{int(date[5:7])}月\"\n", encoding="utf-8")
    wb = new_workbook()
    ws = wb.create_sheet("月間")
    rows = [[month, r["name"] or "不明"] for r in records() for month in [__import__("rules").month_label(r["date"])]]
    write_table(ws, [["月", "行事"]] + rows[:2], "A1", title="2027年")  # わざと1件落とす
    out = tmp_path / "s.xlsx"
    save(wb, out, new=True)
    (tmp_path / "checks.py").write_text(
        textwrap.dedent(
            """
            from xlspec import check, records
            import rules

            @check("every record has a row")
            def _(book):
                have = {(r["月"], r["行事"]) for r in book.table("月間")}
                return [f"missing {rules.month_label(r['date'])} {r['name'] or '不明'}" for r in records()
                        if (rules.month_label(r["date"]), r["name"] or "不明") not in have]

            @check("title present")
            def _(book):
                return any(v == "2027年" for _, v in book.texts("月間"))

            @check("broken check")
            def _(book):
                return book.table("月間")[0]["存在しない列"]
            """
        ),
        encoding="utf-8",
    )
    result = run_checks(out, "checks.py")
    assert result["checks"] == 3 and result["passed"] == 1
    assert result["failed"] == [{"check": "every record has a row", "problems": ["missing 11月 不明"]}]
    assert result["errors"][0]["check"] == "broken check" and "KeyError" in result["errors"][0]["error"]


def test_records_keeps_identical_records_written_in_different_topics(tmp_path, capsys):
    """同じ日・同じ金額の取引のように、別の調査（トピック）に同じ内容があれば別の記録として残す。"""
    line = '{"date": "2021-05-01", "item": "文具", "amount": 1234}'
    head_a = "## 2021 receipts A\n<!-- 2026-11-07 08:55:00 by explore -->\n```records\n"
    head_b = "## 2021 receipts B\n<!-- 2026-11-07 08:56:00 by explore -->\n```records\n"
    text = head_a + line + "\n```\n\n" + head_b + line + "\n" + line + "\n```\n"
    rows = records(notes_path=_notes(tmp_path, text))
    assert [r["_topic"] for r in rows] == ["2021 receipts A", "2021 receipts B"]
    assert '"duplicate_lines_merged": 1' in capsys.readouterr().out  # B の中の重複だけをまとめる


def test_run_checks_treats_a_returned_string_as_one_problem(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_SRC_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    wb = new_workbook()
    write_table(wb.create_sheet("S"), [["a", "b"], [1, 2]])
    out = tmp_path / "c.xlsx"
    save(wb, out, new=True)
    (tmp_path / "checks.py").write_text(
        textwrap.dedent(
            """
            from xlspec import check

            @check("one string")
            def _(book):
                return "B7: expected 2026/12, got 2027/1"

            @check("empty string passes")
            def _(book):
                return ""
            """
        ),
        encoding="utf-8",
    )
    result = run_checks(out, "checks.py")
    assert result["failed"] == [{"check": "one string", "problems": ["B7: expected 2026/12, got 2027/1"]}]
    assert result["passed"] == 1


def test_book_reads_formula_values_with_data_only(tmp_path):
    import openpyxl
    from xlspec import Book

    path = tmp_path / "f.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["a", "b"])
    ws.append([1, "=A2*2"])
    wb.save(path)
    assert Book(path).table("S")[0]["b"] == "=A2*2"
    # openpyxl で作っただけのファイルには計算済みの値が無い（Excel で保存すれば入る）
    assert Book(path, data_only=True).table("S")[0]["b"] is None
