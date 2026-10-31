"""ディレクトリ配下の期限切れファイルを定期的に削除する汎用ユーティリティ。

添付・埋め込み画像（data/elements/<thread_id>/）やパスメモリーのレジストリファイル
（data/path_memory/<thread_id>.json、app.py が起動時・定期的に呼ぶ）など、
「日数ベースの保持期間を過ぎたファイルを自動削除する」という同じパターンを
複数箇所で使うため、ここに共通実装を置く。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_old_files(directory: Path, retention_days: int, pattern: str = "*") -> int:
    """directory 直下の、更新日時が retention_days より古いファイルを削除する。

    Args:
        directory: チェック対象のディレクトリ。
        retention_days: 保持日数。この日数を過ぎた（更新日時が古い）ファイルを
            削除する。0以下を指定した場合は何もせず 0 を返す（無効化）。
        pattern: 対象を絞る glob パターン（既定 "*" は directory 直下の全
            ファイルが対象、従来どおりの挙動）。複数種類のファイルが同居する
            ディレクトリで一部だけを対象にしたい場合に使う
            （例: log_dir で "app_*.log" のみを対象にし、他のログを除外する）。

    Returns:
        削除したファイル数。
    """
    if retention_days <= 0:
        return 0

    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for path in directory.glob(pattern):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
                logger.info("Deleted expired file: %s", path)
        except OSError:
            logger.warning("Failed to delete file: %s", path, exc_info=True)
    return deleted


def cleanup_old_dirs(directory: Path, retention_days: int, pattern: str = "*") -> int:
    """directory 直下の、更新日時が retention_days より古いサブディレクトリを丸ごと削除する。

    `cleanup_old_files` はファイルのみを対象にするため、`_tmp_<thread_id>`
    （src/tools.py の `_resolve_exec_workdir()` が作る execute_python_code の
    実行用ディレクトリ）のような、ディレクトリ単位の一時生成物を掃除する
    ための別実装。本来は `on_chat_end`（app.py）でセッション終了時に
    即時削除されるが、異常終了でそのフックが発火しなかった場合の保険として、
    起動時に一度だけ呼ぶ想定（`default_workdir` 配下のみが対象。ユーザー
    指定の work_dir はここでは触らない）。

    Args:
        directory: チェック対象のディレクトリ。
        retention_days: 保持日数。0以下を指定した場合は何もせず 0 を返す。
        pattern: 対象を絞る glob パターン（既定 "*"）。

    Returns:
        削除したディレクトリ数。
    """
    if retention_days <= 0:
        return 0

    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for path in directory.glob(pattern):
        if not path.is_dir():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
                deleted += 1
                logger.info("Deleted expired directory: %s", path)
        except OSError:
            logger.warning("Failed to delete directory: %s", path, exc_info=True)
    return deleted


def cleanup_old_files_in_subdirs(directory: Path, retention_days: int, pattern: str = "*") -> int:
    """directory 直下の各サブディレクトリの中身に対し、ファイル単位で cleanup_old_files を適用する。

    config.elements_dir/<thread_id>/ のように、スレッドIDなどキーごとの
    サブフォルダに実ファイルがフラットに溜まる構成向け。サブフォルダ
    自体の更新日時（＝直近の追加日時）で一括判定してしまうと、
    使い続けているスレッドの古いファイルがいつまでも消えないため、
    ファイルごとの更新日時で個別に削除する。削除の結果サブフォルダが
    空になった場合は、そのサブフォルダも削除する。

    Args:
        directory: チェック対象の親ディレクトリ（直下にサブフォルダが並ぶ）。
        retention_days: 保持日数。0以下を指定した場合は何もせず 0 を返す。
        pattern: 各サブフォルダ内で対象を絞る glob パターン（既定 "*"）。

    Returns:
        削除したファイル数の合計。
    """
    if retention_days <= 0:
        return 0

    deleted = 0
    for sub in directory.iterdir():
        if not sub.is_dir():
            continue
        deleted += cleanup_old_files(sub, retention_days, pattern)
        try:
            next(sub.iterdir())
        except StopIteration:
            try:
                sub.rmdir()
                logger.info("Deleted empty subfolder: %s", sub)
            except OSError:
                logger.warning("Failed to delete empty subfolder: %s", sub, exc_info=True)
        except OSError:
            logger.warning("Failed to check subfolder contents: %s", sub, exc_info=True)
    return deleted


async def run_cleanup_loop(
    directory: Path, retention_days: int, interval_hours: float, pattern: str = "*"
) -> None:
    """interval_hours 間隔で cleanup_old_files を回し続ける常駐タスク。

    asyncio.create_task() でアプリ起動時に1回だけ起動する想定。
    キャンセルされるまで戻らない。

    Args:
        directory: チェック対象のディレクトリ。
        retention_days: 保持日数。0以下の場合は即座に return する。
        interval_hours: チェック間隔（時間）。
        pattern: cleanup_old_files に渡す glob パターン（既定 "*"）。

    Returns:
        None。
    """
    if retention_days <= 0:
        return

    interval_seconds = interval_hours * 3600
    while True:
        await asyncio.sleep(interval_seconds)
        cleanup_old_files(directory, retention_days, pattern)


async def run_cleanup_dirs_loop(
    directory: Path, retention_days: int, interval_hours: float, pattern: str = "*"
) -> None:
    """interval_hours 間隔で cleanup_old_dirs を回し続ける常駐タスク。

    run_cleanup_loop のディレクトリ単位版（Chainlitのセッションファイル
    配信ディレクトリ `.files/<session_id>/` のように、ファイルではなく
    サブディレクトリ単位で溜まっていくものを対象にする場合に使う）。

    Args:
        directory: チェック対象のディレクトリ。
        retention_days: 保持日数。0以下の場合は即座に return する。
        interval_hours: チェック間隔（時間）。
        pattern: cleanup_old_dirs に渡す glob パターン（既定 "*"）。

    Returns:
        None。
    """
    if retention_days <= 0:
        return

    interval_seconds = interval_hours * 3600
    while True:
        await asyncio.sleep(interval_seconds)
        cleanup_old_dirs(directory, retention_days, pattern)


async def run_cleanup_files_in_subdirs_loop(
    directory: Path, retention_days: int, interval_hours: float, pattern: str = "*"
) -> None:
    """interval_hours 間隔で cleanup_old_files_in_subdirs を回し続ける常駐タスク。

    Args:
        directory: チェック対象の親ディレクトリ（直下にサブフォルダが並ぶ）。
        retention_days: 保持日数。0以下の場合は即座に return する。
        interval_hours: チェック間隔（時間）。
        pattern: cleanup_old_files_in_subdirs に渡す glob パターン（既定 "*"）。

    Returns:
        None。
    """
    if retention_days <= 0:
        return

    interval_seconds = interval_hours * 3600
    while True:
        await asyncio.sleep(interval_seconds)
        cleanup_old_files_in_subdirs(directory, retention_days, pattern)
