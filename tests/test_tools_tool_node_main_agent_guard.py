"""[main_agent_tool_guard] の許可リスト方式への回帰テスト。

背景: 従来は entries に登録された対象だけ回数制限がかかり、未登録の
ツール・run_scriptスキルスクリプトはメインエージェントから無制限に呼べて
しまうブロックリスト方式だった。これを許可リスト方式（entries未登録＝
呼び出し不可）へ変更したため、その核心動作
（src/tools/tool_node.py の _guard_main_agent_tool_limit）を検証する。
"""

from types import SimpleNamespace

import pytest

from src.config import _parse_main_agent_tool_guard_allow_entries
from src.tools import tool_node


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def _make_input(name: str, args: dict | None = None) -> dict:
    return {
        "__type": "tool_call_with_context",
        "tool_call": {"name": name, "args": args or {}, "id": "call_1"},
        "state": {},
    }


def _setup(monkeypatch, *, entries, mode: str = "all"):
    cfg = SimpleNamespace(
        main_agent_tool_guard_mode=mode,
        main_agent_tool_guard_allow_entries=frozenset(entries),
    )
    monkeypatch.setattr(tool_node._state, "_LLM_CONFIG", cfg)
    monkeypatch.setattr(tool_node._state, "_AGENT_TYPES", {"worker": object()})
    session = _FakeUserSession()
    monkeypatch.setattr(tool_node.cl, "user_session", session)
    return session


def test_unregistered_tool_is_blocked(monkeypatch) -> None:
    """許可リスト（entries）に無い名前は、たとえ無害なツールでも一切呼べない。"""
    _setup(monkeypatch, entries=[("Glob", 1)])

    result = tool_node._guard_main_agent_tool_limit(_make_input("some_unlisted_tool"))

    assert result is not None
    content = result["messages"][0].content
    assert "some_unlisted_tool" in content
    assert "is not allowed for the main agent" in content


def test_max_calls_minus_one_is_unlimited(monkeypatch) -> None:
    """max_calls=-1 で登録したツールは、何度呼んでも許可される。"""
    _setup(monkeypatch, entries=[("dispatch_agent", -1)])

    for _ in range(5):
        result = tool_node._guard_main_agent_tool_limit(_make_input("dispatch_agent"))
        assert result is None


def test_max_calls_zero_always_blocks(monkeypatch) -> None:
    """max_calls=0 で登録したツールは常に完全ブロックされる（従来通り）。"""
    _setup(monkeypatch, entries=[("Read", 0)])

    result = tool_node._guard_main_agent_tool_limit(_make_input("Read"))

    assert result is not None
    content = result["messages"][0].content
    assert "max_calls=0" in content


def test_positive_max_calls_blocks_after_limit(monkeypatch) -> None:
    """正の max_calls は従来通り、上限到達後にブロックされる。"""
    _setup(monkeypatch, entries=[("Glob", 2)])

    first = tool_node._guard_main_agent_tool_limit(_make_input("Glob"))
    second = tool_node._guard_main_agent_tool_limit(_make_input("Glob"))
    third = tool_node._guard_main_agent_tool_limit(_make_input("Glob"))

    assert first is None
    assert second is None
    assert third is not None
    assert "call limit" in third["messages"][0].content


def test_run_script_pair_unregistered_is_blocked(monkeypatch) -> None:
    """run_script経由のスキルスクリプトも、[skill, script]ペア未登録ならブロックされる。"""
    _setup(monkeypatch, entries=[(("pdf-tools", "render_pdf_pages.py"), 0)])

    result = tool_node._guard_main_agent_tool_limit(
        _make_input("run_script", {"skill_name": "web-search", "script_filename": "search_web.py"})
    )

    assert result is not None
    assert "is not allowed for the main agent" in result["messages"][0].content


def test_run_script_pair_registered_unlimited(monkeypatch) -> None:
    _setup(monkeypatch, entries=[(("pdf-tools", "render_pdf_pages.py"), -1)])

    result = tool_node._guard_main_agent_tool_limit(
        _make_input("run_script", {"skill_name": "pdf-tools", "script_filename": "render_pdf_pages.py"})
    )

    assert result is None


