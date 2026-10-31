"""Locohane のバージョン番号。

番号の正本はプロジェクト直下の VERSION ファイル（1行・セマンティックバージョニング）で、
本体・管理ツール・MCP サーバーはここから読む。リリース手順は README_DETAIL.md の
「バージョン管理とリリース手順」節を参照。
"""

from __future__ import annotations

from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def read_version(path: Path = VERSION_FILE) -> str:
    """VERSION ファイルの番号を返す。読めない場合は "unknown" を返す（起動は止めない）。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return text or "unknown"


__version__ = read_version()
