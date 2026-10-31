"""app.py の _find_orphaned_tool_calls() の回帰テスト。

孤立tool_call（AIMessage.tool_calls はあるが対応する ToolMessage が無い
状態）は、以前は履歴の末尾のAIMessageしか見ておらず、孤立発生後に
loop_nudge等の後続メッセージが追記される・コンテキスト圧縮で圧縮後の
保持ウィンドウの途中に残る、といった経路で検出漏れになっていた
（issue/20260804_234928_orphaned_tool_call_dual_session_freeze.md の再発）。
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import _build_orphaned_placeholder_messages, _find_orphaned_tool_calls
from src.tools import _state
from src.tools import _workdir as tools_workdir
from src.tools.write_scratch_note import _scratch_notes_path_for_run, sanitize_run_id


class _FakeUserSession:
    def __init__(self, thread_id: str = "thread-1") -> None:
        self._data: dict = {"thread_id": thread_id}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value) -> None:
        self._data[key] = value


def _setup_workdir(monkeypatch, tmp_path) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir(exist_ok=True)
    monkeypatch.setattr(_state, "_DEFAULT_WORKDIR", workdir)
    monkeypatch.setattr(tools_workdir.cl, "user_session", _FakeUserSession())


def _ai_with_tool_call(tool_call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "dispatch_agent", "args": {}, "id": tool_call_id, "type": "tool_call"}],
    )


def test_orphaned_tool_call_at_tail_is_detected() -> None:
    messages = [HumanMessage(content="こんにちは"), _ai_with_tool_call("tc-1")]

    orphaned = _find_orphaned_tool_calls(messages)

    assert [tc["id"] for tc in orphaned] == ["tc-1"]


def test_orphaned_tool_call_not_at_tail_is_detected() -> None:
    # 孤立tool_call(tc-1)の後に、無関係なやり取りが続いているケース
    # （loop_nudge注入やコンテキスト圧縮後の保持ウィンドウで起こりうる）。
    messages = [
        HumanMessage(content="こんにちは"),
        _ai_with_tool_call("tc-1"),
        HumanMessage(content="続けて"),
        AIMessage(content="", tool_calls=[{"name": "read_skill", "args": {}, "id": "tc-2", "type": "tool_call"}]),
        ToolMessage(content="ok", tool_call_id="tc-2"),
    ]

    orphaned = _find_orphaned_tool_calls(messages)

    assert [tc["id"] for tc in orphaned] == ["tc-1"]


def test_all_tool_calls_answered_returns_empty() -> None:
    messages = [
        HumanMessage(content="こんにちは"),
        _ai_with_tool_call("tc-1"),
        ToolMessage(content="ok", tool_call_id="tc-1"),
        AIMessage(content="完了しました"),
    ]

    assert _find_orphaned_tool_calls(messages) == []


def test_empty_messages_returns_empty() -> None:
    assert _find_orphaned_tool_calls([]) == []


def test_build_orphaned_placeholder_messages_for_dispatch_agent_with_existing_rescue_file(monkeypatch, tmp_path) -> None:
    """退避先ファイルが実在する場合、ToolMessage・HumanMessage双方に絶対パスと
    再開手順（explore委譲→write_thread_note→ユーザー指示に従う）が含まれ、
    HumanMessageで念押しされること（サブエージェント強制停止時の会話履歴退避機能の一部）。

    以前はToolMessage単体（「エラー: ...」の体裁）だったため、低パラメータ
    モデルが後半の具体的な指示を実行に移さず、案内が実質機能しなかった
    （2026-09-09 実運用で確認）。HumanMessageでも同じ内容を念押しする。
    """
    _setup_workdir(monkeypatch, tmp_path)
    tc = {"name": "dispatch_agent", "args": {}, "id": "tc-1", "type": "tool_call"}
    note_path = _scratch_notes_path_for_run(sanitize_run_id(tc["id"]))
    note_path.write_text("退避内容", encoding="utf-8")

    messages = _build_orphaned_placeholder_messages(tc, "Because the user stopped it (or similar), ")

    assert len(messages) == 2
    tool_msg, human_msg = messages
    assert isinstance(tool_msg, ToolMessage)
    assert tool_msg.tool_call_id == "tc-1"
    assert tool_msg.name == "dispatch_agent"
    assert "Because the user stopped it (or similar), the execution of this tool call was interrupted." in tool_msg.content
    assert str(note_path) in tool_msg.content
    assert "explore" in tool_msg.content
    assert "write_thread_note" in tool_msg.content
    assert isinstance(human_msg, HumanMessage)
    assert str(note_path) in human_msg.content
    assert "change to different work or to stop" in human_msg.content


def test_build_orphaned_placeholder_messages_for_dispatch_agent_without_rescue_file(monkeypatch, tmp_path) -> None:
    """退避先ファイルが存在しない場合はフォールバック文言になり、HumanMessageも依然として追加される。"""
    _setup_workdir(monkeypatch, tmp_path)
    tc = {"name": "dispatch_agent", "args": {}, "id": "tc-missing", "type": "tool_call"}

    messages = _build_orphaned_placeholder_messages(tc, "Because the user stopped it (or similar), ")

    assert len(messages) == 2
    tool_msg, human_msg = messages
    assert "could not be confirmed" in tool_msg.content
    assert "could not be confirmed" in human_msg.content


def test_build_orphaned_placeholder_messages_for_other_tool_returns_single_tool_message() -> None:
    tc = {"name": "read_skill", "args": {}, "id": "tc-2", "type": "tool_call"}

    messages = _build_orphaned_placeholder_messages(tc, "Because the previous session ended abnormally, ")

    assert len(messages) == 1
    assert isinstance(messages[0], ToolMessage)
    assert messages[0].tool_call_id == "tc-2"
    assert "Because the previous session ended abnormally, the execution of this tool call was interrupted." in messages[0].content
    assert "write_scratch_note" not in messages[0].content


def test_build_orphaned_placeholder_messages_for_batch_lists_all_group_rescue_files(monkeypatch, tmp_path) -> None:
    """dispatch_agent_batch はグループごとに `<tool_call_id>_g<N>` の退避ファイルを持つため、全件を案内する。"""
    _setup_workdir(monkeypatch, tmp_path)
    tc = {"name": "dispatch_agent_batch", "args": {}, "id": "tc-b", "type": "tool_call"}
    paths = [_scratch_notes_path_for_run(sanitize_run_id(f"{tc['id']}_g{i}")) for i in (10, 2)]
    for p in paths:
        p.write_text("退避内容", encoding="utf-8")

    messages = _build_orphaned_placeholder_messages(tc, "Because the user stopped it (or similar), ")

    assert len(messages) == 2
    tool_msg, human_msg = messages
    assert tool_msg.name == "dispatch_agent_batch"
    # グループ番号順（g2→g10）に並ぶ。
    assert tool_msg.content.index(str(paths[1])) < tool_msg.content.index(str(paths[0]))
    assert "delegation to dispatch_agent_batch was interrupted" in human_msg.content
    assert str(paths[0]) in human_msg.content
