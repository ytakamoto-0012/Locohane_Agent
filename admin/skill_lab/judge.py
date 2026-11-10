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

import json
import re
import unicodedata

from src.config import Config

from . import evaluation
from .llm import LabLLMError, ask_json

_SYSTEM = """You are the judge of behavior checks of a business AI assistant.
Read what the assistant did in response to the user's instructions (tool calls and results) and its final answer,
and judge whether the judging viewpoint is satisfied in substance.
Judge the substance (the facts, numbers, files and actions the viewpoint asks for), not the wording:
a different phrasing, order, notation (full-width/half-width, thousands separators, date format) or extra explanation
is not a reason to fail. Fail it when something the viewpoint requires is missing, wrong or contradicted,
or when the record does not show that it was done.
Lines starting with "[output file]" list the files the assistant created or changed in the working folder (path and size,
and a summary for Excel files); they are evidence that those files exist. The contents of images and documents are not
shown, so do not fail only because you cannot see inside a file: judge from the tool calls and results that produced it
and from the final answer.
Output only JSON in the following format: {"pass": true or false, "reason": "the grounds for the verdict (concrete)"}"""

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
_HARD_RULES = ("tool_called_any", "tool_not_called")


def judge_run(
    config: Config, *, spec: str, judge_instruction: str, result: dict, excerpt_chars: int, failed_checks: dict | None = None
) -> dict:
    """1回分の結果を判定する。{"pass": bool, "reason": str}（LLM が使えなければ不合格扱い）。

    failed_checks: 完全一致の照合で落ちた条件（言葉・引数）。あれば判断の観点と合わせて見せ、
        観点が許す違い（表記・言い換え等）なら合格にできるようにする。
    """
    checks_block = (
        "## Exact-text checks that failed (expected words/values not found verbatim)\n"
        f"{json.dumps(failed_checks, ensure_ascii=False, default=str)[:3000]}\n"
        "Pass them if the answer gives the same information in substance (different notation, separators or wording), "
        "unless the judging viewpoint requires the exact text.\n\n"
        if failed_checks
        else ""
    )
    user = (
        f"## Spec of the skills and sub-agents\n{spec[:6000]}\n\n"
        f"## Judging viewpoint\n{judge_instruction}\n\n"
        f"{checks_block}"
        f"## Execution record\n{evaluation.transcript_excerpt(result, excerpt_chars)}\n\n"
        f"## Final answer\n{(result.get('final_answer') or '')[:4000]}"
    )
    return _ask_verdict(config, _SYSTEM, user, "AI judge")


def _ask_verdict(config: Config, system: str, user: str, label: str) -> dict:
    try:
        data = ask_json(config, system, user)
    except LabLLMError as e:
        return {"pass": False, "reason": f"Could not run the {label}: {e}"}
    if not isinstance(data, dict) or not isinstance(data.get("pass"), bool):
        return {"pass": False, "reason": f"Invalid format of the {label} response: {str(data)[:300]}"}
    return {"pass": data["pass"], "reason": str(data.get("reason", ""))}


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


def review_rules(config: Config, result: dict, excerpt_chars: int) -> dict | None:
    """機械判定で落ちた回を見直す。見直しの対象外（ツールの呼び方で落ちた等）なら None。

    ケースに「AI が判断する点」（judge）があれば、作成者の判定基準として LLM の見直しにも渡す
    （以前は渡しておらず、「意味が同じなら合格」と書いても言葉の照合の見直しには効かなかった）。

    Returns:
        {"pass": bool, "how": "normalized" | "ai", "rules": [見直したルール名], "reason": str}
    """
    failed = {k: v for k, v in (result.get("rule_results") or {}).items() if isinstance(v, dict) and not v.get("pass")}
    if not failed or any(k in _HARD_RULES for k in failed):
        return None
    final_answer = result.get("final_answer") or ""
    remaining = {k: v for k, v in failed.items() if not _passes_normalized(k, v, final_answer)}
    if not remaining:
        return {
            "pass": True,
            "how": "normalized",
            "rules": sorted(failed),
            "reason": "Only notation differed (full/half width, spaces, thousands separators, path separators or case).",
        }
    checks = {k: {key: v.get(key) for key in ("detail", "missing", "found", "expected") if v.get(key) is not None} for k, v in remaining.items()}
    author_criteria = (result.get("judge") or "").strip()
    user = (
        f"## Failed exact checks\n{json.dumps(checks, ensure_ascii=False, default=str)[:4000]}\n\n"
        + (
            f"## Judging criteria written by the case author (follow these when they say how strict to be)\n{author_criteria[:2000]}\n\n"
            if author_criteria
            else ""
        )
        + f"## Execution record\n{evaluation.transcript_excerpt(result, excerpt_chars)}\n\n"
        f"## Final answer\n{final_answer[:4000]}"
    )
    verdict = _ask_verdict(config, _RULE_REVIEW_SYSTEM, user, "rule review")
    return {**verdict, "how": "ai", "rules": sorted(failed)}


def judge_results(config: Config, *, spec: str, results: list[dict], excerpt_chars: int) -> list[dict]:
    """全回の結果を判定し、各回の最終判定（outcome: pass/fail/error と AI judge の結果）を返す。

    機械判定の文字列・引数の照合だけで落ちた回は review_rules() で見直し、中身が合っていれば
    judge（あれば）へ進める。見直しの結果は rule_review に残す（人がレビューで確認するため）。
    """
    verdicts = []
    for r in results:
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
                    failed_checks = {
                        k: {key: v.get(key) for key in ("detail", "missing", "found", "expected") if v.get(key) is not None}
                        for k, v in (r.get("rule_results") or {}).items()
                        if isinstance(v, dict) and not v.get("pass")
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
        verdicts.append(entry)
    return verdicts


def all_passed(verdicts: list[dict]) -> bool:
    return bool(verdicts) and all(v["outcome"] == "pass" for v in verdicts)
