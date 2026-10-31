---
name: pptx-inspect
description: A read-only skill that reads the structure of PowerPoint files (.pptx) to identify what to edit. Gets the per-slide shape structure (shape_index, type, placeholder type, whether there are tables/images, etc.) as JSON. Always use this skill to find out shape_index before partially editing an existing pptx template with `pptx-edit`. Use `pptx-read` for summarizing content and extracting text.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# pptx-inspect

Before editing an existing template, **always** check the slide structure with this skill,
find out `shape_index`, and then call `pptx-edit`. Run `inspect_pptx.py` to
get the result. While `read_pptx.py` of `pptx-read` returns
a human-oriented summary (title/texts/tables/notes), this returns the per-shape structural information
needed for editing.

On success it prints one line of JSON to stdout with exit code 0; on failure it prints an
error message to stderr with a non-zero exit code.

The per-slide body data is not returned directly on stdout; it is written to a temporary JSON file
and its path (`result_path`) is returned. To check the content, read `result_path`.

## inspect_pptx.py — read the structure to identify what to edit

Call example:
```bash
python inspect_pptx.py "C:\Users\me\template.pptx" --start-slide 1 --max-slides 20
```
`--start-slide` (integer, default `1`) / `--max-slides` (integer, default `20`) can be omitted.
Values below 1 are clamped to 1 (e.g. `--max-slides 0` is treated as `1`).

Output example:
```json
{"path": "C:\\foo\\template.pptx", "total_slides": 4, "start_slide": 1, "end_slide": 4,
 "slides_count": 4, "slide_width_cm": 25.4, "slide_height_cm": 19.05,
 "result_path": "C:\\...\\pptx_inspect\\1a2b3c4d_20260805_153012_123456.json"}
```
The per-slide shape structure (`slides`, each element
`{"index", "layout_name", "layout_index", "shapes", "notes_present"}`;
each element of `shapes` is `{"shape_index", "name", "shape_type", "is_placeholder",
"placeholder_idx", "placeholder_type", "has_text_frame", "text_preview",
"has_table", "table_dims", "has_picture", "left_cm", "top_cm", "width_cm",
"height_cm", "fill_color", "border_color", "text_style", "crop"}`) is left out of stdout
and included only in the JSON file that `result_path` points to. Read it
to check `shape_index`, then call `pptx-edit`.

The following fields are also added at the top level:
- `slide_width_cm`/`slide_height_cm`: the slide size (in cm)
- `warnings`: an array of warnings about structural problems (the key is omitted if there are none). Points out shapes that stick out of the slide boundary.

- `shape_index` exactly matches the `shape_index` specified in each operation of `pptx-edit`
  (a 0-based sequence within the slide).
- `text_preview` is truncated to the first 50 characters (reference information for telling what to edit apart;
  use `pptx-read` to get the full text).
- `left_cm`/`top_cm`/`width_cm`/`height_cm` are the shape's current position and size (in cm).
  They exactly match the units that `set_shape_position` of `pptx-edit` takes,
  so the values read here can be used as is as the basis of calculations (e.g. "place it 10cm to the right of shape_index 2"
  -> make this shape's `left_cm + width_cm + 10` the new `left_cm`).
  Shapes such as placeholders that inherit coordinates from the layout and whose actual coordinates cannot be obtained
  are `null`.
- `fill_color`/`border_color` read back, as RRGGBB, the fill color and border color written by `set_shape_style` of `pptx-edit`
  (only solid fills with direct RGB values are supported; theme colors and table shapes are `null`).
- `text_style` likewise reads back the text decoration written by `set_shape_style`, only for the first run of the shape's first paragraph,
  as `{"text_color","bold","italic","underline","font_size_pt","font_name","align"}`
  (even if formatting is mixed per run, only the first one is returned as the representative value.
  Shapes that cannot hold text, or shapes with no characters at all, are `null`).
- `crop` reads back the cropping amounts written by `crop_picture` of `pptx-edit`
  as `{"crop_left","crop_top","crop_right","crop_bottom"}` (0.0-1.0)
  (`null` for image shapes with no cropping applied, and for non-image shapes).
- Paging is designed the same as `pptx-read` (if `total_slides` exceeds `max_slides`,
  call it again with `--start-slide` set to `end_slide + 1`).

## Edge cases

- A missing file, a directory, or a broken file ends with an error
  (the same behavior as `read_pptx.py`).
- If the dependency `python-pptx` is not installed in the environment, it ends with `ModuleNotFoundError` and
  a non-zero exit code. In that case, prompt the installer to run `pip install python-pptx`.
