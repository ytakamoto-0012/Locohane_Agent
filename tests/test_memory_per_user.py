"""ユーザー別の永続メモリー（src/memory.py・src/tools/memory_tools.py・src/skill_drafts.py のガード）のテスト。"""

from __future__ import annotations

import types

import pytest

from src import memory, skill_drafts
from src.tools import _state, memory_tools


def test_create_for_owner_and_default_visibility(tmp_path):
    memory.create_memory(tmp_path, "how-to", "d", "feedback", "use worker", owner="alice")
    memory.create_memory(tmp_path, "proj", "d", "project", "plan", owner="alice")
    memory.create_memory(tmp_path, "me", "d", "user", "likes short answers", owner="alice", visibility="shared")
    assert (tmp_path / "users" / "alice" / "feedback" / "how-to.md").is_file()
    assert memory.read_memory(tmp_path, "how-to", "alice").visibility == "shared"
    assert memory.read_memory(tmp_path, "proj", "alice").visibility == "private"
    # type user は shared を指定しても private
    assert memory.read_memory(tmp_path, "me", "alice").visibility == "private"
    assert "how-to" in (tmp_path / "users" / "alice" / "MEMORY.md").read_text(encoding="utf-8")
    # 共通の MEMORY.md にはユーザーのメモリーを載せない
    assert not (tmp_path / "MEMORY.md").exists()


@pytest.mark.parametrize(
    "content",
    [
        "連絡先は someone@example.com",
        "電話 03-1234-5678",
        "携帯 09012345678",
        "カード 1234-5678-9012-3456",
        "パスワードは xxx",
        "これは社外秘です",
        r"C:\Users\user\Documents\a.xlsx を参照",
    ],
)
def test_sensitive_content_is_forced_private(tmp_path, content):
    memory.create_memory(tmp_path, "m", "d", "feedback", content, owner="alice", visibility="shared")
    assert memory.read_memory(tmp_path, "m", "alice").visibility == "private"


def test_visibility_and_order_for_viewer(tmp_path):
    memory.create_memory(tmp_path, "common-note", "共通", "reference", "c")
    memory.create_memory(tmp_path, "tips", "alice tips", "feedback", "a", owner="alice")
    memory.create_memory(tmp_path, "secret", "alice secret", "project", "s", owner="alice")
    memory.create_memory(tmp_path, "tips", "bob tips", "feedback", "b", owner="bob")
    visible = [(m.owner, m.name) for m in memory.list_memories(tmp_path, viewer="bob")]
    # 自分 → 共通 → 他のユーザー。他のユーザーの private は出ない
    assert visible == [("bob", "tips"), (None, "common-note"), ("alice", "tips")]
    assert memory.read_visible_memory(tmp_path, "tips", "bob").content == "b"
    with pytest.raises(ValueError, match="not found"):
        memory.read_visible_memory(tmp_path, "secret", "bob")
    with pytest.raises(ValueError, match="not found"):
        memory.read_visible_memory(tmp_path, "secret", "bob", "alice", auto=False)
    block = memory.render_memory_block(tmp_path, viewer="bob")
    assert block.splitlines()[0].startswith("- [feedback] tips (mine, shared)")
    assert "secret" not in block
    assert [m.name for m in memory.search_memories(tmp_path, "tips", viewer="bob")] == ["tips", "tips"]


def test_ambiguous_name_among_other_users(tmp_path):
    memory.create_memory(tmp_path, "tips", "d", "feedback", "a", owner="alice")
    memory.create_memory(tmp_path, "tips", "d", "feedback", "b", owner="bob")
    with pytest.raises(ValueError, match="pass owner"):
        memory.read_visible_memory(tmp_path, "tips", "carol")
    assert memory.read_visible_memory(tmp_path, "tips", "carol", "bob", auto=False).content == "b"


