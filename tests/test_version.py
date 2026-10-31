"""VERSION ファイルからのバージョン番号読み込み（src/version.py）の回帰テスト。"""

from __future__ import annotations

import re

from src import version


def test_version_file_is_semver():
    # リリース手順（README_DETAIL「バージョン管理とリリース手順」）は MAJOR.MINOR.PATCH 前提
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.__version__)


def test_read_version_strips_whitespace(tmp_path):
    path = tmp_path / "VERSION"
    path.write_text("1.2.3\r\n", encoding="utf-8")
    assert version.read_version(path) == "1.2.3"


def test_read_version_missing_or_empty_file_returns_unknown(tmp_path):
    assert version.read_version(tmp_path / "missing") == "unknown"
    empty = tmp_path / "VERSION"
    empty.write_text("\n", encoding="utf-8")
    assert version.read_version(empty) == "unknown"
