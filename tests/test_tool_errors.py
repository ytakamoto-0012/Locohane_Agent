"""src/tool_errors.py（ツールの戻り値がエラーかの判定）のテスト。"""

from __future__ import annotations

import pytest

from src.tool_errors import looks_like_error


@pytest.mark.parametrize(
    "content",
    [
        "Error: file not found: a.xlsx",
        "  error: something failed",
        "エラー: ファイルが見つかりません",
        "[exit code] 1\n[stdout]\n\n[stderr]\nboom",
        "[exit code] timeout\n[stderr]\n",
        "[exit code] 0\n[stderr]\nTraceback (most recent call last):\n  File ...",
    ],
)
def test_error_results(content: str) -> None:
    assert looks_like_error(content)


@pytest.mark.parametrize(
    "content",
    [
        "# excel-edit\n- Without `--new`: an error if it does not exist.",
        "## Summary\nNo errors were found in the 8 files.",
        "[exit code] 0\n[stdout]\nok\n",
        "",
    ],
)
def test_normal_results_mentioning_error(content: str) -> None:
    """本文に error という語が出るだけの正常な戻り値はエラーとみなさない（ログを WARNING にしない）。"""
    assert not looks_like_error(content)
