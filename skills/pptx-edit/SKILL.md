---
name: pptx-edit
description: A skill that partially edits an existing PowerPoint template (.pptx) while keeping the design of its theme, master and layouts. It can replace text, tables and speaker notes, duplicate/delete/reorder slides, add new images and charts, and replace and crop images. Only when the user explicitly asks to change the look, set_shape_style, which changes the colors, text decoration and alignment (left/right/center, etc.) of existing shapes, and set_shape_position, which changes the position and size of shapes, can also be used. Use it when you want to reuse an existing PowerPoint template (an internal format, etc.) and replace only part of it, or duplicate/delete/reorder slides. Always find out shape_index with `pptx-inspect` before running. For simple new generation that does not keep a design, use `pptx-create`.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# pptx-edit

A skill that rewrites only the content of specified slides / duplicates, deletes and reorders slides of an existing pptx (an internal template, etc.)
while keeping the design of its theme, master and layouts.
Run `edit_pptx.py` to get the result. **It is always saved to an `output_path` different from
`template_path`**, and the template itself is not changed (specifying the same path
is an error, as described below).

Before editing, **always** check the slide structure with `inspect_pptx.py` of the `pptx-inspect` skill
and find out `shape_index` (do not build `shape_index` by guesswork).

On success it prints one line of JSON to stdout with exit code 0; on failure it prints an
error message to stderr with a non-zero exit code.

Call example:
```bash
python edit_pptx.py "C:\Users\me\template.pptx" "C:\Users\me\edited.pptx" --data '{"operations": [{"op": "set_title", "slide": 2, "text": "Updated summary"}]}'
```
The value of `--data` is a JSON string following the "JSON operations schema" below. When the JSON is long,
use `<template> <output> --data-file <absolute path of a UTF-8 file with the operations JSON>`
instead of `--data` (always specify exactly one of `--data`/`--data-file`; specifying both or neither is an error).
Add `--overwrite` only when you want to save to the same path as the template
(add it **after confirming with the user that overwriting is OK**).

## JSON operations schema

The top level is `{"operations": [...]}`, and each element is one operation. They are applied to one Presentation
**in order from the start of the array**.

**The `slide` key always refers to "the slide number at the time this edit_pptx.py call started (= the state the preceding pptx-inspect showed)".** Even if later slide numbers shift because `duplicate_slide`/`delete_slide` are used in the same batch, the tool tracks them automatically, so you do not need to calculate by hand "how many it shifted by" for later operations (the numbers you checked with `pptx-inspect` can be used as is for any op). Referring to a slide number deleted after the batch started is an error.

There are 2 exceptions, which specify "where to place it right now", so they refer to **the live slide number at the time the operation is applied** (not the number at the start of the batch):
- `insert_after` of `duplicate_slide` (if omitted, it is set automatically to right after the current live position of the source slide)
- `order` of `reorder_slides` (new slides created by `duplicate_slide` have no number at the start of the batch, so it has to cover all current slides)

When combining `delete_slide`/`duplicate_slide` and `reorder_slides` in the same batch, it is recommended to put `reorder_slides` at the end of the operations (reordering after finishing all structural changes makes the intent clearer).

