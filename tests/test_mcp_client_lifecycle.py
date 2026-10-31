"""src/mcp_client.py の接続ライフサイクルの統合テスト。

tests/fixtures/dummy_mcp_server.py を実プロセスとして起動し、
tools/list → tools/call → shutdown までを実際の stdio transport で通す
（外部ネットワーク通信は行わない、CI/ローカル完結）。
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import mcp_client, tools

_FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "dummy_mcp_server.py"


def _make_config(settings_path: Path, connect_timeout: float = 20.0, call_timeout: float = 10.0):
    return SimpleNamespace(
        mcp_settings_path=settings_path,
        mcp_connect_timeout_seconds=connect_timeout,
        mcp_call_timeout_seconds=call_timeout,
    )


@pytest.fixture(autouse=True)
def _reset_mcp_tools(monkeypatch):
    monkeypatch.setattr(tools.registry, "_MCP_TOOLS", [])
    yield
    monkeypatch.setattr(tools.registry, "_MCP_TOOLS", [])


@pytest.mark.asyncio
async def test_init_mcp_tools_connects_and_registers_tool(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "dummy": {
                        "command": sys.executable,
                        "args": [str(_FIXTURE_SERVER)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = _make_config(settings_path)

    try:
        await mcp_client.init_mcp_tools(config)

        all_tools = tools.get_all_tools()
        wrapped = next((t for t in all_tools if t.name == "mcp__dummy__echo"), None)
        assert wrapped is not None

        result = await wrapped.ainvoke({"text": "hi"})
        assert result == "echo:hi"
    finally:
        await mcp_client.shutdown_mcp_tools()


@pytest.mark.asyncio
async def test_init_mcp_tools_skips_failing_server_without_raising(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "broken": {
                        "command": "this-command-does-not-exist-xyz",
                        "args": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config = _make_config(settings_path, connect_timeout=5.0)

    try:
        await mcp_client.init_mcp_tools(config)

        assert tools.get_all_tools() == tools.registry._BASE_TOOLS
    finally:
        await mcp_client.shutdown_mcp_tools()


@pytest.mark.asyncio
async def test_init_mcp_tools_no_settings_file_registers_nothing(tmp_path) -> None:
    config = _make_config(tmp_path / "nope.json")

    await mcp_client.init_mcp_tools(config)

    assert tools.get_all_tools() == tools.registry._BASE_TOOLS
