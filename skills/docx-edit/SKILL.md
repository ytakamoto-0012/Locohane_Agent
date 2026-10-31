---
name: docx-edit
description: A skill that edits existing Word documents (.docx). It can find and replace strings, append and delete paragraphs, insert images at any paragraph position and resize existing images, find and replace with Track Changes (revisions), and accept or reject all tracked changes at once. Only when the user explicitly asks to change the look, set_paragraph_style/set_table_style, which change the colors, text decoration, alignment (left/right/center, etc.), indentation and line spacing of existing paragraphs and tables, can also be used. Use it when you want to fix or add wording in an existing Word file, propose fixes with tracked changes (review), or insert images into an existing document. Use `docx-create` for creating new ones, and `docx-read` to check the content before editing.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# docx-edit

A skill that reads an existing `.docx` file, applies `ops` (an array of operations) in order and saves it.
Run `edit_docx.py` to get the result.
For creating new documents, use `docx-create` instead of this skill (`edit_docx.py`
is for existing files only and has no `--new`).

On success it prints one line of JSON to stdout with exit code 0; on failure it prints an
error message to stderr with a non-zero exit code.

Call example:
```bash
python edit_docx.py "C:\Users\me\report.docx" --ops-json '[{"op": "find_replace", "old_text": "Company A", "new_text": "Company B"}]'
```
The value of `--ops-json` is a JSON array following "Format of ops" below, as a one-line string.
When the JSON gets long, use `<absolute path of the .docx to edit> --ops-file <absolute path of a file with the JSON array>`
instead of `--ops-json` (always specify exactly one of `--ops-json`/`--ops-file`). To save under another name,
add `--output <output path>` (if omitted, the file being edited is overwritten).

## Procedure (recommended flow)

1. Before editing, check the paragraph indexes of the target document and
   `track_changes.has_pending_revisions` with `read_docx.py` of the `docx-read` skill.
2. If `has_pending_revisions` is `true`, tell the user that if you continue editing as is,
   `find_replace` may not be able to detect text inside the existing tracked changes,
   and if needed confirm whether to resolve them with `accept_all_changes`/`reject_all_changes`
   before editing.
3. Build the `ops` array and run `edit_docx.py`.
4. If `--output` is omitted, the file being edited is overwritten, so confirm with the user beforehand.

## Format of ops

The top level is the `ops` array itself (several ops can be put together in one call).

```json
[
  {"op": "find_replace", "old_text": "Company A", "new_text": "Company B", "track_changes": true, "author": "Taro Yamada"},
  {"op": "find_replace", "old_text": "Old price: 1000 yen", "new_text": "New price: 1200 yen", "paragraph_index": 12, "occurrence": "first"},
  {"op": "append_block", "block": {"type": "heading", "text": "4. Additional notes", "level": 1}},
  {"op": "append_block", "block": {"type": "paragraph", "text": "This note was added on July 13, 2026."}},
  {"op": "delete_paragraph", "index": 25},
  {"op": "accept_all_changes"},
  {"op": "reject_all_changes"}
]
```

| op | Required keys | Main optional keys | Description |
|---|---|---|---|
| `find_replace` | `old_text` | `new_text` (default `""`), `track_changes` (default `false`), `paragraph_index` (default = all paragraphs), `occurrence` (`"all"` default / `"first"`), `author`, `date` | Find and replace text within paragraphs |
| `append_block` | `block` (same format as the `blocks` elements of `docx-create`) | `theme` (used only for `callout`; default `charcoal`) | Add a block at the end of the document (always untracked) |
| `insert_image` | `image_path`, one of `before_index`/`after_index` | `width_cm`/`height_cm` (original size if omitted; specifying only one keeps the aspect ratio) | Create a new paragraph right before/after the specified paragraph and insert an image (see below) |
| `set_image_size` | `image_index` | `width_cm`/`height_cm` (at least one required; specifying only one keeps the aspect ratio) | Resize an existing image (see below) |
| `set_paragraph_style` | `index` | `role` (`heading`/`callout`) + `theme`, or `color`/`bold`/`italic`/`underline`/`font`/`size_pt`/`fill_color`/`border_color`/`align` (`left`/`center`/`right`/`justify`)/`indent_left_cm`/`line_spacing` | Explicitly recolor and reposition an existing paragraph (see below) |
| `set_table_style` | `table_index` | `theme`, or `header_fill`/`header_font_color`, `row`/`all_rows` | Explicitly recolor rows of an existing table (by default the header row = row 1) (see below) |
| `delete_paragraph` | `index` | None | Delete the paragraph at the specified index (untracked only) |
| `accept_all_changes` | None | None | Accept all `w:ins`/`w:del` in the document |
| `reject_all_changes` | None | None | Reject all `w:ins`/`w:del` in the document (revert them) |

