---
name: excel-read
description: A read-only skill for xlsx/xlsm/xls files (getting the sheet list and cell data). For formula cells, you can choose to get either the formula string or the value Excel last calculated. For xlsx/xlsm it gets only cell values and number formats by default (lightweight), and with `--style` it also gets full style information (bold, background color, borders, merged cells, structured tables). No Excel needed (reads directly with openpyxl). Use it when the user wants to check, summarize or search the contents of an Excel file, read the values of table data, or verify that written content and formatting were applied correctly. Use excel-vba-read to read VBA macro code, excel-render to see borders, charts and layout as images, and excel-edit to create or edit values.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# excel-read

A skill that gets the sheet list and cell data of xlsx/xlsm/xls read-only.
Runs `read_excel.py`.

## Calling it

Always pass `file_path` as the first positional argument (the order of flags is free). Combine the arguments as follows depending on the purpose.

```bash
# Get only the sheet list (omit --sheet; run this first)
python read_excel.py "C:\Users\me\book.xlsx"

# Read the cell data of the specified sheet, 200 rows from the top (default offset/limit)
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1

# Continue reading from row 201 (split reading)
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --offset 200 --limit 200

# Read only the columns you need (reduce the amount read at once for tables with many columns)
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --columns "A,C,E"

# Narrow the columns and also split the rows (the basic combination for tables with many rows and columns)
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --columns "A,C,E" --offset 200 --limit 200

# Get formula cells as the value Excel last calculated (when you want the result, not the formula string)
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --data-only

# Also get full style information such as bold, background color and borders (the default lightweight mode gives only value and number format)
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --style

# Get the row ranges per column value as groups (do not count the raw rows by eye)
python read_excel.py "C:\Users\me\book.xlsx" --sheet MonthlySchedule --query-json '[{"op": "group_by", "column": "A"}]'
```

## Arguments

| Argument | Required/optional | Type | Default | Description |
|---|---|---|---|---|
| `file_path` (positional, no flag name) | Required | String (absolute path) | - | Path of the `.xlsx`/`.xlsm`/`.xls` file to read. Pass it as the first element of the `script_args` array |
| `--sheet` | Optional | String | Omitted = sheet list mode | Sheet name, or 0-based index (e.g. `"0"` = the first sheet). When given, it switches to cell data mode |
| `--offset` | Optional | Integer string | `"0"` | Number of rows to skip (0-based). When reading in parts, use the previous `end_row` as is for the next `offset` |
| `--limit` | Optional | Integer string | `"200"` | Maximum number of rows to read. Even for large tables, do not read everything at once; read in parts while watching `total_rows` |
| `--data-only` | Optional, flag without a value | - | Not given = return formula strings | Return formula cells as the cached values Excel last calculated instead of formula strings. Effective only for `.xlsx`/`.xlsm` (`.xls` always gives values only, so it does not matter) |
| `--style` | Optional, flag without a value | - | Not given = lightweight mode | `.xlsx`/`.xlsm` only. When given, also returns full style information such as bold, background color and borders, plus `column_widths`/`row_heights`/`warnings`. By default (not given) cells return only `value` and (if set) `style.number_format`, and `column_widths`/`row_heights`/`warnings` are not even calculated (`merged_cells`/`tables` are returned even by default). Give it only when you want to verify formatting details (bold, background color, etc.). An error with `.xls` (it never gets styles) |
| `--query-json` | Optional | String (a JSON array on one line) | None | Structured queries (details in the "Structured queries" section below). Only `.xlsx`/`.xlsm` are supported and `--sheet` is required (`.xls` or omitting `--sheet` is an error) |
| `--columns` | Optional | String (comma-separated) | Omitted = all columns | The columns to read, as column letters (`"A"`) or 1-based column numbers (`"3"` = column C), comma-separated (e.g. `"A,C,E"`). `--sheet` required. Supported for all of `.xlsx`/`.xlsm`/`.xls`. An error if a specified column exceeds the total number of columns |

