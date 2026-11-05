"""ナレッジの取り込み・コピーを、設定ダッシュボード（管理ツール）とは別のプロセスで動かす（取り込みワーカー）。

取り込みは本文の抽出・埋め込みの応答（JSON）の解析・書き込みの準備等で CPU を使うスレッドを多く動かす。同じプロセスに
いると、Python の GIL を取り合ってダッシュボードの応答（Web サーバーのスレッド）が待たされる（CPU を使うスレッドが
8本あると、小さな要求の処理でも1〜3秒、20本なら3〜10秒遅れる）。そこで取り込み・コピーを子プロセス
（python -m admin.ingest_worker）で動かし、管理ツールのプロセスには軽い処理（ジョブの記録・順番待ち・
時間指定の自動取り込み・削除）だけを残す。子プロセスが異常終了しても（壊れた PDF で PDFium が落ちた等）、
管理ツールは落ちず、動いていたジョブがエラーになる（次の取り込みで子プロセスを起動し直す）。

管理ツール側は IngestWorkerClient（admin/knowledge.py の build_router(ingest_in_worker_process=True)。
admin/server.py が使う）。子プロセスとは multiprocessing.connection（Windows では名前付きパイプ。接続時に
起動ごとの認証キーを確かめる）で次のメッセージをやり取りする。

管理ツール → 子プロセス:
    ("ingest", job_id, cfg, collection, rebuild, instance)
    ("copy", job_id, src_cfg, src_name, dst_cfg, dst_name, with_vectors, include_schedule)
    ("stop", job_id)                       ジョブに停止を求める
    ("resize", workers, processes, requests) 全体の並列数を変える（IngestSlots.resize）
    ("shutdown",)                          動いているジョブに停止を求めて終わる
子プロセス → 管理ツール:
    ("progress", job_id, done, total, path)
    ("bytes", job_id, bytes_done)
    ("done", job_id, result)               result: IngestResult.to_dict() / copy_collection() の戻り値
    ("error", job_id, expected, message)   expected: 想定内の例外（IngestError 等。message は str(exc)）か
    ("slots", snapshot)                    IngestSlots.snapshot()（変わったときだけ、SLOTS_INTERVAL_SECONDS ごと）
    ("log", record)                        子プロセスのログ（管理ツールのログに流す）
"""

from __future__ import annotations

import itertools
import logging
import logging.handlers
import os
import secrets
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ADDRESS_ENV = "LOCOHANE_INGEST_WORKER_ADDRESS"
AUTHKEY_ENV = "LOCOHANE_INGEST_WORKER_AUTHKEY"
# 枠の状態を送る間隔（秒）。画面は5秒ごとに読む。
SLOTS_INTERVAL_SECONDS = 1.0
# 進捗を送る最短の間隔（秒。ファイルごとに送ると、小さいファイルの多い取り込みでメッセージが多くなりすぎるため）。
PROGRESS_INTERVAL_SECONDS = 0.5
# 子プロセスが接続してくるまで待つ時間（秒）。
CONNECT_TIMEOUT_SECONDS = 120.0
# 停止を求めているかを見る間隔（秒）。
STOP_POLL_SECONDS = 0.5
# 終了を求めてから子プロセスを強制終了するまでの時間（秒）。
SHUTDOWN_GRACE_SECONDS = 5.0


class WorkerError(Exception):
    """子プロセスで起きたエラー（想定内の例外なら str(exc)、想定外なら "型名: メッセージ"）。"""


class _Result:
    """子プロセスから受け取った IngestResult（to_dict() だけを持つ）。"""

    def __init__(self, data: dict):
        self._data = data

    def to_dict(self) -> dict:
        return self._data


class _Call:
    """管理ツール側で結果を待っている1件のジョブ。"""

    def __init__(self, progress: Callable | None, progress_bytes: Callable | None):
        self.progress = progress
        self.progress_bytes = progress_bytes
        self.finished = threading.Event()
        self.result = None
        self.error: tuple[bool, str] | None = None


def _idle_snapshot(workers: int, processes: int, requests: int) -> dict:
    from src.vector_db.ingest import IngestSlots

    return IngestSlots(workers, processes, requests).snapshot()


class WorkerSlots:
    """子プロセスの IngestSlots の写し（admin/knowledge.py・admin/server.py が IngestSlots の代わりに読む）。

    snapshot() は子プロセスから最後に届いた状態（子プロセスが動いていなければ空きの枠）、resize() は子プロセスへ送る。
    """

    def __init__(self, client: "IngestWorkerClient", workers: int, processes: int, requests: int):
        self._client = client
        self._lock = threading.Lock()
        self.workers, self.processes, self.requests = workers, processes, requests
        self._snapshot: dict | None = None

    def resize(self, workers: int, processes: int, requests: int = 0) -> None:
        with self._lock:
            self.workers, self.processes, self.requests = workers, processes, requests
            self._snapshot = None
        self._client._send_if_running(("resize", workers, processes, requests))

    def limits(self) -> tuple[int, int, int]:
        with self._lock:
            return self.workers, self.processes, self.requests

    def _update(self, snapshot: dict | None) -> None:
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self) -> dict:
        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            limits = (self.workers, self.processes, self.requests)
        return _idle_snapshot(*limits)


