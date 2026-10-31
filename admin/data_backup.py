"""設定ダッシュボードの「データバックアップ」（[admin] instances_dir と全インスタンスの common_data_dir の zip バックアップ）。

データ本体（SQLite を含む）はローカルディスクに置く（src/config.py の _validate_local_data_paths）代わりに、
ここで日時つきの zip にまとめて保存先（ファイル共有の UNC パスも可）へ定期的に退避する。

- 設定: config.ini の [data_backup] が既定値で、ダッシュボードで保存した値は <instances_dir>/data_backup.json に持ち、
  そちらを優先する（全インスタンス共通。インスタンス別の config_overrides.json では上書きできない）。
- 中身: instances/…（instances_dir）・data/<インスタンス名>/…（common_data_dir）と manifest.json。common_data_dir の
  外に置かれた checkpoint_db・[thread_store] db・[vector_db] dir は data/<インスタンス名>/_external/<キー名>/ に入れる。
- SQLite は使用中でも壊れないよう、SQLite のバックアップ機能で一時ファイルへ写してから入れる（-wal・-shm・-journal は入れない）。
- 作成中は .partial に書き、完了したら改名する。keep 世代を超えた古い zip は消す。
- 時間指定の自動バックアップは BackupScheduler（管理ツールのプロセス内のスレッド。admin/server.py の lifespan で起動）。
"""

from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass, field
from datetime import datetime
import fnmatch
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from typing import Callable
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.config import Config
from src.version import __version__
from src.vector_db import catalog as vcat

from . import audit
from . import instances as inst
from .knowledge import due_minutes, is_due_at, next_run_at
from admin.i18n import t as _t

logger = logging.getLogger(__name__)

SECTION = "data_backup"
SETTINGS_FILE = "data_backup.json"
ZIP_PREFIX = "locohane_backup_"
PARTIAL_SUFFIX = ".partial"
SQLITE_HEADER = b"SQLite format 3\x00"
# SQLite の付属ファイル（本体と一緒にあるときは入れない。本体はバックアップ機能で整合した状態に写す）。
SQLITE_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
ADMIN_LOG_DIR_NAME = "_admin_logs"
APP_STDOUT_LOG = "app_stdout.log"


class BackupError(Exception):
    """設定の誤り・保存先に書けない等でバックアップを始められない。"""


class BackupBusy(BackupError):
    """別のバックアップが実行中。"""


class BackupStopped(Exception):
    """停止を求められた。"""


@dataclass(frozen=True)
class BackupSettings:
    enabled: bool = False
    dest_dir: str = ""
    times: tuple[str, ...] = ("02:00",)
    weekdays: tuple[int, ...] = ()
    keep: int = 7
    exclude_workdir: bool = True
    exclude_logs: bool = True
    exclude: tuple[str, ...] = ()


FIELDS = tuple(BackupSettings.__dataclass_fields__)


def _split(value) -> list[str]:
    """カンマ区切りの文字列か、文字列のリストを、空を除いたリストにする。"""
    items = value if isinstance(value, (list, tuple)) else str(value or "").replace("、", ",").split(",")
    return [str(v).strip() for v in items if str(v).strip()]


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def parse_settings(raw: dict) -> BackupSettings:
    """config.ini の文字列・ダッシュボードの JSON の値を検証して BackupSettings にする（誤りは BackupError）。"""
    defaults = BackupSettings()
    try:
        times = vcat.normalize_schedule_times(_split(raw.get("times", ",".join(defaults.times))))
        weekdays = vcat.normalize_schedule_weekdays([int(d) for d in _split(raw.get("weekdays", ""))])
    except (vcat.CatalogError, ValueError) as exc:
        raise BackupError(_t("自動バックアップの時刻・曜日が正しくありません: {0}", exc)) from exc
    try:
        keep = int(str(raw.get("keep", defaults.keep)).strip())
    except ValueError as exc:
        raise BackupError(_t("残す世代数は 1 以上の整数にしてください。")) from exc
    if keep < 1:
        raise BackupError(_t("残す世代数は 1 以上の整数にしてください。"))
    dest = str(raw.get("dest_dir", "") or "").strip()
    if dest and not Path(dest).is_absolute():
        raise BackupError(_t("保存先は絶対パスで指定してください: {0}", dest))
    enabled = _as_bool(raw.get("enabled", defaults.enabled))
    if enabled and not times:
        raise BackupError(_t("自動バックアップを有効にするときは時刻を1つ以上指定してください。"))
    return BackupSettings(
        enabled=enabled,
        dest_dir=dest,
        times=times,
        weekdays=weekdays,
        keep=keep,
        exclude_workdir=_as_bool(raw.get("exclude_workdir", defaults.exclude_workdir)),
        exclude_logs=_as_bool(raw.get("exclude_logs", defaults.exclude_logs)),
        exclude=tuple(_split(raw.get("exclude", ""))),
    )


