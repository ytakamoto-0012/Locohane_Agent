# Do not invent arguments or queries for excel-read/excel-render by guesswork

For `excel-read` (`read_excel.py`) and `excel-render` (`render_excel.py`), the arguments and query types
written in their respective `SKILL.md` are **all there is**; no other flags or op names
exist. In the actual logs, the most frequent accident was making up arguments and op names by guesswork
based on the assumption "there must be such a feature", without checking this fact, and repeating the same error
many times.

## These are all the arguments of read_excel.py

```
read_excel.py <file_path> [--sheet <name or 0-based index>]
              [--offset <N>] [--limit <N>] [--data-only] [--style]
              [--query-json '<...>'] [--columns 'A,C,E']
```

- If `--sheet` is omitted, only the sheet list is returned (values cannot be read).
- If you just want to read a narrowed range of rows, `--offset`/`--limit` are enough.
  **There is no extra flag like `--mode`.**
- Mistakes of passing sheet names or mode names lined up as **positional arguments** also happened
  (e.g. `script_args: ["book.xlsm", "Transactions", "all"]`). The only
  positional argument of `read_excel.py` is `file_path`; sheets are always specified with `--sheet`.
- The default is the lightweight mode (only value/number_format/merged_cells/tables). Add `--style` only when you need
  full style information such as bold, background color and borders (for the exact argument list and output format,
  see the `read_excel` SKILL.md; this is only the gist).
- To narrow down columns, use `--columns "A,C,E"` (comma-separated column letters / 1-based column numbers,
  `--sheet` required). The `columns` key of the result is the array of column letters included in `rows` this time,
  and the `j`-th element of each row of `rows` is always the value of column `columns[j]`.
  For tables with many columns, narrowing to only the columns you need is safer than reading all columns at once.

## The ops `--query-json` supports are only these 4: `group_by`/`list_images`/`list_charts`/`print_area`

```
_QUERY_HANDLERS = {"group_by": ..., "list_images": ..., "list_charts": ..., "print_area": ...}
```

Real cases happened independently in several sessions where, from natural ideas such as wanting to read a cell range at once,
wanting to know column widths, or wanting to filter by a condition, **nonexistent op names** such as
`get_rows`/`get_column_width`/`range` were sent by guesswork, receiving the error below.

```
Unsupported query: 'get_rows' (supported ops: ['group_by', 'list_charts', 'list_images', 'print_area'])
```

Even though this error message itself contains the list of supported ops, there is a case where the next attempt
switched to yet another made-up op name (and even a made-up flag scheme,
`--mode rows --start-row 130 --end-row 142`) and failed again, only noticing after redoing `read_skill`
on the third attempt. **When you receive an "unsupported op" error, do not guess the next op again;
immediately reread the query section of SKILL.md with `read_skill`.** If you just want to know
the values of a cell range, the normal mode's `--offset`/`--limit` can be used instead of `--query-json`
(other than `group_by`, used to check anchors for `insert_row_group` of [[edit-excel-invocation-contract]],
`list_charts` for checking charts, and `print_area` for checking print areas,
the normal mode is basically enough).

## The only arguments of render_excel.py are `excel_path` and `--print-as-is`

```
render_excel.py <excel_path> [--print-as-is]
```

There are no options to specify sheets, pages or partial output (a simple spec that always renders all sheets
whole). `--print-as-is` is an option that stops the forced fit of the print settings,
not something that selects sheets or the output range. Nevertheless, there is a real case where
`--sheet`/`--pages` were added, giving the following error.

```
usage: render_excel.py [-h] [--print-as-is] excel_path
```

The cause seems to be inferring, dragged along by `read_excel.py` having `--sheet`, that another script must
have the same argument scheme. **Do not apply one skill's argument scheme to another skill by analogy.**
Even if you want to check the look of only a specific sheet, operate on the premise that
`render_excel.py` can only output all sheets.

## The `usage:` line and the "supported ops" list in errors are the answer as is

The `usage:` line that accompanies argparse's `unrecognized arguments` error, and the "supported ops" list of
`--query-json`, are both **the only correct answer** you can check on the spot.
Before retrying, stop once, read them, and rebuild with the correct arguments and op names.
(For the general principle, see [[error-message-first-retry]])
