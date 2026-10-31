"""write_thread_note/list_thread_notes/read_thread_note ツール群。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from langchain_core.tools import tool
from pathlib import Path
import re
import threading

from ._script_job import _JOB_OUTPUT_TAIL_CHARS
from ._state import _IN_SUBAGENT, _SUBAGENT_AGENT_TYPE
from ._workdir import _resolve_exec_workdir


# ノートへの追記と読み直しを直列にするロック（write_scratch_note も使う）。
# 同じ応答で並列に呼ばれたツールは別スレッドで同時に追記するため、Windows では
# 追記どうしが同じ位置に書かれて片方が消えることがある。
NOTE_FILE_LOCK = threading.Lock()


def _thread_notes_path() -> Path:
    """write_thread_note/list_thread_notes/read_thread_note が使う、
    スレッド全体で共有されるノートファイルの絶対パスを決める。

    _scratch_notes_path() と異なり run_id ではなく thread_id のみで決まるため、
    同一スレッド内の全 dispatch_agent 実行（サブエージェントごとに変わる
    run_id）とメインエージェント自身が同じファイルを共有できる。
    """
    workdir = _resolve_exec_workdir()
    return workdir / "_thread_notes.md"


_THREAD_NOTE_BLOCK_RE = re.compile(r"^## (.+)\n<!-- (.+?) by (.+?) -->\n", re.MULTILINE)


def _thread_note_author() -> str:
    """現在の呼び出し元を表す短い文字列（メインエージェントなら"main"、
    サブエージェントなら agent_type）。write_thread_note が書き込むメタ情報、
    list_thread_notes の一覧表示に使う。
    """
    if _IN_SUBAGENT.get():
        return _SUBAGENT_AGENT_TYPE.get() or "subagent"
    return "main"


@dataclass
class _ThreadNoteBlock:
    topic: str
    timestamp: str
    author: str
    content: str


def _parse_thread_notes(text: str) -> list["_ThreadNoteBlock"]:
    """_thread_notes_path() の内容を `## topic` 単位のブロックへ分割する。

    write_thread_note は常に追記のみで既存ブロックを書き換えないため、
    同じ topic 名のブロックが複数存在しうる（list_thread_notes /
    read_thread_note 側で合算・連結して1つの topic として扱う）。
    """
    blocks: list[_ThreadNoteBlock] = []
    matches = list(_THREAD_NOTE_BLOCK_RE.finditer(text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append(
            _ThreadNoteBlock(
                topic=m.group(1).strip(),
                timestamp=m.group(2).strip(),
                author=m.group(3).strip(),
                content=text[start:end].rstrip("\n"),
            )
        )
    return blocks


@tool
def write_thread_note(topic: str, content: str) -> str:
    """Write details found in your investigation to a note shared by the whole thread.

    Unlike write_scratch_note, within the same thread this file can be read by the main agent and
    by any sub-agent (agent_type) with list_thread_notes / read_thread_note (it is not tied to a
    run_id). When you want to keep the final answer to the delegator to a summary, write the
    concrete facts behind it (values, counts, locations, etc.) here, and refer to the topic name in
    the final answer.

    Write condensed, extracted facts, not raw data copied as is
    (if the note itself grows large, it squeezes the context of whoever reads it later).
    Calling it several times with the same topic appends (existing content is not overwritten).

    Args:
        topic: The heading of this entry. Refer to it later with the same string from
            list_thread_notes / read_thread_note (different spellings are treated as different
            topics, so to append to an existing topic, check its exact spelling with
            list_thread_notes).
        content: The content to write (free form, such as Markdown).

    Returns:
        The absolute path written to, the total characters of this topic and of the whole file.
        If topic/content is empty or writing fails, it returns an "Error: ..." string.
    """
    topic_clean = topic.strip()
    if not topic_clean:
        return "Error: topic is empty."
    if not content.strip():
        return "Error: content is empty."
    path = _thread_notes_path()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    author = _thread_note_author()
    block = f"## {topic_clean}\n<!-- {timestamp} by {author} -->\n{content.rstrip()}\n\n"
    try:
        with NOTE_FILE_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(block)
            full_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Error: failed to write to the thread note: {e}"
    topic_chars = sum(b.content.__len__() for b in _parse_thread_notes(full_text) if b.topic == topic_clean)
    return (
        f'Written: topic="{topic_clean}" (this topic in total {topic_chars} chars, '
        f"whole file {len(full_text)} chars). Path: {path}"
    )


def _format_thread_notes_listing(path: Path) -> str | None:
    """thread note ファイルをトピック一覧のテキストへ整形する。

    list_thread_notes（ツール）と thread_note_status_text（圧縮再注入）の
    両方が同じ整形結果を共有するための single source of truth。

    Returns:
        トピックごとの「文字数・書き込み件数・最終更新日時と書き込み者」の
        一覧テキスト。ファイルが無い、または有効なトピックが無ければ None。
    """
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = _parse_thread_notes(text)
    if not blocks:
        return None
    grouped: dict[str, list[_ThreadNoteBlock]] = {}
    for b in blocks:
        grouped.setdefault(b.topic, []).append(b)
    lines = [f"Thread note topics ({len(grouped)}, whole file {len(text)} chars):"]
    for topic, items in grouped.items():
        total_chars = sum(len(b.content) for b in items)
        last = items[-1]
        lines.append(
            f'- "{topic}": {total_chars} chars ({len(items)} writes, last updated {last.timestamp} by {last.author})'
        )
    return "\n".join(lines)


@tool
def list_thread_notes() -> str:
    """Return the list of topics of the note shared by the whole thread (where write_thread_note writes),
    without their bodies.

    Always call this before reading a body, and check the size of the target topic. The note grows
    as the thread goes on, so reading a body directly with read_thread_note can squeeze the
    context. For large topics, consider delegating the analysis with dispatch_agent, as
    read_thread_note suggests.

    Returns:
        A list with, for each topic, its "characters, number of writes, last update time and writer
        (main or agent_type)". If there is no note yet, a text saying so.
    """
    path = _thread_notes_path()
    listing = _format_thread_notes_listing(path)
    if listing is not None:
        return listing
    if not path.is_file():
        return "There are no thread notes yet. You can write the first topic with write_thread_note."
    return "The thread note file exists, but there are no valid topics yet."


def thread_note_status_text() -> str:
    """現在の thread note のトピック一覧を、圧縮再注入（context_compaction.py）
    向けに機械的に整形する。

    current_plan_status_text と同じ理由（要約LLMは write_thread_note の
    tool_calls 引数・ToolMessage を見て要約に含めるかどうかを自分で判断
    しており、含め忘れると圧縮後にthread noteの存在自体が失われる）で、
    要約LLMの出力とは無関係に summary へ無条件で追記するために使う。

    thread note が無い、またはchainlitセッション文脈外（テスト・evals等で
    cl.user_session.get() が ChainlitContextException を送出する場合）は
    空文字列を返す。
    """
    try:
        path = _thread_notes_path()
        listing = _format_thread_notes_listing(path)
    except Exception:
        return ""
    return listing or ""


@tool
def read_thread_note(topic: str) -> str:
    """Read only the content of the specified topic from the note shared by the whole thread.

    Always check the size of the target topic with list_thread_notes before calling it.
    If the topic is large in total, this function returns only the beginning and the end and omits
    the middle (so this tool's own output does not squeeze the context).
    If you need to read and analyze all of it, follow the notice returned when truncated and
    delegate the analysis with dispatch_agent to worker or explore, specifying this file path and
    the topic name (a sub-agent's Read is not subject to the main agent's tool call guard, so it can
    Read this file directly and study the whole text).

    Args:
        topic: The exact topic name shown by list_thread_notes.

    Returns:
        The content of the topic (only the beginning and end plus a notice if large). If there is
        no note or the topic is not found, it returns an "Error: ..." string.
    """
    path = _thread_notes_path()
    if not path.is_file():
        return "Error: there are no thread notes yet."
    text = path.read_text(encoding="utf-8", errors="replace")
    topic_clean = topic.strip()
    blocks = [b for b in _parse_thread_notes(text) if b.topic == topic_clean]
    if not blocks:
        return f'Error: topic "{topic_clean}" not found. Check with list_thread_notes.'
    merged = "\n\n".join(f"[{b.timestamp} by {b.author}]\n{b.content}" for b in blocks)
    if len(merged) <= _JOB_OUTPUT_TAIL_CHARS:
        return merged
    half = _JOB_OUTPUT_TAIL_CHARS // 2
    head = merged[:half]
    tail = merged[-half:]
    return (
        f"{head}\n\n"
        f'...(omitted: topic "{topic_clean}" has {len(merged)} chars in total; only the beginning and end are shown)...\n\n'
        f"{tail}\n\n"
        f'[If you need to read and analyze all of it, delegate to worker or explore with dispatch_agent, '
        f'in the form "Read the \"{topic_clean}\" section of {path} and analyze/summarize it".]'
    )
