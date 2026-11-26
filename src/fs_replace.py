"""一時ファイルを本来の名前へ置き換える os.replace の、Windows の一時的な拒否に強い版。

Windows では、置き換え先を別のプロセス（本体の設定の読み直し・管理ツール・ウイルス対策・インデクサー・
エディタ）が一瞬開いていると、os.replace が PermissionError（WinError 5）になる。直前に置き換えたばかりの
ファイルで起きやすい（並列の Edit のテストが負荷の高いときに失敗した）。少し待って数回やり直す。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# やり直すまでの待ち（秒）。合計 1.5 秒ほど。
RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def replace_with_retry(src: str | os.PathLike[str], dst: str | os.PathLike[str], delays: tuple[float, ...] | None = None) -> None:
    """os.replace(src, dst)。PermissionError なら delays の間隔でやり直し、最後の失敗はそのまま投げる。"""
    for delay in RETRY_DELAYS if delays is None else delays:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(Path(src), Path(dst))