def read_ini_defaults(config_ini_path: Path) -> dict[str, str]:
    """config.ini の [data_backup] の生の値（無ければ空）。"""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_ini_path, encoding="utf-8")
    if not parser.has_section(SECTION):
        return {}
    return {k: v for k, v in parser[SECTION].items() if k in FIELDS}


def settings_path(instances_root: Path) -> Path:
    return instances_root / SETTINGS_FILE


def read_saved(instances_root: Path) -> dict:
    """ダッシュボードで保存した値（data_backup.json。無ければ空）。"""
    path = settings_path(instances_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(_t("{0} を読めません（画面で保存し直すか、ファイルを削除してください）: {1}", path, exc)) from exc
    if not isinstance(data, dict):
        raise BackupError(_t("{0}: トップレベルはオブジェクトである必要があります。", path))
    return {k: v for k, v in data.items() if k in FIELDS}


def load_settings(config_ini_path: Path, instances_root: Path) -> tuple[BackupSettings, dict[str, str]]:
    """実効の設定と、項目ごとの出どころ（"dashboard" / "config.ini"）。"""
    saved = read_saved(instances_root)
    settings = parse_settings({**read_ini_defaults(config_ini_path), **saved})
    return settings, {f: "dashboard" if f in saved else "config.ini" for f in FIELDS}


def save_settings(config_ini_path: Path, instances_root: Path, values: dict) -> BackupSettings:
    """values を検証して data_backup.json へ原子的に保存する（検証できなければ BackupError で保存しない）。"""
    values = {k: v for k, v in values.items() if k in FIELDS}
    settings = parse_settings({**read_ini_defaults(config_ini_path), **values})
    data = asdict(settings)
    data["times"], data["weekdays"], data["exclude"] = list(settings.times), list(settings.weekdays), list(settings.exclude)
    path = settings_path(instances_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".data_backup_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return settings


# --- バックアップ元 -----------------------------------------------------------------


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def is_inside(path: Path, root: Path) -> bool:
    """path が root と同じか、root の中にあるか。"""
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


@dataclass
class Source:
    """zip の arc_root（"instances"・"data/<名前>" 等）へ入れるファイル・フォルダ。"""

    arc_root: str
    path: Path
    exclude_paths: list[Path] = field(default_factory=list)
    exclude_names: tuple[str, ...] = ()


def collect_sources(
    instances_root: Path, loader: Callable[[str], Config], settings: BackupSettings
) -> tuple[list[Source], list[str]]:
    """バックアップ元（instances_dir と、全インスタンスの common_data_dir 等）と、設定を読めなかったインスタンスの警告。"""
    log_names = (APP_STDOUT_LOG,) if settings.exclude_logs else ()
    root_excludes = [instances_root / ADMIN_LOG_DIR_NAME] if settings.exclude_logs else []
    sources = [Source("instances", instances_root, root_excludes, log_names)]
    warnings: list[str] = []
    for name in inst.list_instance_names(instances_root):
        try:
            cfg = loader(name)
        except Exception as exc:  # noqa: BLE001 - 設定の読めないインスタンスは飛ばして他を続ける
            warnings.append(_t("インスタンス {0} の設定を読めないため、そのデータはバックアップしていません: {1}", name, exc))
            continue
        excludes: list[Path] = []
        if settings.exclude_workdir:
            excludes.append(cfg.default_workdir)
        if settings.exclude_logs:
            excludes += [cfg.log_dir, cfg.chat_log_dir]
        covered = [s.path for s in sources]
        if not any(is_inside(cfg.common_data_dir, p) for p in covered):
            sources.append(Source(f"data/{name}", cfg.common_data_dir, excludes))
            covered.append(cfg.common_data_dir)
        # common_data_dir の外に置かれた SQLite の置き場も入れる。
        for key, path in (("checkpoint_db", cfg.checkpoint_db), ("thread_store_db", cfg.thread_store_db), ("vector_db_dir", cfg.vector_db_dir)):
            if path.exists() and not any(is_inside(path, p) for p in covered):
                sources.append(Source(f"data/{name}/_external/{key}", path, excludes))
                covered.append(path)
    return sources, warnings


def check_dest(settings: BackupSettings, sources: list[Source]) -> Path:
    """保存先を検証して返す（未設定・バックアップ元の中なら BackupError）。"""
    if not settings.dest_dir:
        raise BackupError(_t("保存先が設定されていません。"))
    dest = Path(settings.dest_dir)
    for s in sources:
        if is_inside(dest, s.path):
            raise BackupError(_t("保存先 {0} はバックアップ元 {1} の中にあります。別の場所を指定してください。", dest, s.path))
    return dest


def _excluded(source: Source, path: Path, rel: str, patterns: tuple[str, ...]) -> bool:
    if any(is_inside(path, e) for e in source.exclude_paths):
        return True
    if path.name in source.exclude_names:
        return True
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(path.name, p) for p in patterns)


def iter_files(sources: list[Source], patterns: tuple[str, ...]) -> list[tuple[Path, str]]:
    """(ファイル, zip の中の名前) の一覧。除外したもの・SQLite の付属ファイルは入れない。"""
    result: list[tuple[Path, str]] = []
    for source in sources:
        if source.path.is_file():
            result.append((source.path, f"{source.arc_root}/{source.path.name}"))
            continue
        if not source.path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(source.path):
            base = Path(dirpath)
            rel_dir = base.relative_to(source.path).as_posix()
            kept = []
            for d in sorted(dirnames):
                rel = d if rel_dir == "." else f"{rel_dir}/{d}"
                if not _excluded(source, base / d, rel + "/", patterns):
                    kept.append(d)
            dirnames[:] = kept
            for f in sorted(filenames):
                path = base / f
                rel = f if rel_dir == "." else f"{rel_dir}/{f}"
                if _excluded(source, path, rel, patterns):
                    continue
                if f.endswith(SQLITE_SIDE_SUFFIXES) and (base / f.rsplit("-", 1)[0]).exists():
                    continue
                result.append((path, f"{source.arc_root}/{rel}"))
    return result


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_HEADER
    except OSError:
        return False


def _add_sqlite(zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
    """使用中の SQLite も整合した状態で入れる（SQLite のバックアップ機能で一時ファイルへ写す）。

    一時ファイルは元の SQLite と同じフォルダに作る（大きいベクター DB をシステムの一時フォルダ（C ドライブ）へ
    写して圧迫しないため。データはローカルディスクにある。src/config.py の _validate_local_data_paths）。
    バックアップの対象のファイル一覧は先に作っているため、この一時ファイルは zip に入らない。
    """
    try:
        fd, tmp = tempfile.mkstemp(prefix=".locohane_backup_", suffix=".sqlite.tmp", dir=str(path.parent))
    except OSError:
        fd, tmp = tempfile.mkstemp(prefix="locohane_backup_", suffix=".sqlite")
    os.close(fd)
    try:
        src = sqlite3.connect(str(path), timeout=30)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        zf.write(tmp, arcname)
    finally:
        Path(tmp).unlink(missing_ok=True)


def backup_files(dest: Path) -> list[Path]:
    """dest の完成したバックアップ（古い順）。"""
    if not dest.is_dir():
        return []
    return sorted(p for p in dest.glob(f"{ZIP_PREFIX}*.zip") if p.is_file())


def prune(dest: Path, keep: int) -> list[str]:
    """keep 世代を超えた古い zip を消し、消した名前を返す。"""
    removed = []
    files = backup_files(dest)
    for path in files[: max(0, len(files) - keep)]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as exc:
            logger.warning("Data backup: cannot delete an old backup %s: %s", path, exc)
    return removed


def _remove_stale_partials(dest: Path) -> None:
    """管理ツールの強制終了等で残った作りかけの zip（.partial）を消す（世代の整理の対象外で、残り続けるため）。

    バックアップは BackupManager が1度に1つだけ動かすため、始める時点の .partial はどれも作りかけの残り。
    """
    for path in dest.glob(f"{ZIP_PREFIX}*.zip{PARTIAL_SUFFIX}"):
        try:
            path.unlink()
            logger.info("Data backup: removed an unfinished backup left over: %s", path)
        except OSError as exc:
            logger.warning("Data backup: cannot delete an unfinished backup %s: %s", path, exc)


def run_backup(
    settings: BackupSettings,
    sources: list[Source],
    *,
    warnings: list[str] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    now: datetime | None = None,
) -> dict:
    """sources を dest_dir の zip にまとめ、keep 世代を超えた古い zip を消す。結果（ファイル名・件数・失敗一覧）を返す。

    読めないファイルは失敗一覧に載せて続ける。停止を求められたら BackupStopped（作りかけの zip は消す）。
    """
    dest = check_dest(settings, sources)
    started = time.monotonic()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(_t("保存先に書き込めません: {0}: {1}", dest, exc)) from exc
    _remove_stale_partials(dest)
    files = iter_files(sources, settings.exclude)
    stamp =(now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    final = dest / f"{ZIP_PREFIX}{stamp}.zip"
    partial = final.with_name(final.name + PARTIAL_SUFFIX)
    failed: list[dict] = []
    total_bytes = 0
    logger.info("Data backup: writing %d files to %s", len(files), final)
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6) as zf:
            for i, (path, arcname) in enumerate(files):
                if should_stop is not None and should_stop():
                    raise BackupStopped()
                if progress is not None:
                    progress(i, len(files), str(path))
                try:
                    if _is_sqlite(path):
                        _add_sqlite(zf, path, arcname)
                    else:
                        zf.write(path, arcname)
                    total_bytes += path.stat().st_size
                except (OSError, sqlite3.Error) as exc:
                    logger.warning("Data backup: cannot back up %s: %s", path, exc)
                    failed.append({"path": str(path), "reason": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "version": __version__,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "sources": [{"arc_root": s.arc_root, "path": str(s.path)} for s in sources],
                "files": len(files) - len(failed),
                "failed": failed,
                "warnings": warnings or [],
                "settings": {"exclude_workdir": settings.exclude_workdir, "exclude_logs": settings.exclude_logs, "exclude": list(settings.exclude)},
            }
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        partial.replace(final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    removed = prune(dest, settings.keep)
    if progress is not None:
        progress(len(files), len(files), "")
    return {
        "file": final.name,
        "dest_dir": str(dest),
        "size": final.stat().st_size,
        "source_bytes": total_bytes,
        "files": len(files) - len(failed),
        "failed": failed,
        "warnings": warnings or [],
        "removed": removed,
        "seconds": round(time.monotonic() - started, 1),
    }


# --- 実行の管理・スケジューラー ---------------------------------------------------------


@dataclass
class BackupJob:
    actor: str
    trigger: str = "manual"  # manual / schedule
    state: str = "running"  # running / done / error / stopped
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None
    done: int = 0
    total: int = 0
    current: str = ""
    result: dict | None = None
    error: str | None = None
    stop_requested: bool = False


class BackupManager:
    """バックアップを1度に1つだけ動かし、最新のジョブを持つ（画面のポーリング用）。"""

    def __init__(self, *, config_ini_path: Path, instances_root: Path, loader: Callable[[str], Config], audit_log_path: Path):
        self.config_ini_path, self.instances_root = config_ini_path, instances_root
        self.loader, self.audit_log_path = loader, audit_log_path
        self._lock = threading.Lock()
        self.job: BackupJob | None = None

    def settings(self) -> tuple[BackupSettings, dict[str, str]]:
        return load_settings(self.config_ini_path, self.instances_root)

    def running(self) -> bool:
        with self._lock:
            return self.job is not None and self.job.state == "running"

    def start(self, actor: str, trigger: str = "manual", *, wait: bool = False) -> BackupJob:
        """バックアップを始める（設定の誤り・保存先が無い等は BackupError、別のバックアップが実行中なら BackupBusy）。

        wait=True ならこのスレッドで最後まで行う（テスト用）。
        """
        settings, _sources = self.settings()
        sources, warnings = collect_sources(self.instances_root, self.loader, settings)
        check_dest(settings, sources)
        with self._lock:
            if self.job is not None and self.job.state == "running":
                raise BackupBusy(_t("バックアップは実行中です。"))
            job = self.job = BackupJob(actor=actor, trigger=trigger)
        audit.append(self.audit_log_path, {"instance": "-", "actor": actor, "remote_addr": "-", "action": "data_backup_start", "trigger": trigger})
        logger.info("Data backup: started (trigger=%s, actor=%s, dest=%s).", trigger, actor, settings.dest_dir)
        if wait:
            self._run(job, settings, sources, warnings)
        else:
            threading.Thread(target=self._run, args=(job, settings, sources, warnings), name="data-backup", daemon=True).start()
        return job

    def _run(self, job: BackupJob, settings: BackupSettings, sources: list[Source], warnings: list[str]) -> None:
        def _progress(done: int, total: int, path: str) -> None:
            job.done, job.total, job.current = done, total, path

        try:
            job.result = run_backup(settings, sources, warnings=warnings, progress=_progress, should_stop=lambda: job.stop_requested)
            job.state = "done"
            logger.info(
                "Data backup: finished %s (%d files, %d failed, %.1fs).",
                job.result["file"], job.result["files"], len(job.result["failed"]), job.result["seconds"],
            )
        except BackupStopped:
            job.state = "stopped"
            logger.info("Data backup: stopped by request.")
        except BackupError as exc:
            job.state, job.error = "error", str(exc)
            logger.warning("Data backup: %s", exc)
        except Exception as exc:  # noqa: BLE001 - 想定外の例外も画面に出して、running のまま残らないようにする
            job.state, job.error = "error", f"{type(exc).__name__}: {exc}"
            logger.exception("Data backup: unexpected error.")
        finally:
            job.current = ""
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            audit.append(
                self.audit_log_path,
                {"instance": "-", "actor": job.actor, "remote_addr": "-", "action": "data_backup_end", "state": job.state,
                 "file": (job.result or {}).get("file"), "error": job.error},
            )

    def stop(self) -> BackupJob | None:
        with self._lock:
            if self.job is None or self.job.state != "running":
                return None
            self.job.stop_requested = True
            return self.job


class BackupScheduler:
    """設定の時刻・曜日に自動バックアップを始める（管理ツールのプロセス内のスレッド。IngestScheduler と同じ方式）。"""

    MAX_CATCH_UP_MINUTES = 10
    POLL_SECONDS = 15

    def __init__(self, manager: BackupManager):
        self._manager = manager
        self._last_minute: datetime | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._last_minute = datetime.now().replace(second=0, microsecond=0)
        self._thread = threading.Thread(target=self._run, name="data-backup-scheduler", daemon=True)
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
                logger.exception("Data backup: failed to check the schedule.")

    def tick(self, now: datetime) -> bool:
        """前回から now までに予定の時刻が来ていればバックアップを始める（始めたら True）。"""
        last = self._last_minute
        self._last_minute = now.replace(second=0, microsecond=0)
        minutes = due_minutes(last, now, self.MAX_CATCH_UP_MINUTES)
        if not minutes:
            return False
        try:
            settings, _ = self._manager.settings()
        except BackupError as exc:
            logger.warning("Data backup: the settings are invalid, skipping the schedule: %s", exc)
            return False
        if not settings.enabled or not any(is_due_at(settings.times, settings.weekdays, m) for m in minutes):
            return False
        if self._manager.running():
            logger.info("Data backup: a backup is already running, skipping this run.")
            return False
        try:
            self._manager.start("scheduler", "schedule")
        except BackupError as exc:
            logger.warning("Data backup: cannot start the scheduled backup: %s", exc)
            return False
        return True


# --- API ------------------------------------------------------------------------------


class SettingsBody(BaseModel):
    enabled: bool = False
    dest_dir: str = ""
    times: list[str] = []
    weekdays: list[int] = []
    keep: int = 7
    exclude_workdir: bool = True
    exclude_logs: bool = True
    exclude: list[str] = []


def build_router(
    *,
    require_login: Callable,
    require_csrf: Callable,
    instances_root: Path,
    config_ini_path: Path,
    audit_log_path: Path,
    client_ip: Callable[[Request], str],
    config_loader: Callable[[str], Config],
) -> APIRouter:
    """データバックアップの API の router を作る。

    返す router の backup_scheduler 属性が自動バックアップの BackupScheduler（start()・stop() は admin/server.py の lifespan で行う）、
    backup_manager 属性が BackupManager。
    """
    router = APIRouter()
    manager = BackupManager(
        config_ini_path=config_ini_path, instances_root=instances_root, loader=config_loader, audit_log_path=audit_log_path
    )
    router.backup_manager = manager
    router.backup_scheduler = BackupScheduler(manager)

    def _audit(request: Request, user: str, action: str, **extra) -> None:
        audit.append(audit_log_path, {"instance": "-", "actor": user, "remote_addr": client_ip(request), "action": action, **extra})

    def _settings_dict(settings: BackupSettings) -> dict:
        d = asdict(settings)
        d["times"], d["weekdays"], d["exclude"] = list(settings.times), list(settings.weekdays), list(settings.exclude)
        return d

    @router.get("/api/data-backup")
    def get_data_backup(user: str = Depends(require_login)):
        """実効の設定・項目ごとの出どころ・実行状態・次回予定・保存先の zip の一覧。"""
        settings_error = None
        try:
            settings, sources = manager.settings()
        except (BackupError, ValueError) as exc:
            settings, sources, settings_error = BackupSettings(), dict.fromkeys(FIELDS, "config.ini"), str(exc)
        next_run = next_run_at(settings.times, settings.weekdays, datetime.now()) if settings.enabled else None
        backups, list_error = [], None
        if settings.dest_dir:
            try:
                backups = [
                    {"name": p.name, "size": p.stat().st_size, "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
                    for p in reversed(backup_files(Path(settings.dest_dir)))
                ]
            except OSError as exc:
                list_error = _t("保存先を読めません: {0}", exc)
        return {
            "settings": _settings_dict(settings),
            "sources": sources,
            "settings_error": settings_error,
            "ini_defaults": read_ini_defaults(config_ini_path),
            "next_run_at": next_run.isoformat(timespec="seconds") if next_run else None,
            "job": asdict(manager.job) if manager.job else None,
            "backups": backups,
            "list_error": list_error,
            "instances_dir": str(instances_root),
        }

    @router.put("/api/data-backup/settings")
    def put_settings(body: SettingsBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        try:
            values = body.model_dump()
            # 保存先がバックアップ元の中にある等の誤りは、保存する前に確かめる（保存してから断ると、
            # 画面はエラーなのに誤った設定が残り、自動バックアップが毎回失敗するため）。
            checked = parse_settings({**read_ini_defaults(config_ini_path), **values})
            if checked.dest_dir:
                sources, _warnings = collect_sources(instances_root, config_loader, checked)
                check_dest(checked, sources)
            settings = save_settings(config_ini_path, instances_root, values)
        except BackupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, "data_backup_settings_save", **_settings_dict(settings))
        logger.info("Data backup: settings saved by %s: %s", user, _settings_dict(settings))
        return _settings_dict(settings)

    @router.delete("/api/data-backup/settings")
    def reset_settings(request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        """ダッシュボードで保存した値を消し、config.ini の既定に戻す。"""
        settings_path(instances_root).unlink(missing_ok=True)
        _audit(request, user, "data_backup_settings_reset")
        logger.info("Data backup: settings reset to config.ini by %s.", user)
        return {"ok": True}

    @router.post("/api/data-backup/run")
    def run_now(request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        try:
            job = manager.start(user, "manual")
        except BackupBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BackupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return asdict(job)

    @router.post("/api/data-backup/stop")
    def stop(request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        job = manager.stop()
        if job is None:
            raise HTTPException(status_code=409, detail=_t("バックアップは実行中ではありません。"))
        _audit(request, user, "data_backup_stop")
        return asdict(job)

    return router
