"""ターンの始めの自動メモリー検索・タスクの終わりの記録の見直し・システムプロンプトに表示するメモリーのテスト。

src/memory.py（relevant_memories・render_system_prompt_memories・set_in_system_prompt・written_since）、
src/memory_recall.py、src/memory_review.py、src/system_prompt_builder.py の fill_session_parts。
"""

from __future__ import annotations

import asyncio
import time
import types

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src import memory, memory_recall, memory_review
from src.tools import _python_fs_guard, _state


@pytest.fixture(autouse=True)
def _clear_policies(monkeypatch):
    monkeypatch.setattr(memory, "_POLICIES", {})


def _seed(root):
    memory.create_memory(root, "monthly-report", "月次レポートの書式", "feedback", "表は売上→費用の順に並べる。", owner="alice")
    memory.create_memory(root, "deploy-steps", "デプロイ手順", "project", "deploy は金曜に行わない。", owner="alice")
    memory.create_memory(root, "printer", "プリンターの場所", "reference", "3階の奥。", owner="bob")


def test_relevant_memories_finds_by_request_text(tmp_path):
    _seed(tmp_path)
    hits = memory.relevant_memories(tmp_path, "今月の月次レポートを作って", viewer="alice")
    assert [m.name for m in hits][:1] == ["monthly-report"]
    assert "printer" not in [m.name for m in hits]


def test_relevant_memories_matches_ascii_word(tmp_path):
    _seed(tmp_path)
    hits = memory.relevant_memories(tmp_path, "please deploy the app", viewer="alice")
    assert [m.name for m in hits] == ["deploy-steps"]


def test_relevant_memories_hides_other_users_private(tmp_path):
    memory.create_memory(tmp_path, "secret", "月次レポートの個人メモ", "user", "月次レポートは自分で書く", owner="bob")
    assert memory.relevant_memories(tmp_path, "月次レポート", viewer="alice") == []
    assert [m.name for m in memory.relevant_memories(tmp_path, "月次レポート", viewer="bob")] == ["secret"]


def test_search_memories_and_of_words_and_width(tmp_path):
    _seed(tmp_path)
    assert [m.name for m in memory.search_memories(tmp_path, "月次 売上", viewer="alice")] == ["monthly-report"]
    assert memory.search_memories(tmp_path, "月次 金曜", viewer="alice") == []
    # 全角英字でも当たる
    assert [m.name for m in memory.search_memories(tmp_path, "ｄｅｐｌｏｙ", viewer="alice")] == ["deploy-steps"]


def test_load_cache_reflects_rewrites(tmp_path):
    memory.create_memory(tmp_path, "m", "d", "feedback", "first", owner="alice")
    assert memory.read_memory(tmp_path, "m", "alice").content == "first"
    memory.update_memory(tmp_path, "m", "second version", owner="alice")
    assert memory.read_memory(tmp_path, "m", "alice").content == "second version"


def test_in_system_prompt_flag_survives_updates_and_renders(tmp_path):
    _seed(tmp_path)
    mem = memory.read_memory(tmp_path, "monthly-report", "alice")
    result = memory.set_in_system_prompt(tmp_path, [mem], True)
    assert len(result.done) == 1
    # AI の更新・種類の変更でも残る
    memory.update_memory(tmp_path, "monthly-report", "表は売上→費用→利益の順。", owner="alice")
    memory.change_memories(tmp_path, [memory.read_memory(tmp_path, "monthly-report", "alice")], memory_type="reference")
    assert memory.read_memory(tmp_path, "monthly-report", "alice").in_system_prompt is True
    block = memory.render_system_prompt_memories(tmp_path, viewer="alice")
    assert "monthly-report" in block and "利益" in block
    assert "deploy-steps" not in block
    # 外すと載らない
    memory.set_in_system_prompt(tmp_path, [memory.read_memory(tmp_path, "monthly-report", "alice")], False)
    assert memory.render_system_prompt_memories(tmp_path, viewer="alice") == "(none)"


