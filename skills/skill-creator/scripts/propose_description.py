"""ドラフトスキルの現在の description とトリガー評価の失敗例から、改善案をLLMに提案させる。

skill-creator スキルの実行スクリプト（progressive disclosure 第3段階）。

    python propose_description.py start --name my-skill --failed-queries <failed.json>
    python propose_description.py status --name my-skill --job-id <job_id>

現在の description はドラフトの SKILL.md から読む。--failed-queries には
run_trigger_eval.py status の per_query から matched: false の項目を抜き出した
JSON配列のファイルを渡す。単発の chat completion 呼び出しだが、ローカルLLMの
応答生成に時間がかかる場合に備え、他の評価系スクリプトと同様 start/status の
非同期パターンにしている。提案は自動では反映しない（write_draft_file.py で書く）。

実際のLLM呼び出しは `_llm_helper.py`（Locohane本体のPython実行環境が
必要）をサブプロセスとして起動して行う。

自己完結（標準ライブラリのみ）。依存なし。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from _common import (
    DEFAULT_MAIN_PYTHON,
    SkillCreatorError,
    draft_context,
    is_process_alive,
    load_job,
    log_tail,
    parse_frontmatter,
    print_json,
    resolve_draft,
    run_main,
    start_background,
    workspace_dir,
)

_SYSTEM_PROMPT = (
    "You are an assistant that improves the description in SKILL.md for an Agent Skills system for local LLMs. "
    "The description is the only clue the LLM uses to decide whether to use this skill, and it must include both "
    "\"what it does\" and \"for what kind of user messages it should be used\". "
    "Keep it 1-1024 characters, in the same language as the current description, keep the existing tone, and improve it "
    "by adding concrete patterns of user messages and context based on the failures. Output only the body of the improved description, "
    "with no preamble, explanation or code block fences."
)


def _build_user_prompt(skill_name: str, current_description: str, failed_queries: list[dict]) -> str:
    lines = [
        f"Skill name: {skill_name}",
        f"Current description:\n{current_description}",
        "",
        "The following are queries whose result differed from the expectation in the trigger accuracy evaluation"
        " (either should_trigger=true but it was not actually used, or"
        " should_trigger=false but it was actually used):",
    ]
    for item in failed_queries:
        expectation = "should be used" if item.get("should_trigger") else "should not be used"
        lines.append(f"- Example message: {item.get('query')!r} / expected: {expectation} / actual trigger rate: {item.get('trigger_rate')}")
    lines.append("")
    lines.append("Based on these failures, propose one improved description.")
    return "\n".join(lines)


def _cmd_start(args: argparse.Namespace) -> int:
    try:
        failed_queries = json.loads(Path(args.failed_queries).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SkillCreatorError(f"Cannot read --failed-queries: {e}") from e
    ctx = draft_context()
    ref = resolve_draft(ctx, args.name, "write")
    fm = parse_frontmatter((ref.dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")) or {}
    user_prompt = _build_user_prompt(ref.name, fm.get("description", ""), failed_queries)

    ws = workspace_dir(ctx, ref)
    fd, input_path = tempfile.mkstemp(suffix=".json", dir=str(ws))
    with open(fd, "w", encoding="utf-8") as f:
        json.dump({"system": _SYSTEM_PROMPT, "user": user_prompt}, f, ensure_ascii=False)
    helper_path = Path(__file__).resolve().parent / "_llm_helper.py"
    print_json(start_background([DEFAULT_MAIN_PYTHON, str(helper_path), input_path], ws))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    ctx = draft_context()
    ref = resolve_draft(ctx, args.name, "write")
    job = load_job(workspace_dir(ctx, ref), args.job_id)
    if is_process_alive(job["pid"]):
        print_json({"job_id": args.job_id, "status": "running", "pid": job["pid"]})
        return 0
    lines = [ln for ln in log_tail(job).splitlines() if ln.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        result = {}
    if "text" not in result:
        print_json(
            {
                "job_id": args.job_id,
                "status": "finished",
                "error": result.get("error", "Could not get a proposal"),
                "log_tail": "\n".join(lines[-20:]),
            }
        )
        return 1
    print_json({"job_id": args.job_id, "status": "finished", "proposed_description": result["text"].strip()})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--name", required=True, help="draft name (for your own drafts, just the skill name)")
    p_start.add_argument("--failed-queries", required=True, help="file with a JSON array of the matched:false entries extracted from per_query")

    p_status = sub.add_parser("status")
    p_status.add_argument("--name", required=True)
    p_status.add_argument("--job-id", required=True)

    args = parser.parse_args()
    return _cmd_start(args) if args.command == "start" else _cmd_status(args)


if __name__ == "__main__":
    run_main(main)
