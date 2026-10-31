"""Grep ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import fnmatch
import json
import logging
import os
import re

from ._duplicate_guard import _check_file_tools_duplicate
from ._file_tools_common import looks_binary, read_text_with_fallback, suggest_similar_dir
from ._path_memory_helpers import _register_path_memory, suggest_from_path_memory
from ._safe_path import _resolve_file_tools_path
from .. import skill_drafts
from . import _state
from ._workdir import _foreign_tmp_dir_names

logger = logging.getLogger(__name__)

_OUTPUT_MODES = ("files_with_matches", "content", "count")

# Grep ツールが返す matches の合計文字数（パス＋行本文）の上限と、1行あたりの上限。
# head_limit は件数しか絞れず、LLM が `pattern=".*", head_limit=5000` のように
# 呼ぶと全ファイルの全行が1つの ToolMessage に載り、それだけでコンテキスト長を
# 超えてサブエージェントが異常終了した（2026-10-01、006 で1回45.9万トークン）。
_MAX_RESULT_CHARS = 20000
_MAX_LINE_CHARS = 500


def _cap_result_size(result: dict) -> None:
    """content モードの結果の matches を合計・1行あたりの文字数上限に収める（in-place）。"""
    kept = []
    total = 0
    for m in result["matches"]:
        if len(m["text"]) > _MAX_LINE_CHARS:
            m = {**m, "text": m["text"][:_MAX_LINE_CHARS] + "…"}
        total += len(m["path"]) + len(m["text"])
        if total > _MAX_RESULT_CHARS and kept:
            break
        kept.append(m)
    if kept == result["matches"]:
        return
    if len(kept) < len(result["matches"]):
        result["truncated"] = True
        result["hint"] = (
            f"The result was too large, so only the first {len(kept)} entries were returned. "
            "Narrow pattern, glob or path and search again. If you only need file names, use Glob."
        )
    result["matches"] = kept
    result["returned"] = len(kept)


def grep_search(
    base: Path,
    pattern: str,
    glob: str = "",
    output_mode: str = "content",
    case_insensitive: bool = False,
    context: int = 0,
    head_limit: int = 50,
    exclude_names: frozenset[str] = frozenset(),
    exclude_paths: frozenset[Path] = frozenset(),
) -> dict:
    """指定ファイル/ディレクトリ配下のテキストから正規表現で検索する。

    Args:
        base: 検索対象の絶対パス（ファイルまたはディレクトリ）。
        pattern: 検索する正規表現。
        glob: ディレクトリ検索時にファイル名を絞り込むglobパターン（省略可）。
        output_mode: "files_with_matches" | "content" | "count"。
        case_insensitive: 大文字小文字を無視するか。
        context: "content" モード時、マッチ行の前後何行を含めるか。
        head_limit: 返却件数の上限。
        exclude_names: 走査対象から除外するディレクトリ名の集合
            （basenameで一致するディレクトリは配下ごと走査しない。空なら
            従来通り無条件で全て対象）。
        exclude_paths: 走査対象から除外するディレクトリの絶対パス（解決済み）の
            集合（他ユーザーのドラフトスキルのように、名前ではなく場所で除外
            したいもの。配下ごと走査しない）。

    Returns:
        output_mode に応じた形状の辞書（マッチ0件は
        {"matched": False, "files": [], "matches": [], "counts": []}）。

    Raises:
        ValueError: 正規表現が不正・output_modeが不正・対象パスが存在しない場合。
    """
    if output_mode not in _OUTPUT_MODES:
        raise ValueError(f"Invalid output_mode: {output_mode}")
    try:
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as e:
        raise ValueError(f"Invalid regular expression: {e}") from e

    if not base.exists():
        hint = suggest_similar_dir(base)
        raise ValueError(f"Search target path not found: {base}{hint}")

    target_files: list[Path] = []
    if base.is_file():
        target_files.append(base)
    else:
        for root, dirs, files in os.walk(base):
            if exclude_names:
                dirs[:] = [d for d in dirs if d not in exclude_names]
            if exclude_paths:
                dirs[:] = [d for d in dirs if (Path(root) / d).resolve() not in exclude_paths]
            for name in files:
                if glob and not fnmatch.fnmatch(name, glob):
                    continue
                target_files.append(Path(root) / name)

    files_out: list[str] = []
    counts_out: list[dict] = []
    matches_out: list[dict] = []

    for file_path in target_files:
        if looks_binary(file_path):
            continue
        try:
            text = read_text_with_fallback(file_path)
        except (OSError, UnicodeDecodeError):
            continue

        lines = text.splitlines()
        match_line_indexes = [i for i, line in enumerate(lines) if regex.search(line)]
        if not match_line_indexes:
            continue

        resolved = str(file_path.resolve())
        files_out.append(resolved)
        counts_out.append({"path": resolved, "count": len(match_line_indexes)})

        if output_mode == "content":
            for idx in match_line_indexes:
                start = max(0, idx - context)
                end = min(len(lines), idx + context + 1)
                for i in range(start, end):
                    matches_out.append({"path": resolved, "line": i + 1, "text": lines[i]})

        if len(files_out) >= head_limit and output_mode != "content":
            break

    if not files_out:
        return {"matched": False, "files": [], "matches": [], "counts": []}

    if output_mode == "files_with_matches":
        truncated = files_out[:head_limit]
        return {
            "matched": True,
            "total_files": len(files_out),
            "returned": len(truncated),
            "files": truncated,
        }
    if output_mode == "count":
        truncated_counts = counts_out[:head_limit]
        return {
            "matched": True,
            "total_files": len(counts_out),
            "returned": len(truncated_counts),
            "counts": truncated_counts,
        }
    truncated_matches = matches_out[:head_limit]
    return {
        "matched": True,
        "total_matches": len(matches_out),
        "returned": len(truncated_matches),
        "truncated": len(matches_out) > head_limit,
        "matches": truncated_matches,
    }


@tool("Grep")
def grep_tool(
    pattern: str,
    path: str = "",
    glob: str = "",
    case_insensitive: bool = False,
    context: int = 0,
    head_limit: int = 50,
) -> str:
    """Search text under a file or directory with a regular expression and return the matching line numbers and contents.

    It is read-only, so you may call it any time regardless of the plan.
    It searches file contents (text), not file names (use `Glob` to search file names).

    Args:
        pattern: The regular expression to search for.
        path: The absolute path to search (a file or directory, `@N` allowed).
            Defaults to searching under the working directory.
        glob: A glob pattern to narrow the target files by name before searching (optional,
            e.g. "*.py"). Only effective when searching a directory. A pre-filter that does not
            appear in the results.
        case_insensitive: If True, ignore case.
        context: How many lines before and after a match to include (default 0).
        head_limit: The maximum number of matches to return (default 50).

    Returns:
        A JSON string shaped like `{"matched", "total_matches", "returned", "truncated",
        "matches": [{"path", "line", "text"}, ...], "path_memory"}`.
        With no matches, `{"matched": false, "files": [], "matches": [], "counts": []}`.
        If the regular expression is invalid or the target path does not exist, it returns an
        "Error: ..." string instead of raising.
    """
    base, error = _resolve_file_tools_path(path)
    if error:
        return f"Error: {error}"
    signature = f"Grep\x00{pattern}\x00{base}\x00{glob}\x00" f"{case_insensitive}\x00{context}\x00{head_limit}"
    dup_error = _check_file_tools_duplicate("Grep", signature)
    if dup_error:
        return dup_error
    try:
        result = grep_search(
            base,
            pattern,
            glob=glob,
            output_mode="content",
            case_insensitive=case_insensitive,
            context=context,
            head_limit=head_limit,
            exclude_names=_foreign_tmp_dir_names(),
            exclude_paths=frozenset(skill_drafts.unreadable_dirs(_state._LLM_CONFIG, skill_drafts.current_draft_user())),
        )
    except ValueError as e:
        hint = suggest_from_path_memory(str(base)) if not base.exists() else ""
        return f"Error: {e}{hint}"
    if result["matched"]:
        _cap_result_size(result)
        paths = list(dict.fromkeys(m["path"] for m in result["matches"]))
        path_memory = _register_path_memory(paths)
        if path_memory:
            result["path_memory"] = path_memory
    logger.info("Grep: pattern=%s base=%s", pattern, base)
    return json.dumps(result, ensure_ascii=False)
