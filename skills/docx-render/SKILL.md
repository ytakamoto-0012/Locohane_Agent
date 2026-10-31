---
name: docx-render
description: A skill that renders the pages of Word documents (.docx) as images to show them to the LLM with analyze_image (OLE -> PDF -> PNG conversion). Needs a local Microsoft Word installation. Use it when you want to check the layout, tables, image placement and emphasis that text extraction alone cannot show, or to check the look of a docx you generated or edited. Use `docx-read` for extracting and searching text, `docx-create` for generating, and `docx-edit` for editing.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# docx-render

A skill that renders DOCX pages as images to show them to the LLM. Run `render_docx.py` to
get the result. `read_docx.py` of `docx-read` can get the text,
but some information, such as the layout, tables, image placement and emphasis, cannot be read from text alone,
so when you want to grasp the intent of a document more accurately, use this to check the content
as images.

On success it prints one line of JSON to stdout with exit code 0; on failure it prints an
error message to stderr with a non-zero exit code.

Call example:
```bash
python render_docx.py "C:\Users\me\report.docx"
```
## Arguments

| Argument | Required/optional | Type | Default | Description |
|---|---|---|---|---|
| `docx_path` (positional) | Required | String (absolute path) | - | Path of the `.docx` file to render. Other extensions are errors |

All pages are rendered at once (a page range cannot be specified).

Output example:
```json
{"path": "C:\\foo\\report.docx", "tool": "docx", "total_pages": 3, "start_page": 1, "end_page": 3, "dpi": 300, "target_dpi": 150, "crop_applied": false,
 "images": [
   {"page": 1, "image_path": "C:\\...\\rendered\\1a2b3c4d_p1.png", "original_dpi": 300},
   {"page": 2, "image_path": "C:\\...\\rendered\\1a2b3c4d_p2.png", "original_dpi": 300},
   {"page": 3, "image_path": "C:\\...\\rendered\\1a2b3c4d_p3.png", "original_dpi": 300}
 ]}
```
`images` contains all `total_pages` entries.

**Important (2-step procedure)**: this script only saves PNG files and returns their paths in JSON;
it does not go as far as showing the images to the LLM. Next, pass the `image_path` (absolute path) of each element of `images`
as is to the `relative_path` argument of the `analyze_image` tool (a shared tool,
not specific to this skill) and call it.
One `analyze_image` call shows one page, so if there are several pages,
call `analyze_image` once per page.

## Edge cases

- A missing file, a directory, an extension other than `.docx`, a broken file, or Word not installed ends with an error.
- If the number of pages after PDF conversion is 0, it is not an error and returns with exit code 0,
  but note that the shape of the returned JSON differs from normal (in addition to `images: []`, `start_page`/`end_page` become
  `null`, and **the `target_dpi` key itself disappears**).
- Generated PNGs are saved in a temporary folder (under `rendered/`). Rerunning on the same file
  overwrites them.
- If the dependencies `pywin32`, `pypdfium2` and `pillow` are not in the environment, it ends
  with `ImportError` and a non-zero exit code.
