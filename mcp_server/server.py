"""Locohaneの skills/ 配下のスキルをMCPサーバーとして公開するエントリーポイント。

詳細な設計・注意点は MCP_README.md を参照。要点のみ:

- Resources方式（SkillsDirectoryProvider）: SKILL.md および
  `scripts/`/`references/`/`assets/` 配下の全ファイルをテキストとして
  配布する。機密ファイル（`.env`系）は `publish.build_publish_dir()` が
  除外する。
- Tools方式（skill_tools）: `list_skills`/`read_skill`/`run_skill_script`
  の3ツールで、スキルの発見・手順閲覧・スクリプトの実際の実行までを
  MCPクライアント（Claude Code等）から行えるようにする。
"""

from __future__ import annotations

import sys
from pathlib import Path

# `python mcp_server/server.py`（相対import不可）でも `python -m mcp_server.server`
# でも同じように動かせるよう、プロジェクトルートを明示的に sys.path へ追加してから
# 絶対importする（cwdがどこであっても __file__ 基準で解決するため安定する）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import FastMCP
from fastmcp.server.providers.skills import SkillsDirectoryProvider

from mcp_server import config, skill_tools
from mcp_server.publish import build_publish_dir
from src.version import __version__

mcp = FastMCP(config.SERVER_NAME, version=__version__)
mcp.add_provider(
    SkillsDirectoryProvider(
        roots=build_publish_dir(),
        supporting_files="resources",
    )
)
mcp.add_tool(skill_tools.list_skills)
mcp.add_tool(skill_tools.read_skill)
mcp.add_tool(skill_tools.run_skill_script)


if __name__ == "__main__":
    mcp.run()
