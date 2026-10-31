---
name: excel-vba-edit
description: A skill that adds, overwrites and deletes the VBA macro code of xlsm files and runs macros. Microsoft Excel must be installed locally and it must be called from an interactive session, and "Trust access to the VBA project object model" must be enabled in the Excel Trust Center. It has a safety policy of not generating code that destroys existing data/files or malicious code (deleting files, running external processes, operating the registry, etc.), and dangerous APIs are detected and refused mechanically. Use it when the user wants to write VBA macro code, fix or delete existing macros, or run macros. Use the excel-vba-read skill first to check existing code.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# excel-vba-edit

A skill that adds, overwrites and deletes the VBA macro code of xlsm and runs macros.
Runs `edit_vba.py`.

## Constraints when generating VBA code (absolutely mandatory)

Never generate code that destroys existing data/files or malicious code, for any reason. Forbidden: deleting files/folders (`Kill`/`RmDir`/`FileSystemObject.DeleteFile`/`DeleteFolder`, etc.), running external commands/processes (`Run`/`Exec` via `Shell`/`WScript.Shell`, etc.), reading/writing the registry, starting or operating other apps, downloading and running over the network, and any other system access beyond the scope of the workbook. When asked for such things, or for destructive operations whose intent is unclear, confirm the purpose or suggest handling it manually, and do not generate it. Do not fix code that got a dangerous API detection error and send it again as is without confirming the purpose.

