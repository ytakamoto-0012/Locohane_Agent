"""skill-creator のドラフト操作スクリプトと、evals/run_all.py のスキル安定化トライアウト集計の回帰テスト。

skill-creator のスクリプトは run_script 経由で、Locohane 本体が渡す環境変数
（AGENT_SKILL_DRAFT_DIR / AGENT_USER / AGENT_OTHER_USERS_DRAFTS / AGENT_SKILL_ROOTS）
だけを頼りにドラフト置き場を扱う。実際にサブプロセスとして起動して確かめる。
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _PROJECT_ROOT / "skills" / "skill-creator" / "scripts"


@pytest.fixture
def draft_env(tmp_path):
    official = tmp_path / "skills"
    (official / "official-skill").mkdir(parents=True)
    (official / "official-skill" / "SKILL.md").write_text(
        "---\nname: official-skill\ndescription: 正式スキル。\n---\n\n# official-skill\n", encoding="utf-8"
    )
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "AGENT_SKILL_DRAFT_DIR": str(drafts),
        "AGENT_USER": "tanaka",
        "AGENT_OTHER_USERS_DRAFTS": "hidden",
        "AGENT_SKILL_ROOTS": str(official),
    }
    return {"env": env, "drafts": drafts, "official": official}


def _run(script: str, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _scaffold(draft_env, name="my-tool"):
    return _run("scaffold_skill.py", "--name", name, "--description", "テスト用のスキル。", env=draft_env["env"])


def test_scaffold_creates_draft_under_own_folder(draft_env):
    result = _scaffold(draft_env)
    assert result.returncode == 0, result.stderr
    skill_dir = draft_env["drafts"] / "tanaka" / "my-tool"
    assert (skill_dir / "SKILL.md").is_file() and (skill_dir / "evals").is_dir()
    meta = json.loads((skill_dir / "_draft_meta.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "new" and meta["author"] == "tanaka" and meta["status"] == "draft"


def test_scaffold_rejects_official_skill_name(draft_env):
    result = _run("scaffold_skill.py", "--name", "official-skill", "--description", "x", env=draft_env["env"])
    assert result.returncode == 1 and "fork_skill.py" in result.stderr


def test_scaffold_requires_session_user(draft_env):
    env = {k: v for k, v in draft_env["env"].items() if k != "AGENT_USER"}
    result = _run("scaffold_skill.py", "--name", "my-tool", "--description", "x", env=env)
    assert result.returncode == 1 and "The draft location is not available" in result.stderr


@pytest.mark.parametrize("path", ["../escape.txt", "_draft_meta.json", "C:/abs.txt"])
def test_write_draft_file_rejects_paths_outside_skill(draft_env, path):
    _scaffold(draft_env)
    result = _run("write_draft_file.py", "--name", "my-tool", "--path", path, "--content", "x", env=draft_env["env"])
    assert result.returncode == 1


def test_write_draft_file_rolls_back_invalid_skill_md(draft_env):
    _scaffold(draft_env)
    skill_md = draft_env["drafts"] / "tanaka" / "my-tool" / "SKILL.md"
    before = skill_md.read_text(encoding="utf-8")
    result = _run("write_draft_file.py", "--name", "my-tool", "--path", "SKILL.md", "--content", "no frontmatter", env=draft_env["env"])
    assert result.returncode == 1 and skill_md.read_text(encoding="utf-8") == before


def test_write_draft_file_patch_requires_unique_match(draft_env):
    _scaffold(draft_env)
    env = draft_env["env"]
    ok = _run("write_draft_file.py", "--name", "my-tool", "--path", "SKILL.md", "--old", "# my-tool", "--new", "# 道具", env=env)
    assert ok.returncode == 0, ok.stderr
    missing = _run("write_draft_file.py", "--name", "my-tool", "--path", "SKILL.md", "--old", "存在しない文", "--new", "x", env=env)
    assert missing.returncode == 1 and "matched 0 places" in missing.stderr


def test_fork_records_base_hash_and_keeps_official_untouched(draft_env):
    result = _run("fork_skill.py", "--name", "official-skill", env=draft_env["env"])
    assert result.returncode == 0, result.stderr
    meta = json.loads((draft_env["drafts"] / "tanaka" / "official-skill" / "_draft_meta.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "improve" and meta["base_skill"] == "official-skill" and len(meta["base_sha256"]) == 64
    assert sorted(p.name for p in (draft_env["official"] / "official-skill").iterdir()) == ["SKILL.md"]


def test_other_users_draft_cannot_be_written_unless_full(draft_env):
    other = draft_env["drafts"] / "suzuki" / "their-tool"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: their-tool\ndescription: 他人の。\n---\n", encoding="utf-8")
    args = ("write_draft_file.py", "--name", "suzuki/their-tool", "--path", "notes.md", "--content", "x")
    assert _run(*args, env=draft_env["env"]).returncode == 1
    full_env = {**draft_env["env"], "AGENT_OTHER_USERS_DRAFTS": "full"}
    assert _run(*args, env=full_env).returncode == 0


def test_list_drafts_hides_other_users_when_hidden(draft_env):
    _scaffold(draft_env)
    other = draft_env["drafts"] / "suzuki" / "their-tool"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: their-tool\ndescription: 他人の。\n---\n", encoding="utf-8")
    listed = json.loads(_run("list_drafts.py", env=draft_env["env"]).stdout)
    assert [d["draft"] for d in listed["drafts"]] == ["tanaka/my-tool"]
    listed = json.loads(_run("list_drafts.py", env={**draft_env["env"], "AGENT_OTHER_USERS_DRAFTS": "listed"}).stdout)
    assert [d["draft"] for d in listed["drafts"]] == ["tanaka/my-tool", "suzuki/their-tool"]


def test_make_eval_case_writes_into_draft(draft_env):
    _scaffold(draft_env)
    result = _run(
        "make_eval_case.py", "--name", "my-tool", "--case-id", "001_basic", "--turns", '["テストして"]', "--judge", "使えたか", env=draft_env["env"]
    )
    assert result.returncode == 0, result.stderr
    case = json.loads((draft_env["drafts"] / "tanaka" / "my-tool" / "evals" / "001_basic.yaml").read_text(encoding="utf-8"))
    assert case["target"] == "my-tool" and case["turns"] == ["テストして"]


def _load_run_all():
    spec = importlib.util.spec_from_file_location("evals_run_all_for_test", _PROJECT_ROOT / "evals" / "run_all.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("results", "verdict"),
    [
        ([{"case_id": "a", "rules_pass": True}, {"case_id": "a", "rules_pass": True}], "pass"),
        ([{"case_id": "a", "rules_pass": True}, {"case_id": "a", "rules_pass": False}], "fail"),
        ([{"case_id": "a", "rules_pass": True}, {"case_id": "a", "error": "timeout"}], "fail"),
        ([{"case_id": "a", "rules_pass": True, "judge": "x"}, {"case_id": "a", "rules_pass": True, "judge": "x"}], "needs_judge"),
    ],
)
def test_tryout_requires_every_repeat_to_pass(results, verdict):
    report = _load_run_all()._tryout_report(results, 2)
    assert report["verdict"] == verdict
    assert sum(report["cases"]["a"].values()) == 2


# --- 2026-10-10 レビュー指摘の修正 ---------------------------------------------


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclass の型解決が sys.modules[cls.__module__] を参照するため登録してから実行する
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_script_module(name: str, filename: str):
    sys.path.insert(0, str(_SCRIPTS))
    try:
        return _load_module(name, _SCRIPTS / filename)
    finally:
        sys.path.remove(str(_SCRIPTS))


def _load_promote_helper():
    return _load_module("promote_helper_for_test", _PROJECT_ROOT / ".claude" / "skills" / "promote-skill" / "scripts" / "promote_helper.py")


def _meta(draft_env, name="my-tool"):
    return json.loads((draft_env["drafts"] / "tanaka" / name / "_draft_meta.json").read_text(encoding="utf-8"))


def _set_meta(draft_env, **updates):
    path = draft_env["drafts"] / "tanaka" / "my-tool" / "_draft_meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(updates)
    path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def test_editing_returned_draft_resubmits_and_resets_tryouts(draft_env):
    """差し戻し後に直すと draft に戻り（再昇格できる）、合格回数の記録は0に戻る。"""
    _scaffold(draft_env)
    _set_meta(draft_env, status="returned", returned_reason="ケース不足", tryouts=[{"verdict": "pass"}])
    result = _run("write_draft_file.py", "--name", "my-tool", "--path", "references/a.md", "--content", "x", env=draft_env["env"])
    assert result.returncode == 0, result.stderr
    meta = _meta(draft_env)
    assert meta["status"] == "draft" and meta["tryouts"] == [] and "resubmitted_at" in meta


def test_editing_rejected_draft_stays_rejected(draft_env):
    _scaffold(draft_env)
    _set_meta(draft_env, status="rejected")
    _run("write_draft_file.py", "--name", "my-tool", "--path", "references/a.md", "--content", "x", env=draft_env["env"])
    assert _meta(draft_env)["status"] == "rejected"


def test_list_drafts_works_under_guard_in_listed_mode(draft_env, tmp_path):
    """listed では他人のドラフトの中身が読み取り禁止。ガード下でも一覧が落ちず、名前だけ出る。"""
    from src.tools._python_fs_guard import _python_fs_guard_preamble

    _scaffold(draft_env)
    other = draft_env["drafts"] / "suzuki" / "their-tool"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: their-tool\ndescription: 他人の。\n---\n", encoding="utf-8")
    guard_dir = tmp_path / "guard"
    guard_dir.mkdir()
    preamble = _python_fs_guard_preamble(
        [draft_env["drafts"] / "tanaka"], deny_roots=[(draft_env["drafts"] / "suzuki").resolve()], write_deny_roots=[]
    )
    (guard_dir / "sitecustomize.py").write_text(preamble, encoding="utf-8")
    env = {**draft_env["env"], "AGENT_OTHER_USERS_DRAFTS": "listed", "PYTHONPATH": str(guard_dir)}
    result = _run("list_drafts.py", env=env)
    assert result.returncode == 0, result.stderr
    drafts = json.loads(result.stdout)["drafts"]
    assert drafts[1] == {"draft": "suzuki/their-tool", "own": False, "readable": False}
    assert drafts[0]["draft"] == "tanaka/my-tool" and drafts[0]["readable"] is True


def test_tree_rules_match_between_evals_and_skill_creator(tmp_path):
    """evals/skill_tree.py と skill-creator の _common.py は同じハッシュ・複製規則を持つ。"""
    import shutil

    from evals import skill_tree

    common = _load_script_module("skill_creator_common_for_test", "_common.py")
    skill = tmp_path / "skill"
    for rel in ("SKILL.md", "references/evals/keep.md", "evals/001.yaml", "_draft_meta.json", "scripts/__pycache__/x.pyc", "scripts/run.py"):
        (skill / rel).parent.mkdir(parents=True, exist_ok=True)
        (skill / rel).write_text(rel, encoding="utf-8")
    assert skill_tree.tree_sha256(skill) == common.tree_sha256(skill)
    assert skill_tree.cases_sha256(skill / "evals") == common.cases_sha256(skill / "evals")
    for name, ignore in (("a", skill_tree.copy_ignore_for(skill)), ("b", common.copy_ignore_for(skill))):
        shutil.copytree(skill, tmp_path / name, ignore=ignore)
        copied = sorted(p.relative_to(tmp_path / name).as_posix() for p in (tmp_path / name).rglob("*") if p.is_file())
        # 下の階層の evals/ はスキル本体として残し、直下の evals/・来歴・キャッシュは除く
        assert copied == ["SKILL.md", "references/evals/keep.md", "scripts/run.py"]
        assert skill_tree.tree_sha256(tmp_path / name) == skill_tree.tree_sha256(skill)


def _tryout_for(draft: Path, repeat: int = 10, verdict: str = "pass") -> dict:
    from evals import skill_tree

    return {
        "repeat": repeat,
        "verdict": verdict,
        "case_files": sorted(p.stem for p in (draft / "evals").glob("*.yaml")),
        "cases_sha256": skill_tree.cases_sha256(draft / "evals"),
        "skill_overlays": [{"path": str(draft), "name": draft.name, "sha256": skill_tree.tree_sha256(draft)}],
    }


def _draft_with_case(draft_env) -> Path:
    _scaffold(draft_env)
    _run("make_eval_case.py", "--name", "my-tool", "--case-id", "001_basic", "--turns", '["テスト"]', "--judge", "x", env=draft_env["env"])
    return draft_env["drafts"] / "tanaka" / "my-tool"


def test_promote_accepts_only_tryout_of_current_draft(draft_env):
    """トライアウト後に1か所でも直したら合格回数は0扱い。回数不足・ケース不足・別スキルの結果も拒否する。"""
    helper = _load_promote_helper()
    draft = _draft_with_case(draft_env)
    tryout = _tryout_for(draft)
    assert helper._tryout_problems(draft, tryout, 10, judged_pass=False) == []
    assert helper._tryout_problems(draft, {**tryout, "repeat": 3}, 10, judged_pass=False)
    assert helper._tryout_problems(draft, {**tryout, "verdict": "needs_judge"}, 10, judged_pass=False)
    assert helper._tryout_problems(draft, {**tryout, "verdict": "needs_judge"}, 10, judged_pass=True) == []
    assert helper._tryout_problems(draft, {**tryout, "verdict": "fail"}, 10, judged_pass=True)
    assert helper._tryout_problems(draft, {**tryout, "skill_overlays": []}, 10, judged_pass=False)

    edit = ("write_draft_file.py", "--name", "my-tool", "--path", "SKILL.md", "--old", "# my-tool", "--new", "# 直した")
    assert _run(*edit, env=draft_env["env"]).returncode == 0
    assert any("ドラフトが変更" in p for p in helper._tryout_problems(draft, tryout, 10, judged_pass=False))

    tryout = _tryout_for(draft)
    _run("make_eval_case.py", "--name", "my-tool", "--case-id", "002_more", "--turns", '["もう1件"]', "--judge", "x", env=draft_env["env"])
    problems = helper._tryout_problems(draft, tryout, 10, judged_pass=False)
    assert any("全ケース" in p for p in problems) and any("ケースが変更" in p for p in problems)


def test_isolated_eval_status_ignores_results_of_changed_draft(draft_env, tmp_path):
    module = _load_script_module("run_isolated_eval_for_test", "run_isolated_eval.py")
    draft = _draft_with_case(draft_env)
    ref = _load_script_module("skill_creator_common_for_test2", "_common.py").DraftRef("tanaka", "my-tool", draft)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "tryout.json").write_text(json.dumps(_tryout_for(draft)), encoding="utf-8")
    assert module._is_stale(out_dir, ref, "with_skill") is False
    (draft / "references" / "new.md").write_text("x", encoding="utf-8")
    assert module._is_stale(out_dir, ref, "with_skill") is True
    assert module._is_stale(out_dir, ref, "without_skill") is False


def test_merged_override_value_keeps_existing_entries():
    import ast

    helper = _load_promote_helper()
    current = '[\n    ["Glob", 1],\n    [["my-tool", "run.py"], 0],\n]'
    value, added = helper._merged_value(current, [("my-tool", "run.py"), ("my-tool", "b.py")], allow_entries=True)
    assert ast.literal_eval(value) == [["Glob", 1], [["my-tool", "run.py"], 0], [["my-tool", "b.py"], -1]]
    assert added == [[["my-tool", "b.py"], -1]]
    value, added = helper._merged_value('[["x", "y.py"]]', [("my-tool", "run.py")], allow_entries=False)
    assert ast.literal_eval(value) == [["x", "y.py"], ["my-tool", "run.py"]]
    assert helper._merged_value('[["my-tool", "run.py"]]', [("my-tool", "run.py")], allow_entries=False) == (None, [])


def test_promote_registers_exemptions_in_instance_overrides(tmp_path, monkeypatch):
    """昇格時の登録は config_overrides.json に、config.ini の既定値を残したまま追記され、load_config() で効く。"""
    from src.config import load_config

    helper = _load_promote_helper()
    monkeypatch.setenv("INSTANCES_DIR", str(tmp_path / "instances"))
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "instances" / "inst1").mkdir(parents=True)
    result = helper._register_guard_exemptions("inst1", [("my-tool", "run.py")], actor="test")
    overrides_path = tmp_path / "instances" / "inst1" / "config_overrides.json"
    assert result["added"] and overrides_path.is_file()
    cfg = load_config(overrides_path=overrides_path, instance_name="inst1")
    assert ("my-tool", "run.py") in cfg.plan_approval_exempt_scripts
    assert (("my-tool", "run.py"), -1) in cfg.main_agent_tool_guard_allow_entries
    # config.ini の既定値も残る（上書きはキー単位で値全体を置き換えるため）
    assert ("Glob", 1) in cfg.main_agent_tool_guard_allow_entries
    assert ("excel-read", "read_excel.py") in cfg.plan_approval_exempt_scripts
    assert helper._register_guard_exemptions("inst1", [("my-tool", "run.py")], actor="test")["added"] == {}
    assert (tmp_path / "instances" / "admin_changes.log").is_file()


def test_promote_refuses_registration_when_instance_env_overrides_key(tmp_path, monkeypatch):
    helper = _load_promote_helper()
    monkeypatch.setenv("INSTANCES_DIR", str(tmp_path / "instances"))
    (tmp_path / "instances" / "inst1").mkdir(parents=True)
    (tmp_path / "instances" / "inst1" / ".env").write_text("PLAN_APPROVAL_EXEMPT_SCRIPTS=[]\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="PLAN_APPROVAL_EXEMPT_SCRIPTS"):
        helper._register_guard_exemptions("inst1", [("my-tool", "run.py")], actor="test")


def test_run_all_freezes_draft_during_tryout_and_always_writes_tryout(draft_env, tmp_path, monkeypatch):
    """--repeat の途中でドラフトを直しても全回が開始時の内容で評価され、tryout.json にそのハッシュが残る。"""
    from evals import skill_tree

    draft = _draft_with_case(draft_env)
    original_hash = skill_tree.tree_sha256(draft)
    run_all = _load_run_all()
    seen = []

    def fake_run_one(case_path, instance_name, extra_args):
        overlay = Path(extra_args[extra_args.index("--skill-overlay") + 1])
        seen.append((case_path.parent, skill_tree.tree_sha256(overlay)))
        # 評価中に作成者がドラフトを直した
        (draft / "references" / f"edit{len(seen)}.md").write_text("x", encoding="utf-8")
        return {"case_id": "001_basic", "rules_pass": True}

    monkeypatch.setattr(run_all, "_run_one", fake_run_one)
    monkeypatch.setattr(run_all, "resolve_instance_name", lambda _name: "default")
    results_dir = tmp_path / "results"
    argv = ["run_all.py", "--cases-dir", str(draft / "evals"), "--skill-overlay", str(draft), "--repeat", "3", "--results-dir", str(results_dir)]
    monkeypatch.setattr(sys, "argv", argv)
    assert run_all.main() == 0
    assert [h for _, h in seen] == [original_hash] * 3
    assert all(parent != draft / "evals" for parent, _ in seen)  # ケースも写しで実行する
    tryout = json.loads(next(results_dir.glob("*/tryout.json")).read_text(encoding="utf-8"))
    assert tryout["skill_overlays"][0]["sha256"] == original_hash and tryout["case_files"] == ["001_basic"]
    assert tryout["verdict"] == "pass" and tryout["repeat"] == 3

    # --repeat 1 でも tryout.json を書く
    monkeypatch.setattr(sys, "argv", [*argv[:5], "--repeat", "1", "--results-dir", str(tmp_path / "results1")])
    assert run_all.main() == 0
    assert list((tmp_path / "results1").glob("*/tryout.json"))


def test_run_case_overlay_scripts_are_evaluated_as_registered(tmp_path):
    """--skill-overlay のスクリプトは、昇格時に登録されるのと同じ免除設定で評価する。"""
    import dataclasses

    from evals.run_case import _with_overlay_guard_exemptions

    skill = tmp_path / "my-tool"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "run.py").write_text("", encoding="utf-8")
    (skill / "SKILL.md").write_text("---\nname: my-tool\ndescription: x\n---\n", encoding="utf-8")

    @dataclasses.dataclass(frozen=True)
    class _Cfg:
        plan_approval_exempt_scripts: frozenset
        main_agent_tool_guard_allow_entries: frozenset

    cfg = _with_overlay_guard_exemptions(_Cfg(frozenset(), frozenset({("Glob", 1)})), tmp_path)
    assert cfg.plan_approval_exempt_scripts == {("my-tool", "run.py")}
    assert cfg.main_agent_tool_guard_allow_entries == {("Glob", 1), (("my-tool", "run.py"), -1)}


def test_trigger_eval_rejects_malformed_eval_set(draft_env, tmp_path):
    _scaffold(draft_env)
    bad = tmp_path / "bad.json"
    bad.write_text('[{"q": "x"}]', encoding="utf-8")
    result = _run("run_trigger_eval.py", "start", "--name", "my-tool", "--eval-set", str(bad), env=draft_env["env"])
    assert result.returncode == 1 and "should_trigger" in result.stderr and "Traceback" not in result.stderr
