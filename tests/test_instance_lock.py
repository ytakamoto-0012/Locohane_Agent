"""src/instance_lock.py（多重起動防止の排他ロック）の回帰テスト。"""

from __future__ import annotations

import msvcrt

import pytest

import app
from src import instance_lock


@pytest.fixture(autouse=True)
def _release_after_each_test():
    """各テスト後に本プロセスが保持したロックを必ず解放し、次のテストへ持ち越さない。"""
    yield
    instance_lock.release()


def test_acquire_creates_lock_file(tmp_path):
    lock_path = tmp_path / "app.lock"
    instance_lock.acquire(lock_path)
    assert lock_path.exists()


def test_second_acquire_in_same_process_fails(tmp_path):
    """同一プロセス内でも、既にロック中のバイト範囲を再ロックしようとすると失敗する
    （別プロセスからの二重起動を模擬するのに十分な検証になる。実際の二重起動
    シナリオはOS側の排他制御そのものであり、プロセスをまたいでも同じAPIが
    同じ理由で失敗する）。
    """
    lock_path = tmp_path / "app.lock"
    instance_lock.acquire(lock_path)

    # 別プロセスを模して、独立したファイルハンドルから同じロックを取りにいく。
    f = open(lock_path, "r+b")
    try:
        with pytest.raises(OSError):
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    finally:
        f.close()


def test_release_allows_reacquire(tmp_path):
    lock_path = tmp_path / "app.lock"
    instance_lock.acquire(lock_path)
    instance_lock.release()

    # 解放後は同じパスへ再取得できる（プロセス終了時にOSが自動解放するのと同じ効果）。
    instance_lock.acquire(lock_path)


def test_release_without_acquire_is_noop():
    instance_lock.release()


def test_acquire_creates_parent_directory(tmp_path):
    lock_path = tmp_path / "nested" / "data" / "app.lock"
    instance_lock.acquire(lock_path)
    assert lock_path.exists()


class _StartupAborted(Exception):
    """テスト用のos._exit()差し替え先が送出する、起動中止を表すセンチネル例外。"""


@pytest.mark.asyncio
async def test_on_app_startup_treats_any_instance_lock_failure_as_fatal(monkeypatch) -> None:
    """instance_lock.acquire() が InstanceAlreadyRunningError 以外の OSError
    （mkdir/write_bytes/open() 由来の権限不足・ディスク満杯等、
    msvcrt.locking() 以外の箇所で発生しうる）を送出した場合も、二重起動検知時
    と同じく os._exit(1) で起動を中止することを確認する（2026-08-26レビュー:
    InstanceAlreadyRunningError だけを捕まえていたため、それ以外のOSErrorが
    chainlit本体のwrap_user_function内のexceptに素通りして飲み込まれ、
    ロックを一切保持しないまま起動が継続してしまう不具合の回帰防止）。
    """

    def fake_acquire(lock_path):
        raise OSError("disk full")

    monkeypatch.setattr(app.instance_lock, "acquire", fake_acquire)

    exit_calls = []

    def fake_exit(code):
        exit_calls.append(code)
        raise _StartupAborted()

    monkeypatch.setattr(app.os, "_exit", fake_exit)

    with pytest.raises(_StartupAborted):
        await app._on_app_startup()

    assert exit_calls == [1]


def test_acquire_retries_transient_lock_failure(tmp_path, monkeypatch):
    """管理ツールの is_locked() が一瞬ロックを取った瞬間と重なって1回目が失敗しても、
    再試行で取得できる（未起動なのに多重起動と誤判定しない）。"""
    lock_path = tmp_path / "app.lock"
    real_locking = msvcrt.locking
    calls = {"n": 0}

    def flaky_locking(fd, mode, nbytes):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real_locking(fd, mode, nbytes)

    monkeypatch.setattr(instance_lock.msvcrt, "locking", flaky_locking)
    monkeypatch.setattr(instance_lock, "_ACQUIRE_RETRY_INTERVAL_SECONDS", 0)
    instance_lock.acquire(lock_path)
    monkeypatch.setattr(instance_lock.msvcrt, "locking", real_locking)
    assert calls["n"] == 2