If `--sheet` is omitted, only the sheet list is returned (the default behavior to avoid reading everything of a large file by mistake). All other arguments can be omitted.

## Input/output types

Success = exit code 0 + one line of JSON on stdout. Failure = non-zero exit code + an error message on stderr. Pass the error text to the user as is. A missing library gives `ImportError` with exit code 1 (cause: `openpyxl`/`xlrd`) -> prompt the user to run `pip install <package name>`.

This script does not print the body (sheet list/rows) to stdout; it writes it to a temporary JSON file and returns `result_path`. Read `result_path` (it can be read in parts).

## Procedure

1. First run without `--sheet` and check the sheet names and the numbers of rows and columns.
2. Specify a sheet name (or a 0-based index in the sheet list) in `--sheet` to get the cell data.
3. When there are many rows or columns, use the arguments appropriately:
   - `--offset`/`--limit`: use them to read cell values in order from the top. Each call returns `--limit` rows in `rows`. Look at `total_rows` to decide whether you need more, and pass the previous `end_row` as the next `--offset` to read on.
   - `--columns`: use it when you know which columns you want to see (you want to check the values of specific columns only, you do not want to read all columns of a wide table at once, etc.). For tables where `total_columns` exceeds about 20, first understand the content of the columns from the list without `--sheet` or by reading just the one header row, then read only the columns you need; that is safer than reading all columns at once with a large `--limit` (reading a lot at once leads to overlooking content).
   - `--query-json`: use it when you want to know the row ranges per column value (checking the ranges of grouping columns, verifying after `insert_rows`/`merge_cells`, etc.) (the "Structured queries" section below). Do not count the raw `rows` by eye to calculate row ranges by hand. It is not affected by the values of `--offset`/`--limit`/`--columns` and always covers the whole sheet (row 1 to `total_rows`). If you do not need the `rows` body, `--limit` can stay at the default `200` (`query_results` returns everything regardless of `--limit`).
4. Formula cells return formula strings (`"=SUM(A1:A10)"`) by default. If you want the value Excel last calculated, use `--data-only` (xlsx only; it affects the value display of both `rows` and `query_results`). If you want the latest values right after writing formulas, first run `recalc_excel.py` of the excel-recalc skill and then read again.
5. For `.xlsx`/`.xlsm` with `--sheet`, only `value` and the number format are returned by default. When you want to verify that formatting written with `edit_excel.py` of the excel-edit skill (bold, background color, merges, tables) was applied correctly, **or when you want to check whether the table data has anything wrong**, add `--style` (it reads the whole file with `read_only=False`, so it is slower than `.xls` or the default mode). By default (without `--style`), structural anomalies such as circular references in formulas and text exceeding column widths are not detected at all. These are put in `warnings` automatically only with `--style`, so when asked "check whether anything is wrong", always read with `--style`. `query_results` of `--query-json` does not include style information (only the `rows` side does).

## Output

The top-level keys of `result_path` (a JSON file) include `path` (the file read) and `mode` (`"sheets"` or `"rows"`) in both modes. The rest depends on whether `--sheet` is given, the extension (`.xlsx`/`.xlsm` or `.xls`) and `--query-json`.

**Sheet list mode** (`--sheet` omitted): only `sheets` = a list of elements `{"name","max_row","max_column","print_area"}`. `print_area` is the print area set on each sheet (e.g. `"'Sheet1'!$A$1:$B$20"`; for sheets with several ranges, a comma-joined string), `null` if not set. Values are filled only for `.xlsx`/`.xlsm`; for `.xls` the `print_area` key itself does not exist (xlrd cannot get print areas, so it is not supported).

