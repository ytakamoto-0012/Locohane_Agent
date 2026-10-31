"""check_work_dir_status ツールと作業ディレクトリアクセス確認。"""

from __future__ import annotations

from dataclasses import dataclass
from langchain_core.tools import tool
from pathlib import Path
import chainlit as cl
import json
import os
import uuid

from . import _state
from ._workdir import _build_workdir_status_info


@dataclass
class WorkDirAccessStatus:
    """作業ディレクトリへの実際のアクセス可否（probe_workdir_access の戻り値）。

    サーバー駆動でローカルネットワーク上の別PCから利用される場合、ユーザーの
    PCからは見える/書き込めるパスでも、サーバープロセス側からは見えない、
    または見えても書き込み権限が無い（読み取り専用共有など）ことがある。
    os.access() はWindowsのネットワーク共有・ACL構成では実態と食い違う
    ことがあるため、実際のI/Oで判定する（probe_workdir_access 参照）。
    """

    path: str
    exists: bool
    readable: bool
    writable: bool
    error: str | None = None


def probe_workdir_access(path: Path) -> WorkDirAccessStatus:
    """作業ディレクトリの読み取り/書き込み可否を実際のI/Oで検証する。

    Args:
        path: 検証対象のディレクトリパス。

    Returns:
        存在確認・読み取り確認（os.listdir）・書き込み確認（一時ファイルの
        作成/削除）の結果をまとめた WorkDirAccessStatus。
    """
    if not path.is_dir():
        return WorkDirAccessStatus(str(path), exists=False, readable=False, writable=False)
    try:
        os.listdir(path)
    except OSError as e:
        return WorkDirAccessStatus(str(path), exists=True, readable=False, writable=False, error=str(e))
    probe = path / f".agent_write_test_{uuid.uuid4().hex}.tmp"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return WorkDirAccessStatus(str(path), exists=True, readable=True, writable=False, error=str(e))
    return WorkDirAccessStatus(str(path), exists=True, readable=True, writable=True)


@tool
def check_work_dir_status() -> str:
    """Check the access status of the working directory.

    Use it when run_script / execute_python_code returns a read/write error with an unknown cause.

    Returns:
        A JSON string with the keys absolute_path, state (read_write/read_only/unreadable/not_found),
        write_dir and description (read_dir is added when it is not accessible). Paths are always
        kept in separate keys, because mixing path values with explanatory text can make
        low-parameter models misread the text as part of one path. **Always write to write_dir.
        Do not use state to decide where to write** (if write_dir equals absolute_path you can
        write there directly; if it differs, writes are redirected).
    """
    if _state._DEFAULT_WORKDIR is None:
        return json.dumps({"error": "init_tools() has not been run"}, ensure_ascii=False)
    work_dir = cl.user_session.get("work_dir")
    status = None
    if work_dir:
        status = probe_workdir_access(Path(work_dir))
        cl.user_session.set("work_dir_access", status)
    result = _build_workdir_status_info(work_dir, status)
    if status is not None and status.error:
        result["detail"] = status.error
    return json.dumps(result, ensure_ascii=False)
