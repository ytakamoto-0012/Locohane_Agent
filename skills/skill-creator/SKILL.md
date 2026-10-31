---
name: skill-creator
description: A meta-skill for creating your own draft skills (unverified skills), making improved versions of existing skills, rewriting their body and scripts, evaluating them repeatedly with the real local LLM (skill stabilization tryout), and improving the trigger accuracy of the description. Whenever there is a request to create, improve or verify a skill itself, such as "I want to make a new skill", "make a skill", "I want to fix this skill", "I want to test whether the skill works properly", "I want to improve the skill's trigger accuracy" or "show me my drafts", always use it even if the user does not say "skill-creator".
license: MIT
metadata:
  author: ytakamoto
  version: "2.0"
---

# skill-creator

A skill for repeating the cycle of creating a draft skill, evaluating it with the local LLM and fixing it.

## Always follow

- Create only **drafts**. Do not rewrite official skills (skills/ etc.). To fix an existing skill, make a draft of the improvement with `fork_skill.py`.
- Drafts appear in the list as `username/skill name` only in the creator's conversations (from the message after creating or deleting). Other people cannot see them.
- Making a skill official is the skill developer's job. When done, say "ask a skill developer to make it official" (the developer finishes it in the Skill Lab of the admin tool before making it official. Drafts that become official disappear from the list).
- Run evaluations (`run_isolated_eval.py`, `run_trigger_eval.py`) one at a time. Do not `start` the next one until `status` becomes `finished`. While it is `running`, wait about a minute before calling `status`.
- You can run this skill's scripts directly yourself without plan approval.
- Fixing a draft in even one place resets its tryout pass count to 0. After fixing, evaluate again from `--repeat 10`.

## Flow

1. Confirm what the skill does, for what kind of messages it is used, and the shape of the output.
2. Create the draft (`scaffold_skill.py` for new ones, `fork_skill.py` for improving an existing skill).
3. Write SKILL.md, scripts/ and references/ with `write_draft_file.py`.
4. Check with `validate_skill.py`.
5. Create 2-3 evaluation cases with `make_eval_case.py`.
6. Evaluate with `run_isolated_eval.py`, look at the results, and go back to 3.
7. Once it is stable, evaluate with `--repeat 10` (skill stabilization tryout).
8. Report the results to the user.

## 1. Create the draft

New (add `--with-script` if it also uses a script):
```
python scaffold_skill.py --name my-skill --description "What it does. For what kind of messages it is used." --with-script
```

An improvement of an existing skill (the official skill stays as is; the official skill's evaluation cases are also copied):
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

Write a script (copy long content from a file in the working folder):
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
- Keep the body of SKILL.md within 500 lines. If it gets longer, split it into references/.
- Write script call examples in the form `python <script>.py <args...>`, and also write what the output keys mean.
- On success, scripts print one line of JSON with exit code 0; on failure, they print the reason to stderr with exit code 1.

Check:
```
python validate_skill.py --name my-skill
```

## 3. Create evaluation cases

```
python make_eval_case.py --name my-skill --case-id 001_basic --turns "[\"a message a user would actually type\"]" --expect "{\"tool_call_args_contains\": {\"read_skill\": {\"skill_name\": \"my-skill\"}}}"
```

Also add judge (free-text judging viewpoint):
```
python make_eval_case.py --name my-skill --case-id 002_output --turns "[\"message\"]" --expect "{\"tool_called_any\": [\"run_script\"]}" --judge "Whether the output contains XX and nothing is fabricated"
```

- The skill name during evaluation is `my-skill` (without the username). Write only the skill name in expect too.
- For the main keys of `--expect`, see `references/schemas.md`.
- Use judge when the result cannot be decided objectively (the tone of text, etc.).
- Write the case messages (turns) in the language the users of the skill will actually use.

## 4. Evaluate

All cases once each:
```
python run_isolated_eval.py start --name my-skill
```

Without the draft, for comparison (no skill for new ones, the official skill as is for improvements):
```
python run_isolated_eval.py start --name my-skill --mode without_skill
```

Only some cases, 3 times each:
```
python run_isolated_eval.py start --name my-skill --case 001_basic --case 002_output --repeat 3
```

Skill stabilization tryout (10 times per case; passes if every run passes):
```
python run_isolated_eval.py start --name my-skill --repeat 10
```

Results:
```
python run_isolated_eval.py status --name my-skill --job-id <job_id>
```

- `verdict` `pass` means every run passed, `fail` means there were failures, and `needs_judge` means read the `judge` and `final_answer` of `runs` and judge it yourself. Write the grounds for your verdict in the report.
- `stale: true` means the draft changed during evaluation. Do not use the results; `start` again.
- Comparison table of with_skill and without_skill: `python aggregate_results.py --name my-skill --input with_skill=<results_path> --input without_skill=<results_path>`

## 5. How to fix

- Write feedback in generalized form. Do not pile up "always ..." rules that only work for the example at hand.
- Remove redundant steps.
- If the same processing is written several times, put it together once in scripts/.
- After fixing, evaluate again with the same cases.
- For drafts sent back by a skill developer, read `returned_reason` from `list_drafts.py` and fix them. Fixing automatically puts them back in the resubmitted state.

## 6. (Optional) Improve trigger accuracy

1. Make a JSON file with 10-20 messages in total, both ones where it should be used and confusing ones where it should not (format in `references/schemas.md`).
2. `python run_trigger_eval.py start --name my-skill --eval-set <JSON file> --repeats 3` -> look at `accuracy` and `per_query` with `status`.
3. Put the `matched: false` items in a JSON file and run `python propose_description.py start --name my-skill --failed-queries <JSON file>` -> receive the proposal with `status`.
4. Apply the proposal to the description with `write_draft_file.py`, and run 2 again.

## References

- `references/schemas.md`: the arguments and output JSON of each script.
- `skills/SKILLS_README.md`: the format of SKILL.md and the conventions for scripts/.
