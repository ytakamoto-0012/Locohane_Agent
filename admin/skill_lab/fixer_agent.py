"""AI 修正の Agent 版（ツールで調べ、修正案を試してから、直し方かケース・仕様・環境の問題を報告する）。

以前の AI 修正（fixer.propose）は、仕様・資産・落ちた回の抜粋を1つのプロンプトに詰めて LLM を1回呼ぶだけで、
AI は自分で追加の情報を取りに行けず、直した案を試すこともできなかった。不具合の要因分析は、記録を読み比べ、
仮説を立て、試して確かめる作業であり、1回の思考では足りない（2026-11-15 ユーザー指摘）。

人が原因を調べる手順に合わせてツールを持たせる:
- 事実を見る: list_runs / run_overview（各回の流れの一覧。回ごとの違いを見つける）/ read_run（1回分の全文）/
  search_runs（全部の回から言葉を探して数える）
- アシスタントが見ていたものを見る: read_assistant_prompt（実際に受け取ったシステムプロンプト。スキル一覧の説明文・
  規則）/ list_other_assets・read_other_asset（研究対象以外のスキル・サブエージェント。取り違えの相手）/
  list_theme_files・read_theme_file（研究対象・仕様・ケース）
- 仮説を確かめる: try_fix（修正案を研究対象の写しに当てて、落ちたケースを数回だけ評価する）/
  try_case_variant（ケースの指示だけを変えた写しで評価する。ケースの不備の裏付け）
- 結論: submit_fix（直し方。形式・規則を確かめてから受け付ける）/ report_blockers（研究対象の外の原因）

試しの評価は研究対象を書き換えない（runs/<反復>/agent/ の下の写しで行う）。結論の直しはワーカーが従来どおり適用する。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from . import evaluation, fixer, sources, themes
from .llm import LabLLMError, invoke_resilient, strip_thinking, with_output_language

# 試しの評価の置き場（runs/<反復>/ の下）
AGENT_DIRNAME = "agent"

_METHOD = """
# How to work: you are an engineer doing root cause analysis, not a one-shot guesser
You have tools. The output format above (JSON) does not apply: you finish by calling submit_fix or report_blockers.
Investigate before deciding, the way the person who tuned this assistant for months does:

## 1. Look at the facts
- Start with run_overview of the latest evaluation: every run side by side (skills read, tools called, outcome).
- read_run a failing run AND a passing run of the same case if there is one, and find the FIRST decision where they differ.
  Read the assistant's thinking just before it: it usually says why.
- When runs vary, measure instead of guessing: search_runs for the deciding words (a skill name, a tool name, a phrase of
  the thinking) and count in how many runs it happens.
- Check the harness notes: turns cut off by a loop guard, time limits, tools refused by settings, missing libraries,
  questions answered automatically.

## 2. Look at what the assistant saw
- read_assistant_prompt: the real system prompt of a run (skill list with descriptions, rules such as when to ask the user,
  plan approval, delegation). list_other_assets / read_other_asset: the other skills and sub-agents.
- Compare the user instruction with every description: a competing skill whose description fits the words of the
  instruction is a frequent cause of reading the wrong skill or none.
- Look for contradictions between the research target and the system prompt or another skill: a model that gets two
  conflicting rules hesitates, loops or asks the user.

## 3. Classify the cause (typical causes seen in past tuning of this assistant)
- Wrong or no skill chosen: the description does not say clearly when to use it, or another description fits better.
  Fix the description (what it does + when to use it, with the words users write). Do not make it claim every message.
- The skill was read but a step was skipped or done differently: the procedure is ambiguous, too long, has several ways
  to do something, or conflicts with the system prompt. Make one clear way; remove or rewrite conflicting text rather than
  adding more. Each added rule can be misread in another situation and make things oscillate.
- Mechanical work done wrong in changing ways (counting, numbering, dates, sums, copying many values): wording does not fix
  it. Move it into a script in scripts/ and have the skill run it.
- The model ignores a restriction written only in words: enforce it with settings in config_patch.json where possible.
- The assistant asked the user and the automatic answer led it astray (see "Questions during the evaluation"):
  set the case's automatic answers, or fix the target so it does not need to ask.
- The run hit the case's time limit (error "timeout"; the record ends mid-work): read where the time went (the harness notes
  give the time of each turn, the flow shows repeated or needless calls). If it was progressing normally, raise the case's
  timeout_seconds (see "Time limit of a case"); if time was wasted (searching again and again, delegating the same work twice,
  reading many files the procedure does not need), fix the target so it does less. Both can be needed.
- A sub-agent's LLM call failed (harness note about a connection error or timeout of the inference server): an environment
  problem; do not change the target because of it.
