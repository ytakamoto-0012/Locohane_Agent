---
name: excel-py
description: A skill that creates new xlsx/xlsm files and edits existing ones by writing openpyxl code directly with execute_python_code, using the helper module xlkit for a consistent look (header colors, borders, banding, column widths, wrapping, merged group columns) and for a check on save (backup, warnings, sheet summary). No Excel needed. Use it when the user wants to create or edit table data, schedules, reports or Excel files with charts. Use excel-read for read-only use, excel-recalc to calculate formulas, excel-render to check the look as images, and excel-vba-read/excel-vba-edit for VBA macros.
license: MIT
metadata:
  author: ytakamoto
  version: "0.1"
---

# excel-py

Create and edit Excel files by writing openpyxl code with `execute_python_code`. Import the helper module `xlkit` from this skill for the table look and for saving.

## Start of every code

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.environ["AGENT_SRC_DIR"]), "skills", "excel-py", "scripts"))
from xlkit import new_workbook, open_workbook, write_table, write_note, merge_same, save, fill
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
| `save(wb, path, new=True/False)` | Save and print one line of JSON: path, `@N` reference, per-sheet summary (size, merged cells, column widths) and `warnings` (text cut off, mixed number formats, unmerged runs in a group column). `new=True` refuses to overwrite an existing file. `new=False` copies the old file to `<name>.bak_<date-time>` first. |

Anything else (fonts, single cells, charts, conditional formatting, data validation, images) is plain openpyxl on the sheet.

## How to write the code

1. Write the whole build script once to a file with `Write` (e.g. `build_xlsx.py`; a relative name goes to the cwd of `execute_python_code`), then run it with `execute_python_code`: `import runpy; runpy.run_path("build_xlsx.py")`. When something fails or a check is wrong, fix only those lines with `Edit` and run the same one-line `execute_python_code` again. Do not resend the whole script.
2. Compute everything from the data in the script: dates, week numbers, groups, averages, counts, which rows exist. Build `rows` (and `row_fills`) as lists in the script, then write each table with one `write_table`. Do not type computed values into the script by hand.
3. Save to the absolute path in the working directory with `save(...)`.
4. Check at the end of the same script: open the saved file again with `openpyxl.load_workbook` and print one check per requirement of the task (e.g. every month has rows for weeks 1–5, every row of a kind has its color, the notes the task asks for are present). Fix and run again until every check passes. Printing only row counts is not a check.
5. Fix every item in `warnings` before reporting.

Example content of `build_xlsx.py`:

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.environ["AGENT_SRC_DIR"]), "skills", "excel-py", "scripts"))
from xlkit import new_workbook, write_table, write_note, merge_same, save
import openpyxl

out = r"C:\work\schedule.xlsx"
records = [("April", "Week 1", "Meeting", "Meeting"), ("April", "Week 2", "Entrance ceremony", "Event"), ("May", "Week 1", "Excursion", "Event")]
colors = {"Meeting": "E1D5E7", "Event": "DAEEF3"}

wb = new_workbook()
ws = wb.create_sheet("Weekly")
rows = [["Month", "Week", "Item", "Kind"]] + [list(r) for r in records]
t = write_table(ws, rows, "A1", title="2026 weekly schedule", row_fills=[colors[r[3]] for r in records])
merge_same(ws, "A", t.first_row, t.last_row)
write_note(ws, t.last_row + 2, "Note: meetings are planned two weeks before each event.", last_col=t.last_col)
save(wb, out, new=True)

# check
ws = openpyxl.load_workbook(out)["Weekly"]
kinds = [ws.cell(row=r, column=4).value for r in range(t.first_row, t.last_row + 1)]
bad = [r for r in range(t.first_row, t.last_row + 1) if ws.cell(row=r, column=3).fill.fgColor.rgb[-6:] != colors[ws.cell(row=r, column=4).value]]
print("rows:", len(kinds), "color mismatches:", bad)
```

## Rules

- Write sheet names, headers and cell text in the user's language (the English in the examples is only for illustration).
- One table per `write_table` call; put several tables on one sheet by giving each a different `top_left`. For a group column (month, category), repeat the value on every row in `rows` and call `merge_same` afterwards.
- Values the reader will change and totals of cells in the sheet: write Excel formulas (`"=SUM(D3:D20)"`) instead of values computed in Python, so the file stays correct when edited. Check formula results with the excel-recalc skill. Values derived from source records outside the sheet (averages of past records, dates): write the computed values.
- Rounding to whole numbers (四捨五入): `Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)`, not `round()`.
- Editing an existing file: `open_workbook(path)`, change only what the task asks, `save(wb, path, new=False)`. To restructure a table (add or remove rows inside it), clear the old table area and write it again with `write_table` rather than `ws.insert_rows`/`ws.delete_rows` (openpyxl does not move merged cells, formulas or charts when rows move).
- `.xlsm`: always open with `open_workbook` and save to `.xlsm`; never create one with `new_workbook()` (it would have no macros). A new macro workbook is created first with excel-vba-edit.
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
