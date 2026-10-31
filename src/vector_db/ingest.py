"""コレクションの取り込み元フォルダを走査して、差分だけをベクターDBへ取り込む。

使い方（CLI）:
    python -m src.vector_db.ingest --instance default --collection manuals [--rebuild]

コレクション自体の作成は設定ダッシュボードの「ナレッジ」タブ（admin/knowledge.py）で行う。
"""

from __future__ import annotations

import argparse
from array import array
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field, replace
import hashlib
import heapq
import itertools
import logging
import os
import shutil
import stat
from pathlib import Path
import tempfile
import threading
import time
import zipfile
from typing import Callable, Collection, NamedTuple, Sequence

import httpx

from ..config import Config
from .catalog import Catalog, CatalogError, FileRecord, open_catalog
from .chunking import split_text
from .embedded_images import EMBEDDED_IMAGE_EXTENSIONS, extract_embedded_images
from .embeddings import EmbeddingStopped, embed_images, embed_texts, embedding_model, open_client
from .extract import SUPPORTED_EXTENSIONS, ExtractError, NotTextError, extract_segments, watch_stop_file
from .gc_pacer import GcPacer
from .journal import IngestJournal, open_journal
from .keyword_index import KeywordIndex, iter_token_rows, open_keyword_index
from .render import IMAGE_FILE_EXTENSIONS, PageImage, close_office_apps, keep_office_apps, render_pages
from .sqlite_batch import DeferredWrites, SqliteBatchMixin
from .store import ChunkRecord, StoreStopped, image_collection_name, open_store, path_collection_name, storage_collection_names

logger = logging.getLogger(__name__)

# 進捗の通知先: (処理済みファイル数, 全ファイル数, 今のファイルパス)
ProgressCallback = Callable[[int, int, str], None]


class IngestError(Exception):
    """取り込みを始められない（フォルダが無い・埋め込みモデルが変わった等）。"""


@dataclass
class IngestResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    # failed のうち [vector_db].max_file_mb を超えて取り込まなかったファイルの数（設定で対処できることを画面に出すため）。
    too_large: int = 0
    # [vector_db].max_files_per_collection を超えたため取り込まなかったファイルの数と、そのときの上限・見つかったファイル数。
    over_limit: int = 0
    max_files: int = 0
    found_files: int = 0
    # added・updated のうち、中身を埋め込まずにファイルパスだけを登録したファイルの数
    # （image_input = false の画像ファイルと、本文を取り出せなかったファイル）。
    path_only: int = 0

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "chunks": self.chunks,
            "path_only": self.path_only,
            "failed": [{"path": p, "reason": r} for p, r in self.failed],
            "too_large": self.too_large,
            "over_limit": self.over_limit,
            "max_files": self.max_files,
            "found_files": self.found_files,
        }


def _over_limit_hint(result: IngestResult) -> str:
    """ファイル数の上限を超えたときの説明（CLI の表示用。画面は admin/static/knowledge.js が同じ内容を出す）。"""
    return (
        f"The source has {result.found_files} files, which exceeds the per-collection limit (config.ini [vector_db] "
        f"max_files_per_collection = {result.max_files}), so {result.over_limit} files were not ingested "
        "(already ingested files take priority, and the rest are chosen from the start in path order). "
        "Increase [vector_db] max_files_per_collection (\"Admin settings\" of the settings dashboard, or config.ini) "
        "and run the ingestion (diff) again to ingest them "
        "(while ingesting, the list of target files is kept in memory, so watch the memory). "
        "You can also split collections by source folder."
    )


def _too_large_hint(cfg: Config, count: int) -> str:
    """上限サイズを超えたファイルへの対処方法（CLI の表示用。画面は admin/static/knowledge.js が同じ内容を出す）。"""
    return (
        f"{count} files exceeded the size limit (config.ini [vector_db] max_file_mb = {cfg.vector_db_max_file_mb}MB, a Locohane limit) "
        "and were not ingested. Increase max_file_mb (\"Admin settings\" of the settings dashboard, or config.ini) "
        "and run the ingestion (diff) again to ingest them."
    )


SLOT_KINDS = ("load", "embed")
# 枠が空くのを待っている間に、停止の求めを見直す間隔（秒）。
_SLOT_POLL_SECONDS = 1.0
# 走査・準備・埋め込みの完了を待っている間に、停止の求めを見直す間隔（秒）。
_STOP_POLL_SECONDS = 0.5
# 取り込みの統計（_IngestStats）をログに出す間隔（秒）。
_STATS_INTERVAL_SECONDS = 60.0


