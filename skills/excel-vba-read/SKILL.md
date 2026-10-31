---
name: excel-vba-read
description: A skill that gets the VBA macro code (standard modules, class modules, Sub/Function) of xlsm/xls files read-only. No Excel needed (it extracts directly from the bytes with oletools, so it has looser prerequisites than edit_vba.py). Use it when the user wants to see the code of VBA macros, or to check the existing code before rewriting it with the excel-vba-edit skill.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# excel-vba-read

A skill that gets the VBA macro code of xlsm/xls read-only.
Runs `read_vba.py`.

## Calling it

```bash
python read_vba.py "C:\Users\me\book.xlsm" --module Module1
```
Omitting `--module` gives the module list mode. It extracts directly from the bytes with `oletools` (`olevba`), so **no Excel or COM is needed** (looser prerequisites, in contrast to the excel-vba-edit skill).

## Input/output types

Success = exit code 0 + one line of JSON on stdout. Failure = non-zero exit code + an error message on stderr. Pass the error text to the user as is. A missing library gives `ImportError` with exit code 1 (cause: `oletools`) -> prompt the user to run `pip install oletools`.

This script does not print the body (module list / full code) to stdout; it writes it to a temporary JSON file and returns `result_path`. Read `result_path`. The list mode gives `modules_count` and `result_path` (its content is `modules` = elements `{"name","type","line_count"}`). With `--module`, it gives `code_length` and `result_path` (content `code` = the full source).

## Procedure

1. Check the list without `--module`.
2. Get the full text of the target module with `--module`.
3. Always check the existing code with this before rewriting it with the excel-vba-edit skill (`set_code` replaces the whole text).

## Edge cases

Targets are xlsm/xls (`.xlsx` errors immediately). Files without a VBA project end normally without an error. stdout has the same shape as in other cases (`modules` is converted to `modules_count: 0`, and `result_path` is included): `{"path":..., "has_vba": false, "modules_count": 0, "result_path": "..."}`. The content of `result_path` (raw JSON) keeps `modules: []`. The module type (`standard`/`class`/`document`/`form`) is a **simple guess** from name patterns and code content and is not guaranteed (if the exact type is needed, guide the user to check in Excel's VBE (Alt+F11)). Specifying a module name that does not exist is an error (with the list of existing ones). UserForms are listed as `type:"form"` and can be read, but are not editable with the excel-vba-edit skill.