def test_subagent_call_is_not_guarded(monkeypatch) -> None:
    """サブエージェント（dispatch_agent配下）内部での呼び出しは、未登録でも対象外。"""
    _setup(monkeypatch, entries=[])
    token = tool_node._IN_SUBAGENT.set(True)
    try:
        result = tool_node._guard_main_agent_tool_limit(_make_input("some_unlisted_tool"))
    finally:
        tool_node._IN_SUBAGENT.reset(token)

    assert result is None


def test_guard_disabled_allows_everything(monkeypatch) -> None:
    _setup(monkeypatch, entries=[], mode="false")

    result = tool_node._guard_main_agent_tool_limit(_make_input("some_unlisted_tool"))

    assert result is None


def test_tools_skills_only_mode_always_allows_unregistered_mcp_tool(monkeypatch) -> None:
    """mode=tools_skills_only なら、未登録のMCP動的ツール（mcp__プレフィックス）は
    常に許可される。"""
    _setup(monkeypatch, entries=[], mode="tools_skills_only")

    result = tool_node._guard_main_agent_tool_limit(_make_input("mcp__locohane-skills__list_skills"))

    assert result is None


def test_tools_skills_only_mode_still_blocks_unregistered_builtin_tool(monkeypatch) -> None:
    """mode=tools_skills_only でも、ビルトインツールは mode=all と同じく
    未登録なら引き続きブロックされる（MCP以外は対象外にしない）。"""
    _setup(monkeypatch, entries=[], mode="tools_skills_only")

    result = tool_node._guard_main_agent_tool_limit(_make_input("some_unlisted_tool"))

    assert result is not None


@pytest.mark.parametrize("entries", [[], [("Glob", 1)]])
def test_empty_entries_blocks_all_unregistered(monkeypatch, entries) -> None:
    """entries が空、または対象外の名前しか登録されていない場合、
    許可リスト方式では未登録名は常にブロックされる（旧・素通り仕様の回帰防止）。"""
    _setup(monkeypatch, entries=entries)

    result = tool_node._guard_main_agent_tool_limit(_make_input("totally_unknown_tool"))

    assert result is not None


def test_parse_rejects_duplicate_key_with_different_max_calls() -> None:
    """同じ対象を異なるmax_callsで重複登録すると、_guard_main_agent_tool_limit側で
    frozenset を dict() 化する際にどちらが勝つかがPYTHONHASHSEED依存の非決定動作に
    なる（実測確認済みの回帰）。パース時点で拒否する。"""
    with pytest.raises(ValueError, match="registered twice"):
        _parse_main_agent_tool_guard_allow_entries('[["Glob", 1], ["Glob", 5]]')


def test_parse_rejects_duplicate_key_with_identical_max_calls() -> None:
    """max_callsが同じ完全同一の重複も、設定ミスとして拒否する。"""
    with pytest.raises(ValueError, match="registered twice"):
        _parse_main_agent_tool_guard_allow_entries('[["Glob", 1], ["Glob", 1]]')


def test_parse_rejects_duplicate_run_script_pair() -> None:
    """[skill, script] ペア形式の対象も同様に重複登録を拒否する。"""
    with pytest.raises(ValueError, match="registered twice"):
        _parse_main_agent_tool_guard_allow_entries(
            '[[["pdf-tools","render_pdf_pages.py"], 0], [["pdf-tools","render_pdf_pages.py"], -1]]'
        )


def test_parse_allows_distinct_keys() -> None:
    result = _parse_main_agent_tool_guard_allow_entries('[["Glob", 1], ["Read", -1]]')
    assert result == frozenset({("Glob", 1), ("Read", -1)})


def _make_tools(*names: str) -> list:
    return [SimpleNamespace(name=n) for n in names]


def _make_cfg(*, entries, mode: str = "all"):
    return SimpleNamespace(
        main_agent_tool_guard_mode=mode,
        main_agent_tool_guard_allow_entries=frozenset(entries),
    )


def test_filter_main_agent_tools_guard_disabled_keeps_everything() -> None:
    tools = _make_tools("Glob", "Read", "run_script")
    cfg = _make_cfg(entries=[], mode="false")

    assert tool_node.filter_main_agent_tools(tools, cfg) == tools


def test_filter_main_agent_tools_tools_skills_only_keeps_unregistered_mcp_tool() -> None:
    """mode=tools_skills_only なら、未登録のMCP動的ツールも bind 対象に残る。"""
    tools = _make_tools("Glob", "mcp__locohane-skills__list_skills")
    cfg = _make_cfg(entries=[], mode="tools_skills_only")

    result = tool_node.filter_main_agent_tools(tools, cfg)

    assert {t.name for t in result} == {"mcp__locohane-skills__list_skills"}


