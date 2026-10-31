# skill-creator reference: input/output formats of each script

Common:
- On success, exit code 0 with one line of JSON on stdout; on failure, exit code 1 with `Error: reason` on stderr.
- `--name` is the draft name. For your own drafts, just the skill name (e.g. `my-skill`); for other people's drafts, `username/skill name`.
- Other people's drafts can be handled only to the extent config.ini `[skill_creator].other_users_drafts` allows (writing and evaluating only with `full`).
- Draft location: `<draft location>/<username>/<skill name>/`. Evaluation results are in `<draft location>/<username>/_workspace/<skill name>/`.

## Asynchronous execution (start / status)

`run_isolated_eval.py`, `run_trigger_eval.py` and `propose_description.py` actually run the local LLM,
so you receive a `job_id` with `start` and the result with `status`. While `status` is `running`, wait about a minute before calling it again.

---

## scaffold_skill.py

```
python scaffold_skill.py --name my-skill --description "..." [--with-script]
```

Creates SKILL.md, references/ and evals/ (and scripts/run.py with `--with-script`) in your draft location.
The same name as an official skill, or as one of your existing drafts, is an error.

Output: `{"draft", "skill_dir", "note"}`

## fork_skill.py

```
python fork_skill.py --name <official skill name>
```

Copies an official skill into your drafts (an improvement; the official skill does not change). The cases of `evals/cases/<name>/` are also copied to evals/.

Output: `{"draft", "skill_dir", "base_dir", "copied_cases", "note"}`

## write_draft_file.py

```
python write_draft_file.py --name my-skill --path SKILL.md --content "full text"
python write_draft_file.py --name my-skill --path scripts/run.py --content-file <file>
python write_draft_file.py --name my-skill --path SKILL.md --old "old text" --new "new text"
```

Exactly one of `--content` / `--content-file` / `--old`+`--new`. `--old` must match exactly one place in the file.
`--path` is a path relative to the skill folder (`..`, absolute paths and `_draft_meta.json` are not allowed).
Writes that would make the frontmatter of SKILL.md invalid are undone.

Output: `{"draft", "path", "chars"}`

## delete_draft.py

```
python delete_draft.py --name my-skill [--path references/old.md]
```

Without `--path`, deletes the whole draft (including evaluation results). SKILL.md and `_draft_meta.json` cannot be deleted on their own.

Output: `{"deleted"}` or `{"draft", "deleted_file"}`

## list_drafts.py

```
python list_drafts.py
```

Output: `{"user", "drafts": [{"draft", "own", "readable", "kind", "base_skill", "status", "description", "updated_at", "eval_cases", "last_tryout", "returned_reason"}]}`
(other users' drafts that cannot be read show only `{"draft", "own": false, "readable": false}`)

- `kind`: `new` (new) / `improve` (an improvement of an existing skill)
- `status`: `draft` / `promoted` (made official) / `returned` (sent back; the reason is in `returned_reason`) / `rejected`
- A `returned` draft goes back to `draft` when its content is fixed with `write_draft_file.py`, `make_eval_case.py` or `delete_draft.py --path`, and becomes a candidate for promotion again.
- Fixing the content empties `tryouts` (the tryout records) (the pass count starts over from 0).

## validate_skill.py

```
python validate_skill.py --name my-skill
```

Output: `{"draft", "valid", "error", "name", "description", "description_length", "script_files", "eval_cases"}`

## make_eval_case.py

```
python make_eval_case.py --name my-skill --case-id 001_basic \
    --turns "[\"message\"]" \
    [--expect "{...}"] [--judge "judging viewpoint"] \
    [--work-dir "./evals/fixtures/xxx"] [--timeout-seconds 600] [--notes "..."]
```

Either `--expect` or `--judge` is required (both are fine). The case is saved as `evals/<case-id>.yaml` of the draft.
The skill name during evaluation is the skill name without the username (write only the skill name in expect too).

Main keys of `--expect`:
- `tool_called_any`: list[str] — passes if any of the tools is called
- `tool_not_called`: list[str] — passes if the specified tools are never called
- `tool_call_args_contains`: dict[str, dict] — e.g. `{"read_skill": {"skill_name": "my-skill"}}`
- `response_contains` / `response_not_contains`: list[str] — partial match on the final answer string

Output: `{"draft", "case_path", "case_id"}`

## run_isolated_eval.py

```
python run_isolated_eval.py start --name my-skill [--mode with_skill|without_skill] [--repeat N] [--case <ID> ...]
python run_isolated_eval.py status --name my-skill --job-id <job_id>
```

- `with_skill` (default): evaluates with the draft layered on top of the production skill setup (for improvements, with the official skill of the same name overridden).
- `without_skill`: without the draft (no skill for new ones, the official skill as is for improvements). For comparison.
- `--repeat N`: repeats each case N times (skill stabilization tryout). Passes if every run passes.

Output of `start`: `{"job_id", "pid", "log_path", "status": "started", "runs"}`
Output of `status` (running): `{"job_id", "status": "running", "started_runs", "total_runs"}`
Output of `status` (finished):
```json
{
  "status": "finished", "mode": "with_skill", "repeat": 10,
  "verdict": "pass | fail | needs_judge",
  "stale": false,
  "cases": {"001_basic": {"pass": 10, "fail": 0, "judge": 0, "error": 0}},
  "runs": [{"case_id", "repeat_index", "outcome", "failed_rules", "judge", "error", "final_answer"}],
  "results_path": "...\\results.json"
}
```

- `needs_judge`: no failures by the rules, but there are cases with judge. Read `judge` and `final_answer` of `runs` (for details, `transcript` in `results_path`) and judge it.
- Finished results are also recorded in `tryouts` of the draft's `_draft_meta.json`. But if the draft (including cases) changed during or after the evaluation, it becomes `stale: true` and is not recorded (evaluate again with the current content).

## aggregate_results.py

```
python aggregate_results.py --name my-skill --input with_skill=<results.json> --input without_skill=<results.json>
```

Makes a comparison table of pass counts and average tokens per case. Output: `{"output_path", "cases", "markdown"}`

## run_trigger_eval.py

Format of the eval-set:
```json
[
  {"query": "a message a user would actually type", "should_trigger": true},
  {"query": "a confusing message where it should not be used", "should_trigger": false}
]
```

```
python run_trigger_eval.py start --name my-skill --eval-set <JSON file> [--repeats 3]
python run_trigger_eval.py status --name my-skill --job-id <job_id>
```

Output of `status` (finished): `{"status": "finished", "accuracy", "per_query": [{"query", "should_trigger", "trigger_rate", "matched"}], "results_path"}`

Extract only the `matched: false` items and pass them to `--failed-queries` of `propose_description.py`.

## propose_description.py

```
python propose_description.py start --name my-skill --failed-queries <JSON file>
python propose_description.py status --name my-skill --job-id <job_id>
```

The current description is read from the draft's SKILL.md. Output of `status` (finished): `{"status": "finished", "proposed_description"}`.
The proposal is not applied automatically (write it with `write_draft_file.py`).
