"""OS の一時フォルダ（%TEMP%）を使わず、置き場を管理できる場所に作って残りを片付けることのテスト。

- run_script の書き込みガード（sitecustomize.py）は [scripts].guard_temp_dir の下に作り、
  起動時の片付け（cleanup_guard_temp）は動きうる時間を過ぎたものだけを消す。
- MCP サーバーの配布用コピーは PUBLISH_ROOT の下に作り、持ち主が終了したもの（ロックの取れるもの）だけを消す。
- 管理ツールの設定プレビューの一時ファイルは config_overrides.json と同じフォルダに作る。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import tools
from src.tools import _subprocess_env


class _FakeUserSession:
    def get(self, key, default=None):
        return {"thread_id": "thread-1"}.get(key, default)


@pytest.fixture
def tools_env(tmp_path, monkeypatch):
    default_workdir = tmp_path / "data" / "temp"
    default_workdir.mkdir(parents=True)
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", default_workdir)
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", None)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", None)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    return tmp_path


def test_guard_dir_is_under_the_configured_folder(tools_env, monkeypatch):
    guard_root = tools_env / "guards"
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", SimpleNamespace(script_guard_temp_dir=guard_root, bin_path=[]))
    workdir = tools_env / "work"
    workdir.mkdir()

    _, guard_dir = _subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")
    try:
        assert guard_dir is not None and guard_dir.parent == guard_root
        assert not str(guard_dir).startswith(tempfile.gettempdir()) or str(guard_root).startswith(tempfile.gettempdir())
    finally:
        shutil.rmtree(guard_dir, ignore_errors=True)


def test_guard_dir_without_config_is_next_to_default_workdir(tools_env):
    assert _subprocess_env.guard_temp_root() == tools_env / "data" / "script_guard_temp"


def test_cleanup_guard_temp_removes_only_old_leftovers(tmp_path):
    old = tmp_path / "agent_fs_guard_old"
    new = tmp_path / "agent_fs_guard_new"
    other = tmp_path / "unrelated"
    for d in (old, new, other):
        d.mkdir()
    past = time.time() - 7200
    os.utime(old, (past, past))
    os.utime(other, (past, past))

    assert _subprocess_env.cleanup_guard_temp(tmp_path, older_than_seconds=3600) == 1
    assert not old.exists() and new.exists() and other.exists()


def test_mcp_publish_dir_is_under_publish_root_and_stale_copies_are_removed(tmp_path, monkeypatch):
    from mcp_server import publish

    src = tmp_path / "skills"
    (src / "demo").mkdir(parents=True)
    (src / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\n", encoding="utf-8")
    (src / "demo" / ".env").write_text("SECRET=1", encoding="utf-8")
    monkeypatch.setattr(publish, "SKILLS_SRC", (src,))
    monkeypatch.setattr(publish.atexit, "register", lambda *a, **k: None)
    root = tmp_path / "publish"
    # 持ち主が終了したコピー（ロックを誰も持っていない）。
    stale = root / "skills_dead"
    stale.mkdir(parents=True)
    (root / "skills_dead.lock").write_bytes(b"")

    publish_dir = publish.build_publish_dir(root)

    assert publish_dir.parent == root
    assert (publish_dir / "demo" / "SKILL.md").is_file() and not (publish_dir / "demo" / ".env").exists()
    assert not stale.exists() and not (root / "skills_dead.lock").exists()
    # 動いているサーバー（このプロセス）のコピーは、別の起動の片付けでは消えない。
    assert publish.cleanup_stale(root) == []
    assert publish_dir.is_dir()
    for f in publish._held_locks:
        publish._unlock(f)
    publish._held_locks.clear()
    assert publish.cleanup_stale(root) == [publish_dir]


def test_overrides_preview_temp_file_is_next_to_the_overrides_file(tmp_path, monkeypatch):
    from admin import overrides

    created: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created.append(Path(name))
        return fd, name

    target = tmp_path / "inst" / "config_overrides.json"
    leftover = target.parent / ".config_overrides_preview_old.tmp"
    target.parent.mkdir(parents=True)
    leftover.write_text("{}", encoding="utf-8")
    past = time.time() - 7200
    os.utime(leftover, (past, past))
    monkeypatch.setattr(overrides.tempfile, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(overrides, "load_config", lambda **kwargs: None)
    monkeypatch.setattr(overrides, "_check_data_dir_conflict", lambda *a, **k: None)
    monkeypatch.setattr(overrides, "_build_new_data", lambda existing, updates, resets, catalog: {})
    monkeypatch.setattr(overrides, "_diff", lambda a, b: [])

    overrides.preview(
        path=target, updates={}, resets=[], ini_catalog=None, config_ini_path=tmp_path / "config.ini",
        instance_name="inst", instances_root=tmp_path,
    )

    assert created and all(p.parent == target.parent for p in created)
    assert not any(p.exists() for p in created)
    assert not leftover.exists()


def test_download_access_error_refuses_other_sessions_tmp(tools_env):
    from src.tools.provide_download import download_access_error

    default_workdir = tools._state._DEFAULT_WORKDIR
    other = default_workdir / "_tmp_000000000000_other" / "secret.txt"
    other.parent.mkdir()
    other.write_text("x", encoding="utf-8")
    mine = tools._workdir._resolve_exec_workdir() / "mine.txt"
    mine.write_text("x", encoding="utf-8")

    assert download_access_error(other)
    assert download_access_error(mine) is None


def test_backup_sqlite_copy_falls_back_to_the_backup_destination(tmp_path, monkeypatch):
    """SQLite の一時コピーを元のフォルダに作れないときは、OS の一時フォルダではなくバックアップの保存先に作る。"""
    import sqlite3
    import zipfile

    from admin import data_backup

    data_dir = tmp_path / "data"
    dest = tmp_path / "backups"
    data_dir.mkdir()
    dest.mkdir()
    db = data_dir / "a.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x)")
    conn.close()
    leftover = dest / ".locohane_backup_old.sqlite.tmp"
    leftover.write_bytes(b"")
    used: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def fake_mkstemp(*args, dir=None, **kwargs):
        if Path(dir) == data_dir:
            raise PermissionError("read-only folder")
        used.append(dir)
        return real_mkstemp(*args, dir=dir, **kwargs)

    monkeypatch.setattr(data_backup.tempfile, "mkstemp", fake_mkstemp)
    with zipfile.ZipFile(dest / "out.zip", "w") as zf:
        data_backup._add_sqlite(zf, db, "a.sqlite", dest)

    assert used == [str(dest)]
    assert not leftover.exists()
    assert not list(dest.glob(".locohane_backup_*"))
