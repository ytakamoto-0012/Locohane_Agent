---
name: skill-creator
description: A meta-skill for creating your own draft skills (unverified skills), making improved versions of existing skills, rewriting their body, and checking their content. Whenever there is a request to create, improve or check a skill itself, such as "I want to make a new skill", "make a skill", "I want to fix this skill", "check my skill" or "show me my drafts", always use it even if the user does not say "skill-creator".
license: MIT
metadata:
  author: ytakamoto
  version: "3.0"
---

# skill-creator

A skill for making a small draft skill, checking its content, and handing it to a skill developer.
A draft is a starting point. Evaluation with the local LLM and polishing are done later by the skill developer in the Skill Lab, not here.

## Start small (most important)

- The first version is SKILL.md only, about 30 lines or fewer: 1-2 sentences of purpose, 3-7 steps, and a few notes.
- Do not make scripts/ or references/ in the first version. Most skills only need instructions; the assistant's usual tools (reading files, Excel, search, etc.) do the work.
- Make a script only when the work cannot be done with the usual tools at all, or the user asked for one.
- Write only what the user asked for. Do not add background, many examples, or rules for cases nobody mentioned.
- When fixing, change only the point in question. Prefer rewriting or removing a line over adding new ones.

## Always follow

- Create only **drafts**. Do not rewrite official skills (skills/ etc.). To fix an existing skill, make a draft of the improvement with `fork_skill.py`.
- Drafts appear in the list as `username/skill name` only in the creator's conversations (from the message after creating or deleting). Other people cannot see them.
- Making a skill official is the skill developer's job. When done, say "ask a skill developer to make it official" (the developer evaluates and finishes it in the Skill Lab of the admin tool. Drafts that become official disappear from the list).
- Do not run evaluations here. Checking the content is enough for a draft.
- You can run this skill's scripts directly yourself without plan approval.

## Flow

1. Confirm what the skill does, for what kind of messages it is used, and the shape of the output.
2. Create the draft (`scaffold_skill.py` for new ones, `fork_skill.py` for improving an existing skill).
3. Write a short SKILL.md with `write_draft_file.py` (see "Start small").
4. Check it (see "3. Check the content").
5. Show the user the SKILL.md and say "ask a skill developer to make it official".

## 1. Create the draft

New (add `--with-script` only when a script is really needed, see "Start small"):
```
python scaffold_skill.py --name my-skill --description "What it does. For what kind of messages it is used."
```

An improvement of an existing skill (the official skill stays as is):
```
python fork_skill.py --name excel-read
```

List of your drafts:
```
python list_drafts.py
```

## 2. Write the content

For your own drafts, `--name` can be just the skill name.

Write a whole file:
```
python write_draft_file.py --name my-skill --path SKILL.md --content "---
name: my-skill
description: ...
---

# my-skill
..."
```

Replace only part (`--old` must match exactly one place in the file):
```
python write_draft_file.py --name my-skill --path SKILL.md --old "old text" --new "new text"
```

Write a script, only when needed (copy long content from a file in the working folder):
```
python write_draft_file.py --name my-skill --path scripts/run.py --content-file <file in the working folder>
```

Delete files or the draft:
```
python delete_draft.py --name my-skill --path references/old.md
python delete_draft.py --name my-skill
```

How to write:
- The description is the only trigger clue. Write concretely "what it does" and "for what kind of messages it is used".
- The body is read by a small local LLM: short sentences, one rule per line, steps as a numbered list.
- If you write a script: show the call as `python <script>.py <args...>` and what the output keys mean. On success it prints one line of JSON with exit code 0; on failure, the reason to stderr with exit code 1.

## 3. Check the content

```
python validate_skill.py --name my-skill
```

- Fix it if `valid` is false (`error` says why).
- If `warnings` say it is long or has scripts, shorten it unless the user asked for that.
- Read the SKILL.md once more and check:
  - The description says what it does and when to use it.
  - The steps do what the user asked, in order, with nothing extra.
  - Nothing is written twice and no step contradicts another.

For drafts sent back by a skill developer, read `returned_reason` from `list_drafts.py` and fix only that. Fixing automatically puts them back in the resubmitted state.

## References

- `references/schemas.md`: the arguments and output JSON of each script.
- `skills/SKILLS_README.md`: the format of SKILL.md and the conventions for scripts/.