| op | Required keys | Main optional keys | Content |
|---|---|---|---|
| `set_title` | `slide` | `text` (if omitted `""` = make it empty) | Replace the text of the title placeholder |
| `set_text` | `slide`, `shape_index`, `bullets` | None | Replace the text of any shape (`bullets` is the same as in `pptx-create`: a list of strings or `{"text":..,"level":..}`) |
| `set_table_cell` | `slide`, `shape_index`, `row`, `col` | `text` (if omitted `""` = make it empty) | Replace one cell of an existing table (row/col are 0-based, including the header row) |
| `set_table` | `slide`, `shape_index` | `headers`, `rows` | Replace an existing table as a whole. Possible **only when the numbers of rows and columns exactly match the existing table** (python-pptx cannot add or remove rows/columns of an existing table; to change them, create a new slide with `pptx-create`). If `headers` is omitted, the number of columns needed is inferred from the number of elements in `rows[0]` (if both `headers` and `rows` are omitted/empty, it is treated as 0 rows and 0 columns, which is an error unless it matches the existing table) |
| `set_notes` | `slide` | `text` (if omitted `""` = make it empty) | Replace the speaker notes |
| `replace_picture` | `slide`, `shape_index`, `image_path` | None | Replace only the image of an existing image shape, keeping its position, size, z-order and shape_index |
| `add_picture` | `slide`, `image_path`, `left_cm`, `top_cm` | `width_cm`/`height_cm` (original size if omitted; specifying only one keeps the aspect ratio) | Add a new image to the slide (see below) |
| `crop_picture` | `slide`, `shape_index` | `crop_left`/`crop_top`/`crop_right`/`crop_bottom` (0.0-1.0, at least one) | Crop an existing image shape (see below) |
| `add_chart` | `slide`, `type`, `left_cm`, `top_cm`, `width_cm`, `height_cm`, `categories`, `series` | `title`, `theme`, `show_data_labels` (default true) | Add a new chart to the slide (see below) |
| `set_shape_style` | `slide`, `shape_index` | `role` (`heading`/`table_header`) + `theme`, or `text_color`/`bold`/`italic`/`underline`/`font_size_pt`/`font_name`/`fill_color`/`border_color`/`align` (`left`/`center`/`right`/`justify`); for table shapes `row`/`all_rows` | Explicitly change the colors, decoration and text alignment of an existing shape (see below) |
| `set_shape_position` | `slide`, `shape_index` | `left_cm`/`top_cm`/`width_cm`/`height_cm` (in cm, at least one) | Change the position and size of an existing shape (see below) |
| `duplicate_slide` | `slide` | `insert_after` (if omitted, the current live position of the source slide), `count` (1 if omitted) | Duplicate the specified slide with the same layout and insert `count` copies right after `insert_after`. It can duplicate placeholders, tables, text boxes and images |
| `delete_slide` | `slide` | None | Delete the specified slide |
| `reorder_slides` | `order` | None | Pass **a permutation of all slide numbers (1-based) at that time** to change to that order (e.g. `[2,1,3]` swaps the 1st and 2nd) |

Sample JSON:
```json
{"operations": [
  {"op": "set_title", "slide": 2, "text": "Updated summary"},
  {"op": "set_table_cell", "slide": 3, "shape_index": 2, "row": 1, "col": 1, "text": "150"},
  {"op": "duplicate_slide", "slide": 4, "insert_after": 4, "count": 2},
  {"op": "delete_slide", "slide": 1}
]}
```

Output example:
```json
{"output_path": "C:\\foo\\edited.pptx", "backup_path": null, "total_slides": 5, "size_bytes": 37998, "applied_operations": 4}
```
When generation is done, tell the user `output_path` and `total_slides`.

**`backup_path` (automatic backup)**: when a file already exists at `output_path`
(when overwriting the template with `--overwrite`, or reusing an existing `output_path`),
it is copied with a timestamp to the same folder right before saving and then
overwritten. This holds the absolute path of the copy. `null` if there was nothing to back up (saving to a new path, etc.).

## Shapes duplicate_slide does not support

If the source slide contains charts, SmartArt, video, OLE objects or group shapes
(`shape_type` is one of `CHART`/`DIAGRAM`/`IGX_GRAPHIC`/`MEDIA`/`WEB_VIDEO`/`EMBEDDED_OLE_OBJECT`/
`LINKED_OLE_OBJECT`/`OLE_CONTROL_OBJECT`/`GROUP`), `duplicate_slide`
**ends with an error** so as not to generate a broken pptx (can be checked beforehand with `shape_type` of `pptx-inspect`).
Placeholders, text boxes, tables and images (`PICTURE`) can be duplicated.

