---
name: pptx-create
description: A skill that generates a new PowerPoint file (.pptx) just by passing a JSON definition. Supports the layouts cover, bullets, section break, two columns, table, number emphasis (stat), picture and blank, and a color theme (8 accent colors) and dark covers can be specified. With the bundled 16:9 template (based on the python-pptx default theme), a designed presentation can be created with one script run. Use it when asked to create a new presentation or slides, or when you want to output a good-looking presentation in pptx format. Use `pptx-edit` for partial edits that keep the design of an existing template (`pptx-inspect` is needed first), and `pptx-read` for checking content.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# pptx-create

A skill that generates a new pptx from a JSON definition. Run `create_pptx.py` to get the
result. It uses `python-pptx` and does not support external apps such as LibreOffice
or rendering thumbnails. The base is the bundled 16:9 template
(`assets/template_16x9.pptx`, based on the python-pptx default theme), and on top of it
`theme` (the color theme) specifies the colors of headings, table header rows and stat numbers.
If a company logo or a custom template needs to be applied, use
`pptx-edit` (partial editing of an existing template) instead of this skill.

On success it prints one line of JSON to stdout with exit code 0; on failure it prints an
error message to stderr with a non-zero exit code.

Call example:
```bash
python create_pptx.py "C:\Users\me\out.pptx" --data '{"slides": [{"layout": "title", "title": "Quarterly report", "subtitle": "FY2026 Q2"}]}'
```
The value of `--data` is a JSON string following the "JSON definition schema" below. When there are
many slides and the JSON gets very long, use
`<absolute path of the output pptx> --data-file <absolute path of a UTF-8 file with the slide definition JSON>`
instead of `--data` (always specify exactly one of `--data`/`--data-file`; specifying both or neither is an error).

## Arguments

| Argument | Required/optional | Type | Default | Description |
|---|---|---|---|---|
| `output_path` (positional) | Required | String (absolute path) | - | Output path of the `.pptx` to generate |
| `--data` | Required, exclusive with `--data-file` | String (JSON) | - | Pass the slide definition JSON as is as a one-line string |
| `--data-file` | Required, exclusive with `--data` | String (absolute path) | - | Path of a UTF-8 file with the slide definition JSON |

Specify an absolute path under a writable directory for `output_path`.

## JSON definition schema

The top level is `{"theme": "...", "slides": [...]}`. `theme` can be omitted
(default `charcoal`) and chooses the colors of the whole presentation from below. Each element of `slides` is
the definition of one slide, and the `layout` key specifies its kind (`content` if omitted).

### theme (color theme, one for the whole presentation)

8 kinds based on the Design Ideas (color palettes) of Anthropic's official pptx skill.
They are applied automatically to the heading text color, the fill of table header rows and the color of stat numbers.
Choose one that suits the topic (when in doubt, `charcoal` is safe; `navy` to convey strength or
expertise; `forest` for growth and environment topics, etc.).

| theme | Main tone |
|---|---|
| `charcoal` (default) | Charcoal gray, a calm neutral tone |
| `navy` | Dark navy, a trustworthy business tone |
| `forest` | Deep green, evoking growth and the environment |
| `coral` | Navy + coral, bright and strong |
| `terracotta` | Terracotta, warm earth tones |
| `ocean` | A deep blue gradient tone |
| `teal` | Teal, a clean tone |
| `berry` | Berry color, calm and elegant |

### layout (slide kind)

| layout | Use | Main keys |
|---|---|---|
| `title` | Cover slide | `title`, `subtitle`, `dark` |
| `content` | Title + bullets | `title`, `bullets`, `notes` |
| `section` | Section break slide | `title`, `dark` |
| `two_content` | Bullets in two columns | `title`, `left_bullets`, `right_bullets` |
| `table` | Title + table | `title`, `table` |
| `stat` | Title + large emphasized numbers (1-4) | `title`, `stats` |
| `picture` | Title + image | `title`, `image_path`, `caption` |
| `blank` | Blank (notes only) | `notes` |

