"""SQLite の書き込みの共通部分（カタログ・sqlite-vec・キーワード索引・取り込みジャーナルで共通）。

- 接続の busy timeout（SQLITE_TIMEOUT_SECONDS）と WAL の設定（connect・configure_connection）。
- インスタンス単位の書き込みロック（instance_write_lock）。同じインスタンスの DB（[vector_db].dir の下）へ書くときは、
  プロセス内でこのロックを取ってから書く。同じインスタンスの複数のコレクションを同時に取り込むと、SQLite の
  busy handler は待つ順番が公平でなく、運の悪い接続が待たされ続けて「database is locked」になるため、
  プロセス内では順番に並べる（別のプロセスとの競合は busy timeout とやり直し（retry_locked）で待つ）。
- まとめ書き（SqliteBatchMixin.apply_deferred）。取り込みはファイルごとに数回の書き込みをするため、1回ずつ
  コミットすると（WAL でもコミットごとにディスクへの書き出しを待つため）ファイル数が多いほど遅くなる。
  書き込みをためておき、1つの短いトランザクションでまとめて書く。
"""

from __future__ import annotations

from contextlib import contextmanager
import functools
import logging
import os
from pathlib import Path
import random
import sqlite3
import threading
import time
from typing import Callable, TypeVar

from .store import StoreStopped

logger = logging.getLogger(__name__)

# 接続の busy timeout（秒）。ほかの接続の書き込みのロックが外れるのを、この秒数まで待つ。
SQLITE_TIMEOUT_SECONDS = 120.0
# journal_mode を WAL へ切り替えるときに、ほかの接続のロックが外れるのを待つ上限（秒。各接続の timeout と同じ）。
_WAL_SWITCH_TIMEOUT_SECONDS = SQLITE_TIMEOUT_SECONDS
# retry_locked() が「database is locked」でやり直し続ける上限（秒）と、やり直すまでの間隔の最初・最大（秒）。
_RETRY_LOCKED_SECONDS = 600.0
_RETRY_FIRST_DELAY_SECONDS = 0.5
_RETRY_MAX_DELAY_SECONDS = 10.0
# retry_locked() に should_stop を渡したとき、停止を確かめる間隔（秒）と、1回の試行の busy timeout（秒。busy_timeout_for）。
_STOP_POLL_SECONDS = 0.5
_STOPPABLE_BUSY_TIMEOUT_SECONDS = 2.0

_T = TypeVar("_T")

# instance_write_lock() のロック（正規化した [vector_db].dir → ロック）。
_instance_locks: dict[str, threading.RLock] = {}
_instance_locks_guard = threading.Lock()


def instance_write_lock(directory: Path) -> threading.RLock:
    """directory（インスタンスの [vector_db].dir）の DB へ書き込むときに取る、プロセス内で共有するロック。

    同じスレッドで重ねて取れる（RLock）。
    """
    key = os.path.normcase(os.path.abspath(directory))
    with _instance_locks_guard:
        lock = _instance_locks.get(key)
        if lock is None:
            lock = _instance_locks[key] = threading.RLock()
        return lock


def connect(path: Path, **kwargs) -> sqlite3.Connection:
    """busy timeout を SQLITE_TIMEOUT_SECONDS にして path を開く。

    開いたスレッドと別のスレッドでも使えるようにする（取り込みは開いたスレッドとは別の書き込みのスレッドで書く。
    ingest._WriterThread）。同時に複数のスレッドから使わないことは呼び出し側で守る。
    """
    return sqlite3.connect(str(path), timeout=SQLITE_TIMEOUT_SECONDS, check_same_thread=False, **kwargs)


def is_locked_error(error: BaseException) -> bool:
    """ほかの接続の書き込みのロックで失敗したか（「database is locked」・SQLITE_BUSY）。"""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _stop_requested(should_stop: Callable[[], bool] | None) -> bool:
    return should_stop is not None and bool(should_stop())


