"""execute_python_code(_readonly) の失敗に添える、直し方の短い案内。"""

from __future__ import annotations

import re

# Windows（日本語）では open() の既定の文字コードが cp932 なので、Write/Edit・スキルが書いた UTF-8 の
# ファイルを encoding なしで開くと UnicodeDecodeError になる（2026-11-07 evals xlkit run3: worker が
# 自分で Write した build_xlsx.py を open(p).read() で読んで失敗し、原因が分からずやり直していた）。
# 既定の文字コード自体は変えない（利用者の cp932 の CSV の読み書きまで変わるため）。
_LOCALE_DECODE_ERROR = re.compile(r"UnicodeDecodeError: '(cp932|shift_jis|mbcs)' codec can't decode")

DECODE_ERROR_HINT = (
    "[hint] open() without encoding reads files as cp932 here. Files written by Write/Edit, the skills and "
    "most tools are UTF-8: open them with open(path, encoding=\"utf-8\"). Use encoding=\"cp932\" only for a "
    "file you know is Shift_JIS (e.g. a CSV saved by Excel)."
)


def error_hints(stderr: str) -> list[str]:
    """stderr から分かる失敗の直し方（無ければ空）。"""
    hints = []
    if stderr and _LOCALE_DECODE_ERROR.search(stderr):
        hints.append(DECODE_ERROR_HINT)
    return hints
