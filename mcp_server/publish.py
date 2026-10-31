"""skills/ の配布用コピー作成（Resources方式向け、機密ファイル除外）。

SkillsDirectoryProvider（PyPI: fastmcp）はスキルフォルダ配下を再帰的に
無条件でスキャンするため（除外パターンの指定機能なし）、`web-search` スキルの
`scripts/.env`（TAVILY_API_KEY）のような機密ファイルがそのまま配布されて
しまう。これを避けるため、配布用の一時コピーを作る際に EXCLUDE_NAMES に
該当するファイルを除外している。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .config import EXCLUDE_NAMES, SKILLS_SRC


def build_publish_dir() -> Path:
    """機密ファイルを除いた配布用コピーを一時ディレクトリに作成して返す。

    SKILLS_SRC は複数ルートを持ちうる。同名スキルが複数ルートに存在する
    場合は先頭に近いルート（`LOCOHANE_MCP_SKILLS_SRC` の並び順）を優先し、
    後続ルートの同名スキルは無視する。
    """
    publish_dir = Path(tempfile.mkdtemp(prefix="locohane_mcp_skills_"))
    seen: set[str] = set()
    for root in SKILLS_SRC:
        if not root.is_dir():
            continue
        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name in seen:
                continue
            seen.add(skill_dir.name)
            shutil.copytree(
                skill_dir,
                publish_dir / skill_dir.name,
                ignore=shutil.ignore_patterns(*EXCLUDE_NAMES),
            )
    return publish_dir
