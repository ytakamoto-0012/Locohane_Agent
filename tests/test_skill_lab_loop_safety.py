"""スキル研究室のループが、直しで悪くしない・同じ失敗を繰り返さないこと（2026-11-13 ユーザー指摘、tune-prompt の規則）。

- 合格していたケースを直しで落としたら、その直しを取り消す（取り消しが続いたら止める）
- 止まったときは、一番合格の多かった内容に戻す
- 1回の修正で触るファイルは少なく（多すぎる提案・適用できない提案は評価し直さずに出し直させる）
- トライアウトは最初の不合格で打ち切る
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from admin import instances as inst
from admin.skill_lab import fixer, promotion, themes
from admin.skill_lab import worker as worker_mod


def _skill_md(name: str, body: str = "") -> str:
    return f"---\nname: {name}\ndescription: テスト用のスキル。\n---\n\n# {name}\n{body}"


@pytest.fixture
def lab_env(tmp_path, monkeypatch):
    root = tmp_path / "instances"
    monkeypatch.setenv("INSTANCES_DIR", str(root))
    for key in ("LOCOHANE_INSTANCE", "CONFIG_OVERRIDES_PATH", "LOCOHANE_INSTANCE_ENV", "COMMON_DATA_DIR", "PROJECT_LOCOHANE_DIR"):
        monkeypatch.delenv(key, raising=False)
    for name, kind in (("org_a", inst.KIND_APP), ("lab1", inst.KIND_SKILL_LAB)):
        (root / name).mkdir(parents=True)
        inst.write_instance(root, inst.InstanceMeta(name=name, display_name=name, app_port=18001 if name == "org_a" else 0, kind=kind))
        (root / name / "config_overrides.json").write_text(
            json.dumps({"paths": {"common_data_dir": str(tmp_path / "data" / name)}}), encoding="utf-8"
        )
    cfg = promotion.instance_config(root, "org_a")
    directory = themes.create_theme(themes.themes_root(cfg.common_data_dir), instance="org_a", title="t", lab="lab1", actor="admin")
    themes.add_asset_new(directory, "skills", "my-skill", _skill_md("my-skill", "v0\n"), actor="admin")
    themes.write_text(directory, "cases/a.yaml", "id: a\ntarget: skill_lab\nturns: [x]\njudge: y\n", actor="admin")
    themes.write_text(directory, "cases/b.yaml", "id: b\ntarget: skill_lab\nturns: [x]\njudge: y\n", actor="admin")
    return {"root": root, "dir": directory}


def _worker(lab_env, **config):
    # ループの上限は対象インスタンス（org_a）の [skill_lab] から読む
    overrides = lab_env["root"] / "org_a" / "config_overrides.json"
    data = json.loads(overrides.read_text(encoding="utf-8"))
    data.setdefault("skill_lab", {})["max_iterations"] = config.pop("skill_lab_max_iterations", 10)
    overrides.write_text(json.dumps(data), encoding="utf-8")
    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.lab, w.instances_root, w.shutdown, w.base_env = "lab1", lab_env["root"], False, dict(os.environ)
    w.config = type(
        "C",
        (),
        {
            "skill_lab_transcript_excerpt_chars": 1000,
            "skill_lab_fixer_max_file_chars": 1000,
            "skill_lab_report_ai_summary": False,
            "skill_lab_evaluate_baseline": False,
            **config,
        },
    )()
    return w


def _skill(lab_env) -> Path:
    return lab_env["dir"] / "assets" / "skills" / "my-skill" / "SKILL.md"


def _scripted_loop(monkeypatch, lab_env, outcomes, fixes, **config):
    """評価結果（ケースごとの合否の並び）と修正（SKILL.md の本文）を台本どおりに進める。"""
    w = _worker(lab_env, **config)
    directory = lab_env["dir"]
    outcomes = list(outcomes)
    fixes = list(fixes)
    fixed_from = []

    def fake_evaluate(instance, d, repeat, phase):
        cases = outcomes.pop(0)
        data = themes.read(d)
        n = len(data.get("iterations") or []) + 1
        run_dir = d / themes.RUNS_DIRNAME / f"{n:03d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        body = _skill(lab_env).read_text(encoding="utf-8")
        results = [{"case_id": c, "transcript": [{"type": "AIMessage", "content": body}]} for c in cases]
        (run_dir / "results.json").write_text(json.dumps(results), encoding="utf-8")
        verdicts = [{"case_id": c, "repeat_index": 1, "outcome": "pass" if ok else "fail"} for c, ok in cases.items()]
        per_case = {c: {"pass": int(ok), "fail": int(not ok), "error": 0} for c, ok in cases.items()}
        passed = all(cases.values())
        it = {"n": n, "phase": phase, "repeat": repeat, "verdict": "pass" if passed else "fail", "cases": per_case,
              "at": themes.now_iso(), "state_sha": themes.state_sha256(d), "out_dir": str(run_dir), "verdicts": verdicts}
        best = (data.get("loop") or {}).get("best") or {}
        score = worker_mod._score(it)
        new_best = not best or tuple(best["score"]) < score
        if new_best:
            themes.snapshot_targets(d, d / themes.RUNS_DIRNAME / worker_mod.BEST_DIRNAME)

        def _apply(data):
            data.setdefault("iterations", []).append(it)
            data["loop"]["iterations_used"] = data["loop"].get("iterations_used", 0) + 1
            if new_best:
                data["loop"]["best"] = {"n": n, "phase": phase, "score": list(score), "state_sha": it["state_sha"]}

        themes.update(d, _apply)
        return {"iteration": it, "run": {"results": results}, "verdicts": verdicts, "passed": passed}

    def fake_propose(config, d, *, failures, history, max_file_chars, previous_errors=None):
        body = fixes.pop(0)
        fixed_from.append(failures)
        return {"analysis": f"fix to {body}", "blockers": [], "edits": [{"path": "assets/skills/my-skill/SKILL.md", "content": _skill_md("my-skill", body)}]}

    monkeypatch.setattr(w, "_evaluate", fake_evaluate)
    monkeypatch.setattr(w, "process_tasks", lambda: False)
    monkeypatch.setattr(w, "_llm_config", lambda *a, **k: None)
    monkeypatch.setattr(w, "_target_config", lambda instance: promotion.instance_config(lab_env["root"], instance))
    monkeypatch.setattr(fixer, "propose", fake_propose)
    monkeypatch.setattr(worker_mod.sources, "subagent_tool_names", lambda: ("Read",))
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop"}}))
    return w, fixed_from


def test_fix_that_breaks_a_passing_case_is_reverted(lab_env, monkeypatch):
    # 1: a 合格・b 不合格 → v1 に直す → 2: a が落ちた（b は合格）→ v1 を取り消して v0 に戻し、1 の記録で直し直す（v2）→ 3: 全合格
    outcomes = [{"a": True, "b": False}, {"a": False, "b": True}, {"a": True, "b": True}, {"a": True, "b": True}]
    w, fixed_from = _scripted_loop(monkeypatch, lab_env, outcomes, ["v1\n", "v2\n"])
    w.run_theme("org_a", lab_env["dir"])
    data = themes.read(lab_env["dir"])
    assert data["status"] == themes.STATUS_REVIEW
    assert data["iterations"][1]["reverted"] == {"fix_of_iteration": 1, "broken_cases": ["a"]}
    assert data["iterations"][0]["fix"]["reverted"]["broken_cases"] == ["a"]
    assert data["loop"]["reverts"] == 1
    # 取り消した後は、取り消した直しの前（v0）の評価の記録で直し直した
    assert "v0" in fixed_from[1] and "v1" not in fixed_from[1]
    assert "v2" in _skill(lab_env).read_text(encoding="utf-8")


def test_loop_stops_when_fixes_keep_breaking_cases(lab_env, monkeypatch):
    outcomes = [{"a": True, "b": False}, {"a": False, "b": True}] * 3
    w, _ = _scripted_loop(monkeypatch, lab_env, outcomes, ["v1\n", "v2\n", "v3\n"], skill_lab_fixer_max_reverts=2)
    w.run_theme("org_a", lab_env["dir"])
    data = themes.read(lab_env["dir"])
    assert data["status"] == themes.STATUS_STALLED and data["loop"]["reverts"] == 2
    assert "a" in data["loop"]["message"]


def test_stopping_at_the_limit_restores_the_best_state(lab_env, monkeypatch):
    # 1: a だけ合格（一番よい）→ v1 → 2: 全不合格（取り消し）… 上限で止まったら、一番よかった v0 に戻す
    outcomes = [{"a": True, "b": False}, {"a": False, "b": False}, {"a": False, "b": False}]
    w, _ = _scripted_loop(monkeypatch, lab_env, outcomes, ["v1\n", "v2\n", "v3\n"], skill_lab_max_iterations=3, skill_lab_fixer_max_reverts=0)
    w.run_theme("org_a", lab_env["dir"])
    data = themes.read(lab_env["dir"])
    assert data["status"] == themes.STATUS_EXHAUSTED
    assert "v0" in _skill(lab_env).read_text(encoding="utf-8")
    assert data["loop"]["restored_best"] and "1" in data["loop"]["restored_best"]
    assert data["loop"]["report"]["iterations"][0]["n"] == 1


def test_too_many_files_is_asked_again_without_evaluating(lab_env, monkeypatch):
    w = _worker(lab_env, skill_lab_fixer_max_files_per_fix=1)
    calls = []

    def fake_propose(config, d, *, failures, history, max_file_chars, previous_errors=None):
        calls.append(previous_errors)
        edits = [{"path": "assets/skills/my-skill/SKILL.md", "content": _skill_md("my-skill", "v1\n")}]
        if previous_errors is None:
            edits.append({"path": "assets/skills/my-skill/references/r.md", "content": "x"})
        return {"analysis": "", "blockers": [], "edits": edits}

    monkeypatch.setattr(fixer, "propose", fake_propose)
    monkeypatch.setattr(w, "_llm_config", lambda *a, **k: None)
    monkeypatch.setattr(w, "_target_config", lambda instance: promotion.instance_config(lab_env["root"], instance))
    monkeypatch.setattr(worker_mod.sources, "subagent_tool_names", lambda: ("Read",))
    themes.update(lab_env["dir"], lambda d: d.update({"iterations": [{"n": 1, "phase": "eval", "verdict": "fail", "cases": {}}]}))
    w._fix("org_a", lab_env["dir"], {"iteration": {"n": 1}, "run": {"results": []}, "verdicts": []})
    assert calls[0] is None and "at most 1 file" in calls[1][0]
    assert "v1" in _skill(lab_env).read_text(encoding="utf-8")
    assert not (lab_env["dir"] / "assets" / "skills" / "my-skill" / "references" / "r.md").exists()
    # 直す前の内容を控えている（取り消し用）
    assert (lab_env["dir"] / "runs" / "001" / worker_mod.BEFORE_FIX_DIRNAME / "assets" / "skills" / "my-skill" / "SKILL.md").is_file()


def test_run_all_stops_tryout_at_the_first_failure(tmp_path, monkeypatch):
    from evals import run_all

    cases = tmp_path / "cases"
    cases.mkdir()
    for cid in ("a", "b"):
        (cases / f"{cid}.yaml").write_text(f"id: {cid}\ntarget: x\nturns: [x]\njudge: y\n", encoding="utf-8")
    runs = []

    def fake_run_one(case_path, instance_name, extra_args):
        runs.append(case_path.stem)
        return {"case_id": case_path.stem, "rules_pass": case_path.stem == "a" or len(runs) < 3}

    monkeypatch.setattr(run_all, "_run_one", fake_run_one)
    monkeypatch.setattr(run_all, "resolve_instance_name", lambda _name: "default")
    results = tmp_path / "results"
    monkeypatch.setattr(sys, "argv", ["run_all.py", "--cases-dir", str(cases), "--repeat", "10", "--results-dir", str(results), "--stop-on-fail"])
    assert run_all.main() == 0
    assert runs == ["a", "b", "a", "b"]  # 2回目の b で落ちたので、残り8回は回さない
    tryout = json.loads(next(results.glob("*/tryout.json")).read_text(encoding="utf-8"))
    assert tryout["verdict"] == "fail" and tryout["stopped_early"] is True
