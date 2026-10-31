---
name: excel-knowledge
description: A skill that refers to a local knowledge base (notes under references/), accumulated from real work logs, on coding practices, standard techniques and common pitfalls when creating, editing and checking xlsx/xlsm files with the excel-edit/excel-read/excel-render/excel-recalc skills. Use it not for requests themselves such as "I want to make a table" or "I want to fix an existing xlsx/xlsm", but when you get stuck or start going in circles on the tool calls themselves while calling skills such as excel-edit, for example "an argument error occurred", "the same error keeps repeating", "I don't know how to build the ops", "how should I handle a request that excel-edit's ops cannot express", "I was told the chart type is not supported" or "the file got corrupted and cannot be opened". VBA macros and their errors are handled by excel-vba-knowledge (for excel-vba-read/excel-vba-edit). Writing a new standalone Python script for Excel operations (openpyxl, etc.) at the user's request is also out of scope.
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# excel-knowledge

A local knowledge base summarizing common pitfalls when calling the `excel-edit`/`excel-read`/`excel-render`/`excel-recalc` skills,
which handle xlsx/xlsm.
It has no scripts; it is a skill where you only read the notes under `references/`
with `read_skill_file`.

## How to use

1. From the "Index" below in the body of this SKILL.md (already read with `read_skill`),
   choose the topics close to the question. Several may apply.
2. Read the matching notes with `read_skill_file`. **Always include the skill folder name
   at the start of `relative_path`, like `excel-knowledge/references/<file name>`**
   (omitting the skill folder name, like `references/edit-excel-invocation-contract.md`,
   makes it not found).
3. If a note has a reference like `[[other note name]]`, read that one too if needed
   (e.g. `[[error-message-first-retry]]` points to
   `references/error-message-first-retry.md`).
4. Do not copy and paste the content of notes as is; apply it to the op, arguments and error message
   you are actually trying to call, and present it as a concrete fix.
5. If no topic in the index applies, or reading the notes does not solve it,
   tell the user so, then reread each skill's own SKILL.md with `read_skill`
   or consider investigating with the `web-search` skill.
6. The work of actually reading and rewriting xlsx/xlsm belongs to the `excel-edit`/`excel-read`/
   `excel-render`/`excel-recalc` skills. Use this skill for research: "checking the correct form of arguments and ops
   before calling" and "getting an idea of the cause when an error occurs".

## Index

| Topic | File | Content |
|---|---|---|
| Arguments and queries of excel-read/excel-render | `excel-knowledge/references/read-render-args-and-queries.md` | The arguments of `read_excel.py`/`render_excel.py` are exactly those in SKILL.md; the ops supported by `--query-json` are only `group_by`/`list_images`/`list_charts`/`print_area`; do not invent nonexistent arguments or op names by guesswork |
| The calling contract of edit_excel.py | `excel-knowledge/references/edit-excel-invocation-contract.md` | `--ops-json`/`--ops-file` are required for any use (it cannot be used as a copy/restore tool); `--new` starts from an empty workbook with 0 sheets (Sheet1 does not exist); confusing `--query` and `--query-json` |
| Requests excel-edit's ops cannot express | `excel-knowledge/references/beyond-excel-edit-capabilities.md` | What to do when you hit a type that `add_chart` does not support, such as stacked charts. A real case where bypassing to raw openpyxl without permission corrupted an xlsm, `keep_vba`, the recommended order of actions |
| When relying on raw openpyxl to change font names | `excel-knowledge/references/raw-openpyxl-xlsm-fallback.md` | excel-edit has no font name option; copy() the Font and change only .name (assigning a new Font loses bold/size/color); a render_excel failure does not necessarily mean corruption; dealing with going in circles by repeating the same hypothesis without running it |
| Retrying after reading the error message | `excel-knowledge/references/error-message-first-retry.md` | How to read each type of error message; before retrying, check that your change corresponds to the cause of the error; if the same kind of error occurs twice in a row, reread SKILL.md with read_skill |
| Tool permission boundaries | `excel-knowledge/references/tool-permission-boundaries.md` | Do not try to simulate running write scripts outside your permissions in code, e.g. from read-only sub-agents |

## Adding and updating notes (for developers)

The `references/` of this skill are meant to be added to **in advance by developers (humans, or the Claude Code
that maintains this project)**, based on failure patterns found in real work logs
and knowledge gained from web research.

Locohane's runtime LLM (the main app that uses this skill) is under a sandbox constraint where
the write destination of script execution tools is limited to the working directory,
so it cannot write under `skills/` (this skill's own folder) during a chat. So **there is no feature
that automatically writes new knowledge gained during a chat back into this knowledge base**. When useful new knowledge comes up in a conversation,
confirm with the user or developer "may I add this know-how to `excel-knowledge`?",
and add the file later (from outside the app).

Steps to add a new note:

1. Create `references/<short English slug for the topic>.md` and write it in the same
   Markdown format as the existing notes (headings, code examples, `[[name]]` links to related notes).
2. Add one row to the "Index" table above (topic name, file path, summary of the content).
3. No app restart is needed (`read_skill_file` reads the file each time).
   But notes not in the index cannot be found by the LLM,
   so always add the index entry at the same time.
