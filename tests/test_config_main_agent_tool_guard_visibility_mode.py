"""_as_main_agent_tool_guard_visibility_mode() の回帰テスト。

[main_agent_tool_guard].visibility_mode は strict/hint の2値のみを許容する
（既定は strict、空欄・None も strict として扱う）。src/config.py の
LLM_ROUTING_STRATEGIES / _as_routing_strategy() と同じ検証パターン。
"""

import pytest

from src.config import _as_main_agent_tool_guard_visibility_mode


def test_defaults_to_strict_when_empty_or_none() -> None:
    assert _as_main_agent_tool_guard_visibility_mode(None) == "strict"
    assert _as_main_agent_tool_guard_visibility_mode("") == "strict"
    assert _as_main_agent_tool_guard_visibility_mode("   ") == "strict"


def test_accepts_strict_and_hint() -> None:
    assert _as_main_agent_tool_guard_visibility_mode("strict") == "strict"
    assert _as_main_agent_tool_guard_visibility_mode("hint") == "hint"


def test_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="visibility_mode"):
        _as_main_agent_tool_guard_visibility_mode("full_block")
