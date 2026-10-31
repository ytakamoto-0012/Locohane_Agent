"""filter_skills_for_main_agent_guard() / render_skills_block_with_hint() の回帰テスト。

main_agent_tool_guard 有効時、[skill_name, script_filename] ペアが
max_calls≠0 で許可されていないスキルは、メインエージェントの {{skills}} から
除外する（run_script/run_script_backgroundを直接呼んでも常に拒否されるため、
見せたままだと無駄な往復が発生する。src/tools/tool_node.py の
filter_main_agent_tools と同じ理由づけ）。scripts/ を持たない SKILL.mdのみの
スキル（has_scripts=False）も同じホワイトリスト方式で、script_filenameを
空文字列にした [skill_name, ""] のダミーエントリを登録しない限り一覧から
除外される（run_script自体の対象は無いため実行許可には影響しない）。
"""

from pathlib import Path
from types import SimpleNamespace

from src.skills import (
    Skill,
    filter_skills_for_main_agent_guard,
    render_skills_block_with_guard_annotation,
    render_skills_block_with_hint,
)


def _make_skill(name: str, *, has_scripts: bool = True) -> Skill:
    return Skill(
        name=name,
        description="test skill",
        dir_path=Path(f"skills/{name}"),
        skill_md_path=Path(f"skills/{name}/SKILL.md"),
        has_scripts=has_scripts,
    )


def _make_cfg(*, entries, mode: str = "all"):
    return SimpleNamespace(
        main_agent_tool_guard_mode=mode,
        main_agent_tool_guard_allow_entries=frozenset(entries),
    )


def test_guard_disabled_keeps_all_skills() -> None:
    skills = [_make_skill("pdf-tools"), _make_skill("web-search")]
    cfg = _make_cfg(entries=[], mode="false")

    assert filter_skills_for_main_agent_guard(skills, cfg) == skills


def test_guard_tools_skills_only_mode_filters_same_as_all() -> None:
    """mode=tools_skills_only はMCP動的ツールのみに関わる設定で、スキル自体の
    フィルタ判定は mode=all と同じになる。"""
    skills = [_make_skill("pdf-tools"), _make_skill("web-search")]
    cfg = _make_cfg(entries=[(("pdf-tools", "render_pdf_pages.py"), -1)], mode="tools_skills_only")

    result = filter_skills_for_main_agent_guard(skills, cfg)

    assert [s.name for s in result] == ["pdf-tools"]


def test_skill_without_allowed_pair_is_dropped() -> None:
    skills = [_make_skill("pdf-tools"), _make_skill("web-search")]
    cfg = _make_cfg(entries=[("Glob", 1), (("pdf-tools", "render_pdf_pages.py"), 0)])

    result = filter_skills_for_main_agent_guard(skills, cfg)

    assert [s.name for s in result] == []


def test_skill_with_allowed_pair_is_kept() -> None:
    skills = [_make_skill("pdf-tools"), _make_skill("web-search")]
    cfg = _make_cfg(entries=[(("pdf-tools", "render_pdf_pages.py"), -1)])

    result = filter_skills_for_main_agent_guard(skills, cfg)

    assert [s.name for s in result] == ["pdf-tools"]


def test_skill_without_scripts_and_without_dummy_entry_is_dropped() -> None:
    """scripts/を持たないSKILL.mdのみのスキルも、ダミーエントリ未登録なら他と同様に除外する。"""
    skills = [_make_skill("pdf-tools"), _make_skill("excel-knowledge", has_scripts=False)]
    cfg = _make_cfg(entries=[("Glob", 1)])

    result = filter_skills_for_main_agent_guard(skills, cfg)

    assert [s.name for s in result] == []


def test_skill_without_scripts_with_dummy_entry_is_kept() -> None:
    """[skill_name, ""] のダミーエントリを登録すれば、scripts/を持たないスキルも一覧に残る。"""
    skills = [_make_skill("pdf-tools"), _make_skill("excel-knowledge", has_scripts=False)]
    cfg = _make_cfg(entries=[(("excel-knowledge", ""), -1)])

    result = filter_skills_for_main_agent_guard(skills, cfg)

    assert [s.name for s in result] == ["excel-knowledge"]


def test_render_skills_block_with_hint_annotates_only_blocked_skills() -> None:
    skills = [
        _make_skill("pdf-tools"),
        _make_skill("excel-knowledge", has_scripts=False),
        _make_skill("web-search"),
    ]
    cfg = _make_cfg(
        entries=[
            (("pdf-tools", "render_pdf_pages.py"), -1),
            (("excel-knowledge", ""), -1),
        ]
    )

    block = render_skills_block_with_hint(skills, cfg)
    lines = block.splitlines()

    assert lines[0] == "- pdf-tools: test skill"
    assert lines[1] == "- excel-knowledge: test skill"
    assert lines[2] == "- web-search: cannot be run directly. Delegate checking details of and running this skill to dispatch_agent"


def test_render_skills_block_with_hint_empty_list() -> None:
    cfg = _make_cfg(entries=[])

    assert render_skills_block_with_hint([], cfg) == "(no skills available)"


def test_render_skills_block_with_guard_annotation_appends_note_to_blocked_skills() -> None:
    skills = [
        _make_skill("pdf-tools"),
        _make_skill("excel-knowledge", has_scripts=False),
        _make_skill("web-search"),
    ]
    cfg = _make_cfg(
        entries=[
            (("pdf-tools", "render_pdf_pages.py"), -1),
            (("excel-knowledge", ""), -1),
        ]
    )

    block = render_skills_block_with_guard_annotation(skills, cfg)
    lines = block.splitlines()

    assert lines[0] == "- pdf-tools: test skill"
    assert lines[1] == "- excel-knowledge: test skill"
    assert lines[2] == "- web-search: test skill (cannot be run directly; delegate running it to dispatch_agent.)"


def test_render_skills_block_with_guard_annotation_empty_list() -> None:
    cfg = _make_cfg(entries=[])

    assert render_skills_block_with_guard_annotation([], cfg) == "(no skills available)"
