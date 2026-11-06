---
name: excel-edit
description: A skill that creates new xlsx/xlsm files and edits existing ones by writing openpyxl code directly with execute_python_code, using the helper module xlkit for a consistent look (header colors, borders, banding, column widths, wrapping, merged group columns, font name, wrapped row heights) and for a check on save (backup, warnings, sheet summary). No Excel needed. Use it when the user wants to create or edit table data, schedules, reports or Excel files with charts. Use excel-read for read-only use, excel-recalc to calculate formulas, excel-render to check the look as images, and excel-vba-read/excel-vba-edit for VBA macros.
license: MIT
metadata:
  author: ytakamoto
  version: "0.3"
---

# excel-edit

Create and edit Excel files by writing openpyxl code with `execute_python_code`. Import the helper module `xlkit` from this skill for the table look and for saving.

## Start of every code

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.environ["AGENT_SRC_DIR"]), "skills", "excel-edit", "scripts"))
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
| `save(wb, path, new=True/False)` | Save and print one line of JSON: path, `@N` reference, per-sheet summary (size, merged cells, column widths, print setup) and `warnings` (text cut off, mixed number formats, unmerged runs in a group column, a long note squeezed into one narrow cell). Before saving it fills in what the code did not set: font names, the height of wrapped rows, and the print setup (see below). `new=True` is for a file this conversation creates: it refuses to overwrite a file that was already there, but running the same build script again simply rebuilds the file it saved before. `new=False` is for editing an existing file: it copies the old file to `<name>.bak_<date-time>` first. |

What `save` fills in (only where the code set nothing; your own `cell.font`, `ws.row_dimensions[row].height` and `ws.page_setup` are kept):

- Font: a workbook built with `new_workbook()` that has Japanese text gets `Yu Gothic` everywhere, also when it is saved over an existing file. In an existing workbook, new cells take the font name the sheet already uses most (an Excel file in Japanese keeps its own font); if the file has no named font at all, Japanese text gets `Yu Gothic`. So do not set font names yourself unless the task asks for a font.
- Row height: every row with wrapped text gets enough height to show it.
- Printing: a sheet with a `write_table` table prints one page wide (pages continue downward), in landscape when the table is too wide for portrait, and repeats the first table's header row on every page. Set `ws.page_setup.orientation` yourself only when the task asks for a layout.

Anything else (single cells, charts, conditional formatting, data validation, images) is plain openpyxl on the sheet.

## How to write the code

1. Write the whole build script once to a file with `Write` (e.g. `build_xlsx.py`; a relative name goes to the cwd of `execute_python_code`), then run it with `execute_python_code`: `import runpy; runpy.run_path("build_xlsx.py")`. When something fails or a check is wrong, fix only those lines with `Edit` and run the same one-line `execute_python_code` again (keep `save(wb, out, new=True)`; it rebuilds the file). Do not resend the whole script.
2. Compute everything from the data in the script: dates, week numbers, groups, averages, counts, which rows exist. Build `rows` (and `row_fills`) as lists in the script, then write each table with one `write_table`. Do not type computed values into the script by hand. When the source data is a file the script can read (CSV, JSON, text), read it in the script instead of writing the values into the script.
3. Save to the absolute path in the working directory with `save(...)`.
4. Check at the end of the same script: open the saved file again with `openpyxl.load_workbook` and print one check per requirement of the task (e.g. every month has rows for weeks 1–5, every row of a kind has its color, the notes the task asks for are present). Fix and run again until every check passes. Printing only row counts is not a check.
5. Fix every item in `warnings` before reporting.

## The shapes that come up most (keep the script to these)

One table with a group column and a note:

```python
t = write_table(ws, [["月", "予定", "種別"]] + rows, "A1", title="2026年度 年間予定表", row_fills=[colors[r[2]] for r in rows])
merge_same(ws, "A", t.first_row, t.last_row)
write_note(ws, t.last_row + 2, "前提: 参加者30人・予算40,000円", last_col=t.last_col)
save(wb, out, new=True)
```

Two views of the same records on two sheets (monthly and weekly): build the records once as a list, then write one table per sheet from that same list. Do not re-derive the values per sheet; the week number and the month are computed in the script.

```python
records = [(year, month, day, name, attendees), ...]   # read them from the source, do not type them
```

Per-row color by kind: pass `row_fills` (one entry per body row), not `banded=False` plus manual fills. `number_formats={col: "#,##0"}` for money and participant counts.

## Rules

- Write sheet names, headers and cell text in the user's language (the English in the examples is only for illustration).
- One table per `write_table` call; put several tables on one sheet by giving each a different `top_left`. For a group column (month, category), repeat the value on every row in `rows` and call `merge_same` afterwards.
- Values the reader will change and totals of cells in the sheet: write Excel formulas (`"=SUM(D3:D20)"`) instead of values computed in Python, so the file stays correct when edited. Check formula results with the excel-recalc skill. Values derived from source records outside the sheet (averages of past records, dates): write the computed values.
- Rounding to whole numbers (四捨五入): `Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)`, not `round()`.
- Every line of text outside a table (premise, note, legend, source) goes through `write_note(..., last_col=t.last_col)`, also on a second sheet. A note typed into one cell wraps inside that narrow column and becomes a tall block.
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
