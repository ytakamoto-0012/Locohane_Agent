---
name: excel-edit
description: A skill that creates new xlsx/xlsm files and edits existing ones by writing openpyxl code directly with execute_python_code, using the helper module xlkit for a consistent look (header colors, borders, banding, column widths, wrapping, merged group columns, font name, wrapped row heights) and for a check on save (backup, warnings, sheet summary). No Excel needed. Use it when the user wants to create or edit table data, schedules, reports or Excel files with charts. Use excel-read for read-only use, excel-recalc to calculate formulas, excel-render to check the look as images, and excel-vba-read/excel-vba-edit for VBA macros.
license: MIT
metadata:
  author: ytakamoto
  version: "0.4"
---

# excel-edit

Create and edit Excel files by writing openpyxl code with `execute_python_code`. Import the helper module `xlkit` from this skill for the table look and for saving, and `xlspec` for the records, rules and checks that the verifier reuses.

## Start of every code

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.environ["AGENT_SRC_DIR"]), "skills", "excel-edit", "scripts"))
from xlkit import new_workbook, open_workbook, write_table, write_note, merge_same, save, fill
from xlspec import records, check, run_checks
```

## xlkit

| Function | What it does |
|---|---|
| `new_workbook()` | A new workbook with no sheets. Create sheets with `wb.create_sheet("name")`. |
| `open_workbook(path)` | Open an existing file. `.xlsm` keeps its macros. |
| `write_table(ws, rows, "A1", title=None, header=True, theme=None, banded=True, row_fills=None, number_formats=None, freeze=True)` | Write a table: `rows[0]` is the header (bold, white on dark blue, centered), every cell gets a thin border, column widths fit the text (full-width characters count as 2, at most 60), long text wraps, the header row is frozen. `title` puts a merged bold line above the table. `row_fills` is one color (`"RRGGBB"`) or `None` per body row (header not counted); banding (light gray every other row) is only added to rows without a color. `number_formats={3: "#,##0"}` formats the 4th column. `theme` is one of `charcoal`, `navy`, `forest`, `coral`, `terracotta`, `ocean`, `teal`, `berry`. Returns a `Table` with `title_row`, `header_row`, `first_row`, `last_row`, `first_col`, `last_col`. |
| `merge_same(ws, "A", first_row, last_row)` | Merge the runs of the same value in one column (month, category) and center them vertically. Call it after writing the table instead of counting merge ranges yourself. |
| `write_note(ws, row, text, first_col=1, last_col=None)` | One line of text outside a table (premise, note, legend): merged across the table width (`last_col`, e.g. `t.last_col`), wrapped, and the row made tall enough to show it all. Use it for every such line instead of writing a single cell. |
| `fill("RRGGBB")` | A solid fill for `cell.fill`. |
| `save(wb, path, new=True/False)` | Save and print one line of JSON: path, `@N` reference, per-sheet summary (size, merged cells, column widths, print setup) and `warnings` (text cut off, mixed number formats, unmerged runs in a group column, a long note squeezed into one narrow cell). Before saving it fills in what the code did not set: font names, the height of wrapped rows, and the print setup (see below). `new=True` is for a file this conversation creates: it refuses to overwrite a file that was already there, but running the same build script again simply rebuilds the file it saved before. `new=False` is for editing an existing file: it copies the old file to `<name>.bak_<date-time>` first (not for a file this conversation saved; that one is just overwritten). |

What `save` fills in (only where the code set nothing; your own `cell.font`, `ws.row_dimensions[row].height` and `ws.page_setup` are kept):

- Font: a workbook built with `new_workbook()` that has Japanese text gets `Yu Gothic` everywhere, also when it is saved over an existing file. In an existing workbook, new cells take the font name the sheet already uses most (an Excel file in Japanese keeps its own font); if the file has no named font at all, new Japanese text gets `Yu Gothic`. Cells that were already in the file keep their font. So do not set font names yourself unless the task asks for a font.
- Row height: every row with wrapped text gets enough height to show it. In an existing workbook only the rows you wrote or whose alignment you changed are adjusted (Excel keeps sizing the other rows itself).
- Printing: a sheet with a `write_table` table prints one page wide (pages continue downward), in landscape when the table is too wide for portrait, and repeats the first table's header row on every page. Set `ws.page_setup.orientation` yourself only when the task asks for a layout.

Anything else (single cells, charts, conditional formatting, data validation, images) is plain openpyxl on the sheet.

## xlspec (records, rules, checks)

| Function | What it does |
|---|---|
| `records(topic=None)` | The records written in ```` ```records ```` blocks of the thread notes (one JSON object per line), in order, each with `_topic`. Read the topics the task names (`[r for t in topics for r in records(t)]`); without `topic` it reads every block, including copies in other topics. Use it instead of copying the facts into the script by hand. A line it cannot read is skipped and reported, not an error, so the whole block still works: several JSON objects on one line are split, and lines identical to an earlier line of the same topic are counted once (identical records in different topics are kept as separate records, so a copy in another topic is counted again: read only the topics the task names). When something was skipped or merged it prints one line of JSON (`records`, `skipped_lines`, `duplicate_lines_merged`, the first skipped lines). A key the writer did not write is absent, so read values with `r.get(key)`. |
| `@check("requirement")` | In `checks.py`: register one check. The function gets `book` and returns a list of problems (empty = OK) or `True`/`False`. `book.table(sheet)` gives the table rows as dicts keyed by the header (merged cells filled on every row, plus `_row` and `_fill`); `book.texts(sheet)` gives `(cell, text)` of every text cell; `book.wb` is the openpyxl workbook. |
| `run_checks(path, "checks.py")` | Run every check and print one line of JSON: `passed`, `failed` (the problems of each failed requirement) and `errors` (a check that crashed: a bug in the check, not in the file). |

