"""永続メモリー（create_memory 等）ツール群。

メモリーはユーザー別（src/memory.py）。ツールは会話のユーザーとして動き、作成は自分のメモリー、
更新・削除は自分のもの（無ければ共通のもの）だけ、検索・一覧・読み取りは自分・共通・他のユーザーの
共有のもの（他のユーザーの個人用のものは src/memory.py が機械的に除く）を対象にする。
"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import logging

from .. import memory
from .. import skill_drafts

from . import _state
from ._safe_path import _resolve_file_tools_path

logger = logging.getLogger(__name__)

# read_memory の owner で共通のメモリーを指す値（一覧の " (common)" と同じ語）。
_COMMON_OWNER_ARG = "common"
# 一覧・検索では自分のメモリーを "(mine, ...)" と表示するため、owner="mine" は自分として受け付ける。
_MINE_OWNER_ARG = "mine"


def _require_memory_root() -> Path:
    """_state._MEMORY_ROOT を返す。init_tools() が未実行なら RuntimeError。"""
    if _state._MEMORY_ROOT is None:
        raise RuntimeError("init_tools() has not been run")
    return _state._MEMORY_ROOT


def _current_owner() -> str:
    """会話のユーザー（メモリーの所有者のフォルダ名）。セッション外（evals・テスト等）は匿名ユーザー。

    スキルのドラフトと同じ名前の整え方（skill_drafts.draft_owner_name）を使い、
    Read/Grep 等で他のユーザーのメモリーフォルダを読ませないガード（skill_drafts.unreadable_dirs）と揃える。
    """
    return skill_drafts.current_draft_user() or skill_drafts.draft_owner_name(None)


def _writable_owner(root: Path, name: str, me: str) -> str | None:
    """update/delete の対象の所有者: 自分のものがあれば自分、無ければ共通（None）。

    どちらにも無ければ ValueError（他のユーザーのものしか無い場合は、書き換えられないことを伝える）。
    """
    if memory.find_memory(root, name, me) is not None:
        return me
    if memory.find_memory(root, name, None) is not None:
        return None
    try:
        other = memory.read_visible_memory(root, name, me)
    except ValueError:
        raise ValueError(f"Memory not found: {name}") from None
    raise ValueError(
        f"The memory {name} belongs to another user ({other.owner}) and cannot be changed or deleted. "
        "Save your own memory with create_memory instead (your own memory takes precedence unless the other user "
        "has a higher rank)."
    )


def _resolve_source_files(source_files: list[str] | None) -> list[Path] | None:
    """source_files の `@N`・相対パスを Read と同じ規則で絶対パスへ解決する。解決できなければ ValueError。"""
    if source_files is None:
        return None
    resolved: list[Path] = []
    for raw in source_files:
        p, error = _resolve_file_tools_path(raw)
        if error or p is None:
            raise ValueError(f"source_files: {error or raw}")
        resolved.append(p)
    return resolved


def _visibility_note(root: Path, mem: memory.Memory, requested: str | None) -> str:
    """保存後の公開範囲の説明（shared を求めた・省略して shared のはずが private になった場合は理由も）。"""
    if mem.visibility == "shared":
        return "Visibility: shared (other users can find and read it)."
    if requested != "private":
        _, reasons = memory.resolve_visibility(
            mem.memory_type, "shared", mem.description, mem.content, mem.sources, memory.policy_for(root)
        )
        if reasons:
            return f"Visibility: private (not shared with other users because it contains: {', '.join(reasons)})."
    return "Visibility: private (only the current user can find and read it)."


def _render_source_checks(mem: memory.Memory) -> str:
    """read_memory の末尾に付ける、元ファイルの照合結果の節（sources が無ければ空文字）。"""
    checks = memory.check_sources(mem)
    if not checks:
        return ""
    labels = {
        "unchanged": "unchanged (sha256 matches the recorded one)",
        "changed": "CHANGED since this memory was recorded",
        "missing": "MISSING (not found or cannot be read)",
    }
    lines = ["", "", "[Source files]"]
    lines += [f"- {c.source.path}: {labels[c.state]}" for c in checks]
    if all(c.state == "unchanged" for c in checks):
        lines.append(
            "All source files are unchanged: you may use the content above as is and cite these files as the "
            "source, without opening them again."
        )
    else:
        lines.append(
            "Do not use the values taken from the changed/missing files: have them read again (explore), "
            "then update this memory with update_memory(source_files=[...]) so the new sha256 is recorded."
        )
    return "\n".join(lines)


def _list_line(root: Path, mem: memory.Memory, me: str) -> str:
    return f"- [{mem.memory_type}] {mem.name}{memory.label_for(mem, me, memory.policy_for(root))}: {mem.description}"


def _other_user_note(policy: memory.MemoryPolicy, mem: memory.Memory, me: str) -> str:
    """他のユーザーのメモリーを読んだときの、自分のメモリーと食い違ったときの従い方。"""
    if not policy.uses_ranks:
        return (
            "\n\n(This is another user's shared memory. If one of your own memories covers the same topic "
            "and says otherwise, follow your own.)"
        )
    theirs, mine = policy.rank_of(mem.owner), policy.rank_of(me)
    if theirs > mine:
        rule = "follow this one (its owner has a higher rank)"
    elif theirs < mine:
        rule = "follow your own (your rank is higher)"
    else:
        rule = "follow your own (same rank)"
    return (
        f"\n\n(This is another user's shared memory: owner's rank {theirs}, your rank {mine}. If another memory "
        f"covers the same topic and says otherwise, follow the one whose owner has the higher rank; for your own "
        f"memory, {rule}.)"
    )


@tool
def create_memory(
    name: str,
    description: str,
    memory_type: str,
    content: str,
    source_files: list[str] | None = None,
    visibility: str | None = None,
) -> str:
    """Save a new persistent memory as the current user's own memory.

    Use it when you learn a valuable fact worth carrying over to future conversations across
    threads. For when to save each memory_type and what must not be saved, see the Memory System
    section of the system prompt. If you already have a memory with the same name it is an error,
    so use update_memory to update an existing memory (if in doubt, first check for duplicates with
    search_memory / list_memories).

    Args:
        name: A unique name. ASCII letters (a-z, A-Z), digits, hyphens and underscores only,
            up to 64 characters. Do not write Japanese letters in the name (put them in
            description instead).
        description: A one-line description (shown as is in the index MEMORY.md).
        memory_type: One of "user" | "feedback" | "project" | "reference" | "glossary".
        content: The memory body. For the feedback/project types, it is best to include
            "**Why:**" and "**How to apply:**" lines after the rule/fact.
        source_files: When the content records what was read from files (values, table rows,
            key points with page numbers), the absolute paths (or `@N`) of those files. Their
            sha256 is recorded, and read_memory later tells whether the files are unchanged
            (then the content can be used without opening the files again). Only list files
            whose content was actually confirmed. At most 10 files; with source_files the
            content must be at most 4000 characters (keep only the relevant parts, not the
            whole file).
        visibility: "shared" lets other users of this system find and read the memory;
            "private" keeps it to the current user. Choose it by the content, following the
            Visibility rules in the Memory System section of the system prompt. If omitted, it is
            shared. Some memory_types and content with personal details are always saved as
            private; the result tells the visibility it was saved with and why.

    Returns:
        A text telling the saved file path and the visibility it was saved with. If
        name/memory_type/visibility is invalid, description/content is empty, you already
        have a memory with the same name, or a source file cannot be read, it returns an
        "Error: ..." string instead of raising.
    """
    try:
        root = _require_memory_root()
        me = _current_owner()
        path = memory.create_memory(
            root,
            name,
            description,
            memory_type,
            content,
            _resolve_source_files(source_files),
            owner=me,
            visibility=visibility,
        )
        mem = memory.read_memory(root, name, me)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info(
        "create_memory: %s type=%s sources=%d visibility=%s (requested=%s)",
        name,
        memory_type,
        len(source_files or []),
        mem.visibility,
        visibility,
    )
    return f"Saved the memory: {path}\n{_visibility_note(root, mem, visibility)}"


@tool
def update_memory(
    name: str, content: str, source_files: list[str] | None = None, visibility: str | None = None
) -> str:
    """Update the body of an existing persistent memory (name/description/memory_type do not change).

    Updates your own memory with that name, or else the common memory with that name. Memories of
    other users cannot be updated (save your own with create_memory instead).

    Args:
        name: The name of the memory to update.
        content: The new body (replaces the existing body entirely).
        source_files: The absolute paths (or `@N`) of the files the new content was read from.
            Pass them whenever the content reflects files read again (their current sha256 is
            recorded). If omitted, the recorded source files are kept as they are; pass [] to
            remove them. Same limits as create_memory.
        visibility: "shared" or "private" to change the visibility of your own memory (same rules
            as create_memory). If omitted, it stays as it is (but becomes private if the new
            content has personal details). Ignored for common memories.

    Returns:
        A text telling the updated file path. If content is empty, no memory has that name,
        the memory belongs to another user, or a source file cannot be read, it returns an
        "Error: ..." string instead of raising.
    """
    try:
        root = _require_memory_root()
        me = _current_owner()
        owner = _writable_owner(root, name, me)
        path = memory.update_memory(
            root,
            name,
            content,
            _resolve_source_files(source_files),
            owner=owner,
            visibility=visibility,
            updated_by=me,
        )
        mem = memory.read_memory(root, name, owner)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info(
        "update_memory: %s sources=%s visibility=%s (requested=%s)",
        name,
        "kept" if source_files is None else len(source_files),
        mem.visibility,
        visibility,
    )
    if owner is None:
        return f"Updated the common memory: {path}"
    return f"Updated the memory: {path}\n{_visibility_note(root, mem, visibility)}"


@tool
def delete_memory(name: str) -> str:
    """Delete a persistent memory (your own memory with that name, or else the common one).

    Memories of other users cannot be deleted.

    Args:
        name: The name of the memory to delete.

    Returns:
        A text telling the deleted file path. If no memory has that name or it belongs to
        another user, it returns an "Error: ..." string instead of raising.
    """
    try:
        root = _require_memory_root()
        path = memory.delete_memory(root, name, _writable_owner(root, name, _current_owner()))
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("delete_memory: %s", name)
    return f"Deleted the memory: {path}"


@tool
def read_memory(name: str, owner: str | None = None) -> str:
    """Read one persistent memory in full, including its body.

    search_memory / list_memories return only a list (name+description), so always read the full
    text with this tool before checking or quoting the content of a memory.

    Args:
        name: The name of the memory to read.
        owner: Usually omit it: your own memory with that name is read first, then the common
            one, then another user's shared one. Pass "mine" (your own), "common", or the user shown
            as "(user: <owner>)" in search results, to read that one instead.

    Returns:
        The full text in the form "[type] name (owner)\\ndescription\\n\\ncontent". If the memory
        was recorded with source_files, a "[Source files]" section follows, telling for each file
        whether it is unchanged since recording (sha256 match) and whether the content can be
        used without opening the files again. If no memory that you can read has that name, it
        returns an "Error: ..." string instead of raising.
    """
    try:
        root = _require_memory_root()
        me = _current_owner()
        if owner is None:
            mem = memory.read_visible_memory(root, name, me)
        else:
            scope = None if owner == _COMMON_OWNER_ARG else (me if owner == _MINE_OWNER_ARG else owner)
            mem = memory.read_visible_memory(root, name, me, scope, auto=False)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    checks = _render_source_checks(mem)
    logger.info("read_memory: %s owner=%s sources=%d", name, mem.owner, len(mem.sources))
    policy = memory.policy_for(root)
    note = _other_user_note(policy, mem, me) if mem.owner is not None and mem.owner != me else ""
    header = f"[{mem.memory_type}] {mem.name}{memory.label_for(mem, me, policy)}"
    if mem.author and mem.author != mem.owner:
        header += f"\nAuthor: {mem.author}"
    if mem.updated_by and mem.updated_by != mem.author:
        header += f"\nLast updated by: {mem.updated_by}"
    return f"{header}\n{mem.description}\n\n{mem.content}{checks}{note}"


@tool
def search_memory(query: str, memory_type: str | None = None) -> str:
    """Search persistent memories by keyword substring match on name/description/body.

    Searches your own memories, the common memories and other users' shared memories (other
    users' private memories are never included), marked "(mine, ...)", "(common)" and
    "(user: <owner>)". When ranks are shown ("rank N"; higher is more senior), hits are ordered
    from the highest rank; within the same rank, your own first, then common, then other users'.

    Returns only a list (name+description), not the bodies (to save context).
    If you need the content itself, pass the name of a hit to read_memory.

    Args:
        query: The search keyword (case-insensitive).
        memory_type: If given, narrow to one of "user"|"feedback"|"project"|"reference"|"glossary"
            (all types if omitted).

    Returns:
        A list of hits as "- [type] name (owner): description" and the count.
        With no hits, "No matching memories." If query is empty or memory_type is invalid,
        it returns an "Error: ..." string instead of raising.
    """
    try:
        me = _current_owner()
        root = _require_memory_root()
        hits = memory.search_memories(root, query, memory_type, viewer=me)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("search_memory: query=%r type=%s hits=%d", query, memory_type, len(hits))
    if not hits:
        return "No matching memories."
    return f"{len(hits)} hits.\n" + "\n".join(_list_line(root, m, me) for m in hits)


@tool
def list_memories(memory_type: str | None = None) -> str:
    """List the persistent memories you can read (your own, the common ones and other users' shared ones).

    Ordered from the owner with the highest rank (when ranks are shown); within the same rank, your
    own first, then common, then other users'.

    Args:
        memory_type: If given, narrow to one of "user"|"feedback"|"project"|"reference"|"glossary"
            (all types if omitted).

    Returns:
        A list of "- [type] name (owner): description". With none,
        "No memories saved." If memory_type is invalid,
        it returns an "Error: ..." string instead of raising.
    """
    try:
        me = _current_owner()
        root = _require_memory_root()
        mems = memory.list_memories(root, memory_type, viewer=me)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("list_memories: type=%s count=%d", memory_type, len(mems))
    if not mems:
        return "No memories saved."
    return "\n".join(_list_line(root, m, me) for m in mems)
