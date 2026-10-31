"""永続メモリー（create_memory 等）ツール群。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import logging

from .. import memory

from . import _state
from ._safe_path import _resolve_file_tools_path

logger = logging.getLogger(__name__)


def _require_memory_root() -> Path:
    """_state._MEMORY_ROOT を返す。init_tools() が未実行なら RuntimeError。"""
    if _state._MEMORY_ROOT is None:
        raise RuntimeError("init_tools() has not been run")
    return _state._MEMORY_ROOT


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


@tool
def create_memory(
    name: str, description: str, memory_type: str, content: str, source_files: list[str] | None = None
) -> str:
    """Save a new persistent memory.

    Use it when you learn a valuable fact worth carrying over to future conversations across
    threads. For when to save each memory_type and what must not be saved, see the Memory System
    section of the system prompt. If a memory with the same name already exists it is an error,
    so use update_memory to update an existing memory (if in doubt, first check for duplicates with
    search_memory / list_memories).

    Args:
        name: A unique name. ASCII letters (a-z, A-Z), digits, hyphens and underscores only,
            up to 64 characters. Do not write Japanese letters in the name (put them in
            description instead).
        description: A one-line description (shown as is in the index MEMORY.md).
        memory_type: One of "user" | "feedback" | "project" | "reference".
        content: The memory body. For the feedback/project types, it is best to include
            "**Why:**" and "**How to apply:**" lines after the rule/fact.
        source_files: When the content records what was read from files (values, table rows,
            key points with page numbers), the absolute paths (or `@N`) of those files. Their
            sha256 is recorded, and read_memory later tells whether the files are unchanged
            (then the content can be used without opening the files again). Only list files
            whose content was actually confirmed. At most 10 files; with source_files the
            content must be at most 4000 characters (keep only the relevant parts, not the
            whole file).

    Returns:
        A text telling the saved file path. If name/memory_type is invalid,
        description/content is empty, a memory with the same name already exists,
        or a source file cannot be read, it returns an "Error: ..." string instead of raising.
    """
    try:
        path = memory.create_memory(
            _require_memory_root(), name, description, memory_type, content, _resolve_source_files(source_files)
        )
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("create_memory: %s sources=%d", name, len(source_files or []))
    return f"Saved the memory: {path}"


@tool
def update_memory(name: str, content: str, source_files: list[str] | None = None) -> str:
    """Update the body of an existing persistent memory (name/description/memory_type do not change).

    Args:
        name: The name of the memory to update.
        content: The new body (replaces the existing body entirely).
        source_files: The absolute paths (or `@N`) of the files the new content was read from.
            Pass them whenever the content reflects files read again (their current sha256 is
            recorded). If omitted, the recorded source files are kept as they are; pass [] to
            remove them. Same limits as create_memory.

    Returns:
        A text telling the updated file path. If content is empty, no memory has that name,
        or a source file cannot be read, it returns an "Error: ..." string instead of raising.
    """
    try:
        path = memory.update_memory(_require_memory_root(), name, content, _resolve_source_files(source_files))
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("update_memory: %s sources=%s", name, "kept" if source_files is None else len(source_files))
    return f"Updated the memory: {path}"


@tool
def delete_memory(name: str) -> str:
    """Delete a persistent memory.

    Args:
        name: The name of the memory to delete.

    Returns:
        A text telling the deleted file path. If no memory has that name,
        it returns an "Error: ..." string instead of raising.
    """
    try:
        path = memory.delete_memory(_require_memory_root(), name)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("delete_memory: %s", name)
    return f"Deleted the memory: {path}"


@tool
def read_memory(name: str) -> str:
    """Read one persistent memory in full, including its body.

    search_memory / list_memories return only a list (name+description), so always read the full
    text with this tool before checking or quoting the content of a memory.

    Args:
        name: The name of the memory to read.

    Returns:
        The full text in the form "[type] name\\ndescription\\n\\ncontent". If the memory was
        recorded with source_files, a "[Source files]" section follows, telling for each file
        whether it is unchanged since recording (sha256 match) and whether the content can be
        used without opening the files again. If no memory has that name, it returns an
        "Error: ..." string instead of raising.
    """
    try:
        mem = memory.read_memory(_require_memory_root(), name)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    checks = _render_source_checks(mem)
    logger.info("read_memory: %s sources=%d", name, len(mem.sources))
    return f"[{mem.memory_type}] {mem.name}\n{mem.description}\n\n{mem.content}{checks}"


@tool
def search_memory(query: str, memory_type: str | None = None) -> str:
    """Search persistent memories by keyword substring match on name/description/body.

    Returns only a list (name+description), not the bodies (to save context).
    If you need the content itself, pass the name of a hit to read_memory.

    Args:
        query: The search keyword (case-insensitive).
        memory_type: If given, narrow to one of "user"|"feedback"|"project"|"reference"
            (all types if omitted).

    Returns:
        A list of hits as "- [type] name: description" and the count.
        With no hits, "No matching memories." If query is empty or memory_type is invalid,
        it returns an "Error: ..." string instead of raising.
    """
    try:
        hits = memory.search_memories(_require_memory_root(), query, memory_type)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("search_memory: query=%r type=%s hits=%d", query, memory_type, len(hits))
    if not hits:
        return "No matching memories."
    lines = [f"- [{m.memory_type}] {m.name}: {m.description}" for m in hits]
    return f"{len(hits)} hits.\n" + "\n".join(lines)


@tool
def list_memories(memory_type: str | None = None) -> str:
    """List the saved persistent memories.

    Args:
        memory_type: If given, narrow to one of "user"|"feedback"|"project"|"reference"
            (all types if omitted).

    Returns:
        A list of "- [type] name: description". With none,
        "No memories saved." If memory_type is invalid,
        it returns an "Error: ..." string instead of raising.
    """
    try:
        mems = memory.list_memories(_require_memory_root(), memory_type)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    logger.info("list_memories: type=%s count=%d", memory_type, len(mems))
    if not mems:
        return "No memories saved."
    return "\n".join(f"- [{m.memory_type}] {m.name}: {m.description}" for m in mems)
