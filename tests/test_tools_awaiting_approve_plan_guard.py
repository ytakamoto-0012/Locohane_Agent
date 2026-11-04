"""create_plan 直後のガード（src/tools/tool_node.py の _guard_awaiting_approve_plan）のテスト。

2026-11-04 evals 001: 承認前に自分の計画の誤り（推測で書いた基準日）に気付いたメインが
create_plan で作り直そうとしたが、ガードが create_plan まで止め、同じ差し戻しを3回繰り返した。
"""

from src import tools
from src.tools import tool_node


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {"awaiting_approve_plan_call": True}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def _call(name: str) -> dict:
    return {"__type": "tool_call_with_context", "tool_call": {"name": name, "args": {}, "id": "c1"}, "state": {}}


def test_create_plan_again_is_allowed_and_keeps_waiting(monkeypatch):
    session = _FakeUserSession()
    monkeypatch.setattr(tools.cl, "user_session", session)
    assert tool_node._guard_awaiting_approve_plan(_call("create_plan")) is None
    assert session.get("awaiting_approve_plan_call") is True


def test_other_tools_are_blocked_with_hint(monkeypatch):
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    result = tool_node._guard_awaiting_approve_plan(_call("dispatch_agent"))
    message = result["messages"][0]
    assert "call approve_plan right after create_plan" in message.content
    assert "call create_plan again" in message.content


def test_approve_plan_clears_the_flag(monkeypatch):
    session = _FakeUserSession()
    monkeypatch.setattr(tools.cl, "user_session", session)
    assert tool_node._guard_awaiting_approve_plan(_call("approve_plan")) is None
    assert session.get("awaiting_approve_plan_call") is False
