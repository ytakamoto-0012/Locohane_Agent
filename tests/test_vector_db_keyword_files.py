"""キーワード索引（keyword_index.py）のコレクションごとのファイルと、ファイルごとの rowid の範囲（*_ranges）のテスト。

- 索引はコレクションごとに1ファイル（本文とファイルパスは別の表）で、書き込みのロックもファイルごと。
- FTS5 の source_path は UNINDEXED のため、ファイルの行の削除・読み出しは rowid の範囲で行う。
- 旧版の1ファイルの索引（keywords.sqlite）は、最初に開いたときにコレクションごとのファイルへ分けて消す。
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import threading

from src.vector_db import keyword_index as ki
from src.vector_db.sqlite_batch import DeferredWrites
from src.vector_db.store import ChunkRecord, path_collection_name


def _chunks(*texts: str) -> list[ChunkRecord]:
    return [ChunkRecord(str(i), t) for i, t in enumerate(texts)]


def _count(idx: ki.KeywordIndex, table: str) -> int:
    return idx._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_replace_and_delete_touch_only_the_source_rows(tmp_path: Path):
    with ki.open_keyword_index(tmp_path, "c") as idx:
        idx.replace_source("c", "a.txt", _chunks("部品A 寸法", "部品A 図面"))
        idx.replace_source("c", "b.txt", _chunks("部品B 寸法"))
        idx.replace_source("c", "a.txt", _chunks("部品A 改訂"))
        assert [r[2] for r in idx.source_rows("c", "a.txt")] == ["部品A 改訂"]
        assert [r[2] for r in idx.source_rows("c", "b.txt")] == ["部品B 寸法"]
        assert _count(idx, "body_chunks") == 2

        idx.delete_source("c", "a.txt")
        assert idx.source_rows("c", "a.txt") is None
        assert _count(idx, "body_chunks") == 1
        assert _count(idx, "body_ranges") == 1
        assert [h.source_path for h in idx.search("c", "寸法", 5)] == ["b.txt"]


def test_body_and_path_are_separate_tables(tmp_path: Path):
    with ki.open_keyword_index(tmp_path, "c") as idx:
        idx.replace_source("c", "a.txt", _chunks("配管 図面"))
        idx.replace_source(path_collection_name("c"), "a.txt", _chunks("配管/図面.txt"))
        assert [h.text for h in idx.search("c", "図面", 5)] == ["配管 図面"]
        assert [h.text for h in idx.search(path_collection_name("c"), "図面", 5)] == ["配管/図面.txt"]
        idx.drop_collection(path_collection_name("c"))
        assert idx.search(path_collection_name("c"), "図面", 5) == []
        assert idx.sources("c") == {"a.txt"} and idx.sources(path_collection_name("c")) == set()


def test_appended_pages_are_read_and_deleted_together(tmp_path: Path):
    with ki.open_keyword_index(tmp_path, "c") as idx:
        idx.delete_source("c", "big.txt")
        idx.append_source_rows("c", "big.txt", [("t1", "0", "一")])
        idx.replace_source("c", "other.txt", _chunks("他"))  # 範囲の間に別のファイルが入る
        idx.append_source_rows("c", "big.txt", [("t2", "1", "二"), ("t3", "2", "三")])
        idx.append_source_rows("c", "empty.txt", [])
        assert [r[2] for r in idx.iter_source_rows("c", "big.txt")] == ["一", "二", "三"]
        assert idx.source_rows("c", "empty.txt") == []

        idx.delete_source("c", "big.txt")
        assert [r[2] for r in idx.source_rows("c", "other.txt")] == ["他"]
        assert _count(idx, "body_chunks") == 1


def test_collections_are_separate_files_even_if_only_case_differs(tmp_path: Path):
    with ki.open_keyword_index(tmp_path, "Docs") as upper, ki.open_keyword_index(tmp_path, "docs") as lower:
        upper.replace_source("Docs", "a.txt", _chunks("大文字"))
        lower.replace_source("docs", "a.txt", _chunks("小文字"))
        assert [r[2] for r in upper.source_rows("Docs", "a.txt")] == ["大文字"]
        assert [r[2] for r in lower.source_rows("docs", "a.txt")] == ["小文字"]
    assert ki.keyword_index_path(tmp_path, "Docs") != ki.keyword_index_path(tmp_path, "docs")


def test_writing_one_collection_does_not_wait_for_another(tmp_path: Path):
    with ki.open_keyword_index(tmp_path, "a") as a, ki.open_keyword_index(tmp_path, "b") as b:
        held, release = threading.Event(), threading.Event()

        def hold_a() -> None:
            with a._write_lock():
                held.set()
                release.wait(5)

        t = threading.Thread(target=hold_a)
        t.start()
        held.wait(5)
        w = DeferredWrites(b, "kw")
        w.replace_source_rows("b", "x.txt", [("x", "0", "x")])
        w.commit()  # a のロックを持たれていても b はすぐ書ける
        release.set()
        t.join()
        assert b.sources("b") == {"x.txt"}


def test_remove_keyword_index_deletes_a_dropped_collection(tmp_path: Path):
    with ki.open_keyword_index(tmp_path, "c") as idx:
        idx.replace_source("c", "a.txt", _chunks("一"))
        idx.drop_collection("c")
        idx.drop_collection(path_collection_name("c"))
        ki.remove_keyword_index(tmp_path, "c")  # 開いている間は残す
        assert ki.keyword_index_path(tmp_path, "c").is_file()
    ki.remove_keyword_index(tmp_path, "c")
    assert not ki.keyword_index_path(tmp_path, "c").exists()


def _write_legacy(directory: Path) -> None:
    """旧版の1ファイルの索引（全コレクションの行が入った順に混ざっているもの）。"""
    conn = sqlite3.connect(directory / ki.LEGACY_KEYWORDS_FILENAME)
    conn.executescript(
        """
        CREATE VIRTUAL TABLE keyword_chunks USING fts5(
            tokens, collection UNINDEXED, source_path UNINDEXED, locator UNINDEXED, text UNINDEXED);
        CREATE TABLE keyword_sources (collection TEXT NOT NULL, source_path TEXT NOT NULL, PRIMARY KEY (collection, source_path));
        """
    )
    rows = [
        ("型番 xr", "c1", "a.txt", "0", "a0"),
        ("型番", "c1", "a.txt", "1", "a1"),
        ("型番", "C1", "b.txt", "0", "b0"),
        ("a txt", "c1.path", "a.txt", "", "a.txt"),
        ("型番", "c1", "a.txt", "2", "a2"),
    ]
    conn.executemany("INSERT INTO keyword_chunks (tokens, collection, source_path, locator, text) VALUES (?, ?, ?, ?, ?)", rows)
    conn.executemany(
        "INSERT INTO keyword_sources VALUES (?, ?)",
        [("c1", "a.txt"), ("c1", "empty.txt"), ("C1", "b.txt"), ("c1.path", "a.txt")],
    )
    conn.commit()
    conn.close()


def test_legacy_index_is_split_per_collection_on_open(tmp_path: Path):
    _write_legacy(tmp_path)
    with ki.open_keyword_index(tmp_path, "c1") as idx:
        assert [r[2] for r in idx.source_rows("c1", "a.txt")] == ["a0", "a1", "a2"]
        assert idx.source_rows("c1", "empty.txt") == []  # チャンクの無いファイルも索引済みのまま
        assert idx.sources(path_collection_name("c1")) == {"a.txt"}
        assert [h.text for h in idx.search("c1", "xr", 5)] == ["a0"]
        assert _count(idx, "body_ranges") == 1  # 別のコレクションの行が間に入っていても、分けた先では続いた範囲1つ
        idx.delete_source("c1", "a.txt")
        assert _count(idx, "body_chunks") == 0
    with ki.open_keyword_index(tmp_path, "C1") as idx:
        assert [r[2] for r in idx.source_rows("C1", "b.txt")] == ["b0"]
    assert not (tmp_path / ki.LEGACY_KEYWORDS_FILENAME).exists()
    assert not (tmp_path / "keywords.migrating").exists()


def test_interrupted_split_is_redone(tmp_path: Path):
    _write_legacy(tmp_path)
    (tmp_path / "keywords.migrating").mkdir()
    (tmp_path / "keywords.migrating" / "c1.sqlite").write_bytes(b"half written")
    with ki.open_keyword_index(tmp_path, "c1") as idx:
        assert [r[2] for r in idx.source_rows("c1", "a.txt")] == ["a0", "a1", "a2"]


def test_legacy_index_left_after_split_is_removed_later(tmp_path: Path):
    # 分け終えた後に、ほかのプロセスが開いていて旧版の索引を消せなかった状態。
    with ki.open_keyword_index(tmp_path, "c1") as idx:
        idx.replace_source("c1", "new.txt", _chunks("新"))
    _write_legacy(tmp_path)
    with ki.open_keyword_index(tmp_path, "c1") as idx:
        assert idx.sources("c1") == {"new.txt"}  # 分け直さない
    assert not (tmp_path / ki.LEGACY_KEYWORDS_FILENAME).exists()
