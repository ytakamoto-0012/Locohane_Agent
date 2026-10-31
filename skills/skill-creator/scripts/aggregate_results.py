"""複数の評価結果（with_skill と without_skill など）をケースごとに比べる Markdown を作る。

skill-creator スキルの実行スクリプト（progressive disclosure 第3段階）。

    python aggregate_results.py --name my-skill \\
        --input with_skill=<results.json> --input without_skill=<results.json>

`--input` には run_isolated_eval.py status が返す results_path（evals/run_all.py が
書く results.json）を `<ラベル>=<パス>` で2つ以上渡す。ケースごとに合格回数・
平均トークン数を並べる。judge 付きのケースは合否を決めず「judge待ち」として数える
（judge は transcript を読んで判断する）。レポートはドラフトの workspace に保存する。

自己完結（標準ライブラリのみ）。依存なし。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from _common import SkillCreatorError, draft_context, print_json, resolve_draft, run_main, workspace_dir


def _classify(result: dict) -> str:
    if result.get("error"):
        return "error"
    if result.get("rules_pass") is False:
        return "fail"
    if result.get("judge"):
        return "judge"
    return "pass"


def _summarize(results: list[dict]) -> dict[str, dict]:
    by_case: dict[str, dict] = {}
    for r in results:
        s = by_case.setdefault(r.get("case_id", "?"), {"runs": 0, "pass": 0, "fail": 0, "judge": 0, "error": 0, "tokens": []})
        s["runs"] += 1
        s[_classify(r)] += 1
        total = (r.get("token_usage_total") or {}).get("total_tokens")
        if total:
            s["tokens"].append(total)
    return by_case


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="draft name (for your own drafts, just the skill name)")
    parser.add_argument("--input", action="append", required=True, help="<label>=<path of results.json> (2 or more)")
    args = parser.parse_args()

    inputs: list[tuple[str, dict[str, dict]]] = []
    for item in args.input:
        label, sep, path = item.partition("=")
        if not sep or not label or not path:
            raise SkillCreatorError(f"Specify --input as <label>=<path>: {item!r}")
        try:
            results = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise SkillCreatorError(f"Cannot read {path}: {e}") from e
        inputs.append((label, _summarize(results if isinstance(results, list) else [results])))

    ctx = draft_context()
    ref = resolve_draft(ctx, args.name, "write")
    case_ids = sorted({cid for _, summary in inputs for cid in summary})
    lines = [f"# {ref.full_name} comparison of evaluation results", "", "| Case | Runs | Passed | Failed | Awaiting judge | Errors | Avg tokens |", "|---|---|---|---|---|---|---|"]
    for cid in case_ids:
        for label, summary in inputs:
            s = summary.get(cid)
            if s is None:
                lines.append(f"| {cid} | {label} | - | - | - | - | - |")
                continue
            avg = round(sum(s["tokens"]) / len(s["tokens"])) if s["tokens"] else "-"
            lines.append(f"| {cid} | {label} | {s['pass']}/{s['runs']} | {s['fail']} | {s['judge']} | {s['error']} | {avg} |")
    output = workspace_dir(ctx, ref) / f"comparison_{datetime.now():%Y%m%d_%H%M%S}.md"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print_json({"output_path": str(output), "cases": len(case_ids), "markdown": "\n".join(lines)})
    return 0


if __name__ == "__main__":
    run_main(main)
