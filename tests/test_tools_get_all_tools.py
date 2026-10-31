"""register_mcp_tools() / get_all_tools() の回帰テスト。

MCPサーバーから取得した動的ツールが、組み込みツール（_BASE_TOOLS）と
合流して get_all_tools() から返ることを検証する。
"""

from langchain_core.tools import tool

from src import tools


@tool
def _dummy_mcp_tool_a(x: str) -> str:
    """テスト用のダミーツールA。"""
    return x


@tool
def _dummy_mcp_tool_b(x: str) -> str:
    """テスト用のダミーツールB。"""
    return x


def test_get_all_tools_includes_base_tools_only_by_default(monkeypatch) -> None:
    monkeypatch.setattr(tools.registry, "_MCP_TOOLS", [])

    all_tools = tools.get_all_tools()

    assert all_tools == tools.registry._BASE_TOOLS


def test_register_mcp_tools_then_get_all_tools_includes_both(monkeypatch) -> None:
    monkeypatch.setattr(tools.registry, "_MCP_TOOLS", [])

    tools.register_mcp_tools([_dummy_mcp_tool_a, _dummy_mcp_tool_b])
    all_tools = tools.get_all_tools()

    assert len(all_tools) == len(tools.registry._BASE_TOOLS) + 2
    assert _dummy_mcp_tool_a in all_tools
    assert _dummy_mcp_tool_b in all_tools
    for base_tool in tools.registry._BASE_TOOLS:
        assert base_tool in all_tools


def test_register_mcp_tools_replaces_previous_registration(monkeypatch) -> None:
    monkeypatch.setattr(tools.registry, "_MCP_TOOLS", [_dummy_mcp_tool_a])

    tools.register_mcp_tools([_dummy_mcp_tool_b])

    assert tools.registry._MCP_TOOLS == [_dummy_mcp_tool_b]
