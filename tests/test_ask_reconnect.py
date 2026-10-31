"""同じセッションの WebSocket 再接続後に、回答待ちの ask が新しい接続へ
送り直されることの回帰テスト（src/ask_reconnect.py 参照）。

背景（2026-10-16 ユーザー報告）: 質問のボタン・入力欄をしばらく放置すると
消えていた。再接続で Chainlit が clear_ask を送る一方、サーバー側の
sio.call は切断済みの古い sid 宛のまま待ち続け、二度と ask を送らなかったため。
"""

import asyncio

import pytest
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from src import ask_reconnect


class _FakeSession:
    """restore 後に emit/emit_call が新しい sid 宛へ差し替わるのを模したセッション。"""

    def __init__(self, session_id: str = "s1"):
        self.id = session_id
        self.sid = "sid-1"
        self.calls: list[tuple[str, str]] = []
        self.emitted: list[tuple[str, str, dict]] = []
        self.answers: dict[str, asyncio.Future] = {}

    async def emit_call(self, event, data, timeout):
        sid = self.sid
        self.calls.append((sid, event))
        fut = asyncio.get_running_loop().create_future()
        self.answers[sid] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # 本物の sio.call と同じ例外へ変換する。
            raise SocketIOTimeoutError() from None

    async def emit(self, event, data):
        self.emitted.append((self.sid, event, data))


async def _wait_calls(session: _FakeSession, n: int) -> None:
    for _ in range(100):
        if len(session.calls) >= n:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"emit_call が {n} 回呼ばれませんでした: {session.calls}")


@pytest.mark.asyncio
async def test_ask_is_resent_to_new_socket_after_reconnect():
    session = _FakeSession()
    action = {"id": "a1", "name": "choice_0", "forId": "step1", "label": "はい"}
    ask_reconnect._sent_actions["a1"] = action
    data = {"msg": {"id": "step1"}, "spec": {"type": "action", "step_id": "step1", "keys": ["a1"]}}

    task = asyncio.create_task(ask_reconnect._resilient_ask_call(session, data, 1000))
    await _wait_calls(session, 1)

    # 再接続（restore で sid が変わり、connection_successful の後に通知される）。
    session.sid = "sid-2"
    ask_reconnect.notify_reconnected(session)
    await _wait_calls(session, 2)

    assert session.calls == [("sid-1", "ask"), ("sid-2", "ask")]
    # ボタンは重複しないよう remove してから送り直す。
    assert [(sid, ev) for sid, ev, _ in session.emitted] == [("sid-2", "remove_action"), ("sid-2", "action")]

    session.answers["sid-2"].set_result({"name": "choice_0", "label": "はい"})
    assert await task == {"name": "choice_0", "label": "はい"}
    assert "a1" not in ask_reconnect._sent_actions


@pytest.mark.asyncio
async def test_custom_element_is_resent_after_reconnect():
    session = _FakeSession()
    element = {"id": "e1", "type": "custom", "name": "MultiChoiceForm"}
    ask_reconnect._sent_custom_elements["e1"] = element
    data = {"msg": {"id": "step1"}, "spec": {"type": "element", "step_id": "step1", "element_id": "e1"}}

    task = asyncio.create_task(ask_reconnect._resilient_ask_call(session, data, 1000))
    await _wait_calls(session, 1)
    session.sid = "sid-2"
    ask_reconnect.notify_reconnected(session)
    await _wait_calls(session, 2)

    assert session.emitted == [("sid-2", "element", element)]
    session.answers["sid-2"].set_result({"submitted": True, "values": []})
    assert await task == {"submitted": True, "values": []}
    assert "e1" not in ask_reconnect._sent_custom_elements


@pytest.mark.asyncio
async def test_answer_without_reconnect_is_returned_as_is():
    session = _FakeSession()
    data = {"msg": {"id": "step1"}, "spec": {"type": "text", "step_id": "step1"}}

    task = asyncio.create_task(ask_reconnect._resilient_ask_call(session, data, 1000))
    await _wait_calls(session, 1)
    session.answers["sid-1"].set_result({"output": "回答"})

    assert await task == {"output": "回答"}
    assert session.calls == [("sid-1", "ask")]
    assert session.emitted == []


@pytest.mark.asyncio
async def test_reconnect_of_other_session_does_not_resend():
    session = _FakeSession("s1")
    other = _FakeSession("s2")
    data = {"msg": {"id": "step1"}, "spec": {"type": "text", "step_id": "step1"}}

    task = asyncio.create_task(ask_reconnect._resilient_ask_call(session, data, 1000))
    await _wait_calls(session, 1)
    ask_reconnect.notify_reconnected(other)
    await asyncio.sleep(0.01)

    assert session.calls == [("sid-1", "ask")]
    session.answers["sid-1"].set_result({"output": "回答"})
    assert await task == {"output": "回答"}


@pytest.mark.asyncio
async def test_timeout_spans_reconnects():
    session = _FakeSession()
    data = {"msg": {"id": "step1"}, "spec": {"type": "text", "step_id": "step1"}}

    task = asyncio.create_task(ask_reconnect._resilient_ask_call(session, data, 0.2))
    await _wait_calls(session, 1)
    session.sid = "sid-2"
    ask_reconnect.notify_reconnected(session)

    with pytest.raises(SocketIOTimeoutError):
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_cancel_cancels_pending_call():
    session = _FakeSession()
    data = {"msg": {"id": "step1"}, "spec": {"type": "text", "step_id": "step1"}}

    task = asyncio.create_task(ask_reconnect._resilient_ask_call(session, data, 1000))
    await _wait_calls(session, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.answers["sid-1"].cancelled()


def test_install_routes_only_ask_through_resilient_call(monkeypatch):
    from chainlit.emitter import ChainlitEmitter

    ask_reconnect.install()
    ask_reconnect.install()  # 二重適用しない

    session = _FakeSession()
    emitter = ChainlitEmitter.__new__(ChainlitEmitter)
    emitter.session = session

    async def run():
        task = asyncio.create_task(emitter.emit_call("ask", {"spec": {"type": "text"}}, 1000))
        await _wait_calls(session, 1)
        session.sid = "sid-2"
        ask_reconnect.notify_reconnected(session)
        await _wait_calls(session, 2)
        session.answers["sid-2"].set_result("ok")
        assert await task == "ok"

        call_fn = asyncio.create_task(emitter.emit_call("call_fn", {}, 1000))
        await _wait_calls(session, 3)
        ask_reconnect.notify_reconnected(session)
        await asyncio.sleep(0.01)
        assert len(session.calls) == 3  # call_fn は送り直さない
        session.answers["sid-2"].set_result("fn")
        assert await call_fn == "fn"

    asyncio.run(run())
