"""ユーザー別の永続メモリー（src/memory.py・src/tools/memory_tools.py・src/skill_drafts.py のガード）のテスト。"""

from __future__ import annotations

import types

import pytest

from src import memory, skill_drafts
from src.tools import _state, memory_tools


def test_create_for_owner_and_default_visibility(tmp_path):
    memory.create_memory(tmp_path, "how-to", "d", "feedback", "use worker", owner="alice")
    memory.create_memory(tmp_path, "proj", "d", "project", "plan", owner="alice")
    memory.create_memory(tmp_path, "ref", "d", "reference", "where the docs are", owner="alice")
    memory.create_memory(tmp_path, "mine", "d", "project", "plan", owner="alice", visibility="private")
    memory.create_memory(tmp_path, "me", "d", "user", "likes short answers", owner="alice", visibility="shared")
    assert (tmp_path / "users" / "alice" / "feedback" / "how-to.md").is_file()
    # 既定は user だけ個人用、ほかは省略すれば共有
    assert memory.read_memory(tmp_path, "how-to", "alice").visibility == "shared"
    assert memory.read_memory(tmp_path, "proj", "alice").visibility == "shared"
    assert memory.read_memory(tmp_path, "ref", "alice").visibility == "shared"
    # shared の種類でも private を指定すれば個人用
    assert memory.read_memory(tmp_path, "mine", "alice").visibility == "private"
    # type user は shared を指定しても private
    assert memory.read_memory(tmp_path, "me", "alice").visibility == "private"
    assert "how-to" in (tmp_path / "users" / "alice" / "MEMORY.md").read_text(encoding="utf-8")
    # 共通の MEMORY.md にはユーザーのメモリーを載せない
    assert not (tmp_path / "MEMORY.md").exists()


@pytest.fixture
def set_policy(monkeypatch):
    """memory_root に公開範囲の規則を登録する（テストの終わりに登録を消す）。"""
    monkeypatch.setattr(memory, "_POLICIES", {})

    def _set(root, **kwargs):
        memory.set_policy(root, memory.MemoryPolicy(**kwargs))

    return _set


@pytest.mark.parametrize(
    "content",
    [
        "カード 1234-5678-9012-3456",
        "パスワードは xxx",
        "口座番号 1234567",
    ],
)
def test_sensitive_content_is_forced_private_by_default(tmp_path, content):
    memory.create_memory(tmp_path, "m", "d", "feedback", content, owner="alice", visibility="shared")
    assert memory.read_memory(tmp_path, "m", "alice").visibility == "private"


@pytest.mark.parametrize(
    "content",
    [
        "連絡先は someone@example.com",
        "電話 03-1234-5678",
        "これは社外秘です",
        r"C:\Users\user\Documents\a.xlsx を参照",
    ],
)
def test_in_house_details_are_shared_by_default(tmp_path, content):
    # 社内のローカル運用が前提のため、社内で共有して困らない目印は既定では見ない
    memory.create_memory(tmp_path, "m", "d", "feedback", content, owner="alice", visibility="shared")
    assert memory.read_memory(tmp_path, "m", "alice").visibility == "shared"


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
def test_all_checks_force_private(tmp_path, set_policy, content):
    set_policy(tmp_path, checks=frozenset(memory.PRIVATE_CHECKS))
    memory.create_memory(tmp_path, "m", "d", "feedback", content, owner="alice", visibility="shared")
    assert memory.read_memory(tmp_path, "m", "alice").visibility == "private"


def test_no_checks_shares_even_passwords(tmp_path, set_policy):
    set_policy(tmp_path, checks=frozenset())
    memory.create_memory(tmp_path, "m", "d", "feedback", "パスワードは xxx", owner="alice", visibility="shared")
    assert memory.read_memory(tmp_path, "m", "alice").visibility == "shared"


def test_private_type_setting(tmp_path, set_policy):
    memory.create_memory(tmp_path, "plan", "d", "project", "p", owner="alice")
    memory.create_memory(tmp_path, "me", "d", "user", "u", owner="alice", visibility="shared")
    assert memory.read_memory(tmp_path, "plan", "alice").visibility == "shared"
    assert [m.name for m in memory.list_memories(tmp_path, viewer="bob")] == ["plan"]
    # project を private にすると、共有済みの project も他のユーザーには見えなくなり、新しいものは個人用になる
    set_policy(tmp_path, type_visibility={"user": "shared", "feedback": "shared", "project": "private", "reference": "shared"})
    assert memory.list_memories(tmp_path, viewer="bob") == []
    memory.create_memory(tmp_path, "plan2", "d", "project", "p", owner="alice", visibility="shared")
    assert memory.read_memory(tmp_path, "plan2", "alice").visibility == "private"
    # user を shared にすれば、user も共有できる
    memory.create_memory(tmp_path, "me2", "d", "user", "u", owner="alice")
    assert memory.read_memory(tmp_path, "me2", "alice").visibility == "shared"