- `bullets` / `left_bullets` / `right_bullets` are lists of strings, possibly mixed with
  dicts like `{"text": "body", "level": 1}` (`level` is the indent depth, 0-based, 0 if omitted).
- `table` has the form `{"headers": ["Col 1", "Col 2"], "rows": [["a", "b"], ["c", "d"]]}` (`headers` can be omitted).
  Specifying `headers` automatically adds a header row in the theme color.
- `stats` has the form `[{"value": "+12%", "label": "Sales growth"}, ...]` (1-4 items).
  It is the "Large stat callouts" pattern of the official pptx skill, with small labels under large bold numbers.
  Passing just one shows a single number emphasized large across the full slide width.
  0 items or 5 or more items end with an error.
- `dark` (only `title`/`section`, default `false`): `true` fills the background with the theme's main color
  and makes the text whitish, giving a dark slide for covers and section breaks (usable for the structure
  recommended by the official skill: "dark covers and closings, light body").
- `image_path` is the absolute path of the image file to insert (it must be readable from the execution environment).
- `notes` (speaker notes) can be specified for every layout.

Sample JSON (compress it into one line to pass it as is to `--data`):
```json
{
  "theme": "navy",
  "slides": [
    {"layout": "title", "title": "Quarterly report", "subtitle": "FY2026 Q2", "dark": true},
    {"layout": "content", "title": "Summary", "bullets": ["Sales +12% year on year", {"text": "Details on the next slide", "level": 1}]},
    {"layout": "stat", "title": "Key metrics", "stats": [{"value": "+12%", "label": "Sales growth"}, {"value": "142", "label": "Monthly contracts"}]},
    {"layout": "table", "title": "Results", "table": {"headers": ["Month", "Sales"], "rows": [["April", "120"], ["May", "135"]]}},
    {"layout": "blank", "notes": "Blank page for Q&A"}
  ]
}
```

Output example:
```json
{"output_path": "C:\\foo\\out.pptx", "total_slides": 4, "size_bytes": 34200}
```
When generation is done, tell the user `output_path` and `total_slides`.

## Edge cases

- Specifying both `--data` and `--data-file`, or neither, ends with an error.
- If the file given to `--data-file` does not exist, it ends with an error.
- If it cannot be parsed as JSON, or the `slides` key is missing/empty, it ends with an error.
- An unsupported `theme` value ends with an error listing the supported ones.
- An unsupported `layout` value ends with an error listing the supported ones.
- With `layout: table`, a missing `table` key or both `headers`/`rows` empty ends with an error.
- With `layout: stat`, empty `stats` (0 items) or 5 or more items end with an error (1-4 items are valid).
- With `layout: picture`, a missing `image_path` or no file at the given path ends with an error.
- If the output directory does not exist, it is created automatically.
- A file with the same name is overwritten (it is good to confirm with the user beforehand whether overwriting is OK).
- Applying a company logo or a custom template (parts that `theme` cannot change) is out of scope for this skill. Use `pptx-edit`.
- If the dependency `python-pptx` is not installed in the environment, it ends with `ModuleNotFoundError` and
  a non-zero exit code. In that case, prompt the installer to run `pip install python-pptx`.

## About the template asset

The bundled `assets/template_16x9.pptx` was generated by `scripts/build_template_16x9.py`
(a build script that scales python-pptx's default 4:3 template to 16:9).
Run it only when revising the template spec itself, and commit the output to
`assets/` again (the LLM does not need to run this script in the normal generation flow).

## Checking after generation, editing existing templates

To check the layout of the generated presentation as images, use `render_pptx.py` of the `pptx-render` skill
+ `analyze_image`. To replace only part of an existing PowerPoint template
(an internal format, etc.) while keeping its design, use `pptx-inspect` -> `pptx-edit`
instead of this skill.