def test_system_prompt_memories_respect_visibility_and_limit(tmp_path):
    memory.create_memory(tmp_path, "private-rule", "d", "user", "x" * 50, owner="bob")
    memory.create_memory(tmp_path, "long", "d", "feedback", "y" * 1000, owner="bob")
    memory.create_memory(tmp_path, "later", "d", "feedback", "z" * 1000, owner="bob")
    mems = [memory.read_memory(tmp_path, n, "bob") for n in ("private-rule", "long", "later")]
    memory.set_in_system_prompt(tmp_path, mems, True)
    shown = memory.render_system_prompt_memories(tmp_path, viewer="alice", max_chars=1100)
    assert "private-rule" not in shown
    assert "long" in shown and "later" in shown  # 上限を超えた分は名前だけ
    assert "read_memory" in shown


def test_fill_session_parts_puts_only_pinned_memories(tmp_path):
    from src.memory import MEMORY_INDEX_MARKER
    from src.system_prompt_builder import fill_session_parts

    _seed(tmp_path)
    memory.set_in_system_prompt(tmp_path, [memory.read_memory(tmp_path, "deploy-steps", "alice")], True)
    config = types.SimpleNamespace(memory_dir=tmp_path, memory_system_prompt_max_chars=6000, vector_db_enabled=False)
    filled = fill_session_parts(f"A\n{MEMORY_INDEX_MARKER}\nB", config, drafts=[], memory_viewer="alice")
    assert "deploy-steps" in filled
    assert "monthly-report" not in filled


def test_build_recall_messages_shape(tmp_path):
    _seed(tmp_path)
    config = types.SimpleNamespace(memory_dir=tmp_path, memory_recall_enabled=True, memory_recall_max_results=5, memory_recall_excerpt_chars=50)
    messages, count = memory_recall.build_recall_messages(config, "月次レポートをお願い", "alice")
    assert count >= 1
    call, result = messages
    assert isinstance(call, AIMessage) and call.tool_calls[0]["name"] == "search_memory"
    assert isinstance(result, ToolMessage) and result.tool_call_id == call.tool_calls[0]["id"]
    assert "monthly-report" in result.content and "売上" in result.content
    assert memory_recall.is_recall_message(call) and memory_recall.is_recall_message(result)


def test_build_recall_messages_disabled(tmp_path):
    _seed(tmp_path)
    config = types.SimpleNamespace(memory_dir=tmp_path, memory_recall_enabled=False)
    assert memory_recall.build_recall_messages(config, "月次レポート", "alice") == ([], 0)


def test_build_recall_messages_without_hits_still_searches(tmp_path):
    _seed(tmp_path)
    config = types.SimpleNamespace(memory_dir=tmp_path, memory_recall_enabled=True)
    messages, count = memory_recall.build_recall_messages(config, "zzzz qqqq", "alice")
    assert count == 0 and len(messages) == 2
    assert "No related memories" in messages[1].content


def test_written_since(tmp_path):
    before = time.time() - 1
    assert not memory.written_since(tmp_path, "alice", before)
    memory.create_memory(tmp_path, "m", "d", "feedback", "c", owner="alice")
    assert memory.written_since(tmp_path, "alice", before)
    assert not memory.written_since(tmp_path, "alice", time.time() + 60)


def _turn(*, tool_calls: int, memory_call: bool = False):
    messages = [HumanMessage(content="表を直して", id="h1")]
    recall_id = "memrecall_x"
    messages.append(AIMessage(content="", tool_calls=[{"name": "search_memory", "args": {"query": "q"}, "id": recall_id}], additional_kwargs={memory_recall.RECALL_KWARG: True}))
    messages.append(ToolMessage(content="none", tool_call_id=recall_id, additional_kwargs={memory_recall.RECALL_KWARG: True}))
    for i in range(tool_calls):
        name = "create_memory" if memory_call and i == 0 else "dispatch_agent"
        messages.append(AIMessage(content="", tool_calls=[{"name": name, "args": {"task": "t"}, "id": f"c{i}"}]))
        messages.append(ToolMessage(content="done", tool_call_id=f"c{i}"))
    messages.append(AIMessage(content="直しました。"))
    return messages


