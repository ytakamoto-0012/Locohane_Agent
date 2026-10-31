"""Read ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import json
import logging

from ._duplicate_guard import _check_file_tools_duplicate
from ._file_tools_common import looks_binary, read_text_with_fallback
from ._path_memory_helpers import _register_path_memory, suggest_from_path_memory
from ._safe_path import _resolve_file_tools_path

logger = logging.getLogger(__name__)


def read_file(path: Path, offset: int = 0, limit: int = 10) -> dict:
    """行番号付きでテキストファイルを読み込む。

    Args:
        path: 読み込む絶対パス。
        offset: 読み飛ばす先頭行数（0始まり）。
        limit: 読み込む最大行数。

    Returns:
        {"path", "total_lines", "start_line", "end_line", "content"}。

    Raises:
        ValueError: ファイルが存在しない・ディレクトリ・バイナリ・
            読み込み権限が無い・読み込みに失敗した場合。
    """
    if not path.exists():
        raise ValueError(f"File not found: {path}")
    if path.is_dir():
        raise ValueError(f"The path is a directory (files only): {path}")
    if looks_binary(path):
        raise ValueError(f"Cannot read because it may be a binary file: {path}")

    try:
        text = read_text_with_fallback(path)
    except PermissionError as e:
        raise ValueError(f"No permission to access the file: {path}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"Failed to read the file: {e}") from e

    lines = text.splitlines()
    total_lines = len(lines)
    offset = max(offset, 0)
    selected = lines[offset : offset + limit]

    return {
        "path": str(path),
        "total_lines": total_lines,
        "start_line": offset + 1 if selected else None,
        "end_line": offset + len(selected) if selected else None,
        "content": "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(selected)),
    }


@tool("Read")
def read_tool(file_path: str, offset: int = 0, limit: int = 10) -> str:
    """Read any text file on the local file system with line numbers.

    Unlike read_skill_file, which is limited to the skills directory, it does not restrict paths
    (the purpose is to read any absolute path the user specifies, except other sessions'
    `_tmp_<created time>_<short ID>` folders, which cannot be read). Use read_skill_file to read
    skill bodies and supporting material, and this tool to read files the user specified. It is
    read-only, so you may call it any time regardless of the plan.

    Args:
        file_path: The absolute path to read (a path memory reference `@N` is also allowed).
            A relative path is resolved against the working directory.
        offset: The number of leading lines to skip (0-based, default 0).
        limit: The maximum number of lines to read (default 10). To read the rest of a large
            file, set offset to the previous end_line.

    Returns:
        A JSON string with `{"path", "total_lines", "start_line", "end_line", "content", "path_memory"}`.
        `content` is "line number\\tcontent" lines joined with newlines.
        A missing file, a directory, a binary file, or calling again with the same arguments
        (allowed up to a limit because it is read-only) returns an "Error: ..." string instead of
        raising.
    """
    path, error = _resolve_file_tools_path(file_path)
    if error:
        return f"Error: {error}"
    dup_error = _check_file_tools_duplicate("Read", f"Read\x00{path}\x00{offset}\x00{limit}")
    if dup_error:
        return dup_error
    try:
        result = read_file(path, offset=offset, limit=limit)
    except ValueError as e:
        hint = suggest_from_path_memory(str(path)) if not path.exists() else ""
        return f"Error: {e}{hint}"
    path_memory = _register_path_memory([result["path"]])
    if path_memory:
        result["path_memory"] = path_memory
    logger.info("Read: %s", path)
    return json.dumps(result, ensure_ascii=False)
