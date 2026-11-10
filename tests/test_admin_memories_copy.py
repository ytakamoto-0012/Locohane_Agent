"""設定ダッシュボードの「メモリーを別のインスタンスへコピー」（admin/memories.py の copy・src/memory.py の copy_memory）のテスト。"""

from __future__ import annotations

import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin import i18n as admin_i18n
from admin import instances as inst
from admin import memories
from src import memory as mem_store


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "instances"
    dirs = {}
    for i, name in enumerate(("src-inst", "dst-inst"), start=1):
        (root / name).mkdir(parents=True)
        inst.write_instance(root, inst.InstanceMeta(name=name, display_name=name, app_port=18000 + i))
        dirs[name] = tmp_path / f"memory_{name}"
    audit_log = tmp_path / "audit.log"
    app = FastAPI()
    app.add_middleware(admin_i18n.LanguageMiddleware)
    app.include_router(
        memories.build_router(
            require_login=lambda: "admin",
            require_csrf=lambda: None,
            instances_root=root,
            audit_log_path=audit_log,
            client_ip=lambda _r: "127.0.0.1",
            config_loader=lambda name: types.SimpleNamespace(memory_dir=dirs[name], name=name),
            backup_keep=2,
        )
    )
    return {"client": TestClient(app), "dirs": dirs, "audit": audit_log, "root": root}


def _copy(env, items, **extra):
    body = {"items": items, "target_instance": "dst-inst", **extra}
    return env["client"].post("/api/instances/src-inst/memories/copy", json=body)


def test_copy_keeps_owner_and_fields(env):
    src, dst = env["dirs"]["src-inst"], env["dirs"]["dst-inst"]
    mem_store.create_memory(src, "term-abc", "ABC: 受発注システム", "glossary", "意味", owner="alice", author="alice")
    mem_store.create_memory(src, "common-note", "共通", "reference", "c")
    res = _copy(env, [{"name": "term-abc", "owner": "alice"}, {"name": "common-note"}])
    assert res.status_code == 200
    assert res.json() == {"copied": 2, "overwritten": 0, "skipped": 0, "skipped_items": []}
    copied = mem_store.find_memory(dst, "term-abc", "alice")
    assert (copied.description, copied.memory_type, copied.content, copied.author) == ("ABC: 受発注システム", "glossary", "意味", "alice")
    assert copied.updated_by == "admin (dashboard)"
    assert mem_store.find_memory(dst, "common-note", None) is not None
    # コピー元は変わらない
    assert mem_store.find_memory(src, "term-abc", "alice") is not None
    assert "term-abc" in (dst / "users" / "alice" / "MEMORY.md").read_text(encoding="utf-8")
    entries = [json.loads(line) for line in env["audit"].read_text(encoding="utf-8").splitlines()]
    assert {e["action"] for e in entries} == {"memory_copy"} and entries[0]["instance"] == "dst-inst"


def test_copy_conflict_skip_then_overwrite(env):
    src, dst = env["dirs"]["src-inst"], env["dirs"]["dst-inst"]
    mem_store.create_memory(src, "note", "new", "feedback", "新しい本文", owner="alice")
    mem_store.create_memory(dst, "note", "old", "project", "古い本文", owner="alice")
    res = _copy(env, [{"name": "note", "owner": "alice"}])
    assert res.json()["skipped"] == 1 and res.json()["skipped_items"] == ["alice / note"]
    assert mem_store.find_memory(dst, "note", "alice").content == "古い本文"
    res = _copy(env, [{"name": "note", "owner": "alice"}], overwrite=True)
    assert res.json()["overwritten"] == 1
    got = mem_store.find_memory(dst, "note", "alice")
    assert (got.content, got.memory_type) == ("新しい本文", "feedback")
    assert not (dst / "users" / "alice" / "project" / "note.md").exists()
    # 置き換え前の内容はバックアップに残る
    assert list((env["root"] / "dst-inst").rglob("note*"))


def test_copy_target_owner(env):
    src, dst = env["dirs"]["src-inst"], env["dirs"]["dst-inst"]
    mem_store.create_memory(src, "a", "d", "reference", "c", owner="alice")
    assert _copy(env, [{"name": "a", "owner": "alice"}], target_owner="").status_code == 200
    assert mem_store.find_memory(dst, "a", None) is not None
    assert _copy(env, [{"name": "a", "owner": "alice"}], target_owner="bob").status_code == 200
    assert mem_store.find_memory(dst, "a", "bob") is not None


def test_copy_validation(env):
    src, dst = env["dirs"]["src-inst"], env["dirs"]["dst-inst"]
    mem_store.create_memory(src, "a", "d", "reference", "c")
    assert env["client"].post("/api/instances/src-inst/memories/copy", json={"items": [{"name": "a"}], "target_instance": "src-inst"}).status_code == 400
    assert _copy(env, []).status_code == 400
    # 1件でも無ければ何もコピーしない
    assert _copy(env, [{"name": "a"}, {"name": "nope"}]).status_code == 404
    assert mem_store.find_memory(dst, "a", None) is None
    assert env["client"].post("/api/instances/src-inst/memories/copy", json={"items": [{"name": "a"}], "target_instance": "ghost"}).status_code == 404
