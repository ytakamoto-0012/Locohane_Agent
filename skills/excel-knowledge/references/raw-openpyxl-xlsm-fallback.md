# A safe procedure when relying on raw openpyxl to change font names

Another real case of the same type of failure as the "stacked chart" in [[beyond-excel-edit-capabilities]]
(a request `excel-edit`'s ops cannot express -> bypassing to raw openpyxl -> a broken `.xlsm`).
This summarizes the additional pitfalls specific to this type, observed in an actual work log
(a request to unify the fonts of an `.xlsm` to "Yu Gothic"), and how to behave so as not to prolong
going in circles.

## Premise: excel-edit's style schema has no font name (font family)

The `style` dict of `excel-edit` (`edit_excel.py`) supports only `bold`/`italic`/`font_color`/
`font_size`/`fill_color`/`number_format`/`align`/`valign`/`wrap_text`/
`border`/`role`, and **there is no key that changes the font name**
(confirmed in `build_font()` of the internal implementation `excel-edit/scripts/_style.py`).
"Unify the font to Yu Gothic" cannot be achieved with ops alone. First follow
"Recommended order of actions" of [[beyond-excel-edit-capabilities]] (tell the user about the constraint first
-> if really necessary, raw openpyxl on the premise of a backup).

## Additional cautions needed when changing only the font name with raw openpyxl

If you proceed to raw openpyxl, in addition to the basic steps for `.xlsm` (`keep_vba=True`, a backup before saving,
checking with render), there is one more **trap specific to font changes**.

```python
from copy import copy
import openpyxl

wb = openpyxl.load_workbook(src, keep_vba=True)

for ws in wb.worksheets:
    for row in ws.iter_rows():
        for cell in row:
            if cell.font is None:
                continue
            # copy() the existing Font and then change only name.
            # Assigning a new Font directly, like cell.font = Font(name="..."),
            # resets bold/size/color etc. that you did not specify back to the defaults,
            # unintentionally erasing the existing formatting (bold, color coding, etc.)
            # (the reason the openpyxl documentation recommends using copy()).
            new_font = copy(cell.font)
            new_font.name = "Yu Gothic"
            cell.font = new_font
```

`Font` objects are immutable per cell and cannot be partially updated
(direct attribute assignment like `cell.font.name = "..."` does not work). Array processing that rewrites
all cells at once (an idea like `set_range` of `excel-edit`) cannot be used for `Font`,
so if there are many target cells, it is good to tell the user beforehand that it takes time.

## A render_excel failure does not necessarily mean "corruption"

In the observed session, `render_excel.py` of `excel-render` repeated failure -> success -> failure -> success
on the same `.xlsm` file, **even though its content had not been changed**
(an error from Excel COM: "Excel cannot open the file '...' because the file format or
file extension is not valid." `-2147352567`/`0x800A03EC`).
The file itself could be reread with openpyxl without problems, so it is likely **a transient phenomenon when trying to open it
with Excel COM automation right after saving**.

**If `render_excel.py` suddenly fails on a file that had been succeeding until just before,
do not immediately conclude that "the file got corrupted".** Run `render_excel.py` once more
without changing the content. Suspect actual corruption only if it keeps failing. Digging deep, such as comparing
the sizes of the zip's internal XML byte by byte, is inefficient for low-spec models (in the actual session too,
it reached no conclusion and wasted time); it is faster to first check only 2 points:
"can it be reread with openpyxl" and "does render pass once more".

## When you start repeating the same hypotheses without running anything, treat it as a sign of going in circles

In the observed session, after bypassing to raw openpyxl contrary to the lesson of [[beyond-excel-edit-capabilities]],
more than an hour and more than 70 turns were consumed **going back again and again to hypotheses once rejected**:
"the VBA project broke" -> "no, there wasn't one" -> "maybe a content-type mismatch" -> "maybe the zip structure
is broken" -> "maybe saving via a temporary file fixes it" -> "maybe it is VBA-related after all", and finally
it was force-stopped by Locohane's own loop detection mechanism. A state of "only thinking circulates without running anything"
continued, where it declared "let's try a simple method" and then returned to considering another hypothesis without running it.

**When you start going back and forth between the same 2-3 options without running them, treat that itself as
a sign of going in circles.** As soon as you notice, actually run **just once** the simplest of the options
you were just considering, and report the result to the user as is, whether it succeeds or fails.
**There is no need to "fully identify the cause before reporting".** A report like
"There is a constraint ..., and I tried A/B/C, but could not identify the root cause in any of them.
The file is currently (fine / restored from the backup). Please tell me what to do next" is more useful
than continuing self-verification forever. Especially in areas like openpyxl/Excel COM, where low-spec models are bad at
diagnosing the internal XML structure by guesswork, handing the decision to the user (a human) early is faster overall
than continuing to dig.

## A clue if it recurs: even with the xlsm extension, there may be no VBA

The target file originally had no `xl/vbaProject.bin` (it had the `.xlsm` extension but was effectively a workbook
without VBA macros). Do not assume "it is `.xlsm`, so it surely contains VBA"; you can check whether it actually exists
by whether `zipfile.ZipFile(path).namelist()` includes `xl/vbaProject.bin`. With only structural changes via
`excel-edit` (inserting rows, applying formatting, etc.), `render_excel.py` succeeded stably, and failures
started to stand out around the time the font name change with raw openpyxl was attempted.
Recording "which route you saved with last" each time speeds up isolating the cause of failures.