**Cell data mode** (`--sheet` given) common keys: `sheet` (the resolved sheet name), `total_rows`/`total_columns` (the numbers of rows and columns of the whole sheet; they do not change even when narrowed with `--columns`), `start_row`/`end_row` (the range of 1-based row numbers included in `rows` this time; both `null` if no rows are returned; this `end_row` can be passed as is as the next `--offset`), `columns` (the array of column letters included in `rows` this time, e.g. `["A","C","E"]`; all columns if `--columns` is omitted), `rows` (a list of one array per row, only within the range of `--offset`/`--limit`/`--columns`). **The `j`-th element of each row of `rows` is always the value of column `columns[j]`** (even when columns are thinned out with `--columns`, this correspondence tells you the original column. Do not count columns by position within the row alone).

- For `.xls`: cell values in `rows` are ISO8601 strings for dates/times and `null` for empty cells (style information is not supported).
- For `.xlsx`/`.xlsm`: each cell of `rows` is `{"value":..., "style":{...}}` (the `style` key only when there is a value). `value` is an ISO8601 string for dates/times, `null` for empty cells, and for formula cells the formula string by default (e.g. `"=SUM(A1:A10)"`); with `--data-only` it becomes the cached value Excel last calculated (e.g. `15`). In addition, `merged_cells` (the whole sheet, all of them regardless of the offset/limit range) and `tables` (the list of structured tables) are added at the top level.
  - **Default (without `--style`, lightweight mode)**: each cell of `rows` returns only `value` and (only when `number_format` is set) `style.number_format` (`bold`/`fill_color` etc. are omitted). The top-level `merged_cells`/`tables` are returned, but the keys `column_widths`/`row_heights`/`warnings` are omitted entirely (the calculation is skipped).
  - **With `--style`**: the full set of `style` keys is returned:
```json
{"bold": true, "italic": false, "font_color": "0000FF", "font_size": 11,
 "fill_color": "FFFF00", "number_format": "#,##0.00",
 "align": "center", "valign": "center", "wrap_text": false,
 "border": "thin"}
```
    Items equal to the defaults are omitted, and cells without formatting omit the `style` key itself. The following additional fields are also added:
    - `column_widths`: the widths of the columns included in `columns` (= the returned columns; only those columns when `--columns` is given) (`{"A": 12.3, "B": null, ...}`). Columns not set are `null`.
    - `row_heights`: the height of each row in the returned range (`start_row`-`end_row`) (`{"3": 15.0, ...}`). Rows not set are omitted.
    - `warnings`: an array of warnings about structural problems (the key is omitted if there are none). Points out possible cut-off text in cells without `wrap_text` whose estimated display width exceeds the column width. It also points out possible circular reference errors in Excel when the reference range of a formula cell (can be judged only without `--data-only`) includes its own cell (e.g. `=SUM(B8:N8)` in cell `N8`) (sheet-qualified references such as `Sheet2!A1:B2` are not checked).
- With `--query-json`: in addition to the `rows` set above, `query_results` is added at the top level. **`query_results` covers the whole sheet regardless of the `--offset`/`--limit` range** (details and examples in the next section, "Structured queries"). `query_results` does not include style information (with or without `--style`).

## Structured queries (`--query-json`, a mandatory rule)

**When verifying after `insert_rows`/`merge_cells`, or checking the ranges of grouped columns (month, category, etc., columns that become merged cells), do not count the raw `rows` by eye to calculate row ranges by hand.** Instead, passing queries with `--query-json` returns, in one call, the result grouped into ranges of consecutive rows per column value.

