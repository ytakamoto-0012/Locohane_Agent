"""インスタンスごとの子プロセス（Locohane本体、chainlit run app.py）の起動・停止・再起動・状態監視。

管理ツールが起動していないインスタンス（app.bat で直接起動した等）は
「外部で起動中」として検知するに留め、管理はしない（src.instance_lock.is_locked
を使う。停止・再起動する手段を持たないため）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from admin.i18n import t as _t
from src import instance_lock
from src.config import PROJECT_ROOT, load_config

from . import instances as inst

logger = logging.getLogger(__name__)

# Windows専用: 新しいプロセスグループで起動する（CTRL_BREAK_EVENTを
# プロセス自身にだけ送るため。指定しないと管理ツール自身も巻き込まれる）。
_CREATE_NEW_PROCESS_GROUP = 0x00000200 if sys.platform == "win32" else 0


class InstanceState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"  # 管理ツールの子プロセスとして稼働中
    EXTERNAL = "external"  # app.bat 等、管理ツール以外から起動中（app.lockで検知）
    CRASHED = "crashed"  # 子プロセスとして起動したが、異常終了コードで終了した
    ERROR = "error"  # 設定（config_overrides.json等）が壊れていて状態を解決できない


@dataclass
class InstanceStatus:
    state: InstanceState
    pid: int | None = None
    exit_code: int | None = None


class SupervisorError(Exception):
    """supervisor.py の操作失敗。"""


class Supervisor:
    """全インスタンスの子プロセスを一元管理する（管理ツールのプロセス内に1つだけ生成する）。

    FastAPI の同期エンドポイントはスレッドプールで並行実行されるため、
    start/stop/restart/status は全て self._lock（再入可能）で保護する
    （2026-09-29 レビューで発見：無保護だと起動ボタンの連打等で Popen が
    二重に呼ばれ、片方が追跡から漏れて「外部で起動中」として固着していた）。
    再入可能にしているのは restart() が同じスレッドから stop()/start() を
    呼ぶため。
    """

    def __init__(
        self,
        instances_root: Path,
        config_ini_path: Path,
        project_root: Path = PROJECT_ROOT,
        ssl_files: tuple[Path, Path] | None = None,
    ):
        self._instances_root = instances_root
        self._config_ini_path = config_ini_path
        self._project_root = project_root
        # HTTPS 用の (証明書, 秘密鍵)。None なら HTTP（src.config.resolve_ssl_files 参照）。
        self._ssl_files = ssl_files
        self._processes: dict[str, subprocess.Popen] = {}
        self._crashed_exit_codes: dict[str, int] = {}
        self._lock = threading.RLock()

    def _lock_path_for(self, name: str) -> Path:
        """そのインスタンスの app.lock パス（src.instance_lock が使うのと同じ規則）。

        common_data_dir の上書きを反映した実効パスを load_config() で解決する
        （config_overrides.json で common_data_dir を独自に変更していても
        正しく追従する）。

        Raises:
            Exception: config_overrides.json が壊れている等で load_config()
                自体が失敗した場合（呼び出し元の status() 参照）。
        """
        cfg = load_config(
            config_path=self._config_ini_path,
            overrides_path=inst.overrides_path(self._instances_root, name),
            instance_name=name,
        )
        return cfg.checkpoint_db.parent / "app.lock"

    def status(self, name: str) -> InstanceStatus:
        with self._lock:
            proc = self._processes.get(name)
            if proc is not None:
                exit_code = proc.poll()
                if exit_code is None:
                    return InstanceStatus(state=InstanceState.RUNNING, pid=proc.pid)
                # 子プロセスが自然終了/クラッシュしていた場合は片付けておく。
                del self._processes[name]
                if exit_code != 0:
                    self._crashed_exit_codes[name] = exit_code
                    return InstanceStatus(state=InstanceState.CRASHED, exit_code=exit_code)
            try:
                locked = instance_lock.is_locked(self._lock_path_for(name))
            except Exception as exc:  # noqa: BLE001 - 設定が壊れていても一覧表示自体は継続させる
                logger.warning("Failed to resolve the settings of instance %r: %s", name, exc)
                return InstanceStatus(state=InstanceState.ERROR)
            if locked:
                return InstanceStatus(state=InstanceState.EXTERNAL)
            crashed = self._crashed_exit_codes.get(name)
            if crashed is not None:
                return InstanceStatus(state=InstanceState.CRASHED, exit_code=crashed)
            return InstanceStatus(state=InstanceState.STOPPED)

    def is_managed(self, name: str) -> bool:
        """このプロセス（管理ツール）が起動した子プロセスとして現在稼働中か。"""
        return self.status(name).state == InstanceState.RUNNING

    def start(self, name: str) -> InstanceStatus:
        """インスタンスを子プロセスとして起動する。

        Raises:
            SupervisorError: 既に稼働中（RUNNING/EXTERNAL）、設定が壊れている
                （ERROR）、またはポート・データ保存先が他インスタンスと
                衝突する場合。inst.InstanceError はここで SupervisorError へ
                変換する（呼び出し元が1種類の例外だけを見ればよいように
                するため。2026-09-29 レビューで発見：以前は変換しておらず、
                起動時の重複エラーが捕捉されないまま500エラー・
                autostart処理自体のクラッシュに繋がっていた）。
        """
        with self._lock:
            current = self.status(name)
            if current.state == InstanceState.RUNNING:
                raise SupervisorError(_t("インスタンス {0!r} は既に稼働中です。", name))
            if current.state == InstanceState.EXTERNAL:
                raise SupervisorError(
                    _t("インスタンス {0!r} は管理ツール以外のプロセスで起動中のため、ここからは操作できません。"
                    "先にそちらを終了してください。", name)
                )
            if current.state == InstanceState.ERROR:
                raise SupervisorError(_t("インスタンス {0!r} の設定を解決できないため起動できません。config_overrides.json を確認してください。", name))
            try:
                meta = inst.read_instance(self._instances_root, name)
                inst.check_conflicts(
                    self._instances_root,
                    self._config_ini_path,
                    name=name,
                    app_host=meta.app_host,
                    app_port=meta.app_port,
                    kind=meta.kind,
                )
            except inst.InstanceError as exc:
                raise SupervisorError(str(exc)) from exc

            env = dict(os.environ)
            env["CONFIG_OVERRIDES_PATH"] = str(inst.overrides_path(self._instances_root, name))
            env["LOCOHANE_INSTANCE_ENV"] = str(inst.env_path(self._instances_root, name))
            env["LOCOHANE_INSTANCE"] = name
            # 子プロセスの標準出力を PYTHONUTF8=1 なしでファイルへリダイレクトすると、
            # Windows では stdout がコンソール既定の cp932 にフォールバックし、絵文字等
            # 非cp932文字の print/logging で UnicodeEncodeError を起こして異常終了しうる
            # （2026-09-29 レビューで発見・instances/default/app_stdout.log の実ログで
            # cp932化を確認）。UTF-8モード強制で、書き込むログファイル自体もUTF-8にする。
            env["PYTHONUTF8"] = "1"

            log_path = inst.stdout_log_path(self._instances_root, name)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "a", encoding="utf-8", errors="replace")

            # --headless: chainlit run は既定でブラウザタブを自動で開く。管理ツールから
            # 複数インスタンスを起動する運用（autostart含む）でその都度タブが開くのは
            # 煩わしいため、インスタンスごとの設定（既定True）で抑止できる（開きたい
            # 場合はUIの「開く」リンクから明示的に開く）。
            # --watch(-w): ファイル変更検知での自動リロード。本番運用では非推奨のため
            # 既定False。開発時の動作確認用にインスタンスごとに有効化できる。
            cmd = self._command_for(meta, self._ssl_files)
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self._project_root),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=_CREATE_NEW_PROCESS_GROUP,
                )
            finally:
                log_file.close()
            self._processes[name] = proc
            self._crashed_exit_codes.pop(name, None)
            if meta.is_skill_lab:
                logger.info("Started skill lab %r (pid=%d).", name, proc.pid)
            else:
                logger.info("Started instance %r (pid=%d, %s:%d).", name, proc.pid, meta.app_host, meta.app_port)
            return InstanceStatus(state=InstanceState.RUNNING, pid=proc.pid)

    @staticmethod
    def _command_for(meta: inst.InstanceMeta, ssl_files: tuple[Path, Path] | None = None) -> list[str]:
        """インスタンスの種類に応じた起動コマンド。

        スキル調整ワーカー（kind=skill_lab）はチャット画面を持たず、研究テーマを処理する
        ワーカー（admin/skill_lab/worker.py）を起動する。ワーカーも app.lock を取るため、
        稼働判定・停止（CTRL_BREAK_EVENT）は本体と同じ仕組みで扱える。
        """
        if meta.is_skill_lab:
            return [sys.executable, "-m", "admin.skill_lab.worker", "--lab", meta.name]
        cmd = [
            sys.executable,
            "-m",
            "chainlit",
            "run",
            "app.py",
            "--host",
            meta.app_host,
            "--port",
            str(meta.app_port),
        ]
        if ssl_files is not None:
            cmd += ["--ssl-cert", str(ssl_files[0]), "--ssl-key", str(ssl_files[1])]
        if meta.headless:
            cmd.append("--headless")
        if meta.watch:
            cmd.append("--watch")
        return cmd

    def stop(self, name: str, timeout_seconds: float = 15.0) -> None:
        """稼働中の子プロセスへ CTRL_BREAK_EVENT を送って正常終了させる（タイムアウトでkill）。

        管理ツールの子プロセスでない（EXTERNAL/STOPPED）場合は何もしない。
        """
        with self._lock:
            proc = self._processes.get(name)
            if proc is None or proc.poll() is not None:
                self._processes.pop(name, None)
                return
            if sys.platform == "win32":
                import signal

                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                logger.warning("Instance %r did not exit within %.0f seconds; killing it.", name, timeout_seconds)
                proc.kill()
                proc.wait(timeout=timeout_seconds)
            self._processes.pop(name, None)
            logger.info("Stopped instance %r.", name)

    def restart(self, name: str, timeout_seconds: float = 15.0) -> InstanceStatus:
        with self._lock:
            was_running = self.status(name).state == InstanceState.RUNNING
            if was_running:
                self.stop(name, timeout_seconds=timeout_seconds)
                self._wait_for_lock_release(name, timeout_seconds=timeout_seconds)
            return self.start(name)

    def _wait_for_lock_release(self, name: str, timeout_seconds: float) -> None:
        """app.lock が解放されるまで（最大 timeout_seconds）待つ。

        stop() で子プロセスの終了自体は待機済みだが、プロセス終了直後の
        OSによるファイルハンドル解放にはわずかな遅延がありうるため、
        再起動時の「多重起動」誤検知を避けるために一呼吸置く。
        """
        deadline = time.monotonic() + timeout_seconds
        lock_path = self._lock_path_for(name)
        while time.monotonic() < deadline:
            if not instance_lock.is_locked(lock_path):
                return
            time.sleep(0.2)

    def stop_all(self) -> None:
        """管理ツール終了時に、子プロセスとして起動した全インスタンスを止める（[admin].stop_apps_on_exit用）。"""
        with self._lock:
            for name in list(self._processes.keys()):
                self.stop(name)
