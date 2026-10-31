---
name: excel-edit
description: A skill that creates new xlsx/xlsm files and edits existing ones. Supports setting cell values and formatting (font/background color/borders/column width/row height/merges), inserting and deleting rows and columns, adding and adjusting charts (data labels shown by default, theme colors supported), adding and positioning images, conditional formatting, data validation and structured tables. No Excel needed (writes directly with openpyxl). It can write formula strings but does not calculate (use the excel-recalc skill to check calculation results and detect error cells). Use it when the user wants to create or edit table data, output formatted reports or Excel files with charts and images, or add data or decoration to an existing xlsx/xlsm. Use excel-read for read-only use, excel-vba-read/excel-vba-edit for reading/writing VBA macros, and excel-render for checking the look as images.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# excel-edit

A skill that creates new xlsx/xlsm files and edits existing ones. Runs `edit_excel.py`.

## Calling it

```bash
python edit_excel.py "C:\Users\me\book.xlsx" --ops-json '[{"op": "set_cell", "sheet": "Sheet1", "cell": "A1", "value": "Total"}]'
```
- Creating new: add `--new` at the end (`--overwrite` is also required if the file exists; otherwise it is an error).
- Without `--new`: reads the target path and edits it (an error if it does not exist). Existing cells, formatting and sheets you did not touch are kept as they are.
- Omitting `--output` overwrites the target path. Add `--output <absolute path>` only when saving to another file.
- `.xlsm` can be edited while keeping its macros (only without `--new`, because the existing file is read with `keep_vba=True`). Reading/writing the macros themselves is done by the excel-vba-read/excel-vba-edit skills.
- **`--new` with `.xlsm` does not produce a "macro-enabled workbook"** (openpyxl's `Workbook()` has no vbaProject, so even with the `.xlsm` extension the content is xlsx-equivalent, producing a file that Excel cannot open / to which macros cannot be added). To create a new `.xlsm` with both table data/formatting and VBA macros, **first create an empty macro-enabled workbook with `--new` of the excel-vba-edit skill, then use this skill without `--new` to add sheets and data to the same path** (not the other way around).
- **Do not edit existing `.xlsm` files directly with raw openpyxl code** (forgetting `keep_vba=True` erases the VBA macros). Use `set_tab_color` for tab colors, `set_active_sheet` for the displayed sheet, and `update_chart` for chart adjustments. If there is no matching op, consult the user.

## Input/output types and how to pass ops

Success = exit code 0 + one line of JSON on stdout. Failure = non-zero exit code + an error message on stderr. Pass the error text to the user as is. A missing library gives `ImportError` with exit code 1 (cause: `openpyxl`) -> prompt the user to run `pip install <package name>`.

Ops are normally passed with `--ops-json '<the ops array as one-line JSON>'`. In either of the following cases, write the ops as a JSON array to a file from the start (without waiting for a syntax error) and pass it with `--ops-file <absolute path>`: (1) more than 5 op elements, (2) a single string value containing more than 10 non-ASCII (e.g. Japanese) characters or long text (embedding long JSON strings directly in command line arguments makes quoting/escaping mistakes likely). If `--ops-json` gives a syntax error (an unclosed quote, etc.) even once, do not try to fix it; switch to the `--ops-file` method immediately.

```bash
python -c "import json; json.dump([{'op': 'set_cell', 'sheet': 'Sheet1', 'cell': 'A1', 'value': 'Total'}], open('ops.json', 'w', encoding='utf-8'), ensure_ascii=False)"
python edit_excel.py "C:\Users\me\book.xlsx" --ops-file "C:\Users\me\ops.json"
```

## List of ops

| op | Required | Optional | Description |
|---|---|---|---|
| `add_sheet` | `name` | `index` | Add a sheet. An error if a sheet with the same name exists (an explicit error rather than relying on automatic renaming; to add to an existing sheet, specify that sheet name in `set_range` etc.). `index` is the **0-based** insertion position passed as is to openpyxl's `Workbook.create_sheet` (`index:0` for the front; appended at the end if omitted). The basis differs from `index` of `insert_rows` (1-based), so do not confuse them |
| `delete_sheet` | `name` | - | Delete a sheet |
| `rename_sheet` | `name`,`new_name` | - | Rename a sheet |
| `set_tab_color` | `sheet`,`color` | - | Change the color of the sheet tab (`color` is an RRGGBB hex string) |
| `set_active_sheet` | `sheet` | - | Specify the sheet shown first when the workbook is opened |
| `set_cell` | `sheet`,`cell` | `value`,`style`,`inherit_style`,`inherit_style_from` | Set the value and formatting of a single cell. If `style` is omitted and the target cell is a new cell that originally had no formatting (e.g. a row regenerated after `delete_rows`), no formatting is applied. To match the look of the surroundings, specify `inherit_style:true` to copy the formatting of an adjacent cell (the left one by default; `inherit_style_from` can choose `"left"`/`"right"`/`"above"`/`"below"`) |
| `set_range` | `sheet`,`start_cell`,`rows` | `style`,`header_style`,`row_styles`,`format_table` | Write several rows at once from the start cell. `header_style` applies only to row 1. Column widths are adjusted automatically (full-width characters count as 2; never narrower than the existing width). **Passing `header_style` automatically applies the equivalent of `format_table` (header colors, borders, banding, freezing the header row, column width adjustment) by default** (can be disabled with `format_table:false`; conversely `format_table:true` forces it even without `header_style`) |
| `set_style` | `sheet`,`range` | `style` | Apply only formatting to existing cells without changing values |
| `format_table` | `sheet`,`range` | `theme` (see below), `header_fill` (default `1F4E78` or `theme`'s primary), `header_font_color` (default `FFFFFF` or `theme`'s text_on_primary), `band_fill` (default `F2F2F2`), `banded` (default true), `border` (default `thin`), `freeze_header` (default true), `autofit` (default true) | Finish a table that has already been written. Row 1 of `range` is treated as the header. The font color and bold of body rows are not changed (so as not to break the `role` color coding) |
| `insert_rows` | `sheet`,`index` | `count` (default 1) | Insert rows (1-based position. You need to calculate the absolute row number yourself. For inserting into tables grouped by month, category, etc., use `insert_row_group`) |
| `insert_row_group` | `sheet`,`rows` | `anchor` (`{"column","equals"}`), `position` (`"before"`/`"after"`, default `"before"`), `start_column`, `style`, `header_style`, `row_styles`, `format_table` | **Insert a new group of rows before/after an existing group in a table with a grouping column (a column such as month or category where the same value continues and is turned into merged cells).** The row range where the value of the column specified by `anchor` first matches is resolved from the sheet state at the time this op runs (= after applying the preceding ops in the same ops batch), and inserting and writing are done in one op, so no absolute row numbers need to be calculated at all. If `anchor` is omitted, it appends at the end of the sheet. `position:"before"` inserts at the start of the target group, `"after"` right after its end. The content to write (`rows`/`style`, etc.) has the same schema as `set_range` |
| `delete_rows` | `sheet`,`index` | `count` (default 1) | Delete rows |
| `insert_cols` | `sheet`,`index` | `count` (default 1) | Insert columns (1-based) |
| `delete_cols` | `sheet`,`index` | `count` (default 1) | Delete columns |
| `set_column_width` | `sheet`,`column`,`width` | - | Set a column width manually. The unit is Excel's character width (default about 8.43). Clamped to 1-60. `column` can be a column letter (`"A"`, etc.) or a 1-based column number (`1`, etc.) |
| `set_row_height` | `sheet`,`row`,`height` | - | Set a row height manually (`row` is a 1-based row number, `height` in points. Clamped to 1-409) |
| `merge_cells` | `sheet`,`range` | - | Merge cells (e.g. `"A1:C1"`) |
| `unmerge_cells` | `sheet`,`range` | - | Unmerge. Specifying an unmerged range is an error |
| `add_table` | `sheet`,`name`,`range` | `style` (default `TableStyleMedium9`), `banded` (default true) | Create a structured table (with filters and structured references; different from the visual decoration of `format_table`). The existing values of row 1 of `range` become the header names. `name` is an identifier unique within the workbook |
| `update_table` | `sheet`,`name` | `range`,`style`,`banded` | Extend the range or change the style of an existing table |
| `remove_table` | `sheet`,`name` | - | Delete only the table definition (values and formatting remain) |
| `freeze_panes` | `sheet`,`cell` | - | Freeze panes (`"A2"` freezes row 1) |
| `add_chart` | `sheet`,`type`,`data_range`,`anchor` | `title`, `categories_range`, `titles_from_data` (default true), `theme` (see below), `show_data_labels` (default true), `grouping` (`type:bar` only; see below) | Add a chart. `type` is `bar`/`line`/`pie`/`scatter`. Data labels (values; % for `pie`) are shown by default, and series are colored when `theme` is given (not `pie`, which keeps Excel's default colors) |
| `update_chart` | `sheet`,`chart_index` | `anchor`,`width_cm`,`height_cm`,`data_range`,`categories_range`,`titles_from_data`,`title`,`theme`,`grouping` (at least one required) | Change the position, size, data range, title, colors and stacking of an existing chart (see "Adding and adjusting images and charts" below) |
| `delete_chart` | `sheet` + (`chart_index` or `title`) | - | Delete a chart. With `title`, 0 or several matches are an error (counted by the same `chart_index` basis as `update_chart`) |
| `add_image` | `sheet`,`image_path`,`left_cm`,`top_cm` | `width_cm`/`height_cm` (original size if omitted; specifying only one keeps the aspect ratio) | Add a new image to the sheet (see below) |
| `set_image_position` | `sheet`,`image_index` | `left_cm`/`top_cm`/`width_cm`/`height_cm` (at least one required) | Change the position and size of an existing image (see below) |
| `add_conditional_format` | `sheet`,`range`,`rule_type` | `params` (effectively required for some `rule_type`s; if omitted it is treated as empty, and an error occurs if needed arguments are missing) | Conditional formatting (see the table below) |
| `add_data_validation` | `sheet`,`range`,`type` | `formula1`,`formula2`,`prompt`,`prompt_title`,`error_message`,`error_title`,`allow_blank` (default true) | Data validation (see below) |

Supplement: `sheet` is a sheet name or a 0-based index. `rows`/`value` are strings, numbers, booleans or `null`. Strings starting with `=` are treated as formulas (this script does not evaluate them; check calculated values with `recalc_excel.py` of the excel-recalc skill).

## Common style schema (shared by `set_cell`/`set_range`/`set_style`/`header_style`)

```json
{"bold": true, "italic": false, "font_color": "0000FF", "font_size": 11,
 "fill_color": "FFFF00", "number_format": "#,##0.00",
 "align": "center", "valign": "center", "wrap_text": false,
 "border": "thin", "role": "input"}
```
All keys are optional. `font_color`/`fill_color` are RRGGBB hex. `align` is `left`/`center`/`right`, `valign` is `top`/`center`/`bottom`. `border` is a string (`thin`/`medium`/`thick`, all 4 sides at once) or `{"top":"thin","bottom":"thin"}` (per side; keys `top`/`bottom`/`left`/`right`).

Cell color coding by `role` (following Anthropic's official skill; recommended) — specifying `role` instead of writing `font_color` gives the conventional color automatically:

| role | Color | Meaning |
|---|---|---|
| `input` | Blue `0000FF` | Hard-coded input values |
| `formula` | Black `000000` | Formulas complete within the same sheet |
| `link` | Green `008000` | Formulas referring to other sheets |

Using `role` with this convention when making tables makes it obvious at a glance "what is manual input and what is calculated".

## Theme colors (theme)

Choosing one of the following for the `theme` key of `header_style` (via `set_range`) or `format_table`
sets the header colors all at once (the same colors and names as the `pptx-create` skill,
so it is easy to align the look when making xlsx and pptx as one set of materials). Individually specified `header_fill` etc.
take precedence.

`theme` of `add_chart`/`update_chart` accepts the same theme names as a switch for "whether to color the chart",
but **the actual series colors do not use the theme's `primary`/`secondary`/`accent`;
they use a fixed palette of 8 colors that takes color vision diversity (CVD) into account** (cycling 3 colors would repeat colors from the 4th series on).
So changing the theme name from `navy` to `forest` does not change
the series colors of charts (only here does the behavior differ from other theme-dependent elements such as header colors).

| theme | Main tone |
|---|---|
| `charcoal` (equivalent to the default) | Charcoal gray, a calm neutral tone |
| `navy` | Dark navy, a trustworthy business tone |
| `forest` | Deep green, evoking growth and the environment |
| `coral` | Navy + coral, bright and strong |
| `terracotta` | Terracotta, warm earth tones |
| `ocean` | A deep blue gradient tone |
| `teal` | Teal, a clean tone |
| `berry` | Berry color, calm and elegant |

```json
[{"op": "set_range", "sheet": "Sheet1", "start_cell": "A1",
  "rows": [["Month","Sales"], ["April",120], ["May",135]], "header_style": {"theme": "navy"}},
 {"op": "add_chart", "sheet": "Sheet1", "type": "bar", "data_range": "B1:B3",
  "categories_range": "A2:A3", "anchor": "D1", "title": "Monthly results", "theme": "navy"}]
```

## The basic recipe for good-looking tables (a mandatory rule)

**For tabular data (reports, lists, schedules, etc.), just add `header_style` to `set_range`** (even an empty `{}` works as the signal "row 1 is the header"). Header colors, borders, banding, freezing the header row and column width adjustment are applied automatically. Avoid building `fill_color`/`font_color`/`border` cell by cell yourself (more calls and an inconsistent look). Override with the options of `format_table` (`header_fill`, etc.) only when colors are specified explicitly.

```json
[{"op": "set_range", "sheet": "Sheet1", "start_cell": "A1",
  "rows": [["Month","Event","Owner"], ["April","Entrance ceremony","Tanaka"], ["May","Excursion","Sato"]],
  "header_style": {}}]
```

Add `"format_table": false` only when automatic finishing is not wanted (raw data without a header, etc.).

**Put the header row and data rows in the `rows` of the same `set_range` call** (do not split the header and data into separate calls. There is a guard against wrongly decorating the first row as a header when split, but it only increases the number of calls with no benefit). When making several tables, calling a separate `set_range` for each table does not interfere.

## When you want different background colors per row (a mandatory rule)

**Avoid counting row numbers by eye after writing and repainting with `set_style`** (miscounting whether to include the header row easily shifts it by one row). Pass `row_styles` with **the same number of elements** as `rows` of `set_range` (each element is the style dict of one row; `null` for no color). The corresponding styles are lined up in the same array at the time you build `rows`, so they cannot shift.

```json
{"op": "set_range", "sheet": "MonthlySchedule", "start_cell": "A2",
 "rows": [["Month","Event"], ["January","Officer selection"], ["January","New Year party"], ["February","Doll festival"]],
 "header_style": {"bold": true, "fill_color": "D9E2F3"},
 "row_styles": [null, {"fill_color": "DAEEF3"}, {"fill_color": "DAEEF3"}, {"fill_color": "F8CBAD"}]}
```
The first element of `row_styles` corresponds to the header row (when both `header_style` and it are passed, `row_styles` takes precedence). A mismatch in the number of elements is an error.

## Merge cells in columns where the same value continues (grouping columns such as month, category, period)

For a column where the same value continues for 2 or more rows (e.g. a "Month" column), do not leave the value repeated; merge the cells.
1. Write the table with `set_range` (at this point you may write the duplicate values as they are).
2. Merge only the consecutive ranges with `merge_cells` (merging automatically clears values other than the top-left, so there is no need to blank them first).
3. Applying `{"valign":"center"}` to the merged cells with `set_style` centers them vertically.

```json
[{"op": "set_range", "sheet": "WeeklySchedule", "start_cell": "A2",
  "rows": [["January","Week 1"], ["January","Week 2"], ["February","Week 1"]], "header_style": {}},
 {"op": "merge_cells", "sheet": "WeeklySchedule", "range": "A2:A3"},
 {"op": "set_style", "sheet": "WeeklySchedule", "range": "A2:A3", "style": {"valign": "center"}}]
```
One `merge_cells` can merge only a contiguous rectangle. If there are several value groups ("January" and "February" above), call `merge_cells` + `set_style` once per group.

**If you just want to finish a table already written** (after appending with `insert_rows`, etc.), use the standalone `format_table` op: `[{"op": "format_table", "sheet": "Sheet1", "range": "A1:C3"}]`. Even if you applied colors to input cells with the `role` convention, `format_table` does not change the font color of body rows, so nothing breaks (the call order may be values -> `format_table` or the reverse).

## When you want to insert a new group of rows before/after an existing group in a table with a grouping column (a mandatory rule)

**Avoid the combination of calculating an absolute row number with `insert_rows` and then writing with `set_range`** (you have to count yourself which row the target, such as "July", starts at, and when inserting several groups also calculate by hand how earlier inserts shift later row numbers; getting the shift wrong by one row causes accidents that overwrite another month's data). Instead use `insert_row_group` and specify with `anchor` "before/after which value to insert" (the op resolves the insertion position automatically, so you do not need to write row numbers).

```json
[{"op": "insert_row_group", "sheet": "MonthlySchedule",
  "anchor": {"column": "A", "equals": "July"}, "position": "before",
  "rows": [["June","Community festival booth","06/14"], ["June","Last event before summer vacation","06/28"]],
  "row_styles": [null, null]}]
```
Even when `insert_row_group` runs several times in a row in the same ops batch, each op always resolves its anchor from the latest sheet state with the preceding ops applied, so overwrite accidents due to shifted rows do not happen (to insert several groups before July, such as "add two events in June", just list `insert_row_group` in this form as many times as needed).

To check the current group structure (checking the value to use for `anchor` of `insert_row_group`, verifying the result of an insertion, etc.), use `--query-json '[{"op": "group_by", "column": "A"}]'` of the `excel-read` skill (do not count the raw `rows` by eye to calculate row ranges by hand).

**`anchor.equals` is matched strictly, including the type.** If the anchor column has numeric cells (e.g. `2024` or `7`), the `value` returned by `group_by` keeps the cell's original type (int/float), so turning it into a string like `"equals": "7"` does not match and gives the error "No value matches the anchor of insert_row_group". Copy the `value` from the `group_by` output as is (for numbers, keep them numeric like `"equals": 7`).

## Adding and adjusting images and charts (`add_image`/`set_image_position`/`update_chart`)

```json
[{"op": "add_image", "sheet": "Sheet1", "image_path": "C:\\img\\logo.png",
  "left_cm": 1.0, "top_cm": 1.0, "width_cm": 5.0, "height_cm": 3.0},
 {"op": "set_image_position", "sheet": "Sheet1", "image_index": 0, "left_cm": 12.0, "width_cm": 6.0},
 {"op": "update_chart", "sheet": "Sheet1", "chart_index": 0,
  "anchor": "F1", "width_cm": 18, "height_cm": 9, "title": "Updated title", "theme": "navy"}]
```

- `add_image`: `left_cm`/`top_cm` are required (cm coordinates from the top-left origin of the sheet).
  Omitting both `width_cm`/`height_cm` gives the image's original size; specifying only one calculates the other automatically
  keeping the aspect ratio.
- `set_image_position`: `image_index` is the 0-based sequence number of the image within the sheet
  (in the order added; `--query-json '[{"op": "list_images"}]'` of `excel-read` shows the current
  `image_index`, position and size). Only those of `left_cm`/`top_cm`/`width_cm`/
  `height_cm` that are specified are changed; the omitted ones keep their original values.
  **When moving an image originally in the template** (placed so as to follow cells) for the first time,
  it is replaced internally with absolute coordinates, and the axis for which `left_cm`/`top_cm` was not specified
  starts from 0cm. If the position looks off, it is recommended to specify both `left_cm`/`top_cm`
  explicitly.
- `update_chart`: `chart_index` is the 0-based sequence number of the chart within the sheet
  (in the order added). At least one of `anchor`/`width_cm`/`height_cm`/`data_range` (+ `categories_range`/
  `titles_from_data`)/`title`/`theme` is required. Specifying `data_range`
  clears all existing series and rebuilds them. **For charts originally in the template**,
  there is a known constraint that openpyxl does not correctly restore their size from the file,
  so if you want to resize them, it is recommended to specify `width_cm`/`height_cm`
  explicitly.
- **Cropping images is not supported.** openpyxl has no stable API for cropping images,
  and forcing an implementation risks corrupting the file, so it is
  intentionally not supported. If the user asks for cropping,
  tell them "excel-edit does not support cropping images" and instead guide them to
  prepare a cropped image file separately.
- **Stacked bar charts are specified with `grouping`** (`type:bar` only). The values are
  `clustered` (default; series side by side) / `stacked` / `percentStacked` (100% stacked).
  With `stacked`/`percentStacked`, the equivalent of `overlap:100` is also set automatically internally
  so that the series overlap correctly (no need to specify it separately). Specifying it for anything other than `type:bar`
  (`line`/`pie`/`scatter`) is an error. With `update_chart` too, you can pass only `grouping`
  to change an existing bar chart to stacked later.
  ```json
  {"op": "add_chart", "sheet": "Sheet1", "type": "bar",
   "data_range": "B1:D5", "anchor": "F1", "grouping": "stacked"}
  ```
- **`data_range`/`categories_range` can only specify a rectangular range within a single sheet.**
  If you want to combine values scattered over several sheets, such as "cell B11 of each of the January-December sheets",
  into the series of one chart, such a range cannot be passed directly to `add_chart`
  (both openpyxl and Excel assume one series is a contiguous range). Trying to build
  `Series`/`Reference` by hand with raw openpyxl code
  easily gets you stuck through misuse of internal APIs (not recommended). Instead, first create one summary
  sheet with `add_sheet`, write formula strings referring to the other sheets in the cells with `set_range`
  (e.g. `"='January'!B11"`) to gather 12 months into one sheet, and then `add_chart` with that summary
  sheet as `data_range`.
  ```json
  [{"op": "add_sheet", "name": "MonthlySummary"},
   {"op": "set_range", "sheet": "MonthlySummary", "start_cell": "A1",
    "rows": [["Month","Total expenses","Total income"],
             ["January","='January'!B11","='January'!B16"],
             ["February","='February'!B11","='February'!B16"]], "header_style": {}},
   {"op": "add_chart", "sheet": "MonthlySummary", "type": "bar",
    "data_range": "B1:C3", "categories_range": "A2:A3", "anchor": "E1"}]
  ```

## Conditional formatting (`add_conditional_format`)

`rule_type`/`params` are passed as is to the constructor arguments of `openpyxl.formatting.rule`.

| rule_type | params example |
|---|---|
| `color_scale` | `{"start_type":"min","start_color":"FFFFFF","end_type":"max","end_color":"FF0000"}` |
| `cell_is` | `{"operator":"greaterThan","formula":["100"],"fill":null}` (`fill` needs a PatternFill, so it is effectively omitted. Applying colors beforehand with `set_style` is also a valid approach) |
| `formula` | `{"formula":["$A1=\"NG\""]}` |
| `data_bar` | `{"start_type":"min","end_type":"max","color":"638EC6"}` |
| `icon_set` | `{"icon_style":"3TrafficLights1","type":"percent"}` |

Invalid `params` return "Failed to apply ops[N]" + the details of the Python exception, so use them as clues to fix it.

## Data validation (`add_data_validation`)

Drop-down example:
```json
{"op": "add_data_validation", "sheet": "Sheet1", "range": "C2:C100", "type": "list",
 "formula1": "\"OK,NG,Pending\"", "prompt": "Choose OK/NG/Pending", "error_message": "The value is not in the list"}
```
`type` is `list`/`whole`/`decimal`/`date`/`textLength`/`custom` (the same as openpyxl's `DataValidation.type`).

## Procedure and edge cases

1. Build the ops from the request (combine data entry = `set_range`, appending to an existing table = `insert_rows` + `set_range`, visual finishing = `format_table`, adjusting individual cells = `set_style`, charts = `add_chart`).
2. Run the command above.
3. When using `--new`, if the target exists and it is unclear whether overwriting is OK, run once without `--overwrite` and confirm with the user based on the error.
4. On success `{"path":...,"sheets":[...],"applied_ops":N}` is returned, so tell the user the save path.

Edge cases: an extension other than xlsx/xlsm / ops not being a JSON array / an op without an `op` key / specifying a nonexistent sheet or op type / specifying an unsupported `theme` name / a missing file without `--new` / an existing file with `--new` and without `--overwrite` — all are errors with exit code 1 (the message includes which op, at which position, failed; use it as a clue to fix it and run again).

## When you want to fix only some columns of an existing table (a mandatory rule)

**When you want to fix only some columns (e.g. A-B) of an existing table with several columns (e.g. A-G), do not delete the rows with `delete_rows` and then pass `set_range` `rows` with only the columns you want to fix.** `set_range`/`insert_row_group` only create the columns included in `rows`, and the columns not passed (C-G in this example) lose their existing data through the row deletion and are not restored, remaining new unformatted empty cells (not only the values but also formatting such as borders and background colors disappear). This call prints a warning in `[stderr]`, so when it appears, switch to one of the following instead of rebuilding the table.

- Operate only on the cells you want to fix, individually, with `set_cell` (values only, or formatting only with `set_style`).
- If the whole table needs to be rebuilt, include the data of all existing columns (both values and formatting) in `rows`.

When writing back only values with `set_cell`, no formatting is applied if the target cell is a newly created unformatted cell as above. To match the look of the surrounding cells, use `inherit_style:true` (see the list of ops above).

## Design check (a mandatory rule)

Before and after adjusting design and layout (colors, borders, column widths, chart placement, how it looks when printed, etc.), always check the sheet as images with the excel-render skill (`render_excel.py` + `analyze_image`). Do not judge the look only from excel-read's cell values and style information and report completion.

## Prohibitions

- Forcibly overwriting an existing file with `--new` and without `--overwrite` (if it errors, confirm with the user whether overwriting is OK before running again).
- Editing or generating `.xls` (an operation outside the supported extensions).
