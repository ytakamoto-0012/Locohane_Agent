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
    assert res.json() == {"copied": 2, "skipped": 0, "failed": 0, "skipped_items": [], "failed_items": []}
    copied = mem_store.find_memory(dst, "term-abc", "alice")
    assert (copied.description, copied.memory_type, copied.content, copied.author) == ("ABC: 受発注システム", "glossary", "意味", "alice")
    assert copied.updated_by == "admin (dashboard)"
    assert mem_store.find_memory(dst, "common-note", None) is not None
    # コピー元は変わらない
    assert mem_store.find_memory(src, "term-abc", "alice") is not None
    assert "term-abc" in (dst / "users" / "alice" / "MEMORY.md").read_text(encoding="utf-8")
    entries = [json.loads(line) for line in env["audit"].read_text(encoding="utf-8").splitlines()]
    assert {e["action"] for e in entries} == {"memory_copy"} and entries[0]["instance"] == "dst-inst"


def test_copy_skips_existing_name_without_replacing(env):
    src, dst = env["dirs"]["src-inst"], env["dirs"]["dst-inst"]
    mem_store.create_memory(src, "note", "new", "feedback", "新しい本文", owner="alice")
    mem_store.create_memory(dst, "note", "old", "project", "古い本文", owner="alice")
    res = _copy(env, [{"name": "note", "owner": "alice"}])
    assert res.json()["skipped"] == 1 and res.json()["skipped_items"] == ["alice / note"]
    got = mem_store.find_memory(dst, "note", "alice")
    assert (got.content, got.memory_type) == ("古い本文", "project")


def test_copy_target_owner(env):
    src, dst = env["dirs"]["src-inst"], env["dirs"]["dst-inst"]
    mem_store.create_memory(src, "a", "d", "reference", "c", owner="alice")
    assert _copy(env, [{"name": "a", "owner": "alice"}], target_owner="").json()["copied"] == 1
    assert mem_store.find_memory(dst, "a", None) is not None
    assert _copy(env, [{"name": "a", "owner": "alice"}], target_owner="bob").json()["copied"] == 1
    assert mem_store.find_memory(dst, "a", "bob") is not None


def test_private_memory_is_copied_only_to_the_same_owner(env):
    src, dst = env["dirs"]["src-inst"], env["dirs"]["dst-inst"]
    mem_store.create_memory(src, "secret", "d", "user", "私的なこと", owner="alice", visibility="private")
    # 認証情報などの有効な目印があれば、shared のまま保存されていても個人用（他のユーザーから見えない）とみなす
    (src / "users" / "alice" / "reference").mkdir(parents=True, exist_ok=True)
    (src / "users" / "alice" / "reference" / "phone.md").write_text(
        "---\nname: phone\ndescription: d\ntype: reference\nvisibility: shared\n---\n\nパスワード: x\n", encoding="utf-8"
    )
    for target_owner in ("", "bob"):
        body = _copy(env, [{"name": "secret", "owner": "alice"}, {"name": "phone", "owner": "alice"}], target_owner=target_owner).json()
        assert body["copied"] == 0 and body["failed"] == 2
        assert all("private" in item or "個人用" in item for item in body["failed_items"])
    assert mem_store.find_memory(dst, "secret", None) is None and mem_store.find_memory(dst, "secret", "bob") is None
    # 同じ所有者へならコピーでき、個人用のまま
    body = _copy(env, [{"name": "secret", "owner": "alice"}]).json()
    assert body["copied"] == 1
    assert mem_store.find_memory(dst, "secret", "alice").visibility == "private"


def test_same_name_from_two_owners_to_one_owner_skips_the_second(env):
    src, dst = env["dirs"]["src-inst"], env["dirs"]["dst-inst"]
    mem_store.create_memory(src, "note", "alice の", "reference", "a", owner="alice")
    mem_store.create_memory(src, "note", "bob の", "reference", "b", owner="bob")
    body = _copy(env, [{"name": "note", "owner": "alice"}, {"name": "note", "owner": "bob"}], target_owner="").json()
    assert (body["copied"], body["skipped"]) == (1, 1)
    assert mem_store.find_memory(dst, "note", None).content == "a"


def test_copy_validation_and_per_item_failures(env):
    src, dst = env["dirs"]["src-inst"], env["dirs"]["dst-inst"]
    mem_store.create_memory(src, "a", "d", "reference", "c")
    assert env["client"].post("/api/instances/src-inst/memories/copy", json={"items": [{"name": "a"}], "target_instance": "src-inst"}).status_code == 400
    assert _copy(env, []).status_code == 400
    # 見つからないもの・名前が不正なものは理由を付けて飛ばし、残りはコピーする
    body = _copy(env, [{"name": "a"}, {"name": "nope"}, {"name": "bad name"}]).json()
    assert body["copied"] == 1 and body["failed"] == 2
    assert any("nope" in item for item in body["failed_items"]) and any("bad name" in item for item in body["failed_items"])
    assert mem_store.find_memory(dst, "a", None) is not None
    assert env["client"].post("/api/instances/src-inst/memories/copy", json={"items": [{"name": "a"}], "target_instance": "ghost"}).status_code == 404


def test_copy_memory_refuses_private_to_other_owner(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    mem_store.create_memory(src, "secret", "d", "user", "x", owner="alice", visibility="private")
    mem = mem_store.find_memory(src, "secret", "alice")
    with pytest.raises(ValueError):
        mem_store.copy_memory(dst, mem, owner=None)
    assert mem_store.copy_memory(dst, mem, owner="alice") == "copied"
    assert mem_store.copy_memory(dst, mem, owner="alice") == "skipped"