def test_should_review_conditions():
    config = types.SimpleNamespace(memory_review_enabled=True, memory_review_min_tool_calls=1)
    assert memory_review.should_review(config, _turn(tool_calls=1), memory_written=False)
    # 自動検索の分は数えない
    assert not memory_review.should_review(config, _turn(tool_calls=0), memory_written=False)
    assert not memory_review.should_review(config, _turn(tool_calls=2), memory_written=True)
    assert not memory_review.should_review(config, _turn(tool_calls=2, memory_call=True), memory_written=False)
    config.memory_review_enabled = False
    assert not memory_review.should_review(config, _turn(tool_calls=2), memory_written=False)


def test_build_turn_record_excludes_recall():
    record, search = memory_review.build_turn_record(_turn(tool_calls=1))
    assert "表を直して" in record and "直しました。" in record
    assert "dispatch_agent" in record
    assert "search_memory" not in record
    assert "表を直して" in search


class _FakeModel:
    """1回目に create_memory を呼び、2回目に終わる偽モデル。"""

    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools):
        self.tool_names = [t.name for t in tools]
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "create_memory",
                        "args": {"name": "table-order", "description": "表の並び", "memory_type": "feedback", "content": "売上→費用"},
                        "id": "x1",
                    }
                ],
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        return AIMessage(content="Saved: table-order", usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})


def test_run_memory_review_saves_with_tools(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(_state, "_MEMORY_ROOT", tmp_path)
    config = types.SimpleNamespace(
        memory_dir=tmp_path,
        system_prompt_path=Path(__file__).resolve().parents[1] / "system_prompt" / "system_prompt.md",
        memory_review_max_iterations=4,
        memory_review_timeout_seconds=30,
    )
    model = _FakeModel()
    result = asyncio.run(memory_review.run_memory_review(config, _turn(tool_calls=1), viewer="anonymous", model=model))
    assert result.error is None
    assert result.saved == ["table-order"]
    assert result.usage["total"] == 20
    assert "save_glossary_term" in model.tool_names and "delete_memory" not in model.tool_names
    assert memory.find_memory(tmp_path, "table-order", "anonymous") is not None


def test_allowed_sandbox_dirs_for_tool(tmp_path, monkeypatch):
    from src.config import SandboxDirEntry

    open_dir = tmp_path / "open"
    code_dir = tmp_path / "code"
    script_dir = tmp_path / "script"
    for d in (open_dir, code_dir, script_dir):
        d.mkdir()
    monkeypatch.setattr(
        _state,
        "_ALLOW_SANDBOX_DIRS",
        (
            SandboxDirEntry(dir=open_dir, allow_entries=frozenset()),
            SandboxDirEntry(dir=code_dir, allow_entries=frozenset({"execute_python_code"})),
            SandboxDirEntry(dir=script_dir, allow_entries=frozenset({("docx-edit", "edit_docx.py")})),
            SandboxDirEntry(dir=tmp_path / "missing", allow_entries=frozenset()),
        ),
    )
    assert _python_fs_guard.allowed_sandbox_dirs_for_tool("execute_python_code") == [open_dir, code_dir]
    # バックグラウンド版は同期版の名前でも許す
    assert _python_fs_guard.allowed_sandbox_dirs_for_tool("execute_python_code_background") == [open_dir, code_dir]
    assert _python_fs_guard.allowed_sandbox_dirs_for_tool("Write") == [open_dir]


def test_knowledge_collection_list_cache_follows_catalog_changes(tmp_path):
    from admin import knowledge
    from src.vector_db import catalog as vcat

    assert knowledge.list_collections_cached(tmp_path / "none") == []
    with vcat.open_catalog(tmp_path) as c:
        c.create("a", [str(tmp_path)])
    assert [c.name for c in knowledge.list_collections_cached(tmp_path)] == ["a"]
    assert [c.name for c in knowledge.list_collections_cached(tmp_path)] == ["a"]
    with vcat.open_catalog(tmp_path) as c:
        c.create("b", [str(tmp_path)])
    assert [c.name for c in knowledge.list_collections_cached(tmp_path)] == ["a", "b"]
