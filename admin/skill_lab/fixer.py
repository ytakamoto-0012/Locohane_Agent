"""AI 修正（評価で失敗した回を研究室の LLM に見せ、テーマのファイルを直させる）。

直せるのはテーマ内の資産（assets/ 配下のスキル・サブエージェント）と設定パッチ（config_patch.json）。
仕様（spec.md）とケース（cases/）は人が決めたものなので直させない。失敗の原因が研究対象の外にある
（ケース: 利用者の指示にスキルを使う理由が無い・名前の綴り違い・採点の条件が満たせない等、
仕様: 自己矛盾、環境: ライブラリが無い・毎回時間切れ等）と AI が判断したときは、blockers として
報告させ、ワーカーはループを止めて人に見直してもらう（何度スキルを直しても合格しないため）。
実行の許可（ガード設定）が原因なら、AI が config_patch.json で直す。

修正は全部適用できるか、全部やめるかのどちらか。パスの制限（themes.resolve_path）と形式の検証
（SKILL.md・エージェント定義の frontmatter と tools 名・ケース・設定パッチ・Python の構文）に
1つでも通らなければ元に戻し、理由を次の反復の入力にする。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from evals import temp_dirs
from src.config import Config

from . import evaluation, themes
from .llm import LabLLMError, ask_json

_SYSTEM = """You fix the skills and sub-agents of a business AI assistant (an agent running on a local LLM).
A skill consists of SKILL.md (frontmatter with name and description at the top, the procedure in the body), scripts/ (Python scripts) and references/.
A sub-agent consists of agents/<name>.md (frontmatter with name, description and tools at the top; the body is the system prompt).
Read the records of failures in the evaluation, identify the cause, and fix the files so that they behave as the spec says.
In the records, "[AI thinking]" is the end of the assistant's internal thoughts before each step (why it chose or did not choose
a skill, tool or sub-agent), "[AI]" is the text it wrote, and "[sub-agent N ...]" are tool calls inside a sub-agent it delegated to.
"[harness note]" lines are facts measured by the evaluation harness (turns cut off by a loop guard, time taken, tools refused by
settings, missing Python libraries, time limits). Use them to tell setting and environment problems from mistakes of the model:
do not "fix" a refusal by a setting or a missing library by rewriting the skill's wording.

Words for people (applies to "analysis" and to every "problem" and "suggestion"): the reader is a person who only sees the dashboard,
not these files. Use the dashboard's own terms and never the internal ones. Say "case" (the dashboard's word for a test), "case XXX"
with its ID, "user instruction" (not "turns"), "what the AI judges" (not "judge"), "word conditions" (not "expect"),
"what it should do (grading criteria)" (not "spec"), "research target" (the skill or sub-agent being improved).
In Japanese: ケース（「試験問題」とは書かない）、利用者の指示、AI が判断する点、言葉の条件、やってほしいこと（採点の基準）、研究対象。
Write plainly: what is wrong, why that makes it impossible to pass, and the exact new wording or value to use. Do not quote
file names, keys or code unless the person must edit them. Do not write "blockers".

