"""本文のチャンクのキーワード索引（[vector_db].dir/keywords/<コレクション名>.sqlite、SQLite FTS5 の BM25）。

意味の近さで探す埋め込みは、型番・品番・数値のような完全一致の語句に弱いため、
キーワードでも探して search.py で埋め込みの結果と順位で統合する。
保存先（backend = sqlite_vec / qdrant）によらず、常にこの SQLite に持つ。

索引はコレクションごとに1ファイルで、本文（body_*）とファイルパス（path_*。store.path_collection_name の
コレクション）を別の表に持つ。全コレクションを1つの表に入れると、検索は語に当たった行を全コレクション分
読んでから絞るため、コレクションが増えるほど遅くなり、書き込みもコレクション間でロックを待ち合うため。
旧版の1ファイルの索引（keywords.sqlite）は読まない（更新後は各コレクションを再構築する。CHANGELOG 参照）。

日本語は単語に分けずに使えるよう、漢字・かな等の連続を2文字ずつ（バイグラム）に区切り、
英数字は語ごとに小文字にして索引に入れる（_tokens）。ひらがなだけの2文字（「の幅」でなく
「する」「ます」等）は助詞・語尾で、どの文書にも現れて順位を乱すため除く。
"""

from __future__ import annotations

import logging
from pathlib import Path
import re
import threading
from typing import Iterable, Iterator
import unicodedata

from .sqlite_batch import SqliteBatchMixin, configure_connection, connect, instance_write_lock
from .store import ChunkRecord, Hit, path_collection_name

logger = logging.getLogger(__name__)

# コレクションごとの索引のファイルを置くフォルダ（[vector_db].dir の下）。
KEYWORDS_DIRNAME = "keywords"

# 本文・ファイルパスの表の接頭辞。
_BODY, _PATH = "body", "path"


def _schema(t: str) -> list[str]:
    return [
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {t}_chunks USING fts5(tokens, source_path UNINDEXED, locator UNINDEXED, text UNINDEXED)",
        # 索引に入れたファイル（チャンクの無いファイルも記録し、取り込みで索引の作り直しの要否に使う）。
        f"CREATE TABLE IF NOT EXISTS {t}_sources (source_path TEXT PRIMARY KEY)",
        # ファイルごとの {t}_chunks の rowid の範囲（1回の書き込みで1行）。FTS5 の source_path は UNINDEXED で、
        # それで絞ると索引全体を読むため、ファイルの行の削除・読み出しはこの範囲の rowid で行う。
        f"CREATE TABLE IF NOT EXISTS {t}_ranges (source_path TEXT NOT NULL, first_rowid INTEGER NOT NULL, last_rowid INTEGER NOT NULL)",
        f"CREATE INDEX IF NOT EXISTS {t}_ranges_source ON {t}_ranges (source_path)",
    ]


_SCHEMA = _schema(_BODY) + _schema(_PATH)

# 英数字の語（型番の "ABC-123" は "abc" と "123" に分かれる。どちらも語として一致する）。
_WORD_RE = re.compile(r"[0-9a-z]+")
_HIRAGANA_RE = re.compile(r"^[ぁ-ゟ]+$")


def _cjk_runs(text: str) -> list[str]:
    """英数字以外の文字（漢字・かな等。文字種が字・数字のもの）の連続を取り出す。長音「ー」も続ける。"""
    runs: list[str] = []
    current: list[str] = []
    for ch in text:
        if not ch.isascii() and (unicodedata.category(ch)[0] in "LN" or ch == "ー"):
            current.append(ch)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def _tokens(text: str) -> list[str]:
    """text を索引・検索に使う語の一覧にする（重複を含む。BM25 の語の頻度に使う）。

    全角英数字・半角カナは NFKC で正規化してから区切る。
    """
    text = unicodedata.normalize("NFKC", text).lower()
    tokens = _WORD_RE.findall(text)
    for run in _cjk_runs(text):
        grams = [run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)]
        tokens.extend(g for g in grams if not _HIRAGANA_RE.match(g))
    return tokens


def _match_query(query: str) -> str | None:
    """検索語を FTS5 の MATCH 式（語の OR）にする。語が無ければ None。"""
    unique = list(dict.fromkeys(_tokens(query)))
    if not unique:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in unique)


def iter_token_rows(chunks: list[ChunkRecord], *, index_texts: list[str] | None = None) -> Iterator[tuple[str, str, str]]:
    """chunks を索引の行 (語, locator, text) にする（KeywordIndex.replace_source_rows に渡す形）。

    index_texts を渡すと、語はチャンクの text ではなくこちら（chunks と同じ順）から作る。
    取り込みは、書き込みのロックを取る前にこれで語に区切っておく（区切る間ロックを持たないため）。
    """
    texts = index_texts if index_texts is not None else [c.text for c in chunks]
    if len(texts) != len(chunks):
        raise ValueError(f"index_texts ({len(texts)}) and chunks ({len(chunks)}) differ in length")
    return ((" ".join(_tokens(t)), c.locator, c.text) for c, t in zip(chunks, texts))


