"""サブプロセスを孫プロセスごと止めるヘルパー（run_script / execute_python_code 系が共有）。

subprocess.run(timeout=...) はタイムアウト時に直接の子プロセスしか kill せず、Windows では
その後に待ち時間の上限なしで標準出力・標準エラーのパイプが閉じるのを待つ。スクリプトが
起動した孫プロセス（外部コマンド等）がパイプを受け継いでいると、そこで止まったままツールが
返らなくなる。孫プロセス自体も残り続ける。ここではプロセスツリーごと止め、パイプの読み切りにも
上限を設ける。
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

# タイムアウトで止めた後、残りの出力を読み切るのを待つ上限（秒）。
_PIPE_DRAIN_SECONDS = 5
_TASKKILL_TIMEOUT_SECONDS = 10


def kill_process_tree(pid: int | None) -> None:
    """pid のプロセスを子孫ごと強制終了する（既に終わっていれば何もしない）。"""
    if pid is None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=_TASKKILL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("taskkill failed (pid=%s): %s", pid, e)
        return
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def run_with_timeout(cmd: list[str], *, cwd: str, env: dict[str, str] | None, timeout: float) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True, text=True, encoding="utf-8") 相当を、孫プロセスごと止める形で行う。

    Raises:
        subprocess.TimeoutExpired: timeout 秒以内に終わらなかった場合（プロセスツリーは停止済み）。
        OSError: 起動できなかった場合。
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        proc.kill()
        try:
            proc.communicate(timeout=_PIPE_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            # 止められなかった孫プロセスがパイプを握っている。読み取りスレッドは
            # デーモンのため放置し、ツール呼び出し自体は返す。
            logger.warning("Script output pipes stayed open after the timeout (pid=%s)", proc.pid)
        raise
    except BaseException:
        kill_process_tree(proc.pid)
        proc.kill()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
