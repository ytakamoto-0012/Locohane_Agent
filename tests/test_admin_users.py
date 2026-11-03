"""管理ツールのユーザー API（一括削除 POST /api/instances/{name}/users/delete・ランク PUT .../users/{user}/rank）のテスト。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from admin import env_files
from admin import instances as inst
from admin import server

HEADERS = {"X-Locohane-Admin": "1"}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    root = tmp_path / "instances"
    project_env = tmp_path / "project.env"
    monkeypatch.setattr(server, "INSTANCES_ROOT", root)
    monkeypatch.setattr(server, "PROJECT_ENV_PATH", project_env)
    monkeypatch.setattr(server, "AUDIT_LOG_PATH", tmp_path / "admin_changes.log")
    monkeypatch.setattr(server, "_require_instance", lambda name: None)
    server.app.dependency_overrides[server.require_login] = lambda: "admin"
    try:
        yield {"client": TestClient(server.app), "env": inst.env_path(root, "default"), "project_env": project_env}
    finally:
        server.app.dependency_overrides.pop(server.require_login, None)


def test_delete_multiple_users(ctx) -> None:
    env_files.write_users(ctx["env"], {"alice": "a", "bob": "b", "carol": "c"})
    res = ctx["client"].post("/api/instances/default/users/delete", headers=HEADERS, json={"usernames": ["alice", "carol"]})
    assert res.status_code == 200
    assert res.json() == {"usernames": ["bob"]}
    assert env_files.read_users(ctx["env"]) == {"bob": "b"}


def test_delete_users_is_all_or_nothing(ctx) -> None:
    env_files.write_users(ctx["env"], {"alice": "a", "bob": "b"})
    res = ctx["client"].post("/api/instances/default/users/delete", headers=HEADERS, json={"usernames": ["alice", "nobody"]})
    assert res.status_code == 404
    assert "nobody" in res.json()["detail"]
    assert env_files.read_users(ctx["env"]) == {"alice": "a", "bob": "b"}


def test_delete_users_rejects_empty_selection(ctx) -> None:
    env_files.write_users(ctx["env"], {"alice": "a"})
    res = ctx["client"].post("/api/instances/default/users/delete", headers=HEADERS, json={"usernames": []})
    assert res.status_code == 400


def test_delete_users_rejects_inherited_project_users(ctx) -> None:
    env_files.write_users(ctx["project_env"], {"alice": "a"})
    res = ctx["client"].post("/api/instances/default/users/delete", headers=HEADERS, json={"usernames": ["alice"]})
    assert res.status_code == 403
    assert env_files.read_users(ctx["project_env"]) == {"alice": "a"}


@pytest.fixture
def rank_ctx(ctx, tmp_path, monkeypatch):
    """ランクの API 用: 実際の config.ini と、tmp に置いたこのインスタンスの config_overrides.json を使う。"""
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))
    meta = inst.InstanceMeta(name="default", display_name="default", app_port=18001)
    monkeypatch.setattr(server, "_require_instance", lambda name: meta)
    return ctx


def test_set_and_clear_user_rank(rank_ctx, tmp_path) -> None:
    from src import memory

    env_files.write_users(rank_ctx["env"], {"Alice": "a", "bob": "b"})
    client = rank_ctx["client"]
    res = client.put("/api/instances/default/users/Alice/rank", headers=HEADERS, json={"rank": 3})
    assert res.status_code == 200 and res.json()["rank"] == 3
    # ユーザー名はメモリーの所有者のフォルダ名（小文字）へ揃えて保存する
    saved = json.loads(inst.overrides_path(tmp_path / "instances", "default").read_text(encoding="utf-8"))
    assert json.loads(saved["memory"]["user_ranks"]) == {"alice": 3}
    listed = client.get("/api/instances/default/users").json()
    assert listed["ranks"] == {"Alice": 3, "bob": None} and listed["default_rank"] == memory.DEFAULT_RANK
    # null で外すと、既定値（{}）に戻るので上書きからも消える
    assert client.put("/api/instances/default/users/Alice/rank", headers=HEADERS, json={"rank": None}).status_code == 200
    saved = json.loads(inst.overrides_path(tmp_path / "instances", "default").read_text(encoding="utf-8"))
    assert "user_ranks" not in saved.get("memory", {})


def test_rank_can_be_set_for_inherited_users_and_rejects_negative(rank_ctx) -> None:
    env_files.write_users(rank_ctx["project_env"], {"alice": "a"})
    client = rank_ctx["client"]
    assert client.put("/api/instances/default/users/alice/rank", headers=HEADERS, json={"rank": 2}).status_code == 200
    assert client.get("/api/instances/default/users").json()["ranks"] == {"alice": 2}
    assert client.put("/api/instances/default/users/alice/rank", headers=HEADERS, json={"rank": -1}).status_code == 400


def test_delete_users_requires_csrf_header(ctx) -> None:
    env_files.write_users(ctx["env"], {"alice": "a"})
    res = ctx["client"].post("/api/instances/default/users/delete", json={"usernames": ["alice"]})
    assert res.status_code == 403
    assert env_files.read_users(ctx["env"]) == {"alice": "a"}


def test_rank_is_rejected_while_env_var_overrides_it(rank_ctx, monkeypatch) -> None:
    """環境変数 MEMORY_USER_RANKS は config_overrides.json より優先されるため、黙って無視させない。"""
    monkeypatch.setenv("MEMORY_USER_RANKS", '{"alice": 5}')
    env_files.write_users(rank_ctx["env"], {"alice": "a"})
    client = rank_ctx["client"]
    res = client.put("/api/instances/default/users/alice/rank", headers=HEADERS, json={"rank": 2})
    assert res.status_code == 409 and "MEMORY_USER_RANKS" in res.json()["detail"]
    assert "MEMORY_USER_RANKS" in client.get("/api/instances/default/users").json()["rank_error"]
