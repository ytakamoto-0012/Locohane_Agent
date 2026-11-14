"""AI judge（judge 付きのケースを、研究室の LLM が仕様と judge の観点で判定する）と、機械判定の見直し。

最終的な合否は人がレビューで確認する（承認が judge 合格の扱いになる）。ここでの判定は、
修正ループが「直ったかどうか」を決めるためと、人が確認するときの手がかりにするため。

機械判定（evals/run_case.py の expect）のうち、文字列・引数の照合は完全一致なので、
「1,234円」と「1234円」、全角と半角、`C:/a` と `C:\\a` のような表記の違いだけで不合格になり、
何度直しても合格しなかった。研究室では次の順で見直す（evals/run_all.py 自体の判定は変えない）。

1. どのツールを呼んだか・呼ばなかったか（tool_called_any / tool_not_called）は機械判定のまま。
2. 文字列・引数の照合（response_contains / response_not_contains / tool_call_args_contains）で
   落ちたものは、まず表記をそろえて照合し直す（全角半角・空白・桁区切り・パス区切り・大文字小文字）。
3. それでも合わないものは、LLM に「表記は違っても中身が同じか」を判定させる。LLM が使えない・
   形式が不正なら不合格のまま（合格にしない）。
"""

from __future__ import annotations

import contextvars
import json
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from src.config import Config

from . import evaluation
from .llm import LabLLMError, ask_json, current_output_language

_SYSTEM = """You are the judge of behavior checks of a business AI assistant.
Read what the assistant did in response to the user's instructions (tool calls and results) and its final answer,
and judge whether the judging viewpoint is satisfied in substance.
What you may fail it for is limited to:
- something the judging viewpoint requires that is missing, wrong or contradicted, or not shown to have been done in the record;
- something listed under "Expected output" of the spec that this case asks for and that is missing or wrong;
- something listed under "Must not do" of the spec (or forbidden by the viewpoint) that the assistant did.
Anything else is not a reason to fail, even if you would have done it differently or think it risky: an approach,
tool choice, order of steps, extra step or explanation, output format or concern that neither the viewpoint nor
"Expected output"/"Must not do" mentions. "Purpose" and "When to use" of the spec are background, not pass conditions.
Judge the substance (the facts, numbers, files and actions asked for), not the wording: a different phrasing, order,
notation (full-width/half-width, thousands separators, date format) or extra explanation is not a reason to fail.
Lines starting with "[AI thinking]" are the assistant's internal thoughts (the end of them); they are not part of the answer,
so do not pass or fail it for what it only thought.
Lines starting with "[harness note]" are facts measured by the evaluation harness (cut-offs, time taken, refusals by settings);
a cut-off alone is not a reason to fail when what the viewpoint asks for was still done.
Lines starting with "[sub-agent ...]" are tool calls made inside a sub-agent the assistant delegated to with
dispatch_agent; they count as work done by the assistant (for example, a skill read with read_skill inside a sub-agent
was read).
Lines starting with "[output file]" list the files the assistant created or changed in the working folder (path and size,
and a summary for Excel files); they are evidence that those files exist. The contents of images and documents are not
shown, so do not fail only because you cannot see inside a file: judge from the tool calls and results that produced it
and from the final answer.
Output only JSON in the following format:
{"pass": true or false,
 "basis": "when failing: quote the requirement of the viewpoint or the item of the spec that was not met or was violated (empty when passing)",
 "reason": "the grounds for the verdict (concrete)"}"""

# AI judge が根拠（観点・仕様のどの項目か）を示さずに不合格にしたときの扱い（_ask_verdict）。
# 観点にも「やってはいけないこと」にも無いことで不合格にするケースがあったため（2026-11-13 ユーザー報告）、
# 根拠を引用できない不合格は合格扱いにする（最終的な合否は人がレビューで確認する）。
_NO_BASIS_NOTE = {
    "ja": "（AI judge は判断の観点・仕様のどの項目に反するかを示さずに不合格としたため、合格扱いにしました）",
    "en": "(The AI judge failed it without citing which requirement of the viewpoint or the spec was not met, so it was treated as a pass.)",
}

_RULE_REVIEW_SYSTEM = """You review mechanical checks of a business AI assistant that compare text exactly.
An exact comparison failed. Decide, for each failed check, whether the assistant still meets what the check
is meant to confirm, allowing differences of wording and notation only.
- "response_contains": pass if the final answer states the same information in substance
  (e.g. "1,234円" for "1234円", "2027年1月1日" for "2027/1/1", a synonym or a fuller phrase). Fail if the
  information is missing, different (another number, another name) or only appears as something not done.
- "response_not_contains": pass if the forbidden text appears but the answer does not actually say what it
  forbids (e.g. it is quoted, negated or part of another word). Fail if the answer says it.
- "tool_call_args_contains": pass if one of the calls has arguments that mean the same values (the same path
  written differently, the same number as text, the same option). Fail if the values differ in meaning.
Output only JSON: {"pass": true or false, "reason": "for each check, why it passes or fails (concrete)"}"""

