"""Grep ツール。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from langchain_core.tools import tool
from pathlib import Path
import fnmatch
import json
import logging
import os

import regex

from ._duplicate_guard import _check_file_tools_duplicate
from ._file_tools_common import format_size, is_link_dir, looks_binary, read_text_with_fallback, split_text_lines, suggest_similar_dir
from ._file_tools_limits import Deadline, FileToolsLimits, current_limits
from ._path_memory_helpers import _register_path_memory, suggest_from_path_memory
from ._safe_path import _resolve_file_tools_path
from .. import skill_drafts
from . import _state
from ._workdir import _search_excluded_tmp_dir_names

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


# 結果に載せる「飛ばしたファイル」の例の件数。
_SKIPPED_EXAMPLES = 5
# 1ファイル内で処理時間の上限を確かめる間隔（行数）。
_DEADLINE_CHECK_LINES = 2000


class _Walk:
    """Grep のフォルダ走査（os.walk の代わり）。

    os.walk は Python 3.11 ではジャンクションをたどり（循環すると延々と潜る）、
    除外判定のためサブフォルダごとに resolve() を呼んでいた。ここでは
    os.scandir で上から順に（os.walk と同じ順序で）たどり、シンボリック
    リンク・ジャンクションのフォルダには入らない。対象ファイルの一覧を先に
    全部作らず1件ずつ返し、処理時間の上限に達したら stopped を立てて止まる
    （glob に一致するファイルが無いフォルダが続いても止まれるように、
    走査側でも上限を見る）。
    """

    def __init__(
        self,
        base: Path,
        glob: str,
        exclude_names: frozenset[str],
        exclude_paths: frozenset[str],
        deadline: Deadline,
    ) -> None:
        self.base = base
        # Grep は配下全体を探すので、LLM が書きがちな先頭の `**/` は取り除く（`**/*.py` は `*.py` と同じ。
        # 取り除かないとファイル名と比べて何にも一致せず、「一致なし」と誤って結論させていた）。
        # `/` が残るパターン（`src/*.py` 等）は、base からの相対パスと比べる。
        normalized = glob.replace("\\", "/")
        while normalized.startswith("**/"):
            normalized = normalized[3:]
        self.glob = normalized
        self.glob_has_dir = "/" in normalized
        self.exclude_names = exclude_names
        self.exclude_paths = exclude_paths
        self.deadline = deadline
        self.stopped = False

    def __iter__(self) -> Iterator[tuple[Path, int]]:
        stack = [str(self.base)]
        while stack:
            current = stack.pop()
            try:
                scandir_it = os.scandir(current)
            except OSError:
                continue
            subdirs: list[str] = []
            with scandir_it as it:
                for entry in it:
                    if self.deadline.expired():
                        self.stopped = True
                        return
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        continue
                    if is_dir:
                        if is_link_dir(entry) or entry.name in self.exclude_names:
                            continue
                        if self.exclude_paths and os.path.normcase(entry.path) in self.exclude_paths:
                            continue
                        subdirs.append(entry.path)
                        continue
                    if self.glob:
                        if self.glob_has_dir:
                            target = os.path.relpath(entry.path, self.base).replace("\\", "/")
                        else:
                            target = entry.name
                        if not fnmatch.fnmatch(target, self.glob):
                            continue
                    try:
                        if not entry.is_file():
                            continue
                        size = entry.stat().st_size
                    except OSError:
                        continue
                    yield Path(entry.path), size
            stack.extend(reversed(subdirs))


def _skipped_summary(paths: list[str], count: int, hint: str) -> dict:
    return {"count": count, "examples": paths, "hint": hint}


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
    limits: FileToolsLimits | None = None,
) -> dict:
    """指定ファイル/ディレクトリ配下のテキストから正規表現で検索する。

    10万件超のフォルダや巨大なファイルで止まらないよう、次の上限で打ち切る
    （limits、config.ini [file_tools_limits]）。打ち切ったときは、それまでの
    結果に理由を添えて返す（マッチ0件でも返すので「無い」と誤解されない）。

    - 処理時間（search_time_limit_seconds）・中身を調べるファイル数
      （grep_max_files）に達したら走査をやめ、"scan_stopped" を付ける。
    - grep_max_file_bytes を超えるファイルは読まずに飛ばし、
      "skipped_large_files" に件数と例を載せる。
    - 正規表現が1行に regex_timeout_seconds 以上かかったら（バックトラックの
      爆発）、そのファイルを飛ばして "skipped_regex_timeout_files" に載せる。
      標準の re には時間制限が無いため、timeout 引数のある regex パッケージを使う。
    - "content" モードでは、返す件数（head_limit）を超えた時点で走査をやめる。
      このとき合計件数は分からないため、total_matches はその旨の文字列になる。

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
        limits: 処理上限（省略時は既定値）。

    Returns:
        output_mode に応じた形状の辞書（マッチ0件は
        {"matched": False, "files": [], "matches": [], "counts": []}。打ち切り・
        飛ばしたファイルがあれば上記のキーが加わる）。

    Raises:
        ValueError: 正規表現が不正・output_modeが不正・対象パスが存在しない場合。
    """
    if output_mode not in _OUTPUT_MODES:
        raise ValueError(f"Invalid output_mode: {output_mode}")
    try:
        compiled = regex.compile(pattern, regex.IGNORECASE if case_insensitive else 0)
    except regex.error as e:
        raise ValueError(f"Invalid regular expression: {e}") from e

    if not base.exists():
        hint = suggest_similar_dir(base)
        raise ValueError(f"Search target path not found: {base}{hint}")

    limits = limits or FileToolsLimits()
    deadline = Deadline(limits.search_time_limit_seconds)
    resolved_base = base.resolve()
    walk: _Walk | None = None
    if resolved_base.is_file():
        target_files: Iterable[tuple[Path, int]] = [(resolved_base, resolved_base.stat().st_size)]
    else:
        walk = _Walk(
            resolved_base,
            glob,
            exclude_names,
            frozenset(os.path.normcase(str(p)) for p in exclude_paths),
            deadline,
        )
        target_files = walk

    files_out: list[str] = []
    counts_out: list[dict] = []
    matches_out: list[dict] = []
    stop_reason: str | None = None
    stopped_at_head_limit = False
    scanned = 0
    large_count = 0
    large_examples: list[str] = []
    slow_count = 0
    slow_examples: list[str] = []

    for file_path, size in target_files:
        if deadline.expired():
            stop_reason = "time"
            break
        if scanned >= limits.grep_max_files:
            stop_reason = "files"
            break
        # バイナリ（動画・ZIP・PDF 等）は大きさに関係なく検索対象外。大きいバイナリを「大きくて飛ばした」と
        # 数えると、LLM が中に一致が隠れていると考えて Read で開き、毎回バイナリのエラーで手番を無駄にする。
        if looks_binary(file_path):
            continue
        if size > limits.grep_max_file_bytes:
            large_count += 1
            if len(large_examples) < _SKIPPED_EXAMPLES:
                large_examples.append(str(file_path))
            continue
        scanned += 1
        try:
            text = read_text_with_fallback(file_path)
        except (OSError, UnicodeDecodeError):
            continue

        lines = split_text_lines(text)
        match_line_indexes: list[int] = []
        try:
            for i, line in enumerate(lines):
                if i % _DEADLINE_CHECK_LINES == 0 and i and deadline.expired():
                    raise TimeoutError
                timeout = min(limits.regex_timeout_seconds, max(deadline.remaining(), 0.01))
                if compiled.search(line, timeout=timeout):
                    match_line_indexes.append(i)
                    if output_mode == "content" and len(matches_out) + len(match_line_indexes) > head_limit:
                        break
        except TimeoutError:
            if deadline.expired():
                stop_reason = "time"
                break
            slow_count += 1
            if len(slow_examples) < _SKIPPED_EXAMPLES:
                slow_examples.append(str(file_path))
            continue
        if not match_line_indexes:
            continue

        path_str = str(file_path)
        files_out.append(path_str)
        counts_out.append({"path": path_str, "count": len(match_line_indexes)})

        if output_mode == "content":
            for idx in match_line_indexes:
                start = max(0, idx - context)
                end = min(len(lines), idx + context + 1)
                for i in range(start, end):
                    matches_out.append({"path": path_str, "line": i + 1, "text": lines[i]})
            if len(matches_out) > head_limit:
                stopped_at_head_limit = True
                break

        if len(files_out) >= head_limit and output_mode != "content":
            break
    if walk is not None and walk.stopped:
        stop_reason = "time"

    extras: dict = {}
    if stop_reason == "time":
        extras["scan_stopped"] = (
            f"The time limit ({limits.search_time_limit_seconds:g} s) was reached after searching {scanned} files, "
            "so the result covers only part of the target. Narrow path or glob and search again."
        )
    elif stop_reason == "files":
        extras["scan_stopped"] = (
            f"The limit of {limits.grep_max_files} files per search was reached, "
            "so the result covers only part of the target. Narrow path or glob and search again."
        )
    if large_count:
        extras["skipped_large_files"] = _skipped_summary(
            large_examples,
            large_count,
            f"Files larger than {format_size(limits.grep_max_file_bytes)} were not searched. "
            "Read them part by part with Read (offset/limit) if needed.",
        )
    if slow_count:
        extras["skipped_regex_timeout_files"] = _skipped_summary(
            slow_examples,
            slow_count,
            "The regular expression took too long on these files (e.g. nested repetition such as (a+)+ "
            "or .* repeated many times), so they were skipped. Simplify the pattern and search again.",
        )

    if not files_out:
        return {"matched": False, "files": [], "matches": [], "counts": [], **extras}

    if output_mode == "files_with_matches":
        truncated = files_out[:head_limit]
        return {
            "matched": True,
            "total_files": len(files_out),
            "returned": len(truncated),
            "files": truncated,
            **extras,
        }
    if output_mode == "count":
        truncated_counts = counts_out[:head_limit]
        return {
            "matched": True,
            "total_files": len(counts_out),
            "returned": len(truncated_counts),
            "counts": truncated_counts,
            **extras,
        }
    truncated_matches = matches_out[:head_limit]
    total_matches: int | str = len(matches_out)
    if stopped_at_head_limit:
        total_matches = f"more than {head_limit} (the search stopped once head_limit was exceeded)"
    return {
        "matched": True,
        "total_matches": total_matches,
        "returned": len(truncated_matches),
        "truncated": len(matches_out) > head_limit,
        "matches": truncated_matches,
        **extras,
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
            e.g. "*.py"). Only effective when searching a directory, which is searched with all
            its subfolders, so "*.py" and "**/*.py" are the same; a pattern with a folder
            ("sub/*.py") is matched against the path relative to `path`. A pre-filter that does
            not appear in the results.
        case_insensitive: If True, ignore case.
        context: How many lines before and after a match to include (default 0).
        head_limit: The maximum number of matches to return (default 50).

    Returns:
        A JSON string shaped like `{"matched", "total_matches", "returned", "truncated",
        "matches": [{"path", "line", "text"}, ...], "path_memory"}`.
        With no matches, `{"matched": false, "files": [], "matches": [], "counts": []}`.
        The search stops once more than `head_limit` matches are found; then `total_matches` is a
        text instead of a number. If `scan_stopped` is present, a time or file-count limit was
        reached and the result (including "no matches") covers only part of the target.
        `skipped_large_files` / `skipped_regex_timeout_files` list files that were not searched.
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
            exclude_names=_search_excluded_tmp_dir_names(base),
            exclude_paths=frozenset(skill_drafts.unreadable_dirs(_state._LLM_CONFIG, skill_drafts.current_draft_user())),
            limits=current_limits(),
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