def test_filter_main_agent_tools_drops_unregistered_and_zero() -> None:
    tools = _make_tools("Glob", "Read", "dispatch_agent")
    cfg = _make_cfg(entries=[("Glob", 1), ("Read", 0)])

    result = tool_node.filter_main_agent_tools(tools, cfg)

    assert [t.name for t in result] == ["Glob"]


def test_filter_main_agent_tools_keeps_unlimited_and_positive() -> None:
    tools = _make_tools("dispatch_agent", "Glob")
    cfg = _make_cfg(entries=[("dispatch_agent", -1), ("Glob", 3)])

    result = tool_node.filter_main_agent_tools(tools, cfg)

    assert {t.name for t in result} == {"dispatch_agent", "Glob"}


def test_filter_main_agent_tools_drops_run_script_without_allowed_pair() -> None:
    tools = _make_tools("run_script", "run_script_background", "Glob")
    cfg = _make_cfg(entries=[("Glob", 1), (("pdf-tools", "render_pdf_pages.py"), 0)])

    result = tool_node.filter_main_agent_tools(tools, cfg)

    assert {t.name for t in result} == {"Glob"}


def test_filter_main_agent_tools_keeps_run_script_with_allowed_pair() -> None:
    tools = _make_tools("run_script", "run_script_background")
    cfg = _make_cfg(entries=[(("pdf-tools", "render_pdf_pages.py"), -1)])

    result = tool_node.filter_main_agent_tools(tools, cfg)

    assert {t.name for t in result} == {"run_script", "run_script_background"}


def test_filter_main_agent_tools_ignores_skill_visibility_dummy_entry_for_run_script() -> None:
    """[skill_name, ""]（scriptsを持たないスキルの一覧表示専用ダミーエントリ）は
    run_script/run_script_background 自体のbind判定にはカウントしない
    （このエントリしか無い場合にrun_scriptを無意味にbindしないようにするため）。"""
    tools = _make_tools("run_script", "run_script_background", "Glob")
    cfg = _make_cfg(entries=[("Glob", 1), (("excel-knowledge", ""), -1)])

    result = tool_node.filter_main_agent_tools(tools, cfg)

    assert {t.name for t in result} == {"Glob"}


def test_list_blocked_tool_names_for_hint_guard_disabled_returns_empty() -> None:
    tools = _make_tools("Glob", "Read", "run_script")
    cfg = _make_cfg(entries=[], mode="false")

    assert tool_node.list_blocked_tool_names_for_hint(tools, cfg) == []


def test_list_blocked_tool_names_for_hint_tools_skills_only_excludes_mcp_tool() -> None:
    """mode=tools_skills_only なら、未登録のMCP動的ツール名はヒント一覧に含めない
    （常に許可扱いのため）。"""
    tools = _make_tools("Glob", "mcp__locohane-skills__list_skills")
    cfg = _make_cfg(entries=[], mode="tools_skills_only")

    result = tool_node.list_blocked_tool_names_for_hint(tools, cfg)

    assert result == ["Glob"]


def test_list_blocked_tool_names_for_hint_includes_unregistered_and_zero() -> None:
    tools = _make_tools("Glob", "Read", "dispatch_agent")
    cfg = _make_cfg(entries=[("Glob", 1), ("Read", 0)])

    result = tool_node.list_blocked_tool_names_for_hint(tools, cfg)

    assert result == ["Read", "dispatch_agent"]


def test_list_blocked_tool_names_for_hint_excludes_unlimited_and_positive() -> None:
    tools = _make_tools("dispatch_agent", "Glob")
    cfg = _make_cfg(entries=[("dispatch_agent", -1), ("Glob", 3)])

    assert tool_node.list_blocked_tool_names_for_hint(tools, cfg) == []


def test_list_blocked_tool_names_for_hint_excludes_run_script_names() -> None:
    tools = _make_tools("run_script", "run_script_background", "Glob")
    cfg = _make_cfg(entries=[("Glob", 1), (("pdf-tools", "render_pdf_pages.py"), 0)])

    result = tool_node.list_blocked_tool_names_for_hint(tools, cfg)

    assert result == []
