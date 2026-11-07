"""Write / Edit ツール（テキストファイルの作成・部分置換）。

背景（2026-11-04 evals 001）: worker がファイルを作る手段がコード実行
（execute_python_code）しかなく、xlsx を作るスクリプトの1か所を直すたびに
約1万文字のスクリプト全体を出し直していた（1回60〜115秒、作成と修正で9回）。
スクリプトをファイルに書いておき、直すときは該当箇所だけ置き換えれば、
出力量（＝時間とコンテキスト）が修正箇所の分だけで済む。

書き込める場所は execute_python_code と同じ（作業ディレクトリと、このセッションの
`_tmp_<作成時刻>_<短縮ID>`）。計画承認が必要。サブエージェント（worker）専用で、
メインエージェントの組み込みツールには入れない（メインは書き込みを worker へ委譲する設計）。
"""

from __future__ import annotations

import difflib
import json
import locale
import logging
from pathlib import Path

import chainlit as cl
from langchain_core.tools import tool

from ._file_tools_common import looks_binary
from ._path_memory_helpers import _register_path_memory, _resolve_path_memory_token
from ._python_fs_guard import _exec_guard_roots
from ._workdir import _foreign_tmp_dir_error, _resolve_exec_workdir

logger = logging.getLogger(__name__)

# 置換後に返す前後の行数（読み直しを省くための確認用）
_CONTEXT_LINES = 3

_NOT_APPROVED = (
    "Error: cannot write because the plan is not approved. "
    "If your tools do not include create_plan/approve_plan (you are a sub-agent), do not try again; "
    "report this error as is to the delegator as your final answer."
)


def _resolve_write_path(file_path: str) -> tuple[Path | None, str | None]:
    """file_path（絶対パス・相対パス・`@N`）を解決し、書き込める場所かを確かめる。

    相対パスは execute_python_code の cwd（このセッションの `_tmp_`）基準にする。
    Write で置いたスクリプトを execute_python_code から相対パスのまま実行できるように。
    """
    if not file_path:
        return None, "Specify file_path."
    resolved, error = _resolve_path_memory_token(file_path)
    if error:
        return None, error
    path = Path(resolved)
    if not path.is_absolute():
        path = _resolve_exec_workdir() / path
    path = path.resolve()
    tmp_error = _foreign_tmp_dir_error(path)
    if tmp_error:
        return None, tmp_error
    _, display_roots = _exec_guard_roots()
    roots = [Path(r).resolve() for r in display_roots]
    if not any(path.is_relative_to(root) for root in roots):
        shown = ", ".join(str(r) for r in roots)
        return None, f"Writing is only allowed under the working directory or this session's temporary folder ({shown}): {path}"
    return path, None


def _read_keeping_format(path: Path) -> tuple[str, str, str]:
    """(本文（改行は \\n にそろえる）, 文字コード, 元の改行) を返す。書き戻しで形式を保つため。"""
    data = path.read_bytes()
    for encoding in dict.fromkeys(("utf-8", locale.getencoding(), "cp932")):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
        except LookupError:
            continue
    else:
        raise ValueError(f"Cannot decode the file as text: {path}")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n").replace("\r", "\n"), encoding, newline


def _write_text(path: Path, text: str, encoding: str, newline: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=encoding, newline="") as f:
        f.write(text.replace("\n", newline) if newline != "\n" else text)


def _numbered(lines: list[str], start: int, end: int) -> str:
    """lines[start:end] を「行番号\\t内容」で連結する（行番号は1始まり）。"""
    return "\n".join(f"{i + 1}\t{lines[i]}" for i in range(max(start, 0), min(end, len(lines))))


# 続きの行が違うときに見せるファイルの行数の上限
_MISMATCH_CONTEXT_LINES = 15


def _closest_hint(text: str, old_string: str) -> str:
    """old_string が見つからないとき、いちばん近い行を示す（空白や記号の写し間違いに気付かせる）。"""
    first = next((line for line in old_string.split("\n") if line.strip()), "")
    if not first:
        return ""
    lines = text.split("\n")
    best = difflib.get_close_matches(first, lines, n=1, cutoff=0.6)
    if not best:
        return ""
    start = lines.index(best[0])
    line_no = start + 1
    old_lines = old_string.split("\n")
    first_offset = old_lines.index(first)
    if best[0] == first and len(old_lines) - first_offset > 1:
        # 1行目は合っていて、続きの行の写し間違い（2026-11-07 evals xlkit: 失敗した Edit の多くが
        # これで、1行目だけを示すと同じ1行目から書き直してまた外していた）。違う行と、その位置の
        # ファイルの実際の行を見せる。
        for offset, old_line in enumerate(old_lines[first_offset:]):
            file_index = start + offset
            if file_index >= len(lines) or lines[file_index] != old_line:
                shown_end = min(start + len(old_lines) - first_offset + 2, start + _MISMATCH_CONTEXT_LINES, len(lines))
                return (
                    f" The first {offset} line(s) of old_string match the file from line {line_no}, but the next line "
                    f"differs: old_string has {old_line!r}. The file has there:\n{_numbered(lines, start, shown_end)}\n"
                    "Copy old_string exactly from these lines."
                )
    return f" The closest line is {line_no}: {best[0]!r}. Copy old_string exactly from the file (Read it if unsure)."


