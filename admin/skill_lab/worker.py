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
from datetime import datetime, timedelta
from pathlib import Path

from src.config import PROJECT_ROOT

from .. import instances as inst
from . import drafting, evaluation, fixer, judge, sources, themes
from .llm import LabLLMError

logger = logging.getLogger("skill_lab.worker")


class _Stop(Exception):
    pass


class Worker:
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

    def run_forever(self) -> None:
        logger.info("Skill tuning worker %s started.", self.lab)
        recovered = False
        while not self.shutdown:
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

    def _run_task(self, instance: str, directory: Path, task: dict) -> None:
        self._set_task(directory, task["id"], status="running", started_at=themes.now_iso())
        params = task.get("params") or {}
        cfg = self.config
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
            instance=instance,
            lab=self.lab,
            repeat=1,
            # 結果のフォルダ名は秒単位の日時のため、試行ごとにフォルダを分けて取り違えないようにする。
            results_root=directory / themes.RUNS_DIRNAME / "trials" / task_id,
            case_ids=[case_id],
            base_env=self.base_env,
            should_stop=lambda: self.shutdown,
        )
        spec = (directory / themes.SPEC_FILENAME).read_text(encoding="utf-8")
        verdicts = judge.judge_results(
            self._llm_config(instance), spec=spec, results=run["results"], excerpt_chars=self.config.skill_lab_transcript_excerpt_chars
        )
        r = run["results"][0] if run["results"] else {}
        return {
            "out_dir": run["out_dir"],
            "case_id": case_id,
            "outcome": verdicts[0]["outcome"] if verdicts else "error",
            "ai_judge": verdicts[0].get("ai_judge") if verdicts else None,
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
        cfg = self.config
        data = themes.read(directory)
        loop = data.get("loop") or {}
        mode = loop.get("mode", "loop")
        if not loop.get("deadline"):
            loop = {
                **loop,
                "mode": mode,
                "started_at": themes.now_iso(),
                "deadline": (datetime.now().astimezone() + timedelta(hours=cfg.skill_lab_max_hours)).isoformat(timespec="seconds"),
                "iterations_used": 0,
            }

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
        try:
            target_cfg = self._target_config(instance)
            if mode == "tryout":
                self._tryout_only(instance, directory, target_cfg.skill_tryout_repeats)
            else:
                self._loop(instance, directory, target_cfg.skill_tryout_repeats)
        except (evaluation.EvaluationStopped, _Stop):
            if self.shutdown:
                self._finish(directory, themes.STATUS_QUEUED, "interrupted because the lab stopped (resumes where it left off when restarted)")
            else:
                self._finish(directory, themes.STATUS_STOPPED, "interrupted by a stop request")
        except Exception as e:  # noqa: BLE001 - テーマ単位で失敗させ、ワーカーは続ける
            logger.error("Processing of theme %s failed: %s\n%s", directory.name, e, traceback.format_exc())
            self._finish(directory, themes.STATUS_ERROR, f"{type(e).__name__}: {e}"[:2000])

    def _finish(self, directory: Path, status: str, detail: str) -> None:
        def _apply(d: dict) -> None:
            d["status"] = status
            if d.get("loop"):
                d["loop"]["stop_requested"] = False
                if status != themes.STATUS_QUEUED:
                    d["loop"]["deadline"] = None
                d["loop"]["message"] = detail
            themes.add_history(d, f"skill tuning worker {self.lab}", f"loop_{status}", detail)

        themes.update(directory, _apply)

    def _check_limits(self, directory: Path) -> str | None:
        data = themes.read(directory)
        loop = data.get("loop") or {}
        if loop.get("iterations_used", 0) >= self.config.skill_lab_max_iterations:
            return f"Reached the iteration limit ({self.config.skill_lab_max_iterations})"
        deadline = loop.get("deadline")
        if deadline and datetime.now().astimezone() > datetime.fromisoformat(deadline):
            return f"Reached the time limit ({self.config.skill_lab_max_hours} hours)"
        return None

    def _evaluate(self, instance: str, directory: Path, repeat: int, phase: str) -> dict:
        """評価して AI judge まで行い、反復として記録する。"""
        data = themes.read(directory)
        n = len(data.get("iterations") or []) + 1
        run_dir = directory / themes.RUNS_DIRNAME / f"{n:03d}"
        run = evaluation.run(
            directory,
            instance=instance,
            lab=self.lab,
            repeat=repeat,
            results_root=run_dir,
            base_env=self.base_env,
            should_stop=lambda: self._stop_requested(directory),
        )
        spec = (directory / themes.SPEC_FILENAME).read_text(encoding="utf-8")
        verdicts = judge.judge_results(
            self._llm_config(instance), spec=spec, results=run["results"], excerpt_chars=self.config.skill_lab_transcript_excerpt_chars
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
        }

        def _apply(d: dict) -> None:
            d.setdefault("iterations", []).append(iteration)
            if d.get("loop") is not None:
                d["loop"]["iterations_used"] = d["loop"].get("iterations_used", 0) + 1
            if phase == "tryout" and passed:
                d["tryout"] = {
                    **run["tryout"],
                    "out_dir": run["out_dir"],
                    "at": themes.now_iso(),
                    "ai_verdict": "pass",
                    "ai_judged": any(v.get("ai_judge") for v in verdicts),
                    "iteration": n,
                }

        themes.update(directory, _apply)
        return {"iteration": iteration, "run": run, "verdicts": verdicts, "passed": passed}

    def _fix(self, instance: str, directory: Path, outcome: dict) -> None:
        cfg = self.config
        data = themes.read(directory)
        failures = fixer.failures_text(outcome["run"]["results"], outcome["verdicts"], cfg.skill_lab_transcript_excerpt_chars)
        history = fixer.history_text(data.get("iterations") or [])
        fix: dict = {}
        try:
            proposal = fixer.propose(self._llm_config(instance), directory, failures=failures, history=history, max_file_chars=cfg.skill_lab_fixer_max_file_chars)
            applied, errors = fixer.apply_edits(directory, proposal["edits"], known_tools=list(sources.subagent_tool_names()))
            fix = {"analysis": proposal["analysis"], "applied": applied, "errors": errors, "edits": len(proposal["edits"])}
        except LabLLMError as e:
            fix = {"analysis": "", "applied": [], "errors": [str(e)]}
        n = outcome["iteration"]["n"]
        run_dir = directory / themes.RUNS_DIRNAME / f"{n:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "fix.json").write_text(json.dumps(fix, ensure_ascii=False, indent=2), encoding="utf-8")

        def _apply(d: dict) -> None:
            for it in d.get("iterations") or []:
                if it["n"] == n:
                    it["fix"] = fix
            themes.add_history(
                d, "AI", "fix", ("fixed files: " + ", ".join(fix["applied"])) if fix["applied"] else ("could not fix: " + " / ".join(fix["errors"]))[:500]
            )

        themes.update(directory, _apply)

    def _loop(self, instance: str, directory: Path, tryout_repeats: int) -> None:
        while True:
            if self._stop_requested(directory):
                raise _Stop()
            limit = self._check_limits(directory)
            if limit:
                self._finish(directory, themes.STATUS_EXHAUSTED, limit)
                return
            # 合間に、他のテーマの短いタスク（1回試行・下書き）を片付ける。
            self.process_tasks()
            outcome = self._evaluate(instance, directory, 1, "eval")
            if self._stop_for_infra(directory, outcome):
                return
            if not outcome["passed"]:
                self._fix(instance, directory, outcome)
                continue
            if self._stop_requested(directory):
                raise _Stop()
            outcome = self._evaluate(instance, directory, tryout_repeats, "tryout")
            if self._stop_for_infra(directory, outcome):
                return
            if outcome["passed"]:
                self._finish(directory, themes.STATUS_REVIEW, f"Passed every run of the tryout ({tryout_repeats} runs per case)")
                return
            self._fix(instance, directory, outcome)

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
