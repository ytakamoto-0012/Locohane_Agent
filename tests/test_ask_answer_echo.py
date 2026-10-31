"""ask_user_choice / AskUserQuestion で送信した内容がチャット窓に表示されることの回帰テスト。

Chainlit の AskActionMessage / AskElementMessage は回答後に質問の吹き出しを
「**Selected:** ラベル」「Thanks for submitting」へ書き換えるだけで、送信内容が
チャットに残らなかった。src/tools/_ask_echo.py が質問文を戻し、送信内容を
type="user_message" のメッセージとして送ることを確認する。
"""

import pytest

import app
import src.tools as tools


class _FakeUserSession:
    def __init__(self, thread_id: str):
        self._data = {"thread_id": thread_id}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _SentMessages:
    """cl.Message の差し替え。send() されたメッセージを記録する。"""

    sent: list = []

    def __init__(self, content="", author=None, type="assistant_message", **kwargs):
        self.content = content
        self.author = author
        self.type = type

    async def send(self):
        _SentMessages.sent.append(self)
        return self


class _FakeAsk:
    """cl.AskActionMessage / AskElementMessage の差し替え。Chainlit と同じく
    回答後に content を書き換え、update() 時点の content を記録する。"""

    response = None
    instances: list = []

    def __init__(self, content=None, actions=None, element=None, timeout=90, **kwargs):
        self.content = content
        self.updated_content = None
        _FakeAsk.instances.append(self)

    async def send(self):
        self.content = "**Selected:** (Chainlitの書き換え)"
        return _FakeAsk.response

    async def update(self):
        self.updated_content = self.content


class _FakeCustomElement:
    def __init__(self, *args, **kwargs):
        pass


@pytest.fixture
def patched(monkeypatch):
    thread_id = "ask-answer-echo-thread"
    app._pending_asks.pop(thread_id, None)
    _SentMessages.sent = []
    _FakeAsk.instances = []
    _FakeAsk.response = None
    monkeypatch.setattr(tools.cl, "AskActionMessage", _FakeAsk)
    monkeypatch.setattr(tools.cl, "AskElementMessage", _FakeAsk)
    monkeypatch.setattr(tools.cl, "CustomElement", _FakeCustomElement)
    monkeypatch.setattr(tools.cl, "Message", _SentMessages)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession(thread_id))


def _echoed() -> list[str]:
    return [m.content for m in _SentMessages.sent if m.type == "user_message"]


@pytest.mark.asyncio
async def test_single_choice_echoes_selected_label(patched) -> None:
    _FakeAsk.response = {"payload": {"value": "赤"}, "label": "赤"}

    result = await tools.ask_user_choice.ainvoke({"question": "色は？", "choices": ["赤", "青"]})

    assert result == "赤"
    assert _echoed() == ["赤"]
    assert _FakeAsk.instances[0].updated_content == "色は？", "質問の吹き出しが質問文に戻っていない"


@pytest.mark.asyncio
async def test_single_choice_cancel_echoes_cancel_label(patched) -> None:
    _FakeAsk.response = {"payload": {"value": "__cancel__"}, "label": "❌ キャンセル"}

    result = await tools.ask_user_choice.ainvoke({"question": "色は？", "choices": ["赤", "青"]})

    assert result.startswith("Error: the user cancelled the choice")
    assert _echoed() == ["❌ キャンセル"]


@pytest.mark.asyncio
async def test_multi_choice_echoes_selected_values(patched) -> None:
    _FakeAsk.response = {"submitted": True, "values": ["赤", "青"], "other": "緑"}

    result = await tools.ask_user_choice.ainvoke(
        {"question": "色は？", "choices": ["赤", "青"], "multi_select": True}
    )

    assert result == "赤、青、緑"
    assert _echoed() == ["赤、青、緑"]
    assert _FakeAsk.instances[0].updated_content == "色は？"


@pytest.mark.asyncio
async def test_multi_text_form_echoes_label_value_pairs(patched) -> None:
    _FakeAsk.response = {"submitted": True, "values": ["山田太郎", "PDF"]}

    result = await tools.ask_user_question.ainvoke({"question": "お名前と形式は？", "labels": ["名前", "形式"]})

    assert result == "名前: 山田太郎\n形式: PDF"
    assert _echoed() == ["- 名前: 山田太郎\n- 形式: PDF"]
    assert _FakeAsk.instances[0].updated_content == "お名前と形式は？"


@pytest.mark.asyncio
async def test_timeout_does_not_echo(patched, monkeypatch) -> None:
    monkeypatch.setattr(tools._state, "_ASK_USER_CHOICE_TIMEOUT_SECONDS", 1)
    _FakeAsk.response = None

    result = await tools.ask_user_choice.ainvoke({"question": "色は？", "choices": ["赤", "青"]})

    assert "timeout" in result
    assert _echoed() == []


class _FakeAskUser:
    """cl.AskUserMessage の差し替え。AskTextBar が返す user_message 形式の応答を返す。"""

    responses: list = []

    def __init__(self, content=None, timeout=90, **kwargs):
        self.content = content

    async def send(self):
        return _FakeAskUser.responses.pop(0)


_TEXT_CANCEL_RESPONSE = {"output": "❌ キャンセル", "metadata": {"askCancel": True}}


@pytest.mark.asyncio
async def test_text_question_cancel_button(patched, monkeypatch) -> None:
    monkeypatch.setattr(tools.cl, "AskUserMessage", _FakeAskUser)
    _FakeAskUser.responses = [dict(_TEXT_CANCEL_RESPONSE)]

    result = await tools.ask_user_question.ainvoke({"question": "ファイル名は？"})

    assert result == "Error: the user cancelled answering the question."


@pytest.mark.asyncio
async def test_text_question_typed_cancel_text_is_not_cancel(patched, monkeypatch) -> None:
    """本文が「❌ キャンセル」でも、キャンセルボタン由来（metadata）でなければ通常の回答として扱う。"""
    monkeypatch.setattr(tools.cl, "AskUserMessage", _FakeAskUser)
    _FakeAskUser.responses = [{"output": "❌ キャンセル", "metadata": {}}]

    result = await tools.ask_user_question.ainvoke({"question": "ファイル名は？"})

    assert result == "❌ キャンセル"


@pytest.mark.asyncio
async def test_multi_text_form_cancel_button(patched) -> None:
    _FakeAsk.response = {"submitted": False, "values": []}

    result = await tools.ask_user_question.ainvoke({"question": "お名前と形式は？", "labels": ["名前", "形式"]})

    assert result == "Error: the user cancelled answering the question."
    assert _echoed() == ["❌ キャンセル"]


@pytest.mark.asyncio
async def test_single_choice_other_free_text_cancel_button(patched, monkeypatch) -> None:
    """ask_user_choice の「その他」から出る自由記述欄（AskTextBar）のキャンセルも効く。"""
    monkeypatch.setattr(tools.cl, "AskUserMessage", _FakeAskUser)
    _FakeAsk.response = {"payload": {"value": "__other__"}, "label": "✏️ その他（自由入力）"}
    _FakeAskUser.responses = [dict(_TEXT_CANCEL_RESPONSE)]

    result = await tools.ask_user_choice.ainvoke({"question": "色は？", "choices": ["赤", "青"]})

    assert result == "Error: the user cancelled the choice."
