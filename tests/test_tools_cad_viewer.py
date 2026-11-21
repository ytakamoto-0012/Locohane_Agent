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
