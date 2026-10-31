"""ツールの戻り値がエラーかどうかの判定（ログを WARNING にするかの判断に使う）。

ツールはエラーを "Error:" で始まる文字列で返す。スクリプト実行系（run_script・
execute_python_*）は "[exit code] N" の行と、例外なら Python の Traceback を含む。
本文に "error" という語が出てくるだけ（SKILL.md の説明・調査結果の要約など）ではエラーとみなさない。
"""

from __future__ import annotations

import re

_ERROR_PREFIXES = ("error:", "エラー")  # 「エラー」は英語化前の戻り値・MCP サーバー等の日本語のエラー
_NONZERO_EXIT = re.compile(r"^\[exit code\] (?!0\s*$)\S", re.MULTILINE)
_TRACEBACK = "Traceback (most recent call last):"


def looks_like_error(content: str) -> bool:
    """ツールの戻り値がエラー（エラーの接頭辞・0 以外の終了コード・Traceback）を示すか。"""
    head = content.lstrip()[:20].lower()
    if head.startswith(_ERROR_PREFIXES):
        return True
    return bool(_NONZERO_EXIT.search(content)) or _TRACEBACK in content
