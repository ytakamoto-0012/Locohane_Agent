"""show_cad_viewer ツール（src/tools/cad_viewer.py）のテスト。

ビューアの表示（cl.AskElementMessage）はスタブにし、ブラウザが返す選択を差し込んで、
引数の検査・計画の承認・サブプロセスでの形の処理・戻り値の整形を確かめる。
"""

from __future__ import annotations

import json
import struct
import sys
from types import SimpleNamespace

import numpy as np
import pytest

import src.tools as tools
from src import ask_relay
from src.tools import cad_viewer
from src.tools._ask_echo import ASK_TIMEOUT_MESSAGE

_THREAD = "cad-viewer-thread"
_CUBE_V = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float) * 10
_CUBE_QUADS = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [2, 3, 7, 6], [1, 2, 6, 5], [3, 0, 4, 7]]
CUBE = _CUBE_V[np.array([t for q in _CUBE_QUADS for t in ([q[0], q[1], q[2]], [q[0], q[2], q[3]])])]


class _FakeUserSession:
    def __init__(self) -> None:
        self.data = {"thread_id": _THREAD}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _Viewer:
    """表示されたビューア（props）を記録し、決めておいた応答を返す。"""

    def __init__(self) -> None:
        self.response: dict | None = {"submitted": True, "selected": []}
        self.shown: list[dict] = []
        self.echoed: list[str] = []
        self.removed = 0


_viewer = _Viewer()


class _FakeCustomElement:
    def __init__(self, name=None, props=None, **kwargs) -> None:
        self.name = name
        self.props = props

    async def remove(self) -> None:
        _viewer.removed += 1


class _FakeAskElement:
    def __init__(self, content=None, element=None, timeout=None, **kwargs) -> None:
        self.content = content
        self.element = element

    async def send(self):
        _viewer.shown.append({"name": self.element.name, **self.element.props})
        return _viewer.response

    async def update(self):
        pass


class _FakeMessage:
    def __init__(self, content=None, **kwargs) -> None:
        self.content = content

    async def send(self):
        _viewer.echoed.append(self.content)
        return self


@pytest.fixture
def env(monkeypatch, tmp_path):
    global _viewer
    _viewer = _Viewer()
    session = _FakeUserSession()
    ask_relay.pending_asks.pop(_THREAD, None)
    monkeypatch.setattr(tools.cl, "user_session", session)
    monkeypatch.setattr(tools.cl, "CustomElement", _FakeCustomElement)
    monkeypatch.setattr(tools.cl, "AskElementMessage", _FakeAskElement)
    monkeypatch.setattr(tools.cl, "Message", _FakeMessage)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", None)
    monkeypatch.setattr(tools._state, "_SCRIPT_PYTHON", sys.executable)
    monkeypatch.setattr(tools._state, "_SCRIPT_TIMEOUT", 120)
    monkeypatch.setattr(tools._state, "_CODE_EXEC_ENABLED", True)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", None)
    return session


def _cube_stl(tmp_path):
    path = tmp_path / "cube.stl"
    with path.open("wb") as f:
        f.write(b" " * 80)
        f.write(struct.pack("<I", len(CUBE)))
        for tri in CUBE:
            f.write(struct.pack("<3f", 0, 0, 0) + struct.pack("<9f", *tri.ravel()) + struct.pack("<H", 0))
    return path


async def _call(**kwargs) -> str:
    return await tools.show_cad_viewer.ainvoke({"question": "面を選んでください", **kwargs})


@pytest.mark.asyncio
async def test_needs_exactly_one_of_file_or_code(env, tmp_path) -> None:
    assert (await _call()).startswith("Error: give exactly one of file_path")
    out = await _call(file_path=str(_cube_stl(tmp_path)), code="result = 1")
    assert out.startswith("Error: give exactly one of file_path")
    assert _viewer.shown == []


@pytest.mark.asyncio
async def test_code_needs_an_approved_plan(env) -> None:
    out = await _call(code="import cadquery as cq\nresult = cq.Workplane().box(1, 1, 1)")
    assert out.startswith("Error: cannot run the CadQuery code because the plan is not approved")
    assert "approve_plan" in out
    assert _viewer.shown == []


@pytest.mark.asyncio
async def test_code_is_refused_when_code_execution_is_disabled(env, monkeypatch) -> None:
    monkeypatch.setattr(tools._state, "_CODE_EXEC_ENABLED", False)
    env.set("plan_approved", True)
    out = await _call(code="result = 1")
    assert "code_execution_enabled=false" in out


@pytest.mark.asyncio
async def test_unsupported_and_missing_files(env, tmp_path) -> None:
    obj = tmp_path / "a.obj"
    obj.write_text("v 0 0 0\n", encoding="ascii")
    assert "unsupported file type '.obj'" in await _call(file_path=str(obj))
    assert (await _call(file_path=str(tmp_path / "none.step"))).startswith("Error: the file does not exist")


