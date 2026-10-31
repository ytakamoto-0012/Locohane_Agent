"""app._remove_message_ids_if_present の回帰テスト。

コンテキスト圧縮（_run_context_compaction）が発火すると、loop_nudge_ids /
empty_nudge_ids に蓄積済みのidが現在のグラフstateから消える（要約に置き換わる、
または新しいuuidを振った複製に置き換わる）ことがある。存在しないidを
RemoveMessage(id=...) に渡すと langgraph の add_messages リデューサが
ValueError を送出し、on_message の外まで伝播していた（本番ログ:
app.py:2103 のトレースバック、"Attempting to delete a message with an ID
that doesn't exist"）。_remove_message_ids_if_present は、現在のstateに
実在するidだけへ絞り込むことでこれを防ぐ。
"""

import pytest
from langchain_core.messages import AIMessage

from app import _CheckpointerTimeout, _remove_message_ids_if_present


class _FakeState:
    def __init__(self, messages):
        self.values = {"messages": messages}


class _FakeGraph:
    def __init__(self, live_ids):
        self.live_ids = set(live_ids)
        self.updated_states: list = []

    async def aget_state(self, config):
        return _FakeState([AIMessage(content="x", id=i) for i in self.live_ids])

    async def aupdate_state(self, config, values):
        self.updated_states.append(values)
        for m in values.get("messages", []):
            self.live_ids.discard(m.id)


@pytest.mark.asyncio
async def test_removes_ids_that_still_exist() -> None:
    graph = _FakeGraph(live_ids={"a", "b"})

    await _remove_message_ids_if_present(graph, {}, ["a", "b"])

    assert graph.live_ids == set()
    assert len(graph.updated_states) == 1


@pytest.mark.asyncio
async def test_skips_stale_ids_without_error() -> None:
    """圧縮等で既に無効化されたidを渡してもValueErrorにならず無視される
    （本番incident app.py:2103 の回帰防止）。"""
    graph = _FakeGraph(live_ids={"a"})  # "b" は既に消えている想定

    await _remove_message_ids_if_present(graph, {}, ["a", "b"])

    assert [m.id for m in graph.updated_states[0]["messages"]] == ["a"]


@pytest.mark.asyncio
async def test_all_ids_stale_does_not_call_aupdate_state() -> None:
    graph = _FakeGraph(live_ids=set())

    await _remove_message_ids_if_present(graph, {}, ["ghost-1", "ghost-2"])

    assert graph.updated_states == []


@pytest.mark.asyncio
async def test_empty_ids_is_noop() -> None:
    graph = _FakeGraph(live_ids={"a"})

    await _remove_message_ids_if_present(graph, {}, [])

    assert graph.updated_states == []
    assert graph.live_ids == {"a"}


@pytest.mark.asyncio
async def test_checkpointer_timeout_is_swallowed() -> None:
    class _TimeoutGraph:
        async def aget_state(self, config):
            raise _CheckpointerTimeout("locked")

    await _remove_message_ids_if_present(_TimeoutGraph(), {}, ["a"])  # must not raise