def _file_name(collection: str) -> str:
    """コレクション（catalog.validate_name の英数字・"-"・"_"）の索引のファイル名。

    Windows はファイル名の大文字小文字を区別しないため、大文字は "^" + 小文字にして "Docs" と "docs" を分ける。
    Windows の予約名（con・nul・com1 等。Windows 10 では拡張子を付けてもデバイスとして扱われる）は "~" を前に付ける
    （"~" はコレクション名に使えないため、ほかのコレクションのファイル名と重ならない）。
    """
    stem = "".join("^" + c.lower() if c.isupper() else c for c in collection)
    if stem in _WINDOWS_RESERVED_NAMES:
        stem = "~" + stem
    return stem + ".sqlite"


# Windows でファイル名（拡張子を除いた部分）に使えない予約名。
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(10)}
)


def keyword_index_path(directory: Path, collection: str) -> Path:
    """コレクション collection（"<名前>.path" ではなく利用者のコレクション名）の索引のファイル。"""
    return directory / KEYWORDS_DIRNAME / _file_name(collection)


def keyword_index_exists(directory: Path, collection: str) -> bool:
    """collection の索引があるか。"""
    return keyword_index_path(directory, collection).is_file()


class KeywordIndex(SqliteBatchMixin):
    """コレクション collection の索引（本文は collection、ファイルパスは path_collection_name(collection) で指す）。"""

    def __init__(self, directory: Path, collection: str):
        self.collection = collection
        self.path = keyword_index_path(directory, collection)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect(self.path)
        configure_connection(self._conn)
        self._conn.executescript(";\n".join(_SCHEMA) + ";")

    def _write_lock(self) -> threading.RLock:
        # 書き込みのロックはファイルごと（別のコレクションの書き込みを待たない）。
        return instance_write_lock(self.path)

    def _table(self, collection: str) -> str:
        if collection == self.collection:
            return _BODY
        if collection == path_collection_name(self.collection):
            return _PATH
        raise ValueError(f"The keyword index of {self.collection!r} does not hold collection {collection!r}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "KeywordIndex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def has_source(self, collection: str, source_path: str) -> bool:
        t = self._table(collection)
        return self._conn.execute(f"SELECT 1 FROM {t}_sources WHERE source_path = ?", (source_path,)).fetchone() is not None

    def sources(self, collection: str) -> set[str]:
        """collection の索引に入れたファイルの一覧（取り込みで has_source() をファイルごとに呼ばずに済むよう、まとめて読む）。"""
        t = self._table(collection)
        return {r[0] for r in self._conn.execute(f"SELECT source_path FROM {t}_sources")}

    def replace_source(
        self, collection: str, source_path: str, chunks: list[ChunkRecord], *, index_texts: list[str] | None = None
    ) -> None:
        """source_path のチャンクを丸ごと入れ替える（chunks が空でも索引済みとして記録する）。

        index_texts を渡すと、索引の語はチャンクの text ではなくこちら（chunks と同じ順）から作る
        （検索結果の text はチャンクの text のまま）。
        """
        # 行はジェネレーターで渡し、チャンクの多いファイルでも全チャンクの語の一覧を一度にメモリーに作らない。
        self.replace_source_rows(collection, source_path, iter_token_rows(chunks, index_texts=index_texts))

    def source_rows(self, collection: str, source_path: str) -> list[tuple[str, str, str]] | None:
        """source_path の索引の行 (tokens, locator, text) を入れた順に返す（索引に無ければ None。インスタンス間のコピー用）。"""
        rows = self.iter_source_rows(collection, source_path)
        return None if rows is None else list(rows)

    def iter_source_rows(self, collection: str, source_path: str) -> Iterator[tuple[str, str, str]] | None:
        """source_rows() と同じ行を、全部をリストにせず順に返す（索引に無ければ None）。

        数十万チャンクの1ファイルでも、語の一覧をまとめてメモリーに読まないためのもの（インスタンス間のコピー用）。
        返したイテレーターは、この接続で書き込む前に読み切ること（replace_source_rows には別の接続のものを渡す）。
        """
        if not self.has_source(collection, source_path):
            return None
        t = self._table(collection)
        return self._iter_rows(t, self._ranges(t, source_path))

    def _iter_rows(self, t: str, ranges: list[tuple[int, int]]) -> Iterator[tuple[str, str, str]]:
        for first, last in ranges:
            cursor = self._conn.execute(
                f"SELECT tokens, locator, text FROM {t}_chunks WHERE rowid BETWEEN ? AND ? ORDER BY rowid", (first, last)
            )
            for r in cursor:
                yield (r[0], r[1], r[2])

    def _ranges(self, t: str, source_path: str) -> list[tuple[int, int]]:
        """source_path の {t}_chunks の rowid の範囲 (最初, 最後) を入れた順に返す。"""
        rows = self._conn.execute(
            f"SELECT first_rowid, last_rowid FROM {t}_ranges WHERE source_path = ? ORDER BY first_rowid", (source_path,)
        )
        return [(r[0], r[1]) for r in rows]

    def _begin(self) -> None:
        if not self._conn.in_transaction:
            self._conn.execute("BEGIN IMMEDIATE")

    def _insert_rows(self, t: str, source_path: str, rows: Iterable[tuple[str, str, str]]) -> None:
        """rows を {t}_chunks の末尾の rowid に続けて入れ、その範囲を {t}_ranges に記録する。"""
        # 最後の rowid を読んでから入れるまでに、ほかの接続に書かれないようにする。
        self._begin()
        last = self._conn.execute(f"SELECT rowid FROM {t}_chunks ORDER BY rowid DESC LIMIT 1").fetchone()
        first = (last[0] if last else 0) + 1
        count = 0

        def numbered() -> Iterator[tuple[int, str, str, str, str]]:
            nonlocal count
            for tokens, locator, text in rows:
                yield (first + count, tokens, source_path, locator, text)
                count += 1

        self._conn.executemany(
            f"INSERT INTO {t}_chunks (rowid, tokens, source_path, locator, text) VALUES (?, ?, ?, ?, ?)", numbered()
        )
        if count:
            self._conn.execute(
                f"INSERT INTO {t}_ranges (source_path, first_rowid, last_rowid) VALUES (?, ?, ?)",
                (source_path, first, first + count - 1),
            )

    def replace_source_rows(self, collection: str, source_path: str, rows: Iterable[tuple[str, str, str]]) -> None:
        """source_rows()・iter_source_rows() で読んだ行で source_path を丸ごと入れ替える（語に区切り直さない）。"""
        t = self._table(collection)
        with self._transaction():
            self._delete_source(t, source_path)
            self._insert_rows(t, source_path, rows)
            self._conn.execute(f"INSERT INTO {t}_sources (source_path) VALUES (?)", (source_path,))

    def append_source_rows(self, collection: str, source_path: str, rows: Iterable[tuple[str, str, str]]) -> None:
        """source_path の索引に rows（source_rows() の形）を足す（rows が空でも索引済みとして記録する）。

        replace_source_rows を数百件ずつに分けて書くためのもの（先に delete_source で消しておく。コピー用）。
        """
        t = self._table(collection)
        with self._transaction():
            self._insert_rows(t, source_path, rows)
            self._conn.execute(f"INSERT OR IGNORE INTO {t}_sources (source_path) VALUES (?)", (source_path,))

    def _delete_source(self, t: str, source_path: str) -> None:
        self._begin()
        # 新しいファイル（範囲が無い）なら {t}_chunks は読まない。
        for first, last in self._ranges(t, source_path):
            self._conn.execute(f"DELETE FROM {t}_chunks WHERE rowid BETWEEN ? AND ?", (first, last))
        self._conn.execute(f"DELETE FROM {t}_ranges WHERE source_path = ?", (source_path,))
        self._conn.execute(f"DELETE FROM {t}_sources WHERE source_path = ?", (source_path,))

    def delete_source(self, collection: str, source_path: str) -> None:
        t = self._table(collection)
        with self._transaction():
            self._delete_source(t, source_path)

    def drop_collection(self, collection: str) -> None:
        """collection の索引を空にする（表を消して作り直す。行の数によらずすぐ終わる）。"""
        t = self._table(collection)
        with self._write():
            self._begin()
            for table in (f"{t}_chunks", f"{t}_ranges", f"{t}_sources"):
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            for statement in _schema(t):
                self._conn.execute(statement)

    def search(self, collection: str, query: str, top_k: int) -> list[Hit]:
        """query の語を含むチャンクを BM25 の高い順に最大 top_k 件返す。

        Hit.score は 0.0（BM25 の値はコサイン類似度と比べられないため載せない。順位だけを統合に使う）。
        """
        t = self._table(collection)
        match = _match_query(query)
        if match is None:
            return []
        rows = self._conn.execute(
            f"SELECT source_path, locator, text FROM {t}_chunks WHERE {t}_chunks MATCH ? ORDER BY bm25({t}_chunks) LIMIT ?",
            (match, int(top_k)),
        ).fetchall()
        return [Hit(collection=collection, score=0.0, source_path=p, locator=loc, text=t_) for p, loc, t_ in rows]


def open_keyword_index(directory: Path, collection: str) -> KeywordIndex:
    """コレクション collection の索引を開く（無ければ作る）。"""
    return KeywordIndex(directory, collection)


def remove_keyword_index(directory: Path, collection: str) -> None:
    """削除したコレクションの索引のファイルを消す（中身は drop_collection で消しておく）。

    ほかの接続が開いている（-wal・-shm がある）ときは消さずに残す（空の索引のため害は無く、同じ名前で作り直せば使う）。
    """
    path = keyword_index_path(directory, collection)
    if any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm")):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.info("vector_db: could not remove the keyword index %s (%s); leaving it empty.", path, e)
