"""_as_main_agent_tool_guard_mode() の回帰テスト。

[main_agent_tool_guard].mode は false/tools_skills_only/all の3値のみを
許容する（既定は all、空欄・None も all として扱う）。src/config.py の
_as_main_agent_tool_guard_visibility_mode() と同じ検証パターン。
"""

import pytest

from src.config import _as_main_agent_tool_guard_mode


def test_defaults_to_all_when_empty_or_none() -> None:
    assert _as_main_agent_tool_guard_mode(None) == "all"
    assert _as_main_agent_tool_guard_mode("") == "all"
    assert _as_main_agent_tool_guard_mode("   ") == "all"


def test_accepts_false_tools_skills_only_and_all() -> None:
    assert _as_main_agent_tool_guard_mode("false") == "false"
    assert _as_main_agent_tool_guard_mode("tools_skills_only") == "tools_skills_only"
    assert _as_main_agent_tool_guard_mode("all") == "all"


def test_case_insensitive() -> None:
    """evalsケースyamlの env: セクションで `MAIN_AGENT_TOOL_GUARD_MODE: false` の
    ように書くと、YAMLがPythonのbool Falseとしてパースされ、
    evals/case_schema.py の str(v) で "False"（先頭大文字）になる。
    _as_bool() 同様、大文字小文字を区別せず受理できる必要がある。"""
    assert _as_main_agent_tool_guard_mode("False") == "false"
    assert _as_main_agent_tool_guard_mode("ALL") == "all"
    assert _as_main_agent_tool_guard_mode("Tools_Skills_Only") == "tools_skills_only"


def test_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="mode"):
        _as_main_agent_tool_guard_mode("true")
