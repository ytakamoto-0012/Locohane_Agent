"""Glob ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import fnmatch
import json
import logging
import os
import re

from ._duplicate_guard import _check_file_tools_duplicate
from ._file_tools_common import format_size, looks_binary, read_text_with_fallback, suggest_similar_dir
from ._file_tools_limits import Deadline, FileToolsLimits, current_limits
from ._path_memory_helpers import _dedupe_paths_with_path_memory, _register_path_memory, suggest_from_path_memory
from ._safe_path import _resolve_file_tools_path
from ._workdir import _search_excluded_tmp_dir_names

logger = logging.getLogger(__name__)


_NOT_COUNTED_TIME_LIMIT = "not counted: time limit reached"


def _count_contents(path: Path, exclude_names: frozenset[str] = frozenset(), deadline: Deadline | None = None) -> dict:
    """path直下(非再帰)に含まれるサブフォルダ数とファイル数を数える。

    深い階層まで舐めると時間がかかりすぎるため、意図的に直下のみを見る
    （配下全体の再帰カウントはしない）。サブエージェントへの分割委譲判断
    に使う概算値のため、権限エラー等の例外は無視して数えられた範囲だけ
    を返す。処理時間の上限（deadline）に達したら数えずに、その旨の文字列を返す。

    Args:
        exclude_names: カウントから除外するディレクトリ名の集合
            （basenameで一致判定。空なら従来通り無条件で全て数える）。
        deadline: Glob 1回分の処理時間の上限（省略時は無制限）。
    """
    if deadline is not None and deadline.expired():
        return {"directory_count": _NOT_COUNTED_TIME_LIMIT, "file_count": _NOT_COUNTED_TIME_LIMIT}
    dir_count = 0
    file_count = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if deadline is not None and deadline.expired():
                    return {"directory_count": _NOT_COUNTED_TIME_LIMIT, "file_count": _NOT_COUNTED_TIME_LIMIT}
                if entry.is_dir():
                    if exclude_names and entry.name in exclude_names:
                        continue
                    dir_count += 1
                elif entry.is_file():
                    file_count += 1
    except OSError:
        pass
    return {"directory_count": dir_count, "file_count": file_count}


def _file_detail(path: Path, size: int, full_read_max_bytes: int, deadline: Deadline | None = None) -> dict:
    """head-limit適用後の1ファイルについてバイナリ判定・サイズ・総行数を調べる。

    total_lines は read_file() の total_lines 算出（splitlines の長さ）と
    完全に揃え、モデルがそのまま limit の値を決められるようにする。
    数えなかったとき（大きすぎる・デコードできない・時間切れ）は、null だと
    「読めなかった」と誤解されるため、理由を文字列で入れる（バイナリは
    binary=true で分かるので null のまま）。
    """
    resolved = str(path)
    if deadline is not None and deadline.expired():
        return {"path": resolved, "binary": None, "size_bytes": size, "total_lines": _NOT_COUNTED_TIME_LIMIT}
    if looks_binary(path):
        return {"path": resolved, "binary": True, "size_bytes": size, "total_lines": None}
    if size > full_read_max_bytes:
        total_lines = (
            f"not counted: file too large ({format_size(size)} > {format_size(full_read_max_bytes)} limit); "
            "Read with offset/limit still works"
        )
        return {"path": resolved, "binary": False, "size_bytes": size, "total_lines": total_lines}
    try:
        text = read_text_with_fallback(path)
    except (OSError, UnicodeDecodeError):
        return {"path": resolved, "binary": False, "size_bytes": size, "total_lines": "not counted: could not decode the text"}
    return {"path": resolved, "binary": False, "size_bytes": size, "total_lines": len(text.splitlines())}


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


_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _direct_child_pattern(pattern: str) -> tuple[str, bool, bool]:
    """Glob の pattern を、直下のエントリ名と照合する名前パターンへ直す。

    Glob は直下しか探さないため、LLM が書きがちな先頭の `**/` は取り除く
    （`**/*.jpg` は `*.jpg` と同じ）。取り除いた後も `/` が残る（フォルダの
    階層を含む）パターンは直下だけでは意味が変わるため、走査せずにエラーにする。

    Returns:
        (名前パターン, フォルダだけに限るか, `**` を取り除いたか)。

    Raises:
        ValueError: 空・絶対パス・フォルダの階層を含むパターンの場合。
    """
    p = pattern.strip().replace("\\", "/")
    if not p:
        raise ValueError("Invalid pattern: the pattern is empty")
    if p.startswith("/") or _DRIVE_PREFIX_RE.match(p):
        raise ValueError(f"Invalid pattern: an absolute path cannot be used as the pattern: {pattern}{ABSOLUTE_PATTERN_HINT}")
    stripped_recursive = False
    while True:
        if p.startswith("**/"):
            p, stripped_recursive = p[3:], True
        elif p.startswith("./"):
            p = p[2:]
        else:
            break
    dirs_only = p.endswith("/")
    p = p.rstrip("/")
    if p in ("", "**"):
        # pathlib の "**" は（再帰的に）フォルダだけを返す。直下のフォルダだけにする。
        p, dirs_only, stripped_recursive = "*", True, stripped_recursive or bool(p)
    if "/" in p:
        raise ValueError(
            f"Invalid pattern: Glob only searches directly under path, so the pattern cannot contain folder levels ({pattern}). "
            "Pass the subfolder itself as path and write only the name part in pattern (e.g. pattern=\"*.jpg\")."
        )
    if "**" in p:
        stripped_recursive = True
        p = re.sub(r"\*{2,}", "*", p)
    return p, dirs_only, stripped_recursive


def glob_search(
    base: Path,
    pattern: str,
    head_limit: int = 200,
    exclude_names: frozenset[str] = frozenset(),
    limits: FileToolsLimits | None = None,
) -> dict:
    """指定ディレクトリ直下でglobパターンに一致するファイル/ディレクトリを検索する。

    深い階層まで舐めると時間がかかりすぎるため、`base` の直下しか探索しない
    （ハードコードの制限で、引数での変更は不可）。pathlib の glob は `**` を含む
    パターンで配下全体を走査してしまう（結果を直下に絞っても走査時間は減らない）
    ため使わず、os.scandir で直下を1回だけ読み、各エントリ名をパターンと照合する。
    `pattern` 先頭の `**/` は取り除き、フォルダの階層を含むパターンはエラーにする
    （_direct_child_pattern 参照）。

    直下の一覧・ファイルの行数・フォルダの中身の件数を数える処理全体に、
    limits.search_time_limit_seconds の時間上限がかかる。

    Args:
        base: 検索起点ディレクトリの絶対パス。
        pattern: globパターン（例: "*.py"）。
        head_limit: ファイル・ディレクトリそれぞれに独立に適用する上限件数。
        exclude_names: 結果・件数から除外するディレクトリ名の集合
            （basenameで一致するエントリを除外。呼び出し元が「他セッションの
            一時ディレクトリ」等、汎用的な理由で除外したい名前を渡す。空なら
            従来通り無条件で全て対象）。
        limits: 処理上限（省略時は既定値）。

    Returns:
        {"base", "base_contents", "total_matches", "returned", "truncated",
         "files", "file_details", "total_directories", "returned_directories",
         "directories_truncated", "directories"}。`**` を取り除いたときは
        "pattern_note"、時間上限で一覧を打ち切ったときは "scan_stopped" が付く。

    Raises:
        ValueError: 起点ディレクトリが存在しない・ディレクトリでない・
            パターンが不正な場合。
    """
    if not base.exists():
        hint = suggest_similar_dir(base)
        raise ValueError(f"Search base directory not found: {base}{hint}")
    if not base.is_dir():
        raise ValueError(f"Search base path is not a directory: {base}")

    limits = limits or FileToolsLimits()
    deadline = Deadline(limits.search_time_limit_seconds)
    name_patterns: list[str] = []
    dirs_only = True
    stripped_recursive = False
    for expanded_pattern in _expand_braces(pattern):
        name_pattern, only_dirs, stripped = _direct_child_pattern(expanded_pattern)
        if name_pattern not in name_patterns:
            name_patterns.append(name_pattern)
        dirs_only = dirs_only and only_dirs
        stripped_recursive = stripped_recursive or stripped

    resolved_base = base.resolve()
    # (パス, 更新日時, サイズ)
    file_matches: list[tuple[Path, float, int]] = []
    dir_matches: list[tuple[Path, float]] = []
    scan_stopped = False
    try:
        scandir_it = os.scandir(resolved_base)
    except OSError as e:
        raise ValueError(f"Cannot list the folder: {e}") from e
    with scandir_it as it:
        for entry in it:
            if deadline.expired():
                scan_stopped = True
                break
            if exclude_names and entry.name in exclude_names:
                continue
            if not any(fnmatch.fnmatch(entry.name, p) for p in name_patterns):
                continue
            try:
                is_dir = entry.is_dir()
                is_file = not is_dir and entry.is_file()
                st = entry.stat() if (is_dir or is_file) else None
            except OSError:
                continue
            if st is None:
                continue
            if is_dir:
                dir_matches.append((Path(entry.path), st.st_mtime))
            elif not dirs_only:
                file_matches.append((Path(entry.path), st.st_mtime, st.st_size))

    file_matches.sort(key=lambda m: m[1], reverse=True)
    dir_matches.sort(key=lambda m: m[1], reverse=True)

    truncated_files = file_matches[:head_limit]
    truncated_dirs = [p for p, _ in dir_matches[:head_limit]]

    files = [str(p) for p, _, _ in truncated_files]
    file_details = [_file_detail(p, size, limits.full_read_max_bytes, deadline) for p, _, size in truncated_files]
    directories = [{"path": str(p), **_count_contents(p, exclude_names, deadline)} for p in truncated_dirs]
    base_contents = _count_contents(resolved_base, exclude_names, deadline)

    result = {
        "base": str(resolved_base),
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
    if stripped_recursive:
        result["pattern_note"] = (
            "`**` was ignored: Glob only searches directly under path. "
            "To look deeper, call it again with a subfolder from `directories` as path."
        )
    if scan_stopped:
        result["scan_stopped"] = (
            f"The time limit ({limits.search_time_limit_seconds:g} s) was reached while listing the folder, "
            "so the counts and lists above cover only part of it. Narrow pattern and search again."
        )
    return result


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
        pattern: A glob pattern for the names directly under path (e.g. "*.py" for all Python
            files directly under path). A leading `**/` is ignored (it still only searches
            directly under path). A pattern with folder levels such as "sub/*.py" is an error:
            pass the subfolder as path instead.
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
        `file_details` is in the same order and count as `files`, with the binary check, the size
        and the total number of lines (check it before deciding the `limit` of `Read`). When the
        lines were not counted (e.g. the file is very large), `total_lines` is a text saying why.
        If `scan_stopped` is present, the time limit was reached and the result is partial.
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
        result = glob_search(
            base, pattern, head_limit=head_limit, exclude_names=_search_excluded_tmp_dir_names(base), limits=current_limits()
        )
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
