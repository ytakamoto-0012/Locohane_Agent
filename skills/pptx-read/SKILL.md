---
name: pptx-read
description: A read-only skill for PowerPoint (.pptx). Extracts the title, body text, tables and speaker notes of each slide as a human-oriented summary. Uses python-pptx; no external apps such as LibreOffice are needed. Use it when the user wants to check or summarize the content of a PowerPoint file, or extract text and tables from a pptx. Use `pptx-create` for creating new ones, `pptx-edit` for partially editing existing templates (`pptx-inspect` is needed first), and `pptx-render` to check the layout as images.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# pptx-read

A skill that reads pptx (extracts text, tables and speaker notes).
Run `read_pptx.py` to get the result.
It uses `python-pptx`; no external apps such as LibreOffice are needed.

On success it prints one line of JSON to stdout with exit code 0; on failure it prints an
error message to stderr with a non-zero exit code.

The per-slide body data is not returned directly on stdout; it is written to a temporary JSON file
and its path (`result_path`) is returned. To check the content, read `result_path`
(the content is formatted over multiple lines, so it can be read partially).

## read_pptx.py — extract text, tables and notes from pptx

Call example:
```bash
python read_pptx.py "C:\Users\me\sample.pptx" --start-slide 1 --max-slides 20
```
`--start-slide`/`--max-slides` can be omitted (defaults start-slide=1, max-slides=20).

Output example:
```json
{"path": "C:\\foo\\sample.pptx", "total_slides": 12, "start_slide": 1, "end_slide": 12,
 "slides_count": 12,
 "result_path": "C:\\...\\pptx_read\\1a2b3c4d_20260805_153012_123456.json"}
```
The per-slide body (`slides`, each element `{"index", "title", "texts", "tables", "notes"}`) is
left out of stdout and included only in the JSON file that `result_path` points to.
Read it to check the content. The keys of each element mean the following.

- `title`: the text of the slide's title placeholder (`null` if none).
- `texts`: the list of texts of shapes with text other than the title (1 shape = 1 element; multiple lines stay separated by `\n`).
- `tables`: if there are tables, stored as a list of 2D arrays (all rows including the first). `[]` if there are no tables.
- `notes`: the text of the speaker notes (`null` if none).

If `total_slides` is larger than `max_slides`, tell the user that to read on, call it again with `--start-slide`
set to `end_slide + 1` (the slide version of the same idea as start-page/max-page of `read_pdf.py`).

## Edge cases

- A missing file, a directory, or a broken non-pptx file ends with an error.
- If `start_slide` exceeds the total number of slides, it is not an error;
  it returns `slides_count: 0`, `start_slide`/`end_slide`: `null` with exit code 0.
- If the dependency `python-pptx` is not installed in the environment, it ends with `ModuleNotFoundError` and
  a non-zero exit code. In that case, prompt the installer to run `pip install python-pptx`.

## To identify what to edit

If you want to know the structure per shape (`shape_index`, etc.) in order to edit an existing template,
use `pptx-inspect` instead of this skill.