- Unstable output format of the model (empty replies, tool calls written as text, loops cut off by the guard) in a way not
  caused by the target: not fixable by wording; if it makes the case fail every time, report it as an environment blocker.
- The case's word conditions or judge points are wrong or stale (they require something the instruction did not ask for,
  or reject a correct answer): a case blocker, with the evidence.
- The instruction does not name or describe the target and fits another skill equally well: try first to make the target's
  description cover these words; if that cannot work without making it claim unrelated messages, it is a case blocker
  (support it with try_case_variant).

## 4. Test before you conclude
- State the hypothesis as a mechanism: "because X in the instruction and Y in the description, the model does Z in N of M runs".
- try_fix the smallest change for that one cause on the failing case(s). A tryout needs every run to pass, so 2 of 3 is not
  good enough: find a steadier fix. If a passing case could be affected, include it in case_ids.
- For a suspected case defect, try_case_variant with a clearer instruction: if that passes reliably and no reasonable fix of
  the target does, that is your evidence for a case blocker.
- Do not repeat a fix that was already tried in this loop (list_runs shows them) or undo an earlier fix; if the same case
  keeps failing for the same reason after several different fixes, the cause is elsewhere: look again at steps 1 to 3.

## Speed
Finishing correctly always comes first; never trade it for speed. Among ways that finish correctly, prefer the one that needs
fewer steps and less time (run_overview shows the time and LLM calls of each run): avoid searching or reading more than the
procedure needs, repeated delegations of the same work, and checks that add nothing. When every case passes but runs are slow,
a fix that removes needless work is welcome.

## 5. Conclude
- submit_fix with the change (preferably one that passed in try_fix) and an analysis a person can follow: the cause, the
  evidence (runs and counts) and why the change addresses it. One cause per fix.
- or report_blockers with the evidence. Even then, put the case settings you may change and found necessary (a raised
  timeout_seconds, automatic answers) into its case_settings: they are applied, so the loop does not fail on them again
  after a person fixes the case.
