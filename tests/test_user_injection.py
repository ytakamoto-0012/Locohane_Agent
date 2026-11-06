"""生成中のユーザーの追加指示（割り込み）のテスト（src/user_injection.py・src/graph.py）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode

from src import graph as graph_mod
from src import user_injection


@pytest.fixture(autouse=True)
def _clear_queues():
    user_injection._queues.clear()
    yield
    user_injection._queues.clear()


def _cfg():
    return SimpleNamespace(
        context_trim_enabled=False,
        graph_token_guard_enabled=False,
        context_compaction_enabled=False,
        context_compaction_pre_note_threshold=0,
        tool_loop_guard_enabled=False,
        tool_loop_guard_max_repeats=3,
        tool_loop_guard_exclude_tools=[],
    )


def test_enqueue_is_rejected_unless_the_thread_is_open():
    assert user_injection.enqueue("t1", "hello") is None
    user_injection.open_thread("t1")
    injection = user_injection.enqueue("t1", "hello")
    assert injection is not None and injection.text == "hello"
    assert user_injection.drain("t1") == [injection]
    assert user_injection.drain("t1") == []
    assert user_injection.is_open("t1")


def test_close_thread_returns_leftovers_and_stops_accepting():
    user_injection.open_thread("t1")
    first = user_injection.enqueue("t1", "a")
    assert user_injection.close_thread("t1") == [first]
    assert not user_injection.is_open("t1")
    assert user_injection.enqueue("t1", "b") is None
    assert user_injection.drain("t1") == []


def test_open_thread_keeps_existing_queue():
    user_injection.open_thread("t1")
    first = user_injection.enqueue("t1", "a")
    user_injection.open_thread("t1")
    assert user_injection.drain("t1") == [first]


def test_drain_without_thread_id_is_noop():
    assert user_injection.drain(None) == []


def test_to_human_message_marks_and_prefixes():
    message = user_injection.to_human_message([user_injection.Injection(id="x", text="CSVで出して")], "turn-1")
    assert isinstance(message, HumanMessage)
    assert message.id == "x"
    assert message.content.endswith("CSVで出して")
    assert message.content.startswith(user_injection.INJECTION_PREFIX)
    assert user_injection.is_injection_message(message)
    assert user_injection.injection_items(message) == [{"id": "x", "text": "CSVで出して", "files": []}]
    assert user_injection.injection_items(message, "turn-1") == [{"id": "x", "text": "CSVで出して", "files": []}]
    assert user_injection.injection_items(message, "turn-2") == []
    assert not user_injection.is_injection_message(HumanMessage(content="普通の発言"))


def test_held_queue_keeps_accepting_but_does_not_drain():
    user_injection.open_thread("t1")
    user_injection.set_held("t1", True)
    queued = user_injection.enqueue("t1", "a")
    assert queued is not None
    assert user_injection.drain("t1") == []
    user_injection.set_held("t1", False)
    assert user_injection.drain("t1") == [queued]


def test_close_with_stale_token_keeps_the_new_turn_open():
    old = user_injection.open_thread("t1")
    user_injection.close_thread("t1")  # 停止ボタン
    new = user_injection.open_thread("t1")
    assert old != new
    assert user_injection.close_thread("t1", old) == []
    assert user_injection.current_token("t1") == new


def test_to_human_message_merges_multiple_instructions_into_one():
    message = user_injection.to_human_message(
        [user_injection.Injection(id="a", text="一つ目"), user_injection.Injection(id="b", text="二つ目")]
    )
    assert message.id == "a"
    assert message.content == user_injection.INJECTION_PREFIX + "一つ目\n\n二つ目"


def test_enqueue_with_same_id_is_not_duplicated():
    user_injection.open_thread("t1")
    first = user_injection.enqueue("t1", "a", "id-1")
    again = user_injection.enqueue("t1", "a", "id-1")
    assert first is again
    assert [i.id for i in user_injection.drain("t1")] == ["id-1"]


def test_requeue_puts_back_in_front_only_while_open():
    user_injection.open_thread("t1")
    first = user_injection.enqueue("t1", "a")
    taken = user_injection.drain("t1")
    later = user_injection.enqueue("t1", "b")
    user_injection.requeue("t1", taken)
    assert user_injection.drain("t1") == [first, later]
    user_injection.close_thread("t1")
    user_injection.requeue("t1", [first])
    assert not user_injection.is_open("t1")


class _ScriptedModel:
    """1回目はツールを呼び、2回目は最終回答を返す偽モデル。受け取った入力を記録する。"""

    def __init__(self):
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return AIMessage(content="", tool_calls=[{"name": "work", "args": {}, "id": "call-1"}])
        return AIMessage(content="done")


class _EventRecorder(AsyncCallbackHandler):
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def on_custom_event(self, name, data, **kwargs):
        self.events.append((name, data))


def _patch_graph_deps(monkeypatch, model, tools):
    async def fake_build_model(config, role, wait_when_busy=True):
        return model

    monkeypatch.setattr(graph_mod, "build_model", fake_build_model)
    monkeypatch.setattr(graph_mod, "enable_thinking_control", lambda m, c, r: m)
    monkeypatch.setattr(graph_mod, "get_all_tools", lambda: tools)
    monkeypatch.setattr(graph_mod, "apply_session_tool_overrides", lambda t: t)
    monkeypatch.setattr(graph_mod, "filter_main_agent_tools", lambda t, c: t)
    # ImageAwareToolNode は Chainlit のセッションを参照するため、標準の ToolNode にする。
    monkeypatch.setattr(graph_mod, "ImageAwareToolNode", ToolNode)


@pytest.mark.asyncio
async def test_handwritten_graph_injects_instruction_after_tool_results(monkeypatch):
    """ツール実行中に届いた指示は、ToolMessage の後・次のモデル呼び出しの前に会話へ入る。"""

    @tool
    def work() -> str:
        """作業する。"""
        # ツールの実行中にユーザーが追加指示を送った想定。
        user_injection.enqueue("t1", "結果はCSVで")
        return "ok"

    model = _ScriptedModel()
    _patch_graph_deps(monkeypatch, model, [work])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())

    user_injection.open_thread("t1")
    recorder = _EventRecorder()
    run_config = {"configurable": {"thread_id": "t1"}, "callbacks": [recorder]}
    result = await graph.ainvoke({"messages": [HumanMessage(content="始めて")]}, config=run_config)

    second_input = model.calls[1]
    assert isinstance(second_input[-2], ToolMessage)
    assert isinstance(second_input[-1], HumanMessage)
    assert second_input[-1].content.endswith("結果はCSVで")
    # チェックポイントにも残る（次のターン以降も文脈として見える）。
    history = result["messages"]
    assert [type(m).__name__ for m in history] == ["HumanMessage", "AIMessage", "ToolMessage", "HumanMessage", "AIMessage"]
    # 画面へ出すための custom event が1回だけ届く。
    applied = [data for name, data in recorder.events if name == user_injection.INJECTION_APPLIED_EVENT]
    assert len(applied) == 1
    assert [i["text"] for i in applied[0]["injections"]] == ["結果はCSVで"]
    assert user_injection.drain("t1") == []


@pytest.mark.asyncio
async def test_handwritten_graph_merges_instructions_sent_together(monkeypatch):
    @tool
    def work() -> str:
        """作業する。"""
        user_injection.enqueue("t4", "一つ目")
        user_injection.enqueue("t4", "二つ目")
        return "ok"

    model = _ScriptedModel()
    _patch_graph_deps(monkeypatch, model, [work])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())
    user_injection.open_thread("t4")
    recorder = _EventRecorder()
    await graph.ainvoke(
        {"messages": [HumanMessage(content="始めて")]},
        config={"configurable": {"thread_id": "t4"}, "callbacks": [recorder]},
    )
    second_input = model.calls[1]
    assert isinstance(second_input[-2], ToolMessage)
    assert second_input[-1].content.endswith("一つ目\n\n二つ目")
    applied = [data for name, data in recorder.events if name == user_injection.INJECTION_APPLIED_EVENT]
    assert [i["text"] for i in applied[0]["injections"]] == ["一つ目", "二つ目"]


@pytest.mark.asyncio
async def test_handwritten_inject_node_writes_nothing_without_injection(monkeypatch):
    """指示が無いときは inject ノードが messages を書かない（毎ステップ履歴全体を保存し直さない）。"""

    @tool
    def work() -> str:
        """作業する。"""
        return "ok"

    model = _ScriptedModel()
    _patch_graph_deps(monkeypatch, model, [work])
    checkpointer = MemorySaver()
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", checkpointer)
    run_config = {"configurable": {"thread_id": "t5"}}
    await graph.ainvoke({"messages": [HumanMessage(content="始めて")]}, config=run_config)
    inject_writes = [
        w for w in checkpointer.writes.values() for (_task, channel, _value, *_rest) in [*w.values()]
        if channel == "messages"
    ]
    # messages を書くのは入力（START）・agent 2回・tools 1回だけ。
    assert len(inject_writes) == 4


@pytest.mark.asyncio
async def test_handwritten_graph_without_injection_is_unchanged(monkeypatch):
    @tool
    def work() -> str:
        """作業する。"""
        return "ok"

    model = _ScriptedModel()
    _patch_graph_deps(monkeypatch, model, [work])
    graph = await graph_mod._build_handwritten_graph(_cfg(), "sp", MemorySaver())

    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="始めて")]}, config={"configurable": {"thread_id": "t2"}}
    )
    assert [type(m).__name__ for m in result["messages"]] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]


@pytest.mark.asyncio
async def test_prebuilt_hook_adds_injection_to_state_and_llm_input(monkeypatch):
    captured = {}

    def fake_create_react_agent(model, tools, prompt, pre_model_hook, checkpointer):
        captured["hook"] = pre_model_hook
        return object()

    async def fake_build_model(config, role, wait_when_busy=True):
        return object()

    monkeypatch.setattr(graph_mod, "create_react_agent", fake_create_react_agent)
    monkeypatch.setattr(graph_mod, "build_model", fake_build_model)
    monkeypatch.setattr(graph_mod, "enable_thinking_control", lambda m, c, r: m)
    monkeypatch.setattr(graph_mod, "filter_main_agent_tools", lambda tools, c: [])
    monkeypatch.setattr(graph_mod, "ImageAwareToolNode", lambda tools: None)

    await graph_mod._build_prebuilt_graph(_cfg(), "sp", None)
    hook = captured["hook"]
    assert hook is not None

    history = [HumanMessage(content="始めて")]
    no_injection = await hook({"messages": history}, {"configurable": {"thread_id": "t3"}})
    assert "messages" not in no_injection
    assert no_injection["llm_input_messages"] == history

    user_injection.open_thread("t3")
    user_injection.enqueue("t3", "急いで")
    update = await hook({"messages": history}, {"configurable": {"thread_id": "t3"}})
    assert len(update["messages"]) == 1 and update["messages"][0].content.endswith("急いで")
    assert update["llm_input_messages"][-1] is update["messages"][0]


@pytest.mark.asyncio
async def test_prebuilt_hook_requeues_when_preparing_input_fails(monkeypatch):
    """フック内で例外が出ると更新は確定しないため、取り出した指示はキューへ戻し、表示の通知もしない。"""
    captured = {}

    def fake_create_react_agent(model, tools, prompt, pre_model_hook, checkpointer):
        captured["hook"] = pre_model_hook
        return object()

    async def fake_build_model(config, role, wait_when_busy=True):
        return object()

    def boom(messages, config):
        raise RuntimeError("boom")

    notified = []

    async def fake_notify(config, injections):
        notified.append(injections)

    monkeypatch.setattr(graph_mod, "create_react_agent", fake_create_react_agent)
    monkeypatch.setattr(graph_mod, "build_model", fake_build_model)
    monkeypatch.setattr(graph_mod, "enable_thinking_control", lambda m, c, r: m)
    monkeypatch.setattr(graph_mod, "filter_main_agent_tools", lambda tools, c: [])
    monkeypatch.setattr(graph_mod, "ImageAwareToolNode", lambda tools: None)
    monkeypatch.setattr(graph_mod, "maybe_append_token_guard", boom)
    monkeypatch.setattr(graph_mod, "_notify_user_injections", fake_notify)

    await graph_mod._build_prebuilt_graph(_cfg(), "sp", None)
    user_injection.open_thread("t6")
    queued = user_injection.enqueue("t6", "急いで")
    with pytest.raises(RuntimeError):
        await captured["hook"]({"messages": [HumanMessage(content="始めて")]}, {"configurable": {"thread_id": "t6"}})
    assert notified == []
    assert user_injection.drain("t6") == [queued]
