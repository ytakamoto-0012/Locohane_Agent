"""メインエージェントの ImageAwareToolNode における並列実行数ガードの回帰テスト。

LangGraph の ToolNode は同一AIMessage内の複数tool_callsを asyncio.gather() で
完全並列実行する。dispatch_agent 専用の _DISPATCH_AGENT_SEMAPHORES と同じ理由づけ
で、メインエージェント側の全ツール呼び出し（ImageAwareToolNode 経由）についても
_TOOL_CALL_SEMAPHORES（config.ini の [graph].max_parallel 由来）が同時実行数を
制御することを検証する。同期 def ツール（別スレッドの ThreadPoolExecutor 上で
実行される）についても、awrap_tool_call 経由で正しくガードされることを合わせて
確認する（tool.func/coroutine を直接書き換える方式では効かない点の回帰確認）。

_TOOL_CALL_SEMAPHORES はセッション（llm.get_current_session()）ごとに独立した
Semaphore を遅延生成する辞書であるため、各テストは monkeypatch で辞書を空に
差し替えてから実行し、他テストが同じセッションキー（未設定時は None）で
生成した Semaphore を再利用してしまわないようにする。
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph

from src import llm, tools


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def _setup(monkeypatch) -> None:
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    # 他テストが同じセッションキー（未設定時は None）で生成した Semaphore を
    # 再利用しないよう、辞書を空の状態から始める。
    monkeypatch.setattr(tools._state, "_TOOL_CALL_SEMAPHORES", {})


def _make_node(*, include_sync_tool: bool = False):
    concurrent = 0
    max_concurrent = 0

    @tool
    async def dummy_a(x: str) -> str:
        """ダミー非同期ツールA。"""
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return f"done:{x}"

    @tool
    async def dummy_b(x: str) -> str:
        """ダミー非同期ツールB。"""
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return f"done:{x}"

    @tool
    def dummy_c_sync(x: str) -> str:
        """ダミー同期ツールC（別スレッドで実行される）。"""
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        import time

        time.sleep(0.05)
        concurrent -= 1
        return f"done:{x}"

    dummy_tools = [dummy_a, dummy_b]
    if include_sync_tool:
        dummy_tools.append(dummy_c_sync)

    node = tools.ImageAwareToolNode(dummy_tools)

    # ImageAwareToolNode（ToolNode）はグラフ実行時の Runtime コンテキストを
    # 要求するため、単体で ainvoke するのではなく、最小の StateGraph に
    # ノードとして組み込んでコンパイルしたグラフ経由で実行する
    # （本番の src/graph.py と同じ組み込み方）。
    graph = StateGraph(MessagesState)
    graph.add_node("tools", node)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    compiled = graph.compile()

    def get_max_concurrent() -> int:
        return max_concurrent

    return compiled, get_max_concurrent


def _tool_call_input(names: list[str]) -> dict:
    tool_calls = [{"name": name, "args": {"x": name}, "id": f"call-{i}"} for i, name in enumerate(names)]
    return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}


@pytest.mark.asyncio
async def test_main_toolnode_default_max_parallel_serializes_calls(monkeypatch) -> None:
    _setup(monkeypatch)
    node, get_max_concurrent = _make_node()
    # モジュールデフォルト（init_tools() 未実行時）は max_parallel=1 ＝ 完全直列化。

    await node.ainvoke(_tool_call_input(["dummy_a", "dummy_b"]))

    assert get_max_concurrent() == 1


@pytest.mark.asyncio
async def test_main_toolnode_max_parallel_two_caps_concurrency(monkeypatch) -> None:
    _setup(monkeypatch)
    monkeypatch.setattr(tools._state, "_TOOL_CALL_MAX_PARALLEL", 2)
    node, get_max_concurrent = _make_node()

    await node.ainvoke(_tool_call_input(["dummy_a", "dummy_b"]))

    assert get_max_concurrent() == 2


@pytest.mark.asyncio
async def test_main_toolnode_max_parallel_disabled_allows_full_concurrency(monkeypatch) -> None:
    _setup(monkeypatch)
    monkeypatch.setattr(tools._state, "_TOOL_CALL_MAX_PARALLEL", 0)
    node, get_max_concurrent = _make_node()

    await node.ainvoke(_tool_call_input(["dummy_a", "dummy_b"]))

    assert get_max_concurrent() == 2


@pytest.mark.asyncio
async def test_main_toolnode_guards_sync_tool_too(monkeypatch) -> None:
    """同期 def ツールも awrap_tool_call 経由で正しくガードされることを確認する。"""
    _setup(monkeypatch)
    monkeypatch.setattr(tools._state, "_TOOL_CALL_MAX_PARALLEL", 1)
    node, get_max_concurrent = _make_node(include_sync_tool=True)

    await node.ainvoke(_tool_call_input(["dummy_a", "dummy_b", "dummy_c_sync"]))

    assert get_max_concurrent() == 1


@pytest.mark.asyncio
async def test_main_toolnode_max_parallel_is_independent_per_session(monkeypatch) -> None:
    """max_parallel=1 でも、別セッション（thread_id）同士は互いに待ち合わない。

    _TOOL_CALL_SEMAPHORES はセッションごとに独立した Semaphore を持つため、
    セッションAの直列化がセッションBの実行をブロックしないことを確認する。
    """
    _setup(monkeypatch)
    monkeypatch.setattr(tools._state, "_TOOL_CALL_MAX_PARALLEL", 1)
    node_a, get_max_concurrent_a = _make_node()
    node_b, get_max_concurrent_b = _make_node()

    async def run_session(thread_id: str, node) -> None:
        llm.set_current_session(thread_id)
        await node.ainvoke(_tool_call_input(["dummy_a", "dummy_b"]))

    # 2セッションを並行実行しても、それぞれの中では直列化（max_concurrent==1）
    # されつつ、セッション間では互いを待たない（全体の所要時間が2セッション分の
    # 直列合計にならない）ことを time 計測込みで確認する。
    loop = asyncio.get_event_loop()
    start = loop.time()
    await asyncio.gather(
        run_session("session-a", node_a),
        run_session("session-b", node_b),
    )
    elapsed = loop.time() - start

    assert get_max_concurrent_a() == 1
    assert get_max_concurrent_b() == 1
    # 各セッション内は2回の asyncio.sleep(0.05) を直列実行するため約0.1秒。
    # セッション間で待ち合っていれば約0.2秒（4回分の直列）になるはず。
    assert elapsed < 0.18


@pytest.mark.asyncio
async def test_main_toolnode_does_not_hold_dispatch_agent_before_start(monkeypatch) -> None:
    """max_parallel=1 でも dispatch_agent はこの枠で開始前に待たされない。

    開始前に待たされると on_tool_start が出ず、同じ応答で並べた委譲のStep（バッジ）が
    画面に出なかったため。同時実行数はツール内の [subagent].max_parallel が別途ガードする。
    """
    _setup(monkeypatch)
    monkeypatch.setattr(tools._state, "_TOOL_CALL_MAX_PARALLEL", 1)
    started: list[str] = []
    release = asyncio.Event()

    @tool("dispatch_agent")
    async def fake_dispatch_agent(x: str) -> str:
        """ダミーの dispatch_agent。"""
        started.append(x)
        await release.wait()
        return "ok"

    @tool
    async def dummy_a(x: str) -> str:
        """ダミー非同期ツールA。"""
        started.append(x)
        await release.wait()
        return "ok"

    graph = StateGraph(MessagesState)
    graph.add_node("tools", tools.ImageAwareToolNode([fake_dispatch_agent, dummy_a]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    compiled = graph.compile()
    tool_calls = [
        {"name": "dispatch_agent", "args": {"x": "d1"}, "id": "call-0"},
        {"name": "dispatch_agent", "args": {"x": "d2"}, "id": "call-1"},
        {"name": "dummy_a", "args": {"x": "a"}, "id": "call-2"},
    ]

    run = asyncio.create_task(compiled.ainvoke({"messages": [AIMessage(content="", tool_calls=tool_calls)]}))
    for _ in range(50):
        if len(started) == 3:
            break
        await asyncio.sleep(0.01)
    # どれも終わらせないうちに、3つとも開始している（dummy_a が枠を1つ使い、dispatch_agent は枠を使わない）。
    started_before_release = sorted(started)
    release.set()
    await run

    assert started_before_release == ["a", "d1", "d2"]


def _graph_with(tool_list):
    graph = StateGraph(MessagesState)
    graph.add_node("tools", tools.ImageAwareToolNode(tool_list))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile()


@pytest.mark.asyncio
async def test_dispatch_agent_with_lookup_in_same_response_is_not_started(monkeypatch) -> None:
    """メモリー検索・Glob 等と同じ応答の委譲は開始せず差し戻す（本番ログ 2026-11-03）。

    委譲の依頼文がそれらの結果を見ずに書かれており、同時に動かしても速くならないため。
    """
    _setup(monkeypatch)
    ran: list[str] = []

    @tool("dispatch_agent")
    async def fake_dispatch_agent(x: str) -> str:
        """ダミーの dispatch_agent。"""
        ran.append("dispatch_agent")
        return "ok"

    @tool("search_memory")
    async def fake_search_memory(x: str) -> str:
        """ダミーの search_memory。"""
        ran.append("search_memory")
        return "No matching memories."

    compiled = _graph_with([fake_dispatch_agent, fake_search_memory])
    tool_calls = [
        {"name": "search_memory", "args": {"x": "q"}, "id": "call-0"},
        {"name": "dispatch_agent", "args": {"x": "d"}, "id": "call-1"},
    ]
    result = await compiled.ainvoke({"messages": [AIMessage(content="", tool_calls=tool_calls)]})

    assert ran == ["search_memory"]
    by_id = {m.tool_call_id: m for m in result["messages"] if getattr(m, "tool_call_id", None)}
    assert by_id["call-1"].content.startswith("Error: not started.")
    assert "search_memory" in by_id["call-1"].content
    assert by_id["call-0"].content == "No matching memories."


@pytest.mark.asyncio
async def test_dispatch_agents_with_progress_update_run_together(monkeypatch) -> None:
    """委譲同士と、進捗更新など同じ応答に並べる手順のツールは差し戻さない。"""
    _setup(monkeypatch)
    ran: list[str] = []

    @tool("dispatch_agent")
    async def fake_dispatch_agent(x: str) -> str:
        """ダミーの dispatch_agent。"""
        ran.append(x)
        return "ok"

    @tool("update_task_progress")
    async def fake_update_task_progress(x: str) -> str:
        """ダミーの update_task_progress。"""
        ran.append(x)
        return "ok"

    compiled = _graph_with([fake_dispatch_agent, fake_update_task_progress])
    tool_calls = [
        {"name": "update_task_progress", "args": {"x": "p"}, "id": "call-0"},
        {"name": "dispatch_agent", "args": {"x": "d1"}, "id": "call-1"},
        {"name": "dispatch_agent", "args": {"x": "d2"}, "id": "call-2"},
    ]
    await compiled.ainvoke({"messages": [AIMessage(content="", tool_calls=tool_calls)]})

    assert sorted(ran) == ["d1", "d2", "p"]