First decide whether the failure can be fixed by editing the research targets or config_patch.json at all.
It cannot when the cause is one of these "blockers" (no reasonable fix of the assets makes it pass):
- kind "case" (the case itself is defective):
  - the user instruction (turns) gives no reason to do what the case checks: it asks for something unrelated to the
    research target's description and "When to use" (e.g. only "hello" or "what is 1+1?" for a skill that must be read),
    and does not name the target either;
  - the instruction names a skill or sub-agent with a wrong spelling (e.g. "hello_world" for "hellow_world") or one that does not exist;
  - the checks (expect, judge) contradict the spec or each other, or require something the instruction did not ask for;
  - the checks can never be satisfied: an exact text or value that cannot be known from the instruction, the input files
    or the research targets (e.g. a number that is not in the data), something that cannot be seen in the record
    (e.g. the assistant's thoughts or speed), or a tool that does not exist;
  - the input files the case needs are missing.
- kind "spec": the spec contradicts itself (e.g. "Expected output" requires what "Must not do" forbids) or asks for something impossible.
- kind "environment": something outside the theme's files, for example a Python library or command the scripts need is not
  installed, the work needs a folder or service the assistant may not access, or every run times out because the machine or
  the model is too slow (not because the skill does needless work).
Settings that config_patch.json can change are not blockers; fix them there yourself: which skill scripts the main agent may call
directly ([main_agent_tool_guard] allow_entries, e.g. [["<skill>", "<script>.py"], -1]), which scripts are exempt from plan
approval ([plan] plan_approval_exempt_scripts, e.g. [["<skill>", "<script>.py"]]) and which skills each sub-agent type may run
([subagent] agent_type_run_script_allowlist, e.g. [["<agent type>", "<skill>"]]). config_patch.json is
{"<section>": {"<key>": [items to add]}}.
If there is a blocker, report it in "blockers" and do not edit any file (the loop stops and a person deals with it).
Do not try to make the assets pass despite a blocker (for example by making a skill claim every message).
If there is no blocker, leave "blockers" empty and fix the assets.

Rules:
- Do not change the spec (spec.md). Fix the assets to match the spec.
- Fix one cause per iteration: pick the most likely cause and make the smallest change for it, so that the next evaluation
  shows whether it worked (changing several things at once hides which change helped or broke something).
  Do not break cases that already pass.
- Do not add ad hoc instructions that only work for the failure at hand (such as "always answer XX"). Generalize the cause and fix that.
- The skills are read by a fairly small local LLM. Write for it: short sentences, one rule per line with what to do instead,
  conditions as bullet points, no long background or many examples. Write example names and values in the users' language
  (the model copies example names such as sheet names into its output).
- Mechanical work that the model keeps getting wrong in different ways (counting, numbering, dates, sums, copying many values,
  checking many cells) is not fixed by more wording: move it into a script in scripts/ that the skill tells the model to run,
  and have the script print what the model needs (and report problems it detects).
- The frontmatter name of SKILL.md and agent definitions must be the same as the folder name / file name.
- On success, scripts print one line of JSON to stdout with exit code 0; on failure, they print the reason to stderr with exit code 1.
- Do not edit, delete or add cases (cases/) or their input files, except the automatic answers to questions (below);
  report other problems with cases in "blockers".
- Questions during the evaluation: no person is present, so when the assistant asks the user (ask_user_choice, AskUserQuestion)
  the harness answers automatically. A choice question gets the next "scripted_choice_answers" entry of the case (the choice whose
  label matches; an answer matching no choice is given as the "Other" free text) or, if there is none, the FIRST choice; a free
  text question gets the next "scripted_text_answers" entry or no answer (the assistant sees a timeout).
  If a run went wrong because of such a question, decide which it is:
  - the question was reasonable (the instruction leaves out something a real user would simply tell when asked): set the
    answers a real user with the case's intent would give, with {"path": "cases/<id>.yaml", "answers": {"scripted_choice_answers": [...],
    "scripted_text_answers": [...]}} (the lists replace the current ones, in the order the questions are asked). Never put the
    result the assistant should produce, or hints about how to do the work, into the answers.
  - the question was unnecessary (the spec or the instruction already says what to do): fix the research target so it does
    not ask (for example state the default to use).
- Time limit of a case: a run that was still making progress (no loop, no repeated failing calls) but was cut off by the
  case's time limit (error "timeout", the record ends mid-work) may need more time on this machine. You may raise it with
  {"path": "cases/<id>.yaml", "answers": {"timeout_seconds": <60 to 3600>}} (the default is 900). Do not raise it when the time
  was wasted (loops, retries, needless work): fix the target so it does less instead.
- Paths you can fix: assets/skills/<name>/... and assets/agents/<name>.md of existing research targets, config_patch.json,
  and only the automatic answers and the time limit (timeout_seconds) of cases/<id>.yaml
  (you cannot create new skills or sub-agents, and you cannot delete SKILL.md or agent definitions themselves)
- Keep the language of the files you edit (do not translate existing text); write text you add to a file in the same language as that file.
  This takes precedence over the output language below for file contents. Write "analysis" in the output language.