@pytest.mark.asyncio
async def test_stl_selection_is_validated_and_returned_with_geometry(env, tmp_path) -> None:
    _viewer.response = {
        "submitted": True,
        "selected": [
            {"kind": "face", "id": 1},
            {"kind": "edge", "id": 0},
            {"kind": "face", "id": 1},  # 重複
            {"kind": "vertex", "id": 0},  # STL には頂点が無い
            {"kind": "face", "id": 999},  # 範囲外
            {"kind": "face", "id": True},  # 型違い
        ],
    }
    out = await _call(file_path=str(_cube_stl(tmp_path)))
    shown = _viewer.shown[0]
    assert shown["name"] == "CadViewer"
    assert shown["select"] == ["face", "edge"]
    assert shown["data"]["stats"]["faces"] == 6
    assert out.startswith("The user confirmed the selection in the 3D viewer: 2 element(s).")
    assert "Model: STL from" in out
    assert "planar regions of the STL mesh" in out
    face_line = next(line for line in out.splitlines() if line.startswith("- face 1: "))
    assert json.loads(face_line.removeprefix("- face 1: "))["geomType"] == "PLANE"
    assert any(line.startswith("- edge 0: ") for line in out.splitlines())
    assert "vertex" not in out.split("[selected]")[1]
    # 何を選んだかがチャットに残る。
    assert _viewer.echoed and "#1" in _viewer.echoed[0]
    # ビューア（数 MB になりうるデータを持つ要素）は答えを受け取ったら消す。
    assert _viewer.removed == 1


@pytest.mark.asyncio
async def test_select_limits_the_kinds(env, tmp_path) -> None:
    _viewer.response = {"submitted": True, "selected": [{"kind": "edge", "id": 0}]}
    out = await _call(file_path=str(_cube_stl(tmp_path)), select=["face"])
    assert _viewer.shown[0]["select"] == ["face"]
    assert out.startswith("The user confirmed with nothing selected.")


@pytest.mark.asyncio
async def test_cancel_and_timeout(env, tmp_path, monkeypatch) -> None:
    stl = _cube_stl(tmp_path)
    _viewer.response = {"submitted": False, "selected": []}
    assert await _call(file_path=str(stl)) == cad_viewer._CANCEL_MESSAGE
    # 応答が無い（タイムアウト）。待ち時間が無期限（既定の 0）だと中継側が待ち続けるので 1 秒にする。
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", SimpleNamespace(show_cad_viewer_timeout_seconds=1, bin_path=[]))
    _viewer.response = None
    assert await _call(file_path=str(stl)) == ASK_TIMEOUT_MESSAGE


@pytest.mark.asyncio
async def test_viewer_data_json_can_be_shown_again(env, tmp_path) -> None:
    await _call(file_path=str(_cube_stl(tmp_path)))
    first = _viewer.shown[0]["data"]
    data_files = list((tools._state._DEFAULT_WORKDIR).glob("_tmp_*/cad_view_*.json"))
    assert len(data_files) == 1
    out = await _call(file_path=str(data_files[0]))
    assert _viewer.shown[1]["data"] == first
    assert "Viewer data:" in out

    bad = tmp_path / "other.json"
    bad.write_text('{"a": 1}', encoding="utf-8")
    assert "is not viewer data made by show_cad_viewer" in await _call(file_path=str(bad))


@pytest.mark.asyncio
async def test_cadquery_code_runs_after_plan_approval(env) -> None:
    pytest.importorskip("cadquery")
    env.set("plan_approved", True)
    _viewer.response = {"submitted": True, "selected": [{"kind": "face", "id": 0}]}
    code = "import cadquery as cq\nresult = cq.Workplane().box(4, 2, 1)\ncq.exporters.export(result, 'made.step')\n"
    out = await _call(code=code)
    assert _viewer.shown[0]["select"] == ["face", "edge", "vertex"]
    assert _viewer.shown[0]["data"]["source"]["format"] == "CADQUERY"
    assert "IDs are 0-based in CadQuery order" in out
    # コードが書いたファイルは知らせ、ビューア用データ自体は数えない。
    assert "made.step" in out
    tail = out.split("[selected]")[1]
    assert "cad_view_" not in tail.split("\n", 2)[-1]


@pytest.mark.asyncio
async def test_cadquery_code_error_is_returned(env) -> None:
    pytest.importorskip("cadquery")
    env.set("plan_approved", True)
    out = await _call(code="raise RuntimeError('boom')\n")
    assert out.startswith("Error: making the 3D model failed (exit code 1).")
    assert "RuntimeError: boom" in out
    assert _viewer.shown == []