# 機械判定のまま見直さないルール（どのツールを呼んだかは表記ゆれが無い）
_HARD_RULES = ("tool_called_any", "tool_not_called", "verify_script")
# 前方一致で機械判定のまま見直さないルール（使ってはいけない呼び出しは、引数の表記ゆれで見逃すと危ない）
_HARD_RULE_PREFIXES = ("tool_call_args_not_contains:",)


def _is_hard(rule: str) -> bool:
    return rule in _HARD_RULES or rule.startswith(_HARD_RULE_PREFIXES)


def judge_run(
    config: Config, *, spec: str, judge_instruction: str, result: dict, excerpt_chars: int, failed_checks: dict | None = None
) -> dict:
    """1回分の結果を判定する。{"pass": bool, "reason": str}（LLM が使えなければ不合格扱い）。

    failed_checks: 完全一致の照合で落ちた条件（言葉・引数）。あれば判断の観点と合わせて見せ、
        観点が許す違い（表記・言い換え等）なら合格にできるようにする。
    """
    lim = evaluation.current_limits()
    checks_block = (
        "## Exact-text checks that failed (expected words/values not found verbatim)\n"
        f"{json.dumps(failed_checks, ensure_ascii=False, default=str)[: lim.judge_checks_chars]}\n"
        "Pass them if the answer gives the same information in substance (different notation, separators or wording), "
        "unless the judging viewpoint requires the exact text.\n\n"
        if failed_checks
        else ""
    )
    user = (
        f"## Spec of the skills and sub-agents\n{spec[: lim.judge_spec_chars]}\n\n"
        f"## Judging viewpoint\n{judge_instruction}\n\n"
        f"{checks_block}"
        f"## Execution record\n{evaluation.transcript_excerpt(result, excerpt_chars)}\n\n"
        f"## Final answer\n{(result.get('final_answer') or '')[: lim.judge_answer_chars]}"
    )
    return _ask_verdict(config, _SYSTEM, user, "AI judge", require_basis=True)


def _ask_verdict(config: Config, system: str, user: str, label: str, *, require_basis: bool = False) -> dict:
    try:
        data = ask_json(config, system, user)
    except LabLLMError as e:
        return {"pass": False, "reason": f"Could not run the {label}: {e}"}
    if not isinstance(data, dict) or not isinstance(data.get("pass"), bool):
        return {"pass": False, "reason": f"Invalid format of the {label} response: {str(data)[:300]}"}
    reason = str(data.get("reason", ""))
    basis = str(data.get("basis") or "").strip()
    if require_basis and not data["pass"]:
        if not basis:
            return {"pass": True, "reason": f"{reason} {_NO_BASIS_NOTE.get(current_output_language(), _NO_BASIS_NOTE['ja'])}".strip()}
        return {"pass": False, "reason": reason, "basis": basis}
    return {"pass": data["pass"], "reason": reason}


# --- 機械判定の見直し --------------------------------------------------------------

_SPACES_RE = re.compile(r"\s+")
_THOUSANDS_RE = re.compile(r"(?<=\d)[,，](?=\d{3}(?!\d))")


def normalize_text(value) -> str:
    """表記の違いをそろえた文字列（全角半角・空白・桁区切り・パス区切り・大文字小文字）。"""
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    text = _THOUSANDS_RE.sub("", text)
    text = text.replace("\\", "/")
    text = _SPACES_RE.sub("", text)
    return text.casefold()


def _normalized_value(value):
    """引数の照合用。文字列・数値は normalize_text、入れ子は中身ごとそろえる。"""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (str, int, float)):
        return normalize_text(value)
    if isinstance(value, dict):
        return {k: _normalized_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized_value(v) for v in value]
    return normalize_text(value)


# 区切り記号（記号・空白・_）。数字どうしの間（1.5・2027/1/1 の区切り）は意味が変わるため残す。
_LOOSE_SEPARATORS_RE = re.compile(r"(?<!\d)[\W_]+|[\W_]+(?!\d)")


def loose_text(value) -> str:
    """normalize_text に加えて、文字と数字の間などの区切りの違いもそろえた文字列。

    「ver_57」と「ver.57」「ver: 57」「Ver-57」のような書き方の違いだけで、言葉の条件が
    何度直しても合格しなかった（2026-11-11 ユーザー報告）。数字どうしの間の区切りは残すので、
    「1.5」と「15」は別のままにする。
    """
    return _LOOSE_SEPARATORS_RE.sub("", normalize_text(value))