def test_render_visibility_block():
    block = memory.render_visibility_block("tips for anyone", "- 本人のこと\n\n- 家族のこと\n", memory.DEFAULT_POLICY)
    # 基準は改行を残し、見出しの下に字下げして入れる（空行は除く）
    assert '- "shared" for:\n  tips for anyone\n- "private" for:\n  - 本人のこと\n  - 家族のこと\n' in block
    assert "type user are always private" in block
    assert "card or ID number, credential or personal ID" in block and "email" not in block
    none = memory.MemoryPolicy(type_visibility=dict(memory.DEFAULT_TYPE_VISIBILITY), checks=frozenset())
    assert "automatically" not in memory.render_visibility_block("a", "b", none)
    all_private = memory.MemoryPolicy(type_visibility={t: "private" for t in memory.MEMORY_TYPES})
    assert memory.render_visibility_block("a", "b", all_private).startswith("All memories are saved as private")


def test_visibility_and_order_for_viewer(tmp_path):
    memory.create_memory(tmp_path, "common-note", "共通", "reference", "c")
    memory.create_memory(tmp_path, "tips", "alice tips", "feedback", "a", owner="alice")
    memory.create_memory(tmp_path, "secret", "alice secret", "project", "s", owner="alice", visibility="private")
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
    path.write_text(path.read_text(encoding="utf-8").replace("plain", "password is xxx"), encoding="utf-8")
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
            "content": "暗証番号は 1234",
            "visibility": "shared",
        }
    )
    assert "Visibility: private" in out and "credential or personal ID" in out
    # 省略しても shared のはずだった場合は、private にした理由を伝える
    out = memory_tools.create_memory.invoke(
        {"name": "me", "description": "d", "memory_type": "user", "content": "likes short answers"}
    )
    assert "Visibility: private" in out and "memory_type user is always private" in out


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


def test_ranks_order_and_label(tmp_path, set_policy):
    memory.create_memory(tmp_path, "common-note", "c", "reference", "c")
    memory.create_memory(tmp_path, "rule", "boss rule", "feedback", "do A", owner="boss")
    memory.create_memory(tmp_path, "rule", "my rule", "feedback", "do B", owner="staff")
    # ランク未設定: 従来どおり 自分 → 共通 → 他のユーザー、ランクは表示しない
    assert [(m.owner, m.name) for m in memory.list_memories(tmp_path, viewer="staff")] == [
        ("staff", "rule"),
        (None, "common-note"),
        ("boss", "rule"),
    ]
    assert "rank" not in memory.render_memory_block(tmp_path, viewer="staff")
    # 上司のランクを上げると、部下の会話でも上司のものが先に並ぶ（共通・未設定のユーザーは default_rank）
    set_policy(tmp_path, ranks={"boss": 3})
    assert [(m.owner, m.name) for m in memory.list_memories(tmp_path, viewer="staff")] == [
        ("boss", "rule"),
        ("staff", "rule"),
        (None, "common-note"),
    ]
    block = memory.render_memory_block(tmp_path, viewer="staff").splitlines()
    assert block[0] == "- [feedback] rule (user: boss, rank 3): boss rule"
    assert block[1] == "- [feedback] rule (mine, shared, rank 1): my rule"
    assert block[2] == "- [reference] common-note (common, rank 1): c"


def test_author_and_updated_by_are_recorded(tmp_path):
    memory.create_memory(tmp_path, "legacy", "d", "project", "old")
    memory.create_memory(tmp_path, "mine", "d", "project", "x", owner="alice")
    assert memory.read_memory(tmp_path, "mine", "alice").author == "alice"
    assert memory.read_memory(tmp_path, "legacy").author is None
    memory.update_memory(tmp_path, "legacy", "new", updated_by="bob")
    m = memory.read_memory(tmp_path, "legacy")
    assert (m.updated_by, m.author) == ("bob", None)
    assert "updated_by: bob" in m.path.read_text(encoding="utf-8")
    # 書き換えても author は残り、updated_by を渡さなければ記録済みの値のまま
    memory.edit_memory(tmp_path, "legacy", description="d2", memory_type="project", content="newer")
    assert memory.read_memory(tmp_path, "legacy").updated_by == "bob"
    # author の記録が無いユーザーのメモリーは、所有者が作ったものとみなす
    path = memory.read_memory(tmp_path, "mine", "alice").path
    path.write_text(path.read_text(encoding="utf-8").replace("author: alice\n", ""), encoding="utf-8")
    assert memory.read_memory(tmp_path, "mine", "alice").author == "alice"


