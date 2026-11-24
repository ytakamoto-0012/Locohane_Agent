"""3D ビューアのデータの受け取り口（GET /locohane/cad/data/{data_id}、app.py）のテスト。

ビューアを出している間（cad_viewer._VIEWER_PACKAGES にある間）、同じ所有者が生成中のスレッドの
パッケージだけを返し、それ以外は 404 にすることを確かめる。
"""

from __future__ import annotations

import pytest
from chainlit.auth import get_current_user
from chainlit.server import app as chainlit_app
from fastapi.testclient import TestClient

import app
from src.tools import cad_viewer


@pytest.fixture(autouse=True)
def _reset(tmp_path):
    cad_viewer._VIEWER_PACKAGES.clear()
    app._generating_owner_threads.clear()
    chainlit_app.dependency_overrides[get_current_user] = lambda: None  # 匿名（所有者 "anonymous"）
    path = tmp_path / "cad_package.lcvd"
    path.write_bytes(b"LCVD-test-bytes")
    cad_viewer._VIEWER_PACKAGES["d1"] = cad_viewer.ViewerPackage(path=path, thread_id="t1")
    yield path
    chainlit_app.dependency_overrides.pop(get_current_user, None)
    cad_viewer._VIEWER_PACKAGES.clear()
    app._generating_owner_threads.clear()


def _get(data_id: str = "d1"):
    return TestClient(chainlit_app).get(f"/locohane/cad/data/{data_id}")


def test_returns_the_package_while_the_thread_is_generating():
    app._generating_owner_threads["anonymous"] = "t1"
    res = _get()
    assert res.status_code == 200
    assert res.content == b"LCVD-test-bytes"
    assert res.headers["cache-control"] == "no-store"


def test_unknown_id_is_404():
    app._generating_owner_threads["anonymous"] = "t1"
    assert _get("nope").status_code == 404


def test_other_thread_or_not_generating_is_404():
    # 生成中でない（ビューアの答えを待つターンが終わった）。
    assert _get().status_code == 404
    # 同じ所有者が別のスレッドを生成中（他のスレッドのデータは返さない）。
    app._generating_owner_threads["anonymous"] = "t2"
    assert _get().status_code == 404


def test_removed_file_is_404(_reset):
    app._generating_owner_threads["anonymous"] = "t1"
    _reset.unlink()
    assert _get().status_code == 404
