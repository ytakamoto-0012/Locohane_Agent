"""visibility_mode=strict のときのスキル選定の委譲（メインの指示と explore の {{skill_catalog}}）の回帰テスト。

strict では直接扱えないスキルをメインの {{skills}} から外す。外したものがあるときだけ、
メインには「explore に目録から選ばせる」指示を、explore には外したスキルの目録を入れる。
all / hint / mode=false / 外したスキルが無いときは、どちらも入れない（両方が同じ条件で出入りする）。
"""

from pathlib import Path
from types import SimpleNamespace

from src.skills import Skill
from src.system_prompt_builder import main_skills_block, render_skill_catalog_block, strict_hidden_skills


def _skill(name: str) -> Skill:
    return Skill(
        name=name,
        description=f"{name} description",
        dir_path=Path(f"skills/{name}"),
        skill_md_path=Path(f"skills/{name}/SKILL.md"),
        has_scripts=True,
    )


def _cfg(*, visibility: str = "strict", mode: str = "all", entries=((("pdf-tools", "read_pdf.py"), -1),)):
    return SimpleNamespace(
        main_agent_tool_guard_mode=mode,
        main_agent_tool_guard_visibility_mode=visibility,
        main_agent_tool_guard_allow_entries=frozenset(entries),
    )


SKILLS = [_skill("pdf-tools"), _skill("excel-create"), _skill("web-search")]


def test_strict_main_block_lists_only_runnable_and_adds_delegation_note() -> None:
    block, hint = main_skills_block(SKILLS, _cfg())

    assert "- pdf-tools: pdf-tools description" in block
    assert "excel-create" not in block and "web-search" not in block
    assert "2 more skills exist" in block
    assert "`explore`" in block and "`worker`" in block
    assert hint == ""


def test_strict_catalog_lists_only_hidden_skills() -> None:
    catalog = render_skill_catalog_block(SKILLS, _cfg())

    assert catalog.startswith("### Skill catalog")
    assert "- excel-create: excel-create description" in catalog
    assert "- web-search: web-search description" in catalog
    assert "pdf-tools" not in catalog


def test_strict_with_no_runnable_skill_says_so() -> None:
    block, _ = main_skills_block(SKILLS, _cfg(entries=()))

    assert block.startswith("(no skills you can run directly)")
    assert "3 more skills exist" in block


def test_no_hidden_skills_means_no_note_and_no_catalog() -> None:
    entries = [((s.name, "x.py"), -1) for s in SKILLS]
    cfg = _cfg(entries=entries)

    block, _ = main_skills_block(SKILLS, cfg)
    assert "more skills exist" not in block
    assert render_skill_catalog_block(SKILLS, cfg) == ""


def test_guard_disabled_means_no_note_and_no_catalog() -> None:
    cfg = _cfg(mode="false")

    assert strict_hidden_skills(SKILLS, cfg) == []
    assert "more skills exist" not in main_skills_block(SKILLS, cfg)[0]
    assert render_skill_catalog_block(SKILLS, cfg) == ""


def test_hint_and_all_have_no_catalog() -> None:
    for visibility in ("hint", "all"):
        cfg = _cfg(visibility=visibility)
        assert strict_hidden_skills(SKILLS, cfg) == []
        assert render_skill_catalog_block(SKILLS, cfg) == ""


def test_explore_template_has_skill_catalog_placeholder() -> None:
    explore_md = Path(__file__).resolve().parent.parent / "agents" / "explore.md"

    assert "{{skill_catalog}}" in explore_md.read_text(encoding="utf-8")
