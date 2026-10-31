"""ユーザー別ドラフトスキル（src/skill_drafts.py と src/tools への組み込み）の回帰テスト。

ドラフトは作成者本人の会話でだけ使え、他ユーザーへの見え方は
[skill_creator].other_users_drafts（hidden/listed/readable/full）で切り替わる。
ドラフトのスクリプトと skills_dir 配下の skill-creator 本体は、計画承認と
[main_agent_tool_guard] を無条件で免除される。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import skill_drafts, tools
from src.config import _validate_skill_draft_dir
from src.tools._safe_path import _is_guard_exempt_script, _resolve_file_tools_path, _safe_path
from src.tools.read_skill import session_read_skill

_SKILL_MD = "---\nname: {name}\ndescription: {name} のテスト用スキル。\n---\n\n# {name}\n"


class _FakeUserSession:
    def __init__(self, data):
        self._data = dict(data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def _make_skill(root: Path, name: str, with_script: bool = True) -> Path:
    skill_dir = root / name
    (skill_dir / "scripts").mkdir(parents=True) if with_script else skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD.format(name=name), encoding="utf-8")
    if with_script:
        (skill_dir / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    return skill_dir


@pytest.fixture
def env(tmp_path, monkeypatch):
    """skills/・.locohane/skills/・ドラフト置き場を持つ最小構成を tools の状態へ入れる。"""
    skills_dir = tmp_path / "skills"
    locohane_skills = tmp_path / ".locohane" / "skills"
    draft_dir = tmp_path / "data" / "skill_drafts"
    _make_skill(skills_dir, "official")
    _make_skill(skills_dir, "skill-creator")
    _make_skill(locohane_skills, "team-skill")
    _make_skill(draft_dir / "tanaka", "my-tool")
    _make_skill(draft_dir / "suzuki", "their-tool")
    (draft_dir / "tanaka" / "_workspace").mkdir()
    config = SimpleNamespace(
        skills_dir=skills_dir,
        agents_dir=tmp_path / "agents",
        project_locohane_dirs=[tmp_path / ".locohane"],
        skill_draft_dir=draft_dir,
        skill_other_users_drafts="hidden",
        main_agent_tool_guard_mode="all",
        main_agent_tool_guard_allow_entries=frozenset(),
        bin_path=[],
    )
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", config)
    monkeypatch.setattr(tools._state, "_SKILLS_ROOTS", [locohane_skills.resolve(), skills_dir.resolve(), draft_dir.resolve()])
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", None)
    default_workdir = tmp_path / "workdir"
    default_workdir.mkdir()
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", default_workdir)
    session = _FakeUserSession({"thread_id": "t1", skill_drafts.SESSION_USER_KEY: "tanaka"})
    monkeypatch.setattr(tools.cl, "user_session", session)
    return SimpleNamespace(config=config, draft_dir=draft_dir, skills_dir=skills_dir, locohane_skills=locohane_skills, session=session)


# --- 設定 -------------------------------------------------------------------


@pytest.mark.parametrize("relation", ["same", "inside", "contains"])
def test_draft_dir_overlapping_scan_roots_is_rejected(tmp_path, relation):
    skills_dir = tmp_path / "skills"
    draft = {"same": skills_dir, "inside": skills_dir / "drafts", "contains": tmp_path}[relation]
    cfg = SimpleNamespace(
        skill_draft_dir=draft,
        skill_archive_dir=tmp_path / "y" / "archive",
        skills_dir=skills_dir,
        agents_dir=tmp_path / "x" / "agents",
        project_locohane_dirs=[],
        instance_locohane_dir=tmp_path / "x" / "instance",
    )
    with pytest.raises(ValueError, match="draft_dir"):
        _validate_skill_draft_dir(cfg)


def _scan_layout(tmp_path, **overrides):
    values = {
        "skill_draft_dir": tmp_path / "data" / "skill_drafts",
        "skill_archive_dir": tmp_path / "data" / "skill_drafts_archive",
        "skills_dir": tmp_path / "skills",
        "agents_dir": tmp_path / "agents",
        "project_locohane_dirs": [tmp_path / ".locohane"],
        "instance_locohane_dir": tmp_path / "instances" / "default",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_draft_dir_outside_scan_roots_is_accepted(tmp_path):
    _validate_skill_draft_dir(_scan_layout(tmp_path))


def test_draft_dir_inside_instance_locohane_dir_is_rejected(tmp_path):
    """インスタンス専用の置き場もスキル走査対象のため、ドラフト置き場と重ねられない。"""
    layout = _scan_layout(tmp_path, skill_draft_dir=tmp_path / "instances" / "default" / "drafts")
    with pytest.raises(ValueError, match="instance_locohane_dir"):
        _validate_skill_draft_dir(layout)


@pytest.mark.parametrize("where", ["skills", "instance", "draft"])
def test_archive_dir_overlapping_is_rejected(tmp_path, where):
    """アーカイブは走査対象とも作成者のドラフト置き場とも重ねられない（一覧に戻ってしまうため）。"""
    archive = {
        "skills": tmp_path / "skills" / "archive",
        "instance": tmp_path / "instances" / "default" / "archive",
        "draft": tmp_path / "data" / "skill_drafts" / "_archive",
    }[where]
    with pytest.raises(ValueError, match="archive_dir"):
        _validate_skill_draft_dir(_scan_layout(tmp_path, skill_archive_dir=archive))


# --- 権限表 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("hidden", set()),
        ("listed", {"list"}),
        ("readable", {"list", "read"}),
        ("full", {"list", "read", "exec", "write"}),
    ],
)
def test_other_user_permissions_follow_mode(mode, expected):
    for op in ("list", "read", "exec", "write"):
        assert skill_drafts.is_allowed("suzuki", "tanaka", mode, op) is (op in expected)
        assert skill_drafts.is_allowed("tanaka", "tanaka", mode, op) is True


def test_unknown_user_is_never_owner():
    assert skill_drafts.is_allowed("tanaka", None, "hidden", "read") is False


# --- 一覧 -------------------------------------------------------------------


def test_hidden_mode_lists_only_own_drafts(env):
    drafts = skill_drafts.scan_visible_drafts(env.config, "tanaka")
    assert [d.skill.name for d in drafts] == ["tanaka/my-tool"]
    assert drafts[0].own and drafts[0].readable and drafts[0].executable


@pytest.mark.parametrize(("mode", "readable", "executable"), [("listed", False, False), ("readable", True, False), ("full", True, True)])
def test_other_drafts_listed_by_mode(env, mode, readable, executable):
    env.config.skill_other_users_drafts = mode
    drafts = {d.skill.name: d for d in skill_drafts.scan_visible_drafts(env.config, "tanaka")}
    assert set(drafts) == {"tanaka/my-tool", "suzuki/their-tool"}
    other = drafts["suzuki/their-tool"]
    assert (other.readable, other.executable) == (readable, executable)


def test_no_session_user_sees_no_drafts(env):
    assert skill_drafts.scan_visible_drafts(env.config, None) == []


def test_render_block_marks_owner_and_usability(env):
    env.config.skill_other_users_drafts = "listed"
    block = skill_drafts.render_draft_skills_block(skill_drafts.scan_visible_drafts(env.config, "tanaka"))
    assert "tanaka/my-tool" in block and "your own draft" in block
    assert "suzuki/their-tool" in block and "not usable" in block


# --- パス解決 ---------------------------------------------------------------


def test_safe_path_resolves_own_draft(env):
    path = _safe_path("tanaka/my-tool/SKILL.md")
    assert path == (env.draft_dir / "tanaka" / "my-tool" / "SKILL.md").resolve()


def test_safe_path_rejects_other_users_draft_when_hidden(env):
    with pytest.raises(ValueError, match="suzuki"):
        _safe_path("suzuki/their-tool/SKILL.md")


def test_safe_path_read_vs_exec_in_readable_mode(env):
    env.config.skill_other_users_drafts = "readable"
    assert _safe_path("suzuki/their-tool/SKILL.md").is_file()
    with pytest.raises(ValueError, match="run"):
        _safe_path("suzuki/their-tool/scripts", op="exec")


def test_official_skills_take_priority_over_drafts(env):
    assert _safe_path("official/SKILL.md") == (env.skills_dir / "official" / "SKILL.md").resolve()


def test_read_tool_cannot_open_other_users_draft(env):
    path, error = _resolve_file_tools_path(str(env.draft_dir / "suzuki" / "their-tool" / "SKILL.md"))
    assert path is None and "suzuki" in error
    own, own_error = _resolve_file_tools_path(str(env.draft_dir / "tanaka" / "my-tool" / "SKILL.md"))
    assert own is not None and own_error is None


def test_draft_root_listing_is_hidden_unless_readable(env):
    _, error = _resolve_file_tools_path(str(env.draft_dir))
    assert error is not None
    env.config.skill_other_users_drafts = "readable"
    assert _resolve_file_tools_path(str(env.draft_dir))[1] is None


# --- 承認・ガードの免除 ------------------------------------------------------


def test_guard_exempt_for_own_draft_and_builtin_skill_creator(env):
    assert _is_guard_exempt_script("tanaka/my-tool", "run.py") is True
    assert _is_guard_exempt_script("skill-creator", "run.py") is True


def test_guard_not_exempt_for_official_or_unrunnable_drafts(env):
    assert _is_guard_exempt_script("official", "run.py") is False
    assert _is_guard_exempt_script("suzuki/their-tool", "run.py") is False
    env.config.skill_other_users_drafts = "full"
    assert _is_guard_exempt_script("suzuki/their-tool", "run.py") is True


def test_same_named_skill_creator_outside_skills_dir_is_not_exempt(env, monkeypatch):
    shutil.rmtree(env.skills_dir / "skill-creator")
    _make_skill(env.locohane_skills, "skill-creator")
    assert _is_guard_exempt_script("skill-creator", "run.py") is False


def test_main_agent_guard_lets_draft_scripts_through(env):
    from src.tools.tool_node import _guard_main_agent_tool_limit

    def call(skill):
        tool_call = {"name": "run_script", "args": {"skill_name": skill, "script_filename": "run.py"}, "id": "c1", "type": "tool_call"}
        return {"__type": "tool_call_with_context", "tool_call": tool_call, "state": {}}

    assert _guard_main_agent_tool_limit(call("tanaka/my-tool")) is None
    blocked = _guard_main_agent_tool_limit(call("official"))
    assert blocked is not None and "is not allowed" in blocked["messages"][0].content


# --- skill-creator の書き込み許可と読み取り禁止ガード -------------------------


def test_skill_creator_may_write_only_own_draft_folder(env):
    from src.tools._subprocess_env import _skill_creator_draft_roots

    assert _skill_creator_draft_roots("skill-creator") == [(env.draft_dir / "tanaka").resolve()]
    assert _skill_creator_draft_roots("official") == []
    env.config.skill_other_users_drafts = "full"
    assert _skill_creator_draft_roots("skill-creator") == [env.draft_dir.resolve()]


def test_subprocess_cannot_read_other_users_draft(env, tmp_path):
    from src.tools._subprocess_env import _run_script_guard_env

    workdir = tmp_path / "work"
    workdir.mkdir()
    guard_env, guard_dir = _run_script_guard_env(workdir, "official", "run.py")
    script = workdir / "peek.py"
    other = env.draft_dir / "suzuki" / "their-tool" / "SKILL.md"
    own = env.draft_dir / "tanaka" / "my-tool" / "SKILL.md"
    script.write_text(
        f"try:\n    open(r'{other}', encoding='utf-8').read()\n    print('OTHER_READ')\nexcept PermissionError:\n    print('OTHER_BLOCKED')\n"
        f"open(r'{own}', encoding='utf-8').read()\nprint('OWN_READ')\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run([sys.executable, str(script)], cwd=str(workdir), capture_output=True, text=True, env=guard_env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)
    assert "OTHER_BLOCKED" in result.stdout and "OWN_READ" in result.stdout, result.stderr


def test_subprocess_env_carries_draft_context(env):
    from src.tools._subprocess_env import _subprocess_env

    sub_env = _subprocess_env()
    assert sub_env["AGENT_USER"] == "tanaka"
    assert Path(sub_env["AGENT_SKILL_DRAFT_DIR"]) == env.draft_dir
    roots = sub_env["AGENT_SKILL_ROOTS"].split(os.pathsep)
    assert str(env.draft_dir.resolve()) not in roots and str(env.skills_dir.resolve()) in roots


# --- セッション用 read_skill -------------------------------------------------


def test_session_read_skill_adds_draft_names_without_touching_shared_tool(monkeypatch):
    from langchain_core.utils.function_calling import convert_to_openai_tool

    read_skill_module = sys.modules["src.tools.read_skill"]

    monkeypatch.setattr(read_skill_module, "_BASE_SKILL_NAMES", ("official",))
    copy = session_read_skill(["tanaka/my-tool"])
    enum = convert_to_openai_tool(copy)["function"]["parameters"]["properties"]["skill_name"]["enum"]
    assert enum == ["official", "tanaka/my-tool"]
    assert session_read_skill([]) is read_skill_module.read_skill


# --- 2026-10-10 レビュー指摘の修正 ---------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("Tanaka", "tanaka"),
        ("alice.", "alice"),
        ("bob . ", "bob"),
        ("..", "u-"),
        (".", "u-"),
        ("_workspace", "u-_workspace"),
        ("CON", "u-con"),
        ("nul.txt", "u-nul.txt"),
        ("a/b", "a_b"),
        (None, "anonymous"),
    ],
)
def test_draft_owner_name_never_points_outside_own_folder(identifier, expected):
    """`..` やWindowsで別名になる形（末尾ドット・大文字小文字・予約名）をフォルダ名にしない。"""
    assert skill_drafts.draft_owner_name(identifier) == expected


def test_unwritable_dirs_cover_other_users_unless_full(env):
    other = (env.draft_dir / "suzuki").resolve()
    for mode in ("hidden", "listed", "readable"):
        env.config.skill_other_users_drafts = mode
        assert skill_drafts.unwritable_dirs(env.config, "tanaka") == [other]
    env.config.skill_other_users_drafts = "full"
    assert skill_drafts.unwritable_dirs(env.config, "tanaka") == []


def _run_guarded(script_body: str, workdir: Path, allowed: list[Path]) -> subprocess.CompletedProcess:
    from src.tools._python_fs_guard import _python_fs_guard_preamble

    script = workdir / "probe.py"
    script.write_text(_python_fs_guard_preamble(allowed) + script_body, encoding="utf-8")
    return subprocess.run([sys.executable, str(script)], cwd=str(workdir), capture_output=True, text=True, encoding="utf-8")


def test_readable_mode_blocks_writing_other_users_draft_even_inside_work_dir(env, tmp_path):
    """作業ディレクトリが draft_dir を含んでいても、readable では他人のドラフトへ書けない（自分のには書ける）。"""
    env.config.skill_other_users_drafts = "readable"
    other = env.draft_dir / "suzuki" / "their-tool" / "notes.md"
    own = env.draft_dir / "tanaka" / "my-tool" / "notes.md"
    body = (
        f"open(r'{other.parent / 'SKILL.md'}', encoding='utf-8').read()\nprint('OTHER_READ')\n"
        f"try:\n    open(r'{other}', 'w').write('x')\n    print('OTHER_WRITTEN')\nexcept PermissionError:\n    print('OTHER_BLOCKED')\n"
        f"open(r'{own}', 'w').write('x')\nprint('OWN_WRITTEN')\n"
    )
    result = _run_guarded(body, tmp_path, [env.draft_dir])
    assert "OTHER_READ" in result.stdout and "OTHER_BLOCKED" in result.stdout and "OWN_WRITTEN" in result.stdout, result.stderr
    assert not other.exists()


def test_hidden_mode_blocks_listing_and_low_level_open(env, tmp_path):
    """open() 以外の経路（os.listdir・os.walk・Path.iterdir・os.open）でも他人のドラフトを覗けない。"""
    other_dir = env.draft_dir / "suzuki" / "their-tool"
    body = (
        "import os, pathlib\n"
        "def probe(label, fn):\n"
        "    try:\n        fn()\n        print(label + '_LEAKED')\n"
        "    except PermissionError:\n        print(label + '_BLOCKED')\n"
        f"probe('LISTDIR', lambda: os.listdir(r'{other_dir}'))\n"
        f"probe('ITERDIR', lambda: list(pathlib.Path(r'{other_dir}').iterdir()))\n"
        f"probe('OSOPEN', lambda: os.open(r'{other_dir / 'SKILL.md'}', os.O_RDONLY))\n"
        f"walked = [d for d, _, _ in os.walk(r'{env.draft_dir}')]\n"
        f"print('WALK_LEAKED' if any('their-tool' in d for d in walked) else 'WALK_BLOCKED')\n"
        f"print('OWN_LIST', os.listdir(r'{env.draft_dir / 'tanaka' / 'my-tool'}'))\n"
    )
    result = _run_guarded(body, tmp_path, [tmp_path])
    out = result.stdout
    assert "LEAKED" not in out, out + result.stderr
    assert all(f"{k}_BLOCKED" in out for k in ("LISTDIR", "ITERDIR", "OSOPEN", "WALK")), out + result.stderr
    assert "SKILL.md" in out


def test_listed_mode_lists_skill_names_but_not_their_files(env, tmp_path):
    """listed は他ユーザーのスキル名までは一覧できるが、その中のファイル名は見えない。"""
    env.config.skill_other_users_drafts = "listed"
    owner_dir = env.draft_dir / "suzuki"
    body = (
        "import os\n"
        f"print('NAMES', os.listdir(r'{owner_dir}'))\n"
        f"try:\n    os.listdir(r'{owner_dir / 'their-tool'}')\n    print('FILES_LEAKED')\nexcept PermissionError:\n    print('FILES_BLOCKED')\n"
    )
    result = _run_guarded(body, tmp_path, [tmp_path])
    assert "their-tool" in result.stdout and "FILES_BLOCKED" in result.stdout, result.stdout + result.stderr


def test_main_agent_run_script_is_limited_to_directly_runnable_skills(env):
    """guard 有効時、run_script は skill-creator・自分のドラフト・allow_entries 登録分だけを選択肢に持つ。"""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from src.tools.tool_node import filter_main_agent_tools

    def enum_of(result, name):
        tool = next(t for t in result if t.name == name)
        return convert_to_openai_tool(tool)["function"]["parameters"]["properties"]["skill_name"]["enum"]

    result = filter_main_agent_tools([tools.run_script, tools.run_script_background], env.config)
    assert enum_of(result, "run_script") == ["skill-creator", "tanaka/my-tool"]
    assert enum_of(result, "run_script_background") == ["skill-creator", "tanaka/my-tool"]
    # 共有のツール本体（サブエージェントも使う）は書き換えない
    assert "enum" not in convert_to_openai_tool(tools.run_script)["function"]["parameters"]["properties"]["skill_name"]

    env.config.main_agent_tool_guard_allow_entries = frozenset({(("official", "run.py"), -1)})
    assert enum_of(filter_main_agent_tools([tools.run_script], env.config), "run_script") == ["official", "skill-creator", "tanaka/my-tool"]


def test_run_script_not_bound_when_nothing_is_directly_runnable(env):
    from src.tools.tool_node import filter_main_agent_tools

    shutil.rmtree(env.skills_dir / "skill-creator")
    shutil.rmtree(env.draft_dir / "tanaka" / "my-tool" / "scripts")
    assert filter_main_agent_tools([tools.run_script], env.config) == []


def test_drafts_signature_changes_when_drafts_change(env):
    before = skill_drafts.drafts_signature(skill_drafts.scan_visible_drafts(env.config, "tanaka"))
    _make_skill(env.draft_dir / "tanaka", "new-tool", with_script=False)
    after = skill_drafts.drafts_signature(skill_drafts.scan_visible_drafts(env.config, "tanaka"))
    assert before != after


def test_guard_exempt_entries_skip_private_modules_and_keep_explicit_settings(tmp_path):
    skill_dir = _make_skill(tmp_path, "my-tool")
    (skill_dir / "scripts" / "_common.py").write_text("", encoding="utf-8")
    (skill_dir / "scripts" / "second.py").write_text("", encoding="utf-8")
    entries = skill_drafts.guard_exempt_entries_for_skill("my-tool", skill_dir)
    assert entries == [("my-tool", "run.py"), ("my-tool", "second.py")]

    plan, allow = skill_drafts.merge_guard_exempt_entries(
        frozenset({("other", "x.py")}), frozenset({("Glob", 1), (("my-tool", "second.py"), 0)}), entries
    )
    assert plan == {("other", "x.py"), ("my-tool", "run.py"), ("my-tool", "second.py")}
    # 明示的に禁止（0）しているものは上書きしない
    assert allow == {("Glob", 1), (("my-tool", "second.py"), 0), (("my-tool", "run.py"), -1)}
