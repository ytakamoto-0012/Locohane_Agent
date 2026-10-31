# When you hit a request that excel-edit's ops cannot express

`add_chart` of `excel-edit` accepts only `bar`/`line`/`pie`/`scatter` for `type`,
and **does not support finer variations such as stacked bar charts or combination charts**
(note: stacked bar charts are now supported with `grouping`; combination charts are still not).
There is a real case where handling this constraint wrongly led to a failure with heavy damage.

## Bad handling: bypassing to raw openpyxl and breaking the xlsm directly

In a session asked for "a stacked bar chart + a line chart", the model correctly noticed that `type` of `add_chart`
had no stacked option, but then switched to raw openpyxl operations,
`openpyxl.load_workbook` -> `wb.save()` with `execute_python_code`, and repeated the following for more than 20 turns.

- `bar_chart.type = "stacked"` (an attribute value that does not exist in openpyxl)
- `Series(value=...)` (correctly `values=`; after that, an AttributeError when referring to `chart.type`)
- referring to `bar_chart.series[0]` on an empty list, giving `IndexError`

Furthermore, saving the `.xlsm` directly with `wb.save()` in raw openpyxl corrupted the file so that Excel COM
could not open it (`edit_excel.py` automatically adds `keep_vba=(ext == ".xlsm")` when reading `.xlsm`,
but this is not guaranteed when you write `load_workbook`/`save` in raw openpyxl yourself. **There is a risk
that the macros are lost, or that the file format breaks and it can no longer be opened, with the error
"Excel cannot open the file '...' because the file format or file extension is not valid"**).
The broken file then also triggered the "misuse of edit_excel.py" of [[edit-excel-invocation-contract]]
during the recovery work, spreading the damage further. For another real case of the same type of failure
(changing font names) and how to get out when going in circles drags on, see [[raw-openpyxl-xlsm-fallback]].

## Recommended order of actions

1. First consider **whether a similar look can be reproduced by combining existing ops**, such as
   `format_table`, `row_styles` and theme colors (even if not an exact match, there is room to confirm
   with the user whether it is acceptable).
2. If it still cannot be expressed, do not force a workaround with raw openpyxl;
   tell the user that "excel-edit does not support fine type specifications like this chart",
   and present alternatives (such as a normal bar chart instead) before proceeding (the same position as
   when cropping is not supported for `add_image`; also see the prohibitions in "Adding and adjusting images
   and charts" of the `excel-edit` SKILL.md).
3. **If the user really wants direct editing with raw openpyxl**,
   always state `load_workbook(path, keep_vba=True)` explicitly when targeting `.xlsm`,
   and take a backup (save under another name with `shutil.copy2`) before overwriting.
   After saving, check with images via `excel-render` that it can be opened, and if possible
   confirm that it can actually be opened with Excel COM etc. before reporting completion.

## General principle

**Do not treat the moment you find "the tool's ops cannot express this" as the signal to bypass
to raw scripts.** A bypass has more freedom than the regular ops, so the damage when it breaks
(file corruption, lost macros) is also larger. Telling the user about the constraint first and aligning expectations
lands faster and more safely than silently trying things for 20 turns and breaking the file.