If submit_fix or report_blockers reports a problem with your input, correct it and call again. You have a limited number of
steps and trials (shown after each tool result); do not spend them re-reading the same things.
"""

_FINISH_NOW = (
    "The step budget is used up. Call submit_fix or report_blockers now with your best conclusion "
    "(prefer a fix that passed in try_fix)."
)


@dataclass
class AgentLimits:
    max_steps: int = 40
    max_trials: int = 3
    trial_repeats: int = 3
    read_chars: int = 12000
    context_chars: int = 150000


@dataclass
class Runner:
    """試しの評価を行う関数（ワーカーが用意する。should_stop・LLM の接続先・判定の並列数などはワーカーの設定）。

    evaluate(case_ids, repeat, results_root, assets_base, config_patch_file, cases_dir) -> (results, verdicts)
    """

    evaluate: Callable[..., tuple[list[dict], list[dict]]]
    should_stop: Callable[[], bool] = lambda: False
    on_step: Callable[[int, str], None] = lambda step, tool: None


@dataclass
class _State:
    directory: Path
    work_dir: Path
    target_cfg: Any
    iterations: list[dict]
    limits: AgentLimits
    runner: Runner
    known_tools: list[str] | None
    temp_root: Path | None
    max_files: int
    trials_used: int = 0
    trials: list[dict] = field(default_factory=list)  # {"id", "kind", "results", "verdicts", "summary"}
    final: dict | None = None
    log: list[dict] = field(default_factory=list)


# --- 記録の読み出し ----------------------------------------------------------------


def _load_results(out_dir: str | None) -> list[dict]:
    if not out_dir:
        return []
    path = Path(out_dir) / "results.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _runs_of(state: _State, source: str) -> tuple[list[dict], list[dict]] | None:
    """"<反復番号>" または "T<試しの番号>" の (results, verdicts)。"""
    source = source.strip()
    if source.upper().startswith("T"):
        for t in state.trials:
            if t["id"].upper() == source.upper():
                return t["results"], t["verdicts"]
        return None
    for it in state.iterations:
        if str(it.get("n")) == source:
            return _load_results(it.get("out_dir")), it.get("verdicts") or []
    return None


def _find_run(state: _State, ref: str) -> tuple[dict, dict] | None:
    """ref = "<反復番号 or T番号>/<ケースID>/<回>"。"""
    parts = ref.strip().split("/")
    if len(parts) != 3:
        return None
    runs = _runs_of(state, parts[0])
    if runs is None:
        return None
    results, verdicts = runs
    for r in results:
        if r.get("case_id") == parts[1] and str(r.get("repeat_index", 1)) == parts[2]:
            v = next((v for v in verdicts if v.get("case_id") == parts[1] and str(v.get("repeat_index", 1)) == parts[2]), {})
            return r, v
    return None


def _outcome(v: dict, r: dict) -> str:
    return str(v.get("outcome") or evaluation.classify(r))


def _page(text: str, offset: int, size: int) -> str:
    offset = max(0, int(offset or 0))
    chunk = text[offset : offset + size]
    rest = len(text) - offset - len(chunk)
    tail = f"\n… ({rest} more characters; call again with offset={offset + len(chunk)})" if rest > 0 else ""
    return chunk + tail


def _short(value, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _run_cost(r: dict) -> str:
    """回の所要時間と LLM 呼び出し回数（速くできる所を見つけるため。evals/run_case.py の turn_timings）。"""
    seconds = calls = 0.0
    for t in r.get("turn_timings") or []:
        llm = [c.get("total_seconds") or 0 for c in t.get("llm_calls") or []]
        tools = [c.get("total_seconds") or 0 for c in t.get("tool_calls") or []]
        seconds += sum(llm) + sum(tools)
        calls += len(llm)
    return f" (about {seconds:.0f} s, {calls:.0f} LLM calls)" if seconds else ""


def _tool_flow(r: dict) -> list[str]:
    """回の中の流れ（ツール呼び出しの順。引数は短く）。"""
    flow = []
    for m in r.get("transcript") or []:
        if m.get("type") == "AIMessage":
            for tc in m.get("tool_calls") or []:
                flow.append(f"{tc.get('name')}({_short(tc.get('args') or {}, 80)})")
    return flow


_FULL_LIMITS = evaluation.ExcerptLimits(
    user_chars=20000,
    thinking_chars=20000,
    ai_text_chars=20000,
    ai_note_chars=20000,
    tool_args_chars=20000,
    tool_result_chars=20000,
    subagent_args_chars=20000,
    subagent_result_chars=20000,
)


def _run_text(r: dict, v: dict) -> str:
    lines = [f"case {r.get('case_id')} run {r.get('repeat_index', 1)}: {_outcome(v, r)}"]
    if r.get("error"):
        lines.append(f"error: {r.get('error')} {r.get('detail') or ''}")
    failed = {k: d for k, d in (r.get("rule_results") or {}).items() if not d.get("pass")}
    if failed:
        lines.append("unmet word conditions: " + _short(failed, 3000))
    review = v.get("rule_review") or {}
    if review.get("reason"):
        lines.append(f"review of the word conditions: {'accepted' if review.get('pass') else 'not met'} - {review['reason']}")
    if v.get("ai_judge"):
        lines.append(f"AI judge: {'pass' if v['ai_judge'].get('pass') else 'fail'} - {v['ai_judge'].get('reason', '')}")
    with evaluation.limits(_FULL_LIMITS):
        lines.append(evaluation.transcript_excerpt(r, 10**7))
    return "\n".join(lines)


# --- ツール -----------------------------------------------------------------------


def _budget(state: _State, step: int) -> str:
    return f"[budget: step {step}/{state.limits.max_steps}, trials left {state.limits.max_trials - state.trials_used}]"


def _list_theme_files(state: _State) -> str:
    rows = [f"- {f['path']} ({f['size']} bytes{'' if f['text'] else ', binary'})" for f in themes.list_files(state.directory)]
    assets = themes.assets_in(state.directory / themes.ASSETS_DIRNAME)
    head = f"research targets: skills {assets['skills']}, sub-agents {assets['agents']}"
    return head + "\n" + "\n".join(rows)


def _read_theme_file(state: _State, path: str, offset: int = 0) -> str:
    try:
        text = themes.read_text(state.directory, path)
    except themes.ThemeError as e:
        return f"error: {e}"
    return _page(text, offset, state.limits.read_chars)


def _list_runs(state: _State) -> str:
    rows = []
    for it in state.iterations:
        failed = fixer.failed_cases_text(it)
        line = f"- iteration {it.get('n')} ({it.get('phase')}, {it.get('repeat')} run(s) per case): verdict {it.get('verdict')}"
        if failed:
            line += f"; failed: {failed}"
        fix = it.get("fix") or {}
        if fix.get("analysis"):
            line += f"\n  fix tried after it: {_short(fix['analysis'], 400)}"
        for e in fix.get("edit_summary") or []:
            line += f"\n    edit {e['path']} ({e['kind']}): {_short(e.get('new', ''), 200)}"
        if it.get("reverted"):
            line += "\n  (that fix was reverted because it broke a passing case)"
        rows.append(line)
    for t in state.trials:
        rows.append(f"- trial {t['id']} ({t['kind']}): {t['summary']}")
    return ("Evaluations of this loop (newest last). Use run_overview with the iteration number or trial id.\n" + "\n".join(rows)) if rows else "(no evaluations)"


def _run_overview(state: _State, source: str) -> str:
    runs = _runs_of(state, str(source))
    if runs is None:
        return f"error: unknown iteration or trial {source!r} (see list_runs)"
    results, verdicts = runs
    if not results:
        return "(no run records for it)"
    vmap = {(v.get("case_id"), v.get("repeat_index", 1)): v for v in verdicts}
    rows = []
    for r in results:
        v = vmap.get((r.get("case_id"), r.get("repeat_index", 1)), {})
        ref = f"{source}/{r.get('case_id')}/{r.get('repeat_index', 1)}"
        row = f"- {ref}: {_outcome(v, r)}{_run_cost(r)}\n  flow: {' → '.join(_tool_flow(r)) or '(no tool calls)'}"
        row += f"\n  final answer: {_short(r.get('final_answer') or '', 160)}"
        reason = (v.get("ai_judge") or {}).get("reason") or (v.get("rule_review") or {}).get("reason")
        if reason and _outcome(v, r) != "pass":
            row += f"\n  why it failed: {_short(reason, 240)}"
        rows.append(row)
    return "\n".join(rows)


def _read_run(state: _State, ref: str, offset: int = 0) -> str:
    found = _find_run(state, ref)
    if found is None:
        return f"error: unknown run {ref!r} (use the refs shown by run_overview, like 12/case_001/2)"
    return _page(_run_text(*found), offset, state.limits.read_chars)


def _search_runs(state: _State, text: str, source: str = "") -> str:
    if not text:
        return "error: give the text to search for"
    sources_to_search = [str(source)] if source else [str(it.get("n")) for it in state.iterations] + [t["id"] for t in state.trials]
    rows = []
    total = hits = 0
    for src in sources_to_search:
        runs = _runs_of(state, src)
        if runs is None:
            continue
        results, verdicts = runs
        vmap = {(v.get("case_id"), v.get("repeat_index", 1)): v for v in verdicts}
        for r in results:
            total += 1
            body = json.dumps(r.get("transcript") or [], ensure_ascii=False) + "\n" + (r.get("final_answer") or "")
            count = body.count(text)
            if not count:
                continue
            hits += 1
            i = body.find(text)
            v = vmap.get((r.get("case_id"), r.get("repeat_index", 1)), {})
            rows.append(
                f"- {src}/{r.get('case_id')}/{r.get('repeat_index', 1)} ({_outcome(v, r)}): {count} time(s), e.g. …{_short(body[max(0, i - 80) : i + 80], 200)}…"
            )
    return f"{text!r} appears in {hits} of {total} runs\n" + "\n".join(rows[:60])


def _read_assistant_prompt(state: _State, ref: str = "", offset: int = 0) -> str:
    found = None
    if ref:
        found = _find_run(state, ref)
        if found is None:
            return f"error: unknown run {ref!r}"
    else:
        for it in reversed(state.iterations):
            results = _load_results(it.get("out_dir"))
            if results:
                found = (results[-1], {})
                break
    if found is None:
        return "(no run records)"
    prompt = found[0].get("system_prompt")
    if not prompt:
        return "(this run was recorded before system prompts were kept; run try_fix or try_case_variant to get a run that has it)"
    return _page(prompt, offset, state.limits.read_chars)


def _list_other_assets(state: _State) -> str:
    targets = themes.assets_in(state.directory / themes.ASSETS_DIRNAME)
    rows = ["Skills the assistant can see (research targets replace the official ones of the same name):"]
    for s in sources.list_official_skills(state.target_cfg):
        mark = " [research target]" if s["name"] in targets["skills"] else ""
        rows.append(f"- {s['name']}{mark} ({s['origin']}): {_short(s['description'] or '', 400)}")
    for name in targets["skills"]:
        if not any(r.startswith(f"- {name} ") or r.startswith(f"- {name}[") for r in rows):
            rows.append(f"- {name} [research target, new]")
    rows.append("Sub-agents:")
    for a in sources.list_official_agents(state.target_cfg):
        mark = " [research target]" if a["name"] in targets["agents"] else ""
        rows.append(f"- {a['name']}{mark}: {_short(a['description'] or '', 300)}")
    return "\n".join(rows)


def _read_other_asset(state: _State, name: str, offset: int = 0) -> str:
    skill_dir = sources.find_official_skill_dir(state.target_cfg, name)
    if skill_dir is not None:
        files = [p.relative_to(skill_dir).as_posix() for p in sorted(skill_dir.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        return _page(f"files: {files}\n\n{text}", offset, state.limits.read_chars)
    agent = sources.find_official_agent_file(state.target_cfg, name)
    if agent is not None:
        return _page(agent.read_text(encoding="utf-8", errors="replace"), offset, state.limits.read_chars)
    return f"error: no official skill or sub-agent named {name!r} (see list_other_assets)"


def _trial_summary(results: list[dict], verdicts: list[dict]) -> str:
    per: dict[str, list[str]] = {}
    for r in results:
        v = next((v for v in verdicts if v.get("case_id") == r.get("case_id") and v.get("repeat_index", 1) == r.get("repeat_index", 1)), {})
        per.setdefault(r.get("case_id", "?"), []).append(_outcome(v, r))
    return "; ".join(f"case {c}: passed {o.count('pass')}/{len(o)}" for c, o in per.items()) or "no results"


def _new_trial_dir(state: _State) -> tuple[str, Path]:
    trial_id = f"T{len(state.trials) + 1}"
    path = state.work_dir / trial_id.lower()
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return trial_id, path


def _trial_cases(state: _State, case_ids) -> list[str] | str:
    known = {c["id"] for c in themes.list_cases(state.directory)}
    ids = [str(c) for c in (case_ids or []) if str(c)]
    if not ids:
        failed = []
        if state.iterations:
            failed = [cid for cid, c in (state.iterations[-1].get("cases") or {}).items() if int(c.get("fail") or 0) + int(c.get("error") or 0)]
        ids = failed or sorted(known)
    unknown = [c for c in ids if c not in known]
    return f"error: unknown case(s) {unknown}; cases are {sorted(known)}" if unknown else ids


def _run_trial(
    state: _State, kind: str, trial_id: str, trial_dir: Path, case_ids: list[str], repeats: int, meta: dict | None = None, **kwargs
) -> str:
    state.trials_used += 1
    results, verdicts = state.runner.evaluate(
        case_ids=case_ids, repeat=repeats, results_root=trial_dir / "results", **kwargs
    )
    summary = _trial_summary(results, verdicts)
    state.trials.append({"id": trial_id, "kind": kind, "results": results, "verdicts": verdicts, "summary": summary, **(meta or {})})
    lines = [f"trial {trial_id}: {summary} (the research targets were not changed)"]
    vmap = {(v.get("case_id"), v.get("repeat_index", 1)): v for v in verdicts}
    for r in results:
        v = vmap.get((r.get("case_id"), r.get("repeat_index", 1)), {})
        if _outcome(v, r) != "pass":
            reason = (v.get("ai_judge") or {}).get("reason") or (v.get("rule_review") or {}).get("reason") or r.get("detail") or ""
            lines.append(f"- {trial_id}/{r.get('case_id')}/{r.get('repeat_index', 1)} failed: flow {' → '.join(_tool_flow(r))}; {_short(reason, 300)}")
    lines.append(f"Read a run with read_run {trial_id}/<case>/<run>, or see all with run_overview {trial_id}.")
    return "\n".join(lines)


def _repeats(state: _State, repeats) -> int:
    try:
        n = int(repeats or state.limits.trial_repeats)
    except (TypeError, ValueError):
        n = state.limits.trial_repeats
    return max(1, min(n, max(state.limits.trial_repeats, 1) * 2))


def _try_fix(state: _State, edits: list, case_ids: list | None = None, repeats: int | None = None) -> str:
    if state.trials_used >= state.limits.max_trials:
        return "error: no trials left. Decide with submit_fix or report_blockers."
    ids = _trial_cases(state, case_ids)
    if isinstance(ids, str):
        return ids
    limit_error = fixer.too_many_files(edits or [], state.max_files)
    if limit_error:
        return f"error: {limit_error}"
    trial_id, trial_dir = _new_trial_dir(state)
    themes.snapshot_targets(state.directory, trial_dir / "candidate")
    candidate = trial_dir / "candidate"
    # 案にケースの自動回答の変更が入ることがあるため、ケースも写しで評価する（入力ファイルも写す: work_dir が指す）
    fixtures = state.directory / themes.CASES_DIRNAME / themes.FIXTURES_DIRNAME
    if fixtures.is_dir():
        shutil.copytree(fixtures, candidate / themes.CASES_DIRNAME / themes.FIXTURES_DIRNAME)
    applied, errors = fixer.apply_edits(candidate, edits or [], known_tools=state.known_tools, temp_root=state.temp_root, record=False)
    if errors:
        shutil.rmtree(trial_dir, ignore_errors=True)
        return "error: the edits could not be applied (no trial was used):\n" + "\n".join(f"- {e}" for e in errors)
    patch = candidate / themes.CONFIG_PATCH_FILENAME
    return _run_trial(
        state,
        "fix " + ", ".join(applied),
        trial_id,
        trial_dir,
        ids,
        _repeats(state, repeats),
        assets_base=candidate / themes.ASSETS_DIRNAME,
        config_patch_file=patch if patch.is_file() else None,
        cases_dir=candidate / themes.CASES_DIRNAME,
    )


def _try_case_variant(state: _State, case_id: str, user_instruction: str, repeats: int | None = None) -> str:
    if state.trials_used >= state.limits.max_trials:
        return "error: no trials left. Decide with submit_fix or report_blockers."
    ids = _trial_cases(state, [case_id])
    if isinstance(ids, str):
        return ids
    if not (user_instruction or "").strip():
        return "error: give the new user instruction"
    trial_id, trial_dir = _new_trial_dir(state)
    cases_dir = trial_dir / themes.CASES_DIRNAME
    shutil.copytree(state.directory / themes.CASES_DIRNAME, cases_dir)
    path = cases_dir / f"{case_id}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    turns = data.get("turns") or []
    first = user_instruction.strip()
    data["turns"] = [first, *turns[1:]] if isinstance(turns, list) and turns else [first]
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    result = _run_trial(
        state,
        f"case {case_id} with the instruction {_short(first, 120)!r}",
        trial_id,
        trial_dir,
        ids,
        _repeats(state, repeats),
        meta={"case_id": case_id, "instruction": first},
        cases_dir=cases_dir,
    )
    return result + "\n(This only tells whether the case is the cause; you cannot change cases. Report it with report_blockers if it is.)"


def _submit_fix(state: _State, analysis: str, edits: list) -> str:
    if not (analysis or "").strip():
        return "error: write the analysis (the cause and how the fix addresses it)"
    limit_error = fixer.too_many_files(edits or [], state.max_files)
    if limit_error:
        return f"error: {limit_error}"
    check_dir = state.work_dir / "submit_check"
    themes.snapshot_targets(state.directory, check_dir)
    try:
        _, errors = fixer.apply_edits(check_dir, edits or [], known_tools=state.known_tools, temp_root=state.temp_root, record=False)
    finally:
        shutil.rmtree(check_dir, ignore_errors=True)
    if errors:
        return "error: the edits cannot be applied; correct them and call submit_fix again:\n" + "\n".join(f"- {e}" for e in errors)
    state.final = {"analysis": analysis.strip(), "edits": edits, "blockers": []}
    return "accepted"


def _report_blockers(state: _State, analysis: str, blockers: list, case_settings: list | None = None) -> str:
    known_cases = {c["id"] for c in themes.list_cases(state.directory)}
    clean = fixer._blockers(blockers or [], known_cases)
    if not clean:
        return (
            "error: no valid blocker. Each needs kind (case, spec or environment), problem, suggestion, and for kind case "
            f"an existing case_id (cases: {sorted(known_cases)})."
        )
    # 止まるときも、AI が変えてよいケースの設定（制限時間・自動回答）は適用しておく。人がケースを直して再開したときに、
    # 同じ時間切れ・質問で落ちないように（2026-11-15 仮想研究室: 1500秒が要ると分かっていながら提案文に書くだけだった）
    settings = [e for e in case_settings or [] if isinstance(e, dict)]
    if any("answers" not in e or not str(e.get("path", "")).startswith(f"{themes.CASES_DIRNAME}/") for e in settings):
        return 'error: case_settings may only contain {"path": "cases/<id>.yaml", "answers": {...}} items'
    if settings:
        check_dir = state.work_dir / "blocker_check"
        themes.snapshot_targets(state.directory, check_dir)
        try:
            _, errors = fixer.apply_edits(check_dir, settings, known_tools=state.known_tools, temp_root=state.temp_root, record=False)
        finally:
            shutil.rmtree(check_dir, ignore_errors=True)
        if errors:
            return "error: the case settings cannot be applied; correct them and call again:\n" + "\n".join(f"- {e}" for e in errors)
    # 直した指示の案を試しで確かめていれば、その結果を添える（人が承認するときの根拠）
    for b in clean:
        if b.get("new_instruction"):
            tried = [t for t in state.trials if t.get("case_id") == b["case_id"] and t.get("instruction") == b["new_instruction"]]
            if tried:
                b["verified"] = tried[-1]["summary"]
    state.final = {"analysis": (analysis or "").strip(), "edits": settings, "blockers": clean}
    return "accepted"


_EDITS_DOC = (
    'edits: a list like [{"path": "assets/skills/<name>/SKILL.md", "old": "exact text to replace (one place)", "new": "new text"}, '
    '{"path": "assets/skills/<name>/scripts/x.py", "content": "whole new file"}, {"path": "...", "delete": true}]'
)


def _tools(state: _State, step_ref: list[int]):
    from typing import Optional

    from langchain_core.tools import StructuredTool
    from pydantic import Field, create_model

    def wrap(fn, tool_name: str, description: str, /, **fields):
        schema = create_model(f"{tool_name}_args", **fields)

        def call(**kwargs):
            try:
                result = fn(state, **kwargs)
            except evaluation.EvaluationStopped:
                raise
            except Exception as e:  # noqa: BLE001 - ツールの失敗は AI に見せて続ける
                result = f"error: {type(e).__name__}: {e}"
            return f"{result}\n{_budget(state, step_ref[0])}"

        return StructuredTool.from_function(func=call, name=tool_name, description=description, args_schema=schema)

    offset = (int, Field(0, description="character offset to continue a long text"))
    edits = (list[dict], Field(..., description=_EDITS_DOC))
    return [
        wrap(_list_theme_files, "list_theme_files", "List the files of the theme: research targets (assets/), the spec (spec.md), cases (cases/) and config_patch.json."),
        wrap(_read_theme_file, "read_theme_file", "Read a file of the theme by its path from list_theme_files. Long files are paged: pass offset to continue.",
             path=(str, Field(..., description="path from list_theme_files")), offset=offset),
        wrap(_list_runs, "list_runs", "List the evaluations of this loop (iterations and your trials) with the failed cases and the fixes tried after each."),
        wrap(_run_overview, "run_overview", "Show every run of one evaluation side by side: outcome, the flow of tool calls, the final answer and why it failed. Gives the refs for read_run.",
             source=(str, Field(..., description="iteration number like '12' or trial id like 'T1'"))),
        wrap(_read_run, "read_run", "Read one run in full: the user instruction, the assistant's thinking, tool calls and results, the final answer, and why it was judged as it was. Paged with offset.",
             ref=(str, Field(..., description="run ref from run_overview, like '12/case_001/2'")), offset=offset),
        wrap(_search_runs, "search_runs", "Count in how many runs a text appears (for example a skill name or a phrase of the thinking), to measure how often a behavior happens.",
             text=(str, Field(..., description="exact text to look for")),
             source=(str, Field("", description="limit to one iteration number or trial id (empty: all)"))),
        wrap(_read_assistant_prompt, "read_assistant_prompt", "Read the system prompt the assistant actually received in a run: the skill list with descriptions and the rules it follows. Paged with offset.",
             ref=(str, Field("", description="run ref (empty: the latest run)")), offset=offset),
        wrap(_list_other_assets, "list_other_assets", "List the skills and sub-agents the assistant can see in the target instance, with descriptions (to find a competing skill)."),
        wrap(_read_other_asset, "read_other_asset", "Read an official skill (SKILL.md and its file list) or sub-agent definition by name. Paged with offset.",
             name=(str, Field(..., description="skill or sub-agent name")), offset=offset),
        wrap(_try_fix, "try_fix",
             "Try a candidate fix without changing the research targets: apply the edits to a copy and evaluate the cases repeats times. Uses one trial.",
             edits=edits,
             case_ids=(Optional[list[str]], Field(None, description="cases to evaluate (default: the cases that failed last)")),
             repeats=(Optional[int], Field(None, description="runs per case (default shown in the budget settings)"))),
        wrap(_try_case_variant, "try_case_variant",
             "Evaluate a case with a different first user instruction and the current research targets, to check whether the case's instruction is the cause. Uses one trial. It does not change the case.",
             case_id=(str, Field(..., description="case ID")),
             user_instruction=(str, Field(..., description="the new first user instruction")),
             repeats=(Optional[int], Field(None, description="runs"))),
        wrap(_submit_fix, "submit_fix",
             "Finish with a fix. The edits are checked and you are told if they cannot be applied.",
             analysis=(str, Field(..., description="the cause found, the evidence, and why the fix addresses it, written for a person")),
             edits=edits),
        wrap(_report_blockers, "report_blockers",
             "Finish by reporting that the cause is outside the research targets, with the evidence.",
             analysis=(str, Field(..., description="what you found, written for a person")),
             blockers=(list[dict], Field(..., description='list of {"kind": "case" | "spec" | "environment", "case_id": "<case ID or empty>", "problem": "...", "suggestion": "...", "new_instruction": "(kind case only, optional) the corrected first user instruction, ideally one you checked with try_case_variant; a person can apply it with one click"}')),
             case_settings=(Optional[list[dict]], Field(None, description='case settings you are allowed to change and found necessary (raised time limits, automatic answers), applied now so the loop does not fail on them again after a person fixes the case: [{"path": "cases/<id>.yaml", "answers": {"timeout_seconds": 1500}}]'))),
    ]


def _compact(messages: list, budget: int, keep_last: int = 6) -> None:
    """ツールの結果が文脈に溜まりすぎたら、古いものから短くする（必要ならもう一度ツールで読み直せる）。"""
    from langchain_core.messages import ToolMessage

    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    total = sum(len(str(m.content)) for m in messages)
    for m in tool_msgs[:-keep_last] if len(tool_msgs) > keep_last else []:
        if total <= budget:
            break
        text = str(m.content)
        if len(text) <= 600:
            continue
        short = text[:400] + "\n… (older result shortened to save context; call the tool again if you need it)"
        total -= len(text) - len(short)
        m.content = short


def propose(
    config,
    directory: Path,
    *,
    system_knowledge: str,
    task: str,
    work_dir: Path,
    target_cfg,
    iterations: list[dict],
    runner: Runner,
    limits: AgentLimits,
    known_tools: list[str] | None,
    temp_root: Path | None,
    max_files: int,
) -> dict:
    """Agent に原因を調べさせ、直し方か研究対象の外の原因を返す（fixer.propose と同じ形 + trials / steps / log）。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    state = _State(
        directory=directory,
        work_dir=work_dir,
        target_cfg=target_cfg,
        iterations=iterations,
        limits=limits,
        runner=runner,
        known_tools=known_tools,
        temp_root=temp_root,
        max_files=max_files,
    )
    asyncio.run(_loop(config, state, system_knowledge + _METHOD, task))
    final = state.final or {}
    return {
        "analysis": final.get("analysis", ""),
        "edits": final.get("edits") or [],
        "blockers": final.get("blockers") or [],
        "trials": [{"id": t["id"], "kind": t["kind"], "summary": t["summary"]} for t in state.trials],
        "steps": len([e for e in state.log if e.get("tool")]),
        "log": state.log,
    }


