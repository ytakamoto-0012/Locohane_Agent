"""MCP サーバーのツール（mcp_server/skill_tools.py）の戻り値・説明文のテスト。

MCP クライアントの LLM に渡る文言は、本体のツールと同じく英語（`Error:`・`[exit code]`・`[stdout]`・`[stderr]`）。
"""

from __future__ import annotations

import re

import pytest

from mcp_server import skill_tools

_JAPANESE = re.compile(r"[぀-ヿ㐀-鿿]")


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    skill = tmp_path / "demo-skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Demo.\n---\n\n# Demo\n", encoding="utf-8")
    (skill / "scripts" / "hello.py").write_text(
        "import sys\nprint('hello')\nprint('oops', file=sys.stderr)\nsys.exit(3)\n", encoding="utf-8"
    )
    monkeypatch.setattr(skill_tools, "SKILLS_SRC", (tmp_path,))
    monkeypatch.setattr(skill_tools, "WORKDIR", None)
    return tmp_path


def test_tool_descriptions_are_english() -> None:
    for tool in (skill_tools.list_skills, skill_tools.read_skill, skill_tools.run_skill_script):
        assert tool.__doc__ and not _JAPANESE.search(tool.__doc__), tool.__name__


def test_run_skill_script_uses_english_headings(skills_root) -> None:
    result = skill_tools.run_skill_script("demo-skill", "hello.py")
    assert result == "[exit code] 3\n[stdout]\nhello\n[stderr]\noops"


def test_run_skill_script_keeps_japanese_output(skills_root, monkeypatch) -> None:
    """子の標準出力を UTF-8 にそろえる（日本語版 Windows では cp932 で書かれ、置換文字に化けていた）。"""
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    (skills_root / "demo-skill" / "scripts" / "jp.py").write_text("print('日本語の出力')\n", encoding="utf-8")
    assert skill_tools.run_skill_script("demo-skill", "jp.py") == "[exit code] 0\n[stdout]\n日本語の出力"


def test_errors_start_with_error_prefix(skills_root) -> None:
    assert skill_tools.read_skill("missing").startswith("Error: ")
    assert skill_tools.read_skill("../outside").startswith("Error: ")
    for result in (
        skill_tools.run_skill_script("demo-skill", "missing.py"),
        skill_tools.run_skill_script("missing", "hello.py"),
    ):
        assert result.startswith("Error: ") and not _JAPANESE.search(result), result


def test_parse_frontmatter_keeps_triple_dash_in_description() -> None:
    """MCP サーバー側も、行全体が --- の行だけを区切りにする（本体の src/skills.py と同じ）。"""
    text = "---\nname: monthly-check\ndescription: Monthly check --- use when closing\n---\n\n# Body\n"
    assert skill_tools._parse_frontmatter(text) == {"name": "monthly-check", "description": "Monthly check --- use when closing"}


def test_list_skills_skips_skills_whose_name_differs_from_the_folder(skills_root) -> None:
    """read_skill はフォルダ名で引くため、name がフォルダ名と違うスキルは一覧に載せない（載せても呼べない）。"""
    other = skills_root / "folder-name"
    other.mkdir()
    (other / "SKILL.md").write_text("---\nname: other-name\ndescription: Mismatch.\n---\n", encoding="utf-8")
    listed = skill_tools.list_skills()
    assert "demo-skill: Demo." in listed
    assert "other-name" not in listed and "folder-name" not in listed
