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


@pytest.mark.asyncio
async def test_status_reports_connected_server_and_calls(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "dummy": {"command": sys.executable, "args": [str(_FIXTURE_SERVER)]},
                    "off": {"command": "x", "disabled": True},
                    "needs_env": {"command": "x", "env": {"K": "${LOCOHANE_TEST_UNSET_ENV_XYZ}"}},
                }
            }
        ),
        encoding="utf-8",
    )
    config = _make_config(settings_path)

    try:
        await mcp_client.init_mcp_tools(config)
        status = mcp_client.get_mcp_status()
        assert status["initialized"] is True and status["settings_found"] is True
        by_name = {s["name"]: s for s in status["servers"]}
        # settings.json の記載順に並ぶ
        assert [s["name"] for s in status["servers"]] == ["dummy", "off", "needs_env"]
        assert by_name["dummy"]["state"] == "connected"
        assert "echo" in by_name["dummy"]["tools"]
        assert by_name["dummy"]["connected_at"]
        assert by_name["off"]["state"] == "skipped" and by_name["off"]["skip_reason"] == "disabled"
        assert by_name["needs_env"]["skip_reason"] == "env_missing"
        assert "LOCOHANE_TEST_UNSET_ENV_XYZ" in by_name["needs_env"]["error"]

        wrapped = next(t for t in tools.get_all_tools() if t.name == "mcp__dummy__echo")
        await wrapped.ainvoke({"text": "hi"})
        dummy = {s["name"]: s for s in mcp_client.get_mcp_status()["servers"]}["dummy"]
        assert dummy["calls"] == 1 and dummy["failures"] == 0
        assert dummy["last_call"]["tool"] == "echo" and dummy["last_call"]["ok"] is True
        # 返り値は複製（呼び出し側が前回値と比べて変化を判定できる）
        assert status["servers"][0]["calls"] == 0
    finally:
        await mcp_client.shutdown_mcp_tools()
    assert mcp_client.get_mcp_status() == {
        "initialized": False,
        "settings_found": False,
        "settings_error": None,
        "servers": [],
    }


@pytest.mark.asyncio
async def test_status_reports_failed_server(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"mcpServers": {"broken": {"command": "this-command-does-not-exist-xyz"}}}),
        encoding="utf-8",
    )
    try:
        await mcp_client.init_mcp_tools(_make_config(settings_path, connect_timeout=5.0))
        (server,) = mcp_client.get_mcp_status()["servers"]
        assert server["state"] == "failed"
        assert server["error"]
    finally:
        await mcp_client.shutdown_mcp_tools()


@pytest.mark.asyncio
async def test_status_reports_settings_syntax_error(tmp_path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{ not json", encoding="utf-8")
    try:
        with pytest.raises(ValueError):
            await mcp_client.init_mcp_tools(_make_config(settings_path))
        assert mcp_client.get_mcp_status()["settings_error"]
    finally:
        await mcp_client.shutdown_mcp_tools()


def test_record_call_marks_connection_closed_as_disconnected(monkeypatch) -> None:
    from mcp.shared.exceptions import McpError
    from mcp.types import CONNECTION_CLOSED, ErrorData

    monkeypatch.setattr(mcp_client, "_STATUS", {})
    mcp_client._set_status("s", state="connected", tools=["t"])
    err = McpError(ErrorData(code=CONNECTION_CLOSED, message="Connection closed"))
    assert mcp_client._is_connection_closed(err)
    mcp_client._record_call("s", "t", ok=False, error=mcp_client._error_text(err), disconnected=True)
    s = mcp_client._STATUS["s"]
    assert s["state"] == "disconnected" and s["failures"] == 1 and s["last_call"]["ok"] is False