def _passes_normalized(rule: str, detail: dict, final_answer: str) -> bool:
    """表記をそろえれば合う失敗か（照合し直すための値が結果に無ければ False）。"""
    if rule == "response_contains":
        missing = detail.get("missing")
        if not missing:
            return False
        answer, loose_answer = normalize_text(final_answer), loose_text(final_answer)
        return all(normalize_text(s) in answer or (loose_text(s) and loose_text(s) in loose_answer) for s in missing)
    if rule.startswith("tool_call_args_contains:"):
        expected, calls = detail.get("expected"), detail.get("calls")
        if not isinstance(expected, dict) or not isinstance(calls, list):
            return False
        want = {k: _normalized_value(v) for k, v in expected.items()}
        return any(
            isinstance(call, dict) and all(_normalized_value(call.get(k)) == v for k, v in want.items()) for call in calls
        )
    # response_not_contains は表記をそろえても「含まれている」ことは変わらない（LLM の見直しだけ）
    return False


def _subagent_calls(result: dict) -> list[dict]:
    """サブエージェントの中で成功したツール呼び出し（evals/run_case.py の subagent_tool_calls）。"""
    return [c for c in result.get("subagent_tool_calls") or [] if isinstance(c, dict) and c.get("name") and not c.get("is_error")]


def _passes_with_subagents(rule: str, detail: dict, result: dict) -> bool:
    """サブエージェントの中の呼び出しも数えれば満たすか（どのツールを呼んだか・引数の条件だけ）。

    機械判定（evals/run_case.py の _evaluate_expect）はメインエージェントの呼び出ししか見ないため、
    検証対象のスキルを委譲先（worker 等）が read_skill して正しく仕上げても「スキルを読んでいない」として
    不合格になり、しかも tool_called_any は見直しの対象外だった（2026-11-13 ユーザー報告）。
    tool_not_called はメインエージェントが直接呼ばなかったか（委譲したか）を確かめる用途があるため数えない。
    """
    calls = _subagent_calls(result)
    if not calls:
        return False
    if rule == "tool_called_any":
        expected = detail.get("expected")
        return isinstance(expected, list) and any(c["name"] in expected for c in calls)
    if rule.startswith("tool_call_args_contains:"):
        tool_name = rule.split(":", 1)[1]
        expected = detail.get("expected")
        if not isinstance(expected, dict):
            return False
        want = {k: _normalized_value(v) for k, v in expected.items()}
        return any(
            c["name"] == tool_name
            and isinstance(c.get("args"), dict)
            and all(_normalized_value(c["args"].get(k)) == v for k, v in want.items())
            for c in calls
        )
    return False


_REVIEW_NOTES = {
    "normalized": {
        "ja": "表記の違い（全角半角・空白・桁区切り・パスの区切り・大文字小文字）だけでした。",
        "en": "Only notation differed (full/half width, spaces, thousands separators, path separators or case).",
    },
    "subagent": {
        "ja": "委譲先のサブエージェントの中で呼んでいました: {0}",
        "en": "Called inside a sub-agent the assistant delegated to: {0}",
    },
}


def _note(key: str, *args) -> str:
    texts = _REVIEW_NOTES[key]
    return texts.get(current_output_language(), texts["ja"]).format(*args)


def review_rules(config: Config, result: dict, excerpt_chars: int) -> dict | None:
    """機械判定で落ちた回を見直す。見直しの対象外（ツールの呼び方で落ちた等）なら None。

    まず、どのツールを呼んだか・引数の条件を、サブエージェントの中の呼び出しも含めて照合し直す。
    ケースに「AI が判断する点」（judge）があれば、作成者の判定基準として LLM の見直しにも渡す
    （以前は渡しておらず、「意味が同じなら合格」と書いても言葉の照合の見直しには効かなかった）。

    Returns:
        {"pass": bool, "how": "subagent" | "normalized" | "ai", "rules": [見直したルール名], "reason": str}
    """
    all_failed = {k: v for k, v in (result.get("rule_results") or {}).items() if isinstance(v, dict) and not v.get("pass")}
    if not all_failed:
        return None
    by_subagent = sorted(k for k, v in all_failed.items() if _passes_with_subagents(k, v, result))
    failed = {k: v for k, v in all_failed.items() if k not in by_subagent}
    if any(_is_hard(k) for k in failed):
        return None
    subagent_note = _note("subagent", ", ".join(by_subagent)) if by_subagent else ""
    if not failed:
        return {"pass": True, "how": "subagent", "rules": by_subagent, "reason": subagent_note}
    final_answer = result.get("final_answer") or ""
    remaining = {k: v for k, v in failed.items() if not _passes_normalized(k, v, final_answer)}
    if not remaining:
        return {
            "pass": True,
            "how": "normalized",
            "rules": sorted(all_failed),
            "reason": " ".join(x for x in (subagent_note, _note("normalized")) if x),
        }
    checks = {k: {key: v.get(key) for key in ("detail", "missing", "found", "expected") if v.get(key) is not None} for k, v in remaining.items()}
    author_criteria = (result.get("judge") or "").strip()
    lim = evaluation.current_limits()
    user = (
        f"## Failed exact checks\n{json.dumps(checks, ensure_ascii=False, default=str)[: lim.judge_checks_chars]}\n\n"
        + (
            f"## Judging criteria written by the case author (follow these when they say how strict to be)\n{author_criteria[: lim.judge_criteria_chars]}\n\n"
            if author_criteria
            else ""
        )
        + f"## Execution record\n{evaluation.transcript_excerpt(result, excerpt_chars)}\n\n"
        f"## Final answer\n{final_answer[: lim.judge_answer_chars]}"
    )
    verdict = _ask_verdict(config, _RULE_REVIEW_SYSTEM, user, "rule review")
    if subagent_note:
        verdict = {**verdict, "reason": f"{subagent_note} {verdict['reason']}"}
    return {**verdict, "how": "ai", "rules": sorted(failed)}