class IngestWorkerClient:
    """取り込みワーカーの子プロセスを起動し（最初のジョブのとき）、ジョブを渡して結果を待つ。

    ingest_collection()・copy_collection() は src.vector_db の同名の関数と同じ引数（テスト用のトランスポートを除く）で、
    呼んだスレッドで子プロセスの完了を待つ（admin/knowledge.py の run_ingest_job・run_copy_job のスレッド）。
    """

    def __init__(self, workers: int, processes: int = 0, requests: int = 0, *, python: str | None = None):
        self.slots = WorkerSlots(self, workers, processes, requests)
        self._python = python or sys.executable
        self._lock = threading.Lock()  # 子プロセスの起動・終了
        self._send_lock = threading.Lock()
        self._conn: Connection | None = None
        self._proc: subprocess.Popen | None = None
        self._calls: dict[int, _Call] = {}
        self._calls_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._closed = False

    # -- 子プロセス -------------------------------------------------------

    def _ensure_started(self) -> Connection:
        with self._lock:
            if self._closed:
                raise WorkerError("管理ツールを終了しています。")
            if self._conn is not None and self._proc is not None and self._proc.poll() is None:
                return self._conn
            self._start_locked()
            assert self._conn is not None
            return self._conn

    def _start_locked(self) -> None:
        authkey = secrets.token_bytes(32)
        listener = Listener(authkey=authkey)
        env = {**os.environ, ADDRESS_ENV: str(listener.address), AUTHKEY_ENV: authkey.hex()}
        proc = subprocess.Popen([self._python, "-m", "admin.ingest_worker"], cwd=str(PROJECT_ROOT), env=env)
        accepted: list[Connection] = []
        failure: list[BaseException] = []

        def accept() -> None:
            try:
                accepted.append(listener.accept())
            except BaseException as exc:  # noqa: BLE001 - 認証の失敗等は起動の失敗として扱う
                failure.append(exc)

        thread = threading.Thread(target=accept, name="ingest-worker-accept", daemon=True)
        thread.start()
        deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
        while thread.is_alive() and proc.poll() is None and time.monotonic() < deadline:
            thread.join(0.2)
        if not accepted:
            if thread.is_alive():
                # accept() を待ったままにしないよう、自分で接続して抜けさせる。
                try:
                    Client(listener.address, authkey=authkey).close()
                except OSError:
                    pass
                thread.join(5)
            listener.close()
            if proc.poll() is None:
                proc.kill()
            reason = f": {failure[0]}" if failure else f" (exit code {proc.poll()})"
            raise WorkerError(f"取り込みワーカーのプロセスを起動できませんでした{reason}")
        listener.close()
        self._conn, self._proc = accepted[0], proc
        logger.info("Knowledge: started the ingest worker process (pid %d).", proc.pid)
        self._send(("resize", *self.slots.limits()))
        threading.Thread(target=self._receive, args=(accepted[0], proc), name="ingest-worker-receiver", daemon=True).start()

    def _send(self, message: tuple) -> None:
        conn = self._conn
        if conn is None:
            raise WorkerError("取り込みワーカーのプロセスが動いていません。")
        with self._send_lock:
            try:
                conn.send(message)
            except (OSError, EOFError, ValueError) as exc:
                raise WorkerError(f"取り込みワーカーのプロセスへ送れませんでした: {exc}") from exc

    def _send_if_running(self, message: tuple) -> None:
        if self._conn is None:
            return
        try:
            self._send(message)
        except WorkerError as exc:
            logger.warning("Knowledge: %s", exc)

    def _receive(self, conn: Connection, proc: subprocess.Popen) -> None:
        try:
            while True:
                try:
                    message = conn.recv()
                except (EOFError, OSError):
                    break
                self._dispatch(message)
        finally:
            with self._lock:
                if self._conn is conn:
                    self._conn = None
            conn.close()
            try:
                code = proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
            self.slots._update(None)
            with self._calls_lock:
                pending = list(self._calls.items())
                self._calls.clear()
            if pending or not self._closed:
                logger.warning("Knowledge: the ingest worker process (pid %d) exited with code %s.", proc.pid, code)
            for _job_id, call in pending:
                call.error = (True, f"取り込みワーカーのプロセスが異常終了しました（終了コード {code}）。次の取り込みで起動し直します。")
                call.finished.set()

    def _dispatch(self, message: tuple) -> None:
        kind = message[0]
        if kind == "log":
            record: logging.LogRecord = message[1]
            logging.getLogger(record.name).handle(record)
            return
        if kind == "slots":
            self.slots._update(message[1])
            return
        with self._calls_lock:
            call = self._calls.get(message[1])
            if call is not None and kind in ("done", "error"):
                del self._calls[message[1]]
        if call is None:
            return
        try:
            if kind == "progress" and call.progress is not None:
                call.progress(*message[2:5])
            elif kind == "bytes" and call.progress_bytes is not None:
                call.progress_bytes(message[2])
        except Exception:  # noqa: BLE001 - 画面用の進捗の失敗で受信を止めない
            logger.exception("Knowledge: progress callback failed.")
        if kind == "done":
            call.result = message[2]
            call.finished.set()
        elif kind == "error":
            call.error = (message[2], message[3])
            call.finished.set()

    def close(self) -> None:
        """子プロセスに終了を求め、SHUTDOWN_GRACE_SECONDS 秒で終わらなければ強制終了する（管理ツールの終了時）。"""
        with self._lock:
            self._closed = True
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._send_if_running(("shutdown",))
        try:
            proc.wait(SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()

    # -- ジョブ ------------------------------------------------------------

    def _run(self, message: tuple, *, progress, progress_bytes, should_stop) -> object:
        self._ensure_started()
        job_id = next(self._ids)
        call = _Call(progress, progress_bytes)
        with self._calls_lock:
            self._calls[job_id] = call
        try:
            self._send((message[0], job_id, *message[1:]))
        except WorkerError:
            with self._calls_lock:
                self._calls.pop(job_id, None)
            raise
        stop_sent = False
        while not call.finished.wait(STOP_POLL_SECONDS):
            if not stop_sent and should_stop is not None and should_stop():
                stop_sent = True
                self._send_if_running(("stop", job_id))
        if call.error is not None:
            _expected, text = call.error
            raise WorkerError(text)
        return call.result

    def ingest_collection(
        self,
        cfg,
        name: str,
        *,
        rebuild: bool = False,
        progress=None,
        should_stop=None,
        instance: str = "",
        progress_bytes=None,
        **_unused,
    ) -> _Result:
        result = self._run(("ingest", cfg, name, rebuild, instance), progress=progress, progress_bytes=progress_bytes, should_stop=should_stop)
        return _Result(result)

    def copy_collection(
        self,
        src_cfg,
        src_name: str,
        dst_cfg,
        dst_name: str,
        *,
        with_vectors: bool = True,
        include_schedule: bool = False,
        progress=None,
        should_stop=None,
        **_unused,
    ) -> dict:
        return self._run(
            ("copy", src_cfg, src_name, dst_cfg, dst_name, with_vectors, include_schedule),
            progress=progress,
            progress_bytes=None,
            should_stop=should_stop,
        )


# ---------------------------------------------------------------------------
# 子プロセス側
# ---------------------------------------------------------------------------


class _ConnectionLogHandler(logging.handlers.QueueHandler):
    """ログを管理ツールへ送る（QueueHandler.prepare でメッセージ・例外を文字列にしてから送る）。"""

    def __init__(self, send: Callable[[tuple], None]):
        super().__init__(None)  # type: ignore[arg-type]
        self._send_message = send

    def enqueue(self, record: logging.LogRecord) -> None:
        record.threadName = f"ingest-worker/{record.threadName}"
        self._send_message(("log", record))


class _WorkerServer:
    def __init__(self, conn: Connection):
        from src.vector_db.ingest import IngestSlots

        self.conn = conn
        self.send_lock = threading.Lock()
        self.slots = IngestSlots(1)
        self.stops: dict[int, threading.Event] = {}
        self.threads: dict[int, threading.Thread] = {}
        self.lock = threading.Lock()
        self.alive = True

    def send(self, message: tuple) -> None:
        with self.send_lock:
            try:
                self.conn.send(message)
            except (OSError, EOFError, ValueError):
                # 管理ツールが終わった（パイプが閉じた）。取り込みを続けても結果を受け取る相手がいない。
                os._exit(1)

    def _throttled(self, job_id: int, kind: str):
        """kind（"progress" / "bytes"）の通知を PROGRESS_INTERVAL_SECONDS ごとに間引いて送る関数と、
        間引いた最後の通知を送る関数（ジョブの終わりに呼ぶ）の組。"""
        lock = threading.Lock()
        state = {"sent_at": 0.0, "pending": None}

        def notify(*args) -> None:
            now = time.monotonic()
            with lock:
                if now - state["sent_at"] < PROGRESS_INTERVAL_SECONDS:
                    state["pending"] = args
                    return
                state["sent_at"], state["pending"] = now, None
            self.send((kind, job_id, *args))

        def flush() -> None:
            with lock:
                args, state["pending"] = state["pending"], None
            if args is not None:
                self.send((kind, job_id, *args))

        return notify, flush

    def _run_job(self, job_id: int, target: Callable[..., object]) -> None:
        from src.vector_db import catalog as vcat
        from src.vector_db.collection_copy import CopyError
        from src.vector_db.embeddings import EmbeddingError
        from src.vector_db.ingest import IngestError
        from src.vector_db.store import StoreError

        stop = self.stops[job_id]
        progress, flush_progress = self._throttled(job_id, "progress")
        progress_bytes, flush_bytes = self._throttled(job_id, "bytes")
        try:
            try:
                result = target(stop, progress, progress_bytes)
            finally:
                flush_progress()
                flush_bytes()
        except (IngestError, EmbeddingError, StoreError, CopyError, vcat.CatalogError, OSError) as exc:
            logger.warning("Ingest worker: job %d error: %s", job_id, exc, exc_info=True)
            self.send(("error", job_id, True, str(exc)))
        except Exception as exc:  # noqa: BLE001 - 想定外の例外も管理ツールへ返して、ジョブが動いたまま残らないようにする
            logger.exception("Ingest worker: job %d unexpected error.", job_id)
            self.send(("error", job_id, False, f"{type(exc).__name__}: {exc}"))
        else:
            self.send(("done", job_id, result))
        finally:
            with self.lock:
                self.stops.pop(job_id, None)
                self.threads.pop(job_id, None)

    def _start(self, job_id: int, name: str, target: Callable[..., object]) -> None:
        with self.lock:
            self.stops[job_id] = threading.Event()
            thread = threading.Thread(target=self._run_job, args=(job_id, target), name=name, daemon=True)
            self.threads[job_id] = thread
        thread.start()

    def handle(self, message: tuple) -> None:
        kind = message[0]
        if kind == "ingest":
            _kind, job_id, cfg, collection, rebuild, instance = message

            def ingest(stop: threading.Event, progress, progress_bytes):
                from src.vector_db.ingest import ingest_collection

                return ingest_collection(
                    cfg,
                    collection,
                    rebuild=rebuild,
                    progress=progress,
                    should_stop=stop.is_set,
                    slots=self.slots,
                    instance=instance,
                    progress_bytes=progress_bytes,
                ).to_dict()

            self._start(job_id, f"ingest-{instance}-{collection}", ingest)
        elif kind == "copy":
            _kind, job_id, src_cfg, src_name, dst_cfg, dst_name, with_vectors, include_schedule = message

            def copy(stop: threading.Event, progress, _progress_bytes):
                from src.vector_db.collection_copy import copy_collection

                return copy_collection(
                    src_cfg,
                    src_name,
                    dst_cfg,
                    dst_name,
                    with_vectors=with_vectors,
                    include_schedule=include_schedule,
                    progress=progress,
                    should_stop=stop.is_set,
                )

            self._start(job_id, f"copy-{dst_name}", copy)
        elif kind == "stop":
            with self.lock:
                event = self.stops.get(message[1])
            if event is not None:
                event.set()
        elif kind == "resize":
            self.slots.resize(*message[1:4])
        elif kind == "shutdown":
            self.alive = False

    def slots_loop(self) -> None:
        last = None
        while True:
            snapshot = self.slots.snapshot()
            if snapshot != last:
                self.send(("slots", snapshot))
                last = snapshot
            time.sleep(SLOTS_INTERVAL_SECONDS)

    def serve(self) -> None:
        threading.Thread(target=self.slots_loop, name="slots", daemon=True).start()
        while self.alive:
            try:
                message = self.conn.recv()
            except (EOFError, OSError):
                break
            self.handle(message)
        # 終了を求められた・管理ツールが終わった: 動いているジョブに停止を求め、少し待ってから終わる。
        with self.lock:
            for event in self.stops.values():
                event.set()
            threads = list(self.threads.values())
        deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS - 1
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))


def main() -> None:
    address = os.environ.get(ADDRESS_ENV)
    authkey = os.environ.get(AUTHKEY_ENV)
    if not address or not authkey:
        sys.exit("admin.ingest_worker is started by the settings dashboard (admin.server); do not run it directly.")
    conn = Client(address, authkey=bytes.fromhex(authkey))
    server = _WorkerServer(conn)
    root = logging.getLogger()
    root.handlers[:] = [_ConnectionLogHandler(server.send)]
    root.setLevel(logging.INFO)
    logger.info("Ingest worker: started (pid %d).", os.getpid())
    server.serve()
    os._exit(0)


if __name__ == "__main__":
    main()
