---
name: excel-render
description: A skill that renders xlsx/xlsm/xls sheets as images via OLE -> PDF -> PNG conversion, showing the LLM the look that cell values alone cannot show, such as borders, formatting, charts and layout. Microsoft Excel must be installed locally. Rendering alone does not let the LLM see the content, so the 2-step procedure of checking with analyze_image next is mandatory. Use it when the user wants to check the layout or borders of an Excel file, or to verify the look before and after decoration work with excel-edit.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# excel-render

A skill that renders Excel sheets as images to show them to the LLM. Runs `render_excel.py`.

The excel-read skill can get numbers and text but cannot read borders, formatting, charts or layout, so check with images when you want to grasp the intent of a sheet more accurately.

Before converting to PDF, it automatically forces each sheet's print settings to "1 page wide x 1 page tall" fit printing (`Zoom=False` + `FitToPagesWide=1` + `FitToPagesTall=1`), preventing the used range from being split into several pages (fragmented images). With `--print-as-is`, it does not force this and converts to PDF using the print area and scaling already set on the sheet. PDF -> image conversion is done at 300 DPI by default, and the cropped output is shrunk to `target_dpi`=150. The smaller the sheet's scale, the more the capture DPI and `target_dpi` are raised automatically (up to 900 DPI; no boost with `--print-as-is`). All sheets (all pages after PDF conversion) are rendered at once.

## Calling it

If you just want to check the look, such as borders, formatting and charts, use this first (it forces a fit to 1 page wide x 1 page tall regardless of the print settings).

```bash
python render_excel.py "C:\Users\me\book.xlsx"
```

Add `--print-as-is` only when you want to check the print area and scaling already set on the sheet as they are (without forced fitting). Use it when told "I want to check how it looks when printed" or "forced fitting looks unnatural / differs from the print area I set".

```bash
python render_excel.py "C:\Users\me\book.xlsx" --print-as-is
```

## Arguments

| Argument | Required/optional | Type | Default | Description |
|---|---|---|---|---|
| `excel_path` (positional) | Required | String (absolute path) | - | Path of the `.xlsx`/`.xlsm`/`.xls` file to render. Other extensions are errors |
| `--print-as-is` | Optional | Flag | Not specified (= forced fit) | When given, converts to PDF using the print area, orientation and scaling (Zoom/FitToPagesWide/FitToPagesTall) already set on each sheet. It does not do the default "forced fit to 1 page wide x 1 page tall", so depending on the sheet's settings it may be split into several pages (in this case too, no DPI boost and no split warning (10% minimum) are applied) |

## Input/output types

Success = exit code 0 + one line of JSON on stdout. Failure = non-zero exit code + an error message on stderr. Pass the error text to the user as is.

## Output

```json
{"path": "C:\\foo\\book.xlsx", "tool": "excel", "total_pages": 5, "start_page": 1, "end_page": 5,
 "dpi": 300, "target_dpi": 150, "crop_applied": true,
 "images": [{"page": 1, "sheet": "Sheet1", "image_path": "C:\\...\\rendered\\1a2b3c4d_Sheet1_cropped.png", "original_dpi": 300, "cropped": true}, ...]}
```
`images` contains all pages (= all sheets, normally `total_pages` entries, excluding hidden sheets).
The sheet name is used as is in the image file name (`1a2b3c4d_Sheet1_cropped.png`, etc.). A page number like `_p2` is added only when one sheet is split into several pages.
Each element of `images` always has `original_dpi` (the actual resolution before cropping) and `sheet` (the original sheet name). `cropped` (`true` if bbox detection
succeeded and it could be cropped, `false` if detection failed and the image is left as is) is added to each image by default.

**Dynamic DPI boost**: the harder the sheet content is to fit on the paper, the more the capture DPI and `target_dpi` may be raised automatically (up to 900 DPI).

## Important (2-step procedure, a mandatory rule)

This script only saves PNGs and returns their paths in JSON; showing them to the LLM is a separate step. Pass each `images[].image_path` (absolute path) next to the `relative_path` argument of `analyze_image` (a shared tool). One `analyze_image` call shows one page, so split the calls for several pages. Always follow this procedure for the `image_path` returned by `render_excel.py` (rendering alone does not count as having checked the content).

Before and after adjusting design and layout (colors, borders, column widths, chart placement, how it looks when printed, etc.), always check the sheet as images with this skill + `analyze_image`. Do not judge the look only from excel-read's cell values and style information and report completion.

Margin removal is on by default (black/white boundary detection).

## Edge cases

A missing file / a directory / an extension other than `.xlsx`, `.xlsm`, `.xls` / a broken file / Excel not installed ends with an error. If the PDF has 0 pages, e.g. because there are no sheets, it returns with exit code 0, but note that the shape of the JSON differs from normal (in addition to `images: []`, `start_page`/`end_page` become `null`, and **the `target_dpi` key itself disappears**). Generated PNGs are saved under `rendered/` and overwritten when rerun on the same file. Missing `pywin32`/`pypdfium2`/`pillow` gives `ImportError` (prompt the user to run `pip install <package name>`).

It starts EXCEL.EXE temporarily. It always closes it when done, but if the script is forcibly terminated, e.g. by the execution timeout, it may be left behind. In that case, instead of ending it manually in Task Manager, run `recalc_excel.py` of the `excel-recalc` skill or `edit_vba.py` of the `excel-vba-edit` skill with only `--recover-locks` (it terminates internally only those COM processes started by this session with excel-render etc. that were left behind and are still alive).

If the total of a sheet's column widths and row heights falls below the minimum scale (10%) for fitting it on one page, it is split into several pages and a `warnings` array is added to the output JSON (the same content is also printed to stderr). The exit code stays 0. Review the column widths with `set_column_width` of `excel-edit` (the unit is character width, roughly 1-60).
