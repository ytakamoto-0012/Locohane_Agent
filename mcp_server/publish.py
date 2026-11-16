"""skills/ の配布用コピー作成（Resources方式向け、機密ファイル除外）。

SkillsDirectoryProvider（PyPI: fastmcp）はスキルフォルダ配下を再帰的に
無条件でスキャンするため（除外パターンの指定機能なし）、`web-search` スキルの
`scripts/.env`（TAVILY_API_KEY）のような機密ファイルがそのまま配布されて
しまう。これを避けるため、配布用のコピーを作る際に EXCLUDE_NAMES に
該当するファイルを除外している。

コピーは OS の一時フォルダではなく config.PUBLISH_ROOT（既定 data/mcp_publish）の下に
サーバーごとに作る（以前は %TEMP% に作って消さなかったため、MCP クライアントが
サーバーを起動するたびにスキル一式の写しが溜まっていた）。同時に複数のサーバーが
動くことがあるため、各コピーの隣に `<名前>.lock` を置いてプロセスが生きている間
ロックし続け、起動時はロックの取れる（＝持ち主が終了した）ものだけを消す。
"""

from __future__ import annotations

import atexit
import msvcrt
import os
import shutil
import tempfile
from pathlib import Path

from .config import EXCLUDE_NAMES, PUBLISH_ROOT, SKILLS_SRC

_LOCK_SUFFIX = ".lock"
# プロセスが生きている間ロックを持ち続けるためのハンドル（GC で閉じられないよう保持する）。
_held_locks: list = []


def _try_lock(lock_path: Path):
    """lock_path をロックして開いたファイルを返す。別のプロセスが持っていれば None。"""
    f = open(lock_path, "a+b")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def _unlock(f) -> None:
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        f.close()


def cleanup_stale(root: Path) -> list[Path]:
    """root の下の、持ち主のサーバーが終了したコピー（ロックの取れるもの）を消す。消したものを返す。"""
    removed: list[Path] = []
    if not root.is_dir():
        return removed
    for entry in root.iterdir():
        if entry.name.endswith(_LOCK_SUFFIX):
            # コピーだけが先に消えてロックが残ったもの。
            if not entry.with_name(entry.name[: -len(_LOCK_SUFFIX)]).exists():
                f = _try_lock(entry)
                if f is not None:
                    _unlock(f)
                    entry.unlink(missing_ok=True)
            continue
        if not entry.is_dir():
            continue
        lock_path = entry.with_name(entry.name + _LOCK_SUFFIX)
        f = _try_lock(lock_path)
        if f is None:
            continue  # 動いている別のサーバーのコピー
        try:
            shutil.rmtree(entry, ignore_errors=True)
        finally:
            _unlock(f)
        lock_path.unlink(missing_ok=True)
        removed.append(entry)
    return removed


def _remove_own(publish_dir: Path, lock_file) -> None:
    shutil.rmtree(publish_dir, ignore_errors=True)
    _unlock(lock_file)
    publish_dir.with_name(publish_dir.name + _LOCK_SUFFIX).unlink(missing_ok=True)


def build_publish_dir(root: Path = PUBLISH_ROOT) -> Path:
    """機密ファイルを除いた配布用コピーを root の下に作成して返す（プロセス終了時に消す）。

    SKILLS_SRC は複数ルートを持ちうる。同名スキルが複数ルートに存在する
    場合は先頭に近いルート（`LOCOHANE_MCP_SKILLS_SRC` の並び順）を優先し、
    後続ルートの同名スキルは無視する。
    """
    root.mkdir(parents=True, exist_ok=True)
    cleanup_stale(root)
    # ロックを先に取ってからフォルダを作る（逆だと、ロックの無いフォルダを別サーバーの
    # cleanup_stale が持ち主のいない残りとみなして消しうる）。
    fd, lock_name = tempfile.mkstemp(prefix="skills_", suffix=_LOCK_SUFFIX, dir=root)
    os.close(fd)
    lock_path = Path(lock_name)
    publish_dir = lock_path.with_name(lock_path.name[: -len(_LOCK_SUFFIX)])
    lock_file = _try_lock(lock_path)
    publish_dir.mkdir()
    if lock_file is not None:
        _held_locks.append(lock_file)
        atexit.register(_remove_own, publish_dir, lock_file)
    seen: set[str] = set()
    for src_root in SKILLS_SRC:
        if not src_root.is_dir():
            continue
        for skill_dir in sorted(src_root.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name in seen:
                continue
            seen.add(skill_dir.name)
            shutil.copytree(
                skill_dir,
                publish_dir / skill_dir.name,
                ignore=shutil.ignore_patterns(*EXCLUDE_NAMES),
            )
    return publish_dir