The `block` of `append_block` can use the same formats as the elements of the `blocks` array of `docx-create`
(`heading`/`paragraph`/`bullet_list`/`number_list`/`table`/`image`/`callout`/
`page_break`). **But unlike `docx-create`, `heading`/`table` are not colored
even if `theme` is given** (to respect as is the colors of the Word built-in styles
the document itself has). Since the content you append to an existing document
already has a look established by the document (heading styles, etc.), not overwriting it is the default behavior.
Only `callout` is a new concept with no corresponding Word style, so it is colored with `theme`
(`charcoal`/`navy`/`forest`/`coral`/`terracotta`/`ocean`/`teal`/`berry`,
`charcoal` if omitted).

## set_paragraph_style / set_table_style (explicitly changing the existing design and placement)

`find_replace`/`append_block`/`delete_paragraph` handle only the text content and never touch
the look. **Only when the user explicitly asks to change the look or placement**, such as "make it look cooler", "easier to read",
"center it" or "add an indent", recolor and reposition existing paragraphs and tables
with these 2 ops. Do not call them proactively when not asked.

These ops cover most "fix the layout" requests: text color,
background color, left border color, bold/italic/underline, font, font size, text alignment,
left indent, line spacing (`set_paragraph_style`), and recoloring any row of a table
(`set_table_style`). For requests they still cannot handle (column widths, page margins/paper size,
borders of the whole table, applying to several paragraphs at once), see "Not supported" at the end of this section
and tell the user it cannot be done.

```json
[
  {"op": "set_paragraph_style", "index": 3, "role": "heading", "theme": "navy"},
  {"op": "set_paragraph_style", "index": 5, "role": "callout", "theme": "navy"},
  {"op": "set_paragraph_style", "index": 7, "align": "center"},
  {"op": "set_paragraph_style", "index": 8, "indent_left_cm": 1.0, "line_spacing": 1.5},
  {"op": "set_table_style", "table_index": 0, "theme": "navy"},
  {"op": "set_table_style", "table_index": 0, "theme": "navy", "all_rows": true}
]
```

- `set_paragraph_style`: targets `index` (the 0-based index of `doc.paragraphs`, counted the same way as
  `paragraph_index` of `find_replace`), and recolors/repositions it with `role` (`"heading"` = text color in `theme`'s
  primary color + bold / `"callout"` = background filled with `theme`'s secondary color and a left border in the primary color)
  + `theme`, or individual keys (`color`/`bold`/`italic`/`underline`/`font`/`size_pt`/
  `fill_color`/`border_color`/`align`/`indent_left_cm`/`line_spacing`). Individual keys take precedence over the defaults
  from `role`. **One call can target only one paragraph**, so to apply the same change to several paragraphs,
  list several ops.
- `set_table_style`: targets the table at `table_index` (the 0-based index of `doc.tables`), and recolors it with
  `theme` or `header_fill`/`header_font_color`. By default **only the header row
  (row 1)** (bold + the heading font are also applied). Specifying `row` (one 0-based row number)
  or `all_rows: true` changes the target rows (in this case only fill and text color;
  bold is not forced).
- For both, a call with no `theme`/style keys at all is an error.

**Not supported (what the current ops cannot do)**: column widths and row heights, page margins, paper size and
orientation, borders of the table body (only shading of header cells is supported), header/footer,
applying a style to several paragraphs at once (1 paragraph per op). Images are Word inline objects,
so free placement with x/y coordinates is not supported (`insert_image` can only specify which paragraph to insert at;
see below).

## insert_image / set_image_size (inserting and resizing images)

```json
[
  {"op": "insert_image", "image_path": "C:\\img\\photo.png", "before_index": 10, "width_cm": 12.0},
  {"op": "set_image_size", "image_index": 0, "width_cm": 8.0}
]
```

- `insert_image`: specify **only one** of `before_index` (insert right before the specified paragraph) or `after_index`
  (right after the specified paragraph; specifying the last paragraph of the document appends at the end).
  Omitting both `width_cm`/`height_cm` gives the image's original size; specifying only one
  calculates the other automatically keeping the aspect ratio. If you just want to add an image at the end of the document,
  the `image` block of `append_block` is simpler (you cannot specify the insertion position, but it takes one op).
- The new paragraph created by `insert_image` is not included in the paragraph numbers (the numbers `docx-read` showed)
  on which `before_index`/`after_index` of this batch are resolved,
  so later ops in the same batch cannot refer to the new paragraph directly (to refer to it next,
  call `docx-read` again once).
- `set_image_size`: `image_index` is the 0-based sequence number of `doc.inline_shapes`
  (the order in which images appear in the whole document). It exactly matches the `image_index` of
  `inline_images` returned by `docx-read`. If only one of `width_cm`/`height_cm`
  is specified, the other is calculated automatically keeping the aspect ratio.
- **Cropping images is not supported.** python-docx has no stable API for cropping images,
  and forcing an implementation risks corrupting the file, so it is
  intentionally not supported. If the user asks for cropping,
  tell them "docx-edit does not support cropping images" and instead guide them to
  prepare a cropped image file separately.

## Constraints of find_replace (important)

- `old_text` must fit within "one run". In Word documents,
  one paragraph is often split into several runs (chunks of text with different formatting), and
  the middle of a string with mixed formatting, or strings spanning several runs, cannot be detected.
  If it cannot be detected it is an error, so shorten `old_text`, or check the paragraph text with `read_docx.py`
  and adjust the search range.
- Text inside existing unresolved Track Changes (`w:ins`/`w:del`) is not searched.
- Runs containing tabs, line breaks or fields are not searched.
- Unless `occurrence: "first"` is specified, all matches in the target paragraphs are
  replaced.

## About Track Changes

- `track_changes: true` takes effect only for `find_replace`. `append_block`/
  `delete_paragraph` are always untracked (written as accepted changes).
- If `author`/`date` are omitted, `author="AI Agent"` and `date` is the execution time
  (UTC, ISO8601).
- The generated tracked changes can be shown and individually accepted/rejected on the "Review" tab of Microsoft Word.
  To accept/reject them all at once, use `accept_all_changes`/
  `reject_all_changes`.
- `accept_all_changes`/`reject_all_changes` also cover tracked changes inside tables
  (they scan everything under the document body recursively). Headers/footers are not covered.
- Tracked changes per table row/column, paragraph property changes, formatting-only changes and moves
  (`moveFrom`/`moveTo`) are not supported. These can only occur when opening documents containing tracked changes
  added by Word itself; the tracked changes this skill itself generates
  are always only per-run `w:ins`/`w:del`, so they are not affected.

## About deleting paragraph marks (merging paragraphs)

`delete_paragraph` does not support `track_changes` (deleting the paragraph mark itself
and merging the paragraphs before and after it are complex in OOXML, so it is not supported). If you want to
"delete" a paragraph as a tracked change, instead of deleting the paragraph itself, replace the whole text of the paragraph
with an empty string using `find_replace` (`track_changes: true`) as an alternative
(the paragraph mark remains, but in Word it is shown as a deletion with strikethrough).

`index`/`paragraph_index` always refer to "the paragraph number at the time this edit_docx.py call started (= the state
the preceding docx-read showed)". Even if one `ops` contains several `delete_paragraph`,
the tool tracks which paragraphs survive automatically for the `index`/`paragraph_index` of all later ops,
so **the order of deletion does not matter** (in whatever order you specify them, the numbers you saw with docx-read
can be used as is). Referring later to a paragraph number deleted after the batch started is
an error.

