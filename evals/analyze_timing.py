"""config_timeouts ターゲットの実測結果から timeout系設定の推奨値を算出する。

使い方:
    python evals/analyze_timing.py config_timeouts
    python evals/analyze_timing.py config_timeouts --results-dir evals/results/config_timeouts/20260727_120000

`evals/run_all.py config_timeouts` が出力した `results.json`
（各要素が `evals/run_case.py` の結果 dict、`turn_timings` フィールドを含む）を
集計し、`config.ini` の現在値と比較した推奨値テーブルを標準出力へ表示する。

このスクリプトは config.ini を直接書き換えない（推奨値の提示のみ）。実際の
適用判断・編集は人間が行う（管理ツールで運用中のインスタンスは、管理ツール
から該当インスタンスの値を変更する）。
"""

from __future__ import annotations

import configparser
import json
import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Windows のコンソールコードページ（既定 cp932）で日本語の標準出力が
# 文字化けするのを防ぐ（evals/run_case.py・evals/run_all.py と同じ対策）。
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# (config.ini セクション, キー, turn_timings 内の集計フィールド, 倍率, 下限フロア秒)
_METRICS = [
    ("llm", "request_timeout_seconds", "max_llm_total_seconds", 1.5, 60),
    ("llm", "stream_chunk_timeout_seconds", "max_stream_chunk_gap_seconds", 2.0, 30),
    ("scripts", "timeout", "max_script_seconds", 1.5, 60),
]

# 推奨値と現在値の差がこの割合未満なら「変更不要」とし、測定誤差による
# 無意味なチャーン・振動を避ける。
_CHANGE_THRESHOLD_RATIO = 0.20


def _find_latest_results_dir(target: str) -> Path:
    """`evals/results/<target>/` 配下で最新（辞書順最大=タイムスタンプ最大）のディレクトリを返す。"""
    base = PROJECT_ROOT / "evals" / "results" / target
    if not base.is_dir():
        raise SystemExit(f"結果ディレクトリが見つかりません: {base}（先に run_all.py {target} を実行してください）")
    candidates = sorted((p for p in base.iterdir() if p.is_dir() and (p / "results.json").exists()))
    if not candidates:
        raise SystemExit(f"results.json を含むディレクトリが {base} 配下に見つかりません")
    return candidates[-1]


def _load_current_config_value(section: str, key: str) -> float | None:
    """config.ini から現在値を読む（存在しなければ None）。"""
    parser = configparser.ConfigParser()
    parser.read(PROJECT_ROOT / "config.ini", encoding="utf-8")
    if parser.has_option(section, key):
        try:
            return parser.getfloat(section, key)
        except ValueError:
            return None
    return None


def _collect_max(results: list[dict], field: str) -> float | None:
    """全ケース・全ターンの turn_timings から指定フィールドの最大値を集める。"""
    values: list[float] = []
    for case_result in results:
        for turn in case_result.get("turn_timings") or []:
            value = turn.get(field)
            if value is not None:
                values.append(value)
    return max(values) if values else None


def _recommend(measured_max: float | None, ratio: float, floor_seconds: float) -> float | None:
    if measured_max is None:
        return None
    return max(math.ceil(measured_max * ratio), floor_seconds)


def analyze(target: str, results_dir: Path | None) -> dict:
    """results_dir（省略時は最新）を読み込み、推奨値テーブルを構築する。"""
    resolved_dir = results_dir or _find_latest_results_dir(target)
    results = json.loads((resolved_dir / "results.json").read_text(encoding="utf-8"))

    rows = []
    for section, key, field, ratio, floor_seconds in _METRICS:
        current = _load_current_config_value(section, key)
        measured_max = _collect_max(results, field)
        recommended = _recommend(measured_max, ratio, floor_seconds)

        needs_change = False
        diff_ratio = None
        if current is not None and recommended is not None and current > 0:
            diff_ratio = (recommended - current) / current
            needs_change = abs(diff_ratio) >= _CHANGE_THRESHOLD_RATIO

        rows.append(
            {
                "section": section,
                "key": key,
                "current": current,
                "measured_max_seconds": measured_max,
                "recommended": recommended,
                "diff_ratio": diff_ratio,
                "needs_change": needs_change,
            }
        )

    return {"target": target, "results_dir": str(resolved_dir), "rows": rows}


def _render_markdown(report: dict) -> str:
    lines = [
        f"# config_timeouts 推奨値レポート（{report['target']}）",
        "",
        f"結果: `{report['results_dir']}`",
        "",
        "| 設定 | 現在値 | 実測最大値 | 推奨値 | 差分 | 変更要否（±20%基準） |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in report["rows"]:
        setting = f"[{row['section']}].{row['key']}"
        current = f"{row['current']:.0f}" if row["current"] is not None else "?"
        measured = f"{row['measured_max_seconds']:.1f}" if row["measured_max_seconds"] is not None else "実測なし"
        recommended = f"{row['recommended']:.0f}" if row["recommended"] is not None else "-"
        diff = f"{row['diff_ratio'] * 100:+.0f}%" if row["diff_ratio"] is not None else "-"
        verdict = "変更推奨" if row["needs_change"] else "変更不要"
        lines.append(f"| {setting} | {current} | {measured} | {recommended} | {diff} | {verdict} |")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) < 2:
        print("使い方: python evals/analyze_timing.py <target> [--results-dir <path>]", file=sys.stderr)
        return 2

    target = sys.argv[1]
    results_dir = None
    if "--results-dir" in sys.argv:
        idx = sys.argv.index("--results-dir")
        results_dir = PROJECT_ROOT / sys.argv[idx + 1]

    report = analyze(target, results_dir)
    print(_render_markdown(report))

    out_dir = Path(report["results_dir"])
    (out_dir / "recommendations.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n詳細: {out_dir / 'recommendations.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