def _judge_one(config: Config, spec: str, r: dict, excerpt_chars: int) -> dict:
    """1回分の結果の最終判定（機械判定 → 表記ゆれ・LLM の見直し → AI judge）。"""
    outcome = evaluation.classify(r)
    entry = {"case_id": r.get("case_id"), "repeat_index": r.get("repeat_index", 1), "outcome": outcome}
    failed_checks: dict = {}
    if outcome == "fail":
        review = review_rules(config, r, excerpt_chars)
        if review is not None:
            entry["rule_review"] = review
            if review["pass"]:
                outcome = "judge" if r.get("judge") else "pass"
            elif r.get("judge"):
                # 言葉・引数の照合（表記ゆれのありうる条件）だけで落ち、「AI が判断する点」が書かれているなら、
                # AI judge まで進めてその判断を優先する（落ちた照合も見せる）。以前はここで不合格が確定し、
                # 「意味が同じなら合格」等の判断の観点が一度も使われなかった。
                outcome = "judge"
                # サブエージェントの中で満たしていた条件は見せない（review の rules は残った条件だけ）。
                failed_checks = {
                    k: {key: v.get(key) for key in ("detail", "missing", "found", "expected") if v.get(key) is not None}
                    for k, v in (r.get("rule_results") or {}).items()
                    if isinstance(v, dict) and not v.get("pass") and k in review.get("rules", [])
                }
            entry["outcome"] = outcome
    if outcome == "judge":
        ai = judge_run(
            config,
            spec=spec,
            judge_instruction=r.get("judge") or "",
            result=r,
            excerpt_chars=excerpt_chars,
            failed_checks=failed_checks,
        )
        entry["ai_judge"] = ai
        entry["outcome"] = "pass" if ai["pass"] else "fail"
    return entry


def judge_results(
    config: Config,
    *,
    spec: str,
    results: list[dict],
    excerpt_chars: int,
    on_progress: Callable[[int, int, str], None] | None = None,
    max_workers: int = 1,
) -> list[dict]:
    """全回の結果を判定し、各回の最終判定（outcome: pass/fail/error と AI judge の結果）を返す（結果と同じ順）。

    機械判定の文字列・引数の照合だけで落ちた回は review_rules() で見直し、中身が合っていれば
    judge（あれば）へ進める。見直しの結果は rule_review に残す（人がレビューで確認するため）。
    on_progress: 各回の判定が終わるたびに (終わった件数, 全件数, そのケース ID) で呼ぶ（画面に進み具合を出すため）。
    max_workers: 同時に判定する数。評価の LLM を持つスキル調整ワーカーの [llm] max_concurrent_requests
        （推論サーバーの同時実行数）。1 以下なら1件ずつ。ケース・トライアウトの回数が多いとき、判定を待つ時間を縮める。
    """
    total = len(results)
    done = 0
    lock = threading.Lock()

    def _run(r: dict) -> dict:
        nonlocal done
        entry = _judge_one(config, spec, r, excerpt_chars)
        if on_progress is not None:
            with lock:
                done += 1
                on_progress(done, total, str(r.get("case_id") or ""))
        return entry

    workers = min(max(int(max_workers), 1), max(total, 1))
    if workers <= 1:
        return [_run(r) for r in results]
    # ContextVar（使用量の記録・出力言語・切り詰めの設定）はスレッドへ引き継がれないため、呼び出し側の文脈で動かす。
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lab-judge") as pool:
        futures = [pool.submit(contextvars.copy_context().run, _run, r) for r in results]
        return [f.result() for f in futures]


def all_passed(verdicts: list[dict]) -> bool:
    return bool(verdicts) and all(v["outcome"] == "pass" for v in verdicts)
