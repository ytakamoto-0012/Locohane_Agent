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


def test_errors_start_with_error_prefix(skills_root) -> None:
    assert skill_tools.read_skill("missing").startswith("Error: ")
    assert skill_tools.read_skill("../outside").startswith("Error: ")
    for result in (
        skill_tools.run_skill_script("demo-skill", "missing.py"),
        skill_tools.run_skill_script("missing", "hello.py"),
    ):
        assert result.startswith("Error: ") and not _JAPANESE.search(result), result
