---
name: pdf-tools
description: Extracts and reads text from PDF files, renders PDF pages as images so the LLM can grasp the layout, figures/tables and scanned content visually, and generates designed PDF files from semantic HTML (headings, tables, bullets, emphasis boxes, etc.) (supports Japanese). Use it when the user wants to check the content of a PDF, summarize or search the text of a PDF, check the tables, figures and layout of a PDF, know the content of a scanned PDF whose text cannot be extracted, or output and save reports and documents as good-looking PDFs.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# pdf-tools

A skill that reads PDFs (text extraction / rendering pages as images) and generates PDFs. Run its three
scripts to get results. It supports reading and writing Japanese PDFs.

On success each script prints one line of JSON to stdout with exit code 0; on failure it prints an
error message to stderr with a non-zero exit code.

`read_pdf.py` does not return the extracted page text directly on stdout; it writes it to a temporary JSON
file and returns its path (`result_path`). To check the content,
read `result_path` (the content is formatted over multiple lines, so it can be read partially).

## 1. read_pdf.py — extract text from PDF

Call example:
```bash
python read_pdf.py "C:\Users\me\report.pdf" --start-page 1 --max-pages 20
```
`--start-page`/`--max-pages` can be omitted (defaults start-page=1, max-pages=20).

Output example:
```json
{"path": "C:\\foo\\report.pdf", "total_pages": 42, "start_page": 1, "end_page": 20,
 "metadata": {"title": "Document title", "author": null, "subject": null},
 "pages_count": 20,
 "result_path": "C:\\...\\pdf_read\\1a2b3c4d_20260805_153012_123456.json"}
```
The page text (`pages`, each element `{"page", "text", "width_pt", "height_pt"}`) is left out of stdout and
included only in the JSON file that `result_path` points to. Read `result_path`,
and report the content to the user by joining the `text` of each element, or summarize it.
`width_pt`/`height_pt` (in pt) are the page size and are useful when verifying layout.
If `total_pages` is larger than `max_pages`, tell the user that to read on, call it again with `--start-page`
set to `end_page + 1` (the page version of the same idea as offset/limit of `read_file.py`).

Edge cases:
- A missing file or a directory ends with an error.
- A broken PDF or a non-PDF file also ends with an error.
- For password-protected PDFs, it tries decrypting with an empty password and ends with an error if that fails
  (stderr says the PDF is password-protected).
- If `start_page` exceeds the total number of pages, it is not an error;
  it returns `pages_count: 0`, `start_page`/`end_page`: `null` with exit code 0.
