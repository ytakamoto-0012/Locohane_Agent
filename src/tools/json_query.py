"""json_query ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import hashlib
import json
import logging

import jmespath
from jmespath.exceptions import JMESPathError

from ._duplicate_guard import _check_file_tools_duplicate
from ._file_tools_common import read_text_with_fallback
from ._path_memory_helpers import suggest_from_path_memory
from ._safe_path import _resolve_file_tools_path

logger = logging.getLogger(__name__)


def query_json(query: str, file_path: Path | None = None, json_text: str | None = None) -> dict:
    """JSONデータにJMESPathクエリを実行する。

    Args:
        query: JMESPathクエリ文字列。
        file_path: クエリ対象のJSONファイルの絶対パス。
        json_text: クエリ対象のJSON文字列を直接渡す場合に使う。
            file_path と同時指定・両方省略はエラー。

    Returns:
        {"result": ...}。

    Raises:
        ValueError: file_path/json_text の指定が排他条件を満たさない・
            ファイルが存在しない・JSONの解析に失敗した・
            JMESPathクエリが不正な場合。
    """
    if file_path is not None and json_text is not None:
        raise ValueError("file_path and json_text cannot be specified together")
    if file_path is None and json_text is None:
        raise ValueError("Specify either file_path or json_text")

    if file_path is not None:
        if not file_path.exists():
            raise ValueError(f"File not found: {file_path}")
        try:
            raw = read_text_with_fallback(file_path)
        except (OSError, UnicodeDecodeError) as e:
            raise ValueError(f"Failed to read the file: {e}") from e
    else:
        raw = json_text or ""
        if not raw.strip():
            raise ValueError("json_text is empty")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON: {e}") from e

    try:
        result = jmespath.search(query, data)
    except JMESPathError as e:
        raise ValueError(f"Invalid JMESPath query: {e}") from e

    return {"result": result}


@tool
def json_query(query: str, file_path: str = "", json_text: str = "") -> str:
    """Run a JMESPath query on JSON/dict data.

    Note that JMESPath syntax differs from jq.
    - Do not put `.` at the start of a path (`a.b`, not `.a.b`).
    - In filters `[?...]`, wrap numbers, booleans and null on the right-hand side in backquotes.
      No backslash escaping is needed (correct: `` age > `30` ``).
    - Wrap string literals in single quotes (e.g. `name == 'foo'`).

    Examples:
    - `items[?active == `true`].{name: name, id: id}` -> extract only elements whose active is
      true, shaped into name/id
    - `a.b[0].c` -> access nested keys and array indices

    It is read-only, so you may call it any time regardless of the plan.

    Args:
        query: The JMESPath query string.
        file_path: The absolute path of the JSON file to query (`@N` allowed).
            Always specify exactly one of file_path/json_text.
        json_text: Use this to pass the JSON string to query directly
            (e.g. to query the output of execute_python_code on the spot).
            Specifying it together with file_path, or omitting both, is an error.

    Returns:
        A JSON string `{"result": ...}` (`{"result": null}` if there is no matching data).
        If file_path/json_text is specified incorrectly, JSON parsing fails, or the query is
        invalid, it returns an "Error: ..." string instead of raising.
    """
    resolved_path: Path | None = None
    if file_path:
        resolved_path, error = _resolve_file_tools_path(file_path)
        if error:
            return f"Error: {error}"
    signature_source = json_text if not file_path else ""
    signature = (
        f"json_query\x00{query}\x00{resolved_path or ''}\x00"
        f"{hashlib.sha256(signature_source.encode('utf-8')).hexdigest() if signature_source else ''}"
    )
    dup_error = _check_file_tools_duplicate("json_query", signature)
    if dup_error:
        return dup_error
    try:
        result = query_json(query, file_path=resolved_path, json_text=json_text or None)
    except ValueError as e:
        hint = suggest_from_path_memory(str(resolved_path)) if resolved_path and not resolved_path.exists() else ""
        return f"Error: {e}{hint}"
    logger.info("json_query: %s", query)
    return json.dumps(result, ensure_ascii=False)
