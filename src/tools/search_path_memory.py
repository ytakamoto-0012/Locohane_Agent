"""search_path_memory ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import chainlit as cl
import json

from .. import path_memory

from . import _state

_DEFAULT_TOP_K = 5
_MAX_TOP_K = 50


@tool
def search_path_memory(query: str = "", top_k: int = 5) -> str:
    """Search the path memory (`@N`) of the current conversation for paths similar to query.

    Pass part of a file or folder name (full-/half-width differences, case, `\\` vs `/` and
    small typos are tolerated). Use it when you do not know the `@N`, or when told
    "Path memory @N is not registered". It is read-only, so you may call it any time regardless of
    the plan.

    Args:
        query: Part of the file/folder name to look for, or a path.
            If empty, returns the top_k most recently registered entries.
        top_k: The maximum number of entries to return (default 5, at most 50).

    Returns:
        A JSON string `{"entries": [{"index", "path", "valid", "description", "score"}, ...]}`
        (by similarity when query is given, newest first when empty; no score when empty).
        Paths deleted or moved after registration that no longer exist are not included.
        If there are no matches, a `hint` is added.
    """
    if _state._PATH_MEMORY_DIR is None:
        return json.dumps({"entries": []}, ensure_ascii=False)
    # 0以下だと必ず空になり「一致なし」と誤って案内してしまう。大きすぎる値は
    # 候補の実在確認（UNC では1件ずつ遅い）が増えるだけなので上限を設ける。
    top_k = _DEFAULT_TOP_K if not isinstance(top_k, int) or top_k < 1 else min(top_k, _MAX_TOP_K)
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    if query.strip():
        entries = path_memory.search_entries(
            thread_id,
            query,
            _state._PATH_MEMORY_DIR,
            top_k=top_k,
            min_score=_state._PATH_MEMORY_SEARCH_MIN_SCORE,
            filename_weight=_state._PATH_MEMORY_SEARCH_FILENAME_WEIGHT,
        )
    else:
        entries = path_memory.recent_entries(thread_id, _state._PATH_MEMORY_DIR, top_k=top_k)
    result: dict = {"entries": entries}
    if not entries:
        result["hint"] = "No matching entries. Search again with Glob."
    return json.dumps(result, ensure_ascii=False)