## set_shape_style / set_shape_position (explicitly changing the existing design and placement)

All the other ops of this skill replace only content (text, table values, images, slide structure)
and **never touch the look** (keeping the template's theme, master and layouts
is the reason this skill exists). `set_shape_style`/`set_shape_position` are
the only exceptions; use them **only when the user explicitly asks to change the look or placement**, such as "make it look cooler",
"easier to read", "center it", "make it bigger".
Do not call them proactively when not asked.

These ops cover most "fix the layout" requests: text color, fill,
border color, bold/italic/underline, font, font size, text alignment (`set_shape_style`), and
the position and size of the shape itself (`set_shape_position`). For requests they still cannot handle (border colors of tables,
special effects such as shadows/gradients, slide background color), see "Not supported" at the end of this section
and tell the user it cannot be done.

```json
{"op": "set_shape_style", "slide": 2, "shape_index": 0, "role": "heading", "theme": "navy"}
{"op": "set_shape_style", "slide": 3, "shape_index": 2, "role": "table_header", "theme": "navy"}
{"op": "set_shape_style", "slide": 1, "shape_index": 1, "fill_color": "F2F2F2", "text_color": "1E2761", "bold": true, "align": "center"}
{"op": "set_shape_style", "slide": 1, "shape_index": 2, "italic": true, "underline": true, "font_name": "Yu Gothic", "border_color": "1E2761"}
{"op": "set_shape_position", "slide": 1, "shape_index": 1, "left_cm": 2.5, "top_cm": 1.0}
{"op": "set_shape_position", "slide": 1, "shape_index": 1, "width_cm": 10, "height_cm": 6}
```

