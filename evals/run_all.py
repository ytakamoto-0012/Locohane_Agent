"""指定カテゴリの eval ケースを全件、または指定ケースのみ実行し、結果を集計する。

使い方:
    python evals/run_all.py system_prompt
    python evals/run_all.py system_prompt 001_annual_schedule_investigation_before_plan 005_glob_single_call_then_delegate
    python evals/run_all.py system_prompt --instance <インスタンス名>

第3引数以降にケースID（ファイル名から拡張子`.yaml`を除いたもの）を
指定すると、そのケースのみを実行する（tune-promptスキルの対話選択で使用）。
省略時は従来通り対象ディレクトリ配下の全ケースを実行する。

--instance で設定ダッシュボードのインスタンス（instances/<name>/）を指定すると、
全ケースをそのインスタンスの設定で実行する（省略時は環境変数
LOCOHANE_INSTANCE、それも無ければ default。evals/instance.py 参照）。

evals/cases/<target>/*.yaml を昇順に glob し、ケースごとに
`<このプロセスと同じ python> -m evals.run_case <file>` をサブプロセスとして
直列実行する（ローカル1台の llama.cpp server に同時多重リクエストを
かけないための配慮）。結果は evals/results/<target>/<timestamp>/ 配下に
results.json（全件の生データ）と summary.md（pass/fail 一覧 + judge待ち
ケースの transcript 抜粋）として保存し、同じ内容を標準出力にも表示する。

--repeat N（スキル安定化トライアウト）・--skill-overlay・--cases-dir は
ドラフトスキルの評価と昇格（promote-skill）に使う。開始時にケースと
--skill-overlay のスキルを一時フォルダへ写して固定し、全回をその内容で
評価する（途中でドラフトが編集されても回ごとに中身が変わらない）。
評価した内容のハッシュ（evals/skill_tree.py）とケースごとの合格回数を
tryout.json に残し、昇格時に今のドラフトと照合する（修正が入っていれば
合格回数は0に戻ったものとして昇格させない）。

--agent-overlay・--config-patch・--llm-from-instance はスキル研究室
（admin/skill_lab/）の評価に使う。開発中のサブエージェントと設定パッチも
同じく開始時に固定してハッシュを残す。ケースフォルダの fixtures/（ケースの
入力ファイル）もケースと一緒に固定し、cases_sha256 に含める。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# `python evals/run_all.py` はスクリプト直接実行のため sys.path[0] が evals/ に
# なり、`import evals.xxx` が解決できない（run_case.py 同様の対処）。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evals.case_schema import load_case  # noqa: E402
from evals.instance import resolve_instance_name  # noqa: E402
from evals.skill_tree import FIXTURES_DIRNAME, cases_sha256, file_sha256, tree_sha256  # noqa: E402
# 無言終了時の自動リトライ（src/graph.py の ainvoke_ensuring_final_text、
# 既定 max_retries=2）や大量画像を扱うケースはグラフの ainvoke が複数回・
# 長時間かかることがあるため、600秒では単体実行なら成功するケースまで
# タイムアウト扱いになることがある（tune-prompt iter13で確認）。実運用の
# app.py にはこの制限は存在しないため、テストハーネス側の都合として
# 900秒に緩和する。
CASE_TIMEOUT_SECONDS = 900

# Windows のコンソールコードページ（既定 cp932）でサマリの日本語が
# 文字化けするのを防ぐ。
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def _iter_case_paths(target: str, case_ids: list[str] | None = None, cases_dir: Path | None = None) -> list[Path]:
    """evals/cases/<target>/*.yaml（cases_dir 指定時はその直下の *.yaml）を昇順に列挙する。

    Args:
        target: チューニング対象カテゴリ名（例: "system_prompt"）。
        cases_dir: evals/cases/<target>/ の代わりに使うケースフォルダ
            （ドラフトスキルの <ドラフト>/evals/ 等、--cases-dir）。
        case_ids: 指定された場合、これらのケースID（拡張子抜きファイル名）
            のみに絞り込む。None または空リストなら全件。

    Returns:
        yaml ファイルパスの昇順リスト。

    Raises:
        SystemExit: 対象ディレクトリが存在しない場合、または指定した
            case_ids に対応する yaml が見つからない場合。
    """
    cases_dir = cases_dir or PROJECT_ROOT / "evals" / "cases" / target
    if not cases_dir.is_dir():
        raise SystemExit(f"ケースディレクトリが見つかりません: {cases_dir}")
    all_paths = sorted(cases_dir.glob("*.yaml"))
    if not case_ids:
        return all_paths

    by_stem = {p.stem: p for p in all_paths}
    missing = [cid for cid in case_ids if cid not in by_stem]
    if missing:
        raise SystemExit(
            f"指定されたケースが見つかりません: {missing}（対象: {cases_dir}）"
        )
    return [by_stem[cid] for cid in case_ids]


def _run_one(case_path: Path, instance_name: str, extra_args: list[str] | None = None) -> dict:
    """1ケースを run_case.py のサブプロセスとして実行し、結果 dict を返す。

    Args:
        case_path: 実行する eval ケースの yaml パス。
        instance_name: 実行対象インスタンス名（run_case.py の --instance へ渡す）。
        extra_args: run_case.py へそのまま渡す追加引数（--skill-overlay 等）。

    Returns:
        run_case.py が出力した結果 JSON をパースした dict。標準出力が空、
        または JSON として不正な場合はエラーを表す dict を返す（例外は伝播させない）。

    Raises:
        subprocess.TimeoutExpired: タイムアウト秒数以内に終わらなかった場合
            （呼び出し側で捕捉する）。
    """
    # ケースが timeout_seconds を指定していればそちらを優先する（大量ファイルを
    # 扱う重量級ケース等、既定値では完走できないケース専用の上書き）。
    case = load_case(case_path)
    timeout = case.timeout_seconds or CASE_TIMEOUT_SECONDS
    proc = subprocess.run(
        [sys.executable, "-m", "evals.run_case", str(case_path), "--instance", instance_name, *(extra_args or [])],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    stdout = proc.stdout.strip()
    if not stdout:
        return {
            "case_id": case_path.stem,
            "instance": instance_name,
            "error": "no_output",
            "detail": f"標準出力が空でした（終了コード {proc.returncode}）。",
        }
    try:
        return json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError as e:
        return {
            "case_id": case_path.stem,
            "instance": instance_name,
            "error": "invalid_json",
            "detail": f"結果のJSON解析に失敗しました: {e}",
        }


def _classify(result: dict) -> str:
    """1件の結果を "error" / "fail" / "judge"（要judge判定） / "pass" に分類する。"""
    if result.get("error"):
        return "error"
    if result.get("rules_pass") is False:
        return "fail"
    if result.get("judge"):
        return "judge"
    return "pass"


def _tryout_report(results: list[dict], repeat: int) -> dict:
    """スキル安定化トライアウト（--repeat）の集計を返す。

    同じケースを repeat 回繰り返した結果を、ケースごとに分類別の件数へまとめる。
    判定（verdict）は、全ケースが repeat 回すべて pass なら "pass"、1回でも
    fail/error があれば "fail"、それ以外（fail/error は無いが judge 判定待ちが
    ある）は "needs_judge"（全回の judge を読んで合格と判断できた場合のみ合格）。

    Returns:
        {"repeat", "cases": {case_id: {"pass", "fail", "judge", "error"}}, "verdict"}。
        main() はこれに評価した内容のハッシュ等（cases_dir, cases_sha256,
        case_files, skill_overlays）と instance を足して tryout.json に書く。
    """
    cases: dict[str, dict[str, int]] = {}
    for r in results:
        counts = cases.setdefault(r.get("case_id", "?"), {"pass": 0, "fail": 0, "judge": 0, "error": 0})
        counts[_classify(r)] += 1
    if any(c["fail"] or c["error"] for c in cases.values()):
        verdict = "fail"
    elif any(c["judge"] for c in cases.values()):
        verdict = "needs_judge"
    else:
        verdict = "pass"
    return {"repeat": repeat, "cases": cases, "verdict": verdict}


def _render_tryout(report: dict) -> str:
    """_tryout_report() の結果を Markdown にする。"""
    n = report["repeat"]
    labels = {"pass": "合格", "fail": "不合格", "needs_judge": "judge 判定待ち（全回の judge を読んで判断する）"}
    lines = ["## スキル安定化トライアウト", "", f"各ケース {n} 回実行。判定: **{labels[report['verdict']]}**", ""]
    for cid, c in report["cases"].items():
        lines.append(f"- {cid}: pass {c['pass']}/{n}, fail {c['fail']}, judge待ち {c['judge']}, error {c['error']}")
    lines.append("")
    return "\n".join(lines)


def _render_summary(target: str, instance_name: str, results: list[dict]) -> str:
    """結果一覧から人間可読な Markdown サマリを組み立てる。

    Args:
        target: チューニング対象カテゴリ名。
        instance_name: 実行対象インスタンス名。
        results: _run_one() の戻り値のリスト。

    Returns:
        pass/fail/judge待ち/error の集計表 + judge待ちケースの詳細を含む Markdown。
    """
    lines = [f"# eval 結果: {target}", "", f"インスタンス: {instance_name}", ""]
    n_pass = n_fail = n_judge = n_error = 0
    token_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    n_token_measured = 0
    for r in results:
        cid = r.get("case_id", "?")
        if r.get("error"):
            n_error += 1
            lines.append(f"- ⚠ {cid}: ERROR ({r['error']}) {r.get('detail', '')}")
            continue

        usage = r.get("token_usage_total") or {}
        usage_suffix = ""
        if usage.get("total_tokens"):
            n_token_measured += 1
            for key in token_total:
                token_total[key] += usage.get(key, 0) or 0
            usage_suffix = (
                f" [tokens in={usage.get('input_tokens', 0)} "
                f"out={usage.get('output_tokens', 0)} total={usage.get('total_tokens', 0)}]"
            )

        cutoffs = r.get("turn_cutoffs") or []
        cutoff_suffix = ""
        if cutoffs:
            detail = ", ".join(f"{c['reason']}(turn {c['turn_index']})" for c in cutoffs)
            cutoff_suffix = f" [⚠ turn cutoff: {detail}]"

        rules_pass = r.get("rules_pass")
        judge = r.get("judge")
        if rules_pass is False:
            n_fail += 1
            failed = [k for k, v in r.get("rule_results", {}).items() if not v["pass"]]
            lines.append(f"- ✗ {cid}: ルールFAIL ({', '.join(failed)}){usage_suffix}{cutoff_suffix}")
        elif judge:
            n_judge += 1
            status = "PASS" if rules_pass else "未定義"
            lines.append(f"- ? {cid}: 要judge判定（ルールは{status}）{usage_suffix}{cutoff_suffix}")
        else:
            n_pass += 1
            lines.append(f"- ✓ {cid}: ルールPASS{usage_suffix}{cutoff_suffix}")

    lines.append("")
    lines.append(
        f"集計: pass={n_pass} fail={n_fail} judge待ち={n_judge} error={n_error} / 全{len(results)}件"
    )
    if n_token_measured:
        lines.append(
            f"トークン使用量合計: 入力 {token_total['input_tokens']} / "
            f"出力 {token_total['output_tokens']} / 合計 {token_total['total_tokens']}"
            f"（計測できた{n_token_measured}/{len(results)}件）"
        )
    lines.append("")

    judge_cases = [r for r in results if not r.get("error") and r.get("judge")]
    if judge_cases:
        lines.append("## judge が必要なケースの詳細")
        for r in judge_cases:
            lines.append(f"### {r['case_id']}")
            lines.append(f"judge指示: {r['judge']}")
            lines.append(f"最終回答: {r.get('final_answer', '')}")
            lines.append("")

    return "\n".join(lines)


def main() -> int:
    """CLI エントリポイント。"""
    parser = argparse.ArgumentParser(prog="python evals/run_all.py")
    parser.add_argument("target", nargs="?", help="evals/cases/ 配下のケース群名（--cases-dir 指定時は省略可）")
    parser.add_argument("case_ids", nargs="*")
    parser.add_argument(
        "--instance",
        help="実行対象インスタンス名（instances/<name>/。省略時は環境変数 LOCOHANE_INSTANCE、それも無ければ default）",
    )
    parser.add_argument("--cases-dir", type=Path, help="evals/cases/<target>/ の代わりに使うケースフォルダ（ドラフトスキルの evals/ 等）")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="スキル安定化トライアウト: 各ケースを指定回数だけ直列に繰り返し、ケースごとの合格回数を集計する（既定1）",
    )
    parser.add_argument("--results-dir", type=Path, help="結果の出力先ルート（既定 evals/results/<target>/）")
    parser.add_argument("--skill-overlay", action="append", default=[], help="run_case.py の --skill-overlay へ渡す（複数可）")
    parser.add_argument("--exclude-skill", action="append", default=[], help="run_case.py の --exclude-skill へ渡す（複数可）")
    parser.add_argument("--agent-overlay", action="append", default=[], help="run_case.py の --agent-overlay へ渡す（複数可）")
    parser.add_argument("--config-patch", type=Path, help="run_case.py の --config-patch へ渡す")
    parser.add_argument("--llm-from-instance", help="run_case.py の --llm-from-instance へ渡す")
    args = parser.parse_args()
    if not args.target and not args.cases_dir:
        parser.error("Specify either target or --cases-dir")
    if args.repeat < 1:
        parser.error("--repeat must be an integer of 1 or more")
    cases_dir = args.cases_dir.resolve() if args.cases_dir else None
    case_ids = args.case_ids
    if cases_dir:
        # --cases-dir 指定時は target が不要なため、位置引数はすべてケースIDとして扱う
        # （「run_all.py --cases-dir X 001_basic」で 001_basic が target に入るのを防ぐ）。
        case_ids = [args.target, *args.case_ids] if args.target else args.case_ids
        target = cases_dir.name
    else:
        target = args.target
    # 全ケースを走らせる前に一度だけ解決・検証する（存在しないインスタンス名で
    # 全ケースが同じエラーになるのを避ける）。
    instance_name = resolve_instance_name(args.instance)

    case_paths = _iter_case_paths(target, case_ids, cases_dir)
    if not case_paths:
        print(f"対象ケースが1件もありません: {cases_dir or PROJECT_ROOT / 'evals' / 'cases' / target}", file=sys.stderr)
        return 1
    source_cases_dir = case_paths[0].parent

    print(f"対象インスタンス: {instance_name}", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="evals_run_all_", ignore_cleanup_errors=True) as tmp:
        # ケースと --skill-overlay のスキルを開始時点の内容で固定する。--repeat の
        # 途中でドラフトが編集されても、全回を同じ内容で評価するため。ケースは
        # 対象外のものも含めてフォルダごと写し、ハッシュはその写しで求める
        # （昇格時に今のドラフトと照合する。evals/skill_tree.py）。
        snapshot_cases_dir = Path(tmp) / "cases"
        snapshot_cases_dir.mkdir()
        for yaml_path in source_cases_dir.glob("*.yaml"):
            shutil.copy2(yaml_path, snapshot_cases_dir / yaml_path.name)
        if (source_cases_dir / FIXTURES_DIRNAME).is_dir():
            # ケースの work_dir（ケースのファイルからの相対パス）が写しを指すように一緒に固定する。
            shutil.copytree(source_cases_dir / FIXTURES_DIRNAME, snapshot_cases_dir / FIXTURES_DIRNAME)
        run_paths = [snapshot_cases_dir / p.name for p in case_paths]
        overlays = []
        extra_args: list[str] = []
        for i, d in enumerate(args.skill_overlay):
            source = Path(d).resolve()
            snapshot = Path(tmp) / "overlays" / str(i) / source.name
            shutil.copytree(source, snapshot, ignore=shutil.ignore_patterns("__pycache__"))
            overlays.append({"path": str(source), "name": source.name, "sha256": tree_sha256(snapshot)})
            extra_args += ["--skill-overlay", str(snapshot)]
        for name in args.exclude_skill:
            extra_args += ["--exclude-skill", name]
        agent_overlays = []
        for i, f in enumerate(args.agent_overlay):
            source = Path(f).resolve()
            snapshot = Path(tmp) / "agent_overlays" / str(i) / source.name
            snapshot.parent.mkdir(parents=True)
            shutil.copy2(source, snapshot)
            agent_overlays.append({"path": str(source), "name": source.stem, "sha256": file_sha256(snapshot)})
            extra_args += ["--agent-overlay", str(snapshot)]
        config_patch = None
        if args.config_patch:
            source = args.config_patch.resolve()
            snapshot = Path(tmp) / "config_patch.json"
            if source.is_file():
                shutil.copy2(source, snapshot)
            else:
                snapshot.write_text("{}", encoding="utf-8")
            config_patch = {"path": str(source), "sha256": file_sha256(snapshot)}
            extra_args += ["--config-patch", str(snapshot)]
        if args.llm_from_instance:
            extra_args += ["--llm-from-instance", args.llm_from_instance]
        snapshot_info = {
            "cases_dir": str(source_cases_dir),
            "cases_sha256": cases_sha256(snapshot_cases_dir),
            "case_files": [p.stem for p in case_paths],
            "skill_overlays": overlays,
            "agent_overlays": agent_overlays,
            "config_patch": config_patch,
            "llm_from_instance": args.llm_from_instance,
        }

        results = []
        # 同じケースを続けて回すより、ケースを一巡してから次の回へ進む方が、
        # 途中で打ち切っても全ケースの回数が揃う（各結果には repeat_index を付ける）。
        for repeat_index in range(1, args.repeat + 1):
            for path in run_paths:
                label = f"（{repeat_index}/{args.repeat}回目）" if args.repeat > 1 else ""
                print(f"実行中: {path.name}{label}", file=sys.stderr)
                try:
                    result = _run_one(path, instance_name, extra_args)
                except subprocess.TimeoutExpired as e:
                    result = {
                        # 集計キーを run_case.py の結果（yaml の id）と揃える。
                        "case_id": load_case(path).id,
                        "instance": instance_name,
                        "error": "timeout",
                        "detail": f"{e.timeout:.0f}秒でタイムアウトしました。",
                    }
                if args.repeat > 1:
                    result["repeat_index"] = repeat_index
                results.append(result)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_root = args.results_dir.resolve() if args.results_dir else PROJECT_ROOT / "evals" / "results" / target
    out_dir = results_root / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = _render_summary(target, instance_name, results)
    # tryout.json は --repeat 1 でも必ず書く（昇格の照合と skill-creator の
    # run_isolated_eval.py が、評価した内容のハッシュを読むため）。
    report = {**_tryout_report(results, args.repeat), "instance": instance_name, **snapshot_info}
    (out_dir / "tryout.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.repeat > 1:
        summary = f"{summary}\n{_render_tryout(report)}"
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")

    print(summary)
    print(f"\n詳細: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
