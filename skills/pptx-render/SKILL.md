---
name: pptx-render
description: A skill that renders the slides of PowerPoint files (.pptx) as images to show them to the LLM with analyze_image (OLE -> PDF -> PNG conversion). Needs a local Microsoft PowerPoint installation. Use it when you want to check the layout, figures, image placement and emphasis that text extraction alone cannot show, or to check the look of a pptx you generated or edited. Use `pptx-read` for extracting text, `pptx-create` for creating new ones, and `pptx-edit` for partially editing existing templates.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# pptx-render

A skill that renders PPTX slides as images to show them to the LLM. Run `render_pptx.py` to
get the result. `read_pptx.py` of `pptx-read` can get the text,
but some information, such as the layout, figures, image placement and emphasis, cannot be read from text alone,
so when you want to grasp the intent of a presentation more accurately, use this to check the content
as images.

On success it prints one line of JSON to stdout with exit code 0; on failure it prints an
error message to stderr with a non-zero exit code.

Call example:
```bash
python render_pptx.py "C:\Users\me\presentation.pptx"
```
## Arguments

| Argument | Required/optional | Type | Default | Description |
|---|---|---|---|---|
| `pptx_path` (positional) | Required | String (absolute path) | - | Path of the `.pptx` file to render. Other extensions are errors |

All slides are rendered at once (a range cannot be specified).

Output example:
```json
{"path": "C:\\foo\\presentation.pptx", "tool": "pptx", "total_pages": 12, "start_page": 1, "end_page": 12, "dpi": 300, "target_dpi": 150, "crop_applied": false,
 "images": [
   {"page": 1, "image_path": "C:\\...\\rendered\\1a2b3c4d_p1.png", "original_dpi": 300},
   {"page": 2, "image_path": "C:\\...\\rendered\\1a2b3c4d_p2.png", "original_dpi": 300},
   {"page": 12, "image_path": "C:\\...\\rendered\\1a2b3c4d_p12.png", "original_dpi": 300}
 ]}
```
`images` contains all `total_pages` entries.

**Important (2-step procedure)**: this script only saves PNG files and returns their paths in JSON;
it does not go as far as showing the images to the LLM. Next, pass the `image_path` (absolute path) of each element of `images`
as is to the `relative_path` argument of the `analyze_image` tool (a shared tool,
not specific to this skill) and call it.
One `analyze_image` call shows one slide, so if there are several slides,
call `analyze_image` once per slide.

## Edge cases

- A missing file, a directory, an extension other than `.pptx`, a broken file, or PowerPoint not installed ends with an error.
- If the number of pages after PDF conversion is 0, it is not an error and returns with exit code 0,
  but note that the shape of the returned JSON differs from normal (in addition to `images: []`, `start_page`/`end_page` become
  `null`, and **the `target_dpi` key itself disappears**).
- Generated PNGs are saved in a temporary folder (under `rendered/`). Rerunning on the same file
  overwrites them.
- If the dependencies `pywin32`, `pypdfium2` and `pillow` are not in the environment, it ends
  with `ImportError` and a non-zero exit code.
