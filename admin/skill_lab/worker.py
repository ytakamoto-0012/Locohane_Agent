"""スキル調整ワーカー（スキル研究室の実行役。instance.json の kind が skill_lab）。

管理ツールの Supervisor が `python -m admin.skill_lab.worker --lab <名前>` で起動する。
チャット画面は持たず、全インスタンス（kind=app）の研究テーマ置き場を見て、自分が担当する
テーマのタスク（1回試行・AI による下書き）と、ループ（評価 → AI 修正 → 再評価 → トライアウト）を
1件ずつ処理する。

- 評価は対象インスタンスの構成で行い、LLM の接続先だけを研究室のものにする（evaluation.py）。
- AI（修正・judge・下書き）も対象インスタンスの設定で呼び、LLM の接続先だけを研究室のものにする
  （_llm_config()。評価と同じ規則。研究室の設定で使うのは接続先・[skill_lab_worker]・[log] だけ）。
- 停止（管理ツールの停止ボタン = CTRL_BREAK_EVENT）で、実行中のテーマは queued に戻す
  （次に起動したとき、記録済みの反復の続きから再開する）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import signal
import sys
import time
import traceback
from pathlib import Path

from evals import temp_dirs
from src.config import PROJECT_ROOT

from .. import ai_usage
from .. import instances as inst
from ..i18n import output_language as output_language_code
from . import drafting, evaluation, fixer, judge, sources, themes
from .llm import LabLLMError, ask_text, current_output_language, output_language, usage_sink

logger = logging.getLogger("skill_lab.worker")


class _Stop(Exception):
    pass


# runs/ の下の控え（一番よかった内容・直す前の内容）
BEST_DIRNAME = "best"
BEFORE_FIX_DIRNAME = "before_fix"

# 止まったときに一番よかった内容へ戻す状態（合格・人の停止・エラーでは戻さない）
_RESTORE_BEST_STATUSES = frozenset({"exhausted", "stalled", "blocked"})
# 総括を残す状態（再開待ち・人の停止以外）
_REPORT_STATUSES = frozenset({"review", "exhausted", "stalled", "blocked", "error", "draft"})

_REPORT_SYSTEM = """You write the end-of-loop report of a skill tuning loop for the person in charge.
The input is JSON: the final status, each iteration (phase, verdict, failed cases with pass counts, files fixed, analysis,
whether the fix was reverted, blockers), the best iteration, whether the targets were restored to it, the results of the
version before the changes (baseline) and the final message.
Write 4 to 8 short lines: what was achieved (compare with the baseline if present), what still fails and the most likely
reason, and what the person should do next (fix a case, the spec or the environment, lower the theme's tryout runs, review and promote, etc.).
Write only facts that are in the input. Output plain text without headings."""


def _score(iteration: dict) -> tuple[int, float]:
    """反復の合格の多さ（全回合格したケースの数, 全回のうち合格した割合）。一番よかった内容を選ぶのに使う。"""
    cases = iteration.get("cases") or {}
    full = sum(1 for c in cases.values() if c.get("pass") and not c.get("fail") and not c.get("error"))
    runs = sum(int(c.get("pass") or 0) + int(c.get("fail") or 0) + int(c.get("error") or 0) for c in cases.values())
    ratio = round(sum(int(c.get("pass") or 0) for c in cases.values()) / runs, 4) if runs else 0.0
    return full, ratio


def _wilson_lower(passes: int, runs: int, z: float = 1.96) -> float:
    """合格率の信頼下限（Wilson）。回数が少ない結果ほど割り引く（1回中1回合格 ≒ 0.21、10回中10回 ≒ 0.72）。"""
    if runs <= 0:
        return 0.0
    p = passes / runs
    denom = 1 + z * z / runs
    centre = p + z * z / (2 * runs)
    margin = z * ((p * (1 - p) / runs + z * z / (4 * runs * runs)) ** 0.5)
    return (centre - margin) / denom


def _state_confidence(iterations: list[dict], state_sha: str) -> float:
    """同じ内容（state_sha）を評価した反復の結果を合算した、ケースごとの合格率の信頼下限の合計。一番よかった内容を選ぶのに使う。

    以前は反復ごとの (全回合格したケースの数, 合格率) で比べていたため、評価1回で1回合格した最初の版が、トライアウト6回中
    5回合格した直した版より「よい」とされ、止まったときに最初の版へ戻していた（2026-11-15 仮想研究室で発覚）。
    """
    totals: dict[str, list[int]] = {}
    for it in iterations:
        if it.get("state_sha") != state_sha:
            continue
        for cid, c in (it.get("cases") or {}).items():
            t = totals.setdefault(cid, [0, 0])
            t[0] += int(c.get("pass") or 0)
            t[1] += int(c.get("pass") or 0) + int(c.get("fail") or 0) + int(c.get("error") or 0)
    return round(sum(_wilson_lower(p, n) for p, n in totals.values()), 6)


class Worker:
    # 処理中のテーマのループの設定（run_theme で作る themes.LoopConfig。ワーカーの設定に、対象インスタンスの
    # [skill_lab] max_iterations・max_hours と、テーマの上級者向け設定を重ねたもの）。テーマの外では None。
    _loop_cfg = None
    # 実行中の反復（評価1回と、続く AI 修正）の期限（time.monotonic()）。反復の外では None。
    _step_deadline: float | None = None
    # AI 修正が「研究対象の外の原因」を報告したときに、止めた後で適用するケースの設定（制限時間・自動回答）
    _pending_case_settings: list = []

    @property
    def _cfg(self):
        """処理中のテーマのループの設定（run_theme の外ではワーカーの設定）。"""
        return self._loop_cfg if self._loop_cfg is not None else self.config

    def __init__(self, lab: str, base_env: dict[str, str]):
        from src.config import load_config, resolve_instances_root

        self.lab = lab
        self.base_env = base_env
        self.instances_root = resolve_instances_root()
        self.config = load_config()  # apply_instance(lab) 済み = 研究室の設定
        self.shutdown = False
        self._broken: set[Path] = set()  # theme.json を読めなかったテーマ（警告は1回だけ出す）

    # --- テーマの列挙 -----------------------------------------------------

    def _lab_names(self) -> list[str]:
        return [n for n in inst.list_instance_names(self.instances_root) if inst.read_instance(self.instances_root, n).is_skill_lab]

    def _assigned(self, data: dict) -> bool:
        lab = data.get("lab")
        if lab:
            return lab == self.lab
        # 担当が未設定なら、研究室が1つだけのときにその研究室が受け持つ。
        return self._lab_names() == [self.lab]

    def _theme_dirs(self) -> list[tuple[str, Path]]:
        from src.config import load_config

        result = []
        seen: set[Path] = set()
        for name in inst.list_instance_names(self.instances_root):
            try:
                meta = inst.read_instance(self.instances_root, name)
                if meta.is_skill_lab:
                    continue
                cfg = load_config(overrides_path=inst.overrides_path(self.instances_root, name), instance_name=name)
            except Exception as e:  # noqa: BLE001 - 1インスタンスの設定エラーで全体を止めない
                logger.warning("Skipping instance %s because its settings cannot be read: %s", name, e)
                continue
            for directory in themes.list_theme_dirs(themes.themes_root(cfg.common_data_dir)):
                # データフォルダを共有しているインスタンスがあっても、テーマは作ったインスタンス
                # （theme.json の instance）の構成でだけ評価する。
                try:
                    owner = themes.read(directory).get("instance")
                except Exception as e:  # noqa: BLE001 - 壊れたテーマ1件で他のテーマを止めない
                    if directory not in self._broken:
                        self._broken.add(directory)
                        logger.warning("Skipping theme %s because its theme.json cannot be read: %s", directory, e)
                    continue
                if directory.resolve() in seen or owner != name:
                    continue
                seen.add(directory.resolve())
                result.append((name, directory))
        return result

    # --- トークン使用量（「利用量・料金」の台帳。admin/ai_usage.py） ----------------

    def _record_usage(self, owner: str | None, usage: dict, calls: int = 1) -> None:
        ai_usage.record_safely(self.instances_root, self.lab, owner, usage, calls=calls)

    def _record_eval_usage(self, owner: str | None, results: list[dict]) -> None:
        usage, calls = evaluation.token_usage(results)
        self._record_usage(owner, usage, calls)

    def _usage_owner(self, data: dict) -> str | None:
        """ループ・トライアウトを始めた人（無ければテーマの作成者）。"""
        return (data.get("loop") or {}).get("by") or data.get("created_by")

    def _target_config(self, instance: str):
        from src.config import load_config

        return load_config(overrides_path=inst.overrides_path(self.instances_root, instance), instance_name=instance)

    def _llm_config(self, instance: str, target_cfg=None):
        """AI（修正・judge・下書き）に渡す設定: 対象インスタンスの設定に、研究室の LLM 接続先だけを重ねる。

        評価（evaluation.py の --llm-from-instance）と同じ規則。推論パラメータ等は対象インスタンスのものを使う。
        """
        return dataclasses.replace(
            target_cfg or self._target_config(instance),
            main_endpoints=self.config.main_endpoints,
            main_routing_strategy=self.config.main_routing_strategy,
            sub_endpoints=self.config.sub_endpoints,
            sub_endpoints_inherit_main=self.config.sub_endpoints_inherit_main,
            sub_routing_strategy=self.config.sub_routing_strategy,
        )

    # --- メインループ -----------------------------------------------------

    def recover(self) -> None:
        """前回の異常終了で「実行中」のまま残ったタスクを待ちに戻す（そのままだと二度と処理されない）。

        テーマ（status=running）は process_one_theme() が中断から再開するものとして先に拾う。
        """
        for _instance, directory in self._theme_dirs():
            data = themes.read(directory)
            if not self._assigned(data) or not any(t.get("status") == "running" for t in data.get("tasks") or []):
                continue

            def _apply(d: dict) -> None:
                for t in d.get("tasks") or []:
                    if t.get("status") == "running":
                        t["status"] = "queued"

            themes.update(directory, _apply)
            logger.info("Returned the interrupted tasks of theme %s to the queue.", directory.name)

    def sweep_temp_dirs(self) -> None:
        """各インスタンス（kind=app）の評価の一時フォルダ（[skill_lab].temp_dir）に残ったごみを片付ける。

        評価の子プロセスが強制終了されると一時フォルダが残る。本体（app.py）を動かしていない
        インスタンスでも残さないよう、ワーカーが [skill_lab].temp_cleanup_interval_hours ごとに片付ける。
        """
        from src.config import load_config

        seen: set[Path] = set()
        for name in inst.list_instance_names(self.instances_root):
            try:
                if inst.read_instance(self.instances_root, name).is_skill_lab:
                    continue
                cfg = load_config(overrides_path=inst.overrides_path(self.instances_root, name), instance_name=name)
            except Exception:  # noqa: BLE001 - 設定を読めないインスタンスは _theme_dirs() が警告する
                continue
            root = cfg.skill_lab_temp_dir.resolve()
            if root in seen:
                continue
            seen.add(root)
            removed = temp_dirs.cleanup_stale(root, cfg.skill_lab_temp_retention_hours)
            if removed:
                logger.info("Removed %d leftover evaluation temp folder(s) in %s", len(removed), root)

    def _maybe_sweep_temp_dirs(self) -> None:
        interval = float(getattr(self.config, "skill_lab_temp_cleanup_interval_hours", 1)) * 3600
        now = time.monotonic()
        if now < getattr(self, "_next_sweep", 0.0):
            return
        self._next_sweep = now + interval
        try:
            self.sweep_temp_dirs()
        except Exception as e:  # noqa: BLE001 - 片付けの失敗でワーカーを止めない
            logger.warning("Failed to clean up evaluation temp folders: %s", e)

    def run_forever(self) -> None:
        logger.info("Skill tuning worker %s started.", self.lab)
        recovered = False
        while not self.shutdown:
            self._maybe_sweep_temp_dirs()
            try:
                if not recovered:
                    self.recover()
                    recovered = True
                worked = self.process_tasks()
                if not self.shutdown:
                    worked = self.process_one_theme() or worked
            except Exception as e:  # noqa: BLE001 - 1件の想定外の失敗（壊れた theme.json 等）でワーカーを止めない
                logger.error("Unexpected error during processing (continuing after a short wait): %s\n%s", e, traceback.format_exc())
                worked = False
            if not worked:
                self._sleep(self.config.skill_lab_poll_interval_seconds)
        logger.info("Skill tuning worker %s is exiting.", self.lab)

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.shutdown and time.monotonic() < end:
            time.sleep(0.5)

    # --- タスク（1回試行・下書き） -----------------------------------------

    def process_tasks(self) -> bool:
        worked = False
        for instance, directory in self._theme_dirs():
            if self.shutdown:
                break
            data = themes.read(directory)
            if not self._assigned(data):
                continue
            for task in data.get("tasks") or []:
                if task.get("status") != "queued" or self.shutdown:
                    continue
                worked = True
                # 依頼した後にテーマが終わった・ループが始まったときは、テーマのファイルを書き換える依頼を実行しない
                # （ループの途中で仕様やケースが変わると、評価と修正の前提が崩れる）。
                status = themes.read(directory).get("status")
                if status in themes.CLOSED_STATUSES:
                    self._set_task(directory, task["id"], status="error", finished_at=themes.now_iso(), error="Not run because the theme has ended.")
                    continue
                if task.get("type") != "trial" and status in themes.ACTIVE_STATUSES:
                    self._set_task(
                        directory, task["id"], status="error", finished_at=themes.now_iso(),
                        error="Not run because a skill tuning loop or tryout has started (stop it and request again).",
                    )
                    continue
                self._run_task(instance, directory, task)
        return worked

    def _set_task(self, directory: Path, task_id: str, **fields) -> None:
        def _apply(data: dict) -> None:
            for t in data.get("tasks") or []:
                if t["id"] == task_id:
                    t.update(fields)

        themes.update(directory, _apply)

    def _output_lang(self, instance: str, requested: str | None) -> str:
        """AI に書かせる文章の言語: 対象インスタンスの [ui].default_language（auto なら依頼した時の画面の言語）。"""
        try:
            setting = self._target_config(instance).ui_default_language
        except Exception:  # noqa: BLE001 - 設定を読めなくても言語の決定で処理を止めない（評価・下書き側で同じ失敗が出る）
            setting = None
        return output_language_code(setting, requested)

    def _theme_cfg(self, instance: str, directory: Path):
        """テーマの設定（ワーカーの設定 + 対象インスタンスのループの上限 + テーマの上級者向け設定）。"""
        return themes.loop_config(self.config, self._target_config(instance), themes.read(directory))

    def _run_task(self, instance: str, directory: Path, task: dict) -> None:
        owner = task.get("by")
        with (
            usage_sink(lambda usage: self._record_usage(owner, usage)),
            output_language(self._output_lang(instance, task.get("lang"))),
            evaluation.limits(evaluation.ExcerptLimits.from_config(self._theme_cfg(instance, directory))),
        ):
            self._run_task_inner(instance, directory, task)

    def _run_task_inner(self, instance: str, directory: Path, task: dict) -> None:
        self._set_task(directory, task["id"], status="running", started_at=themes.now_iso())
        params = task.get("params") or {}
        cfg = self._theme_cfg(instance, directory)
        try:
            if task["type"] != "trial":
                target_cfg = self._target_config(instance)
                llm_cfg = self._llm_config(instance, target_cfg)
            if task["type"] == "trial":
                result = self._trial(instance, directory, params["case_id"], task["id"])
            elif task["type"] == "draft_spec":
                drafting.draft_spec(llm_cfg, directory, max_file_chars=cfg.skill_lab_fixer_max_file_chars, request=params.get("request", ""))
                result = {"message": "Drafted spec.md."}
            elif task["type"] == "draft_cases":
                created = drafting.draft_cases(
                    llm_cfg,
                    directory,
                    max_file_chars=cfg.skill_lab_fixer_max_file_chars,
                    count=min(max(int(params.get("count") or 3), 1), 10),
                    request=params.get("request", ""),
                )
                result = {"message": f"Drafted {len(created)} case(s).", "created": created}
            elif task["type"] == "draft_asset":
                drafting.draft_asset(
                    llm_cfg,
                    directory,
                    asset_type=params["asset_type"],
                    name=params["name"],
                    request=params.get("request", ""),
                    known_tools=list(sources.subagent_tool_names()),
                    skills=sources.list_official_skills(target_cfg)
                    + [{"name": n, "description": "(in development in this theme)"} for n in themes.list_assets(directory)["skills"]],
                    max_file_chars=cfg.skill_lab_fixer_max_file_chars,
                )
                result = {"message": f"Drafted {params['name']}."}
            else:
                raise themes.ThemeError(f"Unknown task: {task['type']}")
            self._set_task(directory, task["id"], status="done", finished_at=themes.now_iso(), result=result)
        except evaluation.EvaluationStopped:
            self._set_task(directory, task["id"], status="queued")
        except Exception as e:  # noqa: BLE001 - タスクの失敗は画面に理由を出して続ける
            logger.warning("Task %s (%s) failed: %s", task["id"], task["type"], e)
            self._set_task(directory, task["id"], status="error", finished_at=themes.now_iso(), error=str(e)[:2000])

    def _trial(self, instance: str, directory: Path, case_id: str, task_id: str) -> dict:
        run = evaluation.run(
            directory,
            jobs=int(getattr(self._theme_cfg(instance, directory), "skill_lab_eval_parallel_runs", 1)),
            instance=instance,
            lab=self.lab,
            repeat=1,
            # 結果のフォルダ名は秒単位の日時のため、試行ごとにフォルダを分けて取り違えないようにする。
            results_root=directory / themes.RUNS_DIRNAME / "trials" / task_id,
            case_ids=[case_id],
            base_env=self.base_env,
            should_stop=lambda: self.shutdown,
        )
        task = next((t for t in themes.read(directory).get("tasks") or [] if t["id"] == task_id), {})
        self._record_eval_usage(task.get("by"), run["results"])
        spec = (directory / themes.SPEC_FILENAME).read_text(encoding="utf-8")
        verdicts = judge.judge_results(
            self._llm_config(instance),
            spec=spec,
            results=run["results"],
            excerpt_chars=self._theme_cfg(instance, directory).skill_lab_transcript_excerpt_chars,
        )
        r = run["results"][0] if run["results"] else {}
        return {
            "out_dir": run["out_dir"],
            "case_id": case_id,
            "outcome": verdicts[0]["outcome"] if verdicts else "error",
            "ai_judge": verdicts[0].get("ai_judge") if verdicts else None,
            "rule_review": verdicts[0].get("rule_review") if verdicts else None,
            "rule_results": r.get("rule_results"),
            "error": r.get("error"),
            "detail": r.get("detail"),
            "final_answer": (r.get("final_answer") or "")[:6000],
            "excerpt": evaluation.transcript_excerpt(r, 20000),
        }

    # --- ループ -------------------------------------------------------------

    def process_one_theme(self) -> bool:
        candidates = []
        for instance, directory in self._theme_dirs():
            data = themes.read(directory)
            if data.get("status") in (themes.STATUS_QUEUED, themes.STATUS_RUNNING) and self._assigned(data):
                # 中断から再開するもの（running）を先に、次に「開始」を押した順（queued_at）。
                order = (0 if data.get("status") == themes.STATUS_RUNNING else 1, (data.get("loop") or {}).get("queued_at") or data.get("updated_at") or "")
                candidates.append((order, instance, directory))
        if not candidates:
            return False
        _, instance, directory = sorted(candidates)[0]
        self.run_theme(instance, directory)
        return True

    def _stop_requested(self, directory: Path) -> bool:
        if self.shutdown:
            return True
        loop = themes.read(directory).get("loop") or {}
        return bool(loop.get("stop_requested"))

    def run_theme(self, instance: str, directory: Path) -> None:
        data = themes.read(directory)
        loop = data.get("loop") or {}
        mode = loop.get("mode", "loop")
        # active: ワーカーの停止で中断したループの続き（反復の回数を数え直さない）。
        # deadline は以前の版がループ全体の期限として書いていた印（同じく続きを表す）。
        if not loop.get("active") and not loop.get("deadline"):
            loop = {
                **loop,
                "mode": mode,
                "started_at": themes.now_iso(),
                "active": True,
                "iterations_used": 0,
            }
        loop.pop("deadline", None)
        self._step_deadline = None
        # ループの上限はスキルが属するインスタンスの設定、ほかはワーカーの設定。どちらもテーマの上級者向け設定で上書きできる。
        self._loop_cfg = themes.loop_config(self.config, self._target_config(instance), data)

        picked = {"ok": False, "stopped": False}

        def _start(d: dict) -> None:
            # 一覧を読んでからここまでの間に、人が停止・担当替えをしていれば始めない。
            if d.get("status") not in themes.ACTIVE_STATUSES or not self._assigned(d):
                return
            if (d.get("loop") or {}).get("stop_requested"):
                # ワーカーが止まっている間に押された停止（再開せずに停止として終える）。
                picked["stopped"] = True
                return
            picked["ok"] = True
            d["status"] = themes.STATUS_RUNNING
            d["loop"] = {**loop, "stop_requested": False, "lab": self.lab}
            themes.add_history(d, f"skill tuning worker {self.lab}", "loop_start" if mode == "loop" else "tryout_start")

        themes.update(directory, _start)
        if picked["stopped"]:
            self._finish(directory, themes.STATUS_STOPPED, "interrupted by a stop request")
            return
        if not picked["ok"]:
            return
        current = themes.read(directory)
        owner = self._usage_owner(current)
        lang = self._output_lang(instance, (current.get("loop") or {}).get("lang"))
        with (
            usage_sink(lambda usage: self._record_usage(owner, usage)),
            output_language(lang),
            evaluation.limits(evaluation.ExcerptLimits.from_config(self._cfg)),
        ):
            self._run_theme_body(instance, directory, mode)

    def _run_theme_body(self, instance: str, directory: Path, mode: str) -> None:
        try:
            # トライアウトの回数はテーマの設定（無ければ対象インスタンスの [skill_lab] tryout_repeats）
            repeats = themes.tryout_repeats(themes.read(directory), self._target_config(instance))
            if mode == "tryout":
                self._tryout_only(instance, directory, repeats)
            else:
                self._loop(instance, directory, repeats)
        except (evaluation.EvaluationStopped, _Stop):
            if self.shutdown:
                self._finish(directory, themes.STATUS_QUEUED, "interrupted because the lab stopped (resumes where it left off when restarted)")
            elif self._step_timed_out() and not self._stop_requested(directory):
                # 反復1回の制限時間を過ぎて評価を打ち切った（その回は記録しない）
                self._finish(directory, themes.STATUS_EXHAUSTED, self._time_limit_message())
            else:
                self._finish(directory, themes.STATUS_STOPPED, "interrupted by a stop request")
        except Exception as e:  # noqa: BLE001 - テーマ単位で失敗させ、ワーカーは続ける
            logger.error("Processing of theme %s failed: %s\n%s", directory.name, e, traceback.format_exc())
            self._finish(directory, themes.STATUS_ERROR, f"{type(e).__name__}: {e}"[:2000])

    def _finish(self, directory: Path, status: str, detail: str) -> None:
        restored = self._restore_best(directory) if status in _RESTORE_BEST_STATUSES else None
        if restored:
            detail = f"{detail}\n{restored}"

        def _apply(d: dict) -> None:
            d["status"] = status
            if d.get("loop"):
                d["loop"]["stop_requested"] = False
                if status != themes.STATUS_QUEUED:
                    d["loop"]["active"] = False
                    d["loop"].pop("deadline", None)
                d["loop"]["message"] = detail
                d["loop"]["progress"] = None
                if restored:
                    d["loop"]["restored_best"] = restored
            themes.add_history(d, f"skill tuning worker {self.lab}", f"loop_{status}", detail)

        themes.update(directory, _apply)
        if status in _REPORT_STATUSES:
            self._write_report(directory, status)

    def _check_limits(self, directory: Path) -> str | None:
        data = themes.read(directory)
        loop = data.get("loop") or {}
        message = None
        if loop.get("iterations_used", 0) >= self._cfg.skill_lab_max_iterations:
            message = f"Reached the iteration limit ({self._cfg.skill_lab_max_iterations})"
        if not message and self._step_timed_out():
            # 判定・AI 修正は途中で打ち切れないため、終わった後のここで止める
            message = self._time_limit_message()
        if message:
            # 最後の評価で落ちたケース（トライアウトなら何回中何回合格したか）を添える。ほとんど合格しているなら
            # 答えがゆらいでいる（トライアウトの回数が厳しすぎる・ケースの条件が細かすぎる）見込みが高い。
            last = (data.get("iterations") or [None])[-1]
            failed = fixer.failed_cases_text(last) if last else ""
            if failed:
                message += f". Failed cases in the last evaluation ({last.get('phase')}): {failed}"
        return message

    def _start_step_clock(self) -> None:
        """反復1回（評価1回と、失敗したときに続く AI 修正）の制限時間（対象インスタンスの [skill_lab] max_hours）を測り始める。

        以前はループ全体の期限だったが、反復の数で使える時間が変わり見積もれないため、1回ごとにした（2026-11-14 ユーザー指摘）。
        """
        hours = float(self._cfg.skill_lab_max_hours or 0)
        # 0 以下は無制限（時間がかかっても完遂させる）
        self._step_deadline = time.monotonic() + hours * 3600 if hours > 0 else None

    def _step_timed_out(self) -> bool:
        return self._step_deadline is not None and time.monotonic() > self._step_deadline

    def _time_limit_message(self) -> str:
        return f"Reached the time limit of one iteration ({self._cfg.skill_lab_max_hours} hours)"

    def _should_stop_step(self, directory: Path) -> bool:
        """評価を打ち切るか（人の停止・ワーカーの停止・反復1回の制限時間）。"""
        return self._stop_requested(directory) or self._step_timed_out()

    def _evaluate(self, instance: str, directory: Path, repeat: int, phase: str) -> dict:
        """評価して AI judge まで行い、反復として記録する。

        ループ中は、合格の多さ（_score）が今までで一番よかった内容を runs/best/ に控える（_restore_best）。
        トライアウトは [skill_lab_worker] tryout_stop_on_first_failure なら最初の不合格で打ち切る。
        """
        data = themes.read(directory)
        n = len(data.get("iterations") or []) + 1
        run_dir = directory / themes.RUNS_DIRNAME / f"{n:03d}"
        # 評価した内容（研究対象・ケース・設定パッチ）。AI 修正が同じ内容に戻したかの判定に使う（_stop_if_stalled）。
        state_sha = themes.state_sha256(directory)
        self._start_step_clock()
        self._progress(directory, phase, n=n, repeat_count=repeat)
        run = evaluation.run(
            directory,
            jobs=int(getattr(self._cfg, "skill_lab_eval_parallel_runs", 1)),
            instance=instance,
            lab=self.lab,
            repeat=repeat,
            results_root=run_dir,
            base_env=self.base_env,
            should_stop=lambda: self._should_stop_step(directory),
            stop_on_fail=phase == "tryout" and bool(getattr(self._cfg, "skill_lab_tryout_stop_on_first_failure", True)),
            on_progress=self._case_progress(directory, phase, n=n, repeat_count=repeat),
        )
        self._record_eval_usage(self._usage_owner(data), run["results"])
        spec = (directory / themes.SPEC_FILENAME).read_text(encoding="utf-8")
        verdicts = judge.judge_results(
            self._llm_config(instance),
            spec=spec,
            results=run["results"],
            excerpt_chars=self._cfg.skill_lab_transcript_excerpt_chars,
            on_progress=self._judge_progress(directory, "judging", n=n, phase=phase),
            max_workers=self._judge_workers(),
        )
        passed = judge.all_passed(verdicts)
        per_case: dict[str, dict[str, int]] = {}
        for v in verdicts:
            c = per_case.setdefault(v["case_id"], {"pass": 0, "fail": 0, "error": 0})
            c["pass" if v["outcome"] == "pass" else ("error" if v["outcome"] == "error" else "fail")] += 1
        iteration = {
            "n": n,
            "phase": phase,
            "repeat": repeat,
            "at": themes.now_iso(),
            "verdict": "pass" if passed else "fail",
            "cases": per_case,
            "out_dir": run["out_dir"],
            "verdicts": verdicts,
            "state_sha": state_sha,
        }
        if (run.get("tryout") or {}).get("stopped_early"):
            iteration["stopped_early"] = True
        score = _score(iteration)
        best = ((data.get("loop") or {}).get("best") or {}) if data.get("loop") else None
        # 同じ内容の評価を合算し、回数の少ない結果を割り引いて比べる（_state_confidence）
        evaluated = [*self._loop_iterations(data), iteration]
        confidence = _state_confidence(evaluated, state_sha)
        best_confidence = _state_confidence(evaluated, best["state_sha"]) if best and best.get("state_sha") else -1.0
        new_best = best is not None and (not best or confidence > best_confidence or best.get("state_sha") == state_sha)
        if new_best:
            themes.snapshot_targets(directory, directory / themes.RUNS_DIRNAME / BEST_DIRNAME)

        def _apply(d: dict) -> None:
            d.setdefault("iterations", []).append(iteration)
            if d.get("loop") is not None:
                d["loop"]["iterations_used"] = d["loop"].get("iterations_used", 0) + 1
                if new_best:
                    d["loop"]["best"] = {"n": n, "phase": phase, "score": list(score), "state_sha": state_sha, "confidence": confidence}
            if phase == "tryout" and passed:
                d["tryout"] = {
                    **run["tryout"],
                    "out_dir": run["out_dir"],
                    "at": themes.now_iso(),
                    "ai_verdict": "pass",
                    # 機械判定を LLM が見直して合格にした回も、人が確認する対象（AI の判定）に数える
                    "ai_judged": any(
                        v.get("ai_judge") or (v.get("rule_review") or {}).get("how") == "ai" for v in verdicts
                    ),
                    "iteration": n,
                }

        themes.update(directory, _apply)
        return {"iteration": iteration, "run": run, "verdicts": verdicts, "passed": passed}

    def _fix(self, instance: str, directory: Path, outcome: dict) -> list[dict]:
        """AI に直させる。AI が研究対象の外の原因を指摘したら、研究対象は直さずにその指摘を返す（無ければ空）。

        直す前の内容を runs/<反復>/before_fix/ に控える（次の評価で合格していたケースが落ちたら戻す。_revert_if_regressed）。
        提案を適用できなかったとき（触るファイルが多すぎる・置き換え元が見つからない等）は、評価し直さずに1回だけ出し直させる。
        """
        cfg = self._cfg
        data = themes.read(directory)
        n = outcome["iteration"]["n"]
        run_dir = directory / themes.RUNS_DIRNAME / f"{n:03d}"
        self._progress(directory, "fixing", n=n)
        failures = fixer.failures_text(outcome["run"]["results"], outcome["verdicts"], cfg.skill_lab_transcript_excerpt_chars)
        history = fixer.history_text(data.get("iterations") or [])
        max_files = int(getattr(cfg, "skill_lab_fixer_max_files_per_fix", 2))
        fix: dict = {}
        # この _fix の中で直す前の内容を控えたか。フォルダの有無で判定すると、「実行の記録をクリア」で
        # 消し残った前の回の runs/<反復>/before_fix/ を今の控えとみなし、取り消しで古い内容に戻してしまう。
        snapshotted = False
        try:
            previous_errors: list[str] | None = None
            for attempt in (1, 2):
                proposal = None
                if attempt == 1 and getattr(cfg, "skill_lab_fixer_agent", False):
                    try:
                        proposal = self._propose_with_agent(instance, directory, outcome, run_dir, history, max_files)
                        fix_extra = {"agent": True, "agent_steps": proposal["steps"], "agent_trials": proposal["trials"]}
                    except (evaluation.EvaluationStopped, _Stop):
                        raise
                    except Exception as e:  # noqa: BLE001 - Agent の失敗でループごと止めない
                        # Agent が結論を出せなかった（手数切れ・ツールを呼ばなくなった・LLM の接続エラー等）: 1回呼び出しの修正で続ける
                        logger.warning("The fixing agent failed (%s); falling back to a single-call fix.", e)
                        previous_errors = [f"(the investigating agent could not conclude: {e})"]
                if proposal is None:
                    proposal = fixer.propose(
                        self._llm_config(instance),
                        directory,
                        failures=failures,
                        history=history,
                        max_file_chars=cfg.skill_lab_fixer_max_file_chars,
                        previous_errors=previous_errors,
                    )
                    fix_extra = {}
                if proposal["blockers"]:
                    # 原因が研究対象の外（ケース・仕様・環境）にあると、いくら直しても合格しない。研究対象は書き換えずに止める。
                    # AI が変えてよいケースの設定（制限時間・自動回答）だけは、止めた後に適用する（_stop_for_blockers）。
                    self._pending_case_settings = [e for e in proposal["edits"] if isinstance(e, dict) and "answers" in e]
                    fix = {"analysis": proposal["analysis"], "applied": [], "errors": [], "edits": 0, "blockers": proposal["blockers"], **fix_extra}
                    if self._pending_case_settings:
                        fix["case_settings"] = fixer.edit_summary(self._pending_case_settings)
                    break
                limit_error = fixer.too_many_files(proposal["edits"], max_files)
                if limit_error:
                    applied, errors = [], [limit_error]
                else:
                    if not snapshotted:
                        themes.snapshot_targets(directory, run_dir / BEFORE_FIX_DIRNAME)
                        snapshotted = True
                    applied, errors = fixer.apply_edits(
                        directory,
                        proposal["edits"],
                        known_tools=list(sources.subagent_tool_names()),
                        temp_root=temp_dirs.config_root(self._target_config(instance)),
                    )
                fix = {"analysis": proposal["analysis"], "applied": applied, "errors": errors, "edits": len(proposal["edits"]), **fix_extra}
                if applied:
                    fix["edit_summary"] = fixer.edit_summary(proposal["edits"])
                if applied or attempt == 2:
                    break
                fix["first_attempt_errors"] = errors
                previous_errors = errors
        except LabLLMError as e:
            fix = {"analysis": "", "applied": [], "errors": [str(e)]}
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "fix.json").write_text(json.dumps(fix, ensure_ascii=False, indent=2), encoding="utf-8")

        def _apply(d: dict) -> None:
            for it in d.get("iterations") or []:
                if it["n"] == n:
                    it["fix"] = fix
            if fix.get("blockers"):
                summary = "blockers: " + ", ".join(f"{b['kind']}{(':' + b['case_id']) if b['case_id'] else ''}" for b in fix["blockers"])
            elif fix["applied"]:
                summary = "fixed files: " + ", ".join(fix["applied"])
            else:
                summary = "could not fix: " + " / ".join(fix["errors"])
            themes.add_history(d, "AI", "fix", summary[:500])

        themes.update(directory, _apply)
        return fix.get("blockers") or []

    def _propose_with_agent(self, instance: str, directory: Path, outcome: dict, run_dir: Path, history: str, max_files: int) -> dict:
        """AI 修正を Agent で行う（admin/skill_lab/fixer_agent.py）。調べた手順は runs/<反復>/fixer_agent.json に残す。"""
        from . import fixer_agent

        cfg = self._cfg
        target_cfg = self._target_config(instance)
        llm_cfg = self._llm_config(instance, target_cfg)
        spec = (directory / themes.SPEC_FILENAME).read_text(encoding="utf-8")
        data = themes.read(directory)
        n = outcome["iteration"]["n"]

        def evaluate(*, case_ids, repeat, results_root, assets_base=None, config_patch_file=None, cases_dir=None):
            run = evaluation.run(
                directory,
                jobs=int(getattr(self._cfg, "skill_lab_eval_parallel_runs", 1)),
                instance=instance,
                lab=self.lab,
                repeat=repeat,
                results_root=results_root,
                case_ids=case_ids,
                base_env=self.base_env,
                should_stop=lambda: self._should_stop_step(directory),
                assets_base=assets_base,
                config_patch_file=config_patch_file,
                cases_dir=cases_dir,
                # 試しの評価の間も、何件目を実行中かを画面に出す（以前は1時間以上「try_fix」のまま止まって見えた）
                on_progress=lambda p: self._progress(directory, "fixing", n=n, agent_trial=True, **p),
            )
            self._record_eval_usage(self._usage_owner(data), run["results"])
            verdicts = judge.judge_results(
                llm_cfg, spec=spec, results=run["results"], excerpt_chars=cfg.skill_lab_transcript_excerpt_chars, max_workers=self._judge_workers()
            )
            return run["results"], verdicts

        def on_step(step: int, tool: str) -> None:
            # 前の試しの評価の「何件目」は消す（次のツールに移った）
            self._progress(
                directory, "fixing", n=n, agent_step=step, agent_tool=tool, agent_trial=False, done=None, total=None, case_id=None, repeat=None, repeats=None
            )

        # 今回の評価（取り消した直しの記録の代わりに、直す前の状態の評価を使うこともある）を必ず含める
        iterations = self._loop_iterations(data)
        if not any(it.get("n") == n for it in iterations):
            iterations.append(outcome["iteration"])
        failed = fixer.failed_cases_text(outcome["iteration"])
        task = (
            f"# Iterations so far in this loop (the fixes already tried, and the cases that still failed after them)\n{history}\n\n"
            f"# This evaluation\nIteration {outcome['iteration']['n']} ({outcome['iteration'].get('phase')}) failed: {failed}.\n"
            "Investigate with the tools (start with run_overview of this iteration), find the root cause, test your fix, "
            "then conclude with submit_fix or report_blockers."
        )
        proposal = fixer_agent.propose(
            llm_cfg,
            directory,
            system_knowledge=fixer.knowledge_text(),
            task=task,
            work_dir=run_dir / fixer_agent.AGENT_DIRNAME,
            target_cfg=target_cfg,
            iterations=iterations,
            runner=fixer_agent.Runner(evaluate=evaluate, should_stop=lambda: self._should_stop_step(directory), on_step=on_step),
            limits=fixer_agent.AgentLimits(
                max_steps=int(getattr(cfg, "skill_lab_fixer_agent_max_steps", 40)),
                max_trials=int(getattr(cfg, "skill_lab_fixer_agent_max_trials", 3)),
                trial_repeats=int(getattr(cfg, "skill_lab_fixer_agent_trial_repeats", 3)),
                read_chars=int(getattr(cfg, "skill_lab_fixer_agent_read_chars", 12000)),
                context_chars=int(getattr(cfg, "skill_lab_fixer_agent_context_chars", 150000)),
            ),
            known_tools=list(sources.subagent_tool_names()),
            temp_root=temp_dirs.config_root(target_cfg),
            max_files=max_files,
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "fixer_agent.json").write_text(
            json.dumps({"trials": proposal["trials"], "steps": proposal["steps"], "log": proposal["log"]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return proposal

    def _progress(self, directory: Path, step: str, **fields) -> None:
        """いまどの段階か（loop.progress）を記録する。画面の「実行の記録」と見出しに出す。

        以前は評価が1回終わって反復の行ができるまで何も出ず、動いているのか分からなかった（2026-11-13 ユーザー指摘）。
        step: baseline（改良前の版の評価）/ baseline_judging / eval / tryout / judging（AI judge・機械判定の見直し）/
        fixing（AI の分析と修正）/ report（総括の作成）。同じ段階の間は since（その段階を始めた時刻）を保つ。
        """
        now = themes.now_iso()

        def _apply(d: dict) -> None:
            loop = d.get("loop")
            if loop is None:
                return
            prev = loop.get("progress") or {}
            same = prev.get("step") == step and prev.get("n") == fields.get("n")
            loop["progress"] = {**(prev if same else {}), "step": step, **fields, "since": prev.get("since") if same else now, "at": now}

        try:
            themes.update(directory, _apply)
        except Exception as e:  # noqa: BLE001 - 進み具合を書けなくても処理は続ける
            logger.debug("Could not record progress of %s: %s", directory.name, e)

    def _judge_workers(self) -> int:
        """結果の判定を同時に行う数（このワーカーの [llm] max_concurrent_requests。0 以下は制限なし = 判定する件数まで）。"""
        value = int(getattr(self.config, "llm_max_concurrent_requests", 1))
        return value if value > 0 else 1000

    def _case_progress(self, directory: Path, step: str, **fields):
        """evaluation.run の on_progress（何問目・何回目）を loop.progress へ書く関数。"""
        return lambda p: self._progress(directory, step, **fields, **p)

    def _judge_progress(self, directory: Path, step: str, **fields):
        """judge.judge_results の on_progress（何件目を判定中か）を loop.progress へ書く関数。"""
        return lambda i, total, case_id: self._progress(directory, step, **fields, done=i, total=total, case_id=case_id)

    def _loop_iterations(self, data: dict) -> list[dict]:
        """このループ（開始以降）の反復。"""
        started = (data.get("loop") or {}).get("started_at") or ""
        return [it for it in data.get("iterations") or [] if str(it.get("at") or "") >= started]

    def _revert_if_regressed(self, directory: Path, outcome: dict) -> dict | None:
        """直前の AI 修正で、それまで全回合格していたケースが落ちたら、その直しを取り消す。

        tune-prompt と同じく「合格しているケースを壊さない」ため。取り消したら、取り消した直しの前の状態の
        評価の記録で直させ直すための outcome を返す（今回の評価は取り消した直しの記録なので使わない）。取り消さなければ None。
        """
        data = themes.read(directory)
        its = self._loop_iterations(data)
        if len(its) < 2:
            return None
        cur, prev = its[-1], its[-2]
        snapshot = directory / themes.RUNS_DIRNAME / f"{prev['n']:03d}" / BEFORE_FIX_DIRNAME
        if not (prev.get("fix") or {}).get("applied") or not snapshot.is_dir():
            return None
        cur_cases = cur.get("cases") or {}
        broken = sorted(
            cid
            for cid, c in (prev.get("cases") or {}).items()
            if c.get("pass") and not c.get("fail") and not c.get("error")
            and (cur_cases.get(cid, {}).get("fail") or cur_cases.get(cid, {}).get("error"))
        )
        if not broken:
            return None
        themes.restore_targets(
            directory, snapshot, actor="AI", message=f"reverted the fix of iteration {prev['n']} because it broke {', '.join(broken)}"
        )

        def _apply(d: dict) -> None:
            for it in d.get("iterations") or []:
                if it["n"] == cur["n"]:
                    it["reverted"] = {"fix_of_iteration": prev["n"], "broken_cases": broken}
                if it["n"] == prev["n"] and it.get("fix") is not None:
                    it["fix"]["reverted"] = {"by_iteration": cur["n"], "broken_cases": broken}
            if d.get("loop") is not None:
                d["loop"]["reverts"] = d["loop"].get("reverts", 0) + 1
            themes.add_history(d, "AI", "revert", f"iteration {prev['n']}: broke {', '.join(broken)}")

        themes.update(directory, _apply)
        try:
            results = json.loads((Path(prev["out_dir"]) / "results.json").read_text(encoding="utf-8"))
        except (OSError, KeyError, ValueError):
            results = []
        return {"iteration": {**cur}, "run": {"results": results}, "verdicts": prev.get("verdicts") or [], "passed": False}

    def _stop_for_reverts(self, directory: Path) -> bool:
        """直しの取り消しが続いたら止める（ケースどうしがぶつかる・答えがゆらいでいる見込みが高い）。"""
        data = themes.read(directory)
        limit = int(getattr(self._cfg, "skill_lab_fixer_max_reverts", 3))
        reverts = (data.get("loop") or {}).get("reverts", 0)
        if limit <= 0 or reverts < limit:
            return False
        broken = sorted({c for it in self._loop_iterations(data) for c in (it.get("reverted") or {}).get("broken_cases", [])})
        messages = {
            "ja": f"AI 修正が、合格していたケース（{', '.join(broken)}）を壊して取り消されることが {reverts} 回あったため、止めました。"
            "ケースどうしが矛盾している（一方を直すと他方が落ちる）か、答えがゆらいでいて偶然合格・不合格になっている見込みがあります。"
            "落ちたケースの記録を見て、ケースか仕様を見直してください。",
            "en": f"Stopped because AI fixes broke cases that had passed ({', '.join(broken)}) and were reverted {reverts} times. "
            "The cases may contradict each other (fixing one breaks another), or the answers vary so that cases pass or fail by chance. "
            "Review the cases or the spec using the records of the failed cases.",
        }
        self._finish(directory, themes.STATUS_STALLED, messages.get(current_output_language(), messages["ja"]))
        return True

    def _restore_best(self, directory: Path) -> str | None:
        """ループで一番合格の多かった内容へ戻す（今の内容がそれと違うとき）。戻したら説明を返す。

        最後の直しは評価されていないことが多く、それまでの直しで悪くなっていることもあるため、上限到達・行き詰まり・
        人の見直しが必要で止まったときは、評価で確かめた一番よい内容を残す（一度も合格が無ければ戻さない）。
        """
        if not getattr(self._cfg, "skill_lab_restore_best_state", True):
            return None
        data = themes.read(directory)
        best = (data.get("loop") or {}).get("best") or {}
        snapshot = directory / themes.RUNS_DIRNAME / BEST_DIRNAME
        if not best or tuple(best.get("score") or (0, 0)) <= (0, 0) or not snapshot.is_dir():
            return None
        if themes.state_sha256(directory) == best.get("state_sha"):
            return None
        themes.restore_targets(directory, snapshot, actor=f"skill tuning worker {self.lab}", message=f"restored the best state (iteration {best['n']})")
        full, ratio = best["score"]
        messages = {
            "ja": f"研究対象を、このループで一番よかった反復 {best['n']} の内容に戻しました（全回合格のケース {full} 件、合格率 {ratio:.0%}）。",
            "en": f"The research targets were restored to the best state of this loop, iteration {best['n']} "
            f"({full} case(s) passed every run, pass rate {ratio:.0%}).",
        }
        return messages.get(current_output_language(), messages["ja"])

    def _evaluate_baseline(self, instance: str, directory: Path) -> None:
        """取り込んだときの版（改良前。baseline/assets/）を1回評価し、改良前後の比較に使う。

        取り込んだ資産（正式スキル・ドラフトの複製）が無い・ケースが前回の評価から変わっていないときは評価しない。
        新しく作った資産は改良前には無いものとして評価する（設定パッチも重ねない）。
        """
        if not getattr(self._cfg, "skill_lab_evaluate_baseline", True):
            return
        base = directory / themes.BASELINE_DIRNAME / themes.ASSETS_DIRNAME
        assets = themes.assets_in(base)
        if not assets["skills"] and not assets["agents"]:
            return
        data = themes.read(directory)
        cases_sha = themes.content_hashes(directory)["cases"]
        if (data.get("baseline_eval") or {}).get("cases_sha") == cases_sha:
            return
        self._start_step_clock()
        self._progress(directory, "baseline")
        run = evaluation.run(
            directory,
            jobs=int(getattr(self._cfg, "skill_lab_eval_parallel_runs", 1)),
            instance=instance,
            lab=self.lab,
            repeat=1,
            results_root=directory / themes.RUNS_DIRNAME / "baseline",
            base_env=self.base_env,
            should_stop=lambda: self._should_stop_step(directory),
            assets_base=base,
            config_patch=False,
            on_progress=self._case_progress(directory, "baseline"),
        )
        self._record_eval_usage(self._usage_owner(data), run["results"])
        verdicts = judge.judge_results(
            self._llm_config(instance),
            spec=(directory / themes.SPEC_FILENAME).read_text(encoding="utf-8"),
            results=run["results"],
            excerpt_chars=self._cfg.skill_lab_transcript_excerpt_chars,
            on_progress=self._judge_progress(directory, "baseline_judging"),
            max_workers=self._judge_workers(),
        )
        per_case: dict[str, dict[str, int]] = {}
        for v in verdicts:
            c = per_case.setdefault(v["case_id"], {"pass": 0, "fail": 0, "error": 0})
            c["pass" if v["outcome"] == "pass" else ("error" if v["outcome"] == "error" else "fail")] += 1
        result = {
            "at": themes.now_iso(),
            "cases": per_case,
            "cases_sha": cases_sha,
            "out_dir": run["out_dir"],
            "score": list(_score({"cases": per_case})),
        }
        themes.update(directory, lambda d: d.update({"baseline_eval": result}))

    def _write_report(self, directory: Path, status: str) -> None:
        """ループ・トライアウトの終わりに、総括（反復ごとの結果・直したもの・改良前との比較・AI のまとめ）を残す。"""
        data = themes.read(directory)
        its = self._loop_iterations(data)
        rows = []
        for it in its:
            fix = it.get("fix") or {}
            rows.append(
                {
                    "n": it["n"],
                    "phase": it.get("phase"),
                    "verdict": it.get("verdict"),
                    "failed": fixer.failed_cases_text(it),
                    "stopped_early": bool(it.get("stopped_early")),
                    "fixed": fix.get("applied") or [],
                    "analysis": str(fix.get("analysis") or "")[:400],
                    "reverted": bool(fix.get("reverted")),
                    "blockers": len(fix.get("blockers") or []),
                }
            )
        last = its[-1] if its else None
        loop = data.get("loop") or {}
        report = {
            "status": status,
            "at": themes.now_iso(),
            "iterations": rows,
            "final": {"n": last["n"], "score": list(_score(last)), "cases": last.get("cases")} if last else None,
            "best": loop.get("best"),
            "restored_best": loop.get("restored_best"),
            "baseline": {k: v for k, v in (data.get("baseline_eval") or {}).items() if k in ("cases", "score", "at")} or None,
            "message": loop.get("message"),
        }
        if getattr(self._cfg, "skill_lab_report_ai_summary", True) and rows:
            self._progress(directory, "report")
            try:
                report["summary"] = ask_text(
                    self._llm_config(data.get("instance") or ""), _REPORT_SYSTEM, json.dumps(report, ensure_ascii=False, default=str)[:12000]
                )
            except Exception as e:  # noqa: BLE001 - まとめが書けなくても総括の表は残す
                logger.warning("Could not write the AI summary of theme %s: %s", directory.name, e)

        def _apply(d: dict) -> None:
            if d.get("loop") is not None:
                d["loop"]["report"] = report

        themes.update(directory, _apply)

    def _stop_if_stalled(self, directory: Path) -> bool:
        """AI 修正の後の内容が、このループで既に評価して不合格だった内容と同じなら止める（状態 stalled）。

        過去の修正を元に戻す・2つの直し方を行ったり来たりする・直せないまま同じ内容で評価し直す、を
        繰り返すと、反復の上限まで同じ失敗を繰り返すだけになる（2026-11-13 ユーザー指摘）。
        直前の反復と同じ内容（修正を適用できなかった）は、一時的な失敗のこともあるので1回だけ評価し直す。
        """
        data = themes.read(directory)
        started = (data.get("loop") or {}).get("started_at") or ""
        failed = [
            it for it in data.get("iterations") or []
            if it.get("verdict") == "fail" and it.get("state_sha") and str(it.get("at") or "") >= started
        ]
        if not failed:
            return False
        now = themes.state_sha256(directory)
        same = [it for it in failed if it["state_sha"] == now]
        # 直前の反復と同じ（直しを適用できなかった）なら1回は評価し直す。ただし取り消した直しをまた入れたのなら止める。
        if not same or (same == [failed[-1]] and not failed[-1].get("reverted")):
            return False
        first = same[0]["n"]
        messages = {
            "ja": f"AI 修正が、このループの反復 {first} で評価して不合格だったのと同じ内容に戻したため、止めました"
            "（同じ直しの繰り返し・行ったり来たり、または直せないまま同じ内容が続いています）。"
            "反復の記録の分析を見て、研究対象・仕様・ケースを人が見直してください。",
            "en": f"Stopped because the AI fix returned to the same content that was evaluated and failed in iteration {first} of this loop "
            "(the same fix repeated, fixes going back and forth, or no change could be made). "
            "Review the research targets, the spec and the cases using the analyses in the iteration records.",
        }
        self._finish(directory, themes.STATUS_STALLED, messages.get(current_output_language(), messages["ja"]))
        return True

    def _stop_for_blockers(self, directory: Path, problems: list[dict]) -> bool:
        """AI 修正が研究対象の外の原因（ケース・仕様・環境）を指摘したら、ループを止めて人に報告する（状態 blocked）。"""
        if not problems:
            return False

        def _apply(d: dict) -> None:
            if d.get("loop") is not None:
                d["loop"]["blockers"] = problems

        themes.update(directory, _apply)
        lines = [
            f"[{p['kind']}{(' ' + p['case_id']) if p.get('case_id') else ''}] {p['problem']}" + (f" → {p['suggestion']}" if p.get("suggestion") else "")
            for p in problems
        ]
        self._finish(directory, themes.STATUS_BLOCKED, "\n".join(lines)[:2000])
        # 一番よかった版へ戻した後に、AI が変えてよいケースの設定（制限時間・自動回答）を入れる。人がケースを直して
        # 再開したとき、同じ時間切れ・質問で落ちないように（先に入れると、戻す処理で巻き戻る）
        settings, self._pending_case_settings = self._pending_case_settings, []
        if settings:
            _, errors = fixer.apply_edits(directory, settings, known_tools=None)
            if errors:
                logger.warning("Could not apply the case settings of the AI fix: %s", errors)
        return True

    def _loop(self, instance: str, directory: Path, tryout_repeats: int) -> None:
        self._evaluate_baseline(instance, directory)
        while True:
            if self._stop_requested(directory):
                raise _Stop()
            limit = self._check_limits(directory)
            if limit:
                self._finish(directory, themes.STATUS_EXHAUSTED, limit)
                return
            # 合間に、他のテーマの短いタスク（1回試行・下書き）と、一時フォルダのごみを片付ける。
            self.process_tasks()
            self._maybe_sweep_temp_dirs()
            outcome = self._evaluate(instance, directory, 1, "eval")
            if self._stop_for_infra(directory, outcome):
                return
            if not outcome["passed"]:
                reverted = self._revert_if_regressed(directory, outcome)
                if reverted is not None:
                    if self._stop_for_reverts(directory):
                        return
                    outcome = reverted
                if self._stop_for_blockers(directory, self._fix(instance, directory, outcome)) or self._stop_if_stalled(directory):
                    return
                continue
            if self._stop_requested(directory):
                raise _Stop()
            outcome = self._evaluate(instance, directory, tryout_repeats, "tryout")
            if self._stop_for_infra(directory, outcome):
                return
            if outcome["passed"]:
                self._finish(directory, themes.STATUS_REVIEW, f"Passed every run of the tryout ({tryout_repeats} runs per case)")
                return
            if self._stop_for_blockers(directory, self._fix(instance, directory, outcome)) or self._stop_if_stalled(directory):
                return

    def _stop_for_infra(self, directory: Path, outcome: dict) -> bool:
        """研究対象を直しても解決しない失敗なら、AI 修正に回さずエラーで止める。"""
        message = evaluation.infra_error(outcome["run"]["results"])
        if message:
            self._finish(directory, themes.STATUS_ERROR, message)
            return True
        return False

    def _tryout_only(self, instance: str, directory: Path, tryout_repeats: int) -> None:
        outcome = self._evaluate(instance, directory, tryout_repeats, "tryout")
        if self._stop_for_infra(directory, outcome):
            return
        if outcome["passed"]:
            self._finish(directory, themes.STATUS_REVIEW, f"Passed every run of the tryout ({tryout_repeats} runs per case)")
        else:
            self._finish(directory, themes.STATUS_DRAFT, "The tryout had failures (fix them with the skill tuning loop or by hand)")


def main() -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m admin.skill_lab.worker")
    parser.add_argument("--lab", required=True, help="Skill tuning worker name (instances/<name>/, kind=skill_lab)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.chdir(PROJECT_ROOT)

    # 評価の子プロセスへは研究室の環境変数（.env）を渡さないため、適用前の環境を控える。
    base_env = dict(os.environ)
    from evals.instance import apply_instance, resolve_instance_name

    lab = resolve_instance_name(args.lab)
    from src.config import resolve_instances_root

    meta = inst.read_instance(resolve_instances_root(), lab)
    if not meta.is_skill_lab:
        raise SystemExit(f"Instance {lab} is not a skill tuning worker (kind=skill_lab).")
    apply_instance(lab)
    # 判定を複数スレッドで同時に行う（judge.judge_results）。LLM の同時数ガード（asyncio.Semaphore）は別々のイベントループ
    # から使えないため、ワーカーでは外し、同時数は判定のスレッドの数（_judge_workers）で守る。
    from src.llm import init_llm_concurrency

    init_llm_concurrency(0)

    from src import instance_lock
    from src.config import load_config

    cfg = load_config()
    instance_lock.acquire(cfg.checkpoint_db.parent / "app.lock")
    if cfg.log_level != "none":
        # 本体と同じ app_*.log 形式で書き、管理ツールの「ログ」タブで見られるようにする
        # （admin/monitor.py の行の形式に合わせ、thread の欄は「-」にする）。
        from src.log_rotation import LineCountRotatingFileHandler

        handler = LineCountRotatingFileHandler(cfg.log_dir, cfg.log_max_lines, cfg.log_clear_on_startup)
        handler.setFormatter(logging.Formatter("%(asctime)s [thread=-] %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)

    worker = Worker(lab, base_env)

    def _on_signal(signum, _frame):
        logger.info("Received a stop request (signal %s). Stopping the current work and exiting.", signum)
        worker.shutdown = True

    for sig in ("SIGBREAK", "SIGINT", "SIGTERM"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _on_signal)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