## How to write the code

When the file is built from records and rules (schedules, lists derived from past records, aggregations), write three files with `Write`, in this order. The verifier reviews `rules.py` and `checks.py` and runs the same checks, so whatever they say is what gets verified. Write the three files with relative names so they stay in the session's own `_tmp_...` folder (the folder `execute_python_code` runs in and where `records()` reads the thread notes from); only the output file goes into the user's working folder.

1. **`rules.py`**: one function per rule of the task and the thread notes (which records are used, the month of an event, the week number of a date, the dates of preparation and meetings, the label of an estimated value). Put the rule sentence as the docstring and handle its edge cases in the code (ties, a single record, outliers, a week that starts in the previous month). When the rule text does not decide a case, choose a reading, write `ASSUMPTION: ...` in the docstring, and list every assumption in your final answer. No values typed from memory: dates and weekdays come from `datetime`.
2. **`checks.py`**: one `@check` per requirement of the task (every selected record has its rows, every month/week that must exist exists, each row has the color of its kind, the notes the task asks for are present, every count, date and list written in a note matches the data). Compute the expected values with `records()` and `rules.py` (`import rules`) and compare them with `book.table(...)`. A problem says the sheet, the row or cell, the expected and the actual value.
Counts of records written in the task text (e.g. "40 records", "7 without a name") are the delegator's estimate. `records()` is the source: when they differ, build from `records()` and report the difference in your final answer; do not stop for it. Lines identical to an earlier line of the same topic are merged by `records()` and reported as `duplicate_lines_merged`, so the count you use is the one `records()` returns.

The output file contains only the tables the task asked for. Items that need the user's confirmation (a handwritten value, an unread name, a value that disagrees with another source) belong in your final answer and the thread note, with a short mark in the row they come from; do not add a sheet or table that lists them all. A verification list is not a deliverable, and the verifier counts extra tables as content the task did not ask for.

