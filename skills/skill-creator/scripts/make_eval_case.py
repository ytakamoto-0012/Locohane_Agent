"""ドラフトスキルの eval ケース（evals/case_schema.py 互換の yaml）を1件作る。

skill-creator スキルの実行スクリプト（progressive disclosure 第3段階）。

    python make_eval_case.py --name my-skill --case-id 001_basic \\
        --turns '["ユーザーが実際に打ちそうな発話"]' \\
        --expect '{"tool_call_args_contains": {"read_skill": {"skill_name": "my-skill"}}}'

作成先はドラフトの中の `evals/<case-id>.yaml`。run_isolated_eval.py で回し、
昇格（promote-skill）時には evals/cases/<スキル名>/ へ移される。評価中の
スキル名はドラフト名（ユーザー名付き）ではなくスキル名そのもの（例: my-skill）
になるため、expect でもスキル名だけを書く。

YAML の手書き生成は特殊文字のエスケープで壊れやすいため、PyYAML には
依存せず「JSON は YAML のサブセットである」という性質を使い、case 内容を
JSON としてシリアライズしてそのまま .yaml として書き出す。

自己完結（標準ライブラリのみ）。依存なし。
"""

from __future__ import annotations

import argparse
import json
import re

from _common import DRAFT_EVALS_DIRNAME, SkillCreatorError, draft_context, print_json, resolve_draft, run_main, touch_meta

_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,80}$")


def _load_json(raw: str | None, label: str):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SkillCreatorError(f"{label} cannot be parsed as JSON: {e}") from e


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="draft name (for your own drafts, just the skill name)")
    parser.add_argument("--case-id", required=True, help="case ID (also used as the file name; e.g. 001_basic)")
    parser.add_argument("--turns", required=True, help='JSON array of user messages. Example: ["..."]')
    parser.add_argument("--expect", default=None, help="part of Expect as a JSON object")
    parser.add_argument("--judge", default=None, help="free-text judging instructions (judged by reading the transcript)")
    parser.add_argument("--auto-approve", choices=["true", "false"], default="true")
    parser.add_argument("--scripted-text-answers", default=None, help="JSON array")
    parser.add_argument("--work-dir", default=None, help="fixture to use as the working folder (relative to the project root)")
    parser.add_argument("--timeout-seconds", type=int, default=None)
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    if not _CASE_ID_RE.match(args.case_id):
        raise SkillCreatorError(f"Specify --case-id with letters, digits, _ and - only: {args.case_id!r}")
    turns = _load_json(args.turns, "--turns")
    if not isinstance(turns, list) or not turns or not all(isinstance(t, str) for t in turns):
        raise SkillCreatorError("Make --turns a JSON array with at least 1 string.")
    expect = _load_json(args.expect, "--expect")
    if expect is None and not args.judge:
        raise SkillCreatorError("Specify --expect or --judge (or both).")
    scripted = _load_json(args.scripted_text_answers, "--scripted-text-answers") or []

    ctx = draft_context()
    ref = resolve_draft(ctx, args.name, "write")
    case_dict = {
        "id": args.case_id,
        "target": ref.name,
        "turns": turns,
        "expect": expect,
        "judge": args.judge,
        "auto_approve": args.auto_approve == "true",
        "scripted_text_answers": scripted,
        "work_dir": args.work_dir,
        "timeout_seconds": args.timeout_seconds,
        "notes": args.notes,
    }
    cases_dir = ref.dir / DRAFT_EVALS_DIRNAME
    cases_dir.mkdir(exist_ok=True)
    case_path = cases_dir / f"{args.case_id}.yaml"
    case_path.write_text(json.dumps(case_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    touch_meta(ctx, ref)
    print_json({"draft": ref.full_name, "case_path": str(case_path), "case_id": args.case_id})
    return 0


if __name__ == "__main__":
    run_main(main)
