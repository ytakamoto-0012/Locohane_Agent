"""割り込み指示（src/user_injection.py）と、ターン中の異常系（ループガード・通信エラー・停止）の組み合わせ。

偽のモデルで実際にグラフ（手書き実装・prebuilt 実装）を動かし、app.py のリトライ・修復と
同じ手順を踏んだときに、指示が失われない・二重にならない・tool_calls と ToolMessage の
対応を壊さないことを確かめる。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode

import app
from src import graph as graph_mod
from src import user_injection
from src.llm import ThinkingLoopDetected, ToolCallLoopDetected


@pytest.fixture(autouse=True)
def _clear_queues():
    user_injection._queues.clear()
    yield
    user_injection._queues.clear()


def _cfg(**overrides):
    values = dict(
        context_trim_enabled=False,
        graph_token_guard_enabled=False,
        context_compaction_enabled=False,
        context_compaction_pre_note_threshold=0,
        tool_loop_guard_enabled=False,
        tool_loop_guard_max_repeats=3,
        tool_loop_guard_exclude_tools=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _EventRecorder(AsyncCallbackHandler):
    def __init__(self):
        self.applied: list[list[str]] = []

    async def on_custom_event(self, name, data, **kwargs):
        if name == user_injection.INJECTION_APPLIED_EVENT:
            self.applied.append([i["text"] for i in data["injections"]])


class _Model:
    """呼ばれるたびに script の次の要素を返す（例外なら送出する）偽モデル。入力を記録する。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _tc(name, call_id, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _patch(monkeypatch, model, tools):
    async def fake_build_model(config, role, wait_when_busy=True):
        return model

    monkeypatch.setattr(graph_mod, "build_model", fake_build_model)
    monkeypatch.setattr(graph_mod, "enable_thinking_control", lambda m, c, r: m)
    monkeypatch.setattr(graph_mod, "get_all_tools", lambda: tools)
    monkeypatch.setattr(graph_mod, "apply_session_tool_overrides", lambda t: t)
    monkeypatch.setattr(graph_mod, "filter_main_agent_tools", lambda t, c: t)
    monkeypatch.setattr(graph_mod, "ImageAwareToolNode", ToolNode)


def _types(messages):
    return [type(m).__name__ for m in messages]


def _assert_tool_calls_answered(messages):
    """tool_calls を持つ AIMessage の直後に、全 tool_call 分の ToolMessage が（HumanMessage を挟まず）続く。"""
    for i, m in enumerate(messages):
        calls = getattr(m, "tool_calls", None) or []
        if not calls:
            continue
        following = messages[i + 1 : i + 1 + len(calls)]
        assert all(isinstance(f, ToolMessage) for f in following), _types(messages)
        assert {f.tool_call_id for f in following} == {c["id"] for c in calls}


@pytest.mark.asyncio
async def test_thinking_loop_retry_keeps_injection_once(monkeypatch):
    """ループ検知（モデル呼び出し中の ThinkingLoopDetected）で再試行しても、指示は1回だけ会話に残る。"""

    @tool
    def work() -> str:
        """作業する。"""
        user_injection.enqueue("t", "CSVで")
        return "ok"

    model = _Model([_tc("work", "c1"), ThinkingLoopDetected("loop"), AIMessage(content="done")])
    _patch(monkeypatch, model, [work])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())
    user_injection.open_thread("t")
    recorder = _EventRecorder()
    config = {"configurable": {"thread_id": "t"}, "callbacks": [recorder]}

    with pytest.raises(ThinkingLoopDetected):
        await graph.ainvoke({"messages": [HumanMessage(content="始めて")]}, config=config)
    # app.py と同じ: 注意メッセージを START から入れて再試行し、成功後に取り除く。
    nudge = HumanMessage(content="nudge", id="nudge-1")
    await graph.ainvoke({"messages": [nudge]}, config=config)
    await graph_mod._remove_message_ids_if_present(graph, config, ["nudge-1"])

    history = (await graph.aget_state(config)).values["messages"]
    injected = [m for m in history if user_injection.is_injection_message(m)]
    assert len(injected) == 1 and injected[0].content.endswith("CSVで")
    assert "nudge" not in [m.content for m in history]
    assert model.calls[2][-2].content.endswith("CSVで")  # 再試行のモデル呼び出しにも見えている
    assert recorder.applied == [["CSVで"]]  # 表示の通知は1回だけ
    _assert_tool_calls_answered(history)