3. **`build_xlsx.py`**: read the records with `records()` (or read the source file when the data is a file), compute every value with `rules.py`, write the tables, save, then `run_checks(out, "checks.py")`. Run it with `execute_python_code`: `import runpy; runpy.run_path("build_xlsx.py")`.
4. When `failed`, `errors` or the `warnings` of `save` are not empty, fix only those lines with `Edit` (in `build_xlsx.py`, `rules.py` or `checks.py`) and run the same one-line `execute_python_code` again (keep `save(wb, out, new=True)`; it rebuilds the file). Do not resend a whole script.
5. Report the file path, the paths of `rules.py` and `checks.py`, the result of `run_checks`, and the assumptions.

Text in notes, legends and remarks is built from the data in `build_xlsx.py` too: counts (`f"{len(unknown)}件"`), lists (`", ".join(...)`), dates and weekdays (`"月火水木金土日"[d.weekday()]`). A number or a date typed into a sentence by hand drifts from the table.

For a small edit of an existing file (change some cells, add a column), one script with a check at its end is enough: reopen the saved file with `openpyxl.load_workbook` and print one check per requirement.

## The shapes that come up most (keep the script to these)

One table with a group column and a note:

```python
t = write_table(ws, [["月", "予定", "種別"]] + rows, "A1", title="2026年度 年間予定表", row_fills=[colors[r[2]] for r in rows])
merge_same(ws, "A", t.first_row, t.last_row)
write_note(ws, t.last_row + 2, "前提: 参加者30人・予算40,000円", last_col=t.last_col)
save(wb, out, new=True)
```

Two views of the same records on two sheets (monthly and weekly): read the records once, compute each value once with `rules.py`, then write one table per sheet from that same list. Do not re-derive the values per sheet.

```python
import rules
recs = records()                                   # from the ```records blocks, not typed
events = [rules.plan_event(r) for r in rules.selected(recs)]
```

Per-row color by kind: pass `row_fills` (one entry per body row), not `banded=False` plus manual fills. `number_formats={col: "#,##0"}` for money and participant counts.

## Rules

- Write sheet names, headers and cell text in the user's language (the English in the examples is only for illustration).
- One table per `write_table` call; put several tables on one sheet by giving each a different `top_left`. For a group column (month, category), repeat the value on every row in `rows` and call `merge_same` afterwards.
- Values the reader will change and totals of cells in the sheet: write Excel formulas (`"=SUM(D3:D20)"`) instead of values computed in Python, so the file stays correct when edited. Check formula results with the excel-recalc skill. Values derived from source records outside the sheet (averages of past records, dates): write the computed values.
- Rounding to whole numbers (四捨五入): `Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)`, not `round()`.
- Every line of text outside a table (premise, note, legend, source) goes through `write_note(..., last_col=t.last_col)`, also on a second sheet. A note typed into one cell wraps inside that narrow column and becomes a tall block.
- Editing an existing file: `open_workbook(path)`, change only what the task asks, `save(wb, path, new=False)`. To restructure a table (add or remove rows inside it), clear the old table area and write it again with `write_table` rather than `ws.insert_rows`/`ws.delete_rows` (openpyxl does not move merged cells, formulas or charts when rows move).
- `.xlsm`: always open with `open_workbook` and save to `.xlsm` (saving it to `.xlsx` drops the macros, with a warning); never create one with `new_workbook()` (it would have no macros). A new macro workbook is created first with excel-vba-edit.
- Do not use `pandas.DataFrame.to_excel` (no formatting, overwrites the whole sheet).
- After saving, check the look with the excel-render skill when the task cares about the look.

## Charts (plain openpyxl)

```python
from openpyxl.chart import BarChart, Reference
chart = BarChart()
chart.title = "Participants"
data = Reference(ws, min_col=4, min_row=t.header_row, max_row=t.last_row)  # include the header for the series name
cats = Reference(ws, min_col=2, min_row=t.first_row, max_row=t.last_row)
chart.add_data(data, titles_from_data=True)
chart.set_categories(cats)
ws.add_chart(chart, "G2")
```
