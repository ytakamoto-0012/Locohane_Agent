"""force_write_thread_note() の回帰テスト。

背景: is_compaction_blocked_by_missing_note による「見送り」は、ユーザーの
次発話でLLMがナッジに応じるのを待つだけで、無視され続けると記録なしで
圧縮が強制されうる（[[locohane_thread_note_status_compaction_reinjection]]
と同根の「事実退避の機会を確実に作りたい」課題）。force_write_thread_note は、
tools を write_thread_note 1件だけに絞った上で tool_choice="required" にして
モデルを1回呼び出し、その場で書き出しを強制する。
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src import tools
from src.context_compaction import force_write_thread_note


@dataclass
class _FakeConfig:
    context_compaction_keep_recent_iterations: int
    context_compaction_summary_source_max_chars: int
    context_compaction_skill_reattach_max_chars_per_skill: int
    context_compaction_skill_reattach_total_max_chars: int
    context_compaction_pre_note_warning_text: str


class _FakeUserSession:
    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def _config() -> _FakeConfig:
    return _FakeConfig(
        context_compaction_keep_recent_iterations=1,
        context_compaction_summary_source_max_chars=2000,
        context_compaction_skill_reattach_max_chars_per_skill=12000,
        context_compaction_skill_reattach_total_max_chars=36000,
        context_compaction_pre_note_warning_text="[**システム通知: 書き出してください**]",
    )


def _messages() -> list:
    return [
        HumanMessage(content="q1"),
        AIMessage(content="ok1"),
        HumanMessage(content="q2"),
        AIMessage(content="ok2"),
    ]


class _FakeToolCallModel:
    """write_thread_note を必ず呼ぶ強制応答を返す偽モデル。"""

    def __init__(self, tool_calls: list[dict] | None):
        self._tool_calls = tool_calls
        self.bind_tools_calls: list[tuple] = []

    def bind_tools(self, tools_arg, tool_choice=None):
        self.bind_tools_calls.append((tools_arg, tool_choice))
        return self

    async def ainvoke(self, messages):
        if self._tool_calls is None:
            return AIMessage(content="ツールは呼びません")
        return AIMessage(content="", tool_calls=self._tool_calls)


class _RaisingModel:
    def bind_tools(self, tools_arg, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        raise RuntimeError("接続エラー")


@pytest.mark.asyncio
async def test_force_write_thread_note_success(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"thread_id": "t1"}))

    model = _FakeToolCallModel(
        [{"name": "write_thread_note", "args": {"topic": "調査結果", "content": "件数は123件"}, "id": "call_1"}]
    )

    result = await force_write_thread_note(_messages(), model, _config())

    assert result is not None
    ai_message, tool_messages = result
    assert ai_message.tool_calls[0]["name"] == "write_thread_note"
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_1"
    assert "Written" in tool_messages[0].content

    # tool_choice="required" かつ write_thread_note のみへ絞って bind_tools していること。
    assert len(model.bind_tools_calls) == 1
    bound_tools, tool_choice = model.bind_tools_calls[0]
    assert tool_choice == "required"
    assert len(bound_tools) == 1

    # 実際にファイルへ書き込まれていること
    # （_thread_notes_path() は _tmp_<thread_id> 配下を返すため、パスを
    # 決め打ちせず list_thread_notes 経由で内容を確認する）。
    listing = tools.list_thread_notes.invoke({})
    assert "調査結果" in listing


@pytest.mark.asyncio
async def test_force_write_thread_note_empty_tool_calls_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"thread_id": "t1"}))

    model = _FakeToolCallModel(None)

    result = await force_write_thread_note(_messages(), model, _config())

    assert result is None


@pytest.mark.asyncio
async def test_force_write_thread_note_missing_args_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"thread_id": "t1"}))

    model = _FakeToolCallModel([{"name": "write_thread_note", "args": {"topic": "見出しだけ"}, "id": "call_2"}])

    result = await force_write_thread_note(_messages(), model, _config())

    assert result is None


@pytest.mark.asyncio
async def test_force_write_thread_note_returns_tool_message_for_every_tool_call(
    monkeypatch, tmp_path: Path
) -> None:
    """tool_calls が複数返っても、全件に対応する ToolMessage を返すこと。

    返り値はそのまま永続履歴へ追記される。OpenAI互換APIは「tool_calls N件
    なら直後に tool メッセージ N件」を要求するため、1件でも欠けると対応の
    取れない tool_call が履歴へ焼き付き、以降そのスレッドのLLM呼び出しが
    全て落ちる。tool_choice="required" でツールを1件に絞っていても、
    モデルがトピック別に複数回呼ぶことはありうる。
    """
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"thread_id": "t1"}))

    model = _FakeToolCallModel(
        [
            {"name": "write_thread_note", "args": {"topic": "件数", "content": "123件"}, "id": "call_1"},
            {"name": "write_thread_note", "args": {"topic": "該当箇所", "content": "L42"}, "id": "call_2"},
        ]
    )

    result = await force_write_thread_note(_messages(), model, _config())

    assert result is not None
    ai_message, tool_messages = result
    issued = {tc["id"] for tc in ai_message.tool_calls}
    answered = {m.tool_call_id for m in tool_messages}
    assert issued == answered == {"call_1", "call_2"}

    listing = tools.list_thread_notes.invoke({})
    assert "件数" in listing
    assert "該当箇所" in listing


@pytest.mark.asyncio
async def test_force_write_thread_note_answers_failed_call_to_avoid_orphan(
    monkeypatch, tmp_path: Path
) -> None:
    """一部の tool_call が引数不足でも、全件に ToolMessage を返すこと
    （1件でも書き込みに成功していれば履歴へ追記する）。"""
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"thread_id": "t1"}))

    model = _FakeToolCallModel(
        [
            {"name": "write_thread_note", "args": {"topic": "件数", "content": "123件"}, "id": "call_1"},
            {"name": "write_thread_note", "args": {"topic": "見出しだけ"}, "id": "call_2"},
        ]
    )

    result = await force_write_thread_note(_messages(), model, _config())

    assert result is not None
    ai_message, tool_messages = result
    issued = {tc["id"] for tc in ai_message.tool_calls}
    answered = {m.tool_call_id for m in tool_messages}
    assert issued == answered
    failed = next(m for m in tool_messages if m.tool_call_id == "call_2")
    assert failed.content.startswith("Error:")


@pytest.mark.asyncio
async def test_force_write_thread_note_llm_error_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"thread_id": "t1"}))

    result = await force_write_thread_note(_messages(), _RaisingModel(), _config())

    assert result is None