@pytest.mark.asyncio
async def test_tool_call_loop_guard_removal_and_retry_with_queued_injection(monkeypatch):
    """ツール呼び出しループ検知 → 繰り返し区間の除去（as_node 指定なし）→ 再試行で、待っていた指示が入る。"""

    @tool
    def work() -> str:
        """作業する。"""
        return "same"

    repeated = [_tc("work", f"c{i}") for i in range(3)]
    model = _Model([*repeated, AIMessage(content="done")])
    _patch(monkeypatch, model, [work])
    graph = await graph_mod._build_handwritten_graph(_cfg(tool_loop_guard_enabled=True), "sp", MemorySaver())
    user_injection.open_thread("t")
    recorder = _EventRecorder()
    config = {"configurable": {"thread_id": "t"}, "callbacks": [recorder]}

    with pytest.raises(ToolCallLoopDetected) as excinfo:
        await graph.ainvoke({"messages": [HumanMessage(content="始めて")]}, config=config)
    # 検知の後（再試行の前）にユーザーが指示を送った想定。
    user_injection.enqueue("t", "やり方を変えて")
    # app.py と同じ: 繰り返した区間を取り除く（as_node を指定しない aupdate_state）。
    await graph_mod._remove_message_ids_if_present(graph, config, list(excinfo.value.remove_ids))
    await graph.ainvoke({"messages": [HumanMessage(content="nudge", id="n1")]}, config=config)

    last_input = model.calls[-1]
    assert last_input[-1].content.endswith("やり方を変えて")
    assert last_input[-2].content == "nudge"
    history = (await graph.aget_state(config)).values["messages"]
    _assert_tool_calls_answered(history)
    assert recorder.applied == [["やり方を変えて"]]


@pytest.mark.asyncio
async def test_injection_does_not_trigger_tool_call_loop_guard(monkeypatch):
    """同じツール呼び出しを繰り返している途中で指示が入ると、連続が途切れてループ扱いにならない。"""

    @tool
    def work() -> str:
        """作業する。"""
        if len(model.calls) == 2:
            user_injection.enqueue("t", "別の方法で")
        return "same"

    model = _Model([_tc("work", "c0"), _tc("work", "c1"), _tc("work", "c2"), AIMessage(content="done")])
    _patch(monkeypatch, model, [work])
    graph = await graph_mod._build_handwritten_graph(_cfg(tool_loop_guard_enabled=True), "sp", MemorySaver())
    user_injection.open_thread("t")
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="始めて")]}, config={"configurable": {"thread_id": "t"}}
    )
    assert result["messages"][-1].content == "done"


@pytest.mark.asyncio
async def test_connection_error_resume_keeps_injection_without_reapplying(monkeypatch):
    """通信エラーでモデル呼び出しが失敗 → inputs=None で再開しても、指示は既に確定済みで二重にならない。"""

    @tool
    def work() -> str:
        """作業する。"""
        user_injection.enqueue("t", "急いで")
        return "ok"

    model = _Model([_tc("work", "c1"), ConnectionError("down"), AIMessage(content="done")])
    _patch(monkeypatch, model, [work])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())
    user_injection.open_thread("t")
    recorder = _EventRecorder()
    config = {"configurable": {"thread_id": "t"}, "callbacks": [recorder]}

    with pytest.raises(ConnectionError):
        await graph.ainvoke({"messages": [HumanMessage(content="始めて")]}, config=config)
    state = await graph.aget_state(config)
    assert state.next == ("agent",)  # inject ノードは確定済みで、失敗した agent から再開する
    assert state.values["messages"][-1].content.endswith("急いで")
    await graph.ainvoke(None, config=config)

    history = (await graph.aget_state(config)).values["messages"]
    assert _types(history) == ["HumanMessage", "AIMessage", "ToolMessage", "HumanMessage", "AIMessage"]
    assert model.calls[2][-1].content.endswith("急いで")
    assert recorder.applied == [["急いで"]]


