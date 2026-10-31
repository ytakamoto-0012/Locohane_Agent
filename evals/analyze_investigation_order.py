"""複数回反復実行した001番ケースの結果(1行1JSON)から、
「create_plan を呼ぶ前に dispatch_agent(explore) への委譲があったか」を
機械的に集計するための一時的な検証スクリプト（tune-prompt本体の対象外）。

使い方:
    python evals/analyze_investigation_order.py <result_json_dir_or_glob...>

各ファイルは `python -m evals.run_case ...` の標準出力を1件1行でリダイレクト
したものを想定（1ファイル1JSON行）。
"""

from __future__ import annotations

import glob
import json
import sys


def classify(result: dict) -> tuple[str, str]:
    transcript = result.get("transcript", [])
    plan_idx = None
    explore_idx = None
    for i, entry in enumerate(transcript):
        for tc in entry.get("tool_calls") or []:
            name = tc.get("name")
            if name == "create_plan" and plan_idx is None:
                plan_idx = i
            if (
                name == "dispatch_agent"
                and (tc.get("args") or {}).get("agent_type") == "explore"
                and explore_idx is None
            ):
                explore_idx = i
        if plan_idx is not None:
            break

    if plan_idx is None:
        return "INCONCLUSIVE", "create_plan が呼ばれていない（別の分岐に進んだ可能性）"
    if explore_idx is None:
        return "FAIL", f"create_plan(idx={plan_idx}) 前に explore への委譲なし"
    if explore_idx < plan_idx:
        return "PASS", f"explore(idx={explore_idx}) -> create_plan(idx={plan_idx})"
    return "FAIL", f"explore(idx={explore_idx}) が create_plan(idx={plan_idx}) より後"


def main() -> None:
    paths: list[str] = []
    for pattern in sys.argv[1:]:
        paths.extend(sorted(glob.glob(pattern)))

    counts = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0, "ERROR": 0}
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
            # run_case.py の標準出力は1行JSONだが、周辺にログ行が混ざる場合が
            # あるので、最後に現れる正しいJSON行を探す。
            result = None
            for line in reversed(text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    result = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
            if result is None:
                raise ValueError("JSON行が見つからない")
            if result.get("error"):
                counts["ERROR"] += 1
                print(f"{path}: ERROR ({result.get('error')}: {result.get('detail','')[:200]})")
                continue
            verdict, detail = classify(result)
            counts[verdict] += 1
            cutoffs = result.get("turn_cutoffs")
            cutoff_note = f" [turn_cutoffs={cutoffs}]" if cutoffs else ""
            print(f"{path}: {verdict} - {detail}{cutoff_note}")
        except Exception as e:  # noqa: BLE001
            counts["ERROR"] += 1
            print(f"{path}: ERROR (読み込み失敗: {e})")

    total = sum(counts.values())
    print("\n--- 集計 ---")
    for k, v in counts.items():
        pct = f"{100*v/total:.1f}%" if total else "-"
        print(f"{k}: {v}/{total} ({pct})")


if __name__ == "__main__":
    main()