def _process_memory_mb() -> float | None:
    """このプロセスが使っている物理メモリー（MB。Windows はワーキングセット、Linux は RSS。分からなければ None）。"""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            get_info = ctypes.WinDLL("psapi").GetProcessMemoryInfo
            get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
            if not get_info(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return None
            return counters.WorkingSetSize / 1024 / 1024
        with open("/proc/self/statm", encoding="ascii") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
    except Exception:  # noqa: BLE001 - 統計のログ用。取れなければ出さない
        return None


class _IngestStats:
    """1つの取り込み（_Pipeline）の埋め込みの送り方の統計。_STATS_INTERVAL_SECONDS 秒ごとにログへ1行出す。

    in-flight（送信中のリクエストの数）は時間で重み付けした平均と最大。並列数（ingest_embed_requests）を
    使い切れているか、準備・書き込みの待ちで止まっていないかを、取り込みのログから確かめるためのもの。
    """

    def __init__(self):
        self.started = self.last = time.monotonic()
        self.requests = self.items = 0
        self.in_flight_sum = 0.0
        self.in_flight_max = 0

    def sample(self, in_flight: int) -> None:
        """前回からの経過時間の間、送信中のリクエストが in_flight 個だったとして足す。"""
        now = time.monotonic()
        self.in_flight_sum += in_flight * (now - self.last)
        self.in_flight_max = max(self.in_flight_max, in_flight)
        self.last = now

    def done(self, items: int) -> None:
        self.requests += 1
        self.items += items

    def due(self) -> bool:
        return self.last - self.started >= _STATS_INTERVAL_SECONDS

    def log(self, pipeline: "_Pipeline") -> None:
        """ここまでの統計をログに出し、数え直す。"""
        seconds = max(self.last - self.started, 1e-9)
        memory = _process_memory_mb()
        logger.info(
            "vector_db ingest stats: instance=%s collection=%s requests=%.1f/s items=%.1f/s in_flight avg=%.1f max=%d limit=%d "
            "shared_embed_slots=%d/%d queued texts=%d images=%d waiting_files=%d loading=%d write_queue=%d done=%d/%d memory=%s",
            pipeline.instance or "-", pipeline.writer.name, self.requests / seconds, self.items / seconds,
            self.in_flight_sum / seconds, self.in_flight_max, pipeline.embed_limit,
            pipeline.slots.in_use("embed"), pipeline.slots.capacity("embed"),
            len(pipeline.text_queue), len(pipeline.image_queue), pipeline.waiting, len(pipeline.loads), pipeline.writer.backlog(),
            pipeline.completed, pipeline.total, "-" if memory is None else f"{memory:.0f}MB",
        )
        self.started = self.last
        self.requests = self.items = 0
        self.in_flight_sum = 0.0
        self.in_flight_max = 0


class _SlotUser:
    """IngestSlots を使う1つの取り込み（_Pipeline）。

    hungry: 種類ごとに、枠が空かずに始められない処理があるか。その種類の枠を自分より少なく使っている取り込みが
        hungry の間は、その種類の枠を取らない（空いた枠は使っている数の少ない取り込みへ回し、取り込み中の
        コレクションに均等に分ける）。
    wakeup: 枠が空いたときに結果を入れる Future（_Pipeline は自分の処理の完了と一緒にこれを待つ）。
    embedding_files: 埋め込み中のファイルの状態の一覧を返す関数（_Pipeline.embedding_files。snapshot() の "files"）。
    """

    def __init__(self, instance: str, collection: str):
        self.instance, self.collection = instance, collection
        self.hungry = dict.fromkeys(SLOT_KINDS, False)
        self.failed: set[str] = set()
        self.wakeup: Future = Future()
        self.embedding_files: Callable[[], list[dict]] | None = None


class IngestSlots:
    """取り込みの並列数の枠（"load": 準備するファイル、"embed": 埋め込みのリクエスト）。

    同じ IngestSlots を渡した取り込み同士で共有する。管理ツール（admin/knowledge.py）は1つを全インスタンス・
    全コレクションの取り込みで共有し、config.ini の [vector_db] ingest_workers・ingest_processes・ingest_embed_requests を
    全体の並列数にする（コレクションごとに並列数分ずつ動くと、埋め込みサーバー・PC に負荷がかかりすぎるため）。
    枠を超える処理は空き待ちになり、空いた枠はその種類の枠を使っている数の少ない取り込みから使う（取り込み中の
    コレクションに均等に分ける。埋め込みサーバーのスループットが上限のとき、1つのコレクションが枠を占めて
    ほかのコレクションが進まなくならないようにするため）。空きを待つ取り込みが無ければ、1つの取り込みが全部を使える。

    snapshot() は各枠の状態と埋め込み中のファイル（設定ダッシュボードのワーカーの表示用）。
    """

    def __init__(self, workers: int, processes: int = 0, requests: int = 0):
        self._lock = threading.Lock()
        self._users: list[_SlotUser] = []
        # 種類ごとの使用中の枠: 枠の番号 → (使っている取り込み, 処理中のファイル等)。
        self._used: dict[str, dict[int, tuple[_SlotUser, str]]] = {kind: {} for kind in SLOT_KINDS}
        self.workers, self.processes, self.requests = workers, processes, requests

    def resize(self, workers: int, processes: int, requests: int = 0) -> None:
        """並列数を変える（設定の変更を反映する。動いている取り込みのスレッド・子プロセスの数は変わらない）。"""
        with self._lock:
            self.workers, self.processes, self.requests = workers, processes, requests
            self._wake_hungry()

    def capacity(self, kind: str) -> int:
        """kind の枠の数（準備は子プロセスを使うならその数、埋め込みは ingest_embed_requests（0 なら ingest_workers））。"""
        if kind == "load":
            return self.processes or self.workers
        return self.requests or self.workers

    def available_for(self, user: _SlotUser, kind: str) -> int:
        """user が kind の枠を均等な取り分の範囲で取れる数の目安（空きと、取り分の残りの少ない方）。

        取り分は、その種類の枠を使っている・空きを待っている取り込み（user を含む）の数で枠を均等に分けた数。
        """
        with self._lock:
            used = self._used[kind]
            free = max(0, self.capacity(kind) - len(used))
            counts = self._counts(kind)
            sharing = {id(u) for u in self._users if u.hungry[kind] or counts.get(id(u))} | {id(user)}
            share = -(-self.capacity(kind) // len(sharing))
            return max(0, min(free, share - counts.get(id(user), 0)))

    def in_use(self, kind: str) -> int:
        """kind の使用中の枠の数。"""
        with self._lock:
            return len(self._used[kind])

    def _counts(self, kind: str) -> dict[int, int]:
        # 取り込み（id(_SlotUser)）ごとの kind の使用中の枠の数。
        counts: dict[int, int] = {}
        for u, _ in self._used[kind].values():
            counts[id(u)] = counts.get(id(u), 0) + 1
        return counts

    def join(self, instance: str, collection: str) -> _SlotUser:
        user = _SlotUser(instance, collection)
        with self._lock:
            self._users.append(user)
        return user

    def leave(self, user: _SlotUser) -> None:
        with self._lock:
            if user in self._users:
                self._users.remove(user)
            self._wake_hungry()

    def begin_round(self, user: _SlotUser) -> None:
        """_Pipeline の1回の処理の開始（end_round() までに枠を取れなかった種類だけを hungry のままにする）。"""
        with self._lock:
            user.failed = set()
            if user.wakeup.done():
                user.wakeup = Future()

    def end_round(self, user: _SlotUser) -> None:
        with self._lock:
            changed = False
            for kind in SLOT_KINDS:
                hungry = kind in user.failed
                changed |= user.hungry[kind] and not hungry
                user.hungry[kind] = hungry
            if changed:
                self._wake_hungry()

    def pause(self, user: _SlotUser) -> None:
        """user の空き待ち（hungry）を取り下げる（しばらく枠を取りに行けないとき。_Pipeline._pause_slot_wait）。

        取りに行かない取り込みが空き待ちのまま残ると、それより多く枠を使っている取り込みは空いた枠を取らずに
        待ち続け、枠が使われないまま空くため。次に枠を取れなかったとき（try_acquire）に、また空き待ちになる。
        """
        with self._lock:
            if any(user.hungry.values()):
                user.hungry = dict.fromkeys(SLOT_KINDS, False)
                self._wake_hungry()

    def try_acquire(self, user: _SlotUser, kind: str, detail: str) -> int | None:
        """kind の枠を1つ取る（取れたら枠の番号、空きが無い・自分より使っている枠の少ない取り込みが空きを待っているなら None）。

        使っている数が同じ取り込み同士は先に試した方が取る（取った方が多くなるため、次の空きは相手に回る）。
        """
        with self._lock:
            used = self._used[kind]
            free = next((i for i in range(self.capacity(kind)) if i not in used), None)
            counts = self._counts(kind)
            mine = counts.get(id(user), 0)
            poorer = any(u is not user and u.hungry[kind] and counts.get(id(u), 0) < mine for u in self._users)
            if free is None or poorer:
                user.failed.add(kind)
                user.hungry[kind] = True
                return None
            used[free] = (user, detail)
            return free

    def release(self, kind: str, index: int) -> None:
        with self._lock:
            self._used[kind].pop(index, None)
            self._wake_hungry()

    def _wake_hungry(self) -> None:
        # 誰が枠を取れるかは try_acquire() で決めるため、空きを待っている取り込みをすべて起こす。
        for u in self._users:
            if any(u.hungry.values()) and not u.wakeup.done():
                u.wakeup.set_result(None)

    def snapshot(self) -> dict:
        """各枠の状態（使用中なら instance・collection・detail、空きなら None）、埋め込み中のファイル（files。
        _Pipeline.embedding_files に instance・collection を付けたもの）と、空きを待っている取り込み。"""
        with self._lock:
            result: dict = {"workers": self.workers, "processes": self.processes, "requests": self.capacity("embed")}
            for kind in SLOT_KINDS:
                used = self._used[kind]
                size = max([self.capacity(kind), *(i + 1 for i in used)])
                result[kind] = [
                    {"instance": used[i][0].instance, "collection": used[i][0].collection, "detail": used[i][1]} if i in used else None
                    for i in range(size)
                ]
            users = list(self._users)
            result["waiting"] = [
                {"instance": u.instance, "collection": u.collection}
                for u in users
                if any(u.hungry.values()) and not any(u is v for used in self._used.values() for v, _ in used.values())
            ]
        # 埋め込み中のファイルは各取り込みが自分のロックで返す（枠のロックの外で呼ぶ）。
        result["files"] = [
            {"instance": u.instance, "collection": u.collection, **f}
            for u in users
            if u.embedding_files is not None
            for f in u.embedding_files()
        ]
        return result


def _is_hidden(path: Path, st: os.stat_result | None = None) -> bool:
    """名前が "." で始まる、または Windows の隠し属性が付いているか（st があればそれの属性を見る）。"""
    if path.name.startswith("."):
        return True
    try:
        return bool(getattr(st or path.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)
    except OSError:
        return False


def effective_extensions(extensions: tuple[str, ...], text_extensions: Collection[str] = ()) -> tuple[str, ...]:
    """include_extensions のうち、実際に取り込む拡張子。

    画像ファイル（.png・.heic 等）は image_input = True なら画像として埋め込み、False でもファイル名・
    フォルダ名で探せるようパスだけを登録する（file_mode() の "path"）。
    text_extensions（[vector_db].text_extensions）の拡張子は、テキストとして読む形式として加える。
    """
    supported = (*SUPPORTED_EXTENSIONS, *IMAGE_FILE_EXTENSIONS, *text_extensions)
    return tuple(e for e in extensions if e in supported)


class FileStat(NamedTuple):
    """走査で得たファイルのサイズ・更新日時（os.stat_result のうち取り込みで使う分だけ）。

    数十万ファイルのコレクションでは、走査の結果をすべてメモリーに持つため、os.stat_result・Path ではなく
    これとパスの文字列で持つ（1ファイルあたりのメモリーが半分以下になる）。
    """

    st_size: int
    st_mtime: float


@dataclass
class ScanResult:
    """scan_source_files() の結果。

    files: 取り込むファイルのパス（文字列）とその FileStat（パス順。max_files を超えたら選んだ分だけ）。
    total: 見つかった取り込み対象のファイルの数（max_files を超えた分も数える）。
    found_known: 見つかったファイルのうち、known に含まれていたもののパス（選ばれなかったものも含む。
        取り込み済みのファイルが消えたかの判定に使う）。
    stopped: 走査の途中で停止を求められた（files 等は途中までの分で、すべてではない）。
    """

    files: list[tuple[str, FileStat]]
    total: int
    found_known: set[str]
    stopped: bool = False

    @property
    def over_limit(self) -> int:
        """max_files を超えたため取り込まないファイルの数。"""
        return self.total - len(self.files)


class _Descending:
    """heapq（小さい順）で「パス順で後ろのもの」を先頭にするためのキー（パス順で先頭の N 件を残すのに使う）。"""

    __slots__ = ("key",)

    def __init__(self, key: str):
        self.key = key

    def __lt__(self, other: "_Descending") -> bool:
        return self.key > other.key


def _is_link(entry: os.DirEntry) -> bool:
    """entry がシンボリックリンクかジャンクション（Windows のマウントポイント）か。"""
    if entry.is_symlink():
        return True
    try:
        tag = getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
    except OSError:
        return False
    return tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)


def _link_target(path: Path) -> str | None:
    """リンクのフォルダの実体のパス（比べるために正規化したもの。たどれなければ None）。"""
    try:
        return os.path.normcase(os.path.realpath(path, strict=True))
    except OSError:
        return None


def _is_same_or_inside(path: str, folder: str) -> bool:
    """path が folder 自身かその中か（どちらも _link_target で正規化し、末尾の区切りを除いたもの）。"""
    return path == folder or path.startswith(folder + os.sep)


@dataclass
class _Subdirectory:
    path: Path
    # シンボリックリンク・ジャンクションなら実体のパス（_link_target）。リンクでなければ None。
    link_target: str | None = None


def _keyword_patterns(keywords: Sequence[str]) -> tuple[str, ...]:
    """取り込み除外・対象キーワードを、_matches_keywords() で比べる形（区切りを / にそろえ、大文字小文字を区別しない）にする。"""
    return tuple(k.strip().replace("\\", "/").casefold() for k in keywords if k.strip())


def _matches_keywords(root: Path, path: Path, is_dir: bool, patterns: tuple[str, ...]) -> bool:
    """取り込み元フォルダ root より下の path（"/" で始め、フォルダなら "/" で終える）が、キーワードのどれかを含むか。

    フォルダ名・ファイル名もパスの一部なので、名前に含むものも当てはまる。前後に / を付けて比べるため、
    "/old/" のように書けば名前が old ちょうどのフォルダ（とその中）だけに当てられる。取り込み元フォルダ自身のパスは比べない。
    "*.xlsx" のように "*." で始まるキーワードは拡張子の指定とし、ファイル名がそれで終わるファイルだけに当てる
    （フォルダには当てない）。
    """
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    text = f"/{relative}/" if is_dir else f"/{relative}"
    text = text.casefold()
    for p in patterns:
        if p.startswith("*."):
            if not is_dir and text.endswith(p[1:]):
                return True
        elif p in text:
            return True
    return False


@dataclass(frozen=True)
class _PathFilter:
    """コレクションの取り込み除外・対象キーワード（_keyword_patterns() の形）による絞り込み。

    除外キーワードに当てはまるフォルダはたどらず、ファイルは取り込まない。対象キーワードがあれば、
    当てはまるファイルだけを取り込む（中に当てはまるファイルがあり得るため、フォルダは対象キーワードでは絞らない）。
    両方に当てはまるファイルは取り込まない（除外が優先）。
    """

    excludes: tuple[str, ...] = ()
    includes: tuple[str, ...] = ()

    @classmethod
    def from_keywords(cls, exclude_keywords: Sequence[str] = (), include_keywords: Sequence[str] = ()) -> "_PathFilter":
        return cls(_keyword_patterns(exclude_keywords), _keyword_patterns(include_keywords))

    def allows_directory(self, root: Path, path: Path) -> bool:
        return not (self.excludes and _matches_keywords(root, path, True, self.excludes))

    def allows_file(self, root: Path, path: Path) -> bool:
        if self.excludes and _matches_keywords(root, path, False, self.excludes):
            return False
        return not self.includes or _matches_keywords(root, path, False, self.includes)


def _read_directory(
    directory: Path, recursive: bool, allowed: set[str], root: Path | None = None, path_filter: _PathFilter = _PathFilter()
) -> tuple[list[tuple[str, FileStat]], list[_Subdirectory]]:
    """directory 直下の取り込み対象のファイルと、たどるサブフォルダを返す（読めなければ空）。

    root は directory を含む取り込み元フォルダ（path_filter のキーワードは root より下のパスと比べる）。
    """
    root = directory if root is None else root
    try:
        entries = list(os.scandir(directory))
    except OSError as e:
        logger.warning("vector_db ingest: cannot read folder: %s: %s", directory, e)
        return [], []
    files: list[tuple[str, FileStat]] = []
    subdirectories: list[_Subdirectory] = []
    for entry in entries:
        path = Path(entry.path)
        try:
            is_dir = entry.is_dir()
            st = entry.stat()
        except OSError:
            continue
        if is_dir:
            if recursive and not _is_hidden(path, st) and path_filter.allows_directory(root, path):
                if _is_link(entry):
                    target = _link_target(path)
                    if target is not None:
                        subdirectories.append(_Subdirectory(path, target))
                else:
                    subdirectories.append(_Subdirectory(path))
        elif (
            path.suffix.lower() in allowed
            and not entry.name.startswith("~$")
            and not _is_hidden(path, st)
            and path_filter.allows_file(root, path)
        ):
            files.append((entry.path, FileStat(st.st_size, st.st_mtime)))
    return files, subdirectories


class _LinkGuard:
    """シンボリックリンク・ジャンクションのフォルダを、ループ・重複せずにたどるための判定。

    実体がいずれかの取り込み元フォルダの中（リンクでないフォルダとしていずれたどる）・取り込み元フォルダを含むフォルダ
    （たどると取り込み元をもう一度たどる）か、既にたどったリンクの実体と同じなら、たどらない
    （自分の親を指すジャンクション等で同じファイルを何度も取り込まないため）。
    """

    def __init__(self, roots: list[Path]):
        self._roots = [t.rstrip("\\/") for t in (_link_target(r) for r in roots) if t]
        self._visited: set[str] = set()

    def allow(self, subdirectory: _Subdirectory) -> bool:
        target = subdirectory.link_target
        if target is None:
            return True
        target = target.rstrip("\\/")
        if any(_is_same_or_inside(target, root) for root in self._roots):
            logger.info("vector_db ingest: not following a link that points inside the source folder: %s", subdirectory.path)
            return False
        if any(_is_same_or_inside(root, target) for root in self._roots):
            logger.info("vector_db ingest: not following a link that points to a folder containing the source folder: %s", subdirectory.path)
            return False
        if target in self._visited:
            logger.info("vector_db ingest: not following a link to an already visited folder: %s", subdirectory.path)
            return False
        self._visited.add(target)
        return True


def _scan_roots(folders: Path | Sequence[Path], recursive: bool) -> list[Path]:
    """走査を始めるフォルダ。recursive なら、他の取り込み元フォルダの中にあるフォルダは外す（同じファイルを2回数えないため）。"""
    roots = [folders] if isinstance(folders, Path) else list(folders)
    if not recursive:
        return roots
    keys = [(_link_target(r) or os.path.normcase(os.path.abspath(r))).rstrip("\\/") for r in roots]
    result = []
    for i, root in enumerate(roots):
        if any(j != i and _is_same_or_inside(keys[i], keys[j]) and (keys[i] != keys[j] or j < i) for j in range(len(roots))):
            logger.info("vector_db ingest: skipping a source folder inside another source folder: %s", root)
            continue
        result.append(root)
    return result


def source_root(folders: Sequence[Path], path: Path) -> Path:
    """path を含む取り込み元フォルダ（入れ子なら一番内側。どれにも含まれなければ最初のフォルダ）。"""
    best: Path | None = None
    for folder in folders:
        try:
            path.relative_to(folder)
        except ValueError:
            continue
        if best is None or len(folder.parts) > len(best.parts):
            best = folder
    return best if best is not None else folders[0]


def scan_source_files(
    folders: Path | Sequence[Path],
    recursive: bool,
    extensions: tuple[str, ...],
    *,
    workers: int = 1,
    max_files: int | None = None,
    known: Collection[str] = (),
    exclude_keywords: Sequence[str] = (),
    include_keywords: Sequence[str] = (),
    text_extensions: Collection[str] = (),
    should_stop: Callable[[], bool] | None = None,
) -> ScanResult:
    """取り込み対象のファイルとその stat を探す（Office の一時ファイル ~$*.docx 等と、隠しファイル・隠しフォルダ配下は除く）。

    folders は取り込み元フォルダ（1つでも複数でもよい。複数ならすべてを走査する）。
    取り込み元フォルダ自身が隠しフォルダでも対象にする（判定は取り込み元より下の名前だけ）。
    exclude_keywords（取り込み除外キーワード）のどれかを取り込み元フォルダより下のパスに含むフォルダ・ファイルも除き、
    include_keywords（取り込み対象キーワード）があれば、どれかを含むファイルだけにする（大文字小文字は区別しない。_PathFilter）。
    シンボリックリンク・ジャンクションのフォルダもたどるが、ループ・重複になるものはたどらない（_LinkGuard）。
    拡張子は effective_extensions()（text_extensions も渡す）で絞る。stat は os.scandir() のもの（Windows ではフォルダを読むときに
    サイズ・更新日時・属性も得られるため、ファイルごとに問い合わせずに済む）。

    workers が 2 以上なら、その数のスレッドで複数のフォルダを同時に読む（ファイルサーバー上のフォルダでは
    フォルダを読む往復の待ち時間が大半を占めるため、並列にすると速い）。

    max_files を超えるファイルがあれば、取り込みのたびに選ぶファイルが入れ替わらないよう、known（取り込み済み）の
    ファイルを優先し、残りの枠を新しいファイルのパス順の先頭から埋める。メモリーに持つのは高々 max_files の
    2倍程度のファイルまで。

    should_stop() が True を返したら、残りのフォルダを読まずにやめる（ScanResult.stopped。ファイルサーバー上の
    大きなフォルダでは走査だけで長くかかるため、フォルダを1つ読むごとに確かめる）。
    """
    allowed = set(effective_extensions(extensions, text_extensions))
    path_filter = _PathFilter.from_keywords(exclude_keywords, include_keywords)
    roots = _scan_roots(folders, recursive)
    links = _LinkGuard(roots)
    known_files: list[tuple[str, FileStat]] = []
    found_known: set[str] = set()
    # known に無いファイル。max_files があればパス順で先頭の max_files 件だけを残す（ヒープの先頭がパス順で最後のもの）。
    new_files: list[tuple[_Descending, str, FileStat]] = []
    total = 0
    stopped = False

    def stop_requested() -> bool:
        nonlocal stopped
        stopped = stopped or (should_stop is not None and should_stop())
        return stopped

    def collect(found: list[tuple[str, FileStat]]) -> None:
        nonlocal total
        for source, st in found:
            total += 1
            if source in known:
                found_known.add(source)
                known_files.append((source, st))
                continue
            item = (_Descending(source.lower()), source, st)
            if max_files is None or len(new_files) < max_files:
                heapq.heappush(new_files, item)
            elif new_files[0][0] < item[0]:
                # パス順で今残しているものの最後より前なら入れ替える。
                heapq.heapreplace(new_files, item)

    if workers <= 1:
        # (読むフォルダ, そのフォルダを含む取り込み元フォルダ)
        directories = [(root, root) for root in reversed(roots)]
        while directories and not stop_requested():
            directory, root = directories.pop()
            found, subdirectories = _read_directory(directory, recursive, allowed, root, path_filter)
            collect(found)
            directories.extend((d.path, root) for d in subdirectories if links.allow(d))
    else:
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vector_db_scan")
        try:
            # 読んでいるフォルダごとの、そのフォルダを含む取り込み元フォルダ。
            running = {pool.submit(_read_directory, root, recursive, allowed, root, path_filter): root for root in roots}
            try:
                while running:
                    # 応答の遅いフォルダを読んでいる間も、停止の求めに気づけるよう _STOP_POLL_SECONDS 秒ごとに見直す。
                    done, _pending = wait(running, timeout=_STOP_POLL_SECONDS, return_when=FIRST_COMPLETED)
                    if stop_requested():
                        break
                    for future in done:
                        root = running.pop(future)
                        found, subdirectories = future.result()
                        collect(found)
                        for d in subdirectories:
                            if links.allow(d):
                                running[pool.submit(_read_directory, d.path, recursive, allowed, root, path_filter)] = root
            except BaseException:
                for future in running:
                    future.cancel()
                raise
        finally:
            # 停止したときは、読んでいる途中のフォルダの応答を待たずに返す（結果は使わない）。
            pool.shutdown(wait=not stopped, cancel_futures=True)

    known_files.sort(key=lambda item: item[0].lower())
    selected = known_files
    new_sorted = sorted(((item[1], item[2]) for item in new_files), key=lambda item: item[0].lower())
    new_files.clear()
    if max_files is not None:
        del selected[max_files:]
        new_sorted = new_sorted[: max_files - len(selected)]
    selected += new_sorted
    selected.sort(key=lambda item: item[0].lower())
    return ScanResult(selected, total, found_known, stopped)


def list_source_files(
    folders: Path | Sequence[Path],
    recursive: bool,
    extensions: tuple[str, ...],
    exclude_keywords: Sequence[str] = (),
    include_keywords: Sequence[str] = (),
    text_extensions: Collection[str] = (),
) -> list[Path]:
    """scan_source_files() のファイルのパスだけを返す。"""
    scan = scan_source_files(
        folders, recursive, extensions,
        exclude_keywords=exclude_keywords, include_keywords=include_keywords, text_extensions=text_extensions,
    )
    return [Path(path) for path, _st in scan.files]


def file_mode(cfg: Config, path: Path) -> str:
    """path の埋め込み方。

    [vector_db].image_input = true で、画像ファイルか image_input_extensions の形式なら "text+image"（本文のチャンクに加えて
    ページ画像も埋め込む。画像ファイルは本文が無いため画像だけ）。それ以外の Office ファイルは embedded_images = true なら
    "text+embedded"（本文のチャンクに加えて、ファイルの中の画像を1枚ずつ埋め込む。embedded_images.py）。
    image_input = false の画像ファイルは "path"（中身は埋め込まず、ファイルパスだけを登録する）。それ以外は "text"。
    """
    return _file_mode(_LoadOptions.from_config(cfg), path)


def _page_chunk_text(cfg: Config, path: Path, page: PageImage, mode: str = "text+image") -> str:
    """ページ画像（mode が "text+embedded" ならファイルの中の画像）のチャンクの本文（検索結果の抜粋）。
    ページの文字（中の画像は代替テキストと周りの文字）を chunk_size 文字までにする。"""
    text = page.text.strip()
    if not text:
        if mode == "text+embedded":
            return "(This is an image embedded in the file, with no description. Check its content in the source image)"
        if path.suffix.lower() in IMAGE_FILE_EXTENSIONS:
            return "(This is an image file. Check its content in the source image)"
        return "(This page has no text. Check its content by rendering the source page as an image)"
    return text if len(text) <= cfg.vector_db_chunk_size else text[: cfg.vector_db_chunk_size] + "…"


def relative_path_text(folder: Path, path: Path) -> str:
    """ファイルパスのベクターに埋め込む文字列（取り込み元フォルダからの相対パス。区切りは "/"）。

    取り込み元フォルダより上の部分は全ファイルで同じで検索の手がかりにならないため含めない。
    """
    try:
        return path.relative_to(folder).as_posix()
    except ValueError:
        return path.as_posix()


def ingest_collection(
    cfg: Config,
    name: str,
    *,
    rebuild: bool = False,
    progress: ProgressCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
    embedding_transport: httpx.BaseTransport | None = None,
    store_transport: httpx.BaseTransport | None = None,
    slots: IngestSlots | None = None,
    instance: str = "",
    progress_bytes: Callable[[int], None] | None = None,
) -> IngestResult:
    """コレクション name の取り込み元フォルダを差分で取り込む。

    サイズ・更新日時が前回と同じファイルは読み直さず、変わっていても内容（SHA-256）が
    同じなら記録だけ更新する。フォルダから消えたファイルのチャンクは削除する。
    読めなかったファイルは失敗一覧に載せて続行するが、埋め込みサーバー・保存先へ
    接続できない等の全体に関わるエラーは例外のまま止める。

    Args:
        cfg: 対象インスタンスの設定。
        name: コレクション名。
        rebuild: True なら既存のベクター・記録を消して全ファイルを取り込み直す
            （埋め込みモデル・document_prefix を変えたときに必要）。
        progress: ファイルの処理を始めるごとに呼ぶ進捗通知（並列のときは最後に始めたファイル）。
        should_stop: True を返したら残りのファイルを処理せずに終える（埋め込みの再試行を
            待っているファイルもその場でやめ、記録せずに次回の取り込みへ回す）。
        embedding_transport / store_transport: テスト用の httpx トランスポート。
        slots: 並列数の枠（IngestSlots）。ほかの取り込みと共有すると、並列数を合計で守る
            （準備・埋め込みのスレッド・子プロセスの数もその並列数にする）。省略時は cfg の
            [vector_db] ingest_workers・ingest_processes・ingest_embed_requests で、この取り込みだけの枠を作る。
        instance: インスタンス名（slots の各枠の状態・ログに出すだけ）。
        progress_bytes: ファイルを記録するごとに、記録まで終わったファイルのサイズの累計（バイト）を渡して呼ぶ
            （設定ダッシュボードの1秒当たりの取り込み容量用。変わっていない・取り込めなかったファイルは数えない）。

    Raises:
        IngestError: コレクションが無い、取り込み元フォルダが無い、または
            埋め込みモデル・document_prefix が前回と違うのに rebuild=False の場合。
        EmbeddingError / StoreError: 埋め込みサーバー・保存先を使えない場合。
    """
    model = embedding_model(cfg)
    result = IngestResult()
    if slots is None:
        slots = IngestSlots(cfg.vector_db_ingest_workers, cfg.vector_db_ingest_processes, cfg.vector_db_ingest_embed_requests)
    started = time.monotonic()
    logger.info(
        "vector_db ingest: start instance=%s collection=%s rebuild=%s model=%s workers=%d processes=%d",
        instance or "-", name, rebuild, model, slots.workers, slots.processes,
    )
    with open_catalog(cfg.vector_db_dir) as catalog, open_journal(cfg.vector_db_dir) as journal:
        try:
            collection = catalog.get(name)
        except CatalogError as e:
            raise IngestError(str(e)) from e
        if not rebuild and journal.drop_pending(name):
            # 前回の再構築・削除が保存先を消している途中で止まった。カタログの記録と保存先の中身が合わないため、作り直す。
            logger.warning("vector_db ingest: collection=%s a previous rebuild or deletion was interrupted, so rebuilding it", name)
            rebuild = True
        folders = [Path(f) for f in collection.source_folders]
        for folder in folders:
            # 1つでも見えなければ、そのフォルダのファイルを「消えた」と誤って削除しないよう取り込まない。
            if not folder.is_dir():
                raise IngestError(f"Source folder does not exist: {folder}")
        if collection.model and collection.model != model and not rebuild:
            raise IngestError(
                f"Collection {name} was built with embedding model {collection.model} (current setting: {model}). "
                "Search does not work with a different model, so rebuild it."
            )
        prefix = cfg.vector_db_document_prefix
        if collection.model and collection.document_prefix is not None and collection.document_prefix != prefix and not rebuild:
            raise IngestError(
                f"Collection {name} was ingested with document_prefix {collection.document_prefix!r} (current setting: {prefix!r}). "
                "Mixing prefixes lowers search accuracy, so rebuild it."
            )
        # Qdrant へ送り直しを待つ間も「停止」に応じる（[vector_db].qdrant_max_retries = -1 なら無制限に待つため）。
        store = open_store(cfg, transport=store_transport, should_stop=should_stop)
        keywords = open_keyword_index(cfg.vector_db_dir)
        # 埋め込みサーバーへの接続は取り込み全体で使い回す（embeddings.open_client 参照）。
        embedding_client = open_client(cfg, transport=embedding_transport, concurrency=slots.capacity("embed"))
        try:
            if rebuild:
                # 保存先を消した後・カタログの記録を消す前に止まると、次の差分取り込みが記録を見て「変わっていない」と
                # 埋め込まないため、先に「消している途中」と記録し、カタログまで消し終えたら外す（drop_pending）。
                journal.begin_drop(name)
                for storage in storage_collection_names(name):
                    store.drop_collection(storage)
                keywords.drop_collection(name)
                keywords.drop_collection(path_collection_name(name))
                catalog.clear_files(name)
                catalog.set_model(name, None, None)
                journal.clear(name)
                collection = catalog.get(name)
            else:
                # qdrant_preset を変えた後は、変わったファイルが無くても既存のコレクションへ反映する。
                for storage in storage_collection_names(name):
                    store.apply_preset(storage)
            _ingest(
                cfg, catalog, store, keywords, journal, name, collection.dim, model, folders, collection.recursive,
                result, progress, should_stop, embedding_client, slots, instance,
                exclude_keywords=collection.exclude_keywords, include_keywords=collection.include_keywords,
                progress_bytes=progress_bytes,
            )
        except StoreStopped:
            # コミットしていない書き込みは _ingest が捨てている（記録されていないファイルは次回の取り込みで処理する）。
            logger.info("vector_db ingest: instance=%s collection=%s stopped while retrying the request to Qdrant", instance or "-", name)
        finally:
            embedding_client.close()
            keywords.close()
            store.close()
        catalog.mark_ingested(name)
    summary = result.to_dict()
    summary["failed"] = len(summary["failed"])
    logger.info("vector_db ingest: finished instance=%s collection=%s in %.1fs %s", instance or "-", name, time.monotonic() - started, summary)
    return result


@dataclass
class _Prepared:
    """1ファイル分の、DB へ書き込む前までの処理結果（_load() で作り、埋め込みを足してメインスレッドで書き込む）。

    kind:
        "unchanged" - 埋め込み直さない（keyword_chunks があればキーワード索引だけ埋める）。
        "touch"     - 内容（SHA-256）は同じで、サイズ・更新日時の記録だけ更新する。
        "failed"    - 取り込めなかった（reason に理由。too_large なら上限サイズ超え）。
        "embed"     - chunks・pages を埋め込む（vectors・image_vectors）。
        "path_only" - 中身は埋め込まず、ファイルパスだけを登録する（image_input = false の画像ファイル、
                      本文も画像にできるページも無かったファイル。reason にその理由）。

    path_vector はファイルパス（path_text）のベクター。"embed" では常に、"unchanged"・"touch" では
    パスのベクターがまだ無いファイル（FileRecord.path_indexed = False）だけ埋め込む。

    streamed は、本文のチャンクを埋め込みが返るたびに保存先へ書き込み済み（_Job.stream）で、vectors を持たないもの。
    """

    path: Path
    record: FileRecord | None
    mode: str
    kind: str
    stat: FileStat | None = None
    sha: str = ""
    reason: str = ""
    too_large: bool = False
    keyword_chunks: list[ChunkRecord] | None = None
    chunks: list[ChunkRecord] = field(default_factory=list)
    pages: list[PageImage] = field(default_factory=list)
    vectors: list[Sequence[float]] = field(default_factory=list)
    image_vectors: list[Sequence[float]] = field(default_factory=list)
    path_text: str = ""
    path_vector: Sequence[float] | None = None
    # pages の PNG を書き出した一時フォルダ（書き込みが終わったら消す。_Pipeline._finish_job）。
    page_dir: Path | None = None
    streamed: bool = False

    def texts_to_embed(self) -> list[str]:
        """埋め込むテキスト（本文のチャンク、続けてファイルパス）。"""
        if self.kind == "embed":
            return [c.text for c in self.chunks] + [self.path_text]
        if self.kind == "path_only":
            return [self.path_text]
        if self.kind in ("unchanged", "touch") and not (self.record is not None and self.record.path_indexed):
            # パスのベクターの導入前に取り込んだファイルは、内容を埋め込み直さずにパスだけを埋め込む。
            return [self.path_text]
        return []

    def set_vectors(self, text_vectors: list[Sequence[float]], image_vectors: list[Sequence[float]]) -> None:
        """texts_to_embed()・pages の順の埋め込みを受け取る。"""
        if self.kind == "embed":
            self.vectors, self.path_vector = text_vectors[:-1], text_vectors[-1]
            self.image_vectors = image_vectors
        elif text_vectors:
            self.path_vector = text_vectors[0]


@dataclass(frozen=True)
class _LoadOptions:
    """_load() に渡す設定（子プロセス（[vector_db].ingest_processes）へ送るため、Config ではなく必要な値だけを持つ）。"""

    chunk_size: int
    chunk_overlap: int
    image_input: bool
    image_input_extensions: tuple[str, ...]
    image_dpi: int
    max_file_mb: float
    embedded_images: bool = False
    embedded_image_min_px: int = 0
    office_timeout: float = 0.0
    # ページ画像の PNG・Office から書き出した PDF を置く一時フォルダ（取り込み1回ごとに [vector_db].ingest_temp_dir の下に作る。
    # _Ingester.run）。空ならシステムの一時フォルダ。
    pages_dir: str = ""
    # テキストとして読む拡張子（[vector_db].text_extensions）。
    text_extensions: tuple[str, ...] = ()
    # 取り込みの停止の印のファイル（できたら本文の抽出・画像化を途中でやめる。extract.watch_stop_file）。空なら見張らない。
    stop_file: str = ""

    @classmethod
    def from_config(cls, cfg: Config, pages_dir: str = "") -> "_LoadOptions":
        return cls(
            cfg.vector_db_chunk_size,
            cfg.vector_db_chunk_overlap,
            cfg.vector_db_image_input,
            cfg.vector_db_image_input_extensions,
            cfg.vector_db_image_dpi,
            cfg.vector_db_max_file_mb,
            cfg.vector_db_embedded_images,
            cfg.vector_db_embedded_image_min_px,
            cfg.vector_db_office_timeout_seconds,
            pages_dir,
            cfg.vector_db_text_extensions,
        )

    @property
    def max_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)


def _file_mode(options: _LoadOptions, path: Path) -> str:
    suffix = path.suffix.lower()
    if options.image_input and (suffix in IMAGE_FILE_EXTENSIONS or suffix in options.image_input_extensions):
        return "text+image"
    if options.image_input and options.embedded_images and suffix in EMBEDDED_IMAGE_EXTENSIONS:
        return "text+embedded"
    return "path" if suffix in IMAGE_FILE_EXTENSIONS else "text"


def _chunk_segments(options: _LoadOptions, path: Path, data: bytes | None = None) -> list[ChunkRecord]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS and suffix not in options.text_extensions:
        return []  # 画像ファイルには本文が無い
    chunks = []
    for segment in extract_segments(path, data, options.text_extensions):
        for text in split_text(segment.text, options.chunk_size, options.chunk_overlap):
            chunks.append(ChunkRecord(locator=segment.locator, text=text))
    return chunks


def _keyword_chunks(options: _LoadOptions, path: Path, needs_keywords: bool) -> list[ChunkRecord] | None:
    """埋め込み直さないファイルがキーワード索引に無ければ（索引の導入前に取り込んだもの）、索引に入れる本文を返す。"""
    if not needs_keywords:
        return None
    try:
        return _chunk_segments(options, path)
    except (OSError, ExtractError):
        return []


def _is_unchanged(record: FileRecord | None, mode: str, st: FileStat) -> bool:
    return record is not None and record.mode == mode and record.size == st.st_size and record.mtime == st.st_mtime


def _load(
    options: _LoadOptions, folder: Path, path: Path, st: FileStat, record: FileRecord | None, needs_keywords: bool
) -> _Prepared:
    """path を読み、必要なら本文の抽出・ページの画像化まで行う（埋め込みと DB への書き込みはしない）。

    ワーカースレッドか子プロセス（[vector_db].ingest_processes）で動く。読めないファイルは kind="failed" で返す。
    ページ画像は options.pages_dir の下のファイルごとの一時フォルダへ PNG で書き出し（_Prepared.page_dir）、
    メモリーには全ページの画像を持たない（400ページの PDF 等でもメモリーを使い切らないため）。
    options.stop_file ができたら（取り込みの停止）、本文の抽出・画像化を途中でやめて kind="failed" で返す（結果は使われない）。
    """
    with watch_stop_file(options.stop_file or None):
        return _load_file(options, folder, path, st, record, needs_keywords)


def _load_file(
    options: _LoadOptions, folder: Path, path: Path, st: FileStat, record: FileRecord | None, needs_keywords: bool
) -> _Prepared:
    # image_input・image_input_extensions・embedded_images を切り替えたら、内容の変わっていないファイルも新しい埋め込み方で取り込み直す。
    mode = _file_mode(options, path)
    path_text = relative_path_text(folder, path)
    if _is_unchanged(record, mode, st):
        return _Prepared(path, record, mode, "unchanged", keyword_chunks=_keyword_chunks(options, path, needs_keywords), path_text=path_text)
    if mode == "path":
        # image_input = false の画像ファイルは中身を読まず、ファイル名・フォルダ名で探せるようパスだけを登録する。
        return _Prepared(path, record, mode, "path_only", stat=st, path_text=path_text, reason="image file (image_input = false)")
    if st.st_size > options.max_bytes:
        reason = f"The file size exceeds the limit ({options.max_file_mb}MB)"
        return _Prepared(path, record, mode, "failed", reason=reason, too_large=True)
    page_dir: Path | None = None
    try:
        # SHA-256 の計算と本文の抽出で同じファイルを2回読まないよう、中身を1回だけ読む（上限サイズ以下のため）。
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if record is not None and record.mode == mode and record.sha256 == sha:
            return _Prepared(
                path, record, mode, "touch", stat=st, sha=sha, keyword_chunks=_keyword_chunks(options, path, needs_keywords),
                path_text=path_text,
            )
        not_text: NotTextError | None = None
        try:
            chunks = _chunk_segments(options, path, data)
        except NotTextError as e:
            # text_extensions の形式でも中身がバイナリなら、失敗にせずパスだけを登録する（画像だけの PDF と同じ）。
            chunks, not_text = [], e
        pages: list[PageImage] = []
        if mode == "text+embedded":
            page_dir = Path(tempfile.mkdtemp(prefix="file_", dir=options.pages_dir or None))
            pages = extract_embedded_images(path, data, dpi=options.image_dpi, min_px=options.embedded_image_min_px, out_dir=page_dir)
        del data
        if mode == "text+image":
            page_dir = Path(tempfile.mkdtemp(prefix="file_", dir=options.pages_dir or None))
            pages = render_pages(
                path, options.image_dpi, out_dir=page_dir, office_timeout=options.office_timeout or None,
                temp_dir=options.pages_dir or None,
            )
    except (OSError, ExtractError, zipfile.BadZipFile) as e:
        _remove_dir(page_dir)
        return _Prepared(path, record, mode, "failed", reason=str(e))
    if not pages:
        _remove_dir(page_dir)
        page_dir = None
    if not chunks and not pages:
        # 画像だけの PDF 等でも、ファイル名・フォルダ名で探せるようパスだけを登録する。
        if mode == "text+image":
            reason = "There was neither text nor a page that could be rendered as an image"
        elif mode == "text+embedded":
            reason = "There was neither text nor an image in the file"
        elif not_text is not None:
            reason = str(not_text)
        else:
            reason = "Could not extract text (e.g. a PDF of images only; with [vector_db] image_input = true it can be ingested as images)"
        return _Prepared(path, record, mode, "path_only", stat=st, sha=sha, path_text=path_text, reason=reason)
    return _Prepared(path, record, mode, "embed", stat=st, sha=sha, chunks=chunks, pages=pages, path_text=path_text, page_dir=page_dir)


def _remove_dir(directory: Path | None) -> None:
    if directory is not None:
        shutil.rmtree(directory, ignore_errors=True)


def _load_in_thread(*args) -> _Prepared:
    # 取り込みの間、このスレッドで起動した Office は終了せずに使い回す（_close_office_in_threads で終了する）。
    keep_office_apps()
    return _load(*args)


def _init_load_process() -> None:
    """子プロセス（[vector_db].ingest_processes）の初期化。Office を使い回し、プロセスの終了時に終了する。"""
    import multiprocessing.util

    keep_office_apps()
    multiprocessing.util.Finalize(None, close_office_apps, exitpriority=10)


def _close_office_in_threads(pool: ThreadPoolExecutor, workers: int) -> None:
    """pool の各スレッドで使い回していた Office を終了する（COM のオブジェクトは作ったスレッドでしか扱えない）。

    全スレッドがそろうまで待つ（Barrier）ことで、各スレッドに1つずつ終了の処理を割り当てる。
    """
    barrier = threading.Barrier(workers)

    def close() -> None:
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        close_office_apps()

    wait([pool.submit(close) for _ in range(workers)])


class _Writer:
    """_Prepared をカタログ・保存先・キーワード索引へ書き込む（書き込みのスレッド（_WriterThread）だけで使う）。

    書き込みは begin() から finish() まで、_COMMIT_INTERVAL_SECONDS 秒・_COMMIT_EVERY_FILES ファイルごとに
    まとめてコミットする。保存先 → キーワード索引 → カタログの順にコミットするため、途中で止まっても
    カタログに記録されたファイルのベクターは必ず保存されている（記録されていないファイルは次回取り込み直す）。

    SQLite の DB（カタログ・キーワード索引・sqlite-vec）へはその場では書かず、コミットのときに DB ごとに
    1つの短いトランザクションでまとめて書く（DeferredWrites）。書き込みのロックを持つのは書いている間だけで、
    Qdrant への送信・埋め込みを待つ間は持たない（同じインスタンスの複数のコレクションを同時に取り込んでも、
    互いのロックを長く待って「database is locked」にならないため）。

    本文・ページ画像を書き換えるファイルは、保存先へ書く前に journal へ記録し、カタログへの記録までコミットしたら
    消す。強制終了等で書きかけのまま残ったファイルは、次の取り込みの _reconcile_journal が片付ける。
    """

    _COMMIT_INTERVAL_SECONDS = 2.0
    _COMMIT_EVERY_FILES = 500

    def __init__(
        self, cfg: Config, catalog: Catalog, store, keywords: KeywordIndex, journal: IngestJournal, name: str, dim, model: str,
        result: IngestResult, should_stop: Callable[[], bool] | None = None,
    ):
        self.cfg, self.journal = cfg, journal
        # 別のプロセスのロックを待つ間に停止を求められたら、待つのをやめる（StoreStopped。retry_locked 参照）。
        self.should_stop = should_stop
        # Qdrant は自分で送る更新をためる（begin_batch）ため、そのまま使う。DeferredWrites も同じ形
        # （begin_batch・commit・rollback・end_batch）で使える。
        self.store = DeferredWrites(store, "store", should_stop=should_stop) if isinstance(store, SqliteBatchMixin) else store
        self.keywords = DeferredWrites(keywords, "keyword index", should_stop=should_stop)
        self.catalog = DeferredWrites(catalog, "catalog", should_stop=should_stop)
        self.name, self.dim, self.model, self.result = name, dim, model, result
        self.image_collection_ready = False
        self.path_collection_ready = False
        # キーワード索引に入っているファイル（has_source() をファイルごとに呼ばないよう、最初にまとめて読む）。
        self.keyword_sources = keywords.sources(name)
        self.path_keyword_sources = keywords.sources(path_collection_name(name))
        self._uncommitted = 0
        self._last_commit = time.monotonic()
        # カタログへ記録したが、まだコミットしていないファイル（コミットしたら journal から消す）。
        self._recorded: list[str] = []

    def _parts(self):
        # コミットの順（保存先 → キーワード索引 → カタログ）。
        return (self.store, self.keywords, self.catalog)

    def begin(self) -> None:
        self.store.begin_batch()

    def finish(self) -> None:
        self._commit()
        self.store.end_batch()

    def _discard_recorded(self) -> None:
        if self._recorded:
            self.journal.discard(self.name, self._recorded, should_stop=self.should_stop)
            self._recorded = []

    def abort(self) -> None:
        """コミットしていない書き込みを捨てる（エラーで止めるとき。journal の記録は次の取り込みで片付けるため残す）。"""
        for part in self._parts():
            try:
                part.rollback()
            except Exception:  # noqa: BLE001 - 元のエラーを優先する
                logger.exception("vector_db ingest: rollback failed")
            part._batching = False

    def _maybe_commit(self) -> None:
        self._uncommitted += 1
        if self._uncommitted >= self._COMMIT_EVERY_FILES:
            self._commit()
        else:
            self.commit_if_due()

    def _commit(self) -> None:
        for part in self._parts():
            part.commit()
        self._discard_recorded()
        self._uncommitted = 0
        self._last_commit = time.monotonic()

    def seconds_until_commit(self) -> float | None:
        """コミットしていない書き込みを、あと何秒でコミットするか（無ければ None）。

        _Pipeline はこの秒数までしか埋め込み・準備の完了を待たない。次のファイルの書き込みを待ってから
        コミットすると、埋め込みの再試行や Office の変換で待つ間、書き終えたファイルが検索に出ず、
        ためた書き込みでメモリーも使い続けるため。
        """
        if not self._uncommitted:
            return None
        return max(0.0, self._COMMIT_INTERVAL_SECONDS - (time.monotonic() - self._last_commit))

    def commit_if_due(self) -> None:
        """前回のコミットから _COMMIT_INTERVAL_SECONDS 秒たっていれば、コミットしていない書き込みをコミットする。"""
        if self._uncommitted and time.monotonic() - self._last_commit >= self._COMMIT_INTERVAL_SECONDS:
            self._commit()

    def _replace_keywords(self, collection: str, source: str, chunks: list[ChunkRecord], index_texts: list[str] | None = None) -> None:
        """キーワード索引の source を入れ替える。語への区切りはここで（書き込みのロックを取る前に）済ませる。

        ためた書き込みは値をそのまま持つため、チャンクの多いファイル（_STREAM_MIN_CHUNKS 件超）は、消してから
        _KEYWORD_PAGE_ROWS 行ずつ足し、_KEYWORD_FLUSH_ROWS 行ためるごとにファイルの途中でもコミットする（全チャンクの
        語の一覧を一度にメモリーに作らないため。書きかけのファイルは journal に記録してあり、カタログへの記録は最後のため、
        途中で止まっても次の取り込みが取り込み直す）。
        """
        if len(chunks) <= _STREAM_MIN_CHUNKS:
            self.keywords.replace_source_rows(collection, source, list(iter_token_rows(chunks, index_texts=index_texts)))
            return
        # 変わっていないファイルの索引の作り直し（_Prepared.keyword_chunks）でも、途中のコミットの後に止まると索引が
        # 書きかけのまま「索引済み」になるため、journal に記録してから書く（次の取り込みが取り込み直す）。
        self.journal.add(self.name, source, should_stop=self.should_stop)
        rows = iter_token_rows(chunks, index_texts=index_texts)
        self.keywords.delete_source(collection, source)
        pending = 0
        while page := list(itertools.islice(rows, _KEYWORD_PAGE_ROWS)):
            self.keywords.append_source_rows(collection, source, page)
            pending += len(page)
            if pending >= _KEYWORD_FLUSH_ROWS:
                self._commit()
                pending = 0
        # 書き終えたら（このファイルの残りの書き込みと一緒に）コミットしたときに journal から消す。
        self._recorded.append(source)

    def _ensure_dim(self, vector: Sequence[float]) -> None:
        """最初の埋め込みで次元を決め、本文の保存先のコレクションを作ってモデルを記録する（作成済みなら何もしない）。"""
        if self.dim is None:
            self.dim = len(vector)
            self.store.ensure_collection(self.name, self.dim)
            self.catalog.set_model(self.name, self.model, self.dim, self.cfg.vector_db_document_prefix)

    def _write_path(self, source: str, prepared: _Prepared) -> None:
        """ファイルパスのベクターを1ファイル1件で入れ替える（store.path_collection_name 参照）。"""
        self._ensure_dim(prepared.path_vector)
        if not self.path_collection_ready:
            self.store.ensure_collection(path_collection_name(self.name), self.dim)
            self.path_collection_ready = True
        self.store.replace_source(
            path_collection_name(self.name), source, [ChunkRecord("", prepared.path_text)], [prepared.path_vector]
        )

    def _write_path_keywords(self, source: str, prepared: _Prepared, *, only_missing: bool) -> None:
        """ファイルパスをキーワード索引にも入れる（索引の中のコレクション名は store.path_collection_name と同じ）。

        拡張子は索引の語に含めない（"pdf"・"jpg" 等が同じ形式の全ファイルに一致してしまうため）。
        検索結果に出す相対パスは拡張子付きのまま。
        only_missing なら索引に無いときだけ入れる（パスのキーワード検索の導入前に取り込んだファイル用）。
        """
        if only_missing and source in self.path_keyword_sources:
            return
        index_text = prepared.path_text.removesuffix(prepared.path.suffix)
        self._replace_keywords(path_collection_name(self.name), source, [ChunkRecord("", prepared.path_text)], [index_text])
        self.path_keyword_sources.add(source)

    def write(self, prepared: _Prepared) -> None:
        self._write(prepared)
        self._maybe_commit()

    def _write(self, prepared: _Prepared) -> None:
        catalog, store, name, result = self.catalog, self.store, self.name, self.result
        path, record, mode = prepared.path, prepared.record, prepared.mode
        source = str(path)
        if prepared.kind == "failed":
            logger.warning("vector_db ingest: collection=%s failed to ingest %s: %s", name, source, prepared.reason)
            result.failed.append((source, prepared.reason))
            if prepared.too_large:
                result.too_large += 1
            return
        if prepared.kind in ("unchanged", "touch"):
            # 保存先 → キーワード索引 → カタログの順に書く（同時に動く別の取り込みとロックを取る順をそろえ、待ち合いで止まらないため）。
            if prepared.path_vector is not None:
                self._write_path(source, prepared)
            if prepared.keyword_chunks is not None:
                self._replace_keywords(name, source, prepared.keyword_chunks)
                self.keyword_sources.add(source)
            self._write_path_keywords(source, prepared, only_missing=True)
            path_indexed = record.path_indexed or prepared.path_vector is not None
            if prepared.kind == "touch":
                st = prepared.stat
                catalog.upsert_file(
                    name, FileRecord(source, st.st_size, st.st_mtime, prepared.sha, record.chunk_count, mode, path_indexed)
                )
            elif prepared.path_vector is not None:
                catalog.mark_path_indexed(name, source)
            result.unchanged += 1
            return
        if prepared.kind == "path_only":
            self._write_path_only(source, prepared)
            return
        chunks, pages, vectors, image_vectors = prepared.chunks, prepared.pages, prepared.vectors, prepared.image_vectors
        self._ensure_dim((vectors or image_vectors or [prepared.path_vector])[0])
        # 本文と画像は別のコレクションに入れる（store.image_collection_name 参照）。
        # 画像の無いファイルも、image_input を切り替える前の画像のベクターが残らないよう消す。
        if not prepared.streamed:
            # 本文のチャンクは append_chunks で書き込み済み（journal への記録も済み）でなければ、ここで入れ替える。
            self.journal.add(name, source)
            if chunks:
                store.replace_source(name, source, chunks, vectors)
            else:
                store.delete_source(name, source)
        if pages:
            if not self.image_collection_ready:
                store.ensure_collection(image_collection_name(name), self.dim)
                self.image_collection_ready = True
            image_chunks = [ChunkRecord(p.locator, _page_chunk_text(self.cfg, path, p, mode)) for p in pages]
            store.replace_source(image_collection_name(name), source, image_chunks, image_vectors)
        else:
            store.delete_source(image_collection_name(name), source)
        self._write_path(source, prepared)
        self._replace_keywords(name, source, chunks)
        self.keyword_sources.add(source)
        self._write_path_keywords(source, prepared, only_missing=False)
        chunk_count = len(chunks) + len(pages)
        st = prepared.stat
        catalog.upsert_file(name, FileRecord(source, st.st_size, st.st_mtime, prepared.sha, chunk_count, mode, True))
        self._recorded.append(source)
        result.chunks += chunk_count
        if record is None:
            result.added += 1
        else:
            result.updated += 1

    def _write_path_only(self, source: str, prepared: _Prepared) -> None:
        """中身は埋め込まず、ファイルパスだけを登録する（_Prepared の "path_only"）。

        以前に本文・ページ画像を埋め込んだファイル（image_input を切り替えた等）なら、それらを消す。
        """
        self._ensure_dim(prepared.path_vector)
        self.journal.add(self.name, source)
        for storage in (self.name, image_collection_name(self.name)):
            self.store.delete_source(storage, source)
        self._write_path(source, prepared)
        # チャンクの無いファイルとして記録する（索引の導入前のファイルとして読み直さないため）。
        self._replace_keywords(self.name, source, [])
        self.keyword_sources.add(source)
        self._write_path_keywords(source, prepared, only_missing=False)
        st = prepared.stat
        self.catalog.upsert_file(self.name, FileRecord(source, st.st_size, st.st_mtime, prepared.sha, 0, prepared.mode, True))
        self._recorded.append(source)
        logger.info("vector_db ingest: registered the path only (%s): %s", prepared.reason, source)
        self.result.path_only += 1
        if prepared.record is None:
            self.result.added += 1
        else:
            self.result.updated += 1

    def append_chunks(self, prepared: _Prepared, start: int, chunks: list[ChunkRecord], vectors: list[Sequence[float]]) -> None:
        """stream のファイル（_Job.stream）の、埋め込みが番号順にそろった本文のチャンク（_Job.pop_ready_chunks）を保存先へ足す。

        最初に足す前に journal へ記録してから古いチャンクを消す。Qdrant は commit() を待たずに送る（rollback() で
        戻らない）ため、途中で止まって・エラーになって書きかけのまま残っても、次の取り込みの _reconcile_journal が
        取り込み直させる（カタログに記録が無ければ書きかけのチャンクを消す）。
        """
        if not chunks:
            return
        source = str(prepared.path)
        if start == 0:
            self._ensure_dim(vectors[0])
            self.journal.add(self.name, source)
            self.store.delete_source(self.name, source)
        self.store.append_source(self.name, source, start, chunks, vectors)
        self._maybe_commit()

    def remove(self, source: str) -> None:
        """フォルダから消えたファイルのチャンク・記録を消す。"""
        for storage in storage_collection_names(self.name):
            self.store.delete_source(storage, source)
        self.keywords.delete_source(self.name, source)
        self.keywords.delete_source(path_collection_name(self.name), source)
        self.catalog.delete_file(self.name, source)
        self.result.removed += 1
        self._maybe_commit()


# _WriterThread のキューにためる書き込みの数と、ためるベクターの値の数（float。array('d') で 8 バイトずつ）の上限
# （どちらかを超えたらメインスレッドは空くのを待つ。キューが空なら、上限を超える1件でも入れる）。
_WRITE_QUEUE_LIMIT = 256
_WRITE_QUEUE_MAX_FLOATS = 32 * 1024 * 1024


def _task_floats(kind: str, args: tuple) -> int:
    """書き込みのキューの1件が持つベクターの値の数（_WRITE_QUEUE_MAX_FLOATS で数える分）。"""
    if kind == "write":
        prepared = args[0]
        vectors = [*(prepared.vectors or ()), *(prepared.image_vectors or ())]
        if prepared.path_vector is not None:
            vectors.append(prepared.path_vector)
    elif kind == "append":
        vectors = args[3]
    else:
        return 0
    return sum(len(v) for v in vectors if v is not None)


class _WriterThread:
    """_Writer を専用のスレッドで動かす（メインスレッドは準備・埋め込みの送り出しだけをする）。

    書き込み（write・append_chunks・remove）はキューへ入れ、書き込みのスレッドが入れた順に行う（journal への記録と
    保存先への書き込みの順序は変わらない）。Qdrant への送信やコミットを待つ間も、メインスレッドは次の埋め込みを送れる。
    コミットの期限（_Writer.seconds_until_commit）が来たら、次の書き込みを待たずにコミットする。

    キューにためるのは _WRITE_QUEUE_LIMIT 件・ベクターの値で _WRITE_QUEUE_MAX_FLOATS 個まで。書き込みが追いつかない
    ときは、メインスレッドが空くのを待つ（埋め込み済みのベクターをメモリーにため続けないため）。待ち始めるときに
    on_block を呼ぶ（_Pipeline は並列数の枠の空き待ちを取り下げる。IngestSlots.pause）。待っている間に停止を
    求められたら（should_stop）、その書き込みとそれ以降の書き込み（finish 以外）は入れずに捨てる（記録されなかった
    ファイルは次回の取り込みで処理する。分けて書き込むファイルの途中が抜けないよう、一度捨てたら以降もすべて捨てる）。

    書き込みのスレッドでエラーになったら、残りの書き込みはせずにコミットしていない分を捨て（_Writer.abort）、
    メインスレッドの次の呼び出し（check()・write() 等）で同じ例外を投げる。failed はそのとき完了する Future
    （_Pipeline は埋め込み・準備の完了と一緒にこれを待つ）。
    on_written は1ファイルを書き終えるごとに、書き込みのスレッドで呼ぶ（_Pipeline._written）。
    """

    def __init__(self, writer: _Writer, should_stop: Callable[[], bool] | None = None):
        self.writer = writer
        self.name = writer.name
        self.keyword_sources = writer.keyword_sources
        self.should_stop = should_stop
        self.on_written: Callable[[_Prepared], None] | None = None
        self.on_block: Callable[[], None] | None = None
        self.failed: Future = Future()
        # 書き込みのキュー（(kind, args, ベクターの値の数)）と、たまっているベクターの値の数。
        self._tasks: deque[tuple[str, tuple, int]] = deque()
        self._floats = 0
        self._cond = threading.Condition()
        # 停止を求められて書き込みを捨て始めたか（_put）。
        self.dropping = False
        self._error: BaseException | None = None
        self._aborting = False
        self._thread = threading.Thread(target=self._run, name=f"vector_db_write_{writer.name}", daemon=True)
        self._thread.start()

    def backlog(self) -> int:
        """キューにたまっている書き込みの数。"""
        with self._cond:
            return len(self._tasks)

    def check(self) -> None:
        """書き込みのスレッドがエラーで止まっていたら、その例外を投げる。"""
        if self._error is not None:
            raise self._error

    def _full(self, floats: int) -> bool:
        if not self._tasks:
            return False
        return len(self._tasks) >= _WRITE_QUEUE_LIMIT or self._floats + floats > _WRITE_QUEUE_MAX_FLOATS

    def _put(self, kind: str, args: tuple, *, droppable: bool = True) -> None:
        floats = _task_floats(kind, args)
        blocked = False
        while True:
            self.check()
            if droppable and self.dropping:
                return
            with self._cond:
                if not self._full(floats):
                    self._tasks.append((kind, args, floats))
                    self._floats += floats
                    self._cond.notify_all()
                    return
                if blocked:
                    self._cond.wait(_STOP_POLL_SECONDS)
            if not blocked:
                # 枠のロックを取るため、キューのロックの外で呼ぶ。
                blocked = True
                if self.on_block is not None:
                    self.on_block()
                continue
            if droppable and self.should_stop is not None and self.should_stop():
                logger.info("vector_db ingest: collection=%s stop requested while the write queue is full; dropping the remaining writes", self.name)
                self.dropping = True
                return

    def write(self, prepared: _Prepared) -> None:
        self._put("write", (prepared,))

    def append_chunks(self, prepared: _Prepared, start: int, chunks: list[ChunkRecord], vectors: list[Sequence[float]]) -> None:
        self._put("append", (prepared, start, chunks, vectors))

    def remove(self, source: str) -> None:
        self._put("remove", (source,))

    def finish(self) -> None:
        """キューの書き込みを全部終えてコミットし、書き込みのスレッドを終える。"""
        self._put("finish", (), droppable=False)
        self._thread.join()
        self.check()

    def abort(self) -> None:
        """キューの残りを書かずに書き込みのスレッドを止め、コミットしていない書き込みを捨てる。"""
        self._aborting = True
        with self._cond:
            self._cond.notify_all()
        self._thread.join()

    def _get(self, timeout: float) -> tuple[str, tuple] | None:
        with self._cond:
            if not self._tasks:
                self._cond.wait(timeout)
            if not self._tasks:
                return None
            kind, args, floats = self._tasks.popleft()
            self._floats -= floats
            self._cond.notify_all()
            return kind, args

    def _run(self) -> None:
        writer = self.writer
        try:
            while True:
                if self._aborting:
                    writer.abort()
                    return
                timeout = writer.seconds_until_commit()
                task = self._get(_STOP_POLL_SECONDS if timeout is None else min(timeout, _STOP_POLL_SECONDS))
                if task is None:
                    writer.commit_if_due()
                    continue
                kind, args = task
                if kind == "finish":
                    writer.finish()
                    return
                if kind == "write":
                    prepared = args[0]
                    try:
                        writer.write(prepared)
                    finally:
                        _remove_dir(prepared.page_dir)
                    if self.on_written is not None:
                        self.on_written(prepared)
                elif kind == "append":
                    writer.append_chunks(*args)
                else:
                    writer.remove(*args)
        except BaseException as e:  # noqa: BLE001 - メインスレッドで投げ直す
            self._error = e
            try:
                writer.abort()
            except Exception:  # noqa: BLE001 - 元のエラーを優先する
                logger.exception("vector_db ingest: abort after a write error failed")
            self.failed.set_result(None)


# 本文のチャンクがこの件数を超えるファイルは、ベクターをためずに分けて書き込む（_Job.stream）。
# 超えないファイルは従来どおり全部そろってから1回で入れ替える（検索に書きかけのファイルが出ない）。
_STREAM_MIN_CHUNKS = 1024
# 変わっていないファイルを続けて片付けている時間がこの秒数を超えたら、枠の空き待ちを取り下げる（_Pipeline._start_loads）。
_UNCHANGED_PAUSE_SECONDS = 0.2
# _STREAM_MIN_CHUNKS 件を超えるファイルのキーワード索引を書くとき、1回に足す行数と、ファイルの途中でもコミットする行数
# （_Writer._replace_keywords）。
_KEYWORD_PAGE_ROWS = 256
_KEYWORD_FLUSH_ROWS = 2048
# 件数のそろわないテキストを、ほかの準備・リクエストが進んでいるときに送る1リクエストの最小の件数の割合
# （embedding_batch_size に対する。_Pipeline._partial_batch_size）。
_PARTIAL_BATCH_MIN_RATIO = 0.25
# stream のファイルで、書き込み済みの位置より先に送ってよい本文のチャンク数（_Pipeline._texts_held）。
# 番号順にそろわず書き込めないベクターは、1ファイルあたりおおむねこの件数までしかためない。
_STREAM_WINDOW = 1024


class _Job:
    """埋め込みを待っている1ファイル。texts_to_embed()・pages の各埋め込みが返るたびに埋め、全部そろったら書き込む。

    seq: 準備が終わった順の番号（_Pipeline._start_embeds は、本文と画像のうち、この番号の小さいファイルの方から送る）。
    requests: このファイルの分を含む、送信中の埋め込みのリクエストの数。
    changed_at: 最後に進んだ（送信中のリクエストが無い状態から送り始めた・リクエストが返った）時刻（time.monotonic()）。
        送信中なのに長く進まないファイルを、設定ダッシュボードで詰まっているものとして見せるため。送る順番を待っていた
        時間は含めない（含めると、順番待ちの長かったファイルが送った直後から詰まっているように見える）。
    stream: 本文のチャンクが _STREAM_MIN_CHUNKS 件を超えるファイル。本文のベクターは全部そろうのを待たず、
        番号順にそろった分から保存先へ書き込み（_Writer.append_chunks）、メモリーにはためない
        （業務データの数十万チャンクの1ファイルでも、ベクターでメモリーを使い切らないため）。
    """

    def __init__(self, prepared: _Prepared, seq: int = 0):
        self.prepared = prepared
        self.seq = seq
        self.texts = prepared.texts_to_embed()
        self.text_vectors: list[Sequence[float] | None] = [None] * len(self.texts)
        self.image_vectors: list[Sequence[float] | None] = [None] * len(prepared.pages)
        self.total = len(self.texts) + len(prepared.pages)
        self.remaining = self.total
        self.requests = 0
        self.changed_at = time.monotonic()
        self.stream = prepared.kind == "embed" and len(prepared.chunks) > _STREAM_MIN_CHUNKS
        # stream のとき、書き込んだ本文のチャンクの数と、返ったが前の番号がまだそろわず書き込めないベクター。
        self.streamed = 0
        self._stream_buffer: dict[int, Sequence[float]] = {}

    def receive(self, kind: str, index: int, vector: list[float]) -> None:
        """リクエストの1件分の埋め込みを受け取る（kind は "text"・"image"、index は texts・pages の番号）。

        書き込むまで持つベクターは array('d') にする（list[float] は1要素ごとに float のオブジェクトを持つため
        約 4 倍のメモリーを使う。値はそのままで、保存先へは同じ値で書く）。
        """
        vector = array("d", vector)
        if kind == "image":
            self.image_vectors[index] = vector
        elif self.stream and index < len(self.prepared.chunks):
            self._stream_buffer[index] = vector
        else:
            self.text_vectors[index] = vector
        self.remaining -= 1

    def pop_ready_chunks(self) -> tuple[int, list[ChunkRecord], list[Sequence[float]]]:
        """stream のとき、まだ書き込んでいない本文のチャンクのうち、番号順にベクターがそろった分を取り出す。

        Returns: (最初のチャンクの番号, チャンク, ベクター)。そろった分が無ければチャンクは空。
        """
        start = end = self.streamed
        vectors = []
        while end in self._stream_buffer:
            vectors.append(self._stream_buffer.pop(end))
            end += 1
        self.streamed = end
        return start, self.prepared.chunks[start:end], vectors

    def finish(self) -> _Prepared:
        self.prepared.set_vectors(self.text_vectors, self.image_vectors)
        if self.stream:
            self.prepared.vectors = []
            self.prepared.streamed = True
        return self.prepared


class _Pipeline:
    """取り込み元のファイルを、準備（読み取り・抽出・画像化）→ 埋め込み → 書き込みの流れで処理する。

    - 準備: [vector_db].ingest_workers 個のスレッド（ingest_processes が 1 以上なら、その数の子プロセス）で並列に行う。
    - 埋め込み: 複数のファイルのテキスト（本文のチャンクとファイルパス）を embedding_batch_size 件ずつにまとめ、
      ingest_embed_requests 個（0 なら ingest_workers 個）まで同時に送る。大きいファイルのチャンクも複数のバッチに分けて同時に送る。画像は1枚ずつ送る。
      枠が空いていれば、件数が足りなくても空いている枠へ分けて送る（_partial_batch_size。埋め込みサーバーを
      遊ばせないため。埋め込みが追いつかないときほど、まとまったバッチになる）。
    - 書き込み: 書き込みの専用のスレッド（_WriterThread）が、埋め込みのそろったファイルから1ファイルずつ書き、
      まとめてコミットする（_Writer）。メインスレッドは書き込み・コミットの終わりを待たずに次の埋め込みを送る。

    メモリーを使いすぎないよう、準備済みで埋め込みを待っているファイルは _max_waiting 個までにし、ページ画像は
    取り込み1回ごとの一時フォルダ（_pages_root）へ書き出して、埋め込むときに1枚ずつ読む。

    子プロセス（ingest_processes）が異常終了した（PDFium が壊れたファイルで落ちた等）ときは、そのとき準備中だった
    ファイルを1つずつ準備し直し、1つだけでも落ちるファイルを失敗一覧に載せて続ける（_pool_broken）。

    準備・埋め込みは、始める前に IngestSlots の枠を1つ取り、終わったら返す（ほかの取り込みと共有するとき、
    並列数を合計で守るため）。枠が空いていなければ、空くまで待つ（_SlotUser.wakeup）。

    停止を求められたら、準備中・埋め込み中のファイルの終わりを待たずに止める。準備中のファイルには停止の印のファイル
    （_LoadOptions.stop_file）で本文の抽出・画像化をやめさせ、スレッド・子プロセスの後片付けは別のスレッドで待つ。
    """

    def __init__(
        self, cfg: Config, folders: list[Path], known: dict[str, FileRecord], writer: _WriterThread, embedding_client, should_stop,
        progress, slots: IngestSlots, instance: str = "", progress_bytes: Callable[[int], None] | None = None,
    ):
        self.cfg, self.folders, self.known, self.writer = cfg, folders, known, writer
        writer.on_written = self._written
        writer.on_block = self._pause_slot_wait
        self.embedding_client, self.should_stop, self.progress = embedding_client, should_stop, progress
        self.progress_bytes = progress_bytes
        self.options = _LoadOptions.from_config(cfg)
        self.slots = slots
        self.slot_user: _SlotUser | None = None
        self.workers = slots.workers
        self.processes = slots.processes
        self.batch_size = cfg.vector_db_embedding_batch_size
        self.load_limit = slots.capacity("load")
        self.embed_limit = slots.capacity("embed")
        self._max_waiting = max(self.batch_size, 2 * max(self.embed_limit, self.load_limit))
        # 準備中のファイル: Future → (_load の引数, 1つだけで準備し直しているものか)。
        self.loads: dict[Future, tuple[tuple, bool]] = {}
        self.embeds: dict[Future, tuple[str, list[tuple[_Job, int]]]] = {}
        self.text_queue: deque[tuple[_Job, int]] = deque()
        self.image_queue: deque[tuple[_Job, int]] = deque()
        # 子プロセスの異常終了に巻き込まれ、1つずつ準備し直すファイルの _load の引数。
        self.isolate: deque[tuple] = deque()
        self.waiting = 0
        # 記録まで終わったファイルの数とサイズの累計（書き込みのスレッドが _written で数える）。
        self.completed = 0
        self.completed_bytes = 0
        # 最後に読み始めたファイル（progress の path。記録が終わったときの通知でも、表示中のファイルを変えない）。
        self.current_source = ""
        # 進捗の通知（_report・_written）は、メインスレッドと書き込みのスレッドの両方から呼ぶため、順番に並べる。
        self._progress_lock = threading.Lock()
        # 準備が終わったファイルに振る番号（_Job.seq）。
        self._job_seq = itertools.count()
        self.stopped = False
        self.load_pool = None
        self.instance = instance
        # 準備の枠が空かずに始められなかった次のファイル（次の回にこれから始める）。
        self._held_item: tuple[str, FileStat] | None = None
        # 埋め込みを待っている・埋め込み中のファイル（書き込んだら外す）。embedding_files() は別のスレッドから呼ばれる。
        self._jobs: dict[int, _Job] = {}
        self._jobs_lock = threading.Lock()
        # 埋め込み・Qdrant の応答（httpx）の循環参照のゴミがたまり続けないよう、間隔を空けて回収する（gc_pacer 参照）。
        self._gc = GcPacer()
        self._stats = _IngestStats()

    def embedding_files(self) -> list[dict]:
        """埋め込みを待っている・埋め込み中のファイルの状態（IngestSlots.snapshot() の "files"）。

        detail: ファイルのパス、done・total: 埋め込み済み・全体の件数、requests: 送信中のリクエストの数、
        idle_seconds: 最後に進んでからの秒数。
        """
        now = time.monotonic()
        with self._jobs_lock:
            return [
                {
                    "detail": str(job.prepared.path),
                    "done": job.total - job.remaining,
                    "total": job.total,
                    "requests": job.requests,
                    "idle_seconds": round(now - job.changed_at, 1),
                }
                for job in self._jobs.values()
            ]

    def _acquire(self, kind: str, detail: str) -> int | None:
        return self.slots.try_acquire(self.slot_user, kind, detail)

    def _pause_slot_wait(self) -> None:
        """しばらく枠を取りに行けない（書き込みの空きを待つ・変わっていないファイルを続けて片付ける）間は、空き待ちを
        取り下げる（空き待ちのままだと、ほかの取り込みが空いた枠を取らずに待ち、枠が使われないまま空くため）。"""
        if self.slot_user is not None:
            self.slots.pause(self.slot_user)

    def _track(self, future: Future, kind: str, index: int) -> None:
        # 完了（キャンセル・例外を含む）したら枠を返す。ほかの取り込みがすぐ使えるよう、ワーカースレッドで返す。
        future.add_done_callback(lambda _f: self.slots.release(kind, index))

    def _new_load_pool(self):
        if self.processes:
            return ProcessPoolExecutor(max_workers=self.processes, initializer=_init_load_process)
        return ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="vector_db_load")

    def run(self, files: list[tuple[str, FileStat]]) -> None:
        """files を処理する（停止したら self.stopped が True）。"""
        # 小さいファイルから始め、多くのファイルを早く検索できるようにする（同じサイズはパスの順のまま）。
        files = sorted(files, key=lambda item: item[1].st_size)
        self.total = len(files)
        self.pending = iter(files)
        self.exhausted = False
        # [vector_db].ingest_temp_dir が空ならシステムの一時フォルダの下に作る。
        temp_dir = self.cfg.vector_db_ingest_temp_dir or None
        if temp_dir is not None:
            Path(temp_dir).mkdir(parents=True, exist_ok=True)
        pages_root = tempfile.mkdtemp(prefix="locohane_vdb_pages_", dir=temp_dir)
        stop_file = Path(pages_root) / "STOP"
        self.options = replace(self.options, pages_dir=pages_root, stop_file=str(stop_file))
        self.load_pool = self._new_load_pool()
        embed_pool = ThreadPoolExecutor(max_workers=self.embed_limit, thread_name_prefix="vector_db_embed")
        self.slot_user = self.slots.join(self.instance, self.writer.name)
        self.slot_user.embedding_files = self.embedding_files
        try:
            while True:
                if not self.stopped and (self.writer.dropping or (self.should_stop is not None and self.should_stop())):
                    self.stopped = True
                if self.stopped:
                    # 準備中・埋め込み中のファイルの終わりを待たずに止める（巨大なファイルの本文の抽出・画像化を待つと、
                    # 停止までに長くかかるため）。それらのファイルは記録せず、次回の取り込みで処理する。
                    break
                self.slots.begin_round(self.slot_user)
                self._start_loads(self.load_pool)
                self._start_embeds(embed_pool)
                self.slots.end_round(self.slot_user)
                hungry = bool(self.slot_user.failed)
                if not self.loads and not self.embeds:
                    if self.stopped or (self.exhausted and not self.isolate and self._held_item is None and not self.waiting):
                        break
                    if not hungry:
                        continue
                # 枠が空くのを待っている間は _SLOT_POLL_SECONDS 秒、停止を求められうるときは _STOP_POLL_SECONDS 秒ごとに見直す。
                # 書き込みのスレッドがエラーで止まったら（writer.failed）すぐに起きる。
                timeout = _SLOT_POLL_SECONDS if hungry else None
                if self.should_stop is not None:
                    timeout = min(timeout, _STOP_POLL_SECONDS) if timeout is not None else _STOP_POLL_SECONDS
                done, _ = wait(
                    list(self.loads) + list(self.embeds) + [self.slot_user.wakeup, self.writer.failed],
                    timeout=timeout, return_when=FIRST_COMPLETED,
                )
                self.writer.check()
                # 待っていた間に送信中だったリクエストの数（完了したものを片付ける前に数える）。
                self._stats.sample(len(self.embeds))
                for future in done:
                    if future in self.loads:
                        args, isolated = self.loads.pop(future)
                        try:
                            prepared = future.result()
                        except BrokenProcessPool:
                            self._pool_broken(args, isolated)
                            continue
                        self._loaded(prepared)
                    elif future in self.embeds:
                        self._embedded(future, *self.embeds.pop(future))
                    # どちらでもなければ、異常終了した子プロセスの準備で、_pool_broken で準備し直しに回したもの。
                self._gc.tick()
                if self._stats.due():
                    self._stats.log(self)
        finally:
            # 停止したとき・埋め込みサーバーに接続できない等で止めるときは、まだ始まっていない処理を始めない。
            unfinished = [future for future in list(self.loads) + list(self.embeds) if not future.cancel()]
            self.slots.leave(self.slot_user)
            load_pool = self.load_pool

            def close_pools() -> None:
                embed_pool.shutdown(wait=True, cancel_futures=True)
                if not self.processes and self.cfg.vector_db_image_input:
                    _close_office_in_threads(load_pool, self.workers)
                load_pool.shutdown(wait=True, cancel_futures=True)
                # 書き込まずに終えたファイル（停止・エラー）のページ画像もここで消す。
                _remove_dir(Path(pages_root))

            if unfinished:
                # 準備中のファイルの本文の抽出・画像化（Office の PDF への書き出しも）を途中でやめさせる（_load 参照）。
                try:
                    stop_file.touch()
                except OSError:
                    logger.warning("vector_db ingest: could not create the stop marker %s", stop_file, exc_info=True)
                # 停止で途中の準備・埋め込みを残したときは、その終わりを待つ後片付け（スレッド・子プロセス・Office の終了と
                # ページ画像の削除）を別のスレッドで行い、取り込みはすぐに終える。結果は使わない。枠（IngestSlots）は
                # 処理が終わるまで使用中のまま残り、ほかの取り込みが並列数を超えて動くことはない。
                threading.Thread(target=close_pools, name="vector_db_ingest_cleanup", daemon=True).start()
            else:
                close_pools()

    def _pool_broken(self, args: tuple, isolated: bool) -> None:
        """子プロセスが異常終了した。巻き込まれた準備中のファイルを1つずつ準備し直す（新しいプロセスで）。

        1つだけで準備していたファイル（isolated）で落ちたなら、そのファイルが原因のため失敗一覧に載せる。
        """
        path, record = args[2], args[4]
        if isolated:
            logger.warning("vector_db ingest: skipping a file whose child process crashed: %s", path)
            reason = "The child process for text extraction/rendering crashed (broken file, etc.)"
            self._loaded(_Prepared(path, record, _file_mode(self.options, path), "failed", reason=reason))
        else:
            self.isolate.append(args)
        # 同じプロセスプールの他の準備も失敗するため、1つずつ準備し直す。
        for other_args, _isolated in self.loads.values():
            self.isolate.append(other_args)
        self.loads.clear()
        self.load_pool.shutdown(wait=False, cancel_futures=True)
        self.load_pool = self._new_load_pool()

    def _submit_load(self, pool, func, args: tuple, isolated: bool) -> bool:
        """枠を取れたら _load を始める（取れなければ False）。"""
        index = self._acquire("load", str(args[2]))
        if index is None:
            return False
        try:
            future = pool.submit(func, *args)
        except BaseException:
            self.slots.release("load", index)
            raise
        self._track(future, "load", index)
        self.loads[future] = (args, isolated)
        return True

    def _start_loads(self, pool) -> None:
        if self.isolate:
            # 異常終了に巻き込まれたファイルは、他のファイルと同時に準備せず1つずつ準備し直す。
            if not self.loads and not self.stopped and self._submit_load(pool, _load, self.isolate[0], True):
                self.isolate.popleft()
            return
        started = time.monotonic()
        paused = False
        while not self.stopped and not self.exhausted and len(self.loads) < self.load_limit and self.waiting < self._max_waiting:
            item, self._held_item = self._held_item or next(self.pending, None), None
            if item is None:
                self.exhausted = True
                return
            if self.writer.dropping or (self.should_stop is not None and self.should_stop()):
                self.stopped = True
                return
            source, st = item
            path = Path(source)
            record = self.known.get(source)
            needs_keywords = source not in self.writer.keyword_sources
            if not needs_keywords and _is_unchanged(record, _file_mode(self.options, path), st):
                # 変わっていないファイルは読まないため、スレッド・子プロセスに渡さずにここで済ませる。
                self._report(source)
                mode = _file_mode(self.options, path)
                path_text = relative_path_text(source_root(self.folders, path), path)
                self._loaded(_Prepared(path, record, mode, "unchanged", path_text=path_text))
                if not paused and time.monotonic() - started >= _UNCHANGED_PAUSE_SECONDS:
                    # 変わっていないファイルが続いて枠を取りに行けない間は、前の回の空き待ちを取り下げる。
                    paused = True
                    self._pause_slot_wait()
                continue
            args = (self.options, source_root(self.folders, path), path, st, record, needs_keywords)
            if not self._submit_load(pool, _load if self.processes else _load_in_thread, args, False):
                self._held_item = item
                return
            self._report(source)

    def _report(self, source: str | None = None) -> None:
        """記録まで終わった件数を progress へ通知する（source はこれから読み始めるファイル。省略時は直前のまま）。

        メインスレッド（読み始め）と書き込みのスレッド（_written）の両方から呼ぶため、_progress_lock の中で通知する。
        """
        with self._progress_lock:
            if source is not None:
                self.current_source = source
            if self.progress is not None:
                self.progress(self.completed, self.total, self.current_source)

    def _loaded(self, prepared: _Prepared) -> None:
        job = _Job(prepared, next(self._job_seq))
        if not job.remaining:
            self._write(job)
            return
        self.waiting += 1
        with self._jobs_lock:
            self._jobs[id(job)] = job
        self.text_queue.extend((job, i) for i in range(len(job.texts)))
        self.image_queue.extend((job, i) for i in range(len(prepared.pages)))

    def _texts_held(self) -> bool:
        """次に送るテキストが、書き込み済みの位置より _STREAM_WINDOW 件以上先の stream のファイル（_Job.stream）のものか。

        前の番号のリクエストが再試行等で返らない間に後ろの番号だけが返り続けると、番号順に書き込めないベクターが
        たまり続けるため、そのファイルの本文は前の番号が返るまで送らない（前の番号は必ず送信中のため、待てば進む）。
        """
        if not self.text_queue:
            return False
        job, index = self.text_queue[0]
        return job.stream and index - job.streamed >= _STREAM_WINDOW

    def _start_embeds(self, pool) -> None:
        """埋め込みのリクエストを枠の空く限り送る。

        本文（text_queue）と画像（image_queue）は、それぞれの先頭のうち先に準備が終わったファイル（_Job.seq の小さい方）
        から送る（同じファイルなら本文から）。本文が1バッチ分たまっていれば常に本文から送ると、後から準備が終わった
        ファイルの本文が、先に本文を送り終えて画像だけが残ったファイルを追い越し続け、そのファイルが後回しになるため。
        """
        while not self.stopped and len(self.embeds) < self.embed_limit:
            count = 0
            if self.text_queue and not self._texts_held():
                count = self.batch_size if len(self.text_queue) >= self.batch_size else self._partial_batch_size()
            if self.image_queue and (not count or self.image_queue[0][0].seq < self.text_queue[0][0].seq):
                started = self._submit_image(pool)
            elif count:
                started = self._submit_texts(pool, count)
            else:
                return
            if not started:
                return

    def _partial_batch_size(self) -> int:
        """embedding_batch_size 件に満たないテキストを、空いている枠で分けて送るときの1リクエストの件数（0 なら今は送らない）。

        件数がそろうのを待たずに、空いている枠（この取り込みの残りと、共有の枠のうちこの取り込みの取り分
        （IngestSlots.available_for）の少ない方）へ均等に分けて送る（埋め込みサーバーを遊ばせないため）。
        サーバーが追いつかず枠が埋まっているときほど、キューにたまってまとまったバッチになる。
        ただし準備中のファイル（この後テキストが増える）と送信中のリクエストの両方があるときは、1リクエストを
        embedding_batch_size の _PARTIAL_BATCH_MIN_RATIO 以上にし、それに満たなければ待つ（小さいファイルの数件ずつの
        リクエストを多数送って、リクエストのオーバーヘッドでかえって遅くならないため）。
        """
        free = min(self.embed_limit - len(self.embeds), self.slots.available_for(self.slot_user, "embed"))
        size = min(self.batch_size, max(1, -(-len(self.text_queue) // max(free, 1))))
        if self.loads and self.embeds:
            minimum = min(self.batch_size, max(1, int(self.batch_size * _PARTIAL_BATCH_MIN_RATIO)))
            if len(self.text_queue) < minimum:
                return 0
            size = max(size, minimum)
        return size

    def _submit_embed(self, pool, kind: str, items: list[tuple[_Job, int]], detail: str, func, /, *args, **kwargs) -> bool:
        index = self._acquire("embed", detail)
        if index is None:
            return False
        try:
            future = pool.submit(func, *args, **kwargs)
        except BaseException:
            self.slots.release("embed", index)
            raise
        self._track(future, "embed", index)
        self.embeds[future] = (kind, items)
        with self._jobs_lock:
            now = time.monotonic()
            for job in _jobs_of(items):
                if not job.requests:
                    # 送る順番を待っていた時間は「進んでいない」に数えない（_Job.changed_at 参照）。
                    job.changed_at = now
                job.requests += 1
        return True

    def _submit_image(self, pool) -> bool:
        job, index = item = self.image_queue[0]
        page = job.prepared.pages[index]
        detail = f"{job.prepared.path} ({page.locator})"
        if not self._submit_embed(pool, "image", [item], detail, _embed_page, self.cfg, page, self.embedding_client, self.should_stop):
            return False
        self.image_queue.popleft()
        return True

    def _submit_texts(self, pool, count: int) -> bool:
        items = list(itertools.islice(self.text_queue, count))
        files = list(dict.fromkeys(str(job.prepared.path) for job, _ in items))
        detail = files[0] if len(files) == 1 else f"{files[0]} (+{len(files) - 1})"
        texts = [job.texts[i] for job, i in items]
        if not self._submit_embed(
            pool, "text", items, detail,
            embed_texts, self.cfg, texts, kind="document", client=self.embedding_client, should_stop=self.should_stop,
        ):
            return False
        for _ in range(count):
            self.text_queue.popleft()
        return True

    def _embedded(self, future: Future, kind: str, items: list[tuple[_Job, int]]) -> None:
        try:
            vectors = future.result()
        except EmbeddingStopped:
            # 埋め込みの再試行中に停止を求められた。このファイルは記録せず、次回の取り込みで処理する。
            self.stopped = True
            return
        finally:
            with self._jobs_lock:
                now = time.monotonic()
                for job in _jobs_of(items):
                    job.requests -= 1
                    job.changed_at = now
        self._stats.done(len(items))
        for (job, index), vector in zip(items, vectors, strict=True):
            job.receive(kind, index, vector)
        for job in _jobs_of(items):
            if job.stream:
                start, chunks, vectors = job.pop_ready_chunks()
                if chunks:
                    self.writer.append_chunks(job.prepared, start, chunks, vectors)
            if not job.remaining:
                self.waiting -= 1
                self._write(job)

    def _write(self, job: _Job) -> None:
        with self._jobs_lock:
            self._jobs.pop(id(job), None)
        self.writer.write(job.finish())

    def _written(self, prepared: _Prepared) -> None:
        """prepared を記録まで書き終えた（書き込みのスレッドで呼ばれる）。"""
        with self._progress_lock:
            self.completed += 1
            # 読み始めたときだけ通知すると、準備の枠が埋まっている間や最後のファイルの準備が終わった後は、
            # 記録が進んでも進捗（バー・○ / ○ ファイル）が止まったままになり、一覧の文書の件数とずれる。
            if self.progress is not None:
                self.progress(self.completed, self.total, self.current_source)
            # stat が無いのは、変わっていない・取り込めなかったファイル（_load）。
            if prepared.stat is not None:
                self.completed_bytes += prepared.stat.st_size
                if self.progress_bytes is not None:
                    self.progress_bytes(self.completed_bytes)


def _jobs_of(items: list[tuple[_Job, int]]) -> list[_Job]:
    """リクエストの items に含まれるファイル（重複なし、順序はそのまま）。"""
    return list({id(job): job for job, _ in items}.values())


def _embed_page(cfg: Config, page: PageImage, client, should_stop) -> list[list[float]]:
    """ページ画像1枚を埋め込む（書き出した PNG は埋め込むときに読む。_Pipeline 参照）。"""
    return embed_images(cfg, [page.read_png()], client=client, should_stop=should_stop)


def _reconcile_journal(catalog: Catalog, store, keywords: KeywordIndex, journal: IngestJournal, name: str) -> None:
    """前回の取り込み・コピーが強制終了等で書きかけのまま残したファイル（journal 参照）を片付ける。

    カタログに記録があるファイルは、保存先の中身が記録と合っている保証が無いため、記録のサイズ・SHA-256 を
    消して必ず取り込み直させる（フォルダから消えていれば remove で消える）。記録が無いファイルは、保存先・
    キーワード索引から消す（記録が無いと、フォルダから消えても消す手がかりが無いため）。
    まとめ書き（begin_batch）の前に呼び、1件ずつその場でコミットする。途中で止まっても次回やり直せるよう、
    ジャーナルからは最後に消す。
    """
    sources = journal.sources(name)
    if not sources:
        return
    files = catalog.files(name)
    for source in sources:
        record = files.get(source)
        if record is None:
            for storage in storage_collection_names(name):
                store.delete_source(storage, source)
            keywords.delete_source(name, source)
            keywords.delete_source(path_collection_name(name), source)
        else:
            catalog.upsert_file(name, replace(record, size=-1, sha256=""))
    logger.warning(
        "vector_db ingest: collection=%s found %d files left half-written by an interrupted ingestion or copy; "
        "re-ingesting the recorded ones and removing the chunks of the unrecorded ones",
        name, len(sources),
    )
    journal.discard(name, sources)


def _ingest(
    cfg, catalog: Catalog, store, keywords: KeywordIndex, journal: IngestJournal, name, dim, model, folders, recursive, result, progress,
    should_stop, embedding_client, slots: IngestSlots, instance: str, *, exclude_keywords: Sequence[str] = (),
    include_keywords: Sequence[str] = (), progress_bytes: Callable[[int], None] | None = None,
) -> None:
    """取り込み元のファイルを _Pipeline で処理し、フォルダから消えたファイル（除外・対象キーワードで外れたものも）を消す。

    停止したときは、埋め込みまで終わったファイルだけを記録する（途中のファイルは次回の取り込みで処理する）。
    """
    _reconcile_journal(catalog, store, keywords, journal, name)
    known = catalog.files(name)
    max_files = cfg.vector_db_max_files_per_collection
    scan = scan_source_files(
        folders, recursive, cfg.vector_db_include_extensions,
        workers=cfg.vector_db_scan_workers, max_files=max_files, known=known,
        exclude_keywords=exclude_keywords, include_keywords=include_keywords, text_extensions=cfg.vector_db_text_extensions,
        should_stop=should_stop,
    )
    if scan.stopped:
        # 途中までの走査では「消えた」ファイルを判定できないため、何も書かずに終える。
        logger.info("vector_db ingest: collection=%s stopped while scanning the source folders", name)
        return
    files = scan.files
    result.found_files, result.max_files, result.over_limit = scan.total, max_files, scan.over_limit
    logger.info(
        "vector_db ingest: collection=%s found %d files (%d to process, %d already recorded)", name, scan.total, len(files), len(known)
    )
    if scan.over_limit:
        logger.warning("vector_db ingest: collection=%s %s", name, _over_limit_hint(result))
    plain_writer = _Writer(cfg, catalog, store, keywords, journal, name, dim, model, result, should_stop)
    plain_writer.begin()
    writer = _WriterThread(plain_writer, should_stop)
    try:
        pipeline = _Pipeline(cfg, folders, known, writer, embedding_client, should_stop, progress, slots, instance, progress_bytes)
        pipeline.run(files)
        if not pipeline.stopped:
            # 停止したときは、未処理のファイルを「消えた」と誤って削除しないよう消さない。
            # 上限を超えて選ばれなかったファイルも、フォルダにある限り消さない（found_known で判定する）。
            for source in sorted(set(known) - scan.found_known):
                writer.remove(source)
        writer.finish()
    except BaseException:
        writer.abort()
        raise
    # 並列だと終わった順に記録されるため、失敗一覧はパス順にそろえる。
    result.failed.sort(key=lambda item: item[0].lower())
    if progress is not None:
        # 停止したときも、最後に読み始めたファイルの時点の件数のまま残らないよう、記録まで終わった件数を通知する。
        progress(pipeline.completed if pipeline.stopped else len(files), len(files), "")


def main(argv: list[str] | None = None) -> int:
    from ..config import DEFAULT_INSTANCE_NAME, load_config, resolve_instances_root

    parser = argparse.ArgumentParser(description="Ingest documents into a vector DB collection")
    parser.add_argument("--instance", default=os.getenv("LOCOHANE_INSTANCE") or None, help="Instance name (default if omitted)")
    parser.add_argument("--collection", required=True, help="Collection name (one created in the \"Knowledge\" tab of the settings dashboard)")
    parser.add_argument("--rebuild", action="store_true", help="Delete the existing vectors and ingest every file again")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    instance = args.instance or DEFAULT_INSTANCE_NAME
    # 管理ツールと同じく instances/<名前>/config_overrides.json の上書きを重ねて読む。
    cfg = load_config(overrides_path=resolve_instances_root() / instance / "config_overrides.json", instance_name=instance)
    if not cfg.vector_db_enabled:
        print("[vector_db] enabled = false. Enable it before running this.")
        return 1

    printed = [""]

    def _progress(done: int, total: int, path: str) -> None:
        # 記録が終わったときの通知は直前に読み始めたファイルの path のまま来るため、読み始めたときだけ表示する。
        if path and path != printed[0]:
            printed[0] = path
            print(f"[{done + 1}/{total}] {path}")

    result = ingest_collection(cfg, args.collection, rebuild=args.rebuild, progress=_progress)
    summary = result.to_dict()
    print(
        f"Added {summary['added']} / Updated {summary['updated']} / Unchanged {summary['unchanged']} / "
        f"Removed {summary['removed']} / Chunks {summary['chunks']} / Failed {len(summary['failed'])}"
    )
    if summary["path_only"]:
        print(f"  {summary['path_only']} of them had no extractable content, so only their file and folder names were registered.")
    for item in summary["failed"]:
        print(f"  Failed: {item['path']}: {item['reason']}")
    if summary["too_large"]:
        print(f"  {_too_large_hint(cfg, summary['too_large'])}")
    if result.over_limit:
        print(f"  {_over_limit_hint(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
