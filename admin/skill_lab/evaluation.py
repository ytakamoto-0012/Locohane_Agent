"""研究テーマの評価（evals/run_all.py にテーマの全資産を重ねて実行する）。

- 構成は対象インスタンスのまま（--instance）。会社専用のスキル・ガード設定を含む本番の構成で評価する。
- テーマのスキル（--skill-overlay）・サブエージェント（--agent-overlay）を最優先で重ね、
  設定パッチ（--config-patch）を足す。昇格後の構成と同じになる。
- LLM の接続先だけをスキル調整ワーカーのものにする（--llm-from-instance）。

評価の子プロセスは、スキル調整ワーカーの環境変数（LOCOHANE_INSTANCE 等・研究室の .env）を
含まない環境で起動する（evals/run_case.py の apply_instance() が対象インスタンスの .env と混ぜないように）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from src.config import PROJECT_ROOT

from . import themes

# 研究室ワーカーのプロセスに入っていても、評価の子プロセスへは渡さない環境変数。
_INSTANCE_ENV_KEYS = ("LOCOHANE_INSTANCE", "CONFIG_OVERRIDES_PATH", "LOCOHANE_INSTANCE_ENV")


class EvaluationStopped(Exception):
    """評価の途中で停止の要求があった。"""


def build_command(
    directory: Path,
    *,
    instance: str,
    lab: str,
    repeat: int,
    results_root: Path,
    case_ids: list[str] | None = None,
) -> list[str]:
    assets = themes.list_assets(directory)
    base = directory / themes.ASSETS_DIRNAME
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "evals" / "run_all.py"),
        *(case_ids or []),
        "--cases-dir",
        str(directory / themes.CASES_DIRNAME),
        "--instance",
        instance,
        "--repeat",
        str(repeat),
        "--results-dir",
        str(results_root),
        "--llm-from-instance",
        lab,
        "--config-patch",
        str(directory / themes.CONFIG_PATCH_FILENAME),
    ]
    for name in assets["skills"]:
        cmd += ["--skill-overlay", str(base / "skills" / name)]
    for name in assets["agents"]:
        cmd += ["--agent-overlay", str(base / "agents" / f"{name}.md")]
    return cmd


def clean_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    for key in _INSTANCE_ENV_KEYS:
        env.pop(key, None)
    env["PYTHONUTF8"] = "1"
    return env


def run(
    directory: Path,
    *,
    instance: str,
    lab: str,
    repeat: int,
    results_root: Path,
    case_ids: list[str] | None = None,
    base_env: dict[str, str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    log_path: Path | None = None,
) -> dict:
    """評価を1回（各ケース repeat 回）行い、結果をまとめて返す。

    Returns:
        {"out_dir", "results"（results.json の中身）, "tryout"（tryout.json の中身）}

    Raises:
        EvaluationStopped: should_stop() が True になった（子プロセスは止める）。
        RuntimeError: 評価が結果を出さずに終わった。
    """
    if not list((directory / themes.CASES_DIRNAME).glob("*.yaml")):
        raise RuntimeError("There are no cases. Create cases before running.")
    results_root.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in results_root.iterdir()}
    cmd = build_command(directory, instance=instance, lab=lab, repeat=repeat, results_root=results_root, case_ids=case_ids)
    log_path = log_path or (results_root / "run_all.log")
    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=clean_env(base_env), stdout=log, stderr=subprocess.STDOUT)
        while proc.poll() is None:
            if should_stop and should_stop():
                _kill_tree(proc)
                raise EvaluationStopped()
            time.sleep(2)
    new_dirs = sorted(p for p in results_root.iterdir() if p.is_dir() and p.name not in before)
    if not new_dirs or not (new_dirs[-1] / "results.json").is_file():
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if log_path.is_file() else ""
        raise RuntimeError(f"The evaluation produced no results (exit code {proc.returncode}). {tail}")
    out_dir = new_dirs[-1]
    return {
        "out_dir": str(out_dir),
        "results": json.loads((out_dir / "results.json").read_text(encoding="utf-8")),
        "tryout": json.loads((out_dir / "tryout.json").read_text(encoding="utf-8")),
    }


def _kill_tree(proc: subprocess.Popen) -> None:
    """評価の子プロセス（run_all.py）と、その下で動いているケース（run_case.py）をまとめて止める。

    run_all.py だけを止めると、実行中のケースが最後まで LLM を使い続ける（Windows では
    子プロセスが親と一緒に終わらない）。
    """
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)


def infra_error(results: list[dict]) -> str | None:
    """研究対象を直しても解決しない失敗（評価で使う LLM に接続できない）なら、その説明を返す。

    このまま AI 修正に回すと、関係のないところを書き換えて反復を使い切ってしまう。
    """
    unreachable = [r for r in results if r.get("error") == "llm_unreachable"]
    if not unreachable:
        return None
    detail = unreachable[0].get("detail") or ""
    return f"Could not connect to the LLM used for evaluation ([llm] main_url of the skill tuning worker). Check the inference server. {detail}"[:2000]


def classify(result: dict) -> str:
    """evals/run_all.py の _classify() と同じ分類（error / fail / judge / pass）。"""
    if result.get("error"):
        return "error"
    if result.get("rules_pass") is False:
        return "fail"
    if result.get("judge"):
        return "judge"
    return "pass"


def transcript_excerpt(result: dict, max_chars: int) -> str:
    """AI（修正・judge）に渡すための、ツール呼び出しと結果・最終回答の抜粋。

    長いツール結果は先頭だけにし、全体を max_chars に収める（末尾の最終回答を優先して残す）。
    """
    lines: list[str] = []
    for entry in result.get("transcript") or []:
        kind = entry.get("type")
        if kind == "HumanMessage":
            lines.append(f"[user] {entry.get('content', '')[:800]}")
        elif kind == "AIMessage":
            for tc in entry.get("tool_calls") or []:
                args = json.dumps(tc.get("args"), ensure_ascii=False)
                lines.append(f"[tool call] {tc.get('name')} {args[:600]}")
            if entry.get("content") and not entry.get("tool_calls"):
                lines.append(f"[AI] {entry['content'][:1500]}")
        elif kind == "ToolMessage":
            lines.append(f"[tool result {entry.get('tool_name', '')}] {entry.get('content', '')[:700]}")
    if result.get("error"):
        lines.append(f"[evaluation error] {result.get('error')}: {result.get('detail', '')}")
    # 作業フォルダに作られた・変わったファイル（evals/run_case.py の output_files）。ファイルを作るスキルは
    # 最終回答に「作成しました」としか書かないことが多く、これが無いと AI judge は「記録で確認できない」として
    # 正しく作られていても不合格にしていた。末尾に置き、長い記録を切り詰めても残す。
    outputs = _output_files_lines(result.get("output_files") or [])
    text = "\n".join(lines)
    tail = "\n".join(outputs)
    budget = max_chars - (len(tail) + 1 if tail else 0)
    if len(text) > budget:
        head = max(budget, 0) // 3
        text = f"{text[:head]}\n… (snip) …\n{text[-(max(budget, 0) - head):]}" if budget > 0 else ""
    return f"{text}\n{tail}" if tail else text


def _output_files_lines(outputs: list) -> list[str]:
    """生成物の一覧を、AI に渡す抜粋の行にする（Excel は見た目の要約も短く添える）。"""
    lines = []
    for entry in outputs[:30]:
        if not isinstance(entry, dict):
            continue
        line = f"[output file] {entry.get('path')} ({entry.get('size')} bytes)"
        if entry.get("xlsx"):
            line += f" sheets: {json.dumps(entry['xlsx'], ensure_ascii=False, default=str)[:400]}"
        if entry.get("xlsx_error"):
            line += f" (could not read: {entry['xlsx_error']})"
        lines.append(line)
    if len(outputs) > 30:
        lines.append(f"[output file] … and {len(outputs) - 30} more")
    return lines


def token_usage(results: list[dict]) -> tuple[dict, int]:
    """評価の全ケース（各回）のトークン使用量の合計と LLM 呼び出し回数（evals/run_case.py の
    token_usage_total・token_usage_max_per_call.calls。サブエージェントの分を含む）。"""
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    calls = 0
    for r in results:
        usage = r.get("token_usage_total") or {}
        for k in total:
            total[k] += int(usage.get(k, 0) or 0)
        calls += int((r.get("token_usage_max_per_call") or {}).get("calls", 0) or 0)
    return total, calls


def summarize(results: list[dict]) -> dict[str, dict[str, int]]:
    """ケースごとの分類別の件数。"""
    cases: dict[str, dict[str, int]] = {}
    for r in results:
        counts = cases.setdefault(r.get("case_id", "?"), {"pass": 0, "fail": 0, "judge": 0, "error": 0})
        counts[classify(r)] += 1
    return cases
