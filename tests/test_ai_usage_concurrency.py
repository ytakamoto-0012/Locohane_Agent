"""使用量の台帳（admin/ai_usage.py）への並列書き込み（2026-11-14 ユーザー指示: SQLite の並列書き込みは入念に）。

スキル調整ワーカーは判定を複数スレッドで同時に行い、管理ツール・別のワーカーも同じファイルへ書く。
加算が1件も欠けないこと、ロックされていても使用量を捨てずに持ち越すことを確かめる。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from admin import ai_usage

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _total(root: Path, source: str = "lab") -> tuple[int, int]:
    with sqlite3.connect(str(ai_usage.db_path(root))) as conn:
        row = conn.execute("SELECT COALESCE(SUM(total), 0), COALESCE(SUM(calls), 0) FROM ai_usage_monthly WHERE source = ?", (source,)).fetchone()
    return int(row[0]), int(row[1])


def test_many_threads_never_lose_an_increment(tmp_path):
    errors = []

    def work(owner: str) -> None:
        try:
            for _ in range(30):
                ai_usage.record(tmp_path, "lab", owner, {"total_tokens": 2, "input_tokens": 1, "output_tokens": 1})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(f"u{i % 3}",)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert _total(tmp_path) == (16 * 30 * 2, 16 * 30)


def test_threads_and_processes_together(tmp_path):
    """管理ツール・別のワーカー（別プロセス）と、ワーカーの判定スレッドが同時に書く。"""
    code = (
        "import sys, pathlib; sys.path.insert(0, sys.argv[1]);"
        "from admin import ai_usage;"
        "root = pathlib.Path(sys.argv[2]);"
        "[ai_usage.record(root, 'lab', 'proc', {'total_tokens': 2}) for _ in range(40)]"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, str(_PROJECT_ROOT), str(tmp_path)]) for _ in range(4)]
    errors = []

    def work() -> None:
        try:
            for _ in range(40):
                ai_usage.record(tmp_path, "lab", "thread", {"total_tokens": 2})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    assert errors == []
    assert _total(tmp_path) == ((4 * 40 + 4 * 40) * 2, 8 * 40)


def test_usage_that_could_not_be_written_is_carried_over(tmp_path, monkeypatch):
    """別の接続が書き込みロックを持っている間は書けない。その分は捨てず、次に書けたとき一緒に加算する。"""
    monkeypatch.setattr(ai_usage, "_BUSY_TIMEOUT_MS", 50)
    monkeypatch.setattr(ai_usage, "_RETRY_DELAYS", (0.01,))
    ai_usage.record(tmp_path, "lab", "u", {"total_tokens": 1})  # 表を作っておく
    blocker = sqlite3.connect(str(ai_usage.db_path(tmp_path)), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        try:
            ai_usage.record(tmp_path, "lab", "u", {"total_tokens": 10}, calls=2)
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("must fail while the database is locked")
        try:
            ai_usage.record(tmp_path, "lab", "other", {"total_tokens": 100})
        except sqlite3.OperationalError:
            pass
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert _total(tmp_path) == (1, 1)  # ロック中は何も書けていない
    ai_usage.record(tmp_path, "lab", "u", {"total_tokens": 1000})  # 書けたとき、持ち越した分（10 と別の行の 100）も一緒に入る
    assert _total(tmp_path) == (1 + 10 + 100 + 1000, 1 + 2 + 1 + 1)
    assert not ai_usage._PENDING


def test_record_safely_does_not_raise_and_readers_still_work(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_usage, "_BUSY_TIMEOUT_MS", 50)
    monkeypatch.setattr(ai_usage, "_RETRY_DELAYS", ())
    ai_usage.record(tmp_path, "lab", "u", {"total_tokens": 1})
    blocker = sqlite3.connect(str(ai_usage.db_path(tmp_path)), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        ai_usage.record_safely(tmp_path, "lab", "u", {"total_tokens": 5})  # 例外を出さない（本来の処理を止めない）
        # 読み取り専用の接続（利用量・料金の画面）は書き込み中でも読める（ジャーナルモードを変えていない）
        with sqlite3.connect(f"file:{ai_usage.db_path(tmp_path).as_posix()}?mode=ro", uri=True, timeout=1) as ro:
            assert ro.execute("SELECT COUNT(*) FROM ai_usage_monthly").fetchone()[0] == 1
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    ai_usage.record(tmp_path, "lab", "u", {"total_tokens": 7})
    assert _total(tmp_path)[0] == 1 + 5 + 7


def test_database_deleted_while_running_is_recreated(tmp_path):
    ai_usage.record(tmp_path, "lab", "u", {"total_tokens": 1})
    ai_usage.db_path(tmp_path).unlink()
    ai_usage.record(tmp_path, "lab", "u", {"total_tokens": 4})
    assert _total(tmp_path) == (4, 1)