def retry_locked(
    func: Callable[[], _T], *, what: str, seconds: float | None = None, should_stop: Callable[[], bool] | None = None
) -> _T:
    """func() を呼び、ほかの接続のロックで失敗したら間隔を空けて（指数的に延ばし、揺らぎを加えて）やり直す。

    func は失敗したら書きかけを巻き戻し、何度呼んでも同じ結果になるものにする。seconds 秒
    （省略時は _RETRY_LOCKED_SECONDS）たっても失敗するなら、最後のエラーをそのまま投げる。
    should_stop を渡すと、やり直しを待つ間に _STOP_POLL_SECONDS 秒ごとに確かめ、True を返したら
    StoreStopped を投げる（取り込み・コピーの「停止」が、ロックの待ちで何分も止まらないようにするため）。
    """
    deadline = time.monotonic() + (_RETRY_LOCKED_SECONDS if seconds is None else seconds)
    delay = _RETRY_FIRST_DELAY_SECONDS
    attempt = 0
    while True:
        try:
            return func()
        except sqlite3.OperationalError as e:
            attempt += 1
            if not is_locked_error(e) or time.monotonic() >= deadline:
                raise
            if _stop_requested(should_stop):
                raise StoreStopped(f"Stopped waiting for the database lock ({what}) because a stop was requested.") from e
            wait = min(delay, _RETRY_MAX_DELAY_SECONDS) * random.uniform(0.5, 1.0)
            logger.warning("vector_db: %s failed because the database is locked (%s). Retrying in %.1fs (attempt %d).", what, e, wait, attempt)
            until = time.monotonic() + wait
            while (left := until - time.monotonic()) > 0:
                time.sleep(min(left, _STOP_POLL_SECONDS))
                if _stop_requested(should_stop):
                    raise StoreStopped(f"Stopped waiting for the database lock ({what}) because a stop was requested.") from e
            delay *= 2


@contextmanager
def busy_timeout_for(conn: sqlite3.Connection, should_stop: Callable[[], bool] | None):
    """should_stop があれば、その間だけ conn の busy timeout を _STOPPABLE_BUSY_TIMEOUT_SECONDS 秒に縮める。

    busy timeout の待ちの中では停止を確かめられないため、短く待って失敗させ、retry_locked のやり直しの間に
    停止を確かめる（やり直しを続ける上限は retry_locked の seconds のまま）。
    """
    if should_stop is None:
        yield
        return
    conn.execute(f"PRAGMA busy_timeout = {int(_STOPPABLE_BUSY_TIMEOUT_SECONDS * 1000)}")
    try:
        yield
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(SQLITE_TIMEOUT_SECONDS * 1000)}")


def configure_connection(conn: sqlite3.Connection) -> None:
    """WAL にし、コミットのたびのディスクへの書き出しの待ちを減らす（synchronous=NORMAL）。

    WAL で NORMAL なら、電源断・OS の停止で直前のコミットが失われることはあっても DB は壊れない
    （取り込みはカタログを最後に書くため、失われた分は次の差分取り込みで取り込み直される）。

    まだ WAL になっていない DB（作ったばかりのファイル）を複数の接続が同時に開くと、WAL への切り替えは
    接続の timeout（busy handler）で待たずに「database is locked」になるため、ここで間隔を空けて試し直す
    （同じインスタンスの複数のコレクションの取り込みを同時に始めたとき等）。
    """
    deadline = time.monotonic() + _WAL_SWITCH_TIMEOUT_SECONDS
    delay = 0.01
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as e:
            if not is_locked_error(e) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
    conn.execute("PRAGMA synchronous=NORMAL")