```bash
python read_excel.py "C:\Users\me\book.xlsx" --sheet MonthlySchedule --query-json '[{"op": "group_by", "column": "A"}]'
```
Output (inside `result_path`, the `query_results` key):
```json
"query_results": [
  {"op": "group_by", "column": "A",
   "items": [{"value": "April", "start_row": 2, "end_row": 2, "row_count": 1},
             {"value": "May", "start_row": 3, "end_row": 4, "row_count": 2}]}
]
```
- Required keys of `group_by`: `"op": "group_by"` and `"column"` (a column letter `"A"` or a 1-based column number, e.g. `"3"` = column C). A missing `column` is an error with exit code 1.
- Behavior: scans the specified column **from row 1** down to `total_rows`, and groups the ranges of consecutive rows per non-null value (including non-anchor rows of merged cells and continuing blank rows) into `{value,start_row,end_row,row_count}`.
- **Header rows are not excluded either.** If the header cell (e.g. "Month" in `A1`) has a value, it is itself mixed in at the start of `items` as an independent item with `start_row:1,end_row:1`. In the output example above, `start_row` starts at `2` only because the header cell happened to be empty; in general you need to tell whether the first item is the header by its `start_row` and exclude it.
- `queries` is an array, so several queries can be put in one call, like `[{"op":"group_by","column":"A"}, {"op":"group_by","column":"C"}]` (no need to repeat separate calls to check ranges of different columns).
- `--sheet` is required (omitting it or a `.xls` extension is an error). Unsupported op names are also errors with exit code 1 (the message includes the list of supported ops).
- It can also be used as is to check the `anchor` (before/after which value to insert) when using `insert_row_group` of the excel-edit skill.

### `list_images` (list of images)

```bash
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --query-json '[{"op": "list_images"}]'
```
Output (inside `result_path`, the `query_results` key):
```json
"query_results": [
  {"op": "list_images",
   "items": [{"image_index": 0, "anchor_type": "absolute",
              "left_cm": 1.0, "top_cm": 1.0, "width_cm": 5.0, "height_cm": 3.0}]}
]
```
`image_index` is the 0-based sequence number of the image within the sheet (in the order added) and exactly matches
the `image_index` of `set_image_position` of excel-edit. When `anchor_type` is other than `"absolute"`
(images originally in the template, placed so as to follow cells),
`left_cm` etc. are `null` (once moved with `set_image_position`, they are replaced with absolute coordinates,
and the coordinates can be obtained from then on).

### `list_charts` (list of charts)

You can check the existence, title, type and position of charts in a list without rendering images.

```bash
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --query-json '[{"op": "list_charts"}]'
```
Output (inside `result_path`, the `query_results` key):
```json
"query_results": [
  {"op": "list_charts",
   "items": [{"chart_index": 0, "type": "line", "title": "Monthly income and expenses", "anchor": "F1"}]}
]
```
`chart_index` is the 0-based sequence number of the chart within the sheet and exactly matches the `chart_index` of
`update_chart`/`delete_chart` of excel-edit. `type` is one of
`line`/`bar`/`pie`/`scatter` (unsupported types return openpyxl's class name
as is). `title` is `null` if not set. If you just want to check whether a chart exists and whether its title
is as intended, this is faster than rendering images with excel-render.

### `print_area` (print area only)

When you want to get only the print area of one specified sheet, lightly (the sheet list mode without `--sheet` also returns `print_area`, but use this to avoid the heavy response with `max_row`/`max_column` for all sheets or `rows`).

```bash
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --query-json '[{"op": "print_area"}]'
```
Output (inside `result_path`, the `query_results` key):
```json
"query_results": [
  {"op": "print_area", "value": "'Sheet1'!$A$1:$H$45"}
]
```
`value` is `null` if not set. For sheets with several ranges it is a comma-joined string (the same format as `print_area` in the sheet list mode). `.xls` does not support `--query-json` at all.

## Edge cases

A missing file / an extension other than xlsx/xlsm/xls / sheet not found / a broken file are errors with exit code 1. `.xls` does not support style information, `--query-json` or `--style` (`--query-json`/`--style` are errors only when given; style information and print areas are limited to `.xlsx`/`.xlsm` and are simply not added). Unlike `--query-json`/`--style`, `--columns` can also be used with `.xls` (there is no `.xls`-specific error). If a specified column exceeds the total number of columns, it is an error with exit code 1 whose message includes the total number of columns (e.g. "Specified column out of range: 'Z' (number of columns in this sheet: 10)").

## About checking the look

Check the look that cell values and style information alone cannot show, such as borders, formatting, charts and layout, as images with the excel-render skill (`render_excel.py` + `analyze_image`), not with this skill.
