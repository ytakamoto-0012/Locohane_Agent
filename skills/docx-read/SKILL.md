---
name: docx-read
description: A read-only skill for Word documents (.docx). Gets the paragraphs, tables, document properties (title, author, creation date, etc.) and whether there are Track Changes (revisions). Unlike Excel etc., the application itself is not needed. Use it when the user wants to check, summarize or search the content of a Word document, understand the chapter structure or the contents of tables, or check before editing whether there are existing tracked changes. Reading .doc (the legacy binary format) is not supported. Use `docx-create` for creating new ones, `docx-edit` for editing existing documents, and `docx-render` to check the layout as images.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# docx-read

A skill that reads Word documents (`.docx`) and gets the paragraphs, tables, document properties and whether there are
Track Changes (revisions). Run `read_docx.py` to get the result.

This skill handles only `.docx` (the XML format of Word 2007 and later). **Reading `.doc`
(the legacy binary format) is not supported.** If the user gives you a `.doc`
file, guide them to open it in Microsoft Word and convert it to `.docx` with
"Save As".

On success it prints one line of JSON to stdout with exit code 0; on failure it prints an
error message to stderr with a non-zero exit code.

The body data (paragraphs, tables) is not returned directly on stdout; it is written to a temporary JSON file
and its path (`result_path`) is returned. To check the content, read `result_path`
(the content is formatted over multiple lines, so it can be read partially).

## read_docx.py — read docx (get paragraphs, tables, properties)

Call example:
```bash
python read_docx.py "C:\Users\me\report.docx" --offset 0 --limit 300
```
`--offset`/`--limit` can be omitted (defaults offset=0, limit=300). Paging is per paragraph
(the same idea as the line-number version of `read_file.py`). Tables (`tables`) are all returned by default.

Output example:
```json
{
  "path": "C:\\foo\\report.docx",
  "total_paragraphs": 450,
  "start_index": 0,
  "end_index": 299,
  "paragraphs_count": 300,
  "table_count": 1,
  "tables_count": 1,
  "body_order_count": 452,
  "core_properties": {"title": "Report", "author": "Taro Yamada", "created": "2026-01-10T09:00:00", "modified": null},
  "track_changes": {"has_pending_revisions": false, "insertion_count": 0, "deletion_count": 0},
  "result_path": "C:\\...\\docx_read\\1a2b3c4d_20260805_153012_123456.json"
}
```

The paragraph text (`paragraphs`, each element `{"index", "style", "text"}`) and the table bodies
(`tables`, "1 table = an array of rows (each row is an array of cell strings)") are left out of stdout
as above and included only in the JSON file that `result_path` points to.
Read `result_path` to check the content.

The following fields are also returned at the top level (for detecting structural problems):
- `sections`: the dimensions and margins of each section (page setup) in the document.
  `{"page_width_cm", "page_height_cm", "left_margin_cm", "right_margin_cm", "top_margin_cm", "bottom_margin_cm"}`.
- `table_column_widths`: for each table, an array of column widths (`[[12.3, null, 8.5], [15.2, 10.0]]`).
  Columns not set because of automatic layout are `null`. Only tables in the body (tables inside text boxes are not included).
- `inline_images`: the list of images inserted directly in the body (`[{"image_index": 0, "width_cm": 10.5, "height_cm": 5.2}, ...]`).
  `image_index` is the 0-based sequence number of `doc.inline_shapes` and exactly matches the `image_index`
  that `set_image_size` of the `docx-edit` skill takes.
- `warnings`: an array of warnings about structural problems (the key is omitted if there are none).
  Points out when the total column width of a table exceeds the page width, or an inserted image exceeds the page width.
  Word wraps text in table cells by default, so "a table/image exceeds the page width" is the main thing detected,
  rather than "text gets cut off".

- The `style` of each element of `paragraphs` can be used to determine heading levels
  (`"Title"`, `"Heading 1"`, `"Heading 2"`, ... , `"Normal"`, etc.).
  Report `text` to the user as the chapter structure according to `style`.
- If `total_paragraphs` is larger than `end_index + 1`, there is more.
  Tell the user to call it again with `--offset` set to `end_index + 1`.
- If `track_changes.has_pending_revisions` is `true`, the document has unresolved
  tracked changes (`w:ins`/`w:del`). `find_replace` of `docx-edit` cannot detect
  text inside these tracked changes, so before editing it is good to confirm with the user
  whether to resolve them with `accept_all_changes`/`reject_all_changes`.
- `body_order_count` on stdout is the number of body paragraph + table elements (the count in the output example above). The body
  `body_order` (`[{"type":"paragraph"/"table","index":N}, ...]`) shows in which order paragraphs and tables
  alternate in the document. `paragraphs`/`tables` are returned as separate flat
  lists, so without it you cannot tell "which paragraph is right before/after table 2".
  Use it when positional relationships are needed, such as adding a paragraph near a table or understanding
  how the chapter structure corresponds to the tables (like `paragraphs`/`tables`, it is included only on the `result_path` side).

### Edge cases

- A missing file or a directory ends with an error.
- If the extension is `.doc`, it returns a dedicated error message (the conversion guidance above) with
  exit code 1. Extensions other than `.docx`/`.doc` also end with an error.
- A broken `.docx` (invalid as ZIP/XML) ends with an error.
- A password-protected `.docx` cannot be opened by python-docx and ends with an error.
- If the dependency `python-docx` (import name `docx`) is not in the environment, it ends with
  `ModuleNotFoundError` and a non-zero exit code. In that case, prompt the installer to
  run `pip install python-docx`.

## To check the layout, tables and emphasis as images

If you want to know the layout, tables, image placement and emphasis that text extraction alone cannot show,
use `render_docx.py` of the `docx-render` skill + `analyze_image`.