def test_shared_memory_hidden_if_edited_to_sensitive(tmp_path):
    path = memory.create_memory(tmp_path, "tips", "d", "feedback", "plain", owner="alice")
    path.write_text(path.read_text(encoding="utf-8").replace("plain", "mail me at a@example.com"), encoding="utf-8")
    assert memory.read_memory(tmp_path, "tips", "alice").visibility == "shared"
    assert memory.list_memories(tmp_path, viewer="bob") == []


def test_missing_visibility_is_private(tmp_path):
    d = tmp_path / "users" / "alice" / "feedback"
    d.mkdir(parents=True)
    (d / "x.md").write_text("---\nname: x\ndescription: d\ntype: feedback\n---\n\nbody\n", encoding="utf-8")
    assert memory.read_memory(tmp_path, "x", "alice").visibility == "private"


def test_invalid_owner_rejected(tmp_path):
    for owner in ("..", "a/b", "a\\b", ""):
        with pytest.raises(ValueError):
            memory.create_memory(tmp_path, "m", "d", "feedback", "c", owner=owner)


@pytest.fixture
def as_user(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    monkeypatch.setattr(_state, "_MEMORY_ROOT", root)
    current = {"user": "alice"}
    monkeypatch.setattr(skill_drafts, "current_draft_user", lambda: current["user"])
    return root, current


def test_tools_write_own_and_refuse_others(as_user):
    root, current = as_user
    out = memory_tools.create_memory.invoke(
        {"name": "tips", "description": "d", "memory_type": "feedback", "content": "use worker"}
    )
    assert "Visibility: shared" in out
    assert (root / "users" / "alice" / "feedback" / "tips.md").is_file()
    current["user"] = "bob"
    out = memory_tools.update_memory.invoke({"name": "tips", "content": "changed"})
    assert out.startswith("Error: ") and "another user (alice)" in out
    assert memory_tools.delete_memory.invoke({"name": "tips"}).startswith("Error: ")
    read = memory_tools.read_memory.invoke({"name": "tips"})
    assert read.startswith("[feedback] tips (user: alice)") and "follow your own" in read
    # bob が同じ名前で自分のメモリーを作ると、そちらが優先される
    memory_tools.create_memory.invoke({"name": "tips", "description": "d", "memory_type": "feedback", "content": "mine"})
    assert "\n\nmine" in memory_tools.read_memory.invoke({"name": "tips"})
    assert "use worker" in memory_tools.read_memory.invoke({"name": "tips", "owner": "alice"})
    listed = memory_tools.list_memories.invoke({}).splitlines()
    assert listed[0].startswith("- [feedback] tips (mine, shared)")
    assert listed[1].startswith("- [feedback] tips (user: alice)")


def test_tools_report_forced_private(as_user):
    out = memory_tools.create_memory.invoke(
        {
            "name": "contact",
            "description": "d",
            "memory_type": "feedback",
            "content": "電話 03-1234-5678",
            "visibility": "shared",
        }
    )
    assert "Visibility: private" in out and "phone number" in out


def test_tools_update_common_memory(as_user):
    root, _ = as_user
    memory.create_memory(root, "legacy", "d", "project", "old")
    out = memory_tools.update_memory.invoke({"name": "legacy", "content": "new"})
    assert out.startswith("Updated the common memory")
    assert memory.read_memory(root, "legacy").content == "new"


def test_guard_blocks_other_users_memory_dirs(tmp_path):
    root = tmp_path / "mem"
    memory.create_memory(root, "a", "d", "feedback", "c", owner="alice")
    memory.create_memory(root, "b", "d", "feedback", "c", owner="bob")
    config = types.SimpleNamespace(memory_dir=root, skill_draft_dir=None)
    alice_file = root / "users" / "alice" / "feedback" / "a.md"
    assert skill_drafts.access_error(alice_file, "read", config, "bob") is not None
    assert skill_drafts.access_error(alice_file, "read", config, "alice") is None
    assert skill_drafts.access_error(root / "feedback", "read", config, "bob") is None
    assert skill_drafts.unreadable_dirs(config, "bob") == [(root / "users" / "alice").resolve()]
    assert skill_drafts.unwritable_dirs(config, "bob") == [(root / "users" / "alice").resolve()]