class SqliteBatchMixin:
    """self._conn（self.path の DB）への書き込みの共通部分。

    書き込むメソッドは `with self._transaction():` の中で書く。apply_deferred() の中で呼ばれたときは
    そのトランザクションにまとめ、それ以外はインスタンスの書き込みロック（instance_write_lock）を取って
    その場でコミットする。
    """

    _conn: sqlite3.Connection
    path: Path
    _batching: bool = False

    @contextmanager
    def _write(self):
        """インスタンスの書き込みロックを取り、抜けるときにコミットする（例外なら巻き戻す）。"""
        with instance_write_lock(self.path.parent), self._conn:
            yield

    @contextmanager
    def _transaction(self):
        if self._batching:
            yield
            return
        with self._write():
            yield

    def apply_deferred(
        self, ops: list[Callable[[], None]], *, what: str = "write", should_stop: Callable[[], bool] | None = None
    ) -> None:
        """ops（このオブジェクトへの書き込みのメソッドの呼び出し）を、1つのトランザクションで順に行ってコミットする。

        インスタンスの書き込みロックを取り、最初に書き込みのロック（BEGIN IMMEDIATE）を取ってから書くため、
        ロックを持つのは書いている間だけ。ほかのプロセスのロックで失敗したら巻き戻し、間隔を空けて最初から
        やり直す（retry_locked。ops は何度呼んでも同じ結果になるよう、ためた値で書くものにする）。
        should_stop が True を返したら、やり直しをやめて StoreStopped を投げる（ops は書かない）。
        """
        if not ops:
            return

        def attempt() -> None:
            with instance_write_lock(self.path.parent), busy_timeout_for(self._conn, should_stop):
                if not self._conn.in_transaction:
                    self._conn.execute("BEGIN IMMEDIATE")
                self._batching = True
                try:
                    for op in ops:
                        op()
                    self._conn.commit()
                except BaseException:
                    if self._conn.in_transaction:
                        self._conn.rollback()
                    raise
                finally:
                    self._batching = False

        retry_locked(attempt, what=f"{what} ({self.path.name})", should_stop=should_stop)

    # --- collection_copy 用（begin_batch() から commit() までのトランザクションを開いたままにする） ---

    def begin_batch(self) -> None:
        self._batching = True

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def end_batch(self) -> None:
        self._conn.commit()
        self._batching = False


# DeferredWrites でためられる書き込みのメソッド（`with self._transaction():` で書き、apply_deferred のトランザクションに
# まとめられるもの）。読み取りや、`with self._write():` でその場でコミットするメソッド（drop_collection・mark_ingested 等）は
# ためられない（ためると読み取りが None を返し、まとめ書きの途中でコミットされてしまうため）。
DEFERRABLE_METHODS = frozenset({
    # 保存先（sqlite_vec_store.SqliteVecStore）
    "ensure_collection", "replace_source", "append_source", "delete_source",
    # カタログ（catalog.Catalog）
    "set_model", "upsert_file", "mark_path_indexed", "delete_file",
    # キーワード索引（keyword_index.KeywordIndex。delete_source は保存先と同じ名前）
    "replace_source_rows", "append_source_rows",
})


class DeferredWrites:
    """SqliteBatchMixin の DB への書き込みをためておき、commit()（apply()）でまとめて書く（取り込み・コピー用）。

    書き込みのメソッド（DEFERRABLE_METHODS）を呼ぶと、その場では書かずに呼び出しを記録する。commit() は記録した
    呼び出しを1つの短いトランザクションで順に行う（SqliteBatchMixin.apply_deferred）。Qdrant への送信や
    コピー元の読み出しを待つ間に書き込みのロックを持たないため。ロック競合でやり直すことがあるため、
    渡す値は後から変えないもの（リストはその場で作ったもの）にする。

    Qdrant の保存先と同じ形（begin_batch・commit・rollback・end_batch）で使えるようにしてある
    （begin_batch・end_batch は何もしない。rollback() はためた書き込みを捨てる）。
    should_stop を渡すと、ロックの待ちの間に停止を求められたら StoreStopped を投げる（retry_locked）。
    """

    def __init__(self, target: SqliteBatchMixin, what: str, *, should_stop: Callable[[], bool] | None = None):
        self.target, self.what, self.should_stop = target, what, should_stop
        self.ops: list[Callable[[], None]] = []

    def __getattr__(self, name: str):
        if name not in DEFERRABLE_METHODS:
            raise AttributeError(f"DeferredWrites cannot defer {type(self.target).__name__}.{name} (not a deferrable write method)")
        method = getattr(self.target, name)

        def defer(*args, **kwargs) -> None:
            self.ops.append(functools.partial(method, *args, **kwargs))

        return defer

    @property
    def pending(self) -> int:
        """ためている書き込みの数。"""
        return len(self.ops)

    def begin_batch(self) -> None:
        pass

    def end_batch(self) -> None:
        self.commit()

    def commit(self) -> None:
        self.target.apply_deferred(self.ops, what=self.what, should_stop=self.should_stop)
        self.ops = []

    apply = commit

    def rollback(self) -> None:
        self.ops = []

    discard = rollback
