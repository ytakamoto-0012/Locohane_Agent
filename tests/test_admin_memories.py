"""設定ダッシュボードの「メモリー」（admin/memories.py・src/memory.py の edit_memory）のテスト。"""

from __future__ import annotations

import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin import instances as inst
from admin import i18n as admin_i18n
from admin import memories
from src import memory as mem_store


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "instances"
    (root / "default").mkdir(parents=True)
    inst.write_instance(root, inst.InstanceMeta(name="default", display_name="default", app_port=18001))
    memory_dir = tmp_path / "memory"
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
            config_loader=lambda name: types.SimpleNamespace(memory_dir=memory_dir, name=name),
            backup_keep=2,
            llm_instance=lambda name: "llm-inst",
            model_factory=lambda config: _FakeModel(config),
        )
    )
    return {"client": TestClient(app), "memory_dir": memory_dir, "root": root, "audit": audit_log}


class _FakeModel:
    """翻訳の LLM の代わり。受け取ったシステムプロンプトの言語名と本文から決まった訳を返す。"""

    calls: list = []

    def __init__(self, config):
        self.config = config

    async def ainvoke(self, messages):
        _FakeModel.calls.append((self.config.name, messages[0].content, messages[1].content))
        return types.SimpleNamespace(content=f"<think>x</think>EN:{messages[1].content}\n")


