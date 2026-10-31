"""取り込みジャーナル（[vector_db].dir/ingest_journal.sqlite）。保存先へ書きかけのファイルを記録する。

取り込み・コピーは、保存先（Qdrant・sqlite-vec）→ キーワード索引 → カタログの順にコミットする。Qdrant は
コミットを待たずに 256 件ごとに送り、ファイルを分けて書き込む（ingest._Job.stream）ため、その途中で
強制終了されると、保存先には書きかけ・書き換えたチャンクがあるのに、カタログは前の記録のまま（または記録が無い）になる。
そのままだと、記録の無いファイルがフォルダから消えてもチャンクが消されず、内容を元に戻したファイルは
「内容が同じ」として埋め込み直されない。

そこで、保存先へ書き始める前にファイルをここへ即時にコミットして記録し（add）、カタログへの記録までコミットしたら
消す（discard）。次の取り込みの開始時に残っているファイルは書きかけのもので、ingest._reconcile_journal が
カタログに記録があれば必ず取り込み直させ、無ければ保存先・キーワード索引から消す。

カタログ等とは別のファイル・別の接続にし、カタログ等へためた書き込み（sqlite_batch.DeferredWrites）とは別に、
その場でコミットする。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from .sqlite_batch import busy_timeout_for, configure_connection, connect, instance_write_lock, retry_locked

JOURNAL_FILENAME = "ingest_journal.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS writing (
    collection TEXT NOT NULL,
    source_path TEXT NOT NULL,
    PRIMARY KEY (collection, source_path)
);
-- 再構築・削除で保存先を消している途中のコレクション（begin_drop）。
CREATE TABLE IF NOT EXISTS dropping (
    collection TEXT PRIMARY KEY
);
"""


class IngestJournal:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / JOURNAL_FILENAME
        # 自動コミット（isolation_level=None）。add() はその場でディスクへ書けていないと意味が無いため。
        self._conn = connect(self.path, isolation_level=None)
        configure_connection(self._conn)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "IngestJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _write(self, what: str, statements: list[tuple[str, list[tuple]]], should_stop: Callable[[], bool] | None = None) -> None:
        """statements（SQL とそのパラメーターの一覧）を1つのトランザクションでその場でコミットする。

        インスタンスの書き込みロック（instance_write_lock）の中で書き、ほかのプロセスのロックで失敗したら
        巻き戻して間隔を空けてやり直す（retry_locked。should_stop が True を返したら StoreStopped を投げる）。
        COMMIT が失敗したときも巻き戻す（トランザクションを開いたままにすると、やり直しの BEGIN が失敗するため）。
        """

        def attempt() -> None:
            with instance_write_lock(self.path.parent), busy_timeout_for(self._conn, should_stop):
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for sql, params in statements:
                        self._conn.executemany(sql, params)
                    self._conn.execute("COMMIT")
                except BaseException:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise

        retry_locked(attempt, what=f"journal {what}", should_stop=should_stop)

    def add(self, collection: str, source_path: str, *, should_stop: Callable[[], bool] | None = None) -> None:
        """source_path を保存先へ書き始める（その場でコミットする）。"""
        self._write(
            "add", [("INSERT OR IGNORE INTO writing (collection, source_path) VALUES (?, ?)", [(collection, source_path)])], should_stop
        )

    def discard(self, collection: str, source_paths: Iterable[str], *, should_stop: Callable[[], bool] | None = None) -> None:
        """カタログへの記録までコミットしたファイルを消す（まとめて1回でコミットする）。"""
        params = [(collection, s) for s in source_paths]
        if params:
            self._write("discard", [("DELETE FROM writing WHERE collection = ? AND source_path = ?", params)], should_stop)

    def clear(self, collection: str) -> None:
        """collection の記録をすべて消す（再構築・コレクションの削除で、保存先・カタログまで消し終えたとき）。"""
        self._write(
            "clear",
            [
                ("DELETE FROM writing WHERE collection = ?", [(collection,)]),
                ("DELETE FROM dropping WHERE collection = ?", [(collection,)]),
            ],
        )

    def begin_drop(self, collection: str) -> None:
        """collection の保存先・キーワード索引・カタログの記録を消し始める（その場でコミットする）。

        途中で止まると、カタログに記録が残ったまま保存先のベクターが消えている（差分取り込みは記録を見て
        埋め込まない）ため、消し終えて clear() するまで drop_pending() が True を返し、次の取り込みが作り直す。
        """
        self._write("begin_drop", [("INSERT OR IGNORE INTO dropping (collection) VALUES (?)", [(collection,)])])

    def drop_pending(self, collection: str) -> bool:
        """前回の再構築・削除が、消している途中で止まったままか。"""
        return self._conn.execute("SELECT 1 FROM dropping WHERE collection = ?", (collection,)).fetchone() is not None

    def sources(self, collection: str) -> list[str]:
        """collection で書きかけのまま残っているファイル。"""
        rows = self._conn.execute("SELECT source_path FROM writing WHERE collection = ? ORDER BY source_path", (collection,))
        return [r[0] for r in rows]


def open_journal(directory: Path) -> IngestJournal:
    return IngestJournal(directory)


__all__ = ["JOURNAL_FILENAME", "IngestJournal", "open_journal"]
