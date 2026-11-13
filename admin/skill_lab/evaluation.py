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
import re
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Iterator

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
    stop_on_fail: bool = False,
    assets_base: Path | None = None,
    config_patch: bool = True,
) -> list[str]:
    """評価のコマンド。

    stop_on_fail: 不合格の回が出たら残りを回さない（トライアウトの早期打ち切り）。
    assets_base: 重ねる資産の置き場（既定はテーマの assets/。改良前の版を評価するときは baseline/assets/）。
    config_patch: 設定パッチを重ねるか（改良前の版の評価では重ねない）。
    """
    base = assets_base or (directory / themes.ASSETS_DIRNAME)
    assets = themes.assets_in(base)
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
    ]
    if config_patch:
        cmd += ["--config-patch", str(directory / themes.CONFIG_PATCH_FILENAME)]
    if stop_on_fail:
        cmd.append("--stop-on-fail")
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
    stop_on_fail: bool = False,
    assets_base: Path | None = None,
    config_patch: bool = True,
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
    cmd = build_command(
        directory,
        instance=instance,
        lab=lab,
        repeat=repeat,
        results_root=results_root,
        case_ids=case_ids,
        stop_on_fail=stop_on_fail,
        assets_base=assets_base,
        config_patch=config_patch,
    )
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


@dataclass(frozen=True)
class ExcerptLimits:
    """AI（修正・judge・機械判定の見直し）に渡す記録の切り詰め（[skill_lab_worker] の excerpt_* 等）。

    既定値は config.ini と同じ。ワーカーがタスク・テーマの処理の間だけ limits() で設定する。
    """

    user_chars: int = 800
    thinking_chars: int = 600
    ai_text_chars: int = 1500
    ai_note_chars: int = 400
    tool_args_chars: int = 600
    tool_result_chars: int = 700
    subagent_args_chars: int = 400
    subagent_result_chars: int = 300
    judge_spec_chars: int = 6000
    judge_answer_chars: int = 4000
    judge_checks_chars: int = 4000
    judge_criteria_chars: int = 2000
    fixer_failed_runs_per_case: int = 2
    fixer_history_iterations: int = 6

    @classmethod
    def from_config(cls, config) -> "ExcerptLimits":
        """Config（src.config）の skill_lab_excerpt_* 等から作る（無い属性は既定値）。"""
        values = {}
        for f in fields(cls):
            key = f"skill_lab_{f.name}" if f.name.startswith(("judge_", "fixer_")) else f"skill_lab_excerpt_{f.name}"
            value = getattr(config, key, None)
            if isinstance(value, int):
                values[f.name] = value
        return cls(**values)


_LIMITS: ContextVar[ExcerptLimits] = ContextVar("skill_lab_excerpt_limits", default=ExcerptLimits())


@contextmanager
def limits(value: ExcerptLimits) -> Iterator[None]:
    token = _LIMITS.set(value)
    try:
        yield
    finally:
        _LIMITS.reset(token)


def current_limits() -> ExcerptLimits:
    return _LIMITS.get()


def transcript_excerpt(result: dict, max_chars: int) -> str:
    """AI（修正・judge）に渡すための、ツール呼び出しと結果・最終回答の抜粋。

    長いツール結果は先頭だけにし、全体を max_chars に収める（末尾の最終回答を優先して残す）。
    項目ごとの文字数は current_limits()（config.ini [skill_lab_worker] の excerpt_*）。
    """
    lim = current_limits()
    lines: list[str] = []
    for entry in result.get("transcript") or []:
        kind = entry.get("type")
        if kind == "HumanMessage":
            lines.append(f"[user] {entry.get('content', '')[: lim.user_chars]}")
        elif kind == "AIMessage":
            # 思考（evals/run_case.py が残した reasoning）。どのスキルを選ぶか・なぜ使わないかの判断が
            # 表れるので AI 修正・judge に見せる（判断は末尾に来ることが多いので末尾を残す）。
            reasoning = (entry.get("reasoning") or "").strip()
            if reasoning:
                if lim.thinking_chars > 0:
                    lines.append(f"[AI thinking] {'…' if len(reasoning) > lim.thinking_chars else ''}{reasoning[-lim.thinking_chars:]}")
            if entry.get("content") and entry.get("tool_calls"):
                # ツールを呼ぶときに一緒に書いた本文（「スキルを読みます」等）も判断の手がかりになる
                lines.append(f"[AI] {entry['content'][: lim.ai_note_chars]}")
            for tc in entry.get("tool_calls") or []:
                args = json.dumps(tc.get("args"), ensure_ascii=False)
                lines.append(f"[tool call] {tc.get('name')} {args[: lim.tool_args_chars]}")
            if entry.get("content") and not entry.get("tool_calls"):
                lines.append(f"[AI] {entry['content'][: lim.ai_text_chars]}")
        elif kind == "ToolMessage":
            lines.append(f"[tool result {entry.get('tool_name', '')}] {entry.get('content', '')[: lim.tool_result_chars]}")
    # 委譲先（dispatch_agent）の中のツール呼び出し（evals/run_case.py の subagent_tool_calls）。
    # 以前は渡しておらず、委譲先が検証対象のスキルを read_skill して正しく仕上げても、AI judge は
    # 「スキルを読んでいない」として不合格にしていた（2026-11-13 ユーザー報告）。
    for call in result.get("subagent_tool_calls") or []:
        if not isinstance(call, dict):
            continue
        args = json.dumps(call.get("args"), ensure_ascii=False, default=str)
        status = " (error)" if call.get("is_error") else ""
        lines.append(f"[sub-agent {call.get('dispatch', '?')} tool call] {call.get('name')} {args[: lim.subagent_args_chars]}{status}")
        if call.get("result"):
            lines.append(f"[sub-agent {call.get('dispatch', '?')} tool result] {str(call['result'])[: lim.subagent_result_chars]}")
    if result.get("error"):
        lines.append(f"[evaluation error] {result.get('error')}: {result.get('detail', '')}")
    # 打ち切り・所要時間・ガードの拒否・ライブラリ不足等（harness_notes）。原因の見立てを誤らせないよう、
    # 長い記録を切り詰めても残るよう末尾に置く。
    outputs_head = [f"[harness note] {n}" for n in harness_notes(result)]
    # 作業フォルダに作られた・変わったファイル（evals/run_case.py の output_files）。ファイルを作るスキルは
    # 最終回答に「作成しました」としか書かないことが多く、これが無いと AI judge は「記録で確認できない」として
    # 正しく作られていても不合格にしていた。末尾に置き、長い記録を切り詰めても残す。
    outputs = outputs_head + _output_files_lines(result.get("output_files") or [])
    text = "\n".join(lines)
    tail = "\n".join(outputs)
    budget = max_chars - (len(tail) + 1 if tail else 0)
    if len(text) > budget:
        head = max(budget, 0) // 3
        text = f"{text[:head]}\n… (snip) …\n{text[-(max(budget, 0) - head):]}" if budget > 0 else ""
    return f"{text}\n{tail}" if tail else text


