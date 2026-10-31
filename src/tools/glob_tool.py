"""Glob ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import json
import logging
import os
import re

from ._duplicate_guard import _check_file_tools_duplicate
from ._file_tools_common import looks_binary, read_text_with_fallback, suggest_similar_dir
from ._path_memory_helpers import _dedupe_paths_with_path_memory, _register_path_memory, suggest_from_path_memory
from ._safe_path import _resolve_file_tools_path
from ._workdir import _foreign_tmp_dir_names

logger = logging.getLogger(__name__)


def _count_contents(path: Path, exclude_names: frozenset[str] = frozenset()) -> dict:
    """path直下(非再帰)に含まれるサブフォルダ数とファイル数を数える。

    深い階層まで舐めると時間がかかりすぎるため、意図的に直下のみを見る
    （配下全体の再帰カウントはしない）。サブエージェントへの分割委譲判断
    に使う概算値のため、権限エラー等の例外は無視して数えられた範囲だけ
    を返す。

    Args:
        exclude_names: カウントから除外するディレクトリ名の集合
            （basenameで一致判定。空なら従来通り無条件で全て数える）。
    """
    dir_count = 0
    file_count = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir():
                    if exclude_names and entry.name in exclude_names:
                        continue
                    dir_count += 1
                elif entry.is_file():
                    file_count += 1
    except OSError:
        pass
    return {"directory_count": dir_count, "file_count": file_count}


def _file_detail(path: Path) -> dict:
    """head-limit適用後の1ファイルについてバイナリ判定と総行数を調べる。

    total_lines は read_file() の total_lines 算出（splitlines の長さ）と
    完全に揃え、モデルがそのまま limit の値を決められるようにする。
    """
    resolved = str(path.resolve())
    if looks_binary(path):
        return {"path": resolved, "binary": True, "total_lines": None}
    try:
        text = read_text_with_fallback(path)
    except (OSError, UnicodeDecodeError):
        return {"path": resolved, "binary": False, "total_lines": None}
    return {"path": resolved, "binary": False, "total_lines": len(text.splitlines())}


_BRACE_GROUP_RE = re.compile(r"\{([^{}]+)\}")

# pathlib.Path.glob は絶対パスのパターン（"C:/images/*.jpg" 等）を ValueError ではなく
# NotImplementedError で拒否する。ToolNode の既定エラーハンドラはこれを再送出して
# ターン全体を落とすため、呼び出し側で捕捉してこのヒントを添えたエラー文字列に変える。
GLOB_PATTERN_ERRORS = (ValueError, NotImplementedError)
ABSOLUTE_PATTERN_HINT = "(Write only the file name part (e.g. *.jpg) in pattern, and pass the folder in path)"


def glob_pattern_error_message(error: Exception) -> str:
    """GLOB_PATTERN_ERRORS を「パターンが不正です: ...」の文言へ変換する。"""
    hint = ABSOLUTE_PATTERN_HINT if isinstance(error, NotImplementedError) else ""
    return f"Invalid pattern: {error}{hint}"


def _expand_braces(pattern: str) -> list[str]:
    """globパターン中の `{a,b,c}` をシェル同様の選択展開として複数パターンへ展開する。

    `pathlib.Path.glob` はブレース展開を解釈せず `{`/`}` を単なるリテラル文字と
    扱うため、`*.{jpg,jpeg,png}` のようなLLMが書きがちなパターンが常に0件に
    なってしまう問題への対応(braceが無ければ元のパターンのみを1件で返す)。
    """
    match = _BRACE_GROUP_RE.search(pattern)
    if not match:
        return [pattern]
    prefix, options, rest = pattern[: match.start()], match.group(1).split(","), pattern[match.end() :]
    return [f"{prefix}{opt}{tail}" for opt in options for tail in _expand_braces(rest)]


def glob_search(base: Path, pattern: str, head_limit: int = 200, exclude_names: frozenset[str] = frozenset()) -> dict:
    """指定ディレクトリ直下でglobパターンに一致するファイル/ディレクトリを検索する。

    深い階層まで舐めると時間がかかりすぎるため、`pattern` に `**` を含めても
    `base` の直下しか探索しない（ハードコードの制限で、引数での変更は不可）。

    Args:
        base: 検索起点ディレクトリの絶対パス。
        pattern: globパターン（例: "*.py"）。
        head_limit: ファイル・ディレクトリそれぞれに独立に適用する上限件数。
        exclude_names: 結果・件数から除外するディレクトリ名の集合
            （basenameで一致するパス階層を含む一致は丸ごと除外。呼び出し元
            が「他セッションの一時ディレクトリ」等、汎用的な理由で除外
            したい名前を渡す。空なら従来通り無条件で全て対象）。

    Returns:
        {"base", "base_contents", "total_matches", "returned", "truncated",
         "files", "file_details", "total_directories", "returned_directories",
         "directories_truncated", "directories"}。

    Raises:
        ValueError: 起点ディレクトリが存在しない・ディレクトリでない・
            パターンが不正な場合。
    """
    if not base.exists():
        hint = suggest_similar_dir(base)
        raise ValueError(f"Search base directory not found: {base}{hint}")
    if not base.is_dir():
        raise ValueError(f"Search base path is not a directory: {base}")

    resolved_base = base.resolve()
    try:
        seen: set[Path] = set()
        all_matches: list[Path] = []
        for expanded_pattern in _expand_braces(pattern):
            for p in base.glob(expanded_pattern):
                if p.parent.resolve() != resolved_base:
                    continue
                if p not in seen:
                    seen.add(p)
                    all_matches.append(p)
    except GLOB_PATTERN_ERRORS as e:
        raise ValueError(glob_pattern_error_message(e)) from e

    if exclude_names:
        all_matches = [p for p in all_matches if not (set(p.parts) & exclude_names)]

    file_matches = [p for p in all_matches if p.is_file()]
    dir_matches = [p for p in all_matches if p.is_dir()]
    file_matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    dir_matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    truncated_files = file_matches[:head_limit]
    truncated_dirs = dir_matches[:head_limit]

    files = [str(p.resolve()) for p in truncated_files]
    directory_paths = [str(p.resolve()) for p in truncated_dirs]
    file_details = [_file_detail(p) for p in truncated_files]
    directories = [
        {"path": path_str, **_count_contents(p, exclude_names)} for path_str, p in zip(directory_paths, truncated_dirs)
    ]
    base_contents = _count_contents(base, exclude_names)

    return {
        "base": str(base.resolve()),
        "base_contents": base_contents,
        "total_matches": len(file_matches),
        "returned": len(truncated_files),
        "truncated": len(file_matches) > head_limit,
        "files": files,
        "file_details": file_details,
        "total_directories": len(dir_matches),
        "returned_directories": len(truncated_dirs),
        "directories_truncated": len(dir_matches) > head_limit,
        "directories": directories,
    }


@tool("Glob")
def glob_tool(pattern: str, path: str = "", head_limit: int = 200) -> str:
    """Search for files and directories matching a glob pattern directly under a directory.

    Only entries directly under `path` are searched; deeper subdirectories are not explored
    (searching deep levels takes too long). To look deeper, call it again with the target
    subdirectory as `path`.

    Use it with `"*"` not only to search file names but also to investigate the directory structure
    itself (e.g. when there may be no files directly under the target, only subdirectories).
    It is read-only, so you may call it any time regardless of the plan. However, the main agent
    itself can call it only once per turn by default (an exception for checking directly under the
    target root). From the second call on it returns an error, so delegate further investigation to
    `dispatch_agent`.

    Args:
        pattern: A glob pattern (e.g. "*.py" for all Python files directly under path).
            Writing `**` still only searches directly under path.
            Shell-like brace expansion `{a,b,c}` is supported
            (e.g. "*.{jpg,jpeg,png,gif,bmp,webp}" for all image files).
            Writing either the upper- or lower-case extension matches both
            (path matching on Windows is case-insensitive).
        path: The absolute path of the base directory (`@N` allowed). Defaults to the working
            directory.
        head_limit: The maximum number of entries, applied separately to files and directories
            (default 200).

    Returns:
        A JSON string with `{"base", "base_contents", "total_matches", "returned", "truncated",
        "files", "file_details", "total_directories", "returned_directories",
        "directories_truncated", "directories", "path_memory"}`.
        `files`/`directories` are each truncated to `head_limit` entries, newest first
        (see `truncated`/`directories_truncated`).
        `file_details` is in the same order and count as `files`, with the binary check and the
        total number of lines (check it before deciding the `limit` of `Read`).
        `files`, `file_details[].path` and `directories[].path` are returned as `@N` references
        instead of absolute paths when they could be registered in path memory (see the
        `path_memory` table for the real paths). `@N` can be passed as is to the absolute path
        argument of other tools.
        If the base directory does not exist or is not a directory, it returns an "Error: ..."
        string instead of raising.
    """
    base, error = _resolve_file_tools_path(path)
    if error:
        return f"Error: {error}"
    dup_error = _check_file_tools_duplicate("Glob", f"Glob\x00{pattern}\x00{base}\x00{head_limit}")
    if dup_error:
        return dup_error
    try:
        result = glob_search(base, pattern, head_limit=head_limit, exclude_names=_foreign_tmp_dir_names())
    except ValueError as e:
        hint = suggest_from_path_memory(str(base)) if not base.exists() else ""
        return f"Error: {e}{hint}"
    path_memory = _register_path_memory([*result["files"], *[d["path"] for d in result["directories"]]])
    if path_memory:
        # フルパスの重複を `@N` へ畳んでから result に載せる（大量ファイル時に
        # 同じパスが3〜4回積まれてコンテキストを食い潰すのを防ぐ）。
        _dedupe_paths_with_path_memory(result, path_memory)
        result["path_memory"] = path_memory
    logger.info("Glob: pattern=%s base=%s", pattern, base)
    return json.dumps(result, ensure_ascii=False)