- For PDFs that are only scanned images (not OCR'd), `text` may be an empty string.
  In that case, render the pages as images with `render_pdf_pages.py` below and show them
  directly to the LLM with `analyze_image` (this skill does not do OCR).
- If only some pages are internally broken, only those pages get `text: ""` and
  an `error` key (the reason for failure), and the other pages are extracted as usual
  (one broken page does not make the whole thing fail).

## 2. render_pdf_pages.py — render PDF pages as images to show them to the LLM

Use it to know the content of scanned or image-based PDFs whose text `read_pdf.py` cannot get well,
and also, since even when the text can be obtained some information such as tables, figures and multi-column layout
cannot be read from text alone, use it to check the content as images when you want to grasp the intent of a document
more accurately.

Call example:
```bash
python render_pdf_pages.py "C:\Users\me\report.pdf"
```
All pages are rendered at once (a range cannot be specified). The resolution is fixed at 150 DPI by default.

Output example:
```json
{"path": "C:\\foo\\report.pdf", "total_pages": 3, "start_page": 1, "end_page": 3, "dpi": 150,
 "images": [
   {"page": 1, "image_path": "C:\\...\\pdf_rendered\\1a2b3c4d_p1.png"},
   {"page": 2, "image_path": "C:\\...\\pdf_rendered\\1a2b3c4d_p2.png"},
   {"page": 3, "image_path": "C:\\...\\pdf_rendered\\1a2b3c4d_p3.png"}
 ]}
```
`images` contains all `total_pages` entries.

**Important (2-step procedure)**: this script only saves PNG files and returns their paths in JSON;
it does not go as far as showing the images to the LLM. Next, pass the `image_path` (absolute path) of each element of `images`
as is to the `relative_path` argument of the `analyze_image` tool (a shared tool,
not specific to this skill) and call it (`analyze_image`
can read absolute paths as is). One `analyze_image` call shows one page,
so if there are several pages, call `analyze_image` once per page.

Edge cases:
- A missing file, a directory, or a broken/encrypted PDF ends with an error.
- Generated PNGs are saved in a temporary folder (under `pdf_rendered/`). Rerunning on the same PDF
  and page overwrites them.
- If only some pages are internally broken, the elements of those pages have
  `error` (the reason for failure) instead of `image_path`, and the other pages are rendered
  as usual (one broken page does not make the whole thing fail).

## 3. create_pdf.py — generate PDF from semantic HTML

The caller (the LLM) writes HTML such as headings, paragraphs, tables and bullets directly, and this script
arranges colors and spacing automatically with a bundled theme. A designed layout
(tables, emphasis boxes, header/footer, page numbers) can be made with one script run.

Call example:
```bash
python create_pdf.py "C:\Users\me\out.pdf" --title Report --header-text Confidential --footer-text "Sample Inc." --page-number --html '<h1>Quarterly report</h1><p>This is the body. <b>Important parts</b> can be bold.</p><div class="callout">Supplementary notes can be emphasized like this.</div><table><tr><th>Month</th><th>Sales</th></tr><tr><td>April</td><td>120</td></tr></table>'
```
The value of `--html` is an HTML fragment following "Available HTML tags" below (only the content of `<body>`;
`<html>`/`<body>` are not needed). When it gets long, use
`<absolute path of the output PDF> --html-file <absolute path of a UTF-8 file with the body HTML>`
instead of `--html` (always specify exactly one of `--html`/`--html-file`; specifying both or neither is an error).
`--html-file` is read as UTF-8, and if it cannot be decoded as UTF-8, reading it as
cp932 (the Shift-JIS family) is tried (to avoid undetectable garbled text,
basically save the body HTML file in UTF-8).
`--title` sets the PDF metadata (title) (optional).

### Available HTML tags and CSS classes

| Tag/class | Use |
|---|---|
| `<h1>`-`<h4>` | Headings (bold sans-serif in the accent color; only `h1` is underlined) |
| `<p>` | Paragraph |
| `<b>`/`<strong>`, `<i>`/`<em>` | Bold, italic |
| `<table><tr><th>...</th></tr><tr><td>...</td></tr></table>` | Table (header row with `th`; an accent-colored header band + rules are added automatically) |
| `<ul>`/`<ol>`+`<li>` | Bullets, numbered lists |
| `<div class="callout">...</div>` | An emphasis box with a colored band on the left (for supplements and cautions) |
| `<div class="page-break"></div>` | Explicit page break |
| `<hr>` | Divider |
| `<img src="C:\\...\\chart.png" width="300">` | Insert an image (`src` is an absolute path that exists) |

**Tags other than the above (`<script>`, `<button>`, JS such as `onclick`, tab switching,
interactive UI such as line charts) cannot be used** (as said above, no JS is
executed). A PDF is a static printed document, so there is no need to design it to "put several patterns on one screen"
with tab switching or collapsible UI meant for browsers;
the same content can be expressed simply by arranging sections vertically with headings (`<h1>`-`<h4>`) and page breaks (`div.page-break`).
If HTML you already generated contains things not in this tag list,
rewrite the HTML using only this tag list before giving up and suggesting another library.

Do the visual refinement with semantic tag structures within this range; there is no need to specify
colors, fonts or spacing individually with `style` attributes (the bundled theme applies them all at once).

### Appearance options (all optional)

| Option | Default | Description |
|---|---|---|
| `--accent-color` | `1F4E78` | Accent color used for headings and table header rows (RRGGBB hex) |
| `--page-size` | `a4` | `a4` or `letter` |
| `--orientation` | `portrait` | `portrait` or `landscape` |
| `--margin-cm` | `2.0` | Margin common to all sides (cm) |
| `--header-text` | None | A string shown small at the top of the page (e.g. Confidential) |
| `--footer-text` | None | A string shown at the bottom center of the page (e.g. company name) |
| `--page-number` | Disabled | When given, adds page numbers in the form `1 / 3` to the footer (can be combined with `--footer-text`) |

Output example:
```json
{"output_path": "C:\\foo\\report.pdf", "size_bytes": 18234}
```
When generation is done, tell the user `output_path`.

Edge cases:
- Specifying both `--html` and `--html-file`, or neither, ends with an error.
- If the file given to `--html-file` does not exist, it ends with an error.
- If `--accent-color` is not a hexadecimal RRGGBB value, it ends with an error.
- If the output directory does not exist, it is created automatically.
- A file with the same name is overwritten (it is good to confirm with the user beforehand whether overwriting is OK).
- Japanese is drawn by embedding the actual glyphs of TTF fonts bundled with Windows (Yu Gothic = sans-serif headings, Yu Mincho = serif body;
  substituted with Meiryo/MS Gothic/MS Mincho if missing) into the PDF.
  It displays with the same glyphs regardless of the viewer. In environments where no standard Windows Japanese font
  is found (e.g. English Windows without the Japanese supplemental fonts), text without Japanese characters is drawn
  with Latin fonts (Arial/Segoe UI, Times New Roman/Georgia) instead, but text containing Japanese characters ends with an error.

## Common edge cases

- If the dependencies `pypdf` (text extraction), `pypdfium2` (rendering) and `reportlab` (generation) are not
  in the environment, it ends with `ModuleNotFoundError` and a non-zero exit code. In that case, prompt the installer to
  run `pip install pypdf pypdfium2 reportlab`.
- None of the scripts throw exceptions; errors are returned via stderr + a non-zero exit code.
  Check the standard error output.
