"""管理ツールのユーザー一括削除 API（POST /api/instances/{name}/users/delete）のテスト。"""

from __future__ import annotations

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


def test_delete_users_requires_csrf_header(ctx) -> None:
    env_files.write_users(ctx["env"], {"alice": "a"})
    res = ctx["client"].post("/api/instances/default/users/delete", json={"usernames": ["alice"]})
    assert res.status_code == 403
    assert env_files.read_users(ctx["env"]) == {"alice": "a"}