def test_tools_tell_to_follow_higher_rank(as_user, set_policy):
    root, current = as_user
    current["user"] = "boss"
    memory_tools.create_memory.invoke({"name": "rule", "description": "d", "memory_type": "feedback", "content": "A"})
    current["user"] = "staff"
    read = memory_tools.read_memory.invoke({"name": "rule"})
    assert "follow your own" in read and "rank" not in read
    set_policy(root, ranks={"boss": 3, "staff": 1})
    read = memory_tools.read_memory.invoke({"name": "rule"})
    assert read.startswith("[feedback] rule (user: boss, rank 3)")
    assert "owner's rank 3, your rank 1" in read and "follow this one" in read
    # 部下が共通のメモリーを書き換えると、更新者として残る
    memory.create_memory(root, "legacy", "d", "project", "old")
    memory_tools.update_memory.invoke({"name": "legacy", "content": "new"})
    assert "Last updated by: staff" in memory_tools.read_memory.invoke({"name": "legacy"})
    # ランクが上でも、他のユーザーのメモリーは書き換えられない
    current["user"] = "boss"
    memory_tools.create_memory.invoke({"name": "tip", "description": "d", "memory_type": "feedback", "content": "x"})
    current["user"] = "staff"
    memory_tools.create_memory.invoke({"name": "own", "description": "d", "memory_type": "feedback", "content": "y"})
    current["user"] = "boss"
    assert memory_tools.update_memory.invoke({"name": "own", "content": "z"}).startswith("Error: ")


def test_guard_treats_no_session_as_anonymous_owner(tmp_path):
    """セッションが無い（eval 等）とき、メモリーのツールは匿名ユーザーとして動くため、そのフォルダは読める。"""
    root = tmp_path / "mem"
    anonymous = skill_drafts.draft_owner_name(None)
    memory.create_memory(root, "a", "d", "feedback", "c", owner=anonymous)
    memory.create_memory(root, "b", "d", "feedback", "c", owner="bob")
    config = types.SimpleNamespace(memory_dir=root, skill_draft_dir=None)
    assert skill_drafts.access_error(root / "users" / anonymous / "feedback" / "a.md", "read", config, None) is None
    assert skill_drafts.access_error(root / "users" / "bob" / "feedback" / "b.md", "read", config, None) is not None
    # Grep・書き込みガードが使う除外一覧も同じ判断（自分のフォルダは除外しない）
    assert skill_drafts.unreadable_dirs(config, None) == [(root / "users" / "bob").resolve()]
    assert skill_drafts.unwritable_dirs(config, None) == [(root / "users" / "bob").resolve()]


def test_read_memory_accepts_mine_as_owner(as_user):
    root, current = as_user
    memory.create_memory(root, "tips", "d", "feedback", "alice's", owner="alice")
    memory.create_memory(root, "tips", "d", "feedback", "common one")
    out = memory_tools.read_memory.invoke({"name": "tips", "owner": "mine"})
    assert out.startswith("[feedback] tips (mine") and "alice's" in out


def test_description_with_triple_dash_still_loads(tmp_path):
    """説明・本文に --- が入っても frontmatter の区切りと取り違えない（AIで翻訳で混入しうる）。"""
    memory.create_memory(tmp_path, "m", "before --- after", "feedback", "line1\n---\nline2", owner="alice")
    mem = memory.read_memory(tmp_path, "m", "alice")
    assert mem.description == "before --- after"
    assert mem.content == "line1\n---\nline2"


def test_glossary_type_saved_and_searchable(tmp_path):
    memory.create_memory(tmp_path, "term-abc", "ABC（エービーシー）: 社内の受発注システム", "glossary", "受発注を管理する社内システム。", owner="alice")
    assert (tmp_path / "users" / "alice" / "glossary" / "term-abc.md").exists()
    assert memory.DEFAULT_TYPE_VISIBILITY["glossary"] == "shared"
