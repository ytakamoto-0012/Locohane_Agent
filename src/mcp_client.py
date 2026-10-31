"""MCP（Model Context Protocol）サーバーへの接続とツール変換。

仕様: https://modelcontextprotocol.io/specification

対応トランスポート: stdio のみ（SSE / streamable-http は対象外）。

役割:
- .locohane/settings.json の mcpServers を読み込む。
- サーバーごとに専用のバックグラウンドタスクで公式SDK（mcpパッケージ）の
  stdio_client + ClientSession を起動・保持する。stdio_client/ClientSession
  は開いたタスクと同じタスクで閉じる必要がある（cross-task cancel-scope
  破損を避けるため）ため、chainlit本体のMCP実装（server.py の connect_mcp）
  と同じ ready_event/stop_event/result_holder パターンを踏襲する。
- 起動後は src.tools.register_mcp_tools() へ StructuredTool のフラット
  リストを渡すだけで、以降のツール実行は既存の ImageAwareToolNode に委ねる
  （MCP固有のディスパッチ経路は持たない）。

既知の限界（v1スコープ外、将来拡張の余地として残す）:
- 接続後にサーバーがクラッシュした場合の自動再接続は行わない
  （以降の呼び出しは "エラー: ..." 文字列を返すのみで、グラフは壊さない）。
- Windows上でプロセスが異常終了した場合、stdioサブプロセスが孤児化する
  リスクへの対策（Job Object等）は持たない。
- MCPツールの応答はテキストブロックのみ抽出する（image/embedded resource
  は対象外。既存の analyze_image パイプラインとは統合しない）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import traceback
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import McpError

from . import tools as tools_module
from .config import Config
from .tools.tool_node import MCP_TOOL_NAME_PREFIX

logger = logging.getLogger(__name__)

_ENV_VAR_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_SERVER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_TOOL_NAME_MAX_CHARS = 64  # OpenAI互換API / Claude Codeのツール名慣習に合わせる


@dataclass(frozen=True)
class McpServerSpec:
    """.locohane/settings.json の mcpServers 1エントリ分の実行時表現。"""

    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str | None


@dataclass
class _ServerLifecycle:
    """1サーバー分のバックグラウンドタスクの制御ハンドル（shutdown時に使う）。"""

    name: str
    task: asyncio.Task
    stop_event: asyncio.Event


_LIFECYCLES: list[_ServerLifecycle] = []


def _resolve_env(server_name: str, env: dict[str, Any]) -> dict[str, str] | None:
    """env の値に含まれる ${ENV_VAR_NAME} を os.environ から展開する。

    .locohane/settings.json はgit管理対象のため、APIキー等の機密情報を
    平文で書かせないための緩和策（Claude Codeの .mcp.json と同じ慣習）。

    Args:
        server_name: ログ出力用のサーバー名。
        env: settings.json の env フィールド（文字列値の辞書）。

    Returns:
        展開済みの辞書。未解決の変数参照が1つでもあれば None
        （呼び出し元はそのサーバーの接続をスキップする）。
    """
    resolved: dict[str, str] = {}
    for key, value in env.items():
        text = str(value)
        m = _ENV_VAR_PATTERN.match(text)
        if m is None:
            resolved[key] = text
            continue
        actual = os.environ.get(m.group(1))
        if actual is None:
            logger.warning(
                "MCP server '%s': skipping the connection because environment variable %s is not set",
                server_name,
                m.group(1),
            )
            return None
        resolved[key] = actual
    return resolved


def _parse_settings(path: Path) -> list[McpServerSpec]:
    """.locohane/settings.json の mcpServers を McpServerSpec のリストへ変換する。

    ファイル不在は空リスト（MCP機能を単にスキップする）。JSON構文エラーは
    起動時のはっきりした失敗にする（config.ini読み込み同様のfail fast方針）。
    個々のサーバーエントリの構造不正（command欠落・不正なサーバー名等）は
    そのエントリのみ警告してスキップする（1サーバーの不備で他サーバーの
    接続やアプリ起動そのものを止めない、フォールト分離）。

    Args:
        path: settings.json の絶対パス（config.mcp_settings_path）。

    Returns:
        妥当な McpServerSpec のリスト（disabled=true のエントリは除外済み）。
    """
    if not path.is_file():
        logger.info("MCP settings file not found (skipped): %s", path)
        return []

    data = json.loads(path.read_text(encoding="utf-8"))  # 構文エラーは呼び出し元へ伝播
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        logger.warning("Skipping MCP because mcpServers is malformed: %s", path)
        return []

    specs: list[McpServerSpec] = []
    for name, raw in servers.items():
        if not _SERVER_NAME_PATTERN.match(name):
            logger.warning("Skipping an invalid MCP server name: %r", name)
            continue
        if not isinstance(raw, dict) or not raw.get("command"):
            logger.warning("Skipping MCP server '%s' because its definition is invalid (command is required)", name)
            continue
        if raw.get("disabled"):
            logger.info("Skipping MCP server '%s' because disabled=true", name)
            continue
        env = _resolve_env(name, raw.get("env", {}) or {})
        if env is None:
            continue
        specs.append(
            McpServerSpec(
                name=name,
                command=str(raw["command"]),
                args=[str(a) for a in raw.get("args", [])],
                env=env,
                cwd=raw.get("cwd"),
            )
        )
    return specs


async def _run_server_lifecycle(
    spec: McpServerSpec,
    connect_timeout: float,
    ready_event: asyncio.Event,
    stop_event: asyncio.Event,
    result_holder: dict[str, Any],
) -> None:
    """1サーバー分の接続を保持し続けるバックグラウンドタスク本体。

    stdio_client/ClientSession を開いたのと同じタスク内で initialize・
    tools/list を行い、以降は stop_event がセットされるまで待機し続けた上で
    同じタスク内で AsyncExitStack を閉じる（サブプロセスの正常終了）。

    Args:
        spec: 接続先サーバーの定義。
        connect_timeout: 起動（プロセス起動+initialize+tools/list）の
            タイムアウト秒数。
        ready_event: 接続試行が完了した（成功/失敗いずれか）ことを示す。
        stop_event: shutdown_mcp_tools() からの停止指示。
        result_holder: 呼び出し元と結果を受け渡す辞書
            （成功時 "session"/"tools"、失敗時 "error"）。
    """
    exit_stack = AsyncExitStack()
    try:
        try:

            async def _connect() -> ClientSession:
                params = StdioServerParameters(
                    command=spec.command,
                    args=spec.args,
                    env={**get_default_environment(), **spec.env},
                    cwd=spec.cwd,
                )
                read, write = await exit_stack.enter_async_context(stdio_client(params))
                session = await exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                return session

            session = await asyncio.wait_for(_connect(), timeout=connect_timeout)
            tools_result = await asyncio.wait_for(session.list_tools(), timeout=connect_timeout)
            result_holder["session"] = session
            result_holder["tools"] = tools_result.tools
        except BaseException as exc:  # noqa: BLE001 - 起動失敗の全経路を握りつぶし呼び出し元へ伝える
            result_holder["error"] = exc
            return
        finally:
            ready_event.set()

        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
    finally:
        try:
            await exit_stack.aclose()
        except BaseException:  # noqa: BLE001 - 終了処理の失敗でアプリ終了自体は止めない
            logger.debug("Error while shutting down MCP server '%s'", spec.name, exc_info=True)


def _sanitize_tool_name(server_name: str, tool_name: str) -> str:
    """mcp__<server>__<tool> 形式のツール名を組み立てる。

    既存の組み込みツールおよびサーバー間でのツール名衝突を防ぐための
    命名規則（Claude CodeのMCPツール命名慣習を踏襲）。OpenAI互換APIの
    ツール名制約（英数字・アンダースコア・ハイフンのみ、64文字以内）に収める。
    プレフィックス（MCP_TOOL_NAME_PREFIX）は src/tools/tool_node.py の
    _is_mcp_tool_name() が同じ値を参照し、[main_agent_tool_guard].mode=
    "tools_skills_only" の判定に使う。
    """
    raw = f"{MCP_TOOL_NAME_PREFIX}{server_name}__{tool_name}"
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    return sanitized[:_TOOL_NAME_MAX_CHARS]


def _wrap_mcp_tool(
    server_name: str,
    session_holder: dict[str, Any],
    mcp_tool: Any,
    call_timeout: float,
) -> BaseTool:
    """MCPツール定義（tools/list の1件）を LangChain の StructuredTool に変換する。

    Args:
        server_name: 接続元サーバー名（ツール名プレフィックス・エラー文言に使う）。
        session_holder: {"session": ClientSession} を保持する辞書。
            サーバー切断時にも同じ辞書を差し替えられるよう間接参照にしている。
        mcp_tool: mcp.types.Tool（name/description/inputSchema を持つ）。
        call_timeout: tools/call 1回あたりのタイムアウト秒数。

    Returns:
        LLMにbindできる StructuredTool。エラーは例外を送出せず、既存の
        組み込みツール群と同じ "エラー: ..." 形式の文字列で返す。
    """
    name = _sanitize_tool_name(server_name, mcp_tool.name)

    async def _call(**kwargs: Any) -> str:
        session = session_holder.get("session")
        if session is None:
            return f"Error: not connected to MCP server '{server_name}'."
        try:
            result = await asyncio.wait_for(
                session.call_tool(mcp_tool.name, kwargs), timeout=call_timeout
            )
        except asyncio.TimeoutError:
            return (
                f"Error: the MCP tool call timed out after {call_timeout} seconds "
                f"(server={server_name}, tool={mcp_tool.name})."
            )
        except McpError as e:
            return f"Error: the MCP tool call failed (server={server_name}, tool={mcp_tool.name}): {e}"
        except Exception as e:  # noqa: BLE001 - 予期しない伝送層エラーもグラフを壊さず返す
            return (
                f"Error: unexpected error during the MCP tool call "
                f"(server={server_name}, tool={mcp_tool.name}): {e}\n{traceback.format_exc()}"
            )
        texts = [b.text for b in result.content if getattr(b, "type", None) == "text"]
        text = "\n".join(texts) if texts else "(non-text or empty response)"
        return f"Error (on the MCP tool side): {text}" if result.isError else text

    return StructuredTool(
        name=name,
        description=mcp_tool.description or f"(no description: {server_name}/{mcp_tool.name})",
        args_schema=mcp_tool.inputSchema,
        coroutine=_call,
    )


async def init_mcp_tools(config: Config) -> None:
    """アプリ起動時（@cl.on_app_startup）に1回呼ぶ。

    .locohane/settings.json の mcpServers 全件へ並行接続し、成功したサーバー
    のツールのみを src.tools.register_mcp_tools() へ登録する。接続に失敗
    したサーバーは警告ログを出すだけでスキップし、アプリ全体の起動は継続する。

    Args:
        config: アプリ設定（mcp_settings_path / mcp_connect_timeout_seconds /
            mcp_call_timeout_seconds を参照する）。
    """
    global _LIFECYCLES
    specs = _parse_settings(config.mcp_settings_path)
    if not specs:
        tools_module.register_mcp_tools([])
        return

    all_wrapped: list[BaseTool] = []
    lifecycles: list[_ServerLifecycle] = []
    for spec in specs:
        ready_event: asyncio.Event = asyncio.Event()
        stop_event: asyncio.Event = asyncio.Event()
        result_holder: dict[str, Any] = {}
        task = asyncio.create_task(
            _run_server_lifecycle(
                spec, config.mcp_connect_timeout_seconds, ready_event, stop_event, result_holder
            ),
            name=f"mcp-server-{spec.name}",
        )
        await ready_event.wait()
        if "error" in result_holder:
            logger.warning(
                "Failed to connect to MCP server '%s': %s", spec.name, result_holder["error"]
            )
            stop_event.set()
            continue
        session_holder = {"session": result_holder["session"]}
        mcp_tools = result_holder["tools"]
        for mcp_tool in mcp_tools:
            all_wrapped.append(
                _wrap_mcp_tool(spec.name, session_holder, mcp_tool, config.mcp_call_timeout_seconds)
            )
        lifecycles.append(_ServerLifecycle(spec.name, task, stop_event))
        logger.info(
            "Connected to MCP server '%s' and registered %d tools", spec.name, len(mcp_tools)
        )

    _LIFECYCLES = lifecycles
    tools_module.register_mcp_tools(all_wrapped)


async def shutdown_mcp_tools() -> None:
    """アプリ終了時（@cl.on_app_shutdown）に1回呼ぶ。

    全サーバーのバックグラウンドタスクへ停止を通知し、各タスクが
    （開いたタスク自身で）stdioサブプロセスを正常終了させるのを待つ。
    """
    for lc in _LIFECYCLES:
        lc.stop_event.set()
    if _LIFECYCLES:
        await asyncio.gather(*(lc.task for lc in _LIFECYCLES), return_exceptions=True)
