"""取り込みワーカー（admin/ingest_worker.py。取り込み・コピーを子プロセスで動かす）のテスト。

子プロセスにはテスト用の httpx トランスポートを渡せないため、偽の埋め込みサーバーを HTTP で立てる。
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from _vector_db_fakes import FakeEmbeddingServer, make_config
from admin import ingest_worker
from admin import instances as inst
from admin import knowledge
from src.config import LLMEndpoint


@pytest.fixture
def embed_server():
    """FakeEmbeddingServer を HTTP で待ち受ける（delay 秒だけ応答を遅らせられる）。"""
    fake = FakeEmbeddingServer()
    state = {"delay": 0.0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(state["delay"])
            res = fake.handler(httpx.Request("POST", f"http://embed{self.path}", content=body))
            data = res.content
            self.send_response(res.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{server.server_address[1]}/v1", "fake": fake, "state": state}
    server.shutdown()


@pytest.fixture
def env(tmp_path, embed_server):
    root = tmp_path / "instances"
    (root / "org_a").mkdir(parents=True)
    inst.write_instance(root, inst.InstanceMeta(name="org_a", display_name="org_a", app_port=18001, kind=inst.KIND_APP))
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(6):
        (docs / f"doc{i}.md").write_text(f"溶接ビードの幅は{i}mm以上とする。" * 20, encoding="utf-8")
    cfg = make_config(
        tmp_path / "org_a",
        vector_db_backend="sqlite_vec",
        vector_db_embedding_endpoints=(LLMEndpoint(base_url=embed_server["url"], api_key="k", model="fake-embed"),),
        vector_db_embedding_retry_interval_seconds=0.1,
    )
    configs = {"org_a": cfg}
    app = FastAPI()
    router = knowledge.build_router(
        require_login=lambda: "admin",
        require_csrf=lambda: None,
        instances_root=root,
        audit_log_path=tmp_path / "audit.log",
        client_ip=lambda _r: "127.0.0.1",
        config_loader=lambda name: configs[name],
        ingest_limits=lambda: (1, 0, 0),
        ingest_in_worker_process=True,
    )
    app.include_router(router)
    client = TestClient(app)
    client.post("/api/instances/org_a/knowledge/collections", json={"name": "docs", "source_folder": str(docs)})
    yield {"client": client, "router": router, "docs": docs, "embed": embed_server, "configs": configs}
    router.ingest_worker.close()


def _wait_job(client, collection="docs", timeout=90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/instances/org_a/knowledge/collections/{collection}/job").json()["job"]
        if job and job["state"] not in knowledge.ACTIVE_STATES:
            return job
        time.sleep(0.1)
    raise AssertionError("取り込みが終わらない")


def test_ingest_runs_in_worker_process_and_reports_progress(env, caplog):
    client, router = env["client"], env["router"]
    with caplog.at_level(logging.INFO):
        assert client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={}).status_code == 200
        job = _wait_job(client)
    assert job["state"] == "done", job
    assert job["result"]["added"] == 6 and (job["done"], job["total"]) == (6, 6)
    assert job["bytes_done"] > 0
    # 子プロセスで動き（このプロセスには取り込みのスレッドが無い）、ログは管理ツールのログに流れる。
    proc = router.ingest_worker._proc
    assert proc is not None and proc.poll() is None
    assert any(r.threadName.startswith("ingest-worker/") for r in caplog.records)
    # 枠の状態は子プロセスから届いた写し。
    snapshot = router.ingest_slots.snapshot()
    assert snapshot["workers"] == 1 and snapshot["embed"] == [None]

    overview = client.get("/api/instances/org_a/knowledge").json()
    assert overview["collections"][0]["file_count"] == 6

    # 2回目は同じ子プロセスを使い回す（変更なし）。
    client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={})
    job = _wait_job(client)
    assert job["state"] == "done" and job["result"]["unchanged"] == 6
    assert router.ingest_worker._proc is proc


def test_stop_is_relayed_to_worker(env):
    client, embed = env["client"], env["embed"]
    embed["state"]["delay"] = 1.0
    client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={})
    time.sleep(1.5)
    assert client.post("/api/instances/org_a/knowledge/collections/docs/stop").status_code == 200
    job = _wait_job(client, timeout=30)
    assert job["state"] == "stopped", job
    assert job["result"]["added"] < 6


def test_worker_crash_fails_running_job_and_restarts(env):
    client, router, embed = env["client"], env["router"], env["embed"]
    embed["state"]["delay"] = 2.0
    client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={})
    deadline = time.monotonic() + 60
    while router.ingest_worker._proc is None and time.monotonic() < deadline:
        time.sleep(0.1)
    time.sleep(1.0)
    router.ingest_worker._proc.kill()
    job = _wait_job(client, timeout=30)
    assert job["state"] == "error" and "取り込みワーカーのプロセスが異常終了しました" in job["error"]

    # 次の取り込みで子プロセスを起動し直して続きから取り込む。
    embed["state"]["delay"] = 0.0
    client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={})
    job = _wait_job(client)
    assert job["state"] == "done", job
    assert router.ingest_worker._proc.poll() is None


def test_worker_reports_expected_errors_as_messages(env):
    """取り込み元フォルダが見えない等の想定内のエラー（IngestError）は、型名を付けずにそのままの文言でジョブに出す。"""
    client, docs = env["client"], env["docs"]
    shutil.rmtree(docs)
    client.post("/api/instances/org_a/knowledge/collections/docs/ingest", json={})
    job = _wait_job(client)
    assert job["state"] == "error", job
    assert str(docs) in job["error"] and not job["error"].startswith(("WorkerError", "IngestError"))


def test_client_requires_worker_environment():
    """子プロセスを直接起動したら、使い方を出して終わる。"""
    import subprocess
    import sys

    res = subprocess.run([sys.executable, "-m", "admin.ingest_worker"], cwd=str(ingest_worker.PROJECT_ROOT), capture_output=True, text=True)
    assert res.returncode != 0 and "admin.server" in res.stderr


def test_idle_snapshot_follows_resize():
    client = ingest_worker.IngestWorkerClient(2, 0, 3)
    assert client.slots.snapshot()["requests"] == 3 and len(client.slots.snapshot()["load"]) == 2
    client.slots.resize(4, 0, 0)
    assert len(client.slots.snapshot()["embed"]) == 4
    json.dumps(client.slots.snapshot())