@tool("Write")
def write_tool(file_path: str, content: str, overwrite: bool = False) -> str:
    """Create a text file (a script, a data file, etc.) with the given content.

    Use it to put a long script in a file once (e.g. `build.py`) and run it with
    `execute_python_code` (`import runpy; runpy.run_path("build.py")`). To change part of it
    later, use `Edit` instead of writing the whole file again.

    A relative path is placed in this session's temporary folder, which is also the cwd of
    `execute_python_code`. Writing is allowed only under the working directory and that folder.
    Needs an approved plan. Do not use it for xlsx/docx/pptx (binary files); build those with
    their skills.

    Args:
        file_path: The path to write (absolute, relative, or a path memory reference `@N`).
        content: The whole text of the file (written as UTF-8).
        overwrite: Set true to replace a file that already exists. Without it, an existing
            file is not touched and an error is returned.

    Returns:
        A JSON string with `{"path", "lines", "path_memory"}`, or an "Error: ..." string.
    """
    if not cl.user_session.get("plan_approved"):
        return _NOT_APPROVED
    path, error = _resolve_write_path(file_path)
    if error:
        return f"Error: {error}"
    if path.is_dir():
        return f"Error: the path is a directory: {path}"
    if path.exists() and not overwrite:
        return f"Error: {path} already exists. To change part of it use Edit; to replace the whole file pass overwrite=true."
    if path.exists() and looks_binary(path):
        return f"Error: {path} is a binary file; Write only writes text files."
    try:
        _write_text(path, content, "utf-8", "\n")
    except OSError as e:
        return f"Error: could not write the file: {e}"
    logger.info("Write: %s (%d chars)", path, len(content))
    result: dict = {"path": str(path), "lines": content.count("\n") + (0 if content.endswith("\n") or not content else 1)}
    path_memory = _register_path_memory([str(path)])
    if path_memory:
        result["path_memory"] = path_memory
    return json.dumps(result, ensure_ascii=False)


@tool("Edit")
def edit_tool(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Replace a part of a text file: find `old_string` and put `new_string` in its place.

    Use it to fix a script or text file you wrote with `Write`, instead of writing the whole
    file again. `old_string` must match the file exactly (spaces and indentation included) and
    must appear only once; include a few surrounding lines to make it unique, or pass
    replace_all=true to replace every occurrence. The result shows the changed lines with line
    numbers, so you do not need to Read the file again.

    The file keeps its encoding and line endings. Writing is allowed only under the working
    directory and this session's temporary folder (a relative path is in that folder). Needs an
    approved plan.

    Args:
        file_path: The file to edit (absolute, relative, or a path memory reference `@N`).
        old_string: The exact text to replace.
        new_string: The text to put in its place (must differ from old_string).
        replace_all: Replace every occurrence instead of requiring exactly one.

    Returns:
        A JSON string with `{"path", "replacements", "changed"}` (`changed` is the edited
        region with line numbers), or an "Error: ..." string.
    """
    if not cl.user_session.get("plan_approved"):
        return _NOT_APPROVED
    path, error = _resolve_write_path(file_path)
    if error:
        return f"Error: {error}"
    if not path.is_file():
        return f"Error: file not found: {path}"
    if looks_binary(path):
        return f"Error: {path} is a binary file; Edit only edits text files."
    if not old_string:
        return "Error: old_string is empty. To create a file use Write."
    if old_string == new_string:
        return "Error: old_string and new_string are the same."
    try:
        text, encoding, newline = _read_keeping_format(path)
    except (OSError, ValueError) as e:
        return f"Error: {e}"
    old = old_string.replace("\r\n", "\n")
    new = new_string.replace("\r\n", "\n")
    count = text.count(old)
    if count == 0:
        return f"Error: old_string was not found in {path}.{_closest_hint(text, old)}"
    if count > 1 and not replace_all:
        return (
            f"Error: old_string appears {count} times in {path}. Include more surrounding lines to make it unique, "
            "or pass replace_all=true to replace all of them."
        )
    first_index = text.index(old)
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        _write_text(path, updated, encoding, newline)
    except OSError as e:
        return f"Error: could not write the file: {e}"
    logger.info("Edit: %s (%d replacement(s))", path, count if replace_all else 1)

    lines = updated.split("\n")
    start_line = text[:first_index].count("\n")
    end_line = start_line + new.count("\n") + 1
    result = {
        "path": str(path),
        "replacements": count if replace_all else 1,
        "changed": _numbered(lines, start_line - _CONTEXT_LINES, end_line + _CONTEXT_LINES),
    }
    return json.dumps(result, ensure_ascii=False)
