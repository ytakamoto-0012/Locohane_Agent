"""生成中の追加指示に添付ファイルを付けた場合（POST /locohane/threads/{id}/inject）のテスト。

受付窓口へ実際に HTTP で送り、添付が受け付けた時点でスレッド専用フォルダへコピーされること、
LLM へ渡す本文が通常の送信と同じ形（保存先と `@N`・貼り付けテキストの展開）になること、
他のスレッド・他の所有者のアップロードを参照できないこと、受付が閉じていれば何もしないこと、
再送で二重にコピー・登録しないことを確かめる。画面表示（_TurnInjections.show）とグラフへの
入り方も確かめる。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from chainlit.auth import get_current_user
from chainlit.server import app as chainlit_app
from chainlit.session import ws_sessions_id
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode

import app
from src import graph as graph_mod
from src import user_injection


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    user_injection._queues.clear()
    app._generating_owner_threads.clear()
    ws_sessions_id.pop("sid-1", None)
    ws_sessions_id.pop("sid-other", None)
    chainlit_app.dependency_overrides[get_current_user] = lambda: None  # 匿名（所有者 "anonymous"）
    workdir = tmp_path / "work"
    monkeypatch.setattr(app, "_resolve_exec_workdir", lambda thread_id=None: workdir)
    registered: dict[str, list[str]] = {}

    def fake_register(paths, description=None, thread_id=None):
        registered.setdefault(thread_id, []).extend(paths)
        return {f"@{i + 1}": p for i, p in enumerate(paths)}

    monkeypatch.setattr(app, "_register_path_memory", fake_register)
    monkeypatch.setattr(app, "register_raw_unc_paths_in_text", lambda text, thread_id=None: text)
    yield SimpleNamespace(workdir=workdir, registered=registered)
    chainlit_app.dependency_overrides.pop(get_current_user, None)
    ws_sessions_id.pop("sid-1", None)
    ws_sessions_id.pop("sid-other", None)
    user_injection._queues.clear()
    app._generating_owner_threads.clear()


def _client():
    return TestClient(chainlit_app)


def _upload(tmp_path: Path, name: str, content: str = "data") -> dict:
    path = tmp_path / "chainlit_files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"id": f"file-{name}", "path": path, "name": name, "type": "text/plain", "size": len(content)}


def _session(session_id: str, thread_id: str, files: list[dict], user=None):
    session = SimpleNamespace(id=session_id, thread_id=thread_id, user=user, files={f["id"]: f for f in files})
    ws_sessions_id[session_id] = session
    return session


def _generating(thread_id: str = "t1") -> str:
    app._generating_owner_threads["anonymous"] = thread_id
    return user_injection.open_thread(thread_id)


def _post(body: dict, thread_id: str = "t1"):
    return _client().post(f"/locohane/threads/{thread_id}/inject", json=body)


def test_rejected_with_409_when_not_generating():
    res = _post({"text": "追加"})
    assert res.status_code == 409


def test_text_only_instruction_is_queued(_reset):
    _generating()
    res = _post({"text": "CSVで", "id": "inj-1"})
    assert res.status_code == 200 and res.json()["id"] == "inj-1"
    [queued] = user_injection.drain("t1")
    assert queued.text == "CSVで" and queued.llm_text == "CSVで" and queued.files == ()
    assert not _reset.workdir.exists()  # 添付が無ければフォルダも作らない


def test_attachments_are_copied_and_listed_for_the_llm(_reset, tmp_path):
    _generating()
    a = _upload(tmp_path, "report.xlsx")
    b = _upload(tmp_path, "photo.png")
    _session("sid-1", "t1", [a, b])

    res = _post({"text": "これも見て", "id": "inj-1", "files": [a["id"], b["id"]], "session_id": "sid-1"})

    assert res.status_code == 200
    [queued] = user_injection.drain("t1")
    saved = [Path(f["path"]) for f in queued.files]
    assert [p.name for p in saved] == ["report.xlsx", "photo.png"]
    assert all(p.is_file() and _reset.workdir in p.parents for p in saved)  # スレッド専用フォルダへコピー済み
    assert queued.text == "これも見て"
    assert "[Saved locations of uploaded files (pass @N to tools)]" in queued.llm_text
    assert f"- @1: {saved[0]}" in queued.llm_text and f"- @2: {saved[1]}" in queued.llm_text
    assert _reset.registered["t1"][:2] == [str(saved[0]), str(saved[1])]  # 生成中のスレッドのパスメモリーへ
    # 元の一時ファイルが消えても、コピー済みなので影響しない。
    Path(a["path"]).unlink()
    assert saved[0].is_file()


def test_attachment_only_instruction_is_allowed(_reset, tmp_path):
    _generating()
    a = _upload(tmp_path, "memo.txt")
    _session("sid-1", "t1", [a])
    res = _post({"text": "", "files": [a["id"]], "session_id": "sid-1"})
    assert res.status_code == 200
    [queued] = user_injection.drain("t1")
    assert queued.text == "" and len(queued.files) == 1


def test_pasted_text_attachment_is_expanded_into_the_llm_text(_reset, tmp_path):
    _generating()
    pasted = _upload(tmp_path, "pasted-text-1700000000000-1.txt", "長い貼り付けテキスト")
    _session("sid-1", "t1", [pasted])
    res = _post({"text": "これを要約して", "files": [pasted["id"]], "session_id": "sid-1"})
    assert res.status_code == 200
    [queued] = user_injection.drain("t1")
    assert "[Pasted text 1" in queued.llm_text and "長い貼り付けテキスト" in queued.llm_text
    assert "[Saved locations of uploaded files" not in queued.llm_text


def test_pasted_text_counts_toward_the_length_limit(monkeypatch, tmp_path):
    from dataclasses import replace

    monkeypatch.setattr(app, "_config", replace(app._config, ui_max_input_chars=10))
    _generating()
    pasted = _upload(tmp_path, "pasted-text-1700000000000-1.txt", "x" * 20)
    _session("sid-1", "t1", [pasted])
    res = _post({"text": "a", "files": [pasted["id"]], "session_id": "sid-1"})
    assert res.status_code == 400
    assert user_injection.drain("t1") == []


@pytest.mark.parametrize(
    "session_thread, owner_user, file_ids",
    [
        ("other-thread", None, ["file-report.xlsx"]),  # 別のスレッドのセッション
        ("t1", SimpleNamespace(identifier="someone-else"), ["file-report.xlsx"]),  # 別の所有者
        ("t1", None, ["no-such-file"]),  # アップロードされていない ID
    ],
)
def test_attachments_from_other_threads_owners_or_unknown_ids_are_rejected(
    _reset, tmp_path, session_thread, owner_user, file_ids
):
    _generating()
    a = _upload(tmp_path, "report.xlsx")
    _session("sid-1", session_thread, [a], user=owner_user)
    res = _post({"text": "見て", "files": file_ids, "session_id": "sid-1"})
    assert res.status_code == 400
    assert user_injection.drain("t1") == []
    assert not _reset.workdir.exists()  # 何もコピーしない


def test_attachments_without_a_live_session_are_rejected(_reset, tmp_path):
    _generating()
    res = _post({"text": "見て", "files": ["file-x"], "session_id": "gone"})
    assert res.status_code == 400
    res = _post({"text": "見て", "files": ["file-x"]})
    assert res.status_code == 400


def test_empty_instruction_is_rejected():
    _generating()
    assert _post({"text": "   "}).status_code == 400


def test_resend_with_the_same_id_does_not_copy_or_queue_twice(_reset, tmp_path):
    _generating()
    a = _upload(tmp_path, "report.xlsx")
    _session("sid-1", "t1", [a])
    body = {"text": "見て", "id": "inj-1", "files": [a["id"]], "session_id": "sid-1"}
    assert _post(body).status_code == 200
    assert len(user_injection.drain("t1")) == 1  # 会話へ入った（取り出し済み）
    assert _post(body).status_code == 200  # 応答を受け取れずに再送した
    assert user_injection.drain("t1") == []
    uploads = list((_reset.workdir / "uploads").iterdir())
    assert len(uploads) == 1  # 送信フォルダは1つだけ（二重にコピーしていない）


def test_closed_queue_does_not_copy_attachments(_reset, tmp_path):
    token = _generating()
    user_injection.close_thread("t1", token)  # ターンが終わりかけ・停止済み
    a = _upload(tmp_path, "report.xlsx")
    _session("sid-1", "t1", [a])
    res = _post({"text": "見て", "files": [a["id"]], "session_id": "sid-1"})
    assert res.status_code == 409
    assert not _reset.workdir.exists()


def test_inject_route_is_registered_even_without_thread_store():
    """[thread_store] が無効でも受付窓口がある（他の /locohane/threads* と違い常に登録する）。"""
    paths = [getattr(r, "path", None) for r in chainlit_app.router.routes]
    assert "/locohane/threads/{thread_id}/inject" in paths
    # SPA の catch-all より前にある（後ろだと index.html が返って呼ばれない）。
    catch_all = next((i for i, r in enumerate(chainlit_app.router.routes) if getattr(r, "path", "") == "/{full_path:path}"), None)
    if catch_all is not None:
        assert paths.index("/locohane/threads/{thread_id}/inject") < catch_all


@pytest.mark.asyncio
async def test_shown_message_carries_the_attachments(monkeypatch, tmp_path):
    saved_png = tmp_path / "photo.png"
    saved_png.write_bytes(b"png")
    saved_xlsx = tmp_path / "report.xlsx"
    saved_xlsx.write_bytes(b"xlsx")
    sent = []

    class _FakeMessage:
        def __init__(self, content=None, author=None, type=None, metadata=None, elements=None):
            self.kwargs = dict(content=content, type=type, metadata=metadata, elements=elements or [])

        async def send(self):
            sent.append(self.kwargs)

    class _FakeElement:
        def __init__(self, name, path, display):
            self.name, self.path = name, path

    class _FakeImage(_FakeElement):
        pass

    class _FakeFile(_FakeElement):
        pass

    monkeypatch.setattr(app.cl, "Message", _FakeMessage)
    monkeypatch.setattr(app.cl, "Image", _FakeImage)
    monkeypatch.setattr(app.cl, "File", _FakeFile)
    monkeypatch.setattr(app.cl, "user_session", SimpleNamespace(get=lambda key, default=None: None))

    tracker = app._TurnInjections("turn")
    files = [
        {"name": "photo.png", "path": str(saved_png)},
        {"name": "report.xlsx", "path": str(saved_xlsx)},
        {"name": "gone.txt", "path": str(tmp_path / "gone.txt")},  # 消えたファイルは添えない
    ]
    await tracker.show([{"id": "inj-1", "text": "見て", "files": files}])
    await tracker.show([{"id": "inj-1", "text": "見て", "files": files}])  # 二重に出さない

    assert len(sent) == 1
    elements = sent[0]["elements"]
    assert [type(e).__name__ for e in elements] == ["_FakeImage", "_FakeFile"]
    assert sent[0]["metadata"] == {app.INJECTION_ID_METADATA_KEY: "inj-1"}
    # 会話ログ用の本文には添付の名前を残す（区切り文字は表示言語による）。
    assert len(tracker.texts) == 1 and tracker.texts[0].startswith("見て [attached: ")
    assert "photo.png" in tracker.texts[0] and "report.xlsx" in tracker.texts[0]


@pytest.mark.asyncio
async def test_graph_passes_llm_text_and_event_carries_files(monkeypatch):
    """会話へ入るのは LLM 向けの本文（保存先付き）、画面表示の通知には本文と添付が載る。"""
    events = []

    from langchain_core.callbacks import AsyncCallbackHandler

    class _EventRecorder(AsyncCallbackHandler):
        async def on_custom_event(self, name, data, **kwargs):
            if name == user_injection.INJECTION_APPLIED_EVENT:
                events.append(data)

    @tool
    def work() -> str:
        """作業する。"""
        user_injection.enqueue(
            "t1",
            "これも見て",
            "inj-1",
            llm_text="これも見て\n\n[Saved locations of uploaded files (pass @N to tools)]\n- @1: C:/work/report.xlsx",
            files=[{"name": "report.xlsx", "path": "C:/work/report.xlsx"}],
        )
        return "ok"

    calls = []

    class _Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            calls.append(list(messages))
            if len(calls) == 1:
                return AIMessage(content="", tool_calls=[{"name": "work", "args": {}, "id": "c1"}])
            return AIMessage(content="done")

    async def fake_build_model(config, role, wait_when_busy=True):
        return _Model()

    monkeypatch.setattr(graph_mod, "build_model", fake_build_model)
    monkeypatch.setattr(graph_mod, "enable_thinking_control", lambda m, c, r: m)
    monkeypatch.setattr(graph_mod, "get_all_tools", lambda: [work])
    monkeypatch.setattr(graph_mod, "apply_session_tool_overrides", lambda t: t)
    monkeypatch.setattr(graph_mod, "filter_main_agent_tools", lambda t, c: t)
    monkeypatch.setattr(graph_mod, "ImageAwareToolNode", ToolNode)
    cfg = SimpleNamespace(
        context_trim_enabled=False,
        graph_token_guard_enabled=False,
        context_compaction_enabled=False,
        context_compaction_pre_note_threshold=0,
        tool_loop_guard_enabled=False,
        tool_loop_guard_max_repeats=3,
        tool_loop_guard_exclude_tools=[],
    )
    graph = await graph_mod._build_handwritten_graph(cfg, "sp", MemorySaver())
    user_injection.open_thread("t1")
    await graph.ainvoke(
        {"messages": [HumanMessage(content="始めて")]},
        config={"configurable": {"thread_id": "t1"}, "callbacks": [_EventRecorder()]},
    )
    injected = calls[1][-1]
    assert isinstance(calls[1][-2], ToolMessage)
    assert injected.content.endswith("- @1: C:/work/report.xlsx")
    assert user_injection.injection_items(injected)[0]["files"] == [{"name": "report.xlsx", "path": "C:/work/report.xlsx"}]
    assert events[0]["injections"][0]["text"] == "これも見て"
    assert events[0]["injections"][0]["files"][0]["name"] == "report.xlsx"
