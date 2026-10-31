# Before retrying, first read the error message itself

What the failure logs of the excel skills (`excel-edit`/`excel-read`/`excel-render`/`excel-recalc`)
had in common was the pattern of **moving on to the next guess without reading the error message,
even though the answer (or a clue to it) was written right there**. Individual cases are summarized in
[[read-render-args-and-queries]] and [[edit-excel-invocation-contract]]; here is the common way of thinking.

## How to read each type of error message

- `Unsupported query: 'xxx' (supported ops: [...])` ->
  the list of supported ops is itself the answer. Choose the next op from the list. Features not in the list
  simply do not exist, so if a new feature is needed, tell the user.
- `usage: script.py [-h] ...` (`unrecognized arguments`) ->
  the `usage:` line is **all** of that script's arguments. Check whether you brought in
  the argument scheme of another skill.
- `Failed to apply ops[N] (op='xxx'): <details of the Python exception>` ->
  it states which op, at which position, failed with what exception.
  Fix the arguments of op N (the `sheet` name, `range` notation, types, etc.) against that exception text
  (the exception text itself is the hint: `KeyError` means a typo in a key name,
  `ValueError` means a value in the wrong format, etc.).
- `one of the arguments --xxx --yyy is required` ->
  that command **always** requires either `--xxx` or `--yyy`.
  Do not assume an exception such as "this time should be a special call that does not use ops".

## If you retry, ask yourself whether your change corresponds to the cause of the error

Accidents have actually happened where the same command was rerun with only arguments unrelated to the error changed,
and failed again with the same error (an example reproduced the "ops required" error just by adding
`--overwrite`; see [[edit-excel-invocation-contract]]).
Right before retrying, check whether "the part you changed this time" matches "the part the previous error message
pointed out". If not, it is a retry that only changed the appearance, and the failure is likely
to repeat.

## If you get the same kind of error twice in a row, recheck the premises with read_skill

Before repeating guesswork tweaks a third and fourth time, rereading the SKILL.md of the relevant skill
(the list of ops, the argument table, the query section) once with `read_skill` gets you to the answer
faster in the end. In the actual logs too, there is a case where, after guessing twice,
rereading SKILL.md solved it immediately.
