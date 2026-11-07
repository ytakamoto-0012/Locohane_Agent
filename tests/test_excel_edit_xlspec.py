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


def test_records_reports_a_bad_line_and_a_missing_block(tmp_path):
    bad = _notes(tmp_path, _NOTES.replace('"participants": 27', "participants: 27"))
    with pytest.raises(ValueError, match='topic "2021 activities", line 1'):
        records(notes_path=bad)
    with pytest.raises(ValueError, match="No ```records block"):
        records("Rules", notes_path=_notes(tmp_path))


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