`add_module`/`set_code`/`find_replace`/`replace_procedure`/`insert_code` **technically detect and refuse with an error** code containing `Kill`/`RmDir`/`Shell`/`WScript.Shell`/`DeleteFile`/`DeleteFolder` among the above (`_check_dangerous_code`. Complete detection of malice is impossible, so the final judgment is the LLM's own responsibility, but obviously dangerous APIs are blocked mechanically).

**A simple syntax check is also done right before writing** (`_lint_vba_syntax`; verifies with regular expressions that blocks such as Sub/End Sub, If/End If and For/Next match). On failure, nothing is written or saved and an error is returned. VBA has no "API that returns whether compilation succeeded", so **semantic** compile errors such as duplicate declarations of undeclared variables, type mismatches and typos cannot be detected by this check (passing it does not mean it is syntactically completely correct).

**When processing that overwrites/updates existing sheets, cells or workbooks is needed for a legitimate purpose** (e.g. "write the totals back", "I want to update the source data on every run"), **always include processing that makes a backup under another name right before the overwriting** (`Save`/writing `Range.Value` etc. are not detected by `_check_dangerous_code`, so the LLM follows this pattern on its own).

```vba
Sub UpdateData()
    Dim backupPath As String
    backupPath = ThisWorkbook.Path & "\" & _
        Left(ThisWorkbook.Name, InStrRev(ThisWorkbook.Name, ".") - 1) & _
        "_backup_" & Format(Now, "yyyymmdd_hhnnss") & ".xlsm"
    ThisWorkbook.SaveCopyAs backupPath

    ' The main processing that overwrites existing data starts here
End Sub
```
`ThisWorkbook.SaveCopyAs` saves the current content under another name without closing the workbook and does not affect the state of the running macro. The backup location is `ThisWorkbook.Path` by default (follow the user's instructions if any).

## Calling it and prerequisites

```bash
python edit_vba.py "C:\Users\me\book.xlsm" --ops-json '[{"op": "set_code", "name": "Module1", "code": "Sub Foo()\nEnd Sub"}]'
```
- Creating new: add `--new` at the end (`--overwrite` is also required if the file exists). It creates a true macro-enabled workbook via Excel COM, so if you also want table data and formatting, **run this skill's `--new` first**, then use the excel-edit skill without `--new` to add sheets and data (using excel-edit's `--new` first makes the content an xlsx-equivalent macro-disabled file that this skill cannot open). If you want to create just the container (a macro-enabled workbook) first without writing VBA code yet, `--ops-json`/`--ops-file` can be omitted (only with `--new`; when omitted, treated as `ops=[]`).
- Without `--new`, it opens the target path and edits it (an error if it does not exist).
- Right after `--new`, the sheet structure is **only one sheet, "Sheet1"** (the default created by Excel COM's `Workbooks.Add()`). Building `rename_sheet` etc. of the excel-edit skill on the assumption of "3 sheets, Sheet1-Sheet3" gives errors for nonexistent sheet names. If you need a second sheet or more, add them first with `add_sheet` of the excel-edit skill and then rename them.
- Omitting `--output` overwrites the target path (the output extension must be `.xlsm`).
- VBA code tends to be a multi-line string, so instead of embedding it directly in `--ops-json`, use `--ops-file <absolute path>` from the start as the basic approach (write the ops as a JSON array to a file and pass its path).

```bash
python -c "import json; json.dump([{'op': 'set_code', 'name': 'Module1', 'code': 'Sub Foo()\nEnd Sub'}], open('ops.json', 'w', encoding='utf-8'), ensure_ascii=False)"
python edit_vba.py "C:\Users\me\book.xlsm" --ops-file "C:\Users\me\ops.json"
```

### Arguments

| Argument | Required/optional | Type | Default | Description |
|---|---|---|---|---|
| `path` (positional) | Required | String (absolute path) | - | Without `--new`, the existing `.xlsm` to edit. With `--new`, the path to create (the save destination itself when `--output` is omitted) |
| `--ops-json` | Exclusive with `--ops-file`; can be omitted only with `--new` | String (JSON array) | `[]` when omitted with `--new` | Pass the ops as is as a one-line JSON string |
| `--ops-file` | Exclusive with `--ops-json`; can be omitted only with `--new` | String (absolute path) | `[]` when omitted with `--new` | Path of a JSON file with the ops |
| `--new` (flag without a value) | Optional | - | Not given = edit an existing file | Create a new macro-enabled workbook without reading an existing file |
| `--overwrite` (flag without a value) | Optional | - | Not given = refuse to overwrite | With `--new`, allow overwriting even if a file already exists at the save destination (`--output` if given, otherwise `path`) |
| `--output` | Optional | String (absolute path, `.xlsm` required) | Omitted = save to `path` | Specify only when you want to save under another name |

Prerequisites (always tell the user before running):
- **Microsoft Excel must be installed locally and it must be called from an interactive session** (the same constraint as the excel-recalc skill).
- **"Trust access to the VBA project object model" must be enabled in the Excel Trust Center** (disabled by default; it cannot be enabled automatically by a program). When it is not set, accessing `workbook.VBProject` itself fails and a message including the setup steps is returned, so guide the user with it as is (setup steps: open Excel -> File -> Options -> Trust Center -> Trust Center Settings -> Macro Settings -> check "Trust access to the VBA project object model").
- Only `.xlsm` is supported (`.xls`/`.xlsx` are not).
- **Creating and editing UserForms is out of scope** (only standard modules, class modules and document modules (`ThisWorkbook`/sheet modules) are supported).
- Only calls that include `run_macro` open the file with macro execution allowed (`msoAutomationSecurityLow`). Others (only `add_module`/`set_code`/`delete_module`) operate with automatic macro execution disabled (`msoAutomationSecurityForceDisable`), so `Workbook_Open` etc. never run unintentionally while editing.

## List of ops

| op | Required | Optional | Description |
|---|---|---|---|
| `add_module` | `name` | `code` (default `""` = empty module), `type` (default `standard`; `standard`/`class`) | Add a new standard/class module. An error if one with the same name exists |
| `set_code` | `name`,`code` | - | **Replace the whole text** of an existing module (including `ThisWorkbook` etc.) |
| `find_replace` | `name`,`old_code`,`new_code` | - | Replace part of the code (`old_code`) with `new_code` (a diff patch). An error if `old_code` is not unique (0 or several matches) |
| `replace_procedure` | `name`,`procedure`,`code` | `kind` (`sub`/`property_get`/`property_let`/`property_set`; detected automatically if omitted) | Replace only the specified Sub/Function/Property as a whole |
| `insert_code` | `name`,`code` | `position` (default `end`; `end`/`start`/a line number) | Add new code without touching the existing code |
| `delete_module` | `name` | - | Delete a module (document modules cannot be deleted; an error) |
| `run_macro` | `name` | `args` (array) | Run the specified macro. Return values, if any, go into the `results` array |

**Do not include `MsgBox`/`InputBox` in macros run with `run_macro`.** In automated runs without an interactive session nobody can close the dialog, so the whole thing hangs until the script timeout (default 300 seconds), and then the Excel process is left behind (confirmed 3 times in the same session on 2026-08-22). **Before calling `Application.Run`, `run_macro` statically checks the code of the target procedure, and refuses with an error before running if it contains `MsgBox`/`InputBox`** (so the hang itself no longer happens). If you get that error, remove `MsgBox`/`InputBox` or replace them with `Debug.Print`/a return value and send it again (do not resend the same code as a stopgap). Show progress with `Debug.Print` or a return value from the start, not `MsgBox`. Operations on shapes and form controls such as `Buttons.Add` are not covered by the static check above, but they are reported to hang easily, so avoid them (if a button is needed, write only the macro code without using `run_macro` and leave running it to the user).

**If `run_macro` still times out after 300 seconds, or you get the error "Saving finished without an exception, but the file's modification time did not change", an Excel process may have been left behind.** In that case, do not build and run `taskkill` or `psutil` yourself (letting it decide PID numbers or image name filters by itself leads to accidents involving unrelated Excel windows, so this is a technically forbidden operation; this accident actually happened on 2026-08-23). Instead, run:

```bash
python edit_vba.py --recover-locks
```

Neither `path` nor ops are needed (ignored if given). This command directly terminates, internally, only those COM processes started by this session with `edit_vba.py`/`recalc_excel.py`/excel-render, pptx-render, docx-render that were left behind and are still alive (it is designed not to take PID numbers as arguments, so there is no way for it to terminate unrelated processes by mistake). Afterwards, `{"recovered": [...]}` holds the list of terminated PIDs. After that, fix the cause of the hang, such as `MsgBox`, before running again.

**`set_code` always needs the whole text of the module.** When you want to change only part of a long module, passing the whole text makes accidents of "meant to change one part but dropped or altered unrelated parts" more likely the smaller the LLM. **For partial changes to existing modules, always prefer the diff ops** (`set_code` only for complete replacement):

| Range you want to change | op to use |
|---|---|
| A few lines to one expression or condition | `find_replace` (copy a unique range for `old_code` as is from the code obtained with the excel-vba-read skill; differences between CRLF/LF line endings are absorbed automatically) |
| The whole content of one Sub/Function/Property | `replace_procedure` (no need to pass other code outside the procedure) |
| Add a new Sub/Function without changing existing code | `insert_code` (no need to exchange the existing code) |

Output: `{"path":..., "backup_path": null, "applied_ops": 2, "results": [3]}`. `results` holds, in order of occurrence, only the results of ops with return values (such as running a Function with `run_macro`). `backup_path` is the absolute path of the timestamped copy made right before overwriting when a file already existed at the save destination (`null` if none).

## Procedure and edge cases

1. Before writing code, check the module list and existing code with `read_vba.py` of the excel-vba-read skill.
2. Changes to existing modules are basically one of `find_replace`/`replace_procedure`/`insert_code` (`set_code` only when rewriting everything).
3. Add `--output` only when you want to save under another name (if omitted, `path` is overwritten).
4. Run the command above.
5. When using `--new`, if the save destination (`--output` if given, otherwise `path`) exists and it is unclear whether overwriting is OK, run once without `--overwrite` and confirm with the user based on the error.

Edge cases: the extension is not `.xlsm` / the output extension is not `.xlsm` / ops is not a JSON array / an op without an `op` key / specifying a nonexistent module / specifying a UserForm as the `type` of `add_module` or the target of `set_code` etc. / `delete_module` on a document module / `add_module` with an existing module name / `old_code` of `find_replace` matching 0 or several places / the `procedure` of `replace_procedure` not found — all are errors with exit code 1 (the message includes which op, at which position, failed). When a dangerous API (`Kill`, etc.) is detected: "This code cannot be written because it contains a dangerous VBA API (...)" with exit code 1 (do not fix the code and resend; first confirm the purpose with the user). When the syntax check fails: "The VBA code may have invalid syntax (...)" with exit code 1 (conditional compilation such as `#If`, colon-separated statements and semantic errors are missed, but it is designed with a low risk of wrongly refusing correct code. Fix it as the message says and resend. **Even if it passes this check, remaining semantic compile errors cannot be detected by this skill**). For the Trust Center not being set, guide the user with the setup steps above as is. Missing `pywin32` gives `ImportError` (prompt the user to run `pip install pywin32`). Excel COM errors give "Editing VBA failed: ..." with exit code 1. On forced termination EXCEL.EXE may be left behind (manual termination in Task Manager is needed). When `MsgBox`/`InputBox` is detected in the procedure targeted by `run_macro`: "Running was aborted because macro '...' contains (MsgBox|InputBox)" with exit code 1 (the `Application.Run` call itself is skipped, so it does not hang. The file stays unchanged and the normal termination processing runs. Fix the code and resend).

## Prohibitions

- Generating VBA code that deletes files/folders, runs external commands/processes, reads/writes the registry, starts or operates other apps, or downloads and runs things over the network.
- Fixing code that got a dangerous API detection error and sending it again as is without confirming the purpose.
- Forcibly overwriting an existing file with `--new` and without `--overwrite` (if it errors, confirm with the user whether overwriting is OK before running again).