async def _run_until_cancelled(graph, inputs, config, started: asyncio.Event):
    task = asyncio.create_task(graph.ainvoke(inputs, config=config))
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stop_during_parallel_tool_calls_repairs_without_injection(monkeypatch):
    """並列ツール呼び出しの途中で停止（片方は完了・片方は実行中、その間に指示が届く）。

    app.py と同じ手順（停止で受付を閉じる → 孤立 tool_call の修復 → 次のメッセージ）で、
    指示は会話へ入らず、完了済みツールの結果は書き戻され、履歴の対応も壊れない。
    """
    started = asyncio.Event()

    @tool
    async def fast() -> str:
        """すぐ終わる。"""
        user_injection.enqueue("t", "やっぱりやめて")
        return "fast-done"

    @tool
    async def slow() -> str:
        """終わらない（サブエージェントの委譲など）。"""
        started.set()
        await asyncio.Event().wait()
        return "never"

    parallel = AIMessage(
        content="",
        tool_calls=[{"name": "fast", "args": {}, "id": "f1"}, {"name": "slow", "args": {}, "id": "s1"}],
    )
    model = _Model([parallel, AIMessage(content="next turn")])
    _patch(monkeypatch, model, [fast, slow])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())
    token = user_injection.open_thread("t")
    recorder = _EventRecorder()
    config = {"configurable": {"thread_id": "t"}, "callbacks": [recorder]}

    await _run_until_cancelled(graph, {"messages": [HumanMessage(content="始めて")]}, config, started)
    app._close_user_injection_on_stop("t")  # on_stop と同じ
    assert user_injection.close_thread("t", token) == []  # on_message の finally（既に閉じている）

    state = await graph.aget_state(config)
    # 標準の ToolNode は並列の tool_call を1タスク内で実行するため、完了済みの結果も確定前の
    # 書き込みに残らず、両方とも仮結果で埋まる（割り込みとは無関係の既存の挙動）。
    repair, orphaned, _recovered = app._orphan_repair_messages(state, "stopped, ")
    assert orphaned >= 1
    await graph.aupdate_state(config, {"messages": repair}, as_node="tools")
    assert (await graph.aget_state(config)).next == ("inject",)

    # 次のターン（新しい受付）。前のターンの指示は入らない。
    user_injection.open_thread("t")
    await graph.ainvoke({"messages": [HumanMessage(content="次の依頼")]}, config=config)
    history = (await graph.aget_state(config)).values["messages"]
    _assert_tool_calls_answered(history)
    assert not any(user_injection.is_injection_message(m) for m in history)
    assert recorder.applied == []


@pytest.mark.asyncio
async def test_stop_while_waiting_for_long_tool_then_new_turn_is_not_closed_by_old_turn(monkeypatch):
    """長いツール（サブエージェント等）の実行中に停止 → 古いターンの後始末より先に次のターンが始まっても、
    古いターンの finally は次のターンの受付を閉じない。"""
    started = asyncio.Event()

    @tool
    async def dispatch() -> str:
        """サブエージェントに委譲する（終わらない）。"""
        started.set()
        await asyncio.Event().wait()
        return "never"

    model = _Model([_tc("dispatch", "d1")])
    _patch(monkeypatch, model, [dispatch])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())
    old_token = user_injection.open_thread("t")
    user_injection.enqueue("t", "古いターンへの指示")

    await _run_until_cancelled(graph, {"messages": [HumanMessage(content="始めて")]}, {"configurable": {"thread_id": "t"}}, started)
    app._close_user_injection_on_stop("t")
    new_token = user_injection.open_thread("t")  # 次のターンが先に始まった
    queued = user_injection.enqueue("t", "新しいターンへの指示")
    assert user_injection.close_thread("t", old_token) == []  # 古いターンの finally
    assert user_injection.current_token("t") is new_token
    assert user_injection.drain("t") == [queued]


@pytest.mark.asyncio
async def test_injection_during_approval_wait_enters_after_tools(monkeypatch):
    """承認待ち（ツールの中でユーザーの応答を待つ）の間に送った指示は、そのツールの結果の後に入る。"""

    @tool
    async def approve_plan() -> str:
        """承認を待つ。"""
        user_injection.enqueue("t", "承認後はCSVで")
        return "approved"

    model = _Model([_tc("approve_plan", "a1"), AIMessage(content="done")])
    _patch(monkeypatch, model, [approve_plan])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())
    user_injection.open_thread("t")
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="始めて")]}, config={"configurable": {"thread_id": "t"}}
    )
    assert _types(result["messages"]) == ["HumanMessage", "AIMessage", "ToolMessage", "HumanMessage", "AIMessage"]
    assert result["messages"][2].content == "approved"


