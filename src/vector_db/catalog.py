"""コレクションの定義と取り込み済みファイルの記録（[vector_db].dir/catalog.sqlite）。

バックエンド（sqlite_vec / qdrant）によらずここに持つ。ベクター本体は store.py 側。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
import sqlite3

from .sqlite_batch import SqliteBatchMixin, configure_connection, connect

CATALOG_FILENAME = "catalog.sqlite"

# コレクション名は Qdrant のコレクション名・ログ・URL にそのまま使うため ASCII に限る。
# 日本語の表示名は description に書く。
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
# 自動取り込みの時刻は1コレクションあたりこの数まで（毎時でも24で足りる）。
MAX_SCHEDULE_TIMES = 24

_SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    source_folder TEXT NOT NULL,
    recursive INTEGER NOT NULL DEFAULT 1,
    model TEXT,
    dim INTEGER,
    document_prefix TEXT,
    created_at TEXT NOT NULL,
    last_ingested_at TEXT,
    schedule_enabled INTEGER NOT NULL DEFAULT 0,
    schedule_times TEXT NOT NULL DEFAULT '',
    schedule_weekdays TEXT NOT NULL DEFAULT '',
    exclude_keywords TEXT NOT NULL DEFAULT '',
    include_keywords TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS files (
    collection TEXT NOT NULL,
    source_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    mode TEXT NOT NULL DEFAULT 'text',
    path_indexed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (collection, source_path)
);
-- 別のインスタンスのコレクションの参照（複製せずに検索対象に加える。references.py・search.py）。
CREATE TABLE IF NOT EXISTS collection_refs (
    name TEXT PRIMARY KEY,
    source_instance TEXT NOT NULL,
    source_collection TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class CatalogError(Exception):
    """コレクション名が不正・存在しない・重複している等。"""


@dataclass(frozen=True)
class Collection:
    name: str
    description: str
    # 取り込み元フォルダ（1つ以上。catalog.sqlite の source_folder 列に改行区切りで持つ）。
    source_folders: tuple[str, ...]
    recursive: bool
    model: str | None
    dim: int | None
    created_at: str
    last_ingested_at: str | None
    file_count: int = 0
    chunk_count: int = 0
    # 取り込みに使った [vector_db].document_prefix（None は記録の無い旧版で作ったもの）。
    document_prefix: str | None = None
    # 時間指定の自動取り込み（差分）。時刻は "HH:MM"、曜日は 0=月〜6=日で空なら毎日（admin/knowledge.py の IngestScheduler）。
    schedule_enabled: bool = False
    schedule_times: tuple[str, ...] = ()
    schedule_weekdays: tuple[int, ...] = ()
    # 取り込み除外キーワード（取り込み元フォルダより下のパスに含むフォルダ・ファイルは取り込まない。
    # catalog.sqlite の exclude_keywords 列に改行区切りで持つ）。
    exclude_keywords: tuple[str, ...] = ()
    # 取り込み対象キーワード（あれば、取り込み元フォルダより下のパスにどれかを含むファイルだけを取り込む。
    # catalog.sqlite の include_keywords 列に改行区切りで持つ）。
    include_keywords: tuple[str, ...] = ()

    @property
    def source_folder(self) -> str:
        """最初の取り込み元フォルダ（1フォルダだけだった旧版の API との互換用）。"""
        return self.source_folders[0]


# 大規模な取り込みでは数十万件をメモリーに持つため、slots にして1件あたりのメモリーを減らす。
@dataclass(frozen=True, slots=True)
class FileRecord:
    """取り込み済みファイルの記録。mode は埋め込み方（"text"・"text+image"・"text+embedded"・"path"、ingest.file_mode()）。

    path_indexed はファイルパスのベクター（store.path_collection_name）を入れ済みか
    （False は導入前に取り込んだファイルで、次の取り込みでパスだけを埋め込む）。
    """

    source_path: str
    size: int
    mtime: float
    sha256: str
    chunk_count: int
    mode: str = "text"
    path_indexed: bool = False


@dataclass(frozen=True)
class Reference:
    """別のインスタンス source_instance のコレクション source_collection を、name の名前で検索対象に加える参照。"""

    name: str
    source_instance: str
    source_collection: str
    created_at: str


def validate_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise CatalogError(f"Collection names must be 1 to 48 characters of letters, digits, hyphens and underscores: {name!r}")
    return name


def normalize_source_folders(folders: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """取り込み元フォルダ（文字列なら改行区切り）の前後の空白・空行を除き、同じフォルダの重複を除く（順序は保つ）。"""
    if isinstance(folders, str):
        folders = folders.splitlines()
    result: list[str] = []
    seen: set[str] = set()
    for raw in folders:
        value = raw.strip()
        if not value:
            continue
        key = os.path.normcase(os.path.normpath(value))
        if key not in seen:
            seen.add(key)
            result.append(value)
    if not result:
        raise CatalogError("Specify the source folder.")
    return tuple(result)


def normalize_path_keywords(keywords: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """取り込み除外・対象キーワード（文字列なら改行区切り）の前後の空白・空行と、大文字小文字だけ違う重複を除く（順序は保つ。空でもよい）。"""
    if isinstance(keywords, str):
        keywords = keywords.splitlines()
    result: list[str] = []
    seen: set[str] = set()
    for raw in keywords:
        value = raw.strip()
        if not value:
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return tuple(result)


def normalize_schedule_times(times: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """自動取り込みの時刻（"H:MM"・"HH:MM"）を検証し、"HH:MM" に揃えて重複を除き昇順にする。"""
    result: set[str] = set()
    for raw in times:
        value = raw.strip()
        if not value:
            continue
        m = _TIME_RE.match(value)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise CatalogError(f"Scheduled ingestion times must be in the 00:00 to 23:59 format: {value!r}")
        result.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    if len(result) > MAX_SCHEDULE_TIMES:
        raise CatalogError(f"Up to {MAX_SCHEDULE_TIMES} scheduled ingestion times are allowed.")
    return tuple(sorted(result))


def normalize_schedule_weekdays(weekdays: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    """自動取り込みの曜日（0=月〜6=日）を検証し、重複を除き昇順にする。空は毎日。"""
    for d in weekdays:
        if not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6:
            raise CatalogError(f"Scheduled ingestion days must be 0 (Mon) to 6 (Sun): {d!r}")
    return tuple(sorted(set(weekdays)))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Catalog(SqliteBatchMixin):
    """catalog.sqlite への読み書き。1インスタンスを1スレッドで使う。"""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / CATALOG_FILENAME
        self._conn = connect(self.path)
        self._conn.row_factory = sqlite3.Row
        configure_connection(self._conn)
        self._conn.executescript(_SCHEMA)
        # mode 列の無い旧版の catalog.sqlite に列を足す（既存の記録はテキストとして取り込んだもの）。
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(files)")}
        if "mode" not in columns:
            with self._write():
                self._conn.execute("ALTER TABLE files ADD COLUMN mode TEXT NOT NULL DEFAULT 'text'")
        # path_indexed 列の無い旧版の catalog.sqlite に列を足す（既存のファイルは次の取り込みでパスを埋め込む）。
        if "path_indexed" not in columns:
            with self._write():
                self._conn.execute("ALTER TABLE files ADD COLUMN path_indexed INTEGER NOT NULL DEFAULT 0")
        # document_prefix 列の無い旧版の catalog.sqlite に列を足す（既存のコレクションは NULL ＝ 不明）。
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(collections)")}
        if "document_prefix" not in columns:
            with self._write():
                self._conn.execute("ALTER TABLE collections ADD COLUMN document_prefix TEXT")
        # 自動取り込みの列の無い旧版の catalog.sqlite に列を足す（既存のコレクションは自動取り込みなし）。
        for column, ddl in (
            ("schedule_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("schedule_times", "TEXT NOT NULL DEFAULT ''"),
            ("schedule_weekdays", "TEXT NOT NULL DEFAULT ''"),
            # 取り込み除外キーワードの列（既存のコレクションは除外なし）。
            ("exclude_keywords", "TEXT NOT NULL DEFAULT ''"),
            # 取り込み対象キーワードの列（既存のコレクションは絞り込みなし）。
            ("include_keywords", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in columns:
                with self._write():
                    self._conn.execute(f"ALTER TABLE collections ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- コレクション ---

    def list_collections(self) -> list[Collection]:
        rows = self._conn.execute(
            """
            SELECT c.*, COUNT(f.source_path) AS file_count, COALESCE(SUM(f.chunk_count), 0) AS chunk_count
            FROM collections c LEFT JOIN files f ON f.collection = c.name
            GROUP BY c.name ORDER BY c.name
            """
        ).fetchall()
        return [_to_collection(r) for r in rows]

    def get(self, name: str) -> Collection:
        for c in self.list_collections():
            if c.name == name:
                return c
        raise CatalogError(f"Collection {name} does not exist.")

    def create(
        self,
        name: str,
        source_folders: str | list[str] | tuple[str, ...],
        description: str = "",
        recursive: bool = True,
        exclude_keywords: str | list[str] | tuple[str, ...] = (),
        include_keywords: str | list[str] | tuple[str, ...] = (),
    ) -> Collection:
        """source_folders は取り込み元フォルダのリスト（文字列なら改行区切りで複数指定できる）。exclude_keywords・include_keywords も同じ。"""
        validate_name(name)
        folders = normalize_source_folders(source_folders)
        excludes = normalize_path_keywords(exclude_keywords)
        includes = normalize_path_keywords(include_keywords)
        if self._reference_row(name) is not None:
            raise CatalogError(f"A reference named {name} already exists.")
        try:
            with self._write():
                self._conn.execute(
                    "INSERT INTO collections (name, description, source_folder, recursive, exclude_keywords, include_keywords, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, description.strip(), "\n".join(folders), int(recursive), "\n".join(excludes), "\n".join(includes), _now()),
                )
        except sqlite3.IntegrityError as e:
            raise CatalogError(f"Collection {name} already exists.") from e
        return self.get(name)

    def update(
        self,
        name: str,
        *,
        source_folders: str | list[str] | tuple[str, ...] | None = None,
        description: str | None = None,
        recursive: bool | None = None,
        schedule_enabled: bool | None = None,
        schedule_times: list[str] | tuple[str, ...] | None = None,
        schedule_weekdays: list[int] | tuple[int, ...] | None = None,
        exclude_keywords: str | list[str] | tuple[str, ...] | None = None,
        include_keywords: str | list[str] | tuple[str, ...] | None = None,
    ) -> Collection:
        current = self.get(name)
        times = normalize_schedule_times(schedule_times) if schedule_times is not None else current.schedule_times
        weekdays = normalize_schedule_weekdays(schedule_weekdays) if schedule_weekdays is not None else current.schedule_weekdays
        enabled = schedule_enabled if schedule_enabled is not None else current.schedule_enabled
        if enabled and not times:
            raise CatalogError("Specify at least one time to enable scheduled ingestion.")
        folders = normalize_source_folders(source_folders) if source_folders is not None else None
        with self._write():
            if schedule_enabled is not None or schedule_times is not None or schedule_weekdays is not None:
                self._conn.execute(
                    "UPDATE collections SET schedule_enabled = ?, schedule_times = ?, schedule_weekdays = ? WHERE name = ?",
                    (int(enabled), ",".join(times), ",".join(str(d) for d in weekdays), name),
                )
            if folders is not None:
                self._conn.execute("UPDATE collections SET source_folder = ? WHERE name = ?", ("\n".join(folders), name))
            if description is not None:
                self._conn.execute("UPDATE collections SET description = ? WHERE name = ?", (description.strip(), name))
            if recursive is not None:
                self._conn.execute("UPDATE collections SET recursive = ? WHERE name = ?", (int(recursive), name))
            if exclude_keywords is not None:
                excludes = normalize_path_keywords(exclude_keywords)
                self._conn.execute("UPDATE collections SET exclude_keywords = ? WHERE name = ?", ("\n".join(excludes), name))
            if include_keywords is not None:
                includes = normalize_path_keywords(include_keywords)
                self._conn.execute("UPDATE collections SET include_keywords = ? WHERE name = ?", ("\n".join(includes), name))
        return self.get(name)

    def delete(self, name: str) -> None:
        with self._write():
            self._conn.execute("DELETE FROM files WHERE collection = ?", (name,))
            self._conn.execute("DELETE FROM collections WHERE name = ?", (name,))

    def set_model(self, name: str, model: str | None, dim: int | None, document_prefix: str | None = None) -> None:
        with self._transaction():
            self._conn.execute(
                "UPDATE collections SET model = ?, dim = ?, document_prefix = ? WHERE name = ?", (model, dim, document_prefix, name)
            )

    def mark_ingested(self, name: str) -> None:
        with self._write():
            self._conn.execute("UPDATE collections SET last_ingested_at = ? WHERE name = ?", (_now(), name))

    def set_last_ingested(self, name: str, value: str | None) -> None:
        """最終取り込み日時をそのまま書く（インスタンス間のコピーでコピー元の日時を写す）。"""
        with self._write():
            self._conn.execute("UPDATE collections SET last_ingested_at = ? WHERE name = ?", (value, name))

    # --- 別のインスタンスのコレクションの参照 ---

    def _reference_row(self, name: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM collection_refs WHERE name = ?", (name,)).fetchone()

    def list_references(self) -> list[Reference]:
        rows = self._conn.execute("SELECT * FROM collection_refs ORDER BY name").fetchall()
        return [Reference(**dict(r)) for r in rows]

    def get_reference(self, name: str) -> Reference:
        row = self._reference_row(name)
        if row is None:
            raise CatalogError(f"Reference {name} does not exist.")
        return Reference(**dict(row))

    def add_reference(self, name: str, source_instance: str, source_collection: str) -> Reference:
        validate_name(name)
        validate_name(source_collection)
        if self._conn.execute("SELECT 1 FROM collections WHERE name = ?", (name,)).fetchone():
            raise CatalogError(f"Collection {name} already exists.")
        try:
            with self._write():
                self._conn.execute(
                    "INSERT INTO collection_refs (name, source_instance, source_collection, created_at) VALUES (?, ?, ?, ?)",
                    (name, source_instance, source_collection, _now()),
                )
        except sqlite3.IntegrityError as e:
            raise CatalogError(f"A reference named {name} already exists.") from e
        return self.get_reference(name)

    def delete_reference(self, name: str) -> None:
        with self._write():
            self._conn.execute("DELETE FROM collection_refs WHERE name = ?", (name,))

    # --- 取り込み済みファイル ---

    def files(self, name: str) -> dict[str, FileRecord]:
        rows = self._conn.execute(
            "SELECT source_path, size, mtime, sha256, chunk_count, mode, path_indexed FROM files WHERE collection = ?", (name,)
        )
        # 全行をリストにしてから作らず1行ずつ作り、キーと記録で同じ文字列を使う（数十万件のコレクションでもメモリーを抑える）。
        result: dict[str, FileRecord] = {}
        for source, size, mtime, sha256, chunk_count, mode, path_indexed in rows:
            result[source] = FileRecord(source, size, mtime, sha256, chunk_count, mode, bool(path_indexed))
        return result

    def upsert_file(self, name: str, record: FileRecord) -> None:
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO files (collection, source_path, size, mtime, sha256, chunk_count, mode, path_indexed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (collection, source_path) DO UPDATE SET
                    size = excluded.size, mtime = excluded.mtime, sha256 = excluded.sha256, chunk_count = excluded.chunk_count,
                    mode = excluded.mode, path_indexed = excluded.path_indexed
                """,
                (
                    name, record.source_path, record.size, record.mtime, record.sha256, record.chunk_count, record.mode,
                    int(record.path_indexed),
                ),
            )

    def mark_path_indexed(self, name: str, source_path: str) -> None:
        with self._transaction():
            self._conn.execute("UPDATE files SET path_indexed = 1 WHERE collection = ? AND source_path = ?", (name, source_path))

    def delete_file(self, name: str, source_path: str) -> None:
        with self._transaction():
            self._conn.execute("DELETE FROM files WHERE collection = ? AND source_path = ?", (name, source_path))

    def clear_files(self, name: str) -> None:
        with self._write():
            self._conn.execute("DELETE FROM files WHERE collection = ?", (name,))


