# The basic contract of edit_excel.py (ops required, the behavior of --new)

Most cases of getting stuck calling `excel-edit` (`edit_excel.py`) were caused by losing sight of the fact
that it is an ops-based API and misusing it as a simple file operation tool.

## `--ops-json`/`--ops-file` are required for any use

`--ops-json` and `--ops-file` are defined with `argparse`'s `mutually_exclusive_group(required=True)`,
so **it errors immediately unless one of them is passed**
(there is no way to call it "without ops", such as creating an empty workbook with only `--new`,
or copying an existing file under another name).

In fact, there is a case where, trying to restore a backup file to the production file, it was called as

```
run_script(excel-edit, edit_excel.py, ["budget.bak_....xlsm", "--output", "budget.xlsm"])
```

omitting `--ops-json`/`--ops-file`, and failed with

```
one of the arguments --ops-json --ops-file is required
```

What is more serious is that right after this, **the same command with only `--overwrite` added was run again,
and failed again with exactly the same error**. `--overwrite` is the flag "allow overwriting with `--new`",
unrelated to the "ops required" error, so of course it did not fix it
(for the general principle of checking that your change corresponds to the cause of the error before retrying,
see [[error-message-first-retry]]).

**Do the file copy / backup restore itself with `shutil.copy2` etc. in `execute_python_code`, and then
apply the ops with `--ops-json` in `excel-edit` — split it into these 2 steps.** `edit_excel.py` is not a file copy tool.

## `--new` starts from an empty workbook with 0 sheets

The implementation with `--new` is:

```python
wb = openpyxl.Workbook()
wb.remove(wb.active)
```

In other words, **the default "Sheet1" does not exist from the start** (general openpyxl knowledge tends to make you assume
"a new workbook has a default sheet", but `--new` of `edit_excel.py` deletes it right after creating it). Because of
this assumption, there is a case where the first op of a new-creation task included

```json
{"op": "delete_sheet", "name": "Sheet1"}
```

and failed with

```
Failed to apply ops[0] (op='delete_sheet'): Sheet not found: Sheet1 (existing sheets: [])
```

**Always make the first op right after `--new` an `add_sheet`**
(do not put delete/rename etc. as the very first op).

## `--query` and `--query-json` are different (about `excel-read`, but easy to confuse)

The flag of `excel-read` is `--query-json`, not `--query`. A typo in the flag name itself is hard to notice
from the error message (it only becomes `unrecognized arguments`), and tends to be noticed only after rereading
SKILL.md, so be conscious of copying the flag name as written in SKILL.md each time, including which of
`--ops-json` (excel-edit) and `--query-json` (excel-read) you are calling.
