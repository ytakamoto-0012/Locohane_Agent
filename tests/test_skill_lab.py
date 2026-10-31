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


def _make_worker(monkeypatch, lab_env, outcomes, max_iterations=8):
    from admin.skill_lab import worker as worker_mod

    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.lab = "lab1"
    w.base_env = dict(os.environ)
    w.instances_root = lab_env["root"]
    w.shutdown = False
    w.config = type(
        "C",
        (),
        {
            "skill_lab_max_iterations": max_iterations,
            "skill_lab_max_hours": 6,
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
    assert calls["eval"][2][1] == _cfg(lab_env, "org_a").skill_tryout_repeats
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
    meta = inst.create_instance(lab_env["root"], Path("config.ini"), name="lab3", kind=inst.KIND_SKILL_LAB)
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
    assert inst.config_key_applies(lab, "skill_lab_worker", "max_iterations") and inst.config_key_applies(lab, "log", "level")
    assert not inst.config_key_applies(lab, "llm", "reasoning_effort")
    assert not inst.config_key_applies(lab, "graph", "token_guard_soft_threshold")
    assert not inst.config_key_applies(lab, "skill_lab", "instance_locohane_dir")
    assert not inst.config_key_applies(app, "skill_lab_worker", "max_iterations")
    assert inst.config_key_applies(app, "skill_lab", "instance_locohane_dir") and inst.config_key_applies(app, "llm", "reasoning_effort")
    # インスタンス別の上書きが無視されるキー（[admin]・[data_backup]・管理者設定のキー）はどちらにも出さない。
    for k in (app, lab):
        assert not inst.config_key_applies(k, "admin", "port")
        assert not inst.config_key_applies(k, "data_backup", "keep")
        assert not inst.config_key_applies(k, "vector_db", "ingest_workers")
    assert inst.config_key_applies(app, "vector_db", "chunk_size")


def test_worker_created_from_app_does_not_inherit_unused_settings(lab_env):
    """本体を複製してワーカーを作っても、ワーカーで効かない設定は持ち込まない。"""
    src = lab_env["root"] / "org_a" / "config_overrides.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    url = '[{"base_url": "http://org-a:8080/v1", "api_key": "x", "model": "m", "provider": "llama_cpp"}]'
    data.update({"llm": {"main_url": url, "reasoning_effort": "low"}, "graph": {"max_parallel": "2"}, "log": {"level": "debug"}})
    src.write_text(json.dumps(data), encoding="utf-8")
    inst.create_instance(lab_env["root"], Path("config.ini"), name="lab2", copy_from="org_a", kind=inst.KIND_SKILL_LAB)
    copied = json.loads((lab_env["root"] / "lab2" / "config_overrides.json").read_text(encoding="utf-8"))
    assert copied == {"llm": {"main_url": url}, "log": {"level": "debug"}}


def test_worker_ai_uses_target_settings_with_lab_endpoints(lab_env):
    """AI 修正・judge・下書きは、対象インスタンスの設定に研究室の接続先だけを重ねて呼ぶ。"""
    path = lab_env["root"] / "org_a" / "config_overrides.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["skill_creator"] = {"tryout_repeats": "3"}
    path.write_text(json.dumps(data), encoding="utf-8")
    w = _lab_worker(lab_env)
    w.config = _cfg(lab_env, "lab1")
    llm_cfg = w._llm_config("org_a")
    assert [e.base_url for e in llm_cfg.main_endpoints] == ["http://lab-host:9999/v1"]
    assert llm_cfg.sub_endpoints == w.config.sub_endpoints
    assert llm_cfg.skill_tryout_repeats == 3 and llm_cfg.common_data_dir == _cfg(lab_env, "org_a").common_data_dir


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
    # 今あるケースの中身を直すのはよい（レビューで強調表示する）
    fixed = _CASE.format(id="001").replace("judge: x", "judge: y")
    assert fixer.apply_edits(directory, [{"path": "cases/001.yaml", "content": fixed}], known_tools=["Read"])[1] == []


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

    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.lab, w.instances_root, w.shutdown, w.base_env = "lab1", lab_env["root"], False, dict(os.environ)
    w.config = type(
        "C",
        (),
        {"skill_lab_max_iterations": 3, "skill_lab_max_hours": 6, "skill_lab_transcript_excerpt_chars": 100, "skill_lab_fixer_max_file_chars": 100},
    )()
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