- `role` (optional): `"heading"` (`theme`'s primary color + bold) or `"table_header"`
  (`theme`'s primary fill + text_on_primary text color + bold on the header row of a table shape;
  an error if specified for a non-table shape). Used together with `theme` (the same 8 kinds as `pptx-create`).
- Individual settings: `text_color`/`bold`/`italic`/`underline`/`font_size_pt`/`font_name` (text),
  `fill_color` (fill; the whole shape for text boxes etc., the cell background of the target rows for table shapes),
  `border_color` (the shape's border color; **not supported for table shapes**), `align` (`left`/`center`/
  `right`/`justify`, paragraph text alignment). They take precedence over `role`.
- For table shapes, decide the target rows with one of `role: "table_header"` (row 1 only) / `row` (one 0-based row number)
  / `all_rows: true` (all rows).
- `set_shape_position` changes only those of `left_cm`/`top_cm`/`width_cm`/`height_cm` that are specified,
  leaving the omitted ones at their original values. It can be used as is on images and charts added with `add_picture`/`add_chart`
  (`shape_type` `PICTURE`/`CHART`)
  (check the new `shape_index` with `pptx-inspect` after adding, then call it).
- A call with only `theme` and no style keys is an error.

**Not supported (what the current ops cannot do)**: cell border colors of table shapes, shape effects such as shadows, gradients and
reflections, slide background color, line spacing and paragraph indentation, adding new shapes (text
boxes, autoshapes, etc.), recoloring charts already in the template
(new charts can be colored with `theme` of `add_chart`; changing the colors of existing charts is not supported).

## add_picture / add_chart (adding new images and charts)

```json
{"op": "add_picture", "slide": 2, "image_path": "C:\\img\\photo.png",
 "left_cm": 2.0, "top_cm": 3.0, "width_cm": 10.0, "height_cm": 6.0}
{"op": "add_chart", "slide": 3, "type": "bar",
 "left_cm": 2, "top_cm": 3, "width_cm": 20, "height_cm": 10,
 "categories": ["April","May","June"],
 "series": [{"name": "Sales", "values": [120, 135, 150]}],
 "title": "Monthly results", "theme": "navy", "show_data_labels": true}
```

- `add_picture`: `left_cm`/`top_cm` are required. Omitting both `width_cm`/`height_cm` gives
  the image's original size; specifying only one calculates the other automatically keeping the aspect ratio.
- `add_chart`: `type` supports only `bar`/`line`/`pie` (**scatter charts are not supported**).
  Write the data directly in JSON with `categories` (an array of category names) and `series` (an array of `{"name","values"}`,
  at least 1; the number of elements in each `values` must match `categories`)
  (pptx has no cell references like Excel). `show_data_labels` defaults to
  `true` (`pie` shows %, others show values).
- Specifying `theme` colors the series (not `pie`). **These series colors do not use `theme`'s
  `primary`/`secondary`/`accent` but a fixed palette of 8 colors that takes color vision diversity (CVD) into account**
  (cycling the 3 colors such as `primary` would repeat colors from the 4th series on).
  `theme` is accepted mainly to keep the notation consistent with other ops such as heading;
  whichever theme name you specify, the series colors themselves do not change.
- Added images and charts just go into `slide.shapes`, so to check their `shape_index`,
  just rerun `pptx-inspect` as usual (there is no checking procedure specific to `add_picture`/`add_chart`).
- Slides containing charts added with `add_chart` cannot be duplicated with `duplicate_slide`
  (`UNSUPPORTED_DUPLICATE_TYPES` includes `CHART`, so it is an error like other charts).

`crop_picture` crops an existing image shape (any of those added with `add_picture`, originally in the template,
or replaced with `replace_picture`):
```json
{"op": "crop_picture", "slide": 2, "shape_index": 3,
 "crop_left": 0.1, "crop_top": 0.0, "crop_right": 0.1, "crop_bottom": 0.2}
```
`crop_left`/`crop_top`/`crop_right`/`crop_bottom` are the fractions cut off from each side of the image
(`0.0` = do not cut, `1.0` = cut that side off completely; the same idea as PowerPoint's
"Crop" feature), and values outside 0.0-1.0 are errors. The original image data
itself is kept and only the displayed range changes, so the cropping can later be removed or readjusted in Office.
The outer frame size of the shape (placement and size) does not change, so to arrange the placement
after cropping, use `set_shape_position` together.

## Edge cases

- If `template_path` and `output_path` are the same and `--overwrite` is not given, it ends with an error.
- Specifying both `--data` and `--data-file`, or neither, ends with an error.
- A nonexistent `slide` / `shape_index`, referring to a `slide` already deleted with `delete_slide` in this batch,
  an operation on the wrong kind of shape (e.g. `set_table_cell` on a non-table shape), a row/column mismatch in `set_table`,
  a `reorder_slides` permutation that does not match the current number of slides,
  a missing `image_path` for `replace_picture`, an unsupported `theme`/`role`/`align`,
  `set_shape_style` with no style keys at all, `set_shape_style` on a table shape without target rows,
  `set_shape_style` on a table shape with `border_color`,
  `set_shape_position` with no position/size keys at all, a missing `image_path` for `add_picture`,
  an unsupported `type` for `add_chart` (other than bar/line/pie) or a mismatch between the number of `values` in
  `series` and the number of `categories`, and `crop_picture` on a non-image shape / with no crop keys / with values
  outside 0.0-1.0 all end with an error that includes the operation number.
- Even running `duplicate_slide` after `delete_slide` left gaps is safe to combine, because collisions of slide
  part names (a known limitation of python-pptx) are avoided automatically internally.
- If an error occurs in the middle of the operations, the file is not saved to `output_path`
  (the template itself is not changed on the spot either, so even a failure midway does not affect existing files).
- An unsupported `op` value ends with an error listing the supported ones.
- If the dependency `python-pptx` is not installed in the environment, it ends with `ModuleNotFoundError` and
  a non-zero exit code. In that case, prompt the installer to run `pip install python-pptx`.

## Checking after editing

To check the layout of the edited result as images, use `render_pptx.py` of the `pptx-render` skill
+ `analyze_image`.