def _audit_actions(path):
    return [json.loads(line)["action"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_create_list_get(env):
    client = env["client"]
    res = client.post(
        "/api/instances/default/memories",
        json={"name": "pref-a", "description": "説明", "type": "user", "content": "本文"},
    )
    assert res.status_code == 200
    data = client.get("/api/instances/default/memories").json()
    assert data["types"] == list(mem_store.MEMORY_TYPES)
    assert [(m["name"], m["type"], m["description"]) for m in data["memories"]] == [("pref-a", "user", "説明")]
    detail = client.get("/api/instances/default/memories/pref-a").json()
    assert detail["content"] == "本文"
    assert detail["sources"] == []
    assert "pref-a" in (env["memory_dir"] / "MEMORY.md").read_text(encoding="utf-8")
    assert _audit_actions(env["audit"]) == ["memory_create"]


def test_create_rejects_duplicate_and_bad_input(env):
    client = env["client"]
    body = {"name": "dup", "description": "d", "type": "user", "content": "c"}
    assert client.post("/api/instances/default/memories", json=body).status_code == 200
    assert client.post("/api/instances/default/memories", json={**body, "type": "project"}).status_code == 409
    assert client.post("/api/instances/default/memories", json={**body, "name": "日本語"}).status_code == 400
    assert client.post("/api/instances/default/memories", json={**body, "name": "x", "type": "bad"}).status_code == 400
    assert client.post("/api/instances/default/memories", json={**body, "name": "x", "content": " "}).status_code == 400


def test_edit_changes_type_and_keeps_created_and_backs_up(env):
    client = env["client"]
    memory_dir = env["memory_dir"]
    mem_store.create_memory(memory_dir, "note", "古い説明", "user", "古い本文")
    created = (memory_dir / "user" / "note.md").read_text(encoding="utf-8").split("created: ")[1].split("\n")[0]
    res = client.put(
        "/api/instances/default/memories/note",
        json={"description": "新しい説明", "type": "project", "content": "新しい本文"},
    )
    assert res.status_code == 200
    assert not (memory_dir / "user" / "note.md").exists()
    m = mem_store.read_memory(memory_dir, "note")
    assert (m.memory_type, m.description, m.content) == ("project", "新しい説明", "新しい本文")
    assert f"created: {created}" in m.path.read_text(encoding="utf-8")
    assert "[project] note: 新しい説明" in (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    backups = list((inst.backups_dir(env["root"], "default") / "memory" / "_common" / "note").iterdir())
    assert len(backups) == 1 and "古い本文" in backups[0].read_text(encoding="utf-8")


def test_edit_keeps_sources(env, tmp_path):
    memory_dir = env["memory_dir"]
    source = tmp_path / "src.txt"
    source.write_text("x", encoding="utf-8")
    mem_store.create_memory(memory_dir, "sourced", "d", "reference", "c", source_files=[source])
    res = env["client"].put(
        "/api/instances/default/memories/sourced", json={"description": "d2", "type": "reference", "content": "c2"}
    )
    assert res.status_code == 200
    detail = env["client"].get("/api/instances/default/memories/sourced").json()
    assert detail["sources"] == [{"path": str(source), "state": "unchanged"}]


def test_delete_backs_up_and_404s_after(env):
    client = env["client"]
    mem_store.create_memory(env["memory_dir"], "gone", "d", "feedback", "c")
    assert client.delete("/api/instances/default/memories/gone").status_code == 200
    assert client.get("/api/instances/default/memories/gone").status_code == 404
    assert client.delete("/api/instances/default/memories/gone").status_code == 404
    assert (inst.backups_dir(env["root"], "default") / "memory" / "_common" / "gone").is_dir()
    assert _audit_actions(env["audit"]) == ["memory_delete"]


def test_unknown_instance_and_bad_memory_name(env):
    client = env["client"]
    assert client.get("/api/instances/nope/memories").status_code == 404
    assert client.get("/api/instances/default/memories/..%5Cx").status_code == 400


def test_owner_scoped_crud_and_visibility(env):
    client = env["client"]
    body = {"name": "tips", "description": "d", "type": "feedback", "content": "c", "owner": "Alice", "visibility": "shared"}
    res = client.post("/api/instances/default/memories", json=body)
    # ログインユーザー名はメモリーのフォルダ名（小文字）へ整える
    assert res.json() == {"success": True, "owner": "alice", "visibility": "shared"}
    # 共通にも同じ名前で作れる（所有者が違えば別物）
    assert client.post("/api/instances/default/memories", json={**body, "owner": None}).status_code == 200
    data = client.get("/api/instances/default/memories").json()
    assert data["owners"] == ["alice"]
    assert sorted((m["owner"] or "", m["visibility"]) for m in data["memories"]) == [("", "shared"), ("alice", "shared")]
    res = client.put(
        "/api/instances/default/memories/tips?owner=alice",
        json={"description": "d", "type": "feedback", "content": "電話 03-1234-5678", "visibility": "shared"},
    )
    assert res.json()["visibility"] == "private"
    assert client.get("/api/instances/default/memories/tips?owner=alice").json()["content"] == "電話 03-1234-5678"
    assert client.get("/api/instances/default/memories/tips").json()["content"] == "c"
    assert client.delete("/api/instances/default/memories/tips?owner=alice").status_code == 200
    assert (inst.backups_dir(env["root"], "default") / "memory" / "alice" / "tips").is_dir()
    assert mem_store.find_memory(env["memory_dir"], "tips") is not None
    entry = json.loads(env["audit"].read_text(encoding="utf-8").splitlines()[-1])
    assert (entry["action"], entry["memory_owner"]) == ("memory_delete", "alice")


def test_translate_uses_selected_llm_and_does_not_save(env):
    _FakeModel.calls.clear()
    res = env["client"].post(
        "/api/instances/default/memories/translate",
        json={"description": "説明\n二行目", "content": "本文"},
        headers={"X-Locohane-Lang": "en"},
    )
    assert res.status_code == 200
    assert res.json() == {"description": "EN:説明 二行目", "content": "EN:本文", "language": "en", "llm_instance": "llm-inst"}
    assert [c[0] for c in _FakeModel.calls] == ["llm-inst", "llm-inst"]
    assert "into English" in _FakeModel.calls[0][1]
    assert not env["memory_dir"].exists()
    # ヘッダーが無ければ（日本語表示）日本語へ訳す
    _FakeModel.calls.clear()
    env["client"].post("/api/instances/default/memories/translate", json={"description": "", "content": "text"})
    assert [("into Japanese" in c[1]) for c in _FakeModel.calls] == [True]
    empty = env["client"].post("/api/instances/default/memories/translate", json={"description": " ", "content": ""})
    assert empty.status_code == 400


def test_bulk_delete_is_all_or_nothing(env):
    client = env["client"]
    memory_dir = env["memory_dir"]
    mem_store.create_memory(memory_dir, "a", "d", "feedback", "c")
    mem_store.create_memory(memory_dir, "b", "d", "feedback", "c", owner="alice")
    mem_store.create_memory(memory_dir, "keep", "d", "feedback", "c", owner="alice")
    # 1件でも見つからなければ何も消さない
    res = client.post(
        "/api/instances/default/memories/delete",
        json={"items": [{"name": "a"}, {"name": "nope", "owner": "alice"}]},
    )
    assert res.status_code == 404 and "alice / nope" in res.json()["detail"]
    assert mem_store.find_memory(memory_dir, "a") is not None
    assert client.post("/api/instances/default/memories/delete", json={"items": []}).status_code == 400
    res = client.post(
        "/api/instances/default/memories/delete",
        json={"items": [{"name": "a"}, {"name": "b", "owner": "alice"}, {"name": "b", "owner": "alice"}]},
    )
    assert res.json() == {"deleted": 2}
    assert mem_store.find_memory(memory_dir, "a") is None
    assert mem_store.find_memory(memory_dir, "b", "alice") is None
    assert mem_store.find_memory(memory_dir, "keep", "alice") is not None
    backups = inst.backups_dir(env["root"], "default") / "memory"
    assert (backups / "_common" / "a").is_dir() and (backups / "alice" / "b").is_dir()
    assert _audit_actions(env["audit"]) == ["memory_delete", "memory_delete"]