def _to_collection(row: sqlite3.Row) -> Collection:
    return Collection(
        name=row["name"],
        description=row["description"],
        source_folders=tuple(f for f in row["source_folder"].split("\n") if f),
        recursive=bool(row["recursive"]),
        model=row["model"],
        dim=row["dim"],
        created_at=row["created_at"],
        last_ingested_at=row["last_ingested_at"],
        file_count=row["file_count"],
        chunk_count=row["chunk_count"],
        document_prefix=row["document_prefix"],
        schedule_enabled=bool(row["schedule_enabled"]),
        schedule_times=tuple(t for t in row["schedule_times"].split(",") if t),
        schedule_weekdays=tuple(int(d) for d in row["schedule_weekdays"].split(",") if d),
        exclude_keywords=tuple(k for k in row["exclude_keywords"].split("\n") if k),
        include_keywords=tuple(k for k in row["include_keywords"].split("\n") if k),
    )


def open_catalog(directory: Path) -> Catalog:
    return Catalog(directory)


def catalog_exists(directory: Path) -> bool:
    """catalog.sqlite が既にあるか（無ければ作らずに「コレクション無し」と扱いたい読み取り側用）。"""
    return (directory / CATALOG_FILENAME).is_file()


__all__ = [
    "Catalog",
    "CatalogError",
    "Collection",
    "FileRecord",
    "Reference",
    "catalog_exists",
    "normalize_path_keywords",
    "normalize_schedule_times",
    "normalize_schedule_weekdays",
    "normalize_source_folders",
    "open_catalog",
    "validate_name",
]
