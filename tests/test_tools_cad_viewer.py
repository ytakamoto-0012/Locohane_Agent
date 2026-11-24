"""show_cad_viewer ツール（src/tools/cad_viewer.py）のテスト。

ビューアの表示（cl.AskElementMessage）はスタブにし、ブラウザが返す選択を差し込んで、
引数の検査・計画の承認・サブプロセスでの形の処理・戻り値の整形を確かめる。
"""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path
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
        # 出してから消した一時的なメッセージ（「準備しています」）。
        self.notices: list[str] = []
        # 表示中に呼ぶ処理（「保存」ボタンの代わり）。props を受け取る。
        self.on_show = None
        self.on_show_result = None


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
        if _viewer.on_show is not None:
            _viewer.on_show_result = await _viewer.on_show(self.element.props)
        return _viewer.response

    async def update(self):
        pass


class _FakeMessage:
    def __init__(self, content=None, **kwargs) -> None:
        self.content = content

    async def send(self):
        _viewer.echoed.append(self.content)
        return self

    async def remove(self):
        # 「準備しています」の一時的なメッセージは、ビューアを出す前に消す。
        _viewer.echoed.remove(self.content)
        _viewer.notices.append(self.content)


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
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    shown = _viewer.shown[0]
    assert shown["name"] == "CadViewer"
    assert shown["select"] == ["face", "edge"]
    assert shown["data"]["stats"]["faces"] == 6
    assert out["status"] == "confirmed"
    assert out["selected_count"] == 2
    assert out["selected_by_kind"] == {"face": 1, "edge": 1}
    assert out["model"]["format"] == "STL"
    assert out["model"]["counts"]["faces"] == 6
    assert "planar regions of the STL mesh" in out["id_note"]
    # 三角形の数を理由に「読めない」と断らせず、戻り値に無い値は explore サブエージェントへ委譲させる。
    assert "never tell the user that the STL or its data cannot be read" in out["id_note"]
    assert "agent_type explore" in out["id_note"] and "json_query" in out["id_note"]
    # 選んだ要素の一覧（ID も）は戻り値に入れず、選択結果のファイル（@N）にだけ書く。
    assert "selected" not in out
    assert "PLANE" not in json.dumps(out)
    assert out["selection_file"] in out["how_to_read"]
    sel_files = list(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_selection_*.json"))
    assert len(sel_files) == 1
    # このテストでは @N を登録しない（_PATH_MEMORY_DIR=None）ので、パスそのものが返る。
    assert out["selection_file"] == str(sel_files[0])
    detail = json.loads(sel_files[0].read_text(encoding="utf-8"))
    assert [(s["kind"], s["id"]) for s in detail["selected"]] == [("face", 1), ("edge", 0)]
    assert detail["selected"][0]["geomType"] == "PLANE" and "center" in detail["selected"][0]
    assert detail["viewer_data"] == out["viewer_data"]
    # チャットには種類ごとの件数だけ残す（一つずつの ID は選択結果の JSON にある）。
    assert _viewer.echoed and "#" not in _viewer.echoed[0]
    assert "1" in _viewer.echoed[0]
    # ビューア（数 MB になりうるデータを持つ要素）は答えを受け取ったら消す。
    assert _viewer.removed == 1
    # 形を読んでいる間は「準備しています」と出し、ビューアを出す前に消す。
    assert len(_viewer.notices) == 1


@pytest.mark.asyncio
async def test_same_file_is_shown_again_from_the_cache(env, tmp_path, monkeypatch) -> None:
    _viewer.response = {"submitted": True, "selected": []}
    stl = _cube_stl(tmp_path)
    await _call(file_path=str(stl))
    cache_dir = tools._state._DEFAULT_WORKDIR.parent / "cad_cache"
    assert len(list(cache_dir.glob("*.json"))) == 1

    real_run = cad_viewer._run

    # 2 回目は形を作り直さない（サブプロセスを動かさない）。会話の作業フォルダには別のファイルとして写す。
    async def _no_run(cmd, workdir):
        raise AssertionError("run_cad should not run on a cache hit")

    monkeypatch.setattr(cad_viewer, "_run", _no_run)
    out = json.loads(await _call(file_path=str(stl)))
    assert _viewer.shown[1]["data"]["stats"]["faces"] == 6
    assert Path(out["viewer_data"]).parent != cache_dir
    assert len(list(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_view_*.json"))) == 2

    # ファイルが変われば作り直す。
    st = stl.stat()
    os.utime(stl, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    ran: list[str] = []

    async def _count_run(cmd, workdir):
        ran.append(cmd[1])
        return await real_run(cmd, workdir)

    monkeypatch.setattr(cad_viewer, "_run", _count_run)
    await _call(file_path=str(stl))
    assert len(ran) == 1


@pytest.mark.asyncio
async def test_cache_is_not_used_when_disabled(env, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cad_viewer, "_cache_settings", lambda: (tmp_path / "cad_cache", 0))
    _viewer.response = {"submitted": True, "selected": []}
    await _call(file_path=str(_cube_stl(tmp_path)))
    assert not (tmp_path / "cad_cache").exists()


@pytest.mark.asyncio
async def test_select_limits_the_kinds(env, tmp_path) -> None:
    _viewer.response = {"submitted": True, "selected": [{"kind": "edge", "id": 0}]}
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path)), select=["face"]))
    assert _viewer.shown[0]["select"] == ["face"]
    assert out["selected_count"] == 0 and out["selected_by_kind"] == {}
    # 何も選ばなければ選択結果のファイルは作らない。
    assert "selection_file" not in out
    assert not list(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_selection_*.json"))


@pytest.mark.asyncio
async def test_return_value_size_does_not_grow_with_the_selection(env, tmp_path) -> None:
    _viewer.response = {"submitted": True, "selected": [{"kind": "face", "id": 0}]}
    one = await _call(file_path=str(_cube_stl(tmp_path)))
    _viewer.response = {
        "submitted": True,
        "selected": [{"kind": "face", "id": i} for i in range(6)] + [{"kind": "edge", "id": i} for i in range(12)],
        "colors": {"faces": {str(i): "#ff0000" for i in range(6)}, "parts": {}},
    }
    many = await _call(file_path=str(_cube_stl(tmp_path)))
    out = json.loads(many)
    assert out["selected_count"] == 18 and out["selected_by_kind"] == {"face": 6, "edge": 12}
    assert out["color_change_count"] == 6 and "color_changes" not in out
    # 要素の数が増えても、戻り値は件数・説明の分しか増えない（要素ごとの項目を持たない）。
    assert len(many) - len(one) < 600
    sel_file = max(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_selection_*.json"), key=lambda p: p.stat().st_mtime_ns)
    detail = json.loads(sel_file.read_text(encoding="utf-8"))
    assert len(detail["selected"]) == 18 and len(detail["color_changes"]) == 6


@pytest.mark.asyncio
async def test_color_changes_are_validated_and_written(env, tmp_path) -> None:
    _viewer.response = {
        "submitted": True,
        "selected": [],
        "colors": {
            "faces": {"2": "#FF0000", "3": "red", "999": "#00ff00", "x": "#00ff00"},
            "parts": {"0": "#00ff00"},  # STL には部品が無い
        },
    }
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    assert out["selected_count"] == 0
    assert out["color_change_count"] == 1
    assert "color_changes" not in out
    # 選んだ要素が無くても、色の変更はファイルに書く。
    sel_file = next(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_selection_*.json"))
    assert json.loads(sel_file.read_text(encoding="utf-8"))["color_changes"] == [{"kind": "face", "id": 2, "from": None, "to": "#ff0000"}]
    assert "1" in _viewer.echoed[0]


@pytest.mark.asyncio
async def test_save_button_writes_the_edited_view(env, tmp_path) -> None:
    payload = {"colors": {"faces": {"1": "#00ff00"}, "parts": {}}, "hidden_parts": [5, "x"], "visible": {"edge": False}}

    async def press_save(props):
        first = await cad_viewer.save_viewer_edits({"save_id": props["save_id"], **payload})
        # 同じビューアでもう一度保存すると、同じファイルを上書きする。
        second = await cad_viewer.save_viewer_edits({"save_id": props["save_id"], **payload})
        return first, second

    _viewer.on_show = press_save
    _viewer.response = {"submitted": True, "selected": [{"kind": "face", "id": 1}], **payload}
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    first, second = _viewer.on_show_result
    assert first["ok"] and second["ok"] and first["path"] == second["path"]
    # 「保存」は 3D モデル（STL なら色付き STL）を書き出す。
    saved = list(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cube_edited_*.stl"))
    assert len(saved) == 1 and str(saved[0]) == first["path"]
    assert any(first["token"] in n for n in out["notes"])
    from src.cad.stl_reader import read_stl

    stl = read_stl(saved[0])
    assert len(stl.triangles) == 12 and stl.color_format == "viscam"
    assert int(stl.colored.sum()) == 2 and tuple(stl.colors[stl.colored][0]) == (0, 255, 0)
    # 書き出したモデルはそのまま開き直せて、変えた色が付いている。
    _viewer.on_show = None
    _viewer.response = {"submitted": True, "selected": []}
    await _call(file_path=str(saved[0]))
    assert [f["color"] for f in _viewer.shown[-1]["data"]["faces"] if f.get("color")] == ["#00ff00"]
    # 色・表示を変えて確定したので、変更を反映したビューア用データが自動で保存され、viewer_data になっている。
    auto = list(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_view_edited_*.json"))
    assert len(auto) == 1 and out["viewer_data"] == str(auto[0])
    data = json.loads(auto[0].read_text(encoding="utf-8"))
    assert data["faces"][1]["color"] == "#00ff00"
    assert data["viewState"] == {"hiddenParts": [], "visible": {"edge": False}}  # STL に部品 5 は無い
    # 書き出しの指示に使った JSON は残さない。
    assert not list(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_export_*.json"))
    # ビューアを閉じた後の保存は受け付けない。
    assert not (await cad_viewer.save_viewer_edits({"save_id": _viewer.shown[0]["save_id"]}))["ok"]
    assert not (await cad_viewer.save_viewer_edits({"save_id": "unknown"}))["ok"]


@pytest.mark.asyncio
async def test_instruction_is_returned_with_the_selection(env, tmp_path) -> None:
    _viewer.response = {"submitted": True, "selected": [{"kind": "face", "id": 1}], "message": "  この面に穴を開けて \n"}
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    assert list(out)[:2] == ["status", "user_instruction"]
    assert out["user_instruction"] == "この面に穴を開けて"
    assert out["selected_count"] == 1
    # チャットにも指示が残る。
    assert "この面に穴を開けて" in _viewer.echoed[0]

    assert "next_step" not in out

    _viewer.response = {"submitted": True, "selected": [], "message": 123}
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    assert "user_instruction" not in out


def _png_data_url() -> str:
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (200, 30, 30)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


async def _call_with_artifact(**kwargs):
    """ToolCall の形で呼ぶ（content_and_artifact の artifact も受け取る）。"""
    msg = await tools.show_cad_viewer.ainvoke(
        {"type": "tool_call", "id": "call_1", "name": "show_cad_viewer", "args": {"question": "面を選んでください", **kwargs}}
    )
    return msg.content, msg.artifact


@pytest.mark.asyncio
async def test_screenshot_is_saved_and_shown_with_an_instruction(env, tmp_path) -> None:
    _viewer.response = {"submitted": True, "selected": [{"kind": "face", "id": 1}], "message": "この面に穴を開けて", "screenshot": _png_data_url()}
    content, artifact = await _call_with_artifact(file_path=str(_cube_stl(tmp_path)))
    out = json.loads(content)
    # ビューアには撮る画面の大きさを渡す。
    assert _viewer.shown[0]["screenshot_long_side"] == 512
    shots = list(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_screenshot_*.png"))
    assert len(shots) == 1
    assert shots[0].read_bytes().startswith(b"\x89PNG")
    assert out["screenshot"] == str(shots[0])
    assert artifact["image_url"].startswith("data:image/png;base64,")
    assert str(shots[0]) in artifact["ref"]


@pytest.mark.asyncio
async def test_screenshot_is_ignored_without_an_instruction_or_when_invalid(env, tmp_path) -> None:
    stl = _cube_stl(tmp_path)
    # 指示が無ければ、画面が届いても保存・表示しない。
    _viewer.response = {"submitted": True, "selected": [{"kind": "face", "id": 1}], "message": " ", "screenshot": _png_data_url()}
    content, artifact = await _call_with_artifact(file_path=str(stl))
    assert artifact is None and "screenshot" not in json.loads(content)
    # PNG でないものは受け付けない（選択と指示はそのまま返す）。
    _viewer.response = {"submitted": True, "selected": [], "message": "見て", "screenshot": "data:image/png;base64,QUJD"}
    content, artifact = await _call_with_artifact(file_path=str(stl))
    out = json.loads(content)
    assert artifact is None and "screenshot" not in out and out["user_instruction"] == "見て"
    assert not list(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_screenshot_*.png"))


@pytest.mark.asyncio
async def test_no_instruction_tells_the_llm_to_just_report(env, tmp_path) -> None:
    # 指示欄が空のまま確定したら、詳細を読みに行かず簡潔に報告するだけにさせる。
    _viewer.response = {"submitted": True, "selected": [{"kind": "face", "id": 1}], "message": "  "}
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    assert list(out)[:2] == ["status", "next_step"]
    assert "do not dispatch agents" in out["next_step"] and "do not read" in out["next_step"]
    assert out["how_to_read"].startswith("Only when the task needs the details")


@pytest.mark.asyncio
async def test_save_shows_a_download_button(env, tmp_path, monkeypatch) -> None:
    sent: list[dict] = []

    class _Msg(_FakeMessage):
        def __init__(self, content=None, **kwargs) -> None:
            super().__init__(content, **kwargs)
            sent.append({"content": content, **kwargs})

    monkeypatch.setattr(tools.cl, "Message", _Msg)
    monkeypatch.setattr(tools.cl, "File", lambda name=None, path=None, **kw: SimpleNamespace(name=name, path=path))

    async def press_save(props):
        await cad_viewer.save_viewer_edits({"save_id": props["save_id"], "colors": {"faces": {"1": "#00ff00"}}})
        await cad_viewer.save_viewer_edits({"save_id": props["save_id"], "colors": {"faces": {"1": "#0000ff"}}})

    _viewer.on_show = press_save
    await _call(file_path=str(_cube_stl(tmp_path)))
    downloads = [m for m in sent if m.get("elements")]
    # 保存するたびに（上書き後の中身で）書き出したモデルのダウンロードボタンを出す。
    assert len(downloads) == 2
    saved = next(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cube_edited_*.stl"))
    assert all(m["elements"][0].path == str(saved) for m in downloads)


@pytest.mark.asyncio
async def test_no_edits_saves_no_viewer_data(env, tmp_path) -> None:
    _viewer.response = {"submitted": True, "selected": [], "hidden_parts": [], "visible": {"face": True, "edge": True, "vertex": True}}
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    assert "cad_view_edited" not in out["viewer_data"]
    assert not list(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_view_edited_*.json"))


@pytest.mark.asyncio
async def test_closing_with_edits_still_saves_the_viewer_data(env, tmp_path) -> None:
    _viewer.response = {"submitted": False, "selected": [], "colors": {"faces": {"0": "#123456"}, "parts": {}}}
    out = await _call(file_path=str(_cube_stl(tmp_path)))
    assert out.startswith("Error: the user closed the 3D viewer")
    auto = next(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_view_edited_*.json"))
    assert str(auto) in out


@pytest.mark.asyncio
async def test_save_exports_a_step_with_part_and_face_colors(env, tmp_path) -> None:
    cq = pytest.importorskip("cadquery")
    assy = cq.Assembly(name="asm")
    assy.add(cq.Workplane().box(10, 10, 10), name="a", color=cq.Color(1, 0, 0, 1))
    assy.add(cq.Workplane().box(10, 10, 10), name="b", loc=cq.Location((20, 0, 0)), color=cq.Color(0, 0, 1, 1))
    assy.add(cq.Workplane().sphere(3), name="c", loc=cq.Location((0, 20, 0)))
    src = tmp_path / "asm.step"
    assy.export(str(src))

    async def press_save(props):
        parts = props["data"]["parts"]
        face_of_b = parts[1]["faces"][0]
        colors = {"faces": {str(face_of_b): "#00ff00"}, "parts": {"0": "#ffff00"}}
        return face_of_b, await cad_viewer.save_viewer_edits({"save_id": props["save_id"], "colors": colors, "hidden_parts": [2]})

    _viewer.on_show = press_save
    await _call(file_path=str(src))
    face_of_b, res = _viewer.on_show_result
    assert res["ok"], res
    assert res["path"].endswith(".step")
    from src.cad.export_mesh import Options, cad_file_to_viewer_data

    data = cad_file_to_viewer_data(res["path"], "STEP", Options())
    # 隠した部品 c は含めず、部品の名前・色と面の色が残る。
    assert [(p["name"], p.get("color")) for p in data["parts"]] == [("a", "#ffff00"), ("b", "#0000ff")]
    assert data["faces"][face_of_b]["color"] == "#00ff00"
    assert sum(1 for f in data["faces"] if f.get("color") == "#00ff00") == 1


@pytest.mark.asyncio
async def test_save_exports_a_step_from_cadquery_code(env) -> None:
    pytest.importorskip("cadquery")
    env.set("plan_approved", True)

    async def press_save(props):
        return await cad_viewer.save_viewer_edits({"save_id": props["save_id"], "colors": {"faces": {"0": "#ff0000"}}})

    _viewer.on_show = press_save
    out = json.loads(await _call(code="import cadquery as cq\nresult = cq.Workplane().box(4, 5, 6)"))
    res = _viewer.on_show_result
    assert res["ok"], res
    assert "cadquery_edited_" in res["path"] and res["path"].endswith(".step")
    # 書き出し用に残した BREP は、コードが作ったファイルとして登録しない。
    assert ".brep" not in json.dumps(out.get("notes", []))
    from src.cad.export_mesh import Options, cad_file_to_viewer_data

    data = cad_file_to_viewer_data(res["path"], "STEP", Options())
    assert data["stats"]["faces"] == 6
    assert data["faces"][0]["color"] == "#ff0000" and sum(1 for f in data["faces"] if f.get("color")) == 1


@pytest.mark.asyncio
async def test_save_exports_a_dxf_with_layer_changes(env, tmp_path) -> None:
    ezdxf = pytest.importorskip("ezdxf")
    pytest.importorskip("cadquery")
    doc = ezdxf.new()
    doc.layers.add("L1", color=1)
    doc.layers.add("L2", color=3)
    doc.modelspace().add_line((0, 0), (10, 0), dxfattribs={"layer": "L1"})
    doc.modelspace().add_circle((0, 0), 5, dxfattribs={"layer": "L2"})
    src = tmp_path / "plan.dxf"
    doc.saveas(src)

    async def press_save(props):
        names = [p["name"] for p in props["data"]["parts"]]
        colors = {"faces": {}, "parts": {str(names.index("L1")): "#123456"}}
        return await cad_viewer.save_viewer_edits({"save_id": props["save_id"], "colors": colors, "hidden_parts": [names.index("L2")]})

    _viewer.on_show = press_save
    await _call(file_path=str(src))
    res = _viewer.on_show_result
    assert res["ok"], res
    out = ezdxf.readfile(res["path"])
    assert out.layers.get("L1").rgb == (0x12, 0x34, 0x56)
    assert out.layers.get("L2").is_off()


@pytest.mark.asyncio
async def test_undo_history_is_saved_and_colors_can_be_cleared(env, tmp_path) -> None:
    """自動保存したデータに「元に戻す」の履歴を入れ、開き直したビューアが色なしの元の状態へ戻せる（色の null）。"""
    start = {"colors": {"faces": {}, "parts": {}}, "hiddenParts": [], "visible": {"face": True, "edge": True, "vertex": True}, "attributes": {}}
    history = {"past": [start], "future": [], "base": {"faces": {"1": None, "999": "#000000"}, "parts": {}, "attributes": {}}}
    _viewer.response = {"submitted": True, "selected": [], "colors": {"faces": {"1": "#00ff00"}, "parts": {}}, "history": history}
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    saved = json.loads(Path(out["viewer_data"]).read_text(encoding="utf-8"))
    assert saved["faces"][1]["color"] == "#00ff00"
    # 範囲外の面は捨てる。
    assert saved["viewState"]["history"] == {"past": [start], "future": [], "base": {"faces": {"1": None}, "parts": {}, "attributes": {}}}

    # 開き直したビューアで「元に戻す」して確定 → 面 1 の色を外す（to: null）。
    _viewer.response = {"submitted": True, "selected": [], "colors": {"faces": {"1": None, "2": None}, "parts": {}}}
    out2 = json.loads(await _call(file_path=out["viewer_data"]))
    assert out2["color_change_count"] == 1  # 面 2 は元から色が無いので変更ではない
    sel = json.loads(Path(out2["selection_file"]).read_text(encoding="utf-8"))
    assert sel["color_changes"] == [{"kind": "face", "id": 1, "from": "#00ff00", "to": None}]
    again = json.loads(Path(out2["viewer_data"]).read_text(encoding="utf-8"))
    assert again["faces"][1]["color"] is None
    # 戻れる時点が無ければ（ブラウザが送らなければ）履歴は残さない。形のおかしい履歴も捨てる。
    assert "history" not in again["viewState"]
    _viewer.response = {"submitted": True, "selected": [], "colors": {"faces": {"3": "#ff0000"}}, "history": {"past": "x", "future": [], "base": {}}}
    out3 = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    assert "history" not in json.loads(Path(out3["viewer_data"]).read_text(encoding="utf-8"))["viewState"]


@pytest.mark.asyncio
async def test_measurements_are_validated_and_returned(env, tmp_path) -> None:
    from src import i18n

    _viewer.response = {
        "submitted": True,
        "selected": [],
        "measurements": [
            {
                "type": "distance",
                "distance": 10.00000004,
                "delta": [10, 0, 0],
                "points": [[0, 0, 0], [10, 0, 0]],
                "items": [{"kind": "face", "id": 0}, {"kind": "face", "id": 999}],
                "approximate": False,
                "extra": "x" * 1000,  # 種類ごとに決めた項目以外は捨てる
            },
            {"type": "plane_distance", "distance": 10, "direction": [0, 0, 1], "items": [{"kind": "face", "id": 0}, {"kind": "face", "id": 1}]},
            {"type": "arc3", "radius": 5, "diameter": 10, "center": [1, 2], "points": [[0, 0, 0]] * 3, "approximate": "yes"},
            {"type": "radius", "radius": "5"},  # 値が数でない
            {"type": "volume", "value": 1},  # 知らない種類
            "distance",
        ],
    }
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    ms = out["measurements"]
    assert [m["type"] for m in ms] == ["distance", "plane_distance", "arc3"]
    assert ms[0] == {
        "type": "distance",
        "distance": 10.0,
        "delta": [10.0, 0.0, 0.0],
        "points": [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]],
        "items": [{"kind": "face", "id": 0}, None],
        "approximate": False,
    }
    # 形の合わない中心・真偽値でない approximate は付けない。
    assert "center" not in ms[2] and "approximate" not in ms[2] and len(ms[2]["points"]) == 3
    assert "model units" in out["measurement_note"]
    assert i18n.t("cad.measurements", count=3) in _viewer.echoed[0]

    # 上限を超えた分は受け取らない。
    _viewer.response = {"submitted": True, "selected": [], "measurements": [{"type": "plane_angle", "angle_deg": 30}] * 80}
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    assert len(out["measurements"]) == cad_viewer._MEASUREMENTS_MAX
    _viewer.response = {"submitted": True, "selected": []}
    assert "measurements" not in json.loads(await _call(file_path=str(_cube_stl(tmp_path))))


@pytest.mark.asyncio
async def test_echo_lists_counts_not_elements(env, tmp_path) -> None:
    from src import i18n

    _viewer.response = {
        "submitted": True,
        "selected": [{"kind": "face", "id": i} for i in range(5)] + [{"kind": "edge", "id": 0}],
        "message": "選択した要素の色を赤に変更して",
    }
    await _call(file_path=str(_cube_stl(tmp_path)))
    echo = _viewer.echoed[0]
    face = i18n.t("cad.count", kind=i18n.t("cad.kind.face"), count=5)
    edge = i18n.t("cad.count", kind=i18n.t("cad.kind.edge"), count=1)
    assert echo.startswith(i18n.t("cad.selected_summary", items=i18n.t("list.separator").join([face, edge])))
    assert "#" not in echo and "PLANE" not in echo
    assert echo.endswith("選択した要素の色を赤に変更して")


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
    out = json.loads(await _call(file_path=str(data_files[0])))
    assert _viewer.shown[1]["data"] == first
    assert out["viewer_data"]

    bad = tmp_path / "other.json"
    bad.write_text('{"a": 1}', encoding="utf-8")
    assert "is not viewer data made by show_cad_viewer" in await _call(file_path=str(bad))


@pytest.mark.asyncio
async def test_cadquery_code_runs_after_plan_approval(env) -> None:
    pytest.importorskip("cadquery")
    env.set("plan_approved", True)
    _viewer.response = {"submitted": True, "selected": [{"kind": "face", "id": 0}]}
    code = "import cadquery as cq\nresult = cq.Workplane().box(4, 2, 1)\ncq.exporters.export(result, 'made.step')\n"
    out = json.loads(await _call(code=code))
    assert _viewer.shown[0]["select"] == ["face", "edge", "vertex"]
    assert _viewer.shown[0]["data"]["source"]["format"] == "CADQUERY"
    assert "IDs are 0-based in CadQuery order" in out["id_note"]
    # コードが書いたファイルは知らせ、ビューア用データ自体は数えない。
    notes = "\n".join(out["notes"])
    assert "made.step" in notes
    assert "cad_view_" not in notes


@pytest.mark.asyncio
async def test_cadquery_code_error_is_returned(env) -> None:
    pytest.importorskip("cadquery")
    env.set("plan_approved", True)
    out = await _call(code="raise RuntimeError('boom')\n")
    assert out.startswith("Error: making the 3D model failed (exit code 1).")
    assert "RuntimeError: boom" in out
    assert _viewer.shown == []


@pytest.mark.asyncio
async def test_screenshot_can_be_turned_off(env, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", SimpleNamespace(cad_viewer_screenshot_enabled=False, bin_path=[]))
    _viewer.response = {"submitted": True, "selected": [{"kind": "face", "id": 1}], "message": "この面に穴を開けて", "screenshot": _png_data_url()}
    content, artifact = await _call_with_artifact(file_path=str(_cube_stl(tmp_path)))
    assert _viewer.shown[0]["screenshot_long_side"] == 0
    assert artifact is None and "screenshot" not in json.loads(content)
    assert not list(tools._state._DEFAULT_WORKDIR.glob("_tmp_*/cad_screenshot_*.png"))


@pytest.mark.asyncio
async def test_compare_with_rejects_mesh_files_and_missing_files(env, tmp_path) -> None:
    stl = str(_cube_stl(tmp_path))
    assert "STL and DXF cannot be compared" in await _call(file_path=stl, compare_with=stl)
    assert (await _call(file_path=stl, compare_with=str(tmp_path / "none.step"))).startswith("Error: compare_with: the file does not exist")
    assert _viewer.shown == []


@pytest.mark.asyncio
async def test_model_check_features_and_diff_are_returned(env, tmp_path) -> None:
    cq = pytest.importorskip("cadquery")
    old = tmp_path / "old.step"
    new = tmp_path / "new.step"
    plate = cq.Workplane().box(40, 30, 10)
    cq.exporters.export(plate, str(old))
    cq.exporters.export(plate.faces(">Z").workplane().hole(6), str(new))
    _viewer.response = {"submitted": True, "selected": []}
    out = json.loads(await _call(file_path=str(new), compare_with=str(old)))
    model = out["model"]
    assert model["check"]["solids"] == 1 and model["check"]["valid"] is True
    assert model["features"]["holes"] == 1 and model["features"]["groups"][0]["through"] is True
    assert len(model["digest"]) == 16
    assert model["step"]["ap"] in ("AP214", "AP242")
    diff = out["diff"]
    assert diff["volume"]["removed"] == pytest.approx(3.14159265 * 9 * 10, rel=1e-3)
    assert diff["features"]["holes"] == {"before": 0, "after": 1}
    assert diff["overlay"].startswith("shown in the viewer")
    # 重ね表示のメッシュはビューアにだけ渡し、LLM への戻り値には入れない。
    assert "removed" in _viewer.shown[0]["data"]["diff"] and "removed" not in diff


@pytest.mark.asyncio
async def test_attribute_edits_are_validated_and_reported(env, tmp_path) -> None:
    pytest.importorskip("cadquery")
    from src.cad import step_p21

    cq = pytest.importorskip("cadquery")
    src = tmp_path / "plate.step"
    cq.exporters.export(cq.Workplane().box(10, 10, 2), str(src))
    text, enc = step_p21.read_text(src)
    name = step_p21.read_products(text)[0]["name"]
    text, _n = step_p21.add_attributes(text, {name: [{"name": "品番", "value": "PN-1"}]})
    src.write_bytes(text.encode(enc))
    _viewer.response = {
        "submitted": True,
        "selected": [],
        "attributes": {name: [{"name": "品番", "value": "PN-2"}, {"name": "表面処理", "value": "黒染め"}, {"name": "", "value": "x"}], "unknown": [{"name": "a", "value": 1}]},
    }
    out = json.loads(await _call(file_path=str(src)))
    assert out["attribute_change_count"] == 2
    sel = json.loads(open(out["selection_file"], encoding="utf-8").read())
    assert sel["attribute_changes"] == [
        {"product": name, "name": "品番", "from": "PN-1", "to": "PN-2"},
        {"product": name, "name": "表面処理", "from": None, "to": "黒染め"},
    ]
    # 自動で保存したビューア用データには編集後の属性が入る。
    edited = json.loads(open(out["viewer_data"], encoding="utf-8").read())
    assert edited["step"]["products"][0]["attributes"] == [{"name": "品番", "value": "PN-2"}, {"name": "表面処理", "value": "黒染め"}]
    assert "属性の変更 2 件" in _viewer.echoed[-1] or "2 attribute change(s)" in _viewer.echoed[-1]


@pytest.mark.asyncio
async def test_other_sessions_tmp_files_cannot_be_opened(env, tmp_path) -> None:
    """他のセッションの `_tmp_` の中のファイルは、Read と同じく file_path・compare_with に渡せない。"""
    other = tools._state._DEFAULT_WORKDIR / "_tmp_000000000000_other"
    other.mkdir()
    stl = _cube_stl(other)
    out = await _call(file_path=str(stl))
    assert out.startswith("Error:") and "another session" in out
    out = await _call(file_path=str(_cube_stl(tmp_path)), compare_with=str(stl))
    assert out.startswith("Error: compare_with:") and "another session" in out
    assert _viewer.shown == []


@pytest.mark.asyncio
async def test_viewer_data_pointing_at_another_sessions_file_is_refused(env, tmp_path) -> None:
    # ビューア用データの source.path（保存・比較で読み直す元の形）が他のセッションのファイルなら開かない。
    other = tools._state._DEFAULT_WORKDIR / "_tmp_000000000000_other"
    other.mkdir()
    await _call(file_path=str(_cube_stl(tmp_path)))
    data = _viewer.shown[0]["data"]
    data["source"]["path"] = str(_cube_stl(other))
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(data), encoding="utf-8")
    out = await _call(file_path=str(forged))
    assert out.startswith("Error: the model file of this viewer data cannot be used") and "another session" in out
    assert len(_viewer.shown) == 1


@pytest.mark.asyncio
async def test_compare_with_chain_to_another_sessions_file_is_refused(env, tmp_path) -> None:
    """compare_with のビューア用データがさらに別のビューア用データを指すとき（比べるときは連鎖をたどる）、
    連鎖の先にある他のセッションのファイルも拒む（1 段目しか確かめていなかった）。"""
    from src.cad.mesh_data import FORMAT as VIEWER_FORMAT

    other = tools._state._DEFAULT_WORKDIR / "_tmp_000000000000_other"
    other.mkdir()
    hidden = other / "secret.brep"
    hidden.write_text("not really a brep", encoding="utf-8")
    inner = tmp_path / "inner.json"
    inner.write_text(json.dumps({"format": VIEWER_FORMAT, "source": {"brep": str(hidden)}}), encoding="utf-8")
    outer = tmp_path / "outer.json"
    outer.write_text(json.dumps({"format": VIEWER_FORMAT, "source": {"path": str(inner)}}), encoding="utf-8")
    out = await cad_viewer._resolve_compare_with(str(outer))
    assert out.startswith("Error: compare_with: the model file of this viewer data cannot be used") and "another session" in out

    # 自分を指す循環でも止まる
    loop = tmp_path / "loop.json"
    loop.write_text(json.dumps({"format": VIEWER_FORMAT, "source": {"path": str(loop)}}), encoding="utf-8")
    out = await cad_viewer._resolve_compare_with(str(loop))
    assert out.startswith("Error: compare_with:") and "refers back to itself" in out


def test_load_previous_stops_on_a_reference_loop(tmp_path) -> None:
    from src.cad.compare import CompareError, load_previous
    from src.cad.mesh_data import FORMAT as VIEWER_FORMAT

    loop = tmp_path / "loop.json"
    loop.write_text(json.dumps({"format": VIEWER_FORMAT, "source": {"path": str(loop)}}), encoding="utf-8")
    with pytest.raises(CompareError, match="more than"):
        load_previous(loop)


@pytest.mark.asyncio
async def test_relative_file_path_is_resolved_in_the_work_folder(env, tmp_path) -> None:
    # 相対パスは Read と同じく、ツールバーで選んだ作業フォルダからのパス（セッションの `_tmp_` ではない）。
    work = tmp_path / "work"
    work.mkdir()
    _cube_stl(work)
    env.set("work_dir", str(work))
    out = json.loads(await _call(file_path="cube.stl"))
    assert out["model"]["source"] == str(work / "cube.stl")


def test_large_selection_is_deduplicated_quickly() -> None:
    import time as _time

    data = {"faces": [{}] * 200_000}
    selected = [{"kind": "face", "id": i % 100_000} for i in range(200_000)] + [{"kind": "face", "id": 200_000}, {"kind": "edge", "id": 0}]
    start = _time.perf_counter()
    picked = cad_viewer._valid_selection(data, ["face", "edge"], selected)
    assert _time.perf_counter() - start < 2
    assert len(picked) == 100_000 and picked[:2] == [("face", 0), ("face", 1)]


def test_attribute_type_must_be_a_plain_name() -> None:
    from src.cad import step_p21

    bad_type = "x');#9=(length_measure"
    data = {"step": {"products": [{"name": "P", "attributes": []}]}}
    rows = [{"name": "L", "value": 1.5, "type": "length_measure"}, {"name": "X", "value": 1, "type": bad_type}]
    out = cad_viewer._valid_attributes(data, {"P": rows})["P"]
    assert out == [{"name": "L", "value": 1.5, "type": "length_measure"}, {"name": "X", "value": 1}]
    # 書き出し側でも、型名に使えない文字を含む型はそのまま書かない。
    assert step_p21._value_text(1.5, bad_type) == ("VALUE_REPRESENTATION_ITEM", "REAL(1.5)")
    assert step_p21._value_text(1.5, "length_measure") == ("VALUE_REPRESENTATION_ITEM", "LENGTH_MEASURE(1.5)")


@pytest.mark.asyncio
async def test_failed_save_leaves_no_partial_file(env, tmp_path, monkeypatch) -> None:
    pytest.importorskip("cadquery")
    real_replace = cad_viewer.os.replace

    def locked(src, dst):
        if ".writing" in str(src):
            raise PermissionError("the file is open in another program")
        return real_replace(src, dst)

    async def press_save(props):
        monkeypatch.setattr(cad_viewer.os, "replace", locked)
        try:
            return await cad_viewer.save_viewer_edits({"save_id": props["save_id"], "colors": {"faces": {"0": "#ff0000"}}})
        finally:
            monkeypatch.setattr(cad_viewer.os, "replace", real_replace)

    _viewer.on_show = press_save
    await _call(file_path=str(_cube_stl(tmp_path)))
    res = _viewer.on_show_result
    assert not res["ok"] and "open in another program" in res["error"]
    assert not list(tools._state._DEFAULT_WORKDIR.rglob(".*.writing*"))


@pytest.mark.asyncio
async def test_confirm_waits_for_a_running_save(env, tmp_path, monkeypatch) -> None:
    """「保存」の書き出し中に確定しても、書き出しの終わりを待って、書き出したことを LLM へ伝える。"""
    import asyncio as _asyncio

    pytest.importorskip("cadquery")
    started = _asyncio.Event()
    real_run = cad_viewer._run

    async def slow_run(cmd, workdir, stdout_sink=None):
        if "--export" in cmd:  # 「保存」の書き出しだけを遅くする（ビューア用データを作るときも _run を通る）
            started.set()
            await _asyncio.sleep(0.3)
        return await real_run(cmd, workdir, stdout_sink)

    monkeypatch.setattr(cad_viewer, "_run", slow_run)
    saves: list = []

    async def press_save_then_confirm(props):
        payload = {"save_id": props["save_id"], "colors": {"faces": {"0": "#ff0000"}}}
        saves.append(_asyncio.ensure_future(cad_viewer.save_viewer_edits(payload)))
        await started.wait()
        # 書き出し中の 2 回目の保存は断る（同じファイルへ同時に書かない）。
        return await cad_viewer.save_viewer_edits({"save_id": props["save_id"]})

    _viewer.on_show = press_save_then_confirm
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    assert _viewer.on_show_result == {"ok": False, "error": "The model is still being saved. Wait until it finishes."}
    assert saves[0].done() and saves[0].result()["ok"]
    assert any("exported the 3D model" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_save_returns_the_export_notes(env, tmp_path, monkeypatch) -> None:
    """書き出しの注意書き（PMI が書けなかった・DXF は面の色を書けない等）を黙って消さず、
    保存結果の notes と LLM への答えの両方に入れる。"""
    from src.cad.run_cad import OK_MARKER

    note = "PMI (GD&T) could not be written, so the file has no PMI"

    real_run = cad_viewer._run

    async def fake_run(cmd, workdir, stdout_sink=None):
        if "--export" not in cmd:  # ビューア用データを作るサブプロセスは本物で行う
            return await real_run(cmd, workdir, stdout_sink)
        out_path = Path(cmd[cmd.index("--out") + 1])
        out_path.write_bytes(b"fake export")
        if stdout_sink is not None:
            stdout_sink.append(f'{OK_MARKER} {json.dumps({"notes": [note]})}\n')
        return None

    async def press_save(props):
        # 注意書きの無い書き出しと有る書き出しの両方を、ビューアが生きている間に（on_show の中で）確かめる。
        # LLM へ伝えるのは最後の保存の注意書きなので、注意書きのある保存を後にする。
        monkeypatch.setattr(cad_viewer, "_run", quiet_run)
        quiet = await cad_viewer.save_viewer_edits({"save_id": props["save_id"], "colors": {}})
        monkeypatch.setattr(cad_viewer, "_run", fake_run)
        noted = await cad_viewer.save_viewer_edits({"save_id": props["save_id"], "colors": {"faces": {"0": "#ff0000"}}})
        return noted, quiet

    async def quiet_run(cmd, workdir, stdout_sink=None):
        if "--export" not in cmd:
            return await real_run(cmd, workdir, stdout_sink)
        Path(cmd[cmd.index("--out") + 1]).write_bytes(b"fake export")
        return None

    _viewer.on_show = press_save
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    first, second = _viewer.on_show_result
    assert first["ok"] and first["notes"] == [note]
    # LLM への答え（extra）にも注意書きが入る
    assert any(note in n for n in out["notes"])
    # 注意書きが無い書き出しは notes が空
    assert second["ok"] and second["notes"] == []


def test_two_viewers_saving_the_same_file_get_different_paths(tmp_path) -> None:
    """同じモデルを別のビューア（同じ秒）で保存しても上書きし合わない。同じビューアは同じファイルを上書きする。"""
    data = {"source": {"path": str(tmp_path / "part.step")}}
    t1 = cad_viewer._SaveTarget(data=data, out_dir=tmp_path, thread_id="t1")
    t2 = cad_viewer._SaveTarget(data=data, out_dir=tmp_path, thread_id="t2")
    p1 = cad_viewer._model_path(t1, ".step")
    assert cad_viewer._model_path(t2, ".step") != p1
    # 同じビューアの 2 回目の保存は target.saved（1 回目の路径）を使う
    t1.saved = p1
    assert (t1.saved or cad_viewer._model_path(t1, ".step")) == p1


def test_malformed_viewer_data_returns_an_error_not_a_crash() -> None:
    """LLM が書いたり手で編集したりしたビューア用データで例外をもらさない（Error: 文字列で返す）。"""
    data = {"format": "VIEWER_JSON", "viewState": {"hiddenParts": ["a"]}, "faces": [], "parts": [], "mesh": {}}
    assert cad_viewer._initial_hidden(data) == set()  # 数ではない項目は捨てる
    out = cad_viewer._finish_answer(data, (None, [], [], ""), "tok", [], [], {})  # res が壊れている
    assert out.startswith("Error:")
    # 部品に id が無くても例外にしない（並び順で補う）
    no_ids = {"parts": [{"name": "A", "layer": {"off": True}}, {"name": "B"}, "junk", {"id": 7, "layer": {"frozen": True}}]}
    assert cad_viewer._initial_hidden(no_ids) == {0, 7}


def test_part_recolored_back_keeps_face_overrides() -> None:
    """部品の色を変えた後、その部品の面の色（元と同じ色）を維持する変更は捨てる（部品の色で上塗りされる）。"""
    data = {
        "parts": [{"id": 0, "color": "#ff0000"}],
        "faces": [{"id": 0, "part": 0, "color": "#00ff00"}],
    }
    # 面は元と同じ色の上書きだが、部品を変えている（#ff0000 → #0000ff）ので残す
    colors = {"faces": {"0": "#00ff00"}, "parts": {"0": "#0000ff"}}
    out = cad_viewer._valid_colors(data, colors)
    assert out == [
        {"kind": "face", "id": 0, "from": "#00ff00", "to": "#00ff00"},
        {"kind": "part", "id": 0, "from": "#ff0000", "to": "#0000ff"},
    ]
    # 部品を変えていないときは、面が元と同じ色の上書きは捨てる（frontend の dropUnchangedPartColors と同じ）
    assert cad_viewer._valid_colors(data, {"faces": {"0": "#00ff00"}, "parts": {"0": "#ff0000"}}) == []


def test_reopened_viewer_data_keeps_earlier_color_changes() -> None:
    """開き直したビューア用データを書き出すとき、前の回に変えた色も変更として残す（edits.color_changes）。"""
    from src.cad.mesh_data import apply_view_edits, merge_color_changes

    previous = [{"kind": "face", "id": 0, "from": None, "to": "#ff0000"}]
    data = {"format": "VIEWER_JSON", "faces": [{"id": 0, "color": "#ff0000"}], "parts": [{"id": 0, "color": None}], "mesh": {}, "edits": {"color_changes": previous}}
    # 今回変更が無い保存でも、前の回の変更は残る（書き出しが色を付けるため）
    assert apply_view_edits(data, face_colors={}, part_colors={}, color_changes=[])["edits"]["color_changes"] == previous
    # 同じ面・部品の変更は最初の from と最後の to を残し、元の色に戻ったものは外す
    assert merge_color_changes(previous, [{"kind": "face", "id": 0, "from": "#ff0000", "to": None}]) == []
    merged = merge_color_changes(previous, [{"kind": "face", "id": 0, "from": "#ff0000", "to": "#0000ff"}])
    assert merged == [{"kind": "face", "id": 0, "from": None, "to": "#0000ff"}]


async def _save_step(src, colors):
    """src の STEP をビューアで開き、colors(data) の色を当てて「保存」し、(結果, 書き出した STEP のビューア用データ) を返す。"""
    from src.cad.export_mesh import Options, cad_file_to_viewer_data

    async def press_save(props):
        return await cad_viewer.save_viewer_edits({"save_id": props["save_id"], "colors": colors(props["data"])})

    _viewer.on_show = press_save
    await _call(file_path=str(src))
    res = _viewer.on_show_result
    assert res["ok"], res
    return res, cad_file_to_viewer_data(res["path"], "STEP", Options())


@pytest.mark.asyncio
async def test_part_color_change_also_recolors_faces_with_their_own_color(env, tmp_path) -> None:
    cq = pytest.importorskip("cadquery")
    assy = cq.Assembly(name="asm")
    assy.add(cq.Workplane().box(10, 10, 10), name="a", color=cq.Color(1, 0, 0, 1))
    assy.add(cq.Workplane().box(10, 10, 10), name="b", loc=cq.Location((20, 0, 0)), color=cq.Color(0, 0, 1, 1))
    src = tmp_path / "asm.step"
    assy.export(str(src))
    # 1 回目: 部品 a の面に個別の色を付けて保存する。
    res, first = await _save_step(src, lambda d: {"faces": {str(d["parts"][0]["faces"][0]): "#00ff00"}, "parts": {}})
    face = first["parts"][0]["faces"][0]
    assert first["faces"][face]["color"] == "#00ff00"
    # 2 回目: その STEP で部品 a の色だけを変える。ビューアでは面もすべて新しい色なので、書き出しも合わせる。
    _res, second = await _save_step(res["path"], lambda d: {"faces": {}, "parts": {"0": "#ffff00"}})
    assert {second["faces"][f]["color"] for f in second["parts"][0]["faces"]} == {"#ffff00"}
    # 3 回目: 面の色を外す（to null）と、面の個別の色は消えて部品の色になる。
    _res, third = await _save_step(res["path"], lambda d: {"faces": {str(face): None}, "parts": {}})
    assert third["faces"][face]["color"] == "#ff0000"


@pytest.mark.asyncio
async def test_color_of_one_copy_of_a_shared_part_survives_reopening(env, tmp_path) -> None:
    cq = pytest.importorskip("cadquery")
    box = cq.Workplane().box(5, 5, 5)
    assy = cq.Assembly(name="asm")
    assy.add(box, name="b1", color=cq.Color(0.5, 0.5, 0.5, 1))
    assy.add(box, name="b2", loc=cq.Location((10, 0, 0)), color=cq.Color(0.5, 0.5, 0.5, 1))
    src = tmp_path / "shared.step"
    assy.export(str(src))
    _res, out = await _save_step(src, lambda d: {"faces": {}, "parts": {"1": "#ff0000"}})
    assert [p.get("color") for p in out["parts"]] == ["#808080", "#ff0000"]


@pytest.mark.asyncio
async def test_save_keeps_the_attributes_of_assembly_products(env, tmp_path) -> None:
    cq = pytest.importorskip("cadquery")
    from src.cad import step_p21

    assy = cq.Assembly(name="asm")
    assy.add(cq.Workplane().box(10, 10, 10), name="a", color=cq.Color(1, 0, 0, 1))
    assy.add(cq.Workplane().box(10, 10, 10), name="b", loc=cq.Location((20, 0, 0)))
    src = tmp_path / "asm.step"
    assy.export(str(src))
    text, enc = step_p21.read_text(src)
    text, n = step_p21.add_attributes(text, {"asm": [{"name": "Project", "value": "P-1"}], "b": [{"name": "PN", "value": "B-1"}]})
    assert n == 2
    src.write_bytes(text.encode(enc))
    res, _out = await _save_step(src, lambda d: {"faces": {}, "parts": {"0": "#00ff00"}})
    saved, _enc = step_p21.read_text(Path(res["path"]))
    attrs = {p["name"]: p.get("attributes") for p in step_p21.read_products(saved)}
    assert attrs["asm"] == [{"name": "Project", "value": "P-1"}]
    assert attrs["b"] == [{"name": "PN", "value": "B-1"}]


def test_dxf_export_resets_a_removed_layer_color(tmp_path) -> None:
    """レイヤーの色を「色なし」に戻して保存したら、元の色を残さず既定の色（ACI 7）にする。id の無い部品でも落ちない。"""
    ezdxf = pytest.importorskip("ezdxf")
    from src.cad.export_model import _export_dxf

    doc = ezdxf.new()
    doc.layers.add("RED", color=1)
    doc.modelspace().add_line((0, 0), (10, 0), dxfattribs={"layer": "RED"})
    src = tmp_path / "plan.dxf"
    doc.saveas(src)
    data = {
        "source": {"kind": "file", "format": "DXF", "path": str(src)},
        "parts": [{"name": "RED", "color": None, "layer": {"color": "#ff0000", "aci": 1}}],
        "viewState": {"hiddenParts": [0]},
    }
    out = tmp_path / "out.dxf"
    notes = _export_dxf(data, out)
    layer = ezdxf.readfile(out).layers.get("RED")
    assert abs(layer.color) == 7 and layer.rgb is None
    assert layer.is_off()
    assert any("RED" in n for n in notes)


@pytest.mark.asyncio
async def test_cone_radius_keeps_the_approximate_flag(env, tmp_path) -> None:
    """円錐の半径を表示用の三角形の上の点で測ったとき（measure.ts）の approximate を LLM へ渡す。"""
    _viewer.response = {
        "submitted": True,
        "selected": [],
        "measurements": [{"type": "radius", "radius": 4.9, "diameter": 9.8, "geomType": "CONE", "radius_at": "clicked point", "approximate": True}],
    }
    out = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    assert out["measurements"][0]["approximate"] is True


@pytest.mark.asyncio
async def test_broken_viewer_data_does_not_raise_on_confirm(env, tmp_path) -> None:
    """LLM が書いた・手で直したビューア用データ（製品の並びに文字列）でも、確定で例外を出さず結果を返す。"""
    _viewer.response = {"submitted": True, "selected": [], "colors": {"faces": {"0": "#ff0000"}, "parts": {}}}
    first = json.loads(await _call(file_path=str(_cube_stl(tmp_path))))
    broken = Path(first["viewer_data"])
    data = json.loads(broken.read_text(encoding="utf-8"))
    data["step"] = {"products": ["not a dict"]}
    broken.write_text(json.dumps(data), encoding="utf-8")
    _viewer.response = {"submitted": True, "selected": [], "attributes": {"x": []}}
    out = await _call(file_path=str(broken))
    assert isinstance(out, str)


@pytest.mark.asyncio
async def test_viewer_data_with_a_relative_or_at_n_source_path_is_refused(env, tmp_path) -> None:
    """source.path が相対パス・@N なら開かない（保存・比較の子プロセスは中の文字列をそのままセッションの
    `_tmp_` を基準に開くので、確かめたファイルと別のファイルを指しうる）。"""
    from src.cad.mesh_data import FORMAT as VIEWER_FORMAT

    for ref in (r"..\_tmp_000000000000_other\x.dxf", "cube.stl", "@1"):
        forged = tmp_path / "forged.json"
        forged.write_text(json.dumps({"format": VIEWER_FORMAT, "source": {"kind": "file", "format": "STL", "path": ref}}), encoding="utf-8")
        out = await _call(file_path=str(forged))
        assert out.startswith("Error: the model file of this viewer data cannot be used") and "absolute path" in out, ref
    assert _viewer.shown == []


@pytest.mark.asyncio
async def test_compare_with_is_refused_when_the_new_version_is_a_mesh(env, tmp_path) -> None:
    """新しい版が STL のときは比べられないので、diff の無い結果を黙って返さずにエラーにする。"""
    cq = pytest.importorskip("cadquery")
    old = tmp_path / "old.step"
    cq.exporters.export(cq.Workplane().box(10, 10, 10), str(old))
    out = await _call(file_path=str(_cube_stl(tmp_path)), compare_with=str(old))
    assert out.startswith("Error: compare_with needs a STEP/IGES/BREP/BIN model") and "STL" in out
    assert _viewer.shown == []


def test_history_size_counts_japanese_as_one_character_each() -> None:
    """履歴の大きさは frontend（JSON.stringify の文字数）と同じ数え方で確かめる（日本語をエスケープした 6 文字で数えない）。"""
    data = {"faces": [{"id": 0}], "parts": []}
    edit = {"colors": {"faces": {}, "parts": {}}, "hiddenParts": [], "visible": {}}
    raw = {"past": [edit], "future": [], "base": {"faces": {}, "parts": {}}, "note": "属" * (cad_viewer._HISTORY_MAX_CHARS // 2)}
    assert len(json.dumps(raw)) > cad_viewer._HISTORY_MAX_CHARS
    assert cad_viewer._valid_history(data, raw) is not None
