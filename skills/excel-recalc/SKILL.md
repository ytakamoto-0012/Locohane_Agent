---
name: excel-recalc
description: A skill that actually recalculates the formulas of xlsx/xlsm/xls files with Microsoft Excel and detects the results and error cells (#DIV/0!, etc.). Microsoft Excel must be installed locally and it must be called from an interactive session (it starts EXCEL.EXE temporarily via COM). Use it when you want to check calculation results right after writing formulas with the excel-edit skill, or to check for formula errors.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# excel-recalc

A skill that actually recalculates the formulas of xlsx/xlsm/xls and detects error cells.
Runs `recalc_excel.py`.

## Calling it

```bash
python recalc_excel.py "C:\Users\me\book.xlsx"
```
Formulas written with `edit_excel.py` of the excel-edit skill are not evaluated by openpyxl, so to check the results, run this script and then read the file again with `read_excel.py --data-only` of the excel-read skill. It starts Excel in the background, actually calculates and overwrites the file.

## Input/output types

Success = exit code 0 + one line of JSON on stdout. Failure = non-zero exit code + an error message on stderr. Pass the error text to the user as is.

## Output

`{"path":..., "recalculated": true, "errors": [{"sheet":"Sheet1","cell":"B5","value":"#DIV/0!"}]}`. If `errors` is empty, there are no formula errors. If there is even one, report the sheet and cell to the user, and fix it with `set_cell` of the excel-edit skill if needed.

## Constraints (information to tell the user before running)

- **Microsoft Excel must be installed locally and it must be called from an interactive session** (it most likely does not work when run as a server service; this project assumes a native Windows environment, so this is normally not a problem, but it cannot be used where Excel is not installed).
- It starts EXCEL.EXE temporarily. It always closes it when done, but **if the script is forcibly terminated, it may be left behind**. In that case, instead of ending it manually in Task Manager, run `python recalc_excel.py --recover-locks` (no `path` needed) (it terminates internally only those COM processes started by this session with `recalc_excel.py`/`edit_vba.py`/excel-render etc. that were left behind and are still alive; there is no way for it to involve unrelated Excel processes). Afterwards `{"recovered": [...]}` returns the list of terminated PIDs.
- Large workbooks may exceed the execution timeout (default 60 seconds). If exceeded, suggest increasing the timeout.
- Files from the internet (with the Mark of the Web) open in Protected View and may not recalculate correctly (files generated or edited with these skills are normally fine).

## Edge cases

A missing file, or an extension other than xlsx/xlsm/xls, is an error with exit code 1. Missing `pywin32` gives `ImportError` (prompt the user to run `pip install pywin32`). Excel COM errors (not installed / not activated / opened by another process, etc.) give "Recalculation with Excel failed: ..." with exit code 1 (pass it to the user as is).