# ツール結果の文言から分かる、モデルのせいではない失敗（src/tools の拒否・失敗のメッセージ）。
# (正規表現, 説明)。説明は AI 修正・judge へ渡す英語（原因の見立て違いを防ぐ）。
_HARNESS_PATTERNS = (
    (
        re.compile(r"is (?:not allowed|forbidden) for the main agent"),
        "the main agent was refused a tool by [main_agent_tool_guard] allow_entries (a setting; config_patch.json can allow a skill script, "
        "otherwise the work must be delegated to a sub-agent)",
    ),
    (
        re.compile(r"run_script skills/scripts callable from agent_type="),
        "a sub-agent was refused a skill script by [subagent] agent_type_run_script_allowlist (a setting; config_patch.json can allow it)",
    ),
    (
        re.compile(r"because the plan is not approved"),
        "a write or script run was refused because the plan was not approved (the assistant must create and approve a plan first, "
        "or the script must be in [plan] plan_approval_exempt_scripts via config_patch.json)",
    ),
    (
        re.compile(r"(?:ModuleNotFoundError|No module named) ['\"]?([A-Za-z0-9_.]+)"),
        "a Python library is not installed in the environment that runs scripts (an environment problem, not fixable in the skill)",
    ),
    (
        re.compile(r"the script timed out after \d+ seconds|reached the limit of \d+ seconds and was forcibly stopped"),
        "a script hit the time limit ([scripts] timeout): the script may be too slow, or the machine is",
    ),
)


def harness_notes(result: dict) -> list[str]:
    """評価の記録から、原因の見立てに要る事実（打ち切り・所要時間・ガードの拒否・ライブラリ不足・時間切れ）を拾う。

    以前はこれらを AI に渡しておらず、評価環境や設定による失敗をモデルの誤り（幻覚）と見立てて
    的外れな直しをすることがあった（evals/tuning_log.md の iter46）。
    """
    notes: list[str] = []
    for cut in result.get("turn_cutoffs") or []:
        snippet = str(cut.get("snippet") or "")[:200]
        notes.append(
            f"turn {int(cut.get('turn_index', 0)) + 1} was cut off by the {cut.get('reason')} guard "
            "(the assistant repeated itself or used too many steps; the turn ended without finishing)"
            + (f": {snippet!r}" if snippet else "")
        )
    for t in result.get("turn_timings") or []:
        llm = [c.get("total_seconds") or 0 for c in t.get("llm_calls") or []]
        tools = [c.get("total_seconds") or 0 for c in t.get("tool_calls") or []]
        if llm or tools:
            notes.append(
                f"turn {int(t.get('turn_index', 0)) + 1} took about {sum(llm) + sum(tools):.0f} s "
                f"({len(llm)} LLM calls, longest {max(llm, default=0):.0f} s; {len(tools)} tool runs, longest {max(tools, default=0):.0f} s)"
            )
    texts = [e.get("content") or "" for e in result.get("transcript") or [] if e.get("type") == "ToolMessage"]
    texts += [str(c.get("result") or "") + str(c.get("result_tail") or "") for c in result.get("subagent_tool_calls") or [] if isinstance(c, dict)]
    seen = set()
    for pattern, description in _HARNESS_PATTERNS:
        for text in texts:
            m = pattern.search(text)
            if m and description not in seen:
                seen.add(description)
                detail = f" ({m.group(1)})" if m.groups() and m.group(1) else ""
                notes.append(description + detail)
                break
    if result.get("error") == "timeout":
        notes.append("the whole case hit the evaluation time limit (timeout_seconds of the case or the default)")
    return notes


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
