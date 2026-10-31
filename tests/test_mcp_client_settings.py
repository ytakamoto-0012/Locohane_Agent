"""src/mcp_client.py の純粋関数（設定パース・環境変数展開・ツール名正規化）の回帰テスト。

実プロセス（stdio_client）を起動しない範囲のみを対象にする
（E2E経路は tests/test_mcp_client_lifecycle.py を参照）。
"""

import json

import pytest

from src import mcp_client


def test_parse_settings_missing_file_returns_empty(tmp_path) -> None:
    specs = mcp_client._parse_settings(tmp_path / "nope.json")

    assert specs == []


def test_parse_settings_invalid_json_raises(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        mcp_client._parse_settings(path)


def test_parse_settings_valid_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MY_KEY", "secret-value")
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "my-server": {
                        "command": "npx",
                        "args": ["-y", "@some/mcp-server"],
                        "env": {"API_KEY": "${MY_KEY}", "PLAIN": "asis"},
                        "cwd": "C:/work",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    specs = mcp_client._parse_settings(path)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "my-server"
    assert spec.command == "npx"
    assert spec.args == ["-y", "@some/mcp-server"]
    assert spec.env == {"API_KEY": "secret-value", "PLAIN": "asis"}
    assert spec.cwd == "C:/work"


def test_parse_settings_skips_entry_missing_command(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"mcpServers": {"broken": {"args": ["x"]}}}),
        encoding="utf-8",
    )

    assert mcp_client._parse_settings(path) == []


def test_parse_settings_skips_invalid_server_name(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"mcpServers": {"bad name!": {"command": "npx"}}}),
        encoding="utf-8",
    )

    assert mcp_client._parse_settings(path) == []


def test_parse_settings_skips_disabled_entry(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {"mcpServers": {"disabled-server": {"command": "npx", "disabled": True}}}
        ),
        encoding="utf-8",
    )

    assert mcp_client._parse_settings(path) == []


def test_parse_settings_skips_server_with_unresolved_env_var(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "srv": {"command": "npx", "env": {"KEY": "${MISSING_VAR}"}}
                }
            }
        ),
        encoding="utf-8",
    )

    assert mcp_client._parse_settings(path) == []


def test_resolve_env_all_plain_values() -> None:
    resolved = mcp_client._resolve_env("srv", {"A": "1", "B": "two"})

    assert resolved == {"A": "1", "B": "two"}


def test_resolve_env_expands_env_var(monkeypatch) -> None:
    monkeypatch.setenv("FOO", "bar")

    resolved = mcp_client._resolve_env("srv", {"KEY": "${FOO}"})

    assert resolved == {"KEY": "bar"}


def test_resolve_env_missing_var_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("MISSING_VAR_XYZ", raising=False)

    resolved = mcp_client._resolve_env("srv", {"KEY": "${MISSING_VAR_XYZ}"})

    assert resolved is None


def test_sanitize_tool_name_basic() -> None:
    name = mcp_client._sanitize_tool_name("my-server", "search_docs")

    assert name == "mcp__my-server__search_docs"


def test_sanitize_tool_name_replaces_invalid_chars() -> None:
    name = mcp_client._sanitize_tool_name("srv", "tool with space")

    assert name == "mcp__srv__tool_with_space"


def test_sanitize_tool_name_truncates_to_64_chars() -> None:
    long_tool_name = "a" * 100

    name = mcp_client._sanitize_tool_name("srv", long_tool_name)

    assert len(name) == 64
