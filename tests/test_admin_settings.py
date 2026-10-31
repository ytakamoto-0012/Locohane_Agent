"""設定ダッシュボードの「管理者設定」（admin/admin_settings.py・src/config.py の admin_settings.json）のテスト。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin import admin_settings
from admin import instances as inst
from src.config import DEFAULT_CONFIG_PATH, load_config


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings_file = tmp_path / "admin_settings.json"
    monkeypatch.setenv("ADMIN_SETTINGS_PATH", str(settings_file))
    for name in ("VECTOR_DB_INGEST_WORKERS", "VECTOR_DB_INGEST_PROCESSES", "ADMIN_ASSISTANT_LLM_INSTANCE", "ADMIN_ASSISTANT_EMBEDDING_INSTANCE"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "instances"
    for i, name in enumerate(("default", "lab1")):
        (root / name).mkdir(parents=True)
        inst.write_instance(root, inst.InstanceMeta(name=name, display_name=name, app_port=18001 + i))
    llm = httpx.MockTransport(lambda request: httpx.Response(200 if "good" in str(request.url) else 503, json={"data": []}))
    base = load_config(overrides_path=tmp_path / "missing.json")

    def loader(name):
        import dataclasses

        url = "http://good/v1" if name == "lab1" else "http://bad/v1"
        endpoint = dataclasses.replace(base.main_endpoints[0], base_url=url, model=f"{name}-model")
        return dataclasses.replace(base, main_endpoints=(endpoint,))

    app = FastAPI()
    app.include_router(
        admin_settings.build_router(
            require_login=lambda: "admin",
            require_csrf=lambda: None,
            instances_root=root,
            config_ini_path=DEFAULT_CONFIG_PATH,
            audit_log_path=tmp_path / "audit.log",
            client_ip=lambda _r: "127.0.0.1",
            config_loader=loader,
            list_instances=lambda: [{"name": n, "display_name": n, "kind": "app", "state": "stopped"} for n in ("default", "lab1")],
            llm_transport=llm,
        )
    )
    return {"client": TestClient(app), "file": settings_file, "tmp": tmp_path}


def _setting(data, key):
    return next(s for s in data["settings"] if f"{s['section']}.{s['key']}" == key)


def test_load_config_layers_admin_settings_between_ini_and_instance_overrides(tmp_path, monkeypatch):
    settings_file = tmp_path / "admin_settings.json"
    settings_file.write_text(
        json.dumps({"vector_db": {"ingest_workers": "3"}, "admin": {"assistant_llm_instance": "lab1"}, "llm": {"main_url": "ignored"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ADMIN_SETTINGS_PATH", str(settings_file))
    monkeypatch.delenv("VECTOR_DB_INGEST_WORKERS", raising=False)
    monkeypatch.delenv("ADMIN_ASSISTANT_LLM_INSTANCE", raising=False)
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({"vector_db": {"ingest_workers": "9"}}), encoding="utf-8")
    cfg = load_config(overrides_path=overrides)
    # インスタンス別の上書きは使わず、管理者設定の値になる。対象外のキー（[llm] main_url）は無視する。
    assert (cfg.vector_db_ingest_workers, cfg.admin_assistant_llm_instance) == (3, "lab1")
    assert cfg.main_endpoints == load_config(overrides_path=tmp_path / "missing.json").main_endpoints
    monkeypatch.setenv("VECTOR_DB_INGEST_WORKERS", "5")
    assert load_config(overrides_path=overrides).vector_db_ingest_workers == 5


@pytest.mark.parametrize("content", ["{broken", "[1, 2]", '{"vector_db": "3"}'])
def test_broken_admin_settings_file_is_ignored(env, content):
    # 手で編集して壊しても、全インスタンス・管理ツールが起動できなくならないよう config.ini の値で動く。
    env["file"].write_text(content, encoding="utf-8")
    assert load_config(overrides_path=env["tmp"] / "missing.json").vector_db_ingest_workers == 1
    client = env["client"]
    assert _setting(client.get("/api/admin-settings").json(), "vector_db.ingest_workers")["value"] == "1"
    # 画面で保存し直せば正しい内容で上書きされる。
    client.put("/api/admin-settings", json={"values": {"vector_db.ingest_workers": 2}}).raise_for_status()
    assert json.loads(env["file"].read_text(encoding="utf-8")) == {"vector_db": {"ingest_workers": "2"}}


def test_get_save_and_reset(env):
    client = env["client"]
    data = client.get("/api/admin-settings").json()
    workers = _setting(data, "vector_db.ingest_workers")
    assert (workers["value"], workers["source"], workers["default"]) == ("1", "config.ini", "1")
    assert [i["name"] for i in data["instances"]] == ["default", "lab1"] and data["instances"][1]["llm"][0]["model"] == "lab1-model"

    body = {"values": {"vector_db.ingest_workers": 4, "vector_db.ingest_processes": "0", "admin.assistant_llm_instance": "lab1"}}
    data = client.put("/api/admin-settings", json=body).json()
    assert (_setting(data, "vector_db.ingest_workers")["value"], _setting(data, "vector_db.ingest_workers")["source"]) == ("4", "dashboard")
    assert _setting(data, "admin.assistant_llm_instance")["value"] == "lab1"
    # config.ini と同じ値は上書きとして持たない。
    assert json.loads(env["file"].read_text(encoding="utf-8")) == {"admin": {"assistant_llm_instance": "lab1"}, "vector_db": {"ingest_workers": "4"}}
    assert admin_settings.current_admin_config(DEFAULT_CONFIG_PATH).vector_db_ingest_workers == 4
    entry = json.loads((env["tmp"] / "audit.log").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["action"] == "admin_settings_save" and {c["key"] for c in entry["changes"]} == {"ingest_workers", "assistant_llm_instance"}

    data = client.delete("/api/admin-settings").json()
    assert _setting(data, "vector_db.ingest_workers")["source"] == "config.ini" and not env["file"].exists()


@pytest.mark.parametrize(
    "values",
    [
        {"vector_db.ingest_workers": 0},
        {"vector_db.ingest_workers": "x"},
        {"vector_db.ingest_processes": -1},
        {"vector_db.qdrant_max_retries": -2},
        {"vector_db.qdrant_max_retries": "unlimited"},
        {"vector_db.qdrant_retry_interval_seconds": 0},
        {"vector_db.max_file_mb": "nan"},
        {"vector_db.office_timeout_seconds": "x"},
        {"vector_db.scan_workers": 0},
        {"vector_db.max_files_per_collection": 1.5},
        {"admin.assistant_llm_instance": "missing"},
    ],
)
def test_invalid_values_are_rejected(env, values):
    assert env["client"].put("/api/admin-settings", json={"values": values}).status_code == 400
    assert not env["file"].exists()


def test_qdrant_max_retries_accepts_unlimited(env, monkeypatch):
    monkeypatch.delenv("VECTOR_DB_QDRANT_MAX_RETRIES", raising=False)
    client = env["client"]
    s = _setting(client.get("/api/admin-settings").json(), "vector_db.qdrant_max_retries")
    assert (s["value"], s["default"], s["kind"]) == ("5", "5", "retry_count")
    data = client.put("/api/admin-settings", json={"values": {"vector_db.qdrant_max_retries": -1}}).json()
    assert _setting(data, "vector_db.qdrant_max_retries")["value"] == "-1"
    assert admin_settings.current_admin_config(DEFAULT_CONFIG_PATH).vector_db_qdrant_max_retries == -1


def test_shared_ingest_limits_are_saved(env, monkeypatch):
    names = ("VECTOR_DB_QDRANT_RETRY_INTERVAL_SECONDS", "VECTOR_DB_SCAN_WORKERS", "VECTOR_DB_MAX_FILES_PER_COLLECTION", "VECTOR_DB_MAX_FILE_MB", "VECTOR_DB_OFFICE_TIMEOUT_SECONDS")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    client = env["client"]
    s = _setting(client.get("/api/admin-settings").json(), "vector_db.qdrant_retry_interval_seconds")
    assert (s["value"], s["default"], s["kind"]) == ("10", "10", "positive_float")
    body = {
        "values": {
            "vector_db.qdrant_retry_interval_seconds": "2.5",
            "vector_db.scan_workers": 16,
            "vector_db.max_files_per_collection": "5000",
            "vector_db.max_file_mb": "50.0",  # config.ini と同じ値は上書きとして持たない
            "vector_db.office_timeout_seconds": 300,
        }
    }
    client.put("/api/admin-settings", json=body).raise_for_status()
    assert json.loads(env["file"].read_text(encoding="utf-8")) == {
        "vector_db": {"qdrant_retry_interval_seconds": "2.5", "scan_workers": "16", "max_files_per_collection": "5000", "office_timeout_seconds": "300"}
    }
    cfg = admin_settings.current_admin_config(DEFAULT_CONFIG_PATH)
    assert (
        cfg.vector_db_qdrant_retry_interval_seconds, cfg.vector_db_scan_workers, cfg.vector_db_max_files_per_collection,
        cfg.vector_db_max_file_mb, cfg.vector_db_office_timeout_seconds,
    ) == (2.5, 16, 5000, 50, 300)


def test_ingest_temp_dir_is_saved_as_written(env, monkeypatch):
    monkeypatch.delenv("VECTOR_DB_INGEST_TEMP_DIR", raising=False)
    client = env["client"]
    s = _setting(client.get("/api/admin-settings").json(), "vector_db.ingest_temp_dir")
    assert (s["value"], s["default"], s["kind"]) == ("", "", "dir")
    # 相対パスは解決した絶対パスではなく、書いたとおりに保存・表示する。
    data = client.put("/api/admin-settings", json={"values": {"vector_db.ingest_temp_dir": " work/tmp "}}).json()
    assert _setting(data, "vector_db.ingest_temp_dir")["value"] == "work/tmp"
    assert json.loads(env["file"].read_text(encoding="utf-8")) == {"vector_db": {"ingest_temp_dir": "work/tmp"}}
    assert admin_settings.current_admin_config(DEFAULT_CONFIG_PATH).vector_db_ingest_temp_dir.endswith("tmp")
    # 空に戻すと上書きとして持たない（システムの一時フォルダ）。
    client.put("/api/admin-settings", json={"values": {"vector_db.ingest_temp_dir": ""}}).raise_for_status()
    assert json.loads(env["file"].read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize("value", ["${common_data_dir}/tmp", "file"])
def test_invalid_ingest_temp_dir_is_rejected(env, value):
    (env["tmp"] / "file").write_text("x", encoding="utf-8")
    path = value if value.startswith("$") else str(env["tmp"] / value)
    assert env["client"].put("/api/admin-settings", json={"values": {"vector_db.ingest_temp_dir": path}}).status_code == 400
    assert not env["file"].exists()


def test_env_override_is_reported(env, monkeypatch):
    monkeypatch.setenv("VECTOR_DB_INGEST_WORKERS", "6")
    s = _setting(env["client"].get("/api/admin-settings").json(), "vector_db.ingest_workers")
    assert (s["value"], s["source"], s["env"]) == ("6", "env", "VECTOR_DB_INGEST_WORKERS")


def test_check_llm(env):
    client = env["client"]
    ok = client.post("/api/admin-settings/check-llm", json={"instance": "lab1"}).json()
    assert ok["ok"] and ok["endpoints"][0]["base_url"] == "http://good/v1"
    bad = client.post("/api/admin-settings/check-llm", json={"instance": "default"}).json()
    assert not bad["ok"] and bad["endpoints"][0]["detail"] == "HTTP 503"
    assert client.post("/api/admin-settings/check-llm", json={"instance": "missing"}).status_code == 404


def test_instance_overrides_reject_admin_setting_keys(tmp_path):
    from admin import overrides
    from admin.ini_catalog import parse_file

    for section, key in (("vector_db", "ingest_workers"), ("admin", "assistant_llm_instance")):
        with pytest.raises(overrides.ValidationError, match="管理者設定|admin settings"):
            overrides._build_new_data({}, {(section, key): "2"}, [], parse_file(DEFAULT_CONFIG_PATH))


def test_assistant_reads_llm_instance_on_every_request():
    from admin import assistant

    current = {"value": ""}
    app = FastAPI()
    app.include_router(
        assistant.build_router(
            require_login=lambda: "admin",
            require_csrf=lambda: None,
            admin_config=load_config(),
            catalog_keys=lambda name: [],
            config_view=lambda name: {"keys": []},
            list_instances=lambda: [],
            instance_kind=lambda name: "app",
            config_loader=lambda name: load_config(),
            index={},
            llm_instance_setting=lambda: current["value"],
        )
    )
    client = TestClient(app)
    assert client.get("/api/assistant/status").json()["llm_instance"] is None
    current["value"] = "lab1"
    assert client.get("/api/assistant/status").json()["llm_instance"] == "lab1"
