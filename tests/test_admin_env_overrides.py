"""admin/env_overrides.py のテスト。"""

from __future__ import annotations

from pathlib import Path

from admin import env_overrides

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY_PATH = PROJECT_ROOT / "src" / "config.py"


def test_build_mapping_from_real_config_py_finds_known_keys():
    mapping = env_overrides.build_mapping_from_file(CONFIG_PY_PATH)
    assert mapping[("scripts", "timeout")] == "SCRIPT_TIMEOUT"
    assert mapping[("ui", "max_display_messages")] == "UI_MAX_DISPLAY_MESSAGES"
    assert mapping[("admin", "port")] == "ADMIN_PORT"
    assert mapping[("llm", "main_url")] == "LLM_MAIN_URL"


def test_build_mapping_excludes_subagent_inherited_keys():
    """[context_trim.subagent]配下は専用の環境変数を持たないため対応表に出ない。"""
    mapping = env_overrides.build_mapping_from_file(CONFIG_PY_PATH)
    assert ("context_trim.subagent", "keep_recent_tool_iterations") not in mapping


def test_build_mapping_from_synthetic_source():
    source = '''
llm = parser["llm"] if parser.has_section("llm") else {}
value = int(os.getenv("LLM_MAX_TOKENS", llm.get("max_tokens", 4096)))
'''
    mapping = env_overrides.build_mapping(source)
    assert mapping == {("llm", "max_tokens"): "LLM_MAX_TOKENS"}


def test_build_mapping_handles_multiline_call():
    source = '''
context_trim = parser["context_trim"] if parser.has_section("context_trim") else {}
value = int(
    os.getenv(
        "CONTEXT_TRIM_KEEP_RECENT_TOOL_ITERATIONS", context_trim.get("keep_recent_tool_iterations", 3)
    )
)
'''
    mapping = env_overrides.build_mapping(source)
    assert mapping == {("context_trim", "keep_recent_tool_iterations"): "CONTEXT_TRIM_KEEP_RECENT_TOOL_ITERATIONS"}


def test_active_overrides_filters_by_actual_env():
    mapping = {("scripts", "timeout"): "SCRIPT_TIMEOUT", ("ui", "max_display_messages"): "UI_MAX_DISPLAY_MESSAGES"}
    active = env_overrides.active_overrides(mapping, env={"SCRIPT_TIMEOUT": "120"})
    assert active == {("scripts", "timeout"): "SCRIPT_TIMEOUT"}


def test_active_overrides_ignores_empty_string_env_value():
    mapping = {("scripts", "timeout"): "SCRIPT_TIMEOUT"}
    active = env_overrides.active_overrides(mapping, env={"SCRIPT_TIMEOUT": ""})
    assert active == {}


def test_active_overrides_empty_when_nothing_set():
    mapping = {("scripts", "timeout"): "SCRIPT_TIMEOUT"}
    assert env_overrides.active_overrides(mapping, env={}) == {}
