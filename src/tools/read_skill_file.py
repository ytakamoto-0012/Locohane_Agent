"""read_skill_file ツール（progressive disclosure 第3段階）。"""

from __future__ import annotations

from langchain_core.tools import tool
import logging

from pathlib import Path

from .. import skill_drafts
from . import _state
from ._duplicate_guard import _check_file_tools_duplicate
from ._path_memory_helpers import _PATH_MEMORY_TOKEN_RE, _resolve_path_memory_token
from ._safe_path import _missing_skill_prefix_hint, _safe_path

logger = logging.getLogger(__name__)

_WORKDIR_FILE_HINT = (
    "(read_skill_file is limited to the skills directory. Read files under the working directory "
    "with the Read tool. If Read is not available, delegate with dispatch_agent)"
)


def _resolve_skill_file_path(relative_path: str) -> Path:
    """relative_path（skills ルートからの相対パス、または `@N`）を絶対パスへ解決する。

    `@N` は read_skill が references/ 一覧に付けたパスメモリー参照を想定する。
    解決先が skills ルート配下に収まらない `@N`（作業ディレクトリ側のファイル等）は
    _safe_path() と同じく境界外として拒否する。

    Raises:
        ValueError: `@N` が未登録、または解決先が skills ルート外の場合。
    """
    if not _PATH_MEMORY_TOKEN_RE.match(relative_path):
        return _safe_path(relative_path)
    resolved, error = _resolve_path_memory_token(relative_path)
    if error:
        raise ValueError(error)
    path = Path(resolved).resolve()
    if not any(path.is_relative_to(root) for root in _state._SKILLS_ROOTS or []):
        raise ValueError(f"{relative_path} is outside the skills directory")
    draft_error = skill_drafts.access_error(path, "read", _state._LLM_CONFIG, skill_drafts.current_draft_user())
    if draft_error:
        raise ValueError(draft_error.removeprefix("Error: "))
    return path


@tool
def read_skill_file(relative_path: str) -> str:
    """Read and return a file under the skills directory.

    Use it only when needed, e.g. when the SKILL.md body refers to references/assets.
    Part of the third stage (Execute) of progressive disclosure in the Agent Skills standard.

    Args:
        relative_path: A path relative to the skills root, or an `@N` from the references/ list of
            read_skill. For a relative path, always include the skill folder name at the start,
            even after reading the skill with read_skill(skill_name)
            (e.g. "excel-knowledge/references/notes.md", not
            "references/notes.md").

    Returns:
        The file contents (UTF-8; undecodable bytes are replaced with errors="replace").
        If it points outside the skills root or the file does not exist, it returns an
        "Error: ..." string instead of raising.
    """
    try:
        path = _resolve_skill_file_path(relative_path)
    except ValueError as e:
        return f"Error: {e}{_WORKDIR_FILE_HINT}"
    if not path.is_file():
        # スキル名プレフィックス漏れか、作業ディレクトリのファイルを渡したのかは
        # 区別できないため、両方の代替行動を示す（メインエージェントが作業
        # ディレクトリのファイル名をそのまま渡す誤りが実際にあった）。
        hint = _missing_skill_prefix_hint(relative_path)
        return f"Error: file not found: {relative_path}{hint}{_WORKDIR_FILE_HINT}"
    dup_error = _check_file_tools_duplicate("read_skill_file", f"read_skill_file\x00{path}")
    if dup_error:
        return dup_error
    logger.info("read_skill_file: %s", relative_path)
    return path.read_text(encoding="utf-8", errors="replace")
