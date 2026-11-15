"""スキル研究室（admin/skill_lab/）と、インスタンス専用の拡張ディレクトリ（[skill_lab].instance_locohane_dir）の回帰テスト。

- インスタンス専用の置き場は最優先で走査され、他のインスタンスには出ない（組織別・役割別のインスタンスを汚染しない）
- 評価は対象インスタンスの構成に、研究室の LLM 接続先・テーマの資産・設定パッチを重ねて行う
- AI 修正は決まった場所だけを書き換え、形式違反なら全部元に戻す
- ワーカーは「評価 → 修正 → 再評価 → トライアウト」を回し、上限で止まる
- 昇格は対象インスタンス専用の置き場にだけ置き、設定を登録し、取り込み元のドラフトをアーカイブする
- テーマはインスタンスごとに分かれ、API は他のインスタンスのテーマに触れない
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from admin import instances as inst
from admin.skill_lab import fixer, promotion, themes
from evals import config_patch
from evals.skill_tree import cases_sha256, tree_sha256


def _skill_md(name: str, desc: str = "テスト用のスキル。") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n"


def _agent_md(name: str, tools: str = "Read, Grep") -> str:
    return f"---\nname: {name}\ndescription: テスト用のサブエージェント。\ntools: {tools}\n---\n\n本文\n\n{{{{skills}}}}\n"


_CASE = "id: '{id}'\ntarget: skill_lab\nturns:\n  - テスト\njudge: x\n"


@pytest.fixture
def lab_env(tmp_path, monkeypatch):
    """インスタンス2つ（org_a・org_b）と研究室1つ（lab1）を tmp に作る。"""
    root = tmp_path / "instances"
    monkeypatch.setenv("INSTANCES_DIR", str(root))
    for key in ("LOCOHANE_INSTANCE", "CONFIG_OVERRIDES_PATH", "LOCOHANE_INSTANCE_ENV", "COMMON_DATA_DIR", "PROJECT_LOCOHANE_DIR"):
        monkeypatch.delenv(key, raising=False)
    for name, kind in (("org_a", inst.KIND_APP), ("org_b", inst.KIND_APP), ("lab1", inst.KIND_SKILL_LAB)):
        (root / name).mkdir(parents=True)
        port = {"org_a": 18001, "org_b": 18002, "lab1": 0}[name]
        inst.write_instance(root, inst.InstanceMeta(name=name, display_name=name, app_port=port, kind=kind))
        overrides = {"paths": {"common_data_dir": str(tmp_path / "data" / name)}}
        if name == "lab1":
            overrides["llm"] = {"main_url": '[{"base_url": "http://lab-host:9999/v1", "api_key": "x", "model": "m", "provider": "llama_cpp"}]'}
        (root / name / "config_overrides.json").write_text(json.dumps(overrides), encoding="utf-8")
    return {"root": root, "tmp": tmp_path}


def _cfg(lab_env, name):
    return promotion.instance_config(lab_env["root"], name)


# --- インスタンス専用の拡張ディレクトリ -------------------------------------------


def test_instance_locohane_dir_is_scanned_last_and_only_by_its_instance(lab_env):
    from src.skills import scan_skills

    cfg_a = _cfg(lab_env, "org_a")
    assert cfg_a.instance_locohane_dir == lab_env["root"] / "org_a"
    assert cfg_a.locohane_skills_dirs[-1] == cfg_a.instance_locohane_dir / "skills"
    # 同梱スキルと同じ名前を置くと、そのインスタンスでだけ置き換わる
    (cfg_a.instance_locohane_dir / "skills" / "pdf-tools").mkdir(parents=True)
    (cfg_a.instance_locohane_dir / "skills" / "pdf-tools" / "SKILL.md").write_text(_skill_md("pdf-tools", "org_a 専用の版"), encoding="utf-8")
    skills_a = {s.name: s.description for s in scan_skills([cfg_a.skills_dir, *cfg_a.locohane_skills_dirs])}
    cfg_b = _cfg(lab_env, "org_b")
    skills_b = {s.name: s.description for s in scan_skills([cfg_b.skills_dir, *cfg_b.locohane_skills_dirs])}
    assert skills_a["pdf-tools"] == "org_a 専用の版"
    assert skills_b["pdf-tools"] != "org_a 専用の版"


def test_instance_locohane_dir_survives_project_locohane_dir_override(lab_env):
    """project_locohane_dir のリストを上書きしても、専用の置き場は外れない。"""
    path = lab_env["root"] / "org_a" / "config_overrides.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["paths"]["project_locohane_dir"] = '["./.locohane_other"]'
    path.write_text(json.dumps(data), encoding="utf-8")
    cfg = _cfg(lab_env, "org_a")
    assert cfg.locohane_skills_dirs[-1] == cfg.instance_locohane_dir / "skills"
    assert cfg.project_instructions_paths[-1] == cfg.instance_locohane_dir / "LOCOHANE.md"


def test_config_candidates_lists_skills_with_scripts_and_agents_of_the_instance(lab_env):
    """設定画面の候補は、project_locohane_dir とインスタンス専用の置き場のスキル・スクリプト・agent_type を含む。"""
    from admin.skill_lab import sources

    shared = lab_env["tmp"] / "shared_locohane"
    (shared / "skills" / "team-tool" / "scripts").mkdir(parents=True)
    (shared / "skills" / "team-tool" / "SKILL.md").write_text(_skill_md("team-tool"), encoding="utf-8")
    for script in ("b_write.py", "a_read.py", "_common.py", "notes.txt"):
        (shared / "skills" / "team-tool" / "scripts" / script).write_text("", encoding="utf-8")
    path = lab_env["root"] / "org_a" / "config_overrides.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["paths"]["project_locohane_dir"] = json.dumps([str(shared)])
    path.write_text(json.dumps(data), encoding="utf-8")
    (lab_env["root"] / "org_a" / "agents").mkdir(parents=True)
    (lab_env["root"] / "org_a" / "agents" / "org-reviewer.md").write_text(_agent_md("org-reviewer"), encoding="utf-8")

    result = sources.config_candidates(_cfg(lab_env, "org_a"))
    skills = {s["name"]: s for s in result["skills"]}
    assert skills["team-tool"]["scripts"] == ["a_read.py", "b_write.py"]
    assert skills["team-tool"]["origin"] == "shared"
    assert "read_pdf.py" in skills["pdf-tools"]["scripts"]
    agents = {a["name"]: a for a in result["agent_types"]}
    assert agents["org-reviewer"]["origin"] == "instance"
    assert "explore" in agents
    # 他のインスタンスには出ない
    other = sources.config_candidates(_cfg(lab_env, "org_b"))
    assert "org-reviewer" not in {a["name"] for a in other["agent_types"]}


# --- 設定パッチ・研究室の LLM 接続先 -----------------------------------------------


def test_config_patch_merges_without_duplicates_and_rejects_unknown_keys():
    value, added = config_patch.merged_value(
        "main_agent_tool_guard", "allow_entries", '[["Glob", 1], [["s", "a.py"], 0]]', [[["s", "a.py"], -1], [["s", "b.py"], -1]]
    )
    import ast

    assert ast.literal_eval(value) == [["Glob", 1], [["s", "a.py"], 0], [["s", "b.py"], -1]]
    assert added == [[["s", "b.py"], -1]]
    with pytest.raises(config_patch.ConfigPatchError):
        config_patch.validate_patch({"llm": {"main_url": ["x"]}})


def test_llm_env_from_instance_reads_lab_endpoint(lab_env):
    from evals.instance import llm_env_from_instance

    env = llm_env_from_instance("lab1")
    assert "http://lab-host:9999/v1" in env["LLM_MAIN_URL"]
    # 研究室の .env があればそちらが優先される
    (lab_env["root"] / "lab1" / ".env").write_text('LLM_MAIN_URL="http://env-host/v1"\n', encoding="utf-8")
    assert llm_env_from_instance("lab1")["LLM_MAIN_URL"] == "http://env-host/v1"


def test_run_case_applies_config_patch_on_top_of_effective_value(lab_env, monkeypatch, tmp_path):
    from evals import run_case

    # _apply_config_patch は os.environ へ直接書くため、テストの間だけ写しに差し替えて後のテストへ漏らさない。
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("LOCOHANE_INSTANCE", "org_a")
    patch = tmp_path / "config_patch.json"
    patch.write_text(json.dumps({"subagent": {"agent_type_run_script_allowlist": [["my-agent", "pdf-tools"]]}}), encoding="utf-8")
    monkeypatch.delenv("SUBAGENT_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST", raising=False)
    run_case._apply_config_patch(patch)
    from src.config import load_config

    cfg = load_config(overrides_path=lab_env["root"] / "org_a" / "config_overrides.json", instance_name="org_a")
    assert ("my-agent", "pdf-tools") in cfg.subagent_agent_type_run_script_allowlist
    # 既定の登録も残る（足すだけ）
    assert len(cfg.subagent_agent_type_run_script_allowlist) > 1


def test_case_work_dir_resolves_beside_case_file(tmp_path):
    from evals import run_case
    from evals.case_schema import load_case

    cases = tmp_path / "cases"
    (cases / "fixtures" / "sample").mkdir(parents=True)
    path = cases / "001.yaml"
    path.write_text("id: '001'\ntarget: x\nturns: [a]\njudge: x\nwork_dir: fixtures/sample\n", encoding="utf-8")
    assert run_case._resolve_work_dir(load_case(path)) == (cases / "fixtures" / "sample").resolve()


def test_cases_sha256_includes_fixtures(tmp_path):
    (tmp_path / "001.yaml").write_text("x", encoding="utf-8")
    before = cases_sha256(tmp_path)
    (tmp_path / "fixtures" / "a").mkdir(parents=True)
    (tmp_path / "fixtures" / "a" / "in.txt").write_text("1", encoding="utf-8")
    after = cases_sha256(tmp_path)
    (tmp_path / "fixtures" / "a" / "in.txt").write_text("2", encoding="utf-8")
    assert len({before, after, cases_sha256(tmp_path)}) == 3


# --- テーマのファイル操作 -------------------------------------------------------------


def _theme(lab_env, instance="org_a"):
    root = themes.themes_root(_cfg(lab_env, instance).common_data_dir)
    return themes.create_theme(root, instance=instance, title="テスト", lab="lab1", actor="admin")


def _add_skill(directory: Path, name="my-skill", script=True):
    themes.add_asset_new(directory, "skills", name, _skill_md(name), actor="admin")
    if script:
        (directory / "assets" / "skills" / name / "scripts" / "run.py").write_text("print('{}')\n", encoding="utf-8")


@pytest.mark.parametrize(
    "rel",
    ["theme.json", "baseline/x.md", "runs/001/x", "../x", "assets/skills/My-Skill/SKILL.md", "assets/agents/a.txt", "cases/a/b.yaml", "C:/x"],
)
def test_resolve_path_rejects_outside_allowed_places(lab_env, rel):
    directory = _theme(lab_env)
    with pytest.raises(themes.ThemeError):
        themes.resolve_path(directory, rel, for_write=True)


def test_write_validates_content_and_invalidates_tryout(lab_env):
    directory = _theme(lab_env)
    _add_skill(directory)
    with pytest.raises(themes.ThemeError, match="SKILL.md"):
        themes.write_text(directory, "assets/skills/my-skill/SKILL.md", "frontmatter なし", actor="admin")
    with pytest.raises(themes.ThemeError, match="syntax error"):
        themes.write_text(directory, "assets/skills/my-skill/scripts/run.py", "def (:", actor="admin")
    themes.update(directory, lambda d: d.update({"tryout": {"repeat": 10}, "status": themes.STATUS_REVIEW}))
    themes.write_text(directory, "cases/001.yaml", _CASE.format(id="001"), actor="admin")
    data = themes.read(directory)
    assert data["tryout"] is None and data["status"] == themes.STATUS_DRAFT


def test_tryout_matches_detects_any_change(lab_env):
    directory = _theme(lab_env)
    _add_skill(directory)
    themes.write_text(directory, "cases/001.yaml", _CASE.format(id="001"), actor="admin")
    tryout = _tryout_for(directory)
    assert themes.tryout_matches(directory, tryout) == []
    (directory / "assets" / "skills" / "my-skill" / "scripts" / "run.py").write_text("print(1)\n", encoding="utf-8")
    assert themes.tryout_matches(directory, tryout)


def _tryout_for(directory: Path, repeat: int = 10, instance: str = "org_a") -> dict:
    h = themes.content_hashes(directory)
    return {
        "repeat": repeat,
        "instance": instance,
        "case_files": sorted(p.stem for p in (directory / "cases").glob("*.yaml")),
        "cases_sha256": h["cases"],
        "skill_overlays": [{"name": n, "sha256": sha} for n, sha in h["skills"].items()],
        "agent_overlays": [{"name": n, "sha256": sha} for n, sha in h["agents"].items()],
        "config_patch": {"sha256": h["config_patch"]},
        "ai_verdict": "pass",
    }


# --- AI 修正の適用 -------------------------------------------------------------------


def test_fixer_applies_all_or_nothing(lab_env):
    directory = _theme(lab_env)
    _add_skill(directory)
    skill_md = directory / "assets" / "skills" / "my-skill" / "SKILL.md"
    original = skill_md.read_text(encoding="utf-8")
    bad = [
        {"path": "assets/skills/my-skill/SKILL.md", "old": "# my-skill", "new": "# 直した"},
        {"path": "spec.md", "content": "仕様を変える"},
    ]
    applied, errors = fixer.apply_edits(directory, bad, known_tools=["Read"])
    assert applied == [] and any("spec" in e for e in errors)
    assert skill_md.read_text(encoding="utf-8") == original
    themes.add_asset_new(directory, "agents", "helper", _agent_md("helper", "Read"), actor="admin")
    invalid_agent = [{"path": "assets/agents/helper.md", "content": _agent_md("helper", "Read, NoSuchTool")}]
    assert fixer.apply_edits(directory, invalid_agent, known_tools=["Read"])[1]
    good = [
        {"path": "assets/skills/my-skill/SKILL.md", "old": "# my-skill", "new": "# 直した"},
        {"path": "assets/agents/helper.md", "content": _agent_md("helper", "Read")},
    ]
    applied, errors = fixer.apply_edits(directory, good, known_tools=["Read"])
    assert errors == [] and len(applied) == 2
    assert "# 直した" in skill_md.read_text(encoding="utf-8")
    assert themes.read(directory)["history"][-1]["actor"] == "AI"


# --- ワーカーのループ -----------------------------------------------------------------


class _StubWorker:
    """Worker の評価・修正だけを差し替えて、ループの状態遷移を確かめる。"""


def _set_target_skill_lab(lab_env, instance="org_a", **values):
    """対象インスタンスの [skill_lab] を config_overrides.json に書く（ループの上限は対象インスタンスの設定）。"""
    path = lab_env["root"] / instance / "config_overrides.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data.setdefault("skill_lab", {}).update(values)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_worker(monkeypatch, lab_env, outcomes, max_iterations=8):
    from admin.skill_lab import worker as worker_mod

    _set_target_skill_lab(lab_env, max_iterations=max_iterations)
    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.lab = "lab1"
    w.base_env = dict(os.environ)
    w.instances_root = lab_env["root"]
    w.shutdown = False
    w.config = type(
        "C",
        (),
        {
            "skill_lab_max_hours": 6,  # run_theme の外で _evaluate を直接呼ぶテスト用（ループでは対象インスタンスの値）
            "skill_lab_transcript_excerpt_chars": 1000,
            "skill_lab_fixer_max_file_chars": 1000,
            "skill_lab_poll_interval_seconds": 1,
        },
    )()
    calls = {"eval": [], "fix": 0}
    queue = list(outcomes)

    def fake_evaluate(instance, directory, repeat, phase):
        passed = queue.pop(0)
        calls["eval"].append((phase, repeat, passed))
        it = {"n": len(calls["eval"]), "phase": phase, "repeat": repeat, "verdict": "pass" if passed else "fail", "cases": {}}

        def _apply(d):
            d.setdefault("iterations", []).append(it)
            d["loop"]["iterations_used"] = d["loop"].get("iterations_used", 0) + 1

        themes.update(directory, _apply)
        return {"iteration": it, "run": {"results": []}, "verdicts": [], "passed": passed}

    def fake_fix(instance, directory, outcome):
        calls["fix"] += 1

    monkeypatch.setattr(w, "_evaluate", fake_evaluate)
    monkeypatch.setattr(w, "_fix", fake_fix)
    monkeypatch.setattr(w, "process_tasks", lambda: False)
    return w, calls


def test_worker_loop_fixes_until_tryout_passes(lab_env, monkeypatch):
    directory = _theme(lab_env)
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop"}}))
    w, calls = _make_worker(monkeypatch, lab_env, [False, True, False, True, True])
    w.run_theme("org_a", directory)
    data = themes.read(directory)
    assert data["status"] == themes.STATUS_REVIEW
    assert [c[0] for c in calls["eval"]] == ["eval", "eval", "tryout", "eval", "tryout"]
    assert calls["eval"][2][1] == _cfg(lab_env, "org_a").skill_lab_tryout_repeats
    assert calls["fix"] == 2


def test_worker_loop_stops_at_iteration_limit(lab_env, monkeypatch):
    directory = _theme(lab_env)
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop"}}))
    w, calls = _make_worker(monkeypatch, lab_env, [False] * 10, max_iterations=3)
    w.run_theme("org_a", directory)
    assert themes.read(directory)["status"] == themes.STATUS_EXHAUSTED
    assert len(calls["eval"]) == 3


def test_worker_picks_only_its_assigned_themes(lab_env, monkeypatch):
    from admin.skill_lab import worker as worker_mod

    other_lab = lab_env["root"] / "lab2"
    other_lab.mkdir()
    inst.write_instance(lab_env["root"], inst.InstanceMeta(name="lab2", display_name="lab2", app_port=0, kind=inst.KIND_SKILL_LAB))
    (other_lab / "config_overrides.json").write_text(json.dumps({"paths": {"common_data_dir": str(lab_env["tmp"] / "data" / "lab2")}}), encoding="utf-8")
    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.lab, w.instances_root = "lab1", lab_env["root"]
    assert w._assigned({"lab": "lab1"}) and not w._assigned({"lab": "lab2"})
    assert not w._assigned({"lab": None})  # 研究室が2つあるときは担当を決めないと拾わない


# --- 昇格 -----------------------------------------------------------------------------


def _draft(lab_env, instance="org_a", owner="tanaka", name="my-skill") -> Path:
    cfg = _cfg(lab_env, instance)
    draft = cfg.skill_draft_dir / owner / name
    (draft / "scripts").mkdir(parents=True)
    (draft / "SKILL.md").write_text(_skill_md(name), encoding="utf-8")
    (draft / "scripts" / "run.py").write_text("print('{}')\n", encoding="utf-8")
    (draft / "_draft_meta.json").write_text(json.dumps({"kind": "new", "status": "draft"}), encoding="utf-8")
    (cfg.skill_draft_dir / owner / "_workspace" / name).mkdir(parents=True)
    return draft


def _ready_theme(lab_env, with_draft=True):
    directory = _theme(lab_env)
    draft = None
    if with_draft:
        draft = _draft(lab_env)
        themes.add_asset_copy(directory, "skills", "my-skill", draft, source={"kind": "draft", "origin": str(draft), "owner": "tanaka"}, actor="admin")
    themes.add_asset_new(directory, "agents", "helper", _agent_md("helper"), actor="admin")
    themes.write_text(directory, "cases/001.yaml", _CASE.format(id="001"), actor="admin")
    themes.write_text(
        directory,
        "config_patch.json",
        json.dumps({"subagent": {"agent_type_run_script_allowlist": [["helper", "my-skill"]]}}),
        actor="admin",
    )
    themes.update(directory, lambda d: d.update({"tryout": _tryout_for(directory), "status": themes.STATUS_REVIEW}))
    return directory, draft


def _promote(lab_env, directory, **kw):
    return promotion.promote(
        lab_env["root"],
        directory,
        distribute_to=kw.get("distribute_to", []),
        discard_source_changes=kw.get("discard", False),
        note="",
        actor="admin",
        remote_addr="local",
        backup_keep=5,
    )


def test_promote_places_into_instance_dir_registers_settings_and_archives_draft(lab_env, monkeypatch):
    monkeypatch.setattr(promotion, "PROMOTION_LOG", lab_env["tmp"] / "promotion_log.md")
    monkeypatch.setattr(promotion, "PROMOTE_HISTORY_DIR", lab_env["tmp"] / "history")
    directory, draft = _ready_theme(lab_env)
    check = promotion.check(lab_env["root"], directory)
    assert check.problems == [] and check.confirmations == []
    record = _promote(lab_env, directory)

    cfg_a = _cfg(lab_env, "org_a")
    dest = cfg_a.instance_locohane_dir
    assert (dest / "skills" / "my-skill" / "scripts" / "run.py").is_file()
    assert (dest / "agents" / "helper.md").is_file()
    assert (dest / "evals" / directory.name / "001.yaml").is_file()
    assert ("my-skill", "run.py") in cfg_a.plan_approval_exempt_scripts
    assert (("my-skill", "run.py"), -1) in cfg_a.main_agent_tool_guard_allow_entries
    assert ("helper", "my-skill") in cfg_a.subagent_agent_type_run_script_allowlist
    # 他のインスタンスには何も入らず、設定も変わらない
    cfg_b = _cfg(lab_env, "org_b")
    assert not (cfg_b.instance_locohane_dir / "skills" / "my-skill").exists()
    assert ("my-skill", "run.py") not in cfg_b.plan_approval_exempt_scripts
    # 取り込み元のドラフトは _workspace ごとアーカイブへ
    assert not draft.exists()
    archived = Path(record["archived_drafts"][0])
    assert archived.is_relative_to(cfg_a.skill_archive_dir) and (archived / "_workspace").is_dir()
    assert json.loads((archived / "_draft_meta.json").read_text(encoding="utf-8"))["status"] == "promoted"
    assert themes.read(directory)["status"] == themes.STATUS_PROMOTED
    assert "テスト" in (lab_env["tmp"] / "promotion_log.md").read_text(encoding="utf-8")


def test_promote_requires_confirmation_when_creator_changed_draft(lab_env, monkeypatch):
    monkeypatch.setattr(promotion, "PROMOTION_LOG", lab_env["tmp"] / "promotion_log.md")
    directory, draft = _ready_theme(lab_env)
    (draft / "SKILL.md").write_text(_skill_md("my-skill", "作成者が直した"), encoding="utf-8")
    assert promotion.check(lab_env["root"], directory).confirmations
    with pytest.raises(promotion.PromotionError, match="確認"):
        _promote(lab_env, directory)
    _promote(lab_env, directory, discard=True)


def test_promote_refuses_stale_tryout_and_name_collision(lab_env):
    directory, _ = _ready_theme(lab_env)
    (directory / "cases" / "002.yaml").write_text(_CASE.format(id="002"), encoding="utf-8")
    assert any("cases" in p for p in promotion.check(lab_env["root"], directory).problems)
    (directory / "cases" / "002.yaml").unlink()
    taken = _cfg(lab_env, "org_a").instance_locohane_dir / "agents"
    taken.mkdir(parents=True)
    (taken / "helper.md").write_text(_agent_md("helper"), encoding="utf-8")
    assert any("same name" in p for p in promotion.check(lab_env["root"], directory).problems)


def test_promote_can_distribute_to_another_instance(lab_env, monkeypatch):
    monkeypatch.setattr(promotion, "PROMOTION_LOG", lab_env["tmp"] / "promotion_log.md")
    directory, _ = _ready_theme(lab_env, with_draft=False)
    _promote(lab_env, directory, distribute_to=["org_b"])
    cfg_b = _cfg(lab_env, "org_b")
    assert (cfg_b.instance_locohane_dir / "agents" / "helper.md").is_file()
    assert ("helper", "my-skill") in cfg_b.subagent_agent_type_run_script_allowlist


def test_promote_rolls_back_files_when_settings_fail(lab_env, monkeypatch):
    monkeypatch.setattr(promotion, "PROMOTION_LOG", lab_env["tmp"] / "promotion_log.md")
    directory, draft = _ready_theme(lab_env)

    def boom(**_kw):
        raise RuntimeError("保存に失敗")

    monkeypatch.setattr(promotion.overrides, "save", boom)
    with pytest.raises(promotion.PromotionError, match="元に戻しました"):
        _promote(lab_env, directory)
    dest = _cfg(lab_env, "org_a").instance_locohane_dir
    assert not (dest / "skills" / "my-skill").exists() and not (dest / "agents" / "helper.md").exists()
    assert draft.is_dir() and themes.read(directory)["status"] == themes.STATUS_REVIEW


def test_return_applies_fixes_to_draft(lab_env):
    directory, draft = _ready_theme(lab_env)
    (directory / "assets" / "skills" / "my-skill" / "SKILL.md").write_text(_skill_md("my-skill", "研究室で直した"), encoding="utf-8")
    promotion.return_to_creators(directory, reason="ここを直して", apply_fixes=True, reject=False, actor="admin")
    meta = json.loads((draft / "_draft_meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "returned" and meta["returned_reason"] == "ここを直して"
    assert "研究室で直した" in (draft / "SKILL.md").read_text(encoding="utf-8")
    assert tree_sha256(draft) == tree_sha256(directory / "assets" / "skills" / "my-skill")


# --- インスタンスの種類 ---------------------------------------------------------------


def test_skill_lab_instance_has_no_port_and_runs_worker(lab_env):
    meta, _ = inst.create_instance(lab_env["root"], Path("config.ini"), name="lab3", kind=inst.KIND_SKILL_LAB)
    assert meta.is_skill_lab and meta.app_port == 0
    assert inst.read_instance(lab_env["root"], "lab3").kind == inst.KIND_SKILL_LAB
    from admin.supervisor import Supervisor

    cmd = Supervisor._command_for(meta)
    assert cmd[1:] == ["-m", "admin.skill_lab.worker", "--lab", "lab3"]
    # 研究室同士・本体とでポートの重複は確かめない（研究室は待ち受けない）
    inst.check_conflicts(lab_env["root"], Path("config.ini"), name="lab3", app_host="127.0.0.1", app_port=0, kind=inst.KIND_SKILL_LAB)


def test_extensions_summary_lists_promoted_assets(lab_env):
    dest = _cfg(lab_env, "org_a").instance_locohane_dir
    (dest / "skills" / "a").mkdir(parents=True)
    (dest / "skills" / "a" / "SKILL.md").write_text(_skill_md("a"), encoding="utf-8")
    (dest / "agents").mkdir()
    (dest / "agents" / "b.md").write_text(_agent_md("b"), encoding="utf-8")
    summary = inst.instance_extensions_summary(lab_env["root"], Path("config.ini"), "org_a")
    assert summary["skills"] == ["a"] and summary["agents"] == ["b"] and summary["deleted_with_instance"]


def test_copy_extensions_copies_only_scanned_entries(lab_env):
    """既定の拡張ディレクトリはインスタンスの設定フォルダそのものなので、複製では skills/ 等だけを写す。"""
    src = lab_env["root"] / "org_a"
    (src / "skills" / "a").mkdir(parents=True)
    (src / "skills" / "a" / "SKILL.md").write_text(_skill_md("a"), encoding="utf-8")
    (src / "evals" / "t1").mkdir(parents=True)
    (src / "LOCOHANE.md").write_text("org_a 専用", encoding="utf-8")
    (src / "app_stdout.log").write_text("log", encoding="utf-8")
    (src / "backups").mkdir()
    inst.create_instance(
        lab_env["root"], Path("config.ini"), name="org_c", app_port=18003, copy_from="org_a", copy_extensions=True
    )
    dst = lab_env["root"] / "org_c"
    assert (dst / "skills" / "a" / "SKILL.md").is_file() and (dst / "evals" / "t1").is_dir()
    assert (dst / "LOCOHANE.md").read_text(encoding="utf-8") == "org_a 専用"
    assert not (dst / "app_stdout.log").exists() and not (dst / "backups").exists()
    assert (dst / "instance.json").is_file()


# --- スキル調整ワーカーの設定の範囲 -----------------------------------------------------


def test_config_key_applies_by_kind():
    """ワーカーは接続先・[skill_lab_worker]・[log] だけ、本体には [skill_lab_worker] を出さない。"""
    lab, app = inst.KIND_SKILL_LAB, inst.KIND_APP
    assert inst.config_key_applies(lab, "llm", "main_url") and inst.config_key_applies(lab, "llm", "sub_url")
    assert inst.config_key_applies(lab, "skill_lab_worker", "poll_interval_seconds") and inst.config_key_applies(lab, "log", "level")
    assert not inst.config_key_applies(lab, "llm", "reasoning_effort")
    assert not inst.config_key_applies(lab, "graph", "token_guard_soft_threshold")
    assert not inst.config_key_applies(lab, "skill_lab", "instance_locohane_dir")
    assert not inst.config_key_applies(app, "skill_lab_worker", "poll_interval_seconds")
    assert inst.config_key_applies(app, "skill_lab", "instance_locohane_dir") and inst.config_key_applies(app, "llm", "reasoning_effort")
    # インスタンス別の上書きが無視されるキー（[admin]・[data_backup]・管理者設定のキー）はどちらにも出さない。
    for k in (app, lab):
        assert not inst.config_key_applies(k, "admin", "port")
        assert not inst.config_key_applies(k, "data_backup", "keep")
        assert not inst.config_key_applies(k, "vector_db", "ingest_workers")
    assert inst.config_key_applies(app, "vector_db", "chunk_size")


def test_worker_created_from_app_does_not_inherit_unused_settings(lab_env):
    """本体を複製してワーカーを作っても、ワーカーで効かない設定（推論パラメータ等）は持ち込まない。"""
    src = lab_env["root"] / "org_a" / "config_overrides.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    url = '[{"base_url": "http://org-a:8080/v1", "api_key": "x", "model": "m", "provider": "llama_cpp"}]'
    data.update({"llm": {"main_url": url, "reasoning_effort": "low"}, "graph": {"max_parallel": "2"}, "log": {"level": "debug"}})
    src.write_text(json.dumps(data), encoding="utf-8")
    inst.create_instance(lab_env["root"], Path("config.ini"), name="lab2", copy_from="org_a", kind=inst.KIND_SKILL_LAB)
    copied = json.loads((lab_env["root"] / "lab2" / "config_overrides.json").read_text(encoding="utf-8"))
    # 接続先・並列数（推論サーバーの能力に合わせる設定）・ログはワーカーの設定として持ち込む。推論パラメータは持ち込まない。
    assert copied == {"llm": {"main_url": url}, "graph": {"max_parallel": "2"}, "log": {"level": "debug"}}


def test_worker_ai_uses_target_settings_with_lab_endpoints(lab_env):
    """AI 修正・judge・下書きは、対象インスタンスの設定に研究室の接続先だけを重ねて呼ぶ。"""
    path = lab_env["root"] / "org_a" / "config_overrides.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["skill_lab"] = {"tryout_repeats": "3"}
    path.write_text(json.dumps(data), encoding="utf-8")
    w = _lab_worker(lab_env)
    w.config = _cfg(lab_env, "lab1")
    llm_cfg = w._llm_config("org_a")
    assert [e.base_url for e in llm_cfg.main_endpoints] == ["http://lab-host:9999/v1"]
    assert llm_cfg.sub_endpoints == w.config.sub_endpoints
    assert llm_cfg.skill_lab_tryout_repeats == 3 and llm_cfg.common_data_dir == _cfg(lab_env, "org_a").common_data_dir


# --- API（インスタンスごとの分離） ----------------------------------------------------------


@pytest.fixture
def client(lab_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from admin.skill_lab import api

    class _Sup:
        def status(self, _name):
            return type("S", (), {"state": type("E", (), {"value": "stopped"})()})()

    app = FastAPI()
    app.include_router(
        api.build_router(
            require_login=lambda: "admin",
            require_csrf=lambda: None,
            instances_root=lab_env["root"],
            supervisor=_Sup(),
            audit_log_path=lab_env["tmp"] / "audit.log",
            client_ip=lambda _r: "127.0.0.1",
            backup_keep=5,
        )
    )
    return TestClient(app)


def test_api_keeps_themes_per_instance(client, lab_env):
    created = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A のテーマ", "lab": "lab1"}).json()
    assert client.get(f"/api/instances/org_a/skill-lab/themes/{created['id']}").status_code == 200
    # 他のインスタンスの下からは同じ ID でも触れない
    assert client.get(f"/api/instances/org_b/skill-lab/themes/{created['id']}").status_code == 404
    assert client.get("/api/instances/org_b/skill-lab/themes").json()["themes"] == []
    # 研究室インスタンスにはテーマを置けない
    assert client.post("/api/instances/lab1/skill-lab/themes", json={"title": "x"}).status_code == 400


def test_api_imports_only_drafts_of_the_instance(client, lab_env):
    draft_b = _draft(lab_env, instance="org_b")
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A", "lab": "lab1"}).json()
    res = client.post(
        f"/api/instances/org_a/skill-lab/themes/{theme['id']}/assets",
        json={"mode": "draft", "asset_type": "skills", "name": "my-skill", "origin": str(draft_b)},
    )
    assert res.status_code == 400
    draft_a = _draft(lab_env, instance="org_a")
    res = client.post(
        f"/api/instances/org_a/skill-lab/themes/{theme['id']}/assets",
        json={"mode": "draft", "asset_type": "skills", "name": "my-skill", "origin": str(draft_a)},
    )
    assert res.status_code == 200
    sources = client.get("/api/instances/org_a/skill-lab/sources").json()
    assert [d["imported_theme"] for d in sources["drafts"]] == [theme["id"]]


def test_api_case_form_roundtrip(client, lab_env):
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A", "lab": "lab1"}).json()
    base = f"/api/instances/org_a/skill-lab/themes/{theme['id']}"
    body = {"data": {"turns": ["見積書を確認して"], "expect": {"tool_call_args_contains": {"dispatch_agent": {"agent_type": "helper"}}}, "auto_approve": False}}
    assert client.put(f"{base}/cases/001_basic", json=body).status_code == 200
    data = client.get(f"{base}/cases/001_basic").json()["data"]
    assert data["id"] == "001_basic" and data["auto_approve"] is False
    assert client.put(f"{base}/cases/002", json={"data": {"turns": ["x"]}}).status_code == 400  # expect も judge も無い


def test_api_overview_shows_worker_workload(client, lab_env):
    """スキル調整ワーカーのカードには、チャットの統計ではなく処理中・開始待ちのテーマと接続先を出す。"""
    created = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A のテーマ", "lab": "lab1"}).json()
    directory = themes.theme_dir(themes.themes_root(_cfg(lab_env, "org_a").common_data_dir), created["id"])
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED}))
    labs = client.get("/api/skill-lab/overview").json()["labs"]
    assert labs[0]["name"] == "lab1" and labs[0]["queued"] == 1 and labs[0]["running"] == []
    assert labs[0]["endpoint"] == "http://lab-host:9999/v1"


def test_spec_filled_requires_purpose(lab_env):
    """仕様の Purpose が見出しだけ・「TODO:」だけなら書かれていないとみなす（開始の条件）。"""
    directory = _theme(lab_env)
    spec = directory / themes.SPEC_FILENAME
    assert [line for line in spec.read_text(encoding="utf-8").splitlines() if line.startswith("## ")] == [
        f"## {heading}" for heading in themes.SPEC_HEADINGS
    ]
    assert not themes.spec_filled(directory)  # 作成直後のひな形
    spec.write_text("# t\n## Purpose\nTODO: 何を確かめるか\n## When to use\n営業の確認\n", encoding="utf-8")
    assert not themes.spec_filled(directory)  # Purpose 以外が書かれていても足りない
    spec.write_text("# t\n## Purpose\nFind calculation errors in quotes\n## When to use\n", encoding="utf-8")
    assert themes.spec_filled(directory)
    # 日本語の旧見出し・「要確認:」で書かれた仕様も同じに扱う
    spec.write_text("# t\n## 目的\n要確認:\n## 使う場面\n営業の確認\n", encoding="utf-8")
    assert not themes.spec_filled(directory)  # 目的以外が書かれていても足りない
    spec.write_text("# t\n## 目的\n見積書の計算ミスを見つける\n## 使う場面\n", encoding="utf-8")
    assert themes.spec_filled(directory)
    spec.write_text("# t\n見出しを使わずに書いた仕様\n", encoding="utf-8")
    assert themes.spec_filled(directory)


def test_api_start_requires_spec_targets_and_cases(client, lab_env):
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A", "lab": "lab1"}).json()
    base = f"/api/instances/org_a/skill-lab/themes/{theme['id']}"
    assert len(client.get(base).json()["start_missing"]) == 3
    res = client.post(f"{base}/start", json={"mode": "loop"})
    assert res.status_code == 400 and "① 研究対象" in res.json()["detail"] and "② やってほしいこと" in res.json()["detail"]

    directory = themes.theme_dir(themes.themes_root(_cfg(lab_env, "org_a").common_data_dir), theme["id"])
    _add_skill(directory)
    case = {"data": {"turns": ["見積書を確認して"], "judge": "誤りを指摘している"}}
    assert client.put(f"{base}/cases/case_001", json=case).status_code == 200
    assert client.get(base).json()["start_missing"] == ["② やってほしいことの「目的」"]
    spec = "# A\n\n## 目的\n見積書の計算ミスを見つける\n"
    assert client.put(f"{base}/files", json={"path": "spec.md", "content": spec}).status_code == 200
    assert client.get(base).json()["start_missing"] == []
    assert client.post(f"{base}/start", json={"mode": "loop"}).status_code == 200


def test_drafting_uses_targets_and_spec(lab_env, monkeypatch):
    """② の下書きは ① の研究対象（入手元・注文・作成中のものも）を、③ の下書きは ① と ② を AI に渡す。"""
    from admin.skill_lab import drafting

    directory = _theme(lab_env)
    themes.add_asset_new(directory, "skills", "estimate-check", _skill_md("estimate-check", "見積書の計算ミスを探す"), actor="admin", description="見積書の計算ミスを一覧にする")
    themes.add_task(directory, "draft_asset", {"asset_type": "agents", "name": "reviewer", "request": "見積書を総合的に見直す"}, actor="admin")
    brief = drafting.targets_brief(directory)
    assert "skill estimate-check: newly created in this theme (order: 見積書の計算ミスを一覧にする)" in brief
    assert "sub-agent reviewer: being created by the AI (order: 見積書を総合的に見直す)" in brief

    prompts = []
    monkeypatch.setattr(drafting, "ask_text", lambda _c, _s, user: prompts.append(user) or "# テスト\n## 目的\n計算ミスを見つける\n")
    drafting.draft_spec(None, directory, max_file_chars=10000)
    assert "## Research targets" in prompts[0] and "assets/skills/estimate-check/SKILL.md" in prompts[0]

    monkeypatch.setattr(drafting, "ask_json", lambda _c, _s, user: prompts.append(user) or [{"id": "c1", "turns": ["見積書を確認して"], "judge": "誤りを指摘している"}])
    assert drafting.draft_cases(None, directory, max_file_chars=10000, count=1) == ["c1"]
    assert "## Research targets" in prompts[1] and "計算ミスを見つける" in prompts[1]


# --- あらゆる状況への備え（抜け道・競合・異常終了） -------------------------------------------


def test_fixer_cannot_game_the_cases_or_targets(lab_env):
    """AI 修正は、ケースの削除・追加、入力ファイルの書き換え、研究対象そのものの削除・新規作成をできない。"""
    directory = _theme(lab_env)
    _add_skill(directory)
    themes.write_text(directory, "cases/001.yaml", _CASE.format(id="001"), actor="admin")
    (directory / "cases" / "fixtures" / "s1").mkdir(parents=True)
    (directory / "cases" / "fixtures" / "s1" / "in.txt").write_text("入力", encoding="utf-8")
    forbidden = [
        [{"path": "cases/001.yaml", "delete": True}],
        [{"path": "cases/002.yaml", "content": _CASE.format(id="002")}],
        [{"path": "cases/fixtures/s1/in.txt", "content": "都合のよい入力"}],
        [{"path": "assets/skills/my-skill/SKILL.md", "delete": True}],
        [{"path": "assets/skills/other/SKILL.md", "content": _skill_md("other")}],
        [{"path": "assets/agents/new-agent.md", "content": _agent_md("new-agent", "Read")}],
    ]
    for edits in forbidden:
        applied, errors = fixer.apply_edits(directory, edits, known_tools=["Read"])
        assert applied == [] and errors, edits
    assert (directory / "cases" / "001.yaml").is_file()
    assert (directory / "cases" / "fixtures" / "s1" / "in.txt").read_text(encoding="utf-8") == "入力"
    # 今あるケースの中身も書き換えさせない（問題は blockers で報告させ、ループを止めて人が直す）
    fixed = _CASE.format(id="001").replace("judge: x", "judge: y")
    assert fixer.apply_edits(directory, [{"path": "cases/001.yaml", "content": fixed}], known_tools=["Read"])[1]
    assert "judge: x" in (directory / "cases" / "001.yaml").read_text(encoding="utf-8")


def test_case_without_effective_condition_is_rejected(lab_env):
    """expect の項目がすべて空で judge も無いケースは、何をしても合格になるため保存させない。"""
    directory = _theme(lab_env)
    empty = "id: '001'\ntarget: skill_lab\nturns: [x]\nexpect:\n  response_contains: []\n"
    with pytest.raises(themes.ThemeError, match="way to decide passing"):
        themes.write_text(directory, "cases/001.yaml", empty, actor="admin")
    # 手で置かれた壊れたケース・無い入力フォルダは、開始の条件で知らせる
    (directory / "cases" / "001.yaml").write_text(empty, encoding="utf-8")
    (directory / "cases" / "002.yaml").write_text(_CASE.format(id="002") + "work_dir: fixtures/nothing\n", encoding="utf-8")
    cases = {c["id"]: c for c in themes.list_cases(directory)}
    assert "way to decide passing" in cases["001"]["error"] and "fixtures/nothing" in cases["002"]["error"]
    assert any("001" in m and "002" in m for m in themes.start_missing(directory))


def test_case_with_conditions_that_can_never_pass_blocks_start(lab_env):
    """どう答えても満たせない条件（含むべき言葉の中に禁止の言葉がある・呼ぶべきツールを禁止している等）は、
    反復を使い切る前に、開始の条件で知らせる（2026-11-13 ユーザー報告）。"""
    directory = _theme(lab_env)
    contradicting = (
        "id: '001'\ntarget: skill_lab\nturns: [合言葉を教えて]\n"
        "expect:\n  response_contains: [ハローワールド]\n  response_not_contains: [ハロー]\n"
        "  tool_called_any: [read_skill]\n  tool_not_called: [read_skill]\n"
    )
    themes.write_text(directory, "cases/001.yaml", contradicting, actor="admin")
    themes.write_text(directory, "cases/002.yaml", _CASE.format(id="002"), actor="admin")
    cases = {c["id"]: c for c in themes.list_cases(directory)}
    assert "ハローワールド" in cases["001"]["error"] and "read_skill" in cases["001"]["error"]
    assert "error" not in cases["002"]
    assert any("001" in m and "002" not in m for m in themes.start_missing(directory))


def test_fixture_paths_and_reserved_names(lab_env):
    directory = _theme(lab_env)
    for rel in ("cases/fixtures/in.txt", "cases/fixtures/s1/CON", "cases/fixtures/s1/nul.txt"):
        with pytest.raises(themes.ThemeError):
            themes.resolve_path(directory, rel, for_write=True)
    # 入力ファイルの .py は「直させる題材」なので、構文が壊れていても置ける
    themes.write_text(directory, "cases/fixtures/s1/broken.py", "def (:\n", actor="admin")
    assert (directory / "cases" / "fixtures" / "s1" / "broken.py").is_file()


def test_spec_purpose_may_have_subheadings(lab_env):
    directory = _theme(lab_env)
    (directory / themes.SPEC_FILENAME).write_text("# t\n## 目的\n### 背景\n見積書の誤りを見つける\n## 使う場面\n", encoding="utf-8")
    assert themes.spec_filled(directory)


def test_config_patch_rejects_malformed_items():
    with pytest.raises(config_patch.ConfigPatchError):
        config_patch.validate_patch({"plan": {"plan_approval_exempt_scripts": [["only-one"]]}})
    with pytest.raises(config_patch.ConfigPatchError):
        config_patch.validate_patch({"subagent": {"agent_type_run_script_allowlist": [{"agent": "x"}]}})
    assert config_patch.validate_patch({"plan": {"plan_approval_exempt_scripts": [["my-skill", "run.py"]]}})


def _lab_worker(lab_env):
    from admin.skill_lab import worker as worker_mod

    _set_target_skill_lab(lab_env, max_iterations=3)
    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.lab, w.instances_root, w.shutdown, w.base_env = "lab1", lab_env["root"], False, dict(os.environ)
    w.config = type("C", (), {"skill_lab_transcript_excerpt_chars": 100, "skill_lab_fixer_max_file_chars": 100})()
    return w


def test_worker_honours_stop_pressed_while_it_was_down(lab_env, monkeypatch):
    """ワーカーが止まっている間に押された停止は、再起動したワーカーが打ち消さない。"""
    directory = _theme(lab_env)
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_RUNNING, "loop": {"mode": "loop", "stop_requested": True}}))
    w = _lab_worker(lab_env)
    monkeypatch.setattr(w, "_loop", lambda *a: pytest.fail("停止されたテーマを再開した"))
    w.run_theme("org_a", directory)
    assert themes.read(directory)["status"] == themes.STATUS_STOPPED
    # 一覧を読んだ後に停止されたテーマも始めない
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_STOPPED, "loop": {"mode": "loop"}}))
    w.run_theme("org_a", directory)
    assert themes.read(directory)["status"] == themes.STATUS_STOPPED


def test_worker_recovers_tasks_left_running(lab_env):
    directory = _theme(lab_env)
    task_id = themes.add_task(directory, "draft_spec", {}, actor="admin")
    w = _lab_worker(lab_env)
    w._set_task(directory, task_id, status="running")
    w.recover()
    assert themes.read(directory)["tasks"][0]["status"] == "queued"


def test_worker_does_not_run_drafts_after_loop_started(lab_env, monkeypatch):
    directory = _theme(lab_env)
    themes.add_task(directory, "draft_spec", {}, actor="admin")
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED}))
    w = _lab_worker(lab_env)
    monkeypatch.setattr(w, "_run_task", lambda *a: pytest.fail("ループ開始後に下書きを実行した"))
    w.process_tasks()
    task = themes.read(directory)["tasks"][0]
    assert task["status"] == "error" and "has started" in task["error"]


def test_worker_stops_without_fixing_when_llm_is_unreachable(lab_env, monkeypatch):
    """評価の LLM に接続できないときは、AI 修正で研究対象を書き換えずにエラーで止める。"""
    directory = _theme(lab_env)
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop"}}))
    w = _lab_worker(lab_env)
    run = {"results": [{"case_id": "001", "error": "llm_unreachable", "detail": "接続できません"}]}
    monkeypatch.setattr(w, "_evaluate", lambda *a: {"iteration": {"n": 1}, "run": run, "verdicts": [], "passed": False})
    monkeypatch.setattr(w, "_fix", lambda *a: pytest.fail("接続できないのに AI 修正した"))
    monkeypatch.setattr(w, "process_tasks", lambda: False)
    w.run_theme("org_a", directory)
    data = themes.read(directory)
    assert data["status"] == themes.STATUS_ERROR and "LLM" in data["loop"]["message"]


def test_worker_evaluates_theme_only_with_its_own_instance(lab_env):
    """データフォルダを共有するインスタンスがあっても、テーマは作ったインスタンスの構成でだけ処理する。"""
    shared = _cfg(lab_env, "org_a").common_data_dir
    path = lab_env["root"] / "org_b" / "config_overrides.json"
    path.write_text(json.dumps({"paths": {"common_data_dir": str(shared)}}), encoding="utf-8")
    directory = _theme(lab_env)
    assert _lab_worker(lab_env)._theme_dirs() == [("org_a", directory)]


def _ready_api_theme(client, lab_env):
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A"}).json()
    base = f"/api/instances/org_a/skill-lab/themes/{theme['id']}"
    directory = themes.theme_dir(themes.themes_root(_cfg(lab_env, "org_a").common_data_dir), theme["id"])
    _add_skill(directory)
    assert client.put(f"{base}/cases/case_001", json={"data": {"turns": ["x"], "judge": "y"}}).status_code == 200
    assert client.put(f"{base}/files", json={"path": "spec.md", "content": "# A\n## 目的\n確かめる\n"}).status_code == 200
    return base, directory


def test_api_start_pins_lab_and_waits_for_drafts(client, lab_env):
    base, directory = _ready_api_theme(client, lab_env)
    themes.add_task(directory, "draft_cases", {}, actor="admin")
    assert client.post(f"{base}/start", json={"mode": "loop"}).status_code == 409
    themes.update(directory, lambda d: d.update({"tasks": []}))
    assert client.post(f"{base}/start", json={"mode": "loop"}).status_code == 200
    data = themes.read(directory)
    # 研究室が1つのときの「自動」でも担当を決めておく（後から研究室が増えても拾われる）
    assert data["status"] == themes.STATUS_QUEUED and data["lab"] == "lab1"
    assert client.post(f"{base}/start", json={"mode": "loop"}).status_code == 409


def test_api_stop_when_worker_is_down_stops_immediately(client, lab_env):
    base, directory = _ready_api_theme(client, lab_env)
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_RUNNING, "loop": {"mode": "loop"}}))
    assert client.post(f"{base}/stop").status_code == 200  # テストの Supervisor は「停止中」
    assert themes.read(directory)["status"] == themes.STATUS_STOPPED


def test_api_new_case_does_not_overwrite_existing(client, lab_env):
    base, _ = _ready_api_theme(client, lab_env)
    res = client.put(f"{base}/cases/case_001?create=true", json={"data": {"turns": ["上書き"], "judge": "z"}})
    assert res.status_code == 409
    assert client.get(f"{base}/cases/case_001").json()["data"]["turns"] == ["x"]


def test_api_rejects_importing_a_draft_twice_and_bad_uploads(client, lab_env):
    draft = _draft(lab_env)
    ids = [client.post("/api/instances/org_a/skill-lab/themes", json={"title": t, "lab": "lab1"}).json()["id"] for t in ("A", "B")]
    body = {"mode": "draft", "asset_type": "skills", "name": "my-skill", "origin": str(draft)}
    assert client.post(f"/api/instances/org_a/skill-lab/themes/{ids[0]}/assets", json=body).status_code == 200
    res = client.post(f"/api/instances/org_a/skill-lab/themes/{ids[1]}/assets", json=body)
    assert res.status_code == 400 and "取り込み済み" in res.json()["detail"]
    upload = {"folder": "s1", "filename": "", "content_base64": "eA=="}
    assert client.post(f"/api/instances/org_a/skill-lab/themes/{ids[0]}/fixtures", json=upload).status_code == 400


def test_return_without_drafts_is_refused(lab_env):
    directory, _ = _ready_theme(lab_env, with_draft=False)
    with pytest.raises(promotion.PromotionError, match="取りやめ"):
        promotion.return_to_creators(directory, reason="x", apply_fixes=False, reject=False, actor="admin")
    assert themes.read(directory)["status"] == themes.STATUS_REVIEW


def test_return_keeps_draft_when_copy_fails(lab_env, monkeypatch):
    directory, draft = _ready_theme(lab_env)
    before = tree_sha256(draft)

    def boom(*_a, **_kw):
        raise OSError("コピーに失敗")

    monkeypatch.setattr(promotion.shutil, "copytree", boom)
    with pytest.raises(promotion.PromotionError, match="元のまま"):
        promotion.return_to_creators(directory, reason="x", apply_fixes=True, reject=False, actor="admin")
    assert tree_sha256(draft) == before and themes.read(directory)["status"] == themes.STATUS_REVIEW


def test_promote_keeps_promotion_when_archiving_fails(lab_env, monkeypatch):
    """配置と設定の登録の後でアーカイブに失敗しても、昇格は取り消さずに記録へ残す。"""
    monkeypatch.setattr(promotion, "PROMOTION_LOG", lab_env["tmp"] / "promotion_log.md")
    monkeypatch.setattr(promotion, "PROMOTE_HISTORY_DIR", lab_env["tmp"] / "history")
    directory, draft = _ready_theme(lab_env)

    def boom(*_a, **_kw):
        raise OSError("使用中")

    monkeypatch.setattr(promotion, "archive_draft", boom)
    record = _promote(lab_env, directory)
    assert record["archive_errors"] and draft.is_dir()
    assert themes.read(directory)["status"] == themes.STATUS_PROMOTED


def test_api_delete_theme_removes_folder_and_unmarks_draft(client, lab_env):
    draft = _draft(lab_env)
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A", "lab": "lab1"}).json()
    base = f"/api/instances/org_a/skill-lab/themes/{theme['id']}"
    res = client.post(f"{base}/assets", json={"mode": "draft", "asset_type": "skills", "name": "my-skill", "origin": str(draft)})
    assert res.status_code == 200
    assert "lab" in json.loads((draft / "_draft_meta.json").read_text(encoding="utf-8"))
    # 他のインスタンスの下からは消せない
    assert client.delete(f"/api/instances/org_b/skill-lab/themes/{theme['id']}").status_code == 404
    assert client.delete(base).status_code == 200
    root = themes.themes_root(_cfg(lab_env, "org_a").common_data_dir)
    assert themes.list_theme_dirs(root) == [] and list(root.iterdir()) == []
    # ドラフトは残り、研究室の印だけが外れて、また取り込める
    assert (draft / "SKILL.md").is_file()
    assert "lab" not in json.loads((draft / "_draft_meta.json").read_text(encoding="utf-8"))
    assert [d["imported_theme"] for d in client.get("/api/instances/org_a/skill-lab/sources").json()["drafts"]] == [None]
    assert client.delete(base).status_code == 404
    actions = [json.loads(line)["action"] for line in (lab_env["tmp"] / "audit.log").read_text(encoding="utf-8").splitlines()]
    assert actions[-1] == "skill_lab_theme_delete"


def test_api_delete_theme_in_use_keeps_draft_mark(client, lab_env, monkeypatch):
    # フォルダが使用中で消せないときは、取り込み元のドラフトの印を外さない（テーマが残るのに印だけ外れない）
    draft = _draft(lab_env)
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A", "lab": "lab1"}).json()
    base = f"/api/instances/org_a/skill-lab/themes/{theme['id']}"
    asset = {"mode": "draft", "asset_type": "skills", "name": "my-skill", "origin": str(draft)}
    assert client.post(f"{base}/assets", json=asset).status_code == 200

    def in_use(directory):
        raise themes.ThemeError("in use")

    monkeypatch.setattr(themes, "stage_theme_for_delete", in_use)
    assert client.delete(base).status_code == 409
    assert "lab" in json.loads((draft / "_draft_meta.json").read_text(encoding="utf-8"))


def test_api_delete_theme_refuses_active_or_busy_and_allows_broken(client, lab_env):
    directory = _theme(lab_env)
    url = f"/api/instances/org_a/skill-lab/themes/{directory.name}"
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_RUNNING}))
    assert client.delete(url).status_code == 409
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_STOPPED, "tasks": [{"id": "t1", "type": "draft_spec", "status": "running"}]}))
    assert client.delete(url).status_code == 409
    assert directory.is_dir()
    # theme.json が壊れて開けないテーマも消せる
    (directory / themes.THEME_FILENAME).write_text("{broken", encoding="utf-8")
    assert client.get(url).status_code == 409
    assert client.delete(url).status_code == 200
    assert not directory.exists()


def test_api_delete_theme_restores_folder_on_any_unmark_error(client, lab_env, monkeypatch):
    # 印を外す処理がどんな例外で失敗しても、テーマのフォルダを元の名前に戻す（消えたことにしない）
    directory = _theme(lab_env)
    url = f"/api/instances/org_a/skill-lab/themes/{directory.name}"

    def broken(staging):
        raise KeyError("id")

    monkeypatch.setattr(promotion, "unmark_imported", broken)
    assert client.delete(url).status_code == 409
    assert directory.is_dir()


# --- 終わったテーマの再開（reopen） ------------------------------------------------------


def test_api_reopen_cancelled_theme_remarks_draft_and_allows_editing(client, lab_env):
    draft = _draft(lab_env)
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A", "lab": "lab1"}).json()
    base = f"/api/instances/org_a/skill-lab/themes/{theme['id']}"
    assert client.post(f"{base}/assets", json={"mode": "draft", "asset_type": "skills", "name": "my-skill", "origin": str(draft)}).status_code == 200
    # 終わっていないテーマは再開できない
    assert client.post(f"{base}/reopen").status_code == 409
    assert client.post(f"{base}/cancel").status_code == 200
    assert "lab" not in json.loads((draft / "_draft_meta.json").read_text(encoding="utf-8"))
    # 取りやめたままでは編集できない
    assert client.put(f"{base}/files", json={"path": "spec.md", "content": "## Purpose\nx\n"}).status_code == 409

    res = client.post(f"{base}/reopen")
    assert res.status_code == 200 and res.json()["previous_status"] == themes.STATUS_CANCELLED
    data = client.get(base).json()
    assert data["status"] == themes.STATUS_DRAFT
    assert data["history"][-1]["action"] == "reopen"
    # 取り込みの印が付き直し、編集できる
    assert json.loads((draft / "_draft_meta.json").read_text(encoding="utf-8"))["lab"]["theme_id"] == theme["id"]
    assert client.put(f"{base}/files", json={"path": "spec.md", "content": "## Purpose\nx\n"}).status_code == 200
    actions = [json.loads(line)["action"] for line in (lab_env["tmp"] / "audit.log").read_text(encoding="utf-8").splitlines()]
    assert actions[-2] == "skill_lab_reopen"


def test_api_reopen_refuses_when_draft_is_taken_by_another_theme(client, lab_env):
    draft = _draft(lab_env)
    themes_api = "/api/instances/org_a/skill-lab/themes"
    first = client.post(themes_api, json={"title": "A", "lab": "lab1"}).json()
    body = {"mode": "draft", "asset_type": "skills", "name": "my-skill", "origin": str(draft)}
    assert client.post(f"{themes_api}/{first['id']}/assets", json=body).status_code == 200
    assert client.post(f"{themes_api}/{first['id']}/cancel").status_code == 200
    second = client.post(themes_api, json={"title": "B", "lab": "lab1"}).json()
    assert client.post(f"{themes_api}/{second['id']}/assets", json=body).status_code == 200
    res = client.post(f"{themes_api}/{first['id']}/reopen")
    assert res.status_code == 409 and second["id"] in res.json()["detail"]
    assert client.get(f"{themes_api}/{first['id']}").json()["status"] == themes.STATUS_CANCELLED


def test_reopened_promoted_theme_can_be_promoted_again_replacing_its_own_assets(lab_env, client, monkeypatch):
    monkeypatch.setattr(promotion, "PROMOTION_LOG", lab_env["tmp"] / "promotion_log.md")
    monkeypatch.setattr(promotion, "PROMOTE_HISTORY_DIR", lab_env["tmp"] / "history")
    directory, _draft_dir = _ready_theme(lab_env)
    first = _promote(lab_env, directory, distribute_to=["org_b"])
    dest = _cfg(lab_env, "org_a").instance_locohane_dir / "skills" / "my-skill" / "SKILL.md"
    assert first["placements"]["org_a"]["placed_sha256"]

    base = f"/api/instances/org_a/skill-lab/themes/{directory.name}"
    assert client.post(f"{base}/reopen").status_code == 200
    data = themes.read(directory)
    assert data["status"] == themes.STATUS_DRAFT and "promotion" not in data and len(data["previous_promotions"]) == 1

    # 直して、もう一度トライアウトに合格したことにする
    themes.write_text(directory, "assets/skills/my-skill/SKILL.md", _skill_md("my-skill", "直したスキル。"), actor="admin")
    themes.update(directory, lambda d: d.update({"tryout": _tryout_for(directory), "status": themes.STATUS_REVIEW}))
    check = promotion.check(lab_env["root"], directory, distribute_to=["org_b"])
    assert check.problems == []
    assert check.placements["org_a"]["replaces"] and check.placements["org_b"]["replaces"]
    _promote(lab_env, directory, distribute_to=["org_b"])
    assert "直したスキル。" in dest.read_text(encoding="utf-8")
    assert themes.read(directory)["status"] == themes.STATUS_PROMOTED

    # 前の昇格の後に別のところで変えられたものは置き換えない
    assert client.post(f"{base}/reopen").status_code == 200
    dest.write_text(_skill_md("my-skill", "誰かが直接直した。"), encoding="utf-8")
    themes.update(directory, lambda d: d.update({"tryout": _tryout_for(directory), "status": themes.STATUS_REVIEW}))
    problems = promotion.check(lab_env["root"], directory).problems
    assert any("changed since then" in p for p in problems)


# --- トークン使用量（「利用量・料金」の台帳） -------------------------------------------------


def test_worker_records_eval_and_ai_token_usage(lab_env, monkeypatch):
    from admin import ai_usage
    from admin.skill_lab import evaluation, judge, llm as lab_llm

    w, _calls = _make_worker(monkeypatch, lab_env, [])
    directory = _theme(lab_env)
    _add_skill(directory)
    themes.write_text(directory, "cases/001.yaml", _CASE.format(id="001"), actor="admin")
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_RUNNING, "loop": {"mode": "loop", "by": "alice"}}))
    results = [
        {"case_id": "001", "token_usage_total": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}, "token_usage_max_per_call": {"calls": 2}},
        {"case_id": "001", "token_usage_total": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60}, "token_usage_max_per_call": {"calls": 1}},
    ]
    assert evaluation.token_usage(results) == ({"input_tokens": 150, "output_tokens": 30, "total_tokens": 180}, 3)
    monkeypatch.setattr(evaluation, "run", lambda *a, **k: {"out_dir": str(directory), "results": results, "tryout": {}})

    def fake_judge(cfg, **kw):
        # judge の LLM 呼び出しの usage が sink（usage_sink）経由で記録される
        sink = lab_llm._USAGE_SINK.get()
        sink({"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
        return [{"case_id": "001", "repeat_index": 1, "outcome": "pass"}]

    monkeypatch.setattr(judge, "judge_results", fake_judge)
    monkeypatch.setattr(w, "_llm_config", lambda *a, **k: None)
    owner = w._usage_owner(themes.read(directory))
    with lab_llm.usage_sink(lambda usage: w._record_usage(owner, usage)):
        type(w)._evaluate(w, "org_a", directory, 1, "eval")
    usage = ai_usage.monthly_usage_by_source(lab_env["root"])["lab1"]
    month = usage["months"][-1]
    assert month["total"] == 190 and month["calls"] == 4
    assert list(month["users"]) == ["alice"]


def test_lab_llm_passes_usage_metadata_to_the_sink(monkeypatch):
    from langchain_core.messages import AIMessage

    import src.llm
    from admin.skill_lab import llm as lab_llm

    class _Model:
        async def ainvoke(self, messages):
            return AIMessage(content='{"ok": true}', usage_metadata={"input_tokens": 11, "output_tokens": 4, "total_tokens": 15})

    async def _build(_cfg):
        return _Model()

    monkeypatch.setattr(src.llm, "build_model", _build)
    seen = []
    with lab_llm.usage_sink(seen.append):
        assert lab_llm.ask_json(None, "sys", "user") == {"ok": True}
    assert lab_llm.ask_text(None, "sys", "user")  # sink の外では記録しない
    assert seen == [{"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}]


def test_old_promotion_record_without_hashes_does_not_replace_the_placed_assets(lab_env, client, monkeypatch):
    """置いた時点のハッシュが無い古い昇格の記録では、変わっていないと確かめられないため置き換えない。"""
    monkeypatch.setattr(promotion, "PROMOTION_LOG", lab_env["tmp"] / "promotion_log.md")
    monkeypatch.setattr(promotion, "PROMOTE_HISTORY_DIR", lab_env["tmp"] / "history")
    directory, _draft_dir = _ready_theme(lab_env)
    _promote(lab_env, directory)

    def _drop_hashes(d):
        for placement in d["promotion"]["placements"].values():
            placement.pop("placed_sha256", None)

    themes.update(directory, _drop_hashes)  # この機能より前の昇格の記録と同じ形
    base = f"/api/instances/org_a/skill-lab/themes/{directory.name}"
    assert client.post(f"{base}/reopen").status_code == 200
    themes.update(directory, lambda d: d.update({"tryout": _tryout_for(directory), "status": themes.STATUS_REVIEW}))
    problems = promotion.check(lab_env["root"], directory).problems
    assert any("cannot be confirmed" in p for p in problems)


# --- 機械判定の見直し（admin/skill_lab/judge.py の review_rules。LLM は差し替える） ----------------


def _rule_result(rule_results, final_answer="", judge_text=None):
    return {
        "case_id": "c1",
        "repeat_index": 1,
        "rule_results": rule_results,
        "rules_pass": all(v["pass"] for v in rule_results.values()),
        "final_answer": final_answer,
        "judge": judge_text,
        "transcript": [],
    }


def test_rule_review_passes_notation_differences_without_the_llm(monkeypatch):
    from admin.skill_lab import judge

    monkeypatch.setattr(judge, "ask_json", lambda *a, **k: pytest.fail("the LLM must not be called"))
    r = _rule_result(
        {
            "response_contains": {"pass": False, "detail": "missing", "missing": ["合計 1234円", "ＡＢＣ"]},
            "tool_call_args_contains:Read": {
                "pass": False,
                "detail": "x",
                "expected": {"file_path": "C:/work/a.xlsx", "limit": 10},
                "calls": [{"file_path": r"C:\work\a.xlsx", "limit": "10"}],
            },
        },
        final_answer="合計 1,234 円です。abc も確認しました。",
    )
    [verdict] = judge.judge_results(None, spec="", results=[r], excerpt_chars=1000)
    assert verdict["outcome"] == "pass"
    assert verdict["rule_review"]["how"] == "normalized" and verdict["rule_review"]["pass"] is True


def test_rule_review_asks_the_llm_for_wording_differences_then_runs_the_judge(monkeypatch):
    from admin.skill_lab import judge

    prompts = []

    def fake_ask(_config, system, user):
        prompts.append(system)
        return {"pass": True, "reason": "same date written differently"}

    monkeypatch.setattr(judge, "ask_json", fake_ask)
    r = _rule_result(
        {"response_contains": {"pass": False, "detail": "missing", "missing": ["2027/1/1"]}},
        final_answer="2027年1月1日（金）に設定しました。",
        judge_text="日付を正しく設定している",
    )
    [verdict] = judge.judge_results(None, spec="", results=[r], excerpt_chars=1000)
    assert verdict["rule_review"]["how"] == "ai" and verdict["rule_review"]["pass"] is True
    assert "ai_judge" in verdict and verdict["outcome"] == "pass"
    assert len(prompts) == 2 and "exactly" in prompts[0]  # 見直し → judge の順


def test_rule_review_keeps_tool_choice_failures_and_llm_failures_as_fail(monkeypatch):
    from admin.skill_lab import judge
    from admin.skill_lab.llm import LabLLMError

    monkeypatch.setattr(judge, "ask_json", lambda *a, **k: pytest.fail("the LLM must not be called"))
    hard = _rule_result(
        {
            "tool_not_called": {"pass": False, "detail": "unexpectedly called: ['execute_python_code']"},
            "response_contains": {"pass": False, "detail": "missing", "missing": ["x"]},
        }
    )
    [verdict] = judge.judge_results(None, spec="", results=[hard], excerpt_chars=1000)
    assert verdict["outcome"] == "fail" and "rule_review" not in verdict

    def broken(*a, **k):
        raise LabLLMError("down")

    monkeypatch.setattr(judge, "ask_json", broken)
    wrong = _rule_result({"response_contains": {"pass": False, "detail": "missing", "missing": ["1234円"]}}, final_answer="5678円")
    [verdict] = judge.judge_results(None, spec="", results=[wrong], excerpt_chars=1000)
    assert verdict["outcome"] == "fail" and verdict["rule_review"]["pass"] is False


def test_fixer_does_not_show_accepted_notation_differences_as_unmet():
    from admin.skill_lab import fixer

    r = _rule_result({"response_contains": {"pass": False, "detail": "missing", "missing": ["x"]}}, judge_text="j")
    verdict = {
        "case_id": "c1",
        "repeat_index": 1,
        "outcome": "fail",
        "rule_review": {"pass": True, "how": "ai", "rules": ["response_contains"], "reason": "ok"},
        "ai_judge": {"pass": False, "reason": "the summary is missing"},
    }
    text = fixer.failures_text([r], [verdict], excerpt_chars=1000)
    assert "Unmet checks" not in text and "accepted" in text and "the summary is missing" in text


# --- 同じ直しの繰り返し・ケース自体の問題でループを止める（2026-11-13 ユーザー指摘） -----------------


def _stall_worker(monkeypatch, lab_env, directory, fixes):
    """評価は常に不合格。修正は fixes の順に SKILL.md の本文を書き換える（None は修正できなかった）。"""
    w, _ = _make_worker(monkeypatch, lab_env, [], max_iterations=10)
    skill_md = directory / "assets" / "skills" / "my-skill" / "SKILL.md"
    queue = list(fixes)
    evaluated = []

    def fake_evaluate(instance, d, repeat, phase):
        it = {"n": len(evaluated) + 1, "phase": phase, "repeat": repeat, "verdict": "fail", "cases": {"001": {"fail": 1}},
              "at": themes.now_iso(), "state_sha": themes.state_sha256(d)}
        evaluated.append(it)

        def _apply(data):
            data.setdefault("iterations", []).append(it)
            data["loop"]["iterations_used"] = data["loop"].get("iterations_used", 0) + 1

        themes.update(d, _apply)
        return {"iteration": it, "run": {"results": []}, "verdicts": [], "passed": False}

    def fake_fix(instance, d, outcome):
        body = queue.pop(0)
        if body is not None:
            skill_md.write_text(_skill_md("my-skill") + body, encoding="utf-8")
        return []

    monkeypatch.setattr(w, "_evaluate", fake_evaluate)
    monkeypatch.setattr(w, "_fix", fake_fix)
    return w, evaluated


@pytest.mark.parametrize(
    "fixes, evaluations",
    [
        (["A", "B", "A"], 3),  # 2つの直し方を行ったり来たり → 3回目の修正で、反復2と同じ内容に戻ったので止める
        ([None, None], 2),  # 直せないまま同じ内容 → 1回だけ評価し直し、2回目で止める
    ],
    ids=["back_and_forth", "no_change"],
)
def test_loop_stops_when_fix_returns_to_failed_content(lab_env, monkeypatch, fixes, evaluations):
    directory = _theme(lab_env)
    _add_skill(directory, script=False)
    themes.write_text(directory, "cases/001.yaml", _CASE.format(id="001"), actor="admin")
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop"}}))
    w, evaluated = _stall_worker(monkeypatch, lab_env, directory, fixes)
    w.run_theme("org_a", directory)
    data = themes.read(directory)
    assert data["status"] == themes.STATUS_STALLED
    assert len(evaluated) == evaluations  # 上限（10回）まで回さない


def test_history_shows_what_was_tried():
    """次の反復の AI には、直したファイル名だけでなく直しの中身と、まだ落ちたケースを見せる。"""
    from admin.skill_lab import fixer as fixer_mod

    summary = fixer_mod.edit_summary([{"path": "assets/skills/s/SKILL.md", "old": "手順1", "new": "手順1（必ず読む）"}])
    text = fixer_mod.history_text([{"n": 1, "phase": "eval", "verdict": "fail", "cases": {"001": {"fail": 1, "pass": 0}},
                                    "fix": {"analysis": "読み込み不足", "applied": ["assets/skills/s/SKILL.md"], "errors": [], "edit_summary": summary}}])
    assert "failed cases 001" in text and "手順1（必ず読む）" in text and "読み込み不足" in text
    # トライアウト（複数回）では何回中何回合格したかも見せる（ゆらぎの見分け）
    assert fixer_mod.failed_cases_text({"cases": {"001": {"pass": 7, "fail": 3}, "002": {"pass": 10, "fail": 0}}}) == "001 (passed 7/10)"


def test_api_deletes_a_case_but_not_during_a_loop(client, lab_env):
    """ケースは一覧の「削除」（ファイル削除の API）で消せる。ループ中は消せない（2026-11-13 ユーザー報告: 画面に削除が無かった）。"""
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A", "lab": "lab1"}).json()
    base = f"/api/instances/org_a/skill-lab/themes/{theme['id']}"
    for cid in ("c1", "c2"):
        assert client.put(f"{base}/cases/{cid}", json={"data": {"turns": ["x"], "judge": "y"}}).status_code == 200
    directory = themes.theme_dir(themes.themes_root(_cfg(lab_env, "org_a").common_data_dir), theme["id"])
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_RUNNING}))
    assert client.delete(f"{base}/files", params={"path": "cases/c1.yaml"}).status_code == 409
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_DRAFT}))
    assert client.delete(f"{base}/files", params={"path": "cases/c1.yaml"}).status_code == 200
    assert [c["id"] for c in client.get(base).json()["cases_list"]] == ["c2"]


def test_tryout_repeats_moved_to_skill_lab_and_old_entries_still_work(lab_env):
    """トライアウトの回数は [skill_lab] tryout_repeats。旧い [skill_creator] の書き方も読む（黙って無視しない）。"""
    path = lab_env["root"] / "org_a" / "config_overrides.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert _cfg(lab_env, "org_a").skill_lab_tryout_repeats == 10
    data["skill_creator"] = {"tryout_repeats": "4"}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert _cfg(lab_env, "org_a").skill_lab_tryout_repeats == 4
    data["skill_lab"] = {"tryout_repeats": "6"}  # 新しい場所の値があればそちらを使う
    path.write_text(json.dumps(data), encoding="utf-8")
    assert _cfg(lab_env, "org_a").skill_lab_tryout_repeats == 6


def test_tryout_repeats_can_be_set_per_theme(client, lab_env, monkeypatch):
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A", "lab": "lab1"}).json()
    base = f"/api/instances/org_a/skill-lab/themes/{theme['id']}"
    detail = client.get(base).json()
    assert detail["tryout_repeats_default"] == 10 and detail["tryout_repeats_effective"] == 10
    assert client.put(base, json={"tryout_repeats": 3}).status_code == 200
    assert client.get(base).json()["tryout_repeats_effective"] == 3
    assert client.put(base, json={"title": "B"}).status_code == 200  # 送らなければ変えない
    assert client.get(base).json()["tryout_repeats"] == 3
    assert client.put(base, json={"tryout_repeats": 0}).status_code == 400
    directory = themes.theme_dir(themes.themes_root(_cfg(lab_env, "org_a").common_data_dir), theme["id"])
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED}))
    assert client.put(base, json={"tryout_repeats": 5}).status_code == 409  # 実行中・開始待ちは変えられない
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_DRAFT}))
    # ワーカーはテーマの回数でトライアウトする
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop"}}))
    w, calls = _make_worker(monkeypatch, lab_env, [True, True])
    w.run_theme("org_a", directory)
    assert calls["eval"][1] == ("tryout", 3, True)
    assert client.put(base, json={"tryout_repeats": None}).status_code == 200  # null で既定に戻す
    assert "tryout_repeats" not in client.get(base).json()


# --- 実行の記録のクリア・進み具合（2026-11-14 ユーザー要望） -----------------------------------


def _theme_with_runs(client, lab_env):
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "A", "lab": "lab1"}).json()
    directory = themes.theme_dir(themes.themes_root(_cfg(lab_env, "org_a").common_data_dir), theme["id"])
    _add_skill(directory)
    themes.write_text(directory, "cases/001.yaml", _CASE.format(id="001"), actor="admin")
    (directory / "runs" / "001").mkdir(parents=True)
    (directory / "runs" / "001" / "results.json").write_text("[]", encoding="utf-8")
    (directory / "runs" / "trials" / "t1").mkdir(parents=True)
    themes.update(
        directory,
        lambda d: d.update(
            {
                "status": themes.STATUS_EXHAUSTED,
                "iterations": [{"n": 1, "phase": "eval", "verdict": "fail", "cases": {}}],
                "tryout": {"repeat": 3},
                "baseline_eval": {"cases": {}},
                "loop": {"mode": "loop", "report": {"summary": "x"}, "progress": {"step": "eval"}},
                "tasks": [{"id": "t1", "type": "trial", "status": "done", "params": {}}, {"id": "d1", "type": "draft_spec", "status": "done", "params": {}}],
            }
        ),
    )
    return f"/api/instances/org_a/skill-lab/themes/{theme['id']}", directory


def test_clear_runs_removes_records_but_keeps_the_work(client, lab_env):
    base, directory = _theme_with_runs(client, lab_env)
    summary = client.get(f"{base}/runs/summary").json()
    assert summary["iterations"] == 1 and summary["trials"] == 1 and summary["files"] == 1 and summary["blocked_reason"] is None
    assert summary["resets_status"] is True
    res = client.delete(f"{base}/runs")
    assert res.status_code == 200 and res.json()["iterations"] == 1
    data = themes.read(directory)
    assert data["iterations"] == [] and data["tryout"] is None and data["loop"] is None and "baseline_eval" not in data
    assert data["status"] == themes.STATUS_DRAFT  # 結果の状態は準備中に戻る
    assert [t["id"] for t in data["tasks"]] == ["d1"]  # 下書きの依頼は残し、1回試行だけ消す
    assert list((directory / "runs").iterdir()) == []
    # 研究対象・ケース・履歴は残る
    assert (directory / "assets" / "skills" / "my-skill" / "SKILL.md").is_file() and (directory / "cases" / "001.yaml").is_file()
    assert data["history"][-1]["action"] == "clear_runs"


def test_clear_runs_is_refused_while_running_or_trial_or_closed(client, lab_env):
    base, directory = _theme_with_runs(client, lab_env)
    for status in (themes.STATUS_QUEUED, themes.STATUS_RUNNING):
        themes.update(directory, lambda d, st=status: d.update({"status": st}))
        assert client.get(f"{base}/runs/summary").json()["blocked_reason"]
        assert client.delete(f"{base}/runs").status_code == 409
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_DRAFT, "tasks": [{"id": "t2", "type": "trial", "status": "running", "params": {}}]}))
    assert client.delete(f"{base}/runs").status_code == 409
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_PROMOTED, "tasks": []}))
    assert client.delete(f"{base}/runs").status_code == 409
    # どれも断られたので、記録は残っている
    assert (directory / "runs" / "001" / "results.json").is_file() and themes.read(directory)["iterations"]


def test_clear_runs_check_and_delete_are_atomic(lab_env, monkeypatch):
    """確かめてから消し終わるまで排他を持つ。確かめた後に実行中になっても消さない（競合しても runs/ を壊さない）。"""
    directory = _theme(lab_env)
    (directory / "runs" / "001").mkdir(parents=True)
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_RUNNING}))
    with pytest.raises(themes.ThemeError):
        themes.clear_runs(directory, actor="admin")
    assert (directory / "runs" / "001").is_dir()


def test_worker_records_progress_and_clears_it_when_finished(lab_env, monkeypatch):
    directory = _theme(lab_env)
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_RUNNING, "loop": {"mode": "loop"}}))
    w = _lab_worker(lab_env)
    w._progress(directory, "eval", n=1, repeat_count=1, done=2, total=5, case_id="c2")
    progress = themes.read(directory)["loop"]["progress"]
    assert progress["step"] == "eval" and progress["done"] == 2 and progress["total"] == 5 and progress["since"]
    since = progress["since"]
    w._progress(directory, "eval", n=1, repeat_count=1, done=3, total=5, case_id="c3")
    assert themes.read(directory)["loop"]["progress"]["since"] == since  # 同じ段階の間は開始時刻を保つ
    monkeypatch.setattr(w, "_write_report", lambda *a: None)
    w._finish(directory, themes.STATUS_STOPPED, "x")
    assert themes.read(directory)["loop"]["progress"] is None


def test_evaluation_reads_progress_lines_of_this_run_only(tmp_path):
    from admin.skill_lab import evaluation

    log = tmp_path / "run_all.log"
    log.write_text("[progress] 9/9 old 1/1\n", encoding="utf-8")
    start = log.stat().st_size
    assert evaluation.read_progress(log, start) is None
    with open(log, "a", encoding="utf-8") as f:
        f.write("実行中: a\n[progress] 1/6 a 1/3\n[progress] 4/6 b 2/3\n")
    assert evaluation.read_progress(log, start) == {"done": 4, "total": 6, "case_id": "b", "repeat": 2, "repeats": 3}


def test_eval_parallelism_comes_from_the_lab_worker(lab_env):
    """並列数（LLM の同時リクエスト数・メイン／サブエージェントの並列）は、評価に使う LLM を持つ研究室ワーカーの値を使う。"""
    from admin import instances as inst_mod
    from evals.instance import llm_env_from_instance

    lab_path = lab_env["root"] / "lab1" / "config_overrides.json"
    data = json.loads(lab_path.read_text(encoding="utf-8"))
    data["llm"]["max_concurrent_requests"] = "3"
    data["graph"] = {"max_parallel": "2"}
    data["subagent"] = {"max_parallel": "4"}
    lab_path.write_text(json.dumps(data), encoding="utf-8")
    env = llm_env_from_instance("lab1")
    assert (env["LLM_MAX_CONCURRENT_REQUESTS"], env["GRAPH_MAX_PARALLEL"], env["SUBAGENT_MAX_PARALLEL"]) == ("3", "2", "4")
    # ワーカーの設定画面に出す（本体インスタンスの値は使わない）
    for section, key in (("llm", "max_concurrent_requests"), ("graph", "max_parallel"), ("subagent", "max_parallel")):
        assert inst_mod.config_key_applies(inst_mod.KIND_SKILL_LAB, section, key)


# --- 大文字小文字・巻き戻し・控えの回帰テスト（2026-11-14 のバグチェック） ----------------------------


@pytest.mark.parametrize("name", ["skill.md", "Skill.md", "SKILL.MD"])
def test_skill_md_in_other_case_cannot_bypass_validation_or_delete_guard(lab_env, name):
    """Windows では skill.md が SKILL.md そのものを指す。名前の比べ方は大文字小文字を区別するため、
    以前は frontmatter の検証・削除の禁止（画面と AI 修正の両方）をすり抜けて SKILL.md を壊せた。"""
    directory = _theme(lab_env)
    _add_skill(directory)
    skill_md = directory / "assets" / "skills" / "my-skill" / "SKILL.md"
    before = skill_md.read_text(encoding="utf-8")
    with pytest.raises(themes.ThemeError, match="SKILL.md"):
        themes.write_text(directory, f"assets/skills/my-skill/{name}", "frontmatter なし", actor="admin")
    with pytest.raises(themes.ThemeError):
        themes.delete_path(directory, f"assets/skills/my-skill/{name}", actor="admin")
    applied, errors = fixer.apply_edits(directory, [{"path": f"assets/skills/my-skill/{name}", "delete": True}], known_tools=None)
    assert applied == [] and errors
    applied, errors = fixer.apply_edits(directory, [{"path": f"assets/skills/my-skill/{name}", "content": "壊れた"}], known_tools=None)
    assert applied == [] and errors
    assert skill_md.read_text(encoding="utf-8") == before


def test_drafted_case_does_not_overwrite_a_case_differing_only_in_case(lab_env, monkeypatch):
    """AI の下書きが 001_Basic を出しても、既存の 001_basic.yaml（Windows では同じファイル）を上書きしない。"""
    from admin.skill_lab import drafting

    directory = _theme(lab_env)
    _add_skill(directory)
    themes.write_text(directory, "cases/001_basic.yaml", _CASE.format(id="001_basic"), actor="admin")
    human = (directory / "cases" / "001_basic.yaml").read_text(encoding="utf-8")
    monkeypatch.setattr(drafting, "ask_json", lambda *_a, **_k: [{"id": "001_Basic", "turns": ["AI のケース"], "judge": "x"}])
    created = drafting.draft_cases(None, directory, max_file_chars=1000, count=1)
    assert created and created[0].casefold() != "001_basic"
    assert (directory / "cases" / "001_basic.yaml").read_text(encoding="utf-8") == human
    assert len(list((directory / "cases").glob("*.yaml"))) == 2


def test_promote_failure_restores_the_assets_and_cases_it_replaced(lab_env, monkeypatch):
    """置き換えるスキルを退避した後に複製が失敗しても、退避した元のスキルと前回の回帰テスト用ケースを戻す。

    以前は退避した組を巻き戻しの記録に入れる前に複製していたため、「元に戻しました」と表示しながら
    元のスキルが退避先に残ったままになり、前回のケース一式も消えていた。
    """
    monkeypatch.setattr(promotion, "PROMOTION_LOG", lab_env["tmp"] / "promotion_log.md")
    monkeypatch.setattr(promotion, "PROMOTE_HISTORY_DIR", lab_env["tmp"] / "history")
    directory, _ = _ready_theme(lab_env, with_draft=False)
    _add_skill(directory)
    themes.update(directory, lambda d: d.update({"tryout": _tryout_for(directory), "status": themes.STATUS_REVIEW}))
    _promote(lab_env, directory)
    dest = _cfg(lab_env, "org_a").instance_locohane_dir
    placed_skill = dest / "skills" / "my-skill"
    first = (placed_skill / "SKILL.md").read_text(encoding="utf-8")
    assert (dest / "evals" / directory.name / "001.yaml").is_file()

    # 再開して直したことにして、もう一度昇格する（今度はスキルの複製が失敗する）
    def _reopen(d):
        d.setdefault("previous_promotions", []).append(d.pop("promotion"))
        d["status"] = themes.STATUS_DRAFT

    themes.update(directory, _reopen)
    themes.write_text(directory, "assets/skills/my-skill/SKILL.md", _skill_md("my-skill", "直したスキル。"), actor="admin")
    themes.update(directory, lambda d: d.update({"tryout": _tryout_for(directory), "status": themes.STATUS_REVIEW}))
    real_copytree = promotion.shutil.copytree

    def failing_copytree(src, dst, *a, **k):
        if Path(dst) == placed_skill:
            raise OSError("ディスクがいっぱい（模擬）")
        return real_copytree(src, dst, *a, **k)

    monkeypatch.setattr(promotion.shutil, "copytree", failing_copytree)
    with pytest.raises(promotion.PromotionError, match="元に戻しました"):
        _promote(lab_env, directory)
    assert (placed_skill / "SKILL.md").read_text(encoding="utf-8") == first
    assert (dest / "evals" / directory.name / "001.yaml").is_file()


def test_archive_failure_does_not_hide_the_draft_from_its_author(lab_env, monkeypatch):
    """移せなかったドラフトの来歴を「昇格済み」にしない（作成者の一覧から消え、アーカイブもされない状態を防ぐ）。"""
    draft = _draft(lab_env)

    def boom(*_a, **_k):
        raise OSError("使用中")

    monkeypatch.setattr(promotion.shutil, "move", boom)
    with pytest.raises(OSError):
        promotion.archive_draft(draft, _cfg(lab_env, "org_a").skill_archive_dir)
    assert json.loads((draft / "_draft_meta.json").read_text(encoding="utf-8"))["status"] == "draft"


def test_fix_snapshots_the_current_state_even_if_an_old_before_fix_remains(lab_env, monkeypatch):
    """「実行の記録をクリア」で消し残った runs/<反復>/before_fix/ を、今回の直す前の控えとして使わない。"""
    from admin.skill_lab import worker as worker_mod

    directory = _theme(lab_env)
    _add_skill(directory)
    stale = directory / "runs" / "001" / "before_fix" / "assets" / "skills" / "my-skill"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text(_skill_md("my-skill", "前の回の古い内容"), encoding="utf-8")
    themes.update(directory, lambda d: d.update({"iterations": [{"n": 1, "phase": "eval", "verdict": "fail", "cases": {}}]}))

    w, _ = _make_worker(monkeypatch, lab_env, [])
    monkeypatch.setattr(w, "_llm_config", lambda *_a, **_k: None)
    monkeypatch.setattr(w, "_target_config", lambda *_a, **_k: None)
    monkeypatch.setattr(worker_mod.temp_dirs, "config_root", lambda _cfg: lab_env["tmp"] / "lab_tmp")
    monkeypatch.setattr(worker_mod.sources, "subagent_tool_names", lambda: ())
    new_md = _skill_md("my-skill", "AI が直した")
    monkeypatch.setattr(
        worker_mod.fixer,
        "propose",
        lambda *_a, **_k: {"analysis": "a", "blockers": [], "edits": [{"path": "assets/skills/my-skill/SKILL.md", "content": new_md}]},
    )
    current = (directory / "assets" / "skills" / "my-skill" / "SKILL.md").read_text(encoding="utf-8")
    worker_mod.Worker._fix(w, "org_a", directory, {"iteration": {"n": 1}, "run": {"results": []}, "verdicts": []})
    assert (stale / "SKILL.md").read_text(encoding="utf-8") == current
    assert (directory / "assets" / "skills" / "my-skill" / "SKILL.md").read_text(encoding="utf-8") == new_md


# --- ループの上限は対象インスタンスの設定・テーマごとの上級者向け設定（2026-11-14 ユーザー指摘） -----------------


def test_loop_limits_come_from_the_target_instance_and_theme_overrides_them(lab_env, monkeypatch):
    directory = _theme(lab_env)
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop"}}))
    w, calls = _make_worker(monkeypatch, lab_env, [False] * 10, max_iterations=4)
    w.run_theme("org_a", directory)
    assert len(calls["eval"]) == 4  # 対象インスタンスの [skill_lab] max_iterations

    # テーマの上級者向け設定があればそちらを使う（再開すると数え直す）
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop"}, "loop_settings": {"max_iterations": 2}}))
    w, calls = _make_worker(monkeypatch, lab_env, [False] * 10, max_iterations=4)
    w.run_theme("org_a", directory)
    data = themes.read(directory)
    assert len(calls["eval"]) == 2
    assert data["status"] == themes.STATUS_EXHAUSTED and "(2)" in data["loop"]["message"]


def test_theme_loop_settings_override_worker_settings(lab_env):
    from admin.skill_lab import worker as worker_mod

    directory = _theme(lab_env)
    themes.update(directory, lambda d: d.update({"loop_settings": {"fixer_max_reverts": 0, "excerpt_user_chars": 5, "bogus": 1}}))
    base = type("C", (), {"skill_lab_fixer_max_reverts": 3, "skill_lab_excerpt_user_chars": 800, "skill_lab_poll_interval_seconds": 10})()
    cfg = themes.loop_config(base, _cfg(lab_env, "org_a"), themes.read(directory))
    assert cfg.skill_lab_fixer_max_reverts == 0 and cfg.skill_lab_excerpt_user_chars == 5
    assert cfg.skill_lab_poll_interval_seconds == 10  # テーマで変えられない項目はワーカーの値
    assert cfg.skill_lab_max_iterations == _cfg(lab_env, "org_a").skill_lab_max_iterations
    from admin.skill_lab import evaluation

    assert evaluation.ExcerptLimits.from_config(cfg).user_chars == 5
    assert worker_mod.Worker  # import の確認


def test_validate_loop_settings():
    clean, errors = themes.validate_loop_settings(
        {"max_iterations": 3, "max_hours": 0.5, "evaluate_baseline": False, "excerpt_user_chars": None, "fixer_max_reverts": ""}
    )
    assert clean == {"max_iterations": 3, "max_hours": 0.5, "evaluate_baseline": False} and errors == []
    _, errors = themes.validate_loop_settings({"max_iterations": 0, "max_hours": "x", "evaluate_baseline": 1, "nope": 1, "fixer_max_reverts": 1.5})
    assert len(errors) == 5


def test_one_iteration_time_limit_cuts_off_the_evaluation(lab_env, monkeypatch):
    from admin.skill_lab import evaluation

    directory = _theme(lab_env)
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop"}}))
    w, _ = _make_worker(monkeypatch, lab_env, [])

    def slow_evaluate(instance, d, repeat, phase):
        w._start_step_clock()
        w._step_deadline = time.monotonic() - 1  # 期限を過ぎた状態にする
        assert w._should_stop_step(d)
        raise evaluation.EvaluationStopped()

    monkeypatch.setattr(w, "_evaluate", slow_evaluate)
    w.run_theme("org_a", directory)
    data = themes.read(directory)
    assert data["status"] == themes.STATUS_EXHAUSTED
    assert "time limit of one iteration" in data["loop"]["message"]
    assert data["loop"]["active"] is False


def test_api_saves_theme_loop_settings(client, lab_env):
    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "t", "lab": "lab1"}).json()
    base = f"/api/instances/org_a/skill-lab/themes/{theme['id']}"
    specs = {s["key"]: s for s in client.get(base).json()["loop_setting_specs"]}
    assert specs["max_iterations"]["source"] == "instance" and specs["max_iterations"]["default"] == _cfg(lab_env, "org_a").skill_lab_max_iterations
    assert specs["fixer_max_reverts"]["source"] == "worker" and "poll_interval_seconds" not in specs
    assert client.put(base, json={"loop_settings": {"max_iterations": 0}}).status_code == 400
    assert client.put(base, json={"loop_settings": {"max_iterations": 3, "evaluate_baseline": False}}).status_code == 200
    assert client.get(base).json()["loop_settings"] == {"max_iterations": 3, "evaluate_baseline": False}
    # 名前だけ変えても上級者向け設定は消えない。空で送ると既定に戻す
    assert client.put(base, json={"title": "t2"}).status_code == 200
    assert client.get(base).json()["loop_settings"] == {"max_iterations": 3, "evaluate_baseline": False}
    assert client.put(base, json={"loop_settings": {}}).status_code == 200
    assert "loop_settings" not in client.get(base).json()
    # 実行中・開始待ちは変えられない
    themes.update(themes.theme_dir(themes.themes_root(_cfg(lab_env, "org_a").common_data_dir), theme["id"]), lambda d: d.update({"status": themes.STATUS_QUEUED}))
    assert client.put(base, json={"loop_settings": {"max_iterations": 5}}).status_code == 409


def test_api_applies_the_ai_proposed_case_instruction(client, lab_env):
    """AI 修正が「ケースの指示が原因」と報告し直した指示の案を添えたら、人の承認でケースに書き込める（AI は自分では変えない）。"""
    import yaml as _yaml

    theme = client.post("/api/instances/org_a/skill-lab/themes", json={"title": "t", "lab": "lab1"}).json()
    base = f"/api/instances/org_a/skill-lab/themes/{theme['id']}"
    assert client.put(f"{base}/cases/c1", json={"data": {"turns": ["このスキルを呼んで"], "judge": "j"}}).status_code == 200
    directory = themes.theme_dir(themes.themes_root(_cfg(lab_env, "org_a").common_data_dir), theme["id"])
    blocker = {"kind": "case", "case_id": "c1", "problem": "名前が無い", "suggestion": "名前を書く", "new_instruction": "my-skill を呼んで"}
    # 止まっていないテーマには適用できない
    themes.update(directory, lambda d: d.update({"loop": {"blockers": [blocker]}}))
    assert client.post(f"{base}/blockers/0/apply").status_code == 409
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_BLOCKED}))
    assert client.post(f"{base}/blockers/0/apply").status_code == 200
    case = _yaml.safe_load((directory / "cases" / "c1.yaml").read_text(encoding="utf-8"))
    assert case["turns"] == ["my-skill を呼んで"] and case["judge"] == "j"
    assert themes.read(directory)["loop"]["blockers"][0]["applied"] is True


def test_blocked_stop_still_applies_the_case_settings_the_ai_may_change(lab_env, monkeypatch):
    """ケースの不備で止まるときも、AI が変えてよい設定（制限時間など）は、一番よかった版へ戻した後に入れておく
    （人がケースを直して再開したときに、同じ時間切れで落ちないように）。"""
    import yaml as _yaml

    directory = _theme(lab_env)
    _add_skill(directory)
    themes.write_text(directory, "cases/c1.yaml", "id: c1\ntarget: skill_lab\nturns: [x]\njudge: j\n", actor="admin")
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_RUNNING, "loop": {"mode": "loop"}}))
    w, _ = _make_worker(monkeypatch, lab_env, [])
    w._pending_case_settings = [{"path": "cases/c1.yaml", "answers": {"timeout_seconds": 1500}}]
    assert w._stop_for_blockers(directory, [{"kind": "case", "case_id": "c1", "problem": "p", "suggestion": "s"}])
    assert themes.read(directory)["status"] == themes.STATUS_BLOCKED
    assert _yaml.safe_load((directory / "cases" / "c1.yaml").read_text(encoding="utf-8"))["timeout_seconds"] == 1500
    assert w._pending_case_settings == []