## Output example

```json
{
  "path": "C:\\foo\\report.docx",
  "backup_path": "C:\\foo\\report.bak_20260812_154200.docx",
  "paragraph_count": 44,
  "table_count": 2,
  "applied_ops": 3,
  "op_results": [
    {"op": "find_replace", "replaced_count": 1},
    {"op": "append_block", "block_type": "heading"},
    {"op": "delete_paragraph", "deleted_index": 25}
  ]
}
```
The `op_results` elements of `accept_all_changes`/`reject_all_changes` are respectively
`{"op": "accept_all_changes", "accepted_ins": N, "accepted_del": N}` /
`{"op": "reject_all_changes", "rejected_ins": N, "rejected_del": N}` (counts are tallied separately for ins/del).

**`backup_path` (automatic backup)**: right before saving, if the file to be overwritten already
exists, it is copied with a timestamp to the same folder and then overwritten at the OS level
(when saving under another name with `--output`, what is copied is the existing file on the `--output` side).
If there was nothing to back up (saving to a new path, etc.),
`backup_path` is `null`. Use this path if the caller needs cleanup etc.

## Edge cases

- A missing target file, a directory, a `.doc` extension, a non-`.docx` extension or
  a broken `.docx` give an error message + exit code 1, the same as `read_docx.py`.
- `ops` not being a JSON array, an element without an `op` key, `old_text` not found,
  a nonexistent `index`/`paragraph_index`/`table_index`/`row` of `set_table_style`,
  an unsupported `op`/`block.type`/`theme`/`role`/`align`,
  `set_paragraph_style`/`set_table_style` with no style keys at all,
  `track_changes: true` on `delete_paragraph`,
  a missing `image_path` for `insert_image`, both or neither of `before_index`/`after_index`,
  a nonexistent `image_index` for `set_image_size` or omitting both
  `width_cm`/`height_cm` —
  all give an error message + exit code 1. The message includes which op (which position, which kind) failed,
  so report it as is, or fix the op and run again.
- If some op fails, nothing is saved, including the ops that succeeded before it
  (the original file stays intact rather than being left partially applied).
- If `--output` is omitted, the file being edited is overwritten. Specify `--output` only when you want to
  save it as a separate file.
- If the dependency `python-docx` (import name `docx`) is not in the environment, it ends with
  `ModuleNotFoundError` and a non-zero exit code. In that case, prompt the installer to
  run `pip install python-docx`.

## Checking after editing

To check the layout of the edited result as images, use `render_docx.py` of the `docx-render` skill
+ `analyze_image`.