class _RecordingFakeChat(FakeMessagesListChatModel):
    """create_react_agent に渡せる偽のチャットモデル（受け取った入力を記録する）。"""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        type(self).seen.append(list(messages))
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=result.generations[0].message)])


@pytest.mark.asyncio
async def test_prebuilt_graph_parallel_tools_then_injection(monkeypatch):
    """prebuilt 実装（ツールごとに別タスク）でも、全ツールの結果の後に指示が1件だけ入る。"""

    @tool
    def a() -> str:
        """A。"""
        user_injection.enqueue("t", "一つ目")
        return "a"

    @tool
    def b() -> str:
        """B。"""
        user_injection.enqueue("t", "二つ目")
        return "b"

    _RecordingFakeChat.seen = []
    parallel = AIMessage(
        content="", tool_calls=[{"name": "a", "args": {}, "id": "a1"}, {"name": "b", "args": {}, "id": "b1"}]
    )
    model = _RecordingFakeChat(responses=[parallel, AIMessage(content="done")])

    async def fake_build_model(config, role, wait_when_busy=True):
        return model

    monkeypatch.setattr(graph_mod, "build_model", fake_build_model)
    monkeypatch.setattr(graph_mod, "enable_thinking_control", lambda m, c, r: m)
    monkeypatch.setattr(graph_mod, "get_all_tools", lambda: [a, b])
    monkeypatch.setattr(graph_mod, "apply_session_tool_overrides", lambda t: t)
    monkeypatch.setattr(graph_mod, "filter_main_agent_tools", lambda t, c: t)
    monkeypatch.setattr(graph_mod, "ImageAwareToolNode", ToolNode)

    graph = await graph_mod._build_prebuilt_graph(_cfg(), "sp", MemorySaver())
    user_injection.open_thread("t")
    recorder = _EventRecorder()
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="始めて")]},
        config={"configurable": {"thread_id": "t"}, "callbacks": [recorder]},
    )
    history = result["messages"]
    _assert_tool_calls_answered(history)
    assert _types(history) == ["HumanMessage", "AIMessage", "ToolMessage", "ToolMessage", "HumanMessage", "AIMessage"]
    assert sorted(history[4].content[len(user_injection.INJECTION_PREFIX) :].split("\n\n")) == ["一つ目", "二つ目"]
    assert _RecordingFakeChat.seen[1][-1].content == history[4].content
    assert len(recorder.applied) == 1


async def _graph_with_committed_injection(monkeypatch, thread_id: str):
    """ツール実行中に届いた指示が会話へ確定した状態のグラフと、そのターンのトークンを返す。"""

    @tool
    def work() -> str:
        """作業する。"""
        user_injection.enqueue(thread_id, "追加")
        return "ok"

    model = _Model([_tc("work", "c1"), AIMessage(content="done")])
    _patch(monkeypatch, model, [work])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())
    token = user_injection.open_thread(thread_id)
    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke({"messages": [HumanMessage(content="始めて")]}, config=config)
    return graph, config, token


@pytest.mark.asyncio
async def test_sync_with_state_shows_injections_whose_event_was_lost(monkeypatch):
    """通知が読まれずに捨てられても（ストリームを途中で閉じた場合）、チェックポイントから表示し直す。"""
    graph, config, token = await _graph_with_committed_injection(monkeypatch, "t")
    tracker = app._TurnInjections(token)
    shown: list[list[dict]] = []

    async def fake_show(items):
        shown.append(items)
        tracker.displayed.update(i["id"] for i in items)

    monkeypatch.setattr(tracker, "show", fake_show)
    await tracker.sync_with_state(graph, config)
    assert [[i["text"] for i in items] for items in shown] == [["追加"]]

    # 別のターンのトークンでは、過去のターンの指示を出し直さない。
    other = app._TurnInjections("another-turn")
    monkeypatch.setattr(other, "show", fake_show)
    await other.sync_with_state(graph, config)
    assert len(shown) == 1