Output only JSON in the following format:
{"analysis": "the cause of the failure and how to fix it",
 "blockers": [{"kind": "case" or "spec" or "environment", "case_id": "<ID of the case concerned (empty if not about one case)>",
               "problem": "what is wrong, in the dashboard terms above (concrete)", "suggestion": "what a person should do, in the dashboard terms above",
               "new_instruction": "for kind case, when the user instruction is the problem: the corrected first user instruction (optional; a person approves it)"}],
 "edits": [
   {"path": "assets/skills/<name>/SKILL.md", "old": "the original string to replace (must match exactly one place in the file)", "new": "the new string"},
   {"path": "assets/skills/<name>/scripts/run.py", "content": "the new content of the whole file"},
   {"path": "assets/skills/<name>/references/old.md", "delete": true},
   {"path": "cases/<id>.yaml", "answers": {"scripted_choice_answers": ["label of the choice"], "scripted_text_answers": ["reply"]}}
 ]}"""


def knowledge_text() -> str:
    """AI 修正の Agent 版に渡す、直し方の知識（_SYSTEM の出力形式より前の部分）。"""
    return _SYSTEM.split("Output only JSON")[0].rstrip() + "\n"


def _read_limited(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "(binary file)"
    if len(text) > limit:
        return text[:limit] + f"\n… ({len(text) - limit} characters omitted)"
    return text


def theme_context(directory: Path, max_file_chars: int, *, include_cases: bool = True) -> str:
    """仕様・資産・設定パッチ・ケースの全文（AI に渡す材料）。"""
    parts = [f"## Spec (spec.md)\n{_read_limited(directory / themes.SPEC_FILENAME, max_file_chars)}"]
    for item in themes.list_files(directory):
        rel = item["path"]
        if rel == themes.SPEC_FILENAME or rel.startswith(f"{themes.CASES_DIRNAME}/{themes.FIXTURES_DIRNAME}/"):
            continue
        if not include_cases and rel.startswith(f"{themes.CASES_DIRNAME}/"):
            continue
        body = _read_limited(directory / rel, max_file_chars) if item["text"] else "(binary file)"
        parts.append(f"## {rel}\n```\n{body}\n```")
    fixtures = themes.list_fixtures(directory)
    if fixtures:
        listing = []
        for f in fixtures:
            base = directory / themes.CASES_DIRNAME / f
            listing += [p.relative_to(directory / themes.CASES_DIRNAME).as_posix() for p in sorted(base.rglob("*")) if p.is_file()]
        parts.append("## Input files of the cases (contents omitted)\n" + "\n".join(f"- {x}" for x in listing))
    return "\n\n".join(parts)


def failures_text(results: list[dict], verdicts: list[dict], excerpt_chars: int, per_case: int | None = None) -> str:
    """失敗した回の説明（ケースごとに最大 per_case 回分。省略時は [skill_lab_worker] fixer_failed_runs_per_case）。"""
    if per_case is None:
        per_case = evaluation.current_limits().fixer_failed_runs_per_case
    by_key = {(v["case_id"], v["repeat_index"]): v for v in verdicts}
    shown: dict[str, int] = {}
    blocks = []
    for r in results:
        v = by_key.get((r.get("case_id"), r.get("repeat_index", 1)))
        if not v or v["outcome"] == "pass":
            continue
        cid = r.get("case_id", "?")
        if shown.get(cid, 0) >= per_case:
            continue
        shown[cid] = shown.get(cid, 0) + 1
        failed_rules = {k: d for k, d in (r.get("rule_results") or {}).items() if not d.get("pass")}
        lines = [f"### Case {cid} (run {r.get('repeat_index', 1)}): {v['outcome']}"]
        review = v.get("rule_review") or {}
        if failed_rules and review.get("pass"):
            # 表記の違いだけだった（judge.review_rules が合格扱いにした）。直す対象として見せない。
            lines.append("Exact text checks differed only in wording/notation and were accepted: " + ", ".join(sorted(failed_rules)))
        elif failed_rules:
            lines.append("Unmet checks: " + json.dumps(failed_rules, ensure_ascii=False)[:1500])
            if review.get("reason"):
                lines.append(f"Reviewer's reason the checks are not met in substance: {review['reason']}")
        if v.get("ai_judge"):
            lines.append(f"AI judge's reason for failing: {v['ai_judge'].get('reason', '')}")
        lines.append(evaluation.transcript_excerpt(r, excerpt_chars))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "(no failure records)"


def edit_summary(edits: list) -> list[dict]:
    """適用した修正の中身の要約（次の反復の AI に「何を試したか」を見せるため、反復の記録に残す）。"""
    summary = []
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("path"), str):
            continue
        if edit.get("delete"):
            summary.append({"path": edit["path"], "kind": "delete"})
        elif isinstance(edit.get("content"), str):
            summary.append({"path": edit["path"], "kind": "rewrite", "new": edit["content"][:400]})
        else:
            summary.append({"path": edit["path"], "kind": "replace", "old": str(edit.get("old", ""))[:200], "new": str(edit.get("new", ""))[:300]})
    return summary


def failed_cases_text(iteration: dict) -> str:
    """反復で落ちたケース。複数回の評価（トライアウト）では何回中何回合格したかも添える
    （一部の回だけ落ちる = 答えがゆらいでいる、を AI 修正と人が見分けられるように）。"""
    parts = []
    for cid, c in sorted((iteration.get("cases") or {}).items()):
        failed = int(c.get("fail") or 0) + int(c.get("error") or 0)
        if not failed:
            continue
        total = failed + int(c.get("pass") or 0)
        parts.append(f"{cid} (passed {total - failed}/{total})" if total > 1 else cid)
    return ", ".join(parts)


def history_text(iterations: list[dict], limit: int | None = None) -> str:
    """これまでの反復（どのケースが落ちたか・何をどう直したか・なぜ直せなかったか）。

    以前は直したファイル名と分析の冒頭しか渡しておらず、AI が試して効かなかった直しを繰り返したり、
    前の直しを元に戻したりしうる状態だった（2026-11-13 ユーザー指摘）。直しの中身まで渡す。
    """
    if limit is None:
        limit = evaluation.current_limits().fixer_history_iterations
    rows = []
    for it in iterations[-limit:] if limit > 0 else []:
        fix = it.get("fix") or {}
        row = f"- Iteration {it.get('n')} ({it.get('phase')}): verdict {it.get('verdict')}"
        failed = failed_cases_text(it)
        if failed:
            row += f", failed cases {failed}"
        if fix.get("analysis"):
            row += f"\n  analysis: {str(fix['analysis'])[:500]}"
        for e in fix.get("edit_summary") or []:
            if e["kind"] == "replace":
                row += f"\n  edit {e['path']}: replaced {json.dumps(e.get('old', ''), ensure_ascii=False)} with {json.dumps(e.get('new', ''), ensure_ascii=False)}"
            elif e["kind"] == "rewrite":
                row += f"\n  edit {e['path']}: rewrote the whole file (starts with {json.dumps(e.get('new', '')[:200], ensure_ascii=False)})"
            else:
                row += f"\n  edit {e['path']}: deleted"
        if fix.get("errors"):
            row += f"\n  why the fix could not be applied: {fix['errors']}"
        rows.append(row)
    return "\n".join(rows) or "(none yet)"


def too_many_files(edits: list, max_files: int) -> str | None:
    """1回の修正で触るファイルが多すぎれば理由を返す（1回の反復で直すのは原因1つ。tune-prompt と同じ規則）。"""
    # ケースの自動回答（answers）は研究対象の直しではないので数えない
    paths = {
        e["path"].replace("\\", "/").strip("/")
        for e in edits
        if isinstance(e, dict) and isinstance(e.get("path"), str) and "answers" not in e
    }
    if max_files > 0 and len(paths) > max_files:
        return (
            f"The fix changes {len(paths)} files ({', '.join(sorted(paths))}); change at most {max_files} file(s) per iteration "
            "and fix only the most likely cause."
        )
    return None


def propose(
    config: Config, directory: Path, *, failures: str, history: str, max_file_chars: int, previous_errors: list[str] | None = None
) -> dict:
    """修正を提案させる。previous_errors: 直前の提案を適用できなかった理由（評価し直さずに出し直させるとき）。"""
    user = (
        f"{theme_context(directory, max_file_chars)}\n\n"
        f"# Iterations so far (the fixes already tried, and the cases that still failed after them)\n{history}\n\n"
        f"# Failure records of this evaluation\n{failures}\n\n"
        "Identify the cause and output the fix in the specified JSON format.\n"
        "Do not repeat a fix that was already tried and did not make the case pass, and do not undo an earlier fix: "
        "look for a different cause. If the same case kept failing for the same reason after several different fixes, "
        "consider whether there is a blocker (a defective case, a contradicting spec or an environment problem).\n"
        "A case that passed in some runs of a tryout and failed in others is not a blocker: make the behavior steadier "
        "(clearer steps, fewer choices left to the model)."
    )
    if previous_errors:
        user += "\n\n# Your previous proposal for this evaluation could not be applied\n" + "\n".join(f"- {e}" for e in previous_errors)
        user += "\nPropose again, avoiding these problems."
    data = ask_json(config, _SYSTEM, user)
    if not isinstance(data, dict) or not isinstance(data.get("edits", []), list):
        raise LabLLMError(f"Invalid format of the fix response: {str(data)[:300]}")
    known_cases = {c["id"] for c in themes.list_cases(directory)}
    return {
        "analysis": str(data.get("analysis", "")),
        "edits": data.get("edits") or [],
        # 旧形式（case_problems）で答えるモデルもあるので、両方読む
        "blockers": _blockers((data.get("blockers") or []) + (data.get("case_problems") or []), known_cases),
    }


BLOCKER_KINDS = ("case", "spec", "environment")


def _blockers(raw, known_cases: set[str]) -> list[dict]:
    """AI が「研究対象を直しても合格しない」とした原因（ケース・仕様・環境）を、形を整えて返す。

    利用者の指示（turns）にスキルを使う理由が無い・採点の条件が満たせない・仕様が矛盾する・
    ライブラリが入っていない等は、何度直しても合格しない。以前は AI 修正がスキルを書き換え続け、
    反復の上限まで回っていた（2026-11-13 ユーザー報告）。
    ケースの指摘は実在するケースのものだけ残す（無いケースを指す指摘でループを止めない）。
    """
    blockers = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "case").strip().lower()
        kind = kind if kind in BLOCKER_KINDS else "case"
        case_id = str(item.get("case_id") or "").strip()
        problem = str(item.get("problem") or "").strip()
        if not problem or (kind == "case" and case_id not in known_cases):
            continue
        if case_id not in known_cases:
            case_id = ""
        blocker = {"kind": kind, "case_id": case_id, "problem": problem, "suggestion": str(item.get("suggestion") or "").strip()}
        # ケースの指示の書き直し案（画面のボタンで、人が承認してケースに書き込み、ループを再開できる）
        new_instruction = item.get("new_instruction")
        if kind == "case" and case_id and isinstance(new_instruction, str) and new_instruction.strip():
            blocker["new_instruction"] = new_instruction.strip()
        blockers.append(blocker)
    return blockers


def _forbidden(directory: Path, rel: str, path: Path, *, delete: bool) -> str | None:
    """AI 修正でしてはいけない変更なら理由を返す。

    試験に通るだけの直し方（失敗したケースを消す・入力ファイルを都合よく書き換える・
    研究対象そのものを消す・別の資産を作って差し替える）を防ぐ。ケースは書き換えさせない
    （問題は blockers で報告させる）。
    """
    parts = rel.split("/")
    if parts[0] == themes.CASES_DIRNAME and len(parts) >= 3:
        return "The AI cannot change the input files of cases"
    if parts[0] == themes.CASES_DIRNAME:
        # ケースに問題があれば blockers で報告させ、ループを止めて人に直してもらう（AI はケースを書き換えない）。
        return (
            "The AI can change only the automatic answers of a case "
            '({"path": "cases/<id>.yaml", "answers": {...}}); report other problems with a case in blockers'
        )
    if parts[0] == themes.ASSETS_DIRNAME:
        asset = path if parts[1] == "agents" else directory / themes.ASSETS_DIRNAME / "skills" / parts[2]
        exists = asset.is_file() if parts[1] == "agents" else (asset / "SKILL.md").is_file()
        if not exists:
            return "The AI cannot create skills or sub-agents that are not research targets"
        if delete and (parts[1] == "agents" or parts[3:] == ["SKILL.md"]):
            return "The AI cannot delete the research targets themselves (SKILL.md, agent definitions)"
    return None


# AI 修正がケースで変えてよい項目（評価中にアシスタントが利用者へ質問したときの自動回答）
CASE_ANSWER_KEYS = ("scripted_choice_answers", "scripted_text_answers", "timeout_seconds")
# AI 修正が延ばせる会話の制限時間の上限（秒）。重いケースが進んでいるのに時間で切れるときのため（採点の前提ではなく評価環境の設定）
CASE_TIMEOUT_MAX_SECONDS = 3600


def _case_with_answers(current: str | None, answers) -> tuple[str | None, str | None]:
    """ケースの yaml の自動回答（CASE_ANSWER_KEYS）だけを書き換えた内容。(新しい内容, エラー)。

    評価中にアシスタントが質問（選択肢・自由記述）して止まると、回答が無い・意図と違う選択肢が選ばれる。
    その場面の答えは人ではなく AI 修正が用意できるようにする（2026-11-15 ユーザー指摘）。
    指示（turns）・採点の条件は変えさせない。
    """
    import yaml

    if current is None:
        return None, "the case does not exist"
    if not isinstance(answers, dict) or not answers:
        return None, f"answers must be an object with {' and/or '.join(CASE_ANSWER_KEYS)}"
    unknown = [k for k in answers if k not in CASE_ANSWER_KEYS]
    if unknown:
        return None, f"only {', '.join(CASE_ANSWER_KEYS)} can be changed (not {unknown})"
    try:
        data = yaml.safe_load(current) or {}
    except yaml.YAMLError as e:
        return None, f"the case cannot be read: {e}"
    for key, value in answers.items():
        if key == "timeout_seconds":
            if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= CASE_TIMEOUT_MAX_SECONDS:
                return None, f"timeout_seconds must be an integer from 60 to {CASE_TIMEOUT_MAX_SECONDS}"
            data[key] = value
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return None, f"{key} must be a list of strings"
        if value:
            data[key] = value
        else:
            data.pop(key, None)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False), None


def apply_edits(
    directory: Path, edits: list, *, known_tools: list[str] | None, temp_root: Path | None = None, record: bool = True
) -> tuple[list[str], list[str]]:
    """edits をすべて適用する（1つでも失敗したら全部元に戻す）。(適用したパス, エラー) を返す。

    record: テーマの変更として記録する（トライアウトを無効にする）。AI 修正が修正案を試すために
        テーマの外の写し（theme.json が無い）へ適用するときは False。

    temp_root: 元に戻すためのバックアップを置く場所（対象インスタンスの [skill_lab].temp_dir）。
        省略時はテーマの runs/ の下（OS の一時フォルダは使わない）。
    """
    errors: list[str] = []
    planned: list[tuple[str, Path, str | None]] = []  # (rel, path, 新しい内容 or None=削除)
    pending: dict[Path, str | None] = {}
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict) or not isinstance(edit.get("path"), str):
            errors.append(f"item {i + 1}: no path")
            continue
        rel = edit["path"].replace("\\", "/").strip().strip("/")
        if rel == themes.SPEC_FILENAME:
            errors.append(f"{rel}: the AI cannot change the spec")
            continue
        try:
            path = themes.resolve_path(directory, rel, for_write=True)
        except themes.ThemeError as e:
            errors.append(str(e))
            continue
        is_answers = rel.startswith(f"{themes.CASES_DIRNAME}/") and rel.count("/") == 1 and "answers" in edit
        forbidden = None if is_answers else _forbidden(directory, rel, path, delete=bool(edit.get("delete")))
        if forbidden:
            errors.append(f"{rel}: {forbidden}")
            continue
        try:
            current = pending[path] if path in pending else (path.read_text(encoding="utf-8") if path.is_file() else None)
        except UnicodeDecodeError:
            errors.append(f"{rel}: not a text file, so the AI cannot fix it")
            continue
        if is_answers:
            new_content, error = _case_with_answers(current, edit.get("answers"))
            if error:
                errors.append(f"{rel}: {error}")
                continue
        elif edit.get("delete"):
            new_content = None
        elif isinstance(edit.get("content"), str):
            new_content = edit["content"]
        elif isinstance(edit.get("old"), str) and isinstance(edit.get("new"), str):
            if current is None:
                errors.append(f"{rel}: the file does not exist, so it cannot be fixed with old/new (write the whole file with content)")
                continue
            count = current.count(edit["old"])
            if count != 1:
                errors.append(f"{rel}: old matched {count} places in the file (make it match exactly one place)")
                continue
            new_content = current.replace(edit["old"], edit["new"])
        else:
            errors.append(f"{rel}: none of content, old/new or delete is specified")
            continue
        if new_content is not None:
            try:
                themes.validate_content(rel, new_content)
                if rel.startswith(f"{themes.ASSETS_DIRNAME}/agents/") and known_tools is not None:
                    error = themes.validate_agent_md(new_content, Path(rel).stem, known_tools)
                    if error:
                        raise themes.ThemeError(error)
            except themes.ThemeError as e:
                errors.append(str(e))
                continue
        pending[path] = new_content
        planned.append((rel, path, new_content))
    if errors:
        return [], errors
    if not planned:
        return [], ["There are no fixes"]

    with temp_dirs.temporary(temp_root or directory / themes.RUNS_DIRNAME, "skill_lab_fix_") as tmp:
        backups: dict[Path, Path | None] = {}
        try:
            for rel, path, content in planned:
                if path not in backups:
                    if path.is_file():
                        backup = Path(tmp) / f"{len(backups)}"
                        shutil.copy2(path, backup)
                        backups[path] = backup
                    else:
                        backups[path] = None
                if content is None:
                    if path.is_file():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
        except OSError as e:
            for path, backup in backups.items():
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    shutil.copy2(backup, path)
            return [], [f"Failed to write files: {e}"]
    applied = list(dict.fromkeys(rel for rel, _, _ in planned))
    if record:
        themes.mark_changed(directory, "AI", "AI fix: " + ", ".join(applied))
    return applied, []
