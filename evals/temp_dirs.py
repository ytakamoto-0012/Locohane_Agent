"""評価の一時フォルダ（[skill_lab].temp_dir。既定 ${common_data_dir}/evals_temp）。

評価（run_all.py・run_case.py）とスキル研究室の AI 修正は、OS の一時フォルダ（%TEMP%）を使わず、
評価対象インスタンスのデータフォルダの下に一時フォルダを作る。OS の一時フォルダでは、強制終了・
異常終了で残ったものがどこにあるか管理できないため（2026-11-13 ユーザー要望）。

残ったものは、次の評価の開始時に cleanup_stale() が消す（最後に更新されてから
[skill_lab].temp_retention_hours を過ぎたものだけ。実行中の別の評価のフォルダは消さない）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _latest_mtime(path: Path) -> float:
    """path（フォルダなら中身も含む）の最後の更新時刻。"""
    latest = path.stat().st_mtime
    if path.is_dir():
        for base, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    latest = max(latest, (Path(base) / name).stat().st_mtime)
                except OSError:
                    continue
    return latest


def cleanup_stale(root: Path, retention_hours: float, *, now: float | None = None) -> list[Path]:
    """root 直下の、最後に更新されてから retention_hours を過ぎたものを消す。消したものを返す。

    retention_hours が 0 以下なら何もしない。消せないもの（使用中等）は飛ばす。
    """
    if retention_hours <= 0 or not root.is_dir():
        return []
    limit = (time.time() if now is None else now) - retention_hours * 3600
    removed = []
    for entry in root.iterdir():
        try:
            if _latest_mtime(entry) >= limit:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed.append(entry)
        except OSError:
            continue
    return removed


def prepare(root: Path, retention_hours: float) -> Path:
    """一時フォルダの置き場を作り、残っている古いものを消して返す。"""
    root.mkdir(parents=True, exist_ok=True)
    cleanup_stale(root, retention_hours)
    return root


def make_dir(root: Path, prefix: str) -> Path:
    """root の下に新しい一時フォルダを作って返す（消すのは呼び出し側）。"""
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def remove(path: Path | None) -> None:
    """一時フォルダを消す（消せなくても止めない。残ったものは cleanup_stale が後で消す）。"""
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def temporary(root: Path, prefix: str) -> Iterator[str]:
    """tempfile.TemporaryDirectory の代わり（root の下に作り、抜けるときに消す）。"""
    path = make_dir(root, prefix)
    try:
        yield str(path)
    finally:
        remove(path)


async def cleanup_loop(root: Path, retention_hours: float, interval_hours: float) -> None:
    """interval_hours ごとに cleanup_stale を繰り返す（本体の app.py がバックグラウンドで動かす）。

    評価が動かない間も、強制終了で残ったものを片付けるため（評価の開始時の片付けだけでは、
    次に評価するまで残り続ける）。
    """
    import asyncio
    import logging

    while True:
        await asyncio.sleep(max(interval_hours, 0.01) * 3600)
        try:
            removed = await asyncio.to_thread(cleanup_stale, root, retention_hours)
            if removed:
                logging.getLogger(__name__).info("Removed %d leftover evaluation temp folder(s) in %s", len(removed), root)
        except Exception as e:  # noqa: BLE001 - 片付けの失敗で本体を止めない
            logging.getLogger(__name__).warning("Failed to clean up evaluation temp folders in %s: %s", root, e)


def config_root(config) -> Path:
    """インスタンスの設定（src.config.Config）から、古いものを片付けた置き場を返す。"""
    return prepare(config.skill_lab_temp_dir, config.skill_lab_temp_retention_hours)
