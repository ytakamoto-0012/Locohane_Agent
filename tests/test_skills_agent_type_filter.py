"""filter_skills_for_agent_type() の回帰テスト。

サブエージェントの {{skills}} は [subagent].agent_type_run_script_allowlist に
その agent_type の登録があれば登録スキルだけに絞り、登録が無ければ全件を返す。
"""

from pathlib import Path

from src.skills import Skill, filter_skills_for_agent_type


def _skill(name: str) -> Skill:
    return Skill(name=name, description=f"{name} desc", dir_path=Path(name), skill_md_path=Path(name) / "SKILL.md")


def test_no_entries_for_agent_type_returns_all() -> None:
    skills = [_skill("a"), _skill("b")]
    entries = frozenset({("verifier", "a")})
    assert filter_skills_for_agent_type(skills, "explore", entries) == skills


def test_filters_by_skill_name_and_script_pair() -> None:
    skills = [_skill("docx-read"), _skill("pdf-tools"), _skill("web-search")]
    entries = frozenset(
        {
            ("explore", "docx-read"),
            ("explore", ("pdf-tools", "render_pdf_pages.py")),
            ("verifier", "web-search"),
        }
    )
    result = filter_skills_for_agent_type(skills, "explore", entries)
    assert [s.name for s in result] == ["docx-read", "pdf-tools"]
