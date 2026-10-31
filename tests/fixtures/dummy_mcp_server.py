"""tests/test_mcp_client_lifecycle.py が実プロセスとして起動する、1ツールだけ
持つ最小のMCPサーバー（stdioトランスポート）。外部ネットワーク通信は行わない。
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("dummy-server")


@mcp.tool()
def echo(text: str) -> str:
    """受け取った文字列をそのまま返す。"""
    return f"echo:{text}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
