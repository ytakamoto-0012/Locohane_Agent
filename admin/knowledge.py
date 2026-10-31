"""設定ダッシュボードの「ナレッジ」タブの HTTP API（admin/server.py が build_router() で組み込む）。

インスタンスごとのベクターDB（src/vector_db/）のコレクションを作成・編集・削除し、
取り込み元フォルダの文書をバックグラウンドで取り込む。取り込みは管理ツールの
プロセス内のスレッドで行い、対象インスタンスの設定（config_overrides.json を重ねたもの）を使う。
本体インスタンスが起動中でも、sqlite は WAL モードのため検索と取り込みを同時に行える。

コレクションごとに時刻（と曜日）を指定した自動取り込み（差分）も行う（IngestScheduler）。
スケジュールは catalog.sqlite に持ち、管理ツールが起動している間だけ動く
（止まっていた間の時刻の分は、起動後にさかのぼって実行しない）。

インスタンス間のコピー（src/vector_db/collection_copy.py。取り込みと同じくジョブとしてスレッドで動かす）と、
別のインスタンスのコレクションの参照（catalog.sqlite の collection_refs。検索は src/vector_db/search.py）も扱う。

取り込みの並列数（[vector_db] ingest_workers・ingest_processes・ingest_embed_requests。「管理者設定」画面か config.ini）は、全インスタンス・全コレクションの
取り込みで1つの枠（src/vector_db/ingest.py の IngestSlots）を共有し、超える分は空き待ちにする
（インスタンス別の上書きは使わない）。コレクションの削除もジョブにし、同じインスタンスの取り込み・コピーが
終わってから行う（キーワード索引等をほかのコレクションの取り込みが書き込み中だと「database is locked」になるため。
_JobRegistry.wait_turn）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.config import Config, load_config
from src.vector_db import catalog as vcat
from src.vector_db.collection_copy import CopyError, check_compatible, copy_collection
from src.vector_db.embeddings import EmbeddingError, embedding_model
from src.vector_db.ingest import IngestError, IngestSlots, effective_extensions, ingest_collection
from src.vector_db.journal import open_journal
from src.vector_db.keyword_index import open_keyword_index
from src.vector_db.store import StoreError, open_store, path_collection_name, storage_collection_names

from . import audit
from . import instances as inst
from admin.i18n import t as _t

CONFIG_INI_PATH = Path(__file__).resolve().parent.parent / "config.ini"
SCHEDULER_ACTOR = "scheduler"

logger = logging.getLogger(__name__)


class CreateCollectionBody(BaseModel):
    name: str
    # 取り込み元フォルダ。source_folders（複数）か、旧版の API の source_folder（1つ）のどちらかを指定する。
    source_folders: list[str] | None = None
    source_folder: str | None = None
    description: str = ""
    recursive: bool = True
    # 取り込み除外キーワード（取り込み元フォルダより下のパスに含むフォルダ・ファイルは取り込まない）。
    exclude_keywords: list[str] = []
    # 取り込み対象キーワード（あれば、取り込み元フォルダより下のパスにどれかを含むファイルだけを取り込む）。
    include_keywords: list[str] = []
    schedule_enabled: bool = False
    schedule_times: list[str] = []
    schedule_weekdays: list[int] = []


class UpdateCollectionBody(BaseModel):
    source_folders: list[str] | None = None
    source_folder: str | None = None
    description: str | None = None
    recursive: bool | None = None
    exclude_keywords: list[str] | None = None
    include_keywords: list[str] | None = None
    schedule_enabled: bool | None = None
    schedule_times: list[str] | None = None
    schedule_weekdays: list[int] | None = None


class IngestBody(BaseModel):
    rebuild: bool = False


class CopyBody(BaseModel):
    dest_instance: str
    dest_name: str
    with_vectors: bool = True
    include_schedule: bool = False


class ReferenceBody(BaseModel):
    name: str
    source_instance: str
    source_collection: str


@dataclass
class IngestJob:
    """1コレクションの取り込み（またはコピー・削除）のジョブの状態（画面のポーリング用）。"""

    collection: str
    rebuild: bool
    actor: str
    trigger: str = "manual"  # manual / schedule（時間指定の自動取り込み）
    kind: str = "ingest"  # ingest / copy（コピー先のコレクションのジョブ）/ delete
    source: str | None = None  # kind=copy のコピー元（"インスタンス名/コレクション名"）
    # queued（同じインスタンスの削除・取り込みの終わり待ち。_JobRegistry.wait_turn）/ running / done / error / stopped
    state: str = "queued"
    seq: int = 0  # 登録順（_JobRegistry が付ける。順番待ちの順に使う）
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None
    done: int = 0
    total: int = 0
    current: str = ""
    # 記録まで終わったファイルのサイズの累計（kind=ingest のみ。ingest_collection の progress_bytes）。
    bytes_done: int = 0
    result: dict | None = None
    error: str | None = None
    stop_requested: bool = False
    # 1秒当たりの取り込み容量の計算用の (time.monotonic(), bytes_done)。to_dict には出さない。
    _byte_samples: deque = field(default_factory=deque, repr=False, compare=False)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("_byte_samples")
        data["bytes_per_second"] = self.bytes_per_second()
        return data

    def start_byte_rate(self) -> None:
        self._byte_samples.clear()
        self._byte_samples.append((time.monotonic(), self.bytes_done))

    def add_bytes(self, bytes_done: int) -> None:
        now = time.monotonic()
        self.bytes_done = bytes_done
        self._byte_samples.append((now, bytes_done))
        # 直近 BYTE_RATE_WINDOW_SECONDS 秒より前の点は、窓の始まりに当たる1点だけ残す。
        while len(self._byte_samples) >= 2 and self._byte_samples[1][0] <= now - BYTE_RATE_WINDOW_SECONDS:
            self._byte_samples.popleft()

    def bytes_per_second(self) -> float:
        """直近（BYTE_RATE_WINDOW_SECONDS 秒ほど）の1秒当たりの取り込み容量（取り込み中でなければ 0）。

        大きいファイルは記録まで終わったときにまとめて数えるため、短い窓だと値が跳ねる。そのため窓は長めにする。
        """
        if self.state != "running" or not self._byte_samples:
            return 0.0
        since, base = self._byte_samples[0]
        elapsed = time.monotonic() - since
        return round((self.bytes_done - base) / elapsed, 1) if elapsed > 0 else 0.0


BYTE_RATE_WINDOW_SECONDS = 30.0


ACTIVE_STATES = ("queued", "running")


def _active(job: IngestJob | None) -> bool:
    return job is not None and job.state in ACTIVE_STATES


class _JobRegistry:
    """(インスタンス名, コレクション名) ごとに最新の取り込みジョブを1件持つ。

    コピー中のコピー元のコレクション（copy_sources）は、コピーの途中で中身が変わらないよう取り込みを始めない。

    同じインスタンスの中では、削除のジョブとほかのジョブ（取り込み・コピー先）を同時に動かさない
    （_can_run。登録順に、削除は先に登録されたジョブがすべて終わってから、取り込み・コピーは先に登録された
    削除が終わってから動く）。始められないジョブは queued のまま wait_turn() で待つ。
    """

    WAIT_POLL_SECONDS = 1.0

    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._jobs: dict[tuple[str, str], IngestJob] = {}
        self._copy_sources: dict[tuple[str, str], int] = {}
        self._seq = 0

    def _register(self, instance: str, job: IngestJob) -> None:
        """job を登録し、すぐ始められるなら running にする（ロックを取って呼ぶ）。"""
        self._seq += 1
        job.seq = self._seq
        self._jobs[(instance, job.collection)] = job
        if job.state == "queued" and self._can_run(instance, job):
            job.state = "running"

    def _can_run(self, instance: str, job: IngestJob) -> bool:
        for (i, _c), other in self._jobs.items():
            if i != instance or other is job or not _active(other):
                continue
            earlier = other.state == "running" or other.seq < job.seq
            if job.kind == "delete" and earlier:
                return False
            if job.kind != "delete" and other.kind == "delete" and earlier:
                return False
        return True

    def wait_turn(self, instance: str, job: IngestJob) -> bool:
        """job を始められるまで待って running にする（待つ間に停止を求められたら stopped にして False）。"""
        with self._lock:
            if job.state == "queued":
                logger.info("Knowledge: %s %s/%s is waiting for other jobs of the instance to finish.", job.kind, instance, job.collection)
            while job.state == "queued":
                # 始められるかより先に停止を見る（停止を求めたのと同時に先のジョブが終わっても、取りやめる）。
                if job.stop_requested:
                    job.state = "stopped"
                    job.finished_at = datetime.now().isoformat(timespec="seconds")
                    self._lock.notify_all()
                    return False
                if self._can_run(instance, job):
                    break
                self._lock.wait(self.WAIT_POLL_SECONDS)
            job.state = "running"
            return True

    def notify(self) -> None:
        """ジョブが終わった・停止を求められた（順番待ちのジョブを見直させる）。"""
        with self._lock:
            self._lock.notify_all()

    def is_copy_source(self, instance: str, collection: str) -> bool:
        with self._lock:
            return self._copy_sources.get((instance, collection), 0) > 0

    def start_copy(self, instance: str, job: IngestJob, source_instance: str, source_collection: str) -> bool:
        """コピー先 (instance, job.collection) のジョブを登録し、コピー元を取り込み不可にする。

        コピー先・コピー元のどちらかが取り込み中（削除待ち・削除中も）なら False。
        """
        with self._lock:
            for key in ((instance, job.collection), (source_instance, source_collection)):
                if _active(self._jobs.get(key)):
                    return False
            self._register(instance, job)
            key = (source_instance, source_collection)
            self._copy_sources[key] = self._copy_sources.get(key, 0) + 1
            return True

    def finish_copy(self, source_instance: str, source_collection: str) -> None:
        with self._lock:
            key = (source_instance, source_collection)
            count = self._copy_sources.get(key, 0) - 1
            if count > 0:
                self._copy_sources[key] = count
            else:
                self._copy_sources.pop(key, None)

    def get(self, instance: str, collection: str) -> IngestJob | None:
        with self._lock:
            return self._jobs.get((instance, collection))

    def for_instance(self, instance: str) -> dict[str, IngestJob]:
        with self._lock:
            return {c: j for (i, c), j in self._jobs.items() if i == instance}

    def running(self) -> dict[str, list[IngestJob]]:
        """取り込み中（順番待ちを含む）のジョブをインスタンス名ごとに返す（インスタンス一覧のカード用）。"""
        with self._lock:
            result: dict[str, list[IngestJob]] = {}
            for (i, _c), j in sorted(self._jobs.items()):
                if _active(j):
                    result.setdefault(i, []).append(j)
            return result

    def start(self, instance: str, job: IngestJob) -> bool:
        """job を登録する（同じコレクションのジョブが動いている・順番待ち中、またはコピー元なら False）。

        すぐ始められるなら job.state は running、同じインスタンスの削除等を待つなら queued になる。
        """
        with self._lock:
            if _active(self._jobs.get((instance, job.collection))):
                return False
            if self._copy_sources.get((instance, job.collection), 0) > 0:
                return False
            self._register(instance, job)
            return True

    def forget(self, instance: str, collection: str) -> None:
        with self._lock:
            self._jobs.pop((instance, collection), None)
            self._lock.notify_all()


def _log_job_end(instance: str, job: IngestJob, started: datetime) -> None:
    seconds = (datetime.now() - started).total_seconds()
    if job.state == "error":
        logger.warning("Knowledge: %s %s/%s failed after %.1fs: %s", job.kind, instance, job.collection, seconds, job.error)
    else:
        logger.info("Knowledge: %s %s/%s finished (%s) in %.1fs.", job.kind, instance, job.collection, job.state, seconds)


def run_ingest_job(cfg: Config, job: IngestJob, *, jobs: _JobRegistry | None = None, instance: str = "", **kwargs) -> None:
    """job の取り込みを実行し、結果・エラーを job に書き込む（スレッドの本体）。

    jobs を渡したら、同じインスタンスの削除が終わるまで待ってから始め（_JobRegistry.wait_turn）、終わったら知らせる。
    kwargs は ingest_collection() へ渡す（slots 等）。
    """

    def _progress(done: int, total: int, path: str) -> None:
        job.done, job.total, job.current = done, total, path

    if jobs is not None and not jobs.wait_turn(instance, job):
        logger.info("Knowledge: ingest %s/%s was cancelled while waiting.", instance, job.collection)
        return
    job.state = "running"
    job.start_byte_rate()
    started = datetime.now()
    logger.info(
        "Knowledge: ingest %s/%s started (rebuild=%s, trigger=%s, actor=%s).", instance, job.collection, job.rebuild, job.trigger, job.actor
    )
    try:
        result = ingest_collection(
            cfg, job.collection, rebuild=job.rebuild, progress=_progress, should_stop=lambda: job.stop_requested,
            instance=instance, progress_bytes=job.add_bytes, **kwargs,
        )
        job.result = result.to_dict()
        job.state = "stopped" if job.stop_requested else "done"
    except (IngestError, EmbeddingError, StoreError, vcat.CatalogError, OSError) as exc:
        logger.warning("Knowledge: ingest %s/%s error: %s", instance, job.collection, exc, exc_info=True)
        job.error = str(exc)
        job.state = "error"
    except Exception as exc:  # noqa: BLE001 - 想定外の例外も画面に出して、ジョブが running のまま残らないようにする
        logger.exception("Knowledge: ingest %s/%s unexpected error.", instance, job.collection)
        job.error = f"{type(exc).__name__}: {exc}"
        job.state = "error"
    finally:
        job.current = ""
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        _log_job_end(instance, job, started)
        if jobs is not None:
            jobs.notify()


def run_delete_job(cfg: Config, instance: str, job: IngestJob, jobs: _JobRegistry, *, store_transport=None) -> None:
    """コレクション job.collection のベクター・キーワード索引・記録を消す（順番が来るまで待つ）。

    成功したらジョブの記録も消し、失敗したら error のジョブを残す（画面のその行にエラーを出す）。
    消す順は保存先 → キーワード索引 → カタログ（途中で失敗しても一覧に残り、削除をやり直せる）。
    """
    if not jobs.wait_turn(instance, job):
        logger.info("Knowledge: delete %s/%s was cancelled while waiting.", instance, job.collection)
        return
    started = datetime.now()
    logger.info("Knowledge: delete %s/%s started (actor=%s).", instance, job.collection, job.actor)
    try:
        with vcat.open_catalog(cfg.vector_db_dir) as catalog, open_journal(cfg.vector_db_dir) as journal:
            catalog.get(job.collection)
            # 途中で止まって一覧に残ったコレクションは、記録はあるのにベクターが消えているため、
            # 次の取り込みで作り直させる（journal.begin_drop。削除をやり直しても消える）。
            journal.begin_drop(job.collection)
            # 削除は止める手段が無く、画面の要求の中で動くこともあるため、Qdrant への送り直しは検索と同じく抑える
            # （失敗したらエラーの行が残り、削除をやり直せる）。
            store = open_store(cfg, transport=store_transport, unstoppable=True)
            try:
                for storage in storage_collection_names(job.collection):
                    store.drop_collection(storage)
            finally:
                store.close()
            with open_keyword_index(cfg.vector_db_dir) as keywords:
                keywords.drop_collection(job.collection)
                keywords.drop_collection(path_collection_name(job.collection))
            catalog.delete(job.collection)
            journal.clear(job.collection)
        job.state = "done"
    except StoreError as exc:
        logger.warning("Knowledge: delete %s/%s error: %s", instance, job.collection, exc, exc_info=True)
        job.error = _t("ベクターを削除できませんでした: {0}", exc)
        job.state = "error"
    except Exception as exc:  # noqa: BLE001 - sqlite のロック等も画面に出して、ジョブが running のまま残らないようにする
        logger.exception("Knowledge: delete %s/%s unexpected error.", instance, job.collection)
        job.error = _t("削除できませんでした: {0}", f"{type(exc).__name__}: {exc}")
        job.state = "error"
    finally:
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        _log_job_end(instance, job, started)
        if job.state == "done":
            jobs.forget(instance, job.collection)
        else:
            jobs.notify()


def run_copy_job(
    src_cfg: Config,
    src_instance: str,
    src_name: str,
    dst_cfg: Config,
    job: IngestJob,
    jobs: _JobRegistry,
    *,
    with_vectors: bool,
    include_schedule: bool,
    store_transport=None,
    dst_instance: str = "",
) -> None:
    """コピーを実行し、結果・エラーを job に書き込む（スレッドの本体）。終わったらコピー元の取り込みを再び許す。

    コピー先のインスタンス dst_instance の削除が終わるまで待ってから始める（_JobRegistry.wait_turn）。
    """

    def _progress(done: int, total: int, path: str) -> None:
        job.done, job.total, job.current = done, total, path

    if not jobs.wait_turn(dst_instance, job):
        logger.info("Knowledge: copy %s -> %s/%s was cancelled while waiting.", job.source, dst_instance, job.collection)
        jobs.finish_copy(src_instance, src_name)
        return
    started = datetime.now()
    logger.info(
        "Knowledge: copy %s -> %s/%s started (with_vectors=%s, actor=%s).", job.source, dst_instance, job.collection, with_vectors, job.actor
    )
    try:
        job.result = copy_collection(
            src_cfg,
            src_name,
            dst_cfg,
            job.collection,
            with_vectors=with_vectors,
            include_schedule=include_schedule,
            progress=_progress,
            should_stop=lambda: job.stop_requested,
            src_store_transport=store_transport,
            dst_store_transport=store_transport,
        )
        job.state = "stopped" if job.result.get("stopped") else "done"
    except (CopyError, StoreError, vcat.CatalogError, OSError) as exc:
        logger.warning("Knowledge: copy %s -> %s/%s error: %s", job.source, dst_instance, job.collection, exc, exc_info=True)
        job.error = str(exc)
        job.state = "error"
    except Exception as exc:  # noqa: BLE001 - 想定外の例外も画面に出して、ジョブが running のまま残らないようにする
        logger.exception("Knowledge: copy %s -> %s/%s unexpected error.", job.source, dst_instance, job.collection)
        job.error = f"{type(exc).__name__}: {exc}"
        job.state = "error"
    finally:
        job.current = ""
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        _log_job_end(dst_instance, job, started)
        jobs.finish_copy(src_instance, src_name)
        jobs.notify()


def job_workers(snapshot: dict, instance: str, collection: str) -> dict:
    """IngestSlots.snapshot() のうち、instance/collection の取り込みの分。

    load: 準備中のファイル、files: 埋め込みを待っている・埋め込み中のファイル（_Pipeline.embedding_files）、
    requests: 送信中の埋め込みのリクエストの数、slots: 取り込みの並列数（埋め込みのアイコンの数）、request_slots: 埋め込みのリクエストの並列数、waiting: 枠の空きを待っているか。
    """
    def mine(slot: dict | None) -> bool:
        return slot is not None and slot["instance"] == instance and slot["collection"] == collection

    def strip(slot: dict) -> dict:
        return {k: v for k, v in slot.items() if k not in ("instance", "collection")}

    return {
        "load": [strip(s) for s in snapshot["load"] if mine(s)],
        "files": [strip(f) for f in snapshot["files"] if mine(f)],
        "requests": sum(1 for s in snapshot["embed"] if mine(s)),
        "slots": snapshot["workers"],
        "request_slots": snapshot["requests"],
        "waiting": any(mine(w) for w in snapshot["waiting"]),
    }


def shared_ingest_limits(config_ini_path: Path) -> tuple[int, int, int]:
    """全インスタンス共通の取り込みの並列数 (ingest_workers, ingest_processes, ingest_embed_requests)。

    config.ini に設定ダッシュボードの「管理者設定」（admin_settings.json）と環境変数を重ねた値で、インスタンス別の
    config_overrides.json は重ねない（存在しない overrides_path を渡し、default インスタンスの上書きも読まないようにする）。
    """
    cfg = load_config(config_path=config_ini_path, overrides_path=Path(os.devnull))
    return cfg.vector_db_ingest_workers, cfg.vector_db_ingest_processes, cfg.vector_db_ingest_embed_requests


def next_run_at(times, weekdays, now: datetime) -> datetime | None:
    """時刻 times（"HH:MM"、昇順）・曜日 weekdays（0=月〜6=日、空は毎日）の予定が次に来る日時（now より後。無ければ None）。

    ナレッジの自動取り込みと、データのバックアップ（admin/data_backup.py）で使う。
    """
    if not times:
        return None
    base = now.replace(second=0, microsecond=0)
    for day in range(8):
        date = base + timedelta(days=day)
        if weekdays and date.weekday() not in weekdays:
            continue
        for t in times:
            hour, minute = (int(x) for x in t.split(":"))
            candidate = date.replace(hour=hour, minute=minute)
            if candidate > now:
                return candidate
    return None


def is_due_at(times, weekdays, minute: datetime) -> bool:
    """minute（秒以下は見ない）が、時刻 times・曜日 weekdays の予定に当たるか。"""
    if weekdays and minute.weekday() not in weekdays:
        return False
    return minute.strftime("%H:%M") in times


def due_minutes(last: datetime | None, now: datetime, max_catch_up: int) -> list[datetime]:
    """前回確認した分 last の次の分から now の分までを順に返す（遡るのは max_catch_up 分まで。初回・同じ分なら空）。

    スレッドの起床が遅れても予定の分を飛ばさないため（IngestScheduler・data_backup.BackupScheduler）。
    """
    current = now.replace(second=0, microsecond=0)
    if last is None or current <= last:
        return []
    minutes = []
    m = max(last + timedelta(minutes=1), current - timedelta(minutes=max_catch_up - 1))
    while m <= current:
        minutes.append(m)
        m += timedelta(minutes=1)
    return minutes


def next_scheduled_run(c: vcat.Collection, now: datetime) -> datetime | None:
    """c の自動取り込みが次に動く日時（now より後の最初の時刻。無効なら None）。"""
    if not c.schedule_enabled:
        return None
    return next_run_at(c.schedule_times, c.schedule_weekdays, now)


def _is_due(c: vcat.Collection, minute: datetime) -> bool:
    return c.schedule_enabled and is_due_at(c.schedule_times, c.schedule_weekdays, minute)


class IngestScheduler:
    """コレクションに指定された時刻に差分取り込みを始める（管理ツールのプロセス内のスレッド）。

    前回確認した分の次の分から現在の分までを順に見て、時刻・曜日が一致するコレクションの
    取り込みを始める（スレッドの起床が遅れても分を飛ばさない。ただし遡るのは MAX_CATCH_UP_MINUTES 分まで）。
    同じコレクションが取り込み中なら、その回は実行しない。
    """

    MAX_CATCH_UP_MINUTES = 10
    POLL_SECONDS = 15

    def __init__(
        self,
        *,
        instances_root: Path,
        jobs: _JobRegistry,
        loader: Callable[[str], Config],
        audit_log_path: Path,
        launch: Callable[[str, Config, IngestJob], None],
    ) -> None:
        """launch: 登録したジョブの取り込みをスレッドで始める関数（build_router の _launch_ingest）。"""
        self._instances_root = instances_root
        self._jobs = jobs
        self._loader = loader
        self._audit_log_path = audit_log_path
        self._launch = launch
        self._last_minute: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._last_minute = datetime.now().replace(second=0, microsecond=0)
        self._thread = threading.Thread(target=self._run, name="knowledge-ingest-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.POLL_SECONDS):
            try:
                self.tick(datetime.now())
            except Exception:  # noqa: BLE001 - 1回の失敗でスケジューラーを止めない
                logger.exception("Failed to check scheduled knowledge ingestion.")

    def tick(self, now: datetime) -> list[tuple[str, str]]:
        """前回から now までに来た時刻の取り込みを始め、始めた (インスタンス名, コレクション名) を返す。"""
        last = self._last_minute
        self._last_minute = now.replace(second=0, microsecond=0)
        minutes = due_minutes(last, now, self.MAX_CATCH_UP_MINUTES)
        if not minutes:
            return []

        started: list[tuple[str, str]] = []
        for name in inst.list_instance_names(self._instances_root):
            try:
                if inst.read_instance(self._instances_root, name).is_skill_lab:
                    continue
                cfg = self._loader(name)
            except Exception as exc:  # noqa: BLE001 - 設定の読めないインスタンスは飛ばして他を続ける
                logger.warning("Scheduled ingestion: cannot read the settings of instance %r: %s", name, exc)
                continue
            if not cfg.vector_db_enabled or not vcat.catalog_exists(cfg.vector_db_dir):
                continue
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                collections = catalog.list_collections()
            for c in collections:
                if not any(_is_due(c, minute) for minute in minutes):
                    continue
                job = IngestJob(collection=c.name, rebuild=False, actor=SCHEDULER_ACTOR, trigger="schedule")
                if not self._jobs.start(name, job):
                    logger.info("Scheduled ingestion: %s/%s is already ingesting or being copied, skipping this run.", name, c.name)
                    continue
                self._launch(name, cfg, job)
                audit.append(
                    self._audit_log_path,
                    {
                        "instance": name,
                        "actor": SCHEDULER_ACTOR,
                        "remote_addr": "-",
                        "action": "knowledge_ingest_start",
                        "collection": c.name,
                        "rebuild": False,
                        "trigger": "schedule",
                    },
                )
                logger.info("Scheduled ingestion: started diff ingestion of %s/%s.", name, c.name)
                started.append((name, c.name))
        return started


def build_router(
    *,
    require_login: Callable,
    require_csrf: Callable,
    instances_root: Path,
    audit_log_path: Path,
    client_ip: Callable[[Request], str],
    config_loader: Callable[[str], Config] | None = None,
    ingest_kwargs: dict | None = None,
    ingest_limits: Callable[[], tuple[int, int, int]] | None = None,
) -> APIRouter:
    """ナレッジ API の router を作る。

    Args:
        config_loader: インスタンス名から設定を読む関数（テスト用の差し替え口。
            既定は instances/<名前>/config_overrides.json を重ねた load_config()）。
        ingest_kwargs: ingest_collection() へ追加で渡す引数（テスト用の httpx トランスポート等）。
        ingest_limits: 全体の並列数 (ingest_workers, ingest_processes, ingest_embed_requests) を返す関数（テスト用の差し替え口。
            既定は config.ini（インスタンス別の上書きを重ねない）の [vector_db] の値。取り込みを始めるたびに読む）。

    返す router の ingest_scheduler 属性が時間指定の自動取り込みの IngestScheduler
    （手動の取り込みとジョブの記録を共有する）。start()・stop() は呼び出し側（admin/server.py の lifespan）で行う。
    ingest_jobs 属性はジョブの記録（_JobRegistry）、ingest_slots 属性は全体で共有する並列数の枠（IngestSlots）。
    """
    router = APIRouter()
    jobs = _JobRegistry()
    extra_ingest_kwargs = dict(ingest_kwargs or {})
    limits = ingest_limits or (lambda: shared_ingest_limits(CONFIG_INI_PATH))
    slots = IngestSlots(*limits())

    def _audit(request: Request, user: str, instance: str, action: str, **extra) -> None:
        audit.append(audit_log_path, {"instance": instance, "actor": user, "remote_addr": client_ip(request), "action": action, **extra})

    def _default_loader(name: str) -> Config:
        return load_config(config_path=CONFIG_INI_PATH, overrides_path=inst.overrides_path(instances_root, name), instance_name=name)

    def _refresh_limits() -> None:
        """config.ini の並列数の変更を、管理ツールを再起動せずに次の取り込みから反映する。"""
        try:
            workers, processes, requests = limits()
        except Exception as exc:  # noqa: BLE001 - 読めなければ今の並列数のまま取り込む
            logger.warning(
                "Knowledge: cannot read [vector_db] ingest_workers / ingest_processes / ingest_embed_requests from config.ini: %s", exc
            )
            return
        if (workers, processes, requests) != (slots.workers, slots.processes, slots.requests):
            logger.info(
                "Knowledge: shared ingestion parallelism changed to workers=%d processes=%d embed_requests=%d.", workers, processes, requests
            )
            slots.resize(workers, processes, requests)

    def _launch_ingest(instance: str, cfg: Config, job: IngestJob) -> None:
        _refresh_limits()
        threading.Thread(
            target=run_ingest_job,
            args=(cfg, job),
            kwargs={"jobs": jobs, "instance": instance, "slots": slots, **extra_ingest_kwargs},
            name=f"knowledge-ingest-{instance}-{job.collection}",
            daemon=True,
        ).start()

    loader = config_loader or _default_loader
    # インスタンス一覧のカード（admin/server.py の /api/monitor/overview）が取り込み中のジョブ・枠の状態を読む口
    router.ingest_jobs = jobs
    router.ingest_slots = slots
    router.ingest_scheduler = IngestScheduler(
        instances_root=instances_root, jobs=jobs, loader=loader, audit_log_path=audit_log_path, launch=_launch_ingest
    )

    def _check_app_instance(name: str) -> None:
        try:
            inst.validate_name(name)
            meta = inst.read_instance(instances_root, name)
        except inst.InstanceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if meta.is_skill_lab:
            raise HTTPException(status_code=400, detail=_t("スキル研究室のインスタンスにはナレッジを置けません。"))

    def _cfg(name: str) -> Config:
        _check_app_instance(name)
        try:
            return loader(name)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=_t("インスタンス {0} の設定を読めません: {1}", name, exc)) from exc

    def _collection_dict(c: vcat.Collection, job: IngestJob | None, referenced_by: list[str] | None = None) -> dict:
        next_run = next_scheduled_run(c, datetime.now())
        return {
            **asdict(c),
            # 旧版の API（取り込み元フォルダが1つだけ）の利用者向けに、最初のフォルダも返す。
            "source_folder": c.source_folder,
            "next_scheduled_at": next_run.isoformat(timespec="seconds") if next_run else None,
            "job": job.to_dict() if job else None,
            "referenced_by": referenced_by or [],
        }

    def _app_instances() -> list[str]:
        """ナレッジを持てるインスタンス（スキル研究室を除く）の名前。"""
        names = []
        for n in inst.list_instance_names(instances_root):
            try:
                if not inst.read_instance(instances_root, n).is_skill_lab:
                    names.append(n)
            except (inst.InstanceError, OSError, ValueError):
                continue
        return names

    def _try_cfg(name: str) -> Config | None:
        try:
            return loader(name)
        except Exception:  # noqa: BLE001 - 設定の読めないインスタンスは一覧から外す
            logger.warning("Knowledge: cannot read the settings of instance %r.", name, exc_info=True)
            return None

    def _model_or_none(cfg: Config) -> str | None:
        try:
            return embedding_model(cfg)
        except EmbeddingError:
            return None

    def _source_check(owner: str, source_instance: str) -> str | None:
        """source_instance を owner の参照元にできなければ理由を返す。"""
        if source_instance == owner:
            return _t("自分自身のインスタンスは参照できません。")
        try:
            inst.validate_name(source_instance)
            meta = inst.read_instance(instances_root, source_instance)
        except inst.InstanceError:
            return _t("インスタンス {0} がありません。", source_instance)
        if meta.is_skill_lab:
            return _t("スキル研究室のインスタンスにはナレッジがありません。")
        return None

    def _reference_dict(owner: str, owner_cfg: Config, r: vcat.Reference) -> dict:
        """参照と、参照元のコレクションの状態（ok / incompatible / missing）。"""
        d = {**asdict(r), "status": "missing", "reason": None, "collection": None}
        reason = _source_check(owner, r.source_instance)
        src_cfg = None if reason else _try_cfg(r.source_instance)
        if reason is None and src_cfg is None:
            reason = _t("インスタンス {0} の設定を読めません。", r.source_instance)
        collection = None
        if src_cfg is not None and vcat.catalog_exists(src_cfg.vector_db_dir):
            with vcat.open_catalog(src_cfg.vector_db_dir) as catalog:
                collection = next((c for c in catalog.list_collections() if c.name == r.source_collection), None)
        if reason is None and collection is None:
            reason = _t("インスタンス {0} にコレクション {1} がありません。", r.source_instance, r.source_collection)
        if reason:
            d["reason"] = reason
            return d
        d["collection"] = asdict(collection)
        model = _model_or_none(owner_cfg)
        problems = []
        if not src_cfg.vector_db_enabled:
            problems.append(_t("参照元の [vector_db] enabled が false です"))
        if collection.model and collection.model != model:
            problems.append(_t("埋め込みモデルが違います（参照元 {0}、このインスタンス {1}）", collection.model, model))
        if collection.model and collection.document_prefix is not None and collection.document_prefix != owner_cfg.vector_db_document_prefix:
            problems.append(
                _t(
                    "document_prefix が違います（参照元 {0}、このインスタンス {1}）",
                    repr(collection.document_prefix),
                    repr(owner_cfg.vector_db_document_prefix),
                )
            )
        if problems:
            d["status"], d["reason"] = "incompatible", _t("、").join(problems)
        else:
            d["status"] = "ok"
        return d

    def _referenced_by(name: str) -> dict[str, list[str]]:
        """name のコレクションを参照している他のインスタンスの参照（コレクション名 → ["インスタンス/参照名", ...]）。"""
        result: dict[str, list[str]] = {}
        for other in _app_instances():
            if other == name:
                continue
            cfg = _try_cfg(other)
            if cfg is None or not vcat.catalog_exists(cfg.vector_db_dir):
                continue
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                refs = catalog.list_references()
            for r in refs:
                if r.source_instance == name:
                    result.setdefault(r.source_collection, []).append(f"{other}/{r.name}")
        return result

    @router.get("/api/instances/{name}/knowledge")
    def get_knowledge(name: str, user: str = Depends(require_login)):
        cfg = _cfg(name)
        collections = []
        refs = []
        if vcat.catalog_exists(cfg.vector_db_dir):
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                collections = catalog.list_collections()
                refs = catalog.list_references()
        running = jobs.for_instance(name)
        referenced_by = _referenced_by(name)
        names = {c.name for c in collections}
        endpoints = cfg.vector_db_embedding_endpoints
        return {
            "enabled": cfg.vector_db_enabled,
            "backend": cfg.vector_db_backend,
            "dir": str(cfg.vector_db_dir),
            "qdrant_url": cfg.vector_db_qdrant_url,
            "qdrant_collection_prefix": cfg.vector_db_qdrant_collection_prefix,
            "qdrant_preset": cfg.vector_db_qdrant_preset,
            "distance": cfg.vector_db_distance,
            "embedding_model": endpoints[0].model if endpoints else None,
            "query_prefix": cfg.vector_db_query_prefix,
            "document_prefix": cfg.vector_db_document_prefix,
            "include_extensions": list(effective_extensions(cfg.vector_db_include_extensions, cfg.vector_db_text_extensions)),
            "image_input": cfg.vector_db_image_input,
            "image_input_extensions": list(cfg.vector_db_image_input_extensions),
            "embedded_images": cfg.vector_db_embedded_images,
            "max_file_mb": cfg.vector_db_max_file_mb,
            "collections": [_collection_dict(c, running.get(c.name), referenced_by.get(c.name)) for c in collections],
            # コピーを始めた直後で、まだ catalog にできていないコピー先のジョブ・定義の作成で失敗したコピーのジョブ。
            "copy_jobs": [j.to_dict() for c, j in running.items() if j.kind == "copy" and c not in names],
            "references": [_reference_dict(name, cfg, r) for r in refs],
            # 全インスタンスで共有する取り込みの枠の状態（画面のワーカーのアイコン）。
            "workers": slots.snapshot(),
        }

    @router.get("/api/instances/{name}/knowledge/jobs")
    def get_knowledge_jobs(name: str, user: str = Depends(require_login)):
        """name のジョブ（コレクション名 → IngestJob.to_dict()）と取り込みの枠の状態だけを返す（取り込み中の画面のポーリング用）。

        GET /api/instances/{name}/knowledge と違い、設定・カタログ（ファイル数の集計）・ほかのインスタンスの参照を読まない
        （大量の取り込みを同時に動かしている間も軽く返すため）。件数等は、ジョブが始まった・終わったときと、取り込み中は
        10 秒ごと（admin/static/knowledge.js の KNOWLEDGE_FULL_REFRESH_MS）に画面が一覧を取り直す。
        """
        _check_app_instance(name)
        return {"jobs": {c: j.to_dict() for c, j in jobs.for_instance(name).items()}, "workers": slots.snapshot()}

    @router.get("/api/knowledge/workers")
    def get_knowledge_workers(user: str = Depends(require_login)):
        """全インスタンスで共有する取り込みの枠の状態（IngestSlots.snapshot）。"""
        return slots.snapshot()

    @router.get("/api/knowledge/sources")
    def get_knowledge_sources(user: str = Depends(require_login)):
        """コピー先・参照元に選べるインスタンスと、そのコレクションの一覧（埋め込みの設定と合わせて返す）。"""
        result = []
        for n in _app_instances():
            cfg = _try_cfg(n)
            if cfg is None:
                continue
            collections = []
            if vcat.catalog_exists(cfg.vector_db_dir):
                with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                    collections = catalog.list_collections()
            meta = inst.read_instance(instances_root, n)
            keys = ("name", "description", "model", "dim", "document_prefix", "file_count", "chunk_count")
            result.append(
                {
                    "name": n,
                    "display_name": meta.display_name,
                    "enabled": cfg.vector_db_enabled,
                    "backend": cfg.vector_db_backend,
                    "distance": cfg.vector_db_distance,
                    "embedding_model": _model_or_none(cfg),
                    "document_prefix": cfg.vector_db_document_prefix,
                    "collections": [{k: v for k, v in asdict(c).items() if k in keys} for c in collections],
                }
            )
        return {"instances": result}

    @router.post("/api/instances/{name}/knowledge/collections")
    def create_collection(name: str, body: CreateCollectionBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        cfg = _cfg(name)
        folders = _body_folders(body.source_folders, body.source_folder)
        if folders is None:
            raise HTTPException(status_code=400, detail=_t("取り込み元フォルダを指定してください。"))
        folders = _check_folders(folders, body.recursive)
        try:
            times = vcat.normalize_schedule_times(body.schedule_times)
            weekdays = vcat.normalize_schedule_weekdays(body.schedule_weekdays)
            if body.schedule_enabled and not times:
                raise vcat.CatalogError(_t("自動取り込みを有効にするときは時刻を1つ以上指定してください。"))
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                catalog.create(body.name, folders, body.description, body.recursive, body.exclude_keywords, body.include_keywords)
                created = catalog.update(body.name, schedule_enabled=body.schedule_enabled, schedule_times=times, schedule_weekdays=weekdays)
        except vcat.CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(
            request,
            user,
            name,
            "knowledge_collection_create",
            collection=body.name,
            source_folders=list(created.source_folders),
            exclude_keywords=list(created.exclude_keywords),
            include_keywords=list(created.include_keywords),
            schedule_enabled=created.schedule_enabled,
            schedule_times=list(created.schedule_times),
            schedule_weekdays=list(created.schedule_weekdays),
        )
        return _collection_dict(created, None)

    @router.put("/api/instances/{name}/knowledge/collections/{collection}")
    def update_collection(
        name: str, collection: str, body: UpdateCollectionBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)
    ):
        cfg = _cfg(name)
        folders = _body_folders(body.source_folders, body.source_folder)
        with vcat.open_catalog(cfg.vector_db_dir) as catalog:
            try:
                current = catalog.get(collection)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if folders is not None:
                folders = _check_folders(folders, body.recursive if body.recursive is not None else current.recursive)
            try:
                updated = catalog.update(
                    collection,
                    source_folders=folders,
                    description=body.description,
                    recursive=body.recursive,
                    exclude_keywords=body.exclude_keywords,
                    include_keywords=body.include_keywords,
                    schedule_enabled=body.schedule_enabled,
                    schedule_times=body.schedule_times,
                    schedule_weekdays=body.schedule_weekdays,
                )
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "knowledge_collection_update", collection=collection, **body.model_dump(exclude_none=True))
        return _collection_dict(updated, jobs.get(name, collection))

    @router.delete("/api/instances/{name}/knowledge/collections/{collection}")
    def delete_collection(name: str, collection: str, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        """コレクションを削除する。

        同じインスタンスで取り込み・コピーが動いていなければその場で削除して {"ok": true} を返す。
        動いていれば削除のジョブを順番待ちにして {"ok": true, "queued": true, "job": ...} を返す
        （それらが終わってから削除する。共有のキーワード索引等への書き込みとぶつからないようにするため）。
        """
        cfg = _cfg(name)
        job = jobs.get(name, collection)
        if _active(job):
            detail = _t("削除の処理中です。") if job.kind == "delete" else _t("取り込み中のコレクションは削除できません。先に停止してください。")
            raise HTTPException(status_code=409, detail=detail)
        if jobs.is_copy_source(name, collection):
            raise HTTPException(status_code=409, detail=_t("コピー中のコレクションは削除できません。コピーが終わってから削除してください。"))
        with vcat.open_catalog(cfg.vector_db_dir) as catalog:
            try:
                catalog.get(collection)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        job = IngestJob(collection=collection, rebuild=False, actor=user, kind="delete")
        if not jobs.start(name, job):
            raise HTTPException(status_code=409, detail=_t("このコレクションは取り込み中です。"))
        _audit(request, user, name, "knowledge_collection_delete", collection=collection)
        store_transport = extra_ingest_kwargs.get("store_transport")
        if job.state == "queued":
            logger.info("Knowledge: delete %s/%s queued until the other jobs of the instance finish.", name, collection)
            threading.Thread(
                target=run_delete_job,
                args=(cfg, name, job, jobs),
                kwargs={"store_transport": store_transport},
                name=f"knowledge-delete-{name}-{collection}",
                daemon=True,
            ).start()
            return {"ok": True, "queued": True, "job": job.to_dict()}
        run_delete_job(cfg, name, job, jobs, store_transport=store_transport)
        if job.state == "error":
            raise HTTPException(status_code=502, detail=job.error)
        return {"ok": True}

    @router.post("/api/instances/{name}/knowledge/collections/{collection}/ingest")
    def start_ingest(
        name: str, collection: str, body: IngestBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)
    ):
        cfg = _cfg(name)
        if not cfg.vector_db_enabled:
            raise HTTPException(status_code=400, detail=_t("このインスタンスの [vector_db] enabled が false です。config.ini タブで有効にしてください。"))
        with vcat.open_catalog(cfg.vector_db_dir) as catalog:
            try:
                catalog.get(collection)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        if jobs.is_copy_source(name, collection):
            raise HTTPException(status_code=409, detail=_t("このコレクションはコピー中です。コピーが終わってから取り込んでください。"))
        job = IngestJob(collection=collection, rebuild=body.rebuild, actor=user)
        if not jobs.start(name, job):
            current = jobs.get(name, collection)
            if current is not None and current.kind == "delete":
                raise HTTPException(status_code=409, detail=_t("このコレクションは削除の処理中です。"))
            raise HTTPException(status_code=409, detail=_t("このコレクションは取り込み中です。"))
        _launch_ingest(name, cfg, job)
        _audit(request, user, name, "knowledge_ingest_start", collection=collection, rebuild=body.rebuild)
        return job.to_dict()

    @router.post("/api/instances/{name}/knowledge/collections/{collection}/copy")
    def copy_collection_to(
        name: str, collection: str, body: CopyBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)
    ):
        src_cfg = _cfg(name)
        dst_cfg = _cfg(body.dest_instance)
        dest_name = body.dest_name.strip()
        with vcat.open_catalog(src_cfg.vector_db_dir) as catalog:
            try:
                catalog.get(collection)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            vcat.validate_name(dest_name)
        except vcat.CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if vcat.catalog_exists(dst_cfg.vector_db_dir):
            with vcat.open_catalog(dst_cfg.vector_db_dir) as catalog:
                taken = {c.name for c in catalog.list_collections()} | {r.name for r in catalog.list_references()}
            if dest_name in taken:
                raise HTTPException(
                    status_code=400,
                    detail=_t("インスタンス {0} には {1} という名前のコレクション（または参照）が既にあります。別の名前を指定してください。", body.dest_instance, dest_name),
                )
        if body.with_vectors:
            reasons = check_compatible(src_cfg, collection, dst_cfg)
            if reasons:
                raise HTTPException(
                    status_code=400,
                    detail=_t("コピー先ではこのベクターを使えないため、ベクターごとはコピーできません（{0}）。定義だけをコピーしてコピー先で取り込んでください。", "; ".join(reasons)),
                )
        job = IngestJob(collection=dest_name, rebuild=False, actor=user, kind="copy", source=f"{name}/{collection}")
        if not jobs.start_copy(body.dest_instance, job, name, collection):
            raise HTTPException(status_code=409, detail=_t("コピー元かコピー先のコレクションが取り込み中です。終わってからコピーしてください。"))
        threading.Thread(
            target=run_copy_job,
            args=(src_cfg, name, collection, dst_cfg, job, jobs),
            kwargs={
                "with_vectors": body.with_vectors,
                "include_schedule": body.include_schedule,
                "store_transport": extra_ingest_kwargs.get("store_transport"),
                "dst_instance": body.dest_instance,
            },
            name=f"knowledge-copy-{name}-{collection}",
            daemon=True,
        ).start()
        _audit(
            request,
            user,
            name,
            "knowledge_collection_copy",
            collection=collection,
            dest_instance=body.dest_instance,
            dest_name=dest_name,
            with_vectors=body.with_vectors,
            include_schedule=body.include_schedule,
        )
        return job.to_dict()

    @router.post("/api/instances/{name}/knowledge/references")
    def add_reference(name: str, body: ReferenceBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        cfg = _cfg(name)
        reason = _source_check(name, body.source_instance)
        if reason:
            raise HTTPException(status_code=400, detail=reason)
        src_cfg = _cfg(body.source_instance)
        exists = False
        if vcat.catalog_exists(src_cfg.vector_db_dir):
            with vcat.open_catalog(src_cfg.vector_db_dir) as catalog:
                exists = any(c.name == body.source_collection for c in catalog.list_collections())
        if not exists:
            raise HTTPException(status_code=400, detail=_t("インスタンス {0} にコレクション {1} がありません。", body.source_instance, body.source_collection))
        try:
            with vcat.open_catalog(cfg.vector_db_dir) as catalog:
                ref = catalog.add_reference(body.name.strip(), body.source_instance, body.source_collection)
        except vcat.CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(
            request, user, name, "knowledge_reference_add",
            reference=ref.name, source_instance=ref.source_instance, source_collection=ref.source_collection,
        )
        return _reference_dict(name, cfg, ref)

    @router.delete("/api/instances/{name}/knowledge/references/{reference}")
    def delete_reference(name: str, reference: str, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        cfg = _cfg(name)
        with vcat.open_catalog(cfg.vector_db_dir) as catalog:
            try:
                catalog.get_reference(reference)
            except vcat.CatalogError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            catalog.delete_reference(reference)
        _audit(request, user, name, "knowledge_reference_delete", reference=reference)
        return {"ok": True}

    @router.get("/api/instances/{name}/knowledge/collections/{collection}/job")
    def get_job(name: str, collection: str, user: str = Depends(require_login)):
        _cfg(name)
        job = jobs.get(name, collection)
        return {"job": job.to_dict() if job else None}

    @router.post("/api/instances/{name}/knowledge/collections/{collection}/stop")
    def stop_ingest(name: str, collection: str, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        _cfg(name)
        job = jobs.get(name, collection)
        if not _active(job):
            raise HTTPException(status_code=409, detail=_t("取り込み中ではありません。"))
        if job.kind == "delete" and job.state == "running":
            raise HTTPException(status_code=409, detail=_t("削除は始まっているため止められません。"))
        job.stop_requested = True
        # 順番待ちのジョブはすぐ取りやめる（_JobRegistry.wait_turn）。
        jobs.notify()
        logger.info("Knowledge: stop requested for %s %s/%s (%s) by %s.", job.kind, name, collection, job.state, user)
        _audit(request, user, name, "knowledge_ingest_stop", collection=collection)
        return job.to_dict()

    return router


def _check_folder(value: str) -> Path:
    folder = Path(value.strip())
    if not value.strip() or not folder.is_absolute():
        raise HTTPException(status_code=400, detail=_t("取り込み元フォルダは絶対パスで指定してください。"))
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail=_t("取り込み元フォルダがありません（管理ツールのPCから見えるパスを指定してください）: {0}", folder))
    return folder


def _body_folders(folders: list[str] | None, folder: str | None) -> list[str] | None:
    """リクエストの source_folders（複数）か source_folder（1つ・旧版の API）。どちらも無ければ None。"""
    if folders is not None:
        return folders
    return [folder] if folder is not None else None


def _check_folders(values: list[str], recursive: bool) -> list[str]:
    """取り込み元フォルダを検証し、重複を除いた絶対パスのリストにする。

    サブフォルダも含めるときは、別の取り込み元フォルダの中にあるフォルダを指定できない（同じファイルを2回たどるため）。
    """
    try:
        values = list(vcat.normalize_source_folders(values))
    except vcat.CatalogError as exc:
        raise HTTPException(status_code=400, detail=_t("取り込み元フォルダを指定してください。")) from exc
    folders = [_check_folder(v) for v in values]
    if recursive:
        keys = [os.path.normcase(os.path.normpath(f)) for f in folders]
        for i, inner in enumerate(keys):
            for j, outer in enumerate(keys):
                if i != j and inner.startswith(outer.rstrip("\\/") + os.sep):
                    raise HTTPException(
                        status_code=400,
                        detail=_t("取り込み元フォルダ {0} は {1} の中にあります（サブフォルダも含めるときは、外側のフォルダだけを指定してください）。", folders[i], folders[j]),
                    )
    return [str(f) for f in folders]
