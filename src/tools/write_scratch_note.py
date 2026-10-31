"""write_scratch_note ツール。"""

from __future__ import annotations

import re
from langchain_core.tools import tool
from pathlib import Path

from ._state import _SUBAGENT_RUN_ID
from .thread_notes import NOTE_FILE_LOCK
from ._workdir import _resolve_exec_workdir

_UNSAFE_RUN_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_run_id(raw: str) -> str:
    """run_id をファイル名の一部として安全な文字列に正規化する。

    dispatch_agent は run_id として LLM/フレームワークが生成する
    tool_call_id をそのまま使う（孤立tool_call検出時にtc["id"]から同じ
    run_idを再計算し、退避先ファイルの絶対パスを案内文に埋め込むため）。
    実測値（llama.cpp経由）は英数字のみだが、生成元のサーバー実装に
    フォーマット保証は無く、将来パス区切り文字等を含む値が来ても
    `_scratch_notes_path_for_run` がファイル名として安全に使えるように
    英数字・アンダースコア・ハイフン以外を "_" に置換する。
    """
    cleaned = _UNSAFE_RUN_ID_CHARS.sub("_", raw)
    return cleaned or "unknown"


def _scratch_notes_path_for_run(run_id: str) -> Path:
    """write_scratch_note が書き込むスクラッチファイルの絶対パスを、
    指定した run_id から直接決める（_SUBAGENT_RUN_ID を読まない版）。

    dispatch_agent のジョブを起動したツール呼び出しとは別の
    非同期コンテキスト（check_dispatch_agent_job / stop_dispatch_agent_job）
    から、同じジョブのスクラッチノートを参照するために使う。呼び出し元は
    事前にジョブの thread_id が現在のセッションと一致することを確認して
    いる前提（_resolve_exec_workdir() は cl.user_session から thread_id を
    読むため、一致していなければ別セッションのディレクトリを指してしまう）。
    """
    workdir = _resolve_exec_workdir()
    return workdir / f"_scratch_notes_{run_id}.md"


def _scratch_notes_path() -> Path:
    """write_scratch_note が書き込むスクラッチファイルの絶対パスを決める。

    _resolve_exec_workdir() と同じ `_tmp_<thread_id>` 配下に、現在の
    dispatch_agent 実行（_SUBAGENT_RUN_ID）ごとの専用ファイルを1つ割り当てる。
    サブエージェント外（run_id が無い状態）で呼ばれた場合は "_main" を使う。
    ファイル名はこの関数が決め打ちするため、呼び出し側が任意パスを
    指定することはできない。
    """
    return _scratch_notes_path_for_run(_SUBAGENT_RUN_ID.get() or "_main")


@tool
def write_scratch_note(content: str) -> str:
    """Append what you found during an investigation to a scratch file on the spot.

    Use it for investigation tasks that read through many files and compile them into one output
    (a summary, extracted data, etc.). If you write down what you found with this tool every so
    often, the content so far is not lost even if this sub-agent itself reaches the token limit and
    is stopped (on stopping, the delegator is told the path of this file and can decide how to
    continue from there).

    Unlike execute_python_code/run_script, it can always be called even if the plan
    (create_plan/approve_plan) is not approved (investigation normally happens before create_plan).
    This tool decides the file name itself, so it cannot write to arbitrary paths, and it never
    touches the user's working directory or output destinations (it uses the same scratch area as
    the intermediate artifacts of execute_python_code).

    Args:
        content: The content to append (free form, such as Markdown or a JSON string).
            Earlier content is not erased; it is appended at the end.

    Returns:
        The absolute path written to and the total characters after appending. If content is empty
        or writing fails, it returns an "Error: ..." string instead of raising.
    """
    if not content.strip():
        return "Error: content is empty."
    path = _scratch_notes_path()
    try:
        with NOTE_FILE_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(content)
                if not content.endswith("\n"):
                    f.write("\n")
            total_chars = len(path.read_text(encoding="utf-8"))
    except OSError as e:
        return f"Error: failed to write to the scratch file: {e}"
    return f"Written: {path} ({total_chars} chars in total)"