@pytest.mark.asyncio
async def test_drop_undisplayed_removes_only_unshown_injections(monkeypatch):
    """停止時: 画面に出ていない指示は会話から取り除き、出した指示は残す。"""
    graph, config, token = await _graph_with_committed_injection(monkeypatch, "t")

    shown_tracker = app._TurnInjections(token)
    state = await graph.aget_state(config)
    injected = [m for m in state.values["messages"] if user_injection.is_injection_message(m)]
    shown_tracker.displayed.update(i["id"] for i in user_injection.injection_items(injected[0]))
    await shown_tracker.drop_undisplayed(graph, config)
    assert any(user_injection.is_injection_message(m) for m in (await graph.aget_state(config)).values["messages"])

    await app._TurnInjections(token).drop_undisplayed(graph, config)
    history = (await graph.aget_state(config)).values["messages"]
    assert not any(user_injection.is_injection_message(m) for m in history)
    _assert_tool_calls_answered(history)


def test_pending_messages_picks_only_this_turns_injections_from_pending_writes():
    """確定前の書き込み（state.tasks の result）にだけある、このターンの指示を拾う。"""
    mine = user_injection.to_human_message([user_injection.Injection(id="a", text="x")], "turn-1")
    other = user_injection.to_human_message([user_injection.Injection(id="b", text="y")], "turn-0")
    state = SimpleNamespace(
        tasks=[
            SimpleNamespace(error=None, result={"messages": [mine, other, ToolMessage(content="r", tool_call_id="c")]}),
            SimpleNamespace(error=RuntimeError("x"), result={"messages": [mine]}),
            SimpleNamespace(error=None, result=None),
        ]
    )
    assert app._TurnInjections("turn-1").pending_messages(state) == [mine]
    assert app._TurnInjections(None).pending_messages(state) == []


@pytest.mark.asyncio
async def test_inject_node_requeues_when_cancelled_while_notifying(monkeypatch):
    """inject ノードが通知の await 中にキャンセルされたら、取り出した指示をキューへ戻す。"""
    started = asyncio.Event()

    async def slow_notify(config, injections):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(graph_mod, "_notify_user_injections", slow_notify)
    model = _Model([AIMessage(content="done")])
    _patch(monkeypatch, model, [])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())
    user_injection.open_thread("t")
    queued = user_injection.enqueue("t", "待って")

    await _run_until_cancelled(graph, {"messages": [HumanMessage(content="始めて")]}, {"configurable": {"thread_id": "t"}}, started)
    assert user_injection.drain("t") == [queued]
    assert model.calls == []


@pytest.mark.asyncio
async def test_instruction_sent_during_subagent_waits_and_enters_main_after_it_returns(monkeypatch):
    """サブエージェント（dispatch_agent）の実行中に送った指示は、サブエージェントには渡らず、
    その結果（ToolMessage）がメインへ返った後、メインの次のモデル呼び出しの前に入る。"""
    seen_in_subagent = {}

    @tool
    async def dispatch_agent(task: str) -> str:
        """サブエージェントへ委譲する。"""
        user_injection.enqueue("t", "結果は表で")
        # サブエージェントの内部ループの途中（この時点ではキューに残ったまま）。
        await asyncio.sleep(0)
        seen_in_subagent["queued"] = [i.text for i in user_injection._queues["t"].items]
        return "sub result"

    model = _Model([_tc("dispatch_agent", "d1", task="調べて"), AIMessage(content="done")])
    _patch(monkeypatch, model, [dispatch_agent])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())
    user_injection.open_thread("t")
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="始めて")]}, config={"configurable": {"thread_id": "t"}}
    )
    assert seen_in_subagent["queued"] == ["結果は表で"]
    history = result["messages"]
    assert _types(history) == ["HumanMessage", "AIMessage", "ToolMessage", "HumanMessage", "AIMessage"]
    assert history[2].content == "sub result"
    assert history[3].content.endswith("結果は表で")


def test_subagent_loop_never_touches_the_injection_queue():
    """サブエージェントのループは割り込みのキューを参照しない（取り出すのはメインのグラフと app.py だけ）。"""
    import inspect

    from src import subagent

    assert "user_injection" not in inspect.getsource(subagent)
