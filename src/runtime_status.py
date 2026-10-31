"""実行中の状態（接続中セッション・生成中スレッド）をファイルへ書き出す。

設定ダッシュボード（admin/、別プロセス）が「各インスタンスの実行中ユーザー・
生成中スレッド」を表示するために読む。接続中セッションや生成中スレッドは
本体プロセスのメモリ上にしか無いため、app.py が数秒おきにスナップショットを
<common_data_dir>/runtime_status.json へ書き出す。

HTTPエンドポイントで公開しない理由: 管理ツールと本体の間で認証用の秘密を
共有する仕組みが無く（app.bat で直接起動した場合は管理ツールが起動に関与しない）、
本体のポートは 0.0.0.0 で公開されうるため。ファイルなら同じマシン上の
管理ツールだけが読める。

Chainlit には依存しない（収集は呼び出し側の app.py が行う）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RUNTIME_STATUS_FILENAME = "runtime_status.json"
# 書き出しの PermissionError がこの回数連続したら警告する（run_writer_loop 参照）。
_PERMISSION_FAILURES_BEFORE_WARNING = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_atomic(path: Path, data: dict) -> None:
    """一時ファイルへ書いてから置き換える（管理ツールが書きかけを読まないように）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to delete %s", path, exc_info=True)


async def run_writer_loop(path: Path, collect: Callable[[], dict], interval_seconds: float) -> None:
    """collect() の結果が前回から変わったときだけ path へ書き出し続ける。

    再接続（Chainlit はタイムアウトまでセッションを保持し、再接続時は
    on_chat_start を呼ばない）のようにフックでは捕捉できない変化も拾えるよう、
    イベント駆動ではなく定期的に収集する。

    Args:
        path: 書き出し先（<common_data_dir>/runtime_status.json）。
        collect: スナップショット（dict）を返す関数。updated_at は含めない
            （変化の有無の比較に使うため。書き出し時にここで付ける）。
        interval_seconds: 収集間隔（秒）。
    """
    previous: dict | None = None
    # Windows では管理ツールがファイルを読んでいる瞬間に os.replace すると
    # PermissionError（WinError 5）になる。previous を更新しなければ次の周期で
    # 書き直されるため、一時的な衝突では警告を出さない（警告ログは管理ツールの
    # 「24時間の警告」件数や monitor-app-log の起票対象になるため）。
    permission_failures = 0
    while True:
        try:
            snapshot = collect()
            if snapshot != previous:
                write_atomic(path, {**snapshot, "updated_at": now_iso()})
                previous = snapshot
            permission_failures = 0
        except asyncio.CancelledError:
            raise
        except PermissionError:
            permission_failures += 1
            logger.log(
                logging.WARNING if permission_failures == _PERMISSION_FAILURES_BEFORE_WARNING else logging.DEBUG,
                "Failed to write the runtime status file %s (%d times in a row)",
                path,
                permission_failures,
                exc_info=True,
            )
        except Exception:  # noqa: BLE001 - 監視用の付帯機能のため本体の動作は止めない
            logger.warning("Failed to write the runtime status file %s", path, exc_info=True)
        await asyncio.sleep(interval_seconds)
