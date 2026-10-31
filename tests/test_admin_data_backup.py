"""設定ダッシュボードの「データバックアップ」（admin/data_backup.py）のテスト。"""

from __future__ import annotations

import dataclasses
from datetime import datetime
import json
import sqlite3
import time
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from _vector_db_fakes import make_config
from admin import data_backup as db
from admin import instances as inst


INI = """[data_backup]
enabled = false
dest_dir =
times = 02:00
weekdays =
keep = 2
exclude_workdir = true
exclude_logs = true
exclude =
"""


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "instances"
    (root / "org_a").mkdir(parents=True)
    inst.write_instance(root, inst.InstanceMeta(name="org_a", display_name="org_a", app_port=18001))
    (root / "org_a" / "app_stdout.log").write_text("stdout", encoding="utf-8")
    (root / "_admin_logs").mkdir()
    (root / "_admin_logs" / "admin_1.log").write_text("log", encoding="utf-8")
    # バックアップが読むパスはすべて tmp_path の中にする（make_config は本物の data/ を指したままのため）。
    data = tmp_path / "data" / "org_a"
    cfg = dataclasses.replace(
        make_config(data),
        common_data_dir=data,
        checkpoint_db=data / "checkpoints.sqlite",
        thread_store_db=data / "chat_threads.sqlite",
        default_workdir=data / "temp",
        log_dir=data / "logs",
        chat_log_dir=data / "logs_chat",
    )
    for sub in ("memory", "temp", "logs"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    (data / "memory" / "note.md").write_text("覚えておくこと", encoding="utf-8")
    (data / "temp" / "work.xlsx").write_bytes(b"big")
    (data / "logs" / "app_1.log").write_text("log", encoding="utf-8")
    # 書き込み中（未コミットのトランザクションあり）の WAL の SQLite。
    conn = sqlite3.connect(data / "checkpoints.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('committed')")
    conn.commit()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO t VALUES ('uncommitted')")
    ini = tmp_path / "config.ini"
    ini.write_text(INI, encoding="utf-8")
    dest = tmp_path / "backup"
    configs = {"org_a": cfg}
    app = FastAPI()
    router = db.build_router(
        require_login=lambda: "admin",
        require_csrf=lambda: None,
        instances_root=root,
        config_ini_path=ini,
        audit_log_path=tmp_path / "audit.log",
        client_ip=lambda _r: "127.0.0.1",
        config_loader=lambda name: configs[name],
    )
    app.include_router(router)
    yield {"root": root, "ini": ini, "dest": dest, "cfg": cfg, "client": TestClient(app), "router": router, "tmp": tmp_path}
    conn.rollback()
    conn.close()


def _settings(env, **values):
    return db.save_settings(env["ini"], env["root"], {"dest_dir": str(env["dest"]), **values})


def _wait(client):
    for _ in range(200):
        job = client.get("/api/data-backup").json()["job"]
        if job and job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("バックアップが終わらない")


def test_backup_zip_contents(env):
    settings = _settings(env)
    sources, warnings = db.collect_sources(env["root"], lambda name: env["cfg"], settings)
    result = db.run_backup(settings, sources, warnings=warnings, now=datetime(2026, 10, 28, 2, 0))
    assert result["file"] == "locohane_backup_20261028_020000.zip" and result["failed"] == []
    with zipfile.ZipFile(env["dest"] / result["file"]) as zf:
        names = set(zf.namelist())
        assert "instances/org_a/instance.json" in names
        assert "data/org_a/memory/note.md" in names
        assert "data/org_a/checkpoints.sqlite" in names
        # 作業フォルダ・ログ・SQLite の付属ファイルは入れない。
        assert not any(n.startswith(("data/org_a/temp/", "data/org_a/logs/", "instances/_admin_logs/")) for n in names)
        assert "instances/org_a/app_stdout.log" not in names
        assert not any(n.endswith(("-wal", "-shm")) for n in names)
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["files"] == result["files"] and {s["arc_root"] for s in manifest["sources"]} == {"instances", "data/org_a"}
        extracted = env["tmp"] / "restored"
        zf.extract("data/org_a/checkpoints.sqlite", extracted)
    # 書き込み中でも、コミット済みの内容で開ける SQLite になっている。
    restored = sqlite3.connect(extracted / "data" / "org_a" / "checkpoints.sqlite")
    assert restored.execute("SELECT v FROM t").fetchall() == [("committed",)]
    restored.close()
    assert not list(env["dest"].glob("*.partial"))


def test_excludes_can_be_turned_off_and_extended(env):
    settings = _settings(env, exclude_workdir=False, exclude_logs=False, exclude=["*.md"])
    sources, _ = db.collect_sources(env["root"], lambda name: env["cfg"], settings)
    names = {arc for _path, arc in db.iter_files(sources, settings.exclude)}
    assert "data/org_a/temp/work.xlsx" in names and "data/org_a/logs/app_1.log" in names
    assert "instances/_admin_logs/admin_1.log" in names and "instances/org_a/app_stdout.log" in names
    assert "data/org_a/memory/note.md" not in names


def test_keep_removes_old_generations(env):
    settings = _settings(env)
    sources, _ = db.collect_sources(env["root"], lambda name: env["cfg"], settings)
    for hour in (1, 2, 3):
        db.run_backup(settings, sources, now=datetime(2026, 10, 28, hour, 0))
    assert [p.name for p in db.backup_files(env["dest"])] == ["locohane_backup_20261028_020000.zip", "locohane_backup_20261028_030000.zip"]


def test_stop_leaves_no_partial_zip(env):
    settings = _settings(env)
    sources, _ = db.collect_sources(env["root"], lambda name: env["cfg"], settings)
    with pytest.raises(db.BackupStopped):
        db.run_backup(settings, sources, should_stop=lambda: True)
    assert list(env["dest"].iterdir()) == []


def test_external_sqlite_paths_are_included(env, tmp_path):
    outside = tmp_path / "elsewhere" / "chat_threads.sqlite"
    outside.parent.mkdir()
    sqlite3.connect(outside).close()
    cfg = dataclasses.replace(env["cfg"], thread_store_db=outside)
    settings = _settings(env)
    sources, _ = db.collect_sources(env["root"], lambda name: cfg, settings)
    names = {arc for _path, arc in db.iter_files(sources, settings.exclude)}
    assert "data/org_a/_external/thread_store_db/chat_threads.sqlite" in names


def test_settings_validation_and_dest_inside_source(env):
    with pytest.raises(db.BackupError):
        db.parse_settings({"times": "25:00"})
    with pytest.raises(db.BackupError):
        db.parse_settings({"keep": "0"})
    with pytest.raises(db.BackupError):
        db.parse_settings({"dest_dir": "relative/dir"})
    settings = db.parse_settings({"dest_dir": str(env["cfg"].common_data_dir / "backups")})
    sources, _ = db.collect_sources(env["root"], lambda name: env["cfg"], settings)
    with pytest.raises(db.BackupError):
        db.check_dest(settings, sources)
    assert db.parse_settings({"times": "7:30, 02:00", "weekdays": "4,0"}).times == ("02:00", "07:30")


def test_settings_api_save_reset_and_sources(env):
    client = env["client"]
    data = client.get("/api/data-backup").json()
    assert data["settings"]["keep"] == 2 and data["sources"]["keep"] == "config.ini" and data["backups"] == []
    body = {"enabled": True, "dest_dir": str(env["dest"]), "times": ["03:00"], "weekdays": [0], "keep": 5}
    assert client.put("/api/data-backup/settings", json=body).status_code == 200
    data = client.get("/api/data-backup").json()
    assert (data["settings"]["keep"], data["sources"]["keep"], data["settings"]["times"]) == (5, "dashboard", ["03:00"])
    assert data["next_run_at"]
    bad = client.put("/api/data-backup/settings", json={**body, "dest_dir": str(env["cfg"].common_data_dir / "x")})
    assert bad.status_code == 400
    # 断った設定は保存しない（前に保存した値のまま）。
    assert client.get("/api/data-backup").json()["settings"]["dest_dir"] == str(env["dest"])
    assert client.delete("/api/data-backup/settings").json() == {"ok": True}
    assert client.get("/api/data-backup").json()["sources"]["keep"] == "config.ini"


def test_run_api_creates_backup_and_lists_it(env):
    client = env["client"]
    assert client.post("/api/data-backup/run").status_code == 400  # 保存先が未設定
    _settings(env)
    assert client.post("/api/data-backup/run").status_code == 200
    job = _wait(client)
    assert job["state"] == "done" and job["result"]["files"] > 0, job
    [backup] = client.get("/api/data-backup").json()["backups"]
    assert backup["name"] == job["result"]["file"]
    assert client.post("/api/data-backup/stop").status_code == 409
    actions = [json.loads(line)["action"] for line in (env["tmp"] / "audit.log").read_text(encoding="utf-8").splitlines()]
    assert actions[-2:] == ["data_backup_start", "data_backup_end"]


def test_broken_settings_file_is_reported_and_can_be_saved_over(env):
    db.settings_path(env["root"]).write_text("{broken", encoding="utf-8")
    client = env["client"]
    assert client.get("/api/data-backup").json()["settings_error"]
    assert client.post("/api/data-backup/run").status_code == 400
    # 自動バックアップの確認でも例外にせず、飛ばす。
    scheduler = env["router"].backup_scheduler
    scheduler._last_minute = datetime(2026, 10, 28, 1, 59)
    assert not scheduler.tick(datetime(2026, 10, 28, 2, 0))
    body = {"dest_dir": str(env["dest"]), "times": ["03:00"], "keep": 3}
    assert client.put("/api/data-backup/settings", json=body).status_code == 200
    assert client.get("/api/data-backup").json()["settings_error"] is None


def test_leftover_partial_is_removed_and_sqlite_copies_are_cleaned_up(env):
    settings = _settings(env)
    env["dest"].mkdir()
    leftover = env["dest"] / "locohane_backup_20261027_020000.zip.partial"
    leftover.write_bytes(b"unfinished")
    sources, _ = db.collect_sources(env["root"], lambda name: env["cfg"], settings)
    db.run_backup(settings, sources, now=datetime(2026, 10, 28, 2, 0))
    assert not leftover.exists()
    # SQLite の一時コピーは元と同じフォルダに作り、終わったら消す。
    assert not list(env["cfg"].common_data_dir.glob("*.tmp"))


def test_scheduler_runs_at_the_set_time(env):
    _settings(env, enabled=True, times=["02:00"])
    scheduler = env["router"].backup_scheduler
    scheduler._last_minute = datetime(2026, 10, 28, 1, 58)
    assert not scheduler.tick(datetime(2026, 10, 28, 1, 59, 30))
    assert scheduler.tick(datetime(2026, 10, 28, 2, 0, 20))
    assert _wait(env["client"])["trigger"] == "schedule"
    # 無効なら動かさない。
    _settings(env, enabled=False)
    scheduler._last_minute = datetime(2026, 10, 29, 1, 59)
    assert not scheduler.tick(datetime(2026, 10, 29, 2, 0))
