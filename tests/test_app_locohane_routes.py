"""app.py 独自の /locohane/* ルートのテスト（登録順・添付ファイルの配信のしかた）。

- GET /locohane/threads/generating が GET /locohane/threads/{thread_id} に吸われない
  （以前は後ろに登録していたため "generating" が thread_id として扱われ、常に 404 だった）。
- /locohane/elements/{id} は、ラスター画像だけを inline で返し、HTML・SVG 等はダウンロードにする
  （inline で返すと、LLM が作った HTML を開いたときにアプリと同じオリジンでスクリプトが動く）。
- /locohane/tutorial は、チャットのチュートリアルを終えたかをユーザー単位で返し、準備前は 503 にする。
"""

from __future__ import annotations

import pytest
from chainlit.auth import get_current_user
from chainlit.server import app as chainlit_app
from fastapi.testclient import TestClient

import app

pytestmark = pytest.mark.skipif(not app._config.thread_store_enabled, reason="[thread_store] is disabled")


@pytest.fixture(autouse=True)
def _anonymous(monkeypatch):
    chainlit_app.dependency_overrides[get_current_user] = lambda: None  # 匿名（所有者 "anonymous"）
    monkeypatch.setattr(app, "_generating_owner_threads", {})
    yield
    chainlit_app.dependency_overrides.pop(get_current_user, None)


def test_generating_route_is_not_shadowed_by_the_thread_route(monkeypatch):
    app._generating_owner_threads["anonymous"] = "t-busy"
    client = TestClient(chainlit_app)

    res = client.get("/locohane/threads/generating")
    assert res.status_code == 200 and res.json() == {"threadId": "t-busy"}
    res = client.get("/locohane/threads/generating", params={"exclude": "t-busy"})
    assert res.status_code == 200 and res.json() == {"threadId": None}


@pytest.mark.parametrize(
    ("name", "mime", "content", "inline"),
    [
        ("photo.png", "image/png", b"\x89PNG\r\n", True),
        ("page.html", "text/html", b"<script>alert(1)</script>", False),
        ("icon.svg", "image/svg+xml", b"<svg onload='alert(1)'/>", False),
    ],
)
def test_elements_are_inline_only_for_raster_images(monkeypatch, tmp_path, name, mime, content, inline):
    stored = tmp_path / f"el-1_{name}"
    stored.write_bytes(content)

    async def fake_get_element_row(conn, element_id, thread_id=None):
        return {"thread_id": "t1", "file_path": str(stored), "mime": mime, "data": {"name": name}}

    async def fake_get_thread_owner(conn, thread_id):
        return "anonymous"

    monkeypatch.setattr(app, "_thread_store_conn", object())
    monkeypatch.setattr(app.thread_store, "get_element_row", fake_get_element_row)
    monkeypatch.setattr(app.thread_store, "get_thread_owner", fake_get_thread_owner)

    res = TestClient(chainlit_app).get("/locohane/elements/el-1")

    assert res.status_code == 200 and res.content == content
    assert res.headers["x-content-type-options"] == "nosniff"
    disposition = res.headers.get("content-disposition", "")
    if inline:
        assert not disposition.startswith("attachment")
    else:
        assert disposition.startswith("attachment") and name in disposition


def test_chat_tutorial_done_is_kept_per_user_on_the_server(monkeypatch):
    """チャットのチュートリアルを終えた印はサーバーにユーザー単位で持つ（ブラウザ・PC が変わっても出ない）。"""
    store: dict[str, dict] = {}

    async def fake_get(conn, identifier):
        return dict(store.get(identifier, {}))

    async def fake_update(conn, identifier, values):
        store.setdefault(identifier, {}).update(values)

    monkeypatch.setattr(app.thread_store, "get_user_metadata", fake_get)
    monkeypatch.setattr(app.thread_store, "update_user_metadata", fake_update)
    client = TestClient(chainlit_app)

    # 準備前は「終えていない」と答えず 503（終えた人に出さないため、フロントは聞き直す）
    monkeypatch.setattr(app, "_thread_store_conn", None)
    assert client.get("/locohane/tutorial").status_code == 503

    monkeypatch.setattr(app, "_thread_store_conn", object())
    assert client.get("/locohane/tutorial").json() == {"done": False}
    assert client.post("/locohane/tutorial/done").json() == {"success": True}
    assert client.get("/locohane/tutorial").json() == {"done": True}
    assert set(store) == {"anonymous"}
