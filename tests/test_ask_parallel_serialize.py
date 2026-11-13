"""並列に呼ばれた ask系ツールの質問が1つずつ順番に出ることの回帰テスト。

LLM が同じ応答で AskUserQuestion と ask_user_choice を並列に呼ぶと、Chainlit は1セッションで
同時に1つの質問しか表示できず、後から出た質問が先の質問を上書きしていた。上書きされた質問は
画面に出ないまま応答を待ち続け、停止するまで何もできなくなっていた（src/ask_relay.py の ask_turn）。
"""

import asyncio

import pytest

import src.tools as tools
from src import ask_relay


class _FakeUserSession:
    def __init__(self, thread_id: str):
        self._data = {"thread_id": thread_id}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _Screen:
    """表示中の質問を記録し、テスト側から1つずつ回答する。"""

    def __init__(self):
        self.showing: list[str] = []
        self.max_showing = 0
        self.answers: dict[str, asyncio.Future] = {}

    async def show(self, content: str, response):
        self.showing.append(content)
        self.max_showing = max(self.max_showing, len(self.showing))
        fut = asyncio.get_running_loop().create_future()
        self.answers[content] = fut
        try:
            await fut
            return response
        finally:
            self.showing.remove(content)


_screen = _Screen()


class _FakeAction:
    def __init__(self, content=None, actions=None, timeout=90, **kwargs):
        self.content = content

    async def send(self):
        return await _screen.show(self.content, {"payload": {"value": "赤"}, "label": "赤"})

    async def update(self):
        pass


class _FakeUser:
    def __init__(self, content=None, timeout=90, **kwargs):
        self.content = content

    async def send(self):
        return await _screen.show(self.content, {"output": "東京"})


class _NoopMessage:
    def __init__(self, *args, **kwargs):
        pass

    async def send(self):
        return self


@pytest.fixture
def patched(monkeypatch):
    global _screen
    _screen = _Screen()
    thread_id = "ask-parallel-thread"
    ask_relay.pending_asks.pop(thread_id, None)
    monkeypatch.setattr(tools.cl, "AskActionMessage", _FakeAction)
    monkeypatch.setattr(tools.cl, "AskUserMessage", _FakeUser)
    monkeypatch.setattr(tools.cl, "Message", _NoopMessage)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession(thread_id))
    yield thread_id
    assert thread_id not in ask_relay._ASK_LOCKS, "使い終わったロックが残っている"


async def _wait_showing(content: str) -> None:
    for _ in range(200):
        if content in _screen.showing:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"質問が表示されない: {content!r} showing={_screen.showing}")


@pytest.mark.asyncio
async def test_parallel_asks_are_shown_one_at_a_time(patched) -> None:
    choice_task = asyncio.create_task(tools.ask_user_choice.ainvoke({"question": "色は？", "choices": ["赤", "青"]}))
    question_task = asyncio.create_task(tools.ask_user_question.ainvoke({"question": "場所は？"}))

    first = "色は？"
    await _wait_showing(first)
    await asyncio.sleep(0.05)
    assert _screen.showing == [first], "2つの質問が同時に表示された（後の質問が先の質問を上書きする）"
    _screen.answers[first].set_result(None)

    await _wait_showing("場所は？")
    _screen.answers["場所は？"].set_result(None)

    assert await choice_task == "赤"
    assert await question_task == "東京"
    assert _screen.max_showing == 1


@pytest.mark.asyncio
async def test_cancelled_ask_releases_turn(patched) -> None:
    """停止（タスクの取り消し）で待っていた質問を止めても、次の質問が出られる。"""
    first = asyncio.create_task(tools.ask_user_question.ainvoke({"question": "場所は？"}))
    await _wait_showing("場所は？")
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(tools.ask_user_choice.ainvoke({"question": "色は？", "choices": ["赤", "青"]}))
    await _wait_showing("色は？")
    _screen.answers["色は？"].set_result(None)
    assert await second == "赤"