async def _loop(config, state: _State, system: str, task: str) -> None:
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    from src.llm import build_model

    step_ref = [0]
    tools = _tools(state, step_ref)
    by_name = {t.name: t for t in tools}

    async def build_all():
        return (await build_model(config)).bind_tools(tools)

    async def build_finish():
        # 手数を使い切ったとき: 結論のツールだけを持たせて出させる
        return (await build_model(config)).bind_tools([by_name["submit_fix"], by_name["report_blockers"]], tool_choice="required")

    messages: list = [SystemMessage(content=with_output_language(system)), HumanMessage(content=task)]
    nudges = 0
    finishing = 0
    while state.final is None:
        if state.runner.should_stop():
            raise evaluation.EvaluationStopped()
        step_ref[0] += 1
        if step_ref[0] > state.limits.max_steps:
            finishing += 1
            if finishing > 2:
                raise LabLLMError("The fixing agent did not conclude within its step budget.")
            messages.append(HumanMessage(content=_FINISH_NOW))
            build = build_finish
        else:
            build = build_all
        _compact(messages, state.limits.context_chars)
        # 応答が反復ループで打ち切られたら作り直してやり直す（llm.invoke_resilient）
        response = await invoke_resilient(build, messages)
        messages.append(response)
        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            text = strip_thinking(response.content if isinstance(response.content, str) else str(response.content))
            state.log.append({"step": step_ref[0], "text": text[:2000]})
            nudges += 1
            if nudges > 3:
                raise LabLLMError("The fixing agent stopped calling tools without concluding.")
            messages.append(
                HumanMessage(content="Continue by calling a tool. When you have concluded, call submit_fix or report_blockers.")
            )
            continue
        for call in calls:
            name = call.get("name")
            state.runner.on_step(step_ref[0], name or "")
            tool = by_name.get(name)
            if tool is None:
                result = f"error: unknown tool {name!r}"
            else:
                try:
                    result = await asyncio.to_thread(tool.invoke, call.get("args") or {})
                except evaluation.EvaluationStopped:
                    raise
                except Exception as e:  # noqa: BLE001 - 引数の形の誤り等は AI に見せて直させる
                    result = f"error: {type(e).__name__}: {e}"
            state.log.append({"step": step_ref[0], "tool": name, "args": _short(call.get("args") or {}, 1500), "result": str(result)[:3000]})
            messages.append(ToolMessage(content=str(result), tool_call_id=call.get("id") or name or "call"))
            if state.final is not None:
                break
