"""大量のファイルを読む前に、時間がかかることを伝えてユーザーへ確認するガード。

作業ディレクトリに年別フォルダの写真が数十〜数百枚あるようなタスクでは、メイン
エージェントは全ファイルを読むサブエージェントを並べて委譲し、それだけで数十分〜
数時間かかる（本番実例: 読み取りだけで80分）。サブエージェントは
[subagent].max_parallel（既定1）の本数ずつしか動かないため、時間はファイル数に
ほぼ比例する。時間がかかると分かってから聞いても、それは作業が止まるだけで
意味がない。そこで、何も読む前に、コードが「全部読む／フォルダごとに代表だけ読む／
中止」を1回だけ確認する。LLM の判断には任せない（プロンプトの「代表サンプルで
足りる」という一文は守られなかった）。

聞くタイミングは2つ（どちらもセッションにつき1回だけ。答えは使い回す）:
1. 構造調査の直後（主経路）: 最初の explore が Glob で調べたフォルダの下に読み取り対象の
   ファイルが閾値を超えてあれば、その explore の結果をメインへ返す前に聞き、答えを結果の
   末尾に付ける。ファイル数が分かるのはこの時点で、メインはまだ何も読ませていない。
   本番ではメインが構造調査のあと自分の質問（週の定義など）や計画を挟み、読み取りの委譲は
   その後になったため、委譲の直前では遅かった。Glob を使わない explore（1ファイルだけ読む等）では聞かない。
2. 読み取りの委譲の直前（保険）: 構造調査を経ずに大量読みの委譲が来たとき。
   - 同じ AIMessage に explore/worker の dispatch_agent が2本以上並んでいる、または
   - dispatch_agent_batch の対象（files 指定ならその件数、それ以外は作業ディレクトリの件数）が閾値を超える。
   並列の委譲（同じ AIMessage の兄弟）が同時に来ても質問は1回で、「代表だけ」「中止」
   「自由記述」の答えならその AIMessage の委譲はすべて実行せず、答えに沿って委譲し直すよう伝える。
応答がタイムアウト（[read_volume_guard] ask_timeout_seconds）したら全部読んで続ける。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import chainlit as cl
from langchain_core.messages import AIMessage, ToolMessage

from . import _state
from ._ask_echo import ASK_TIMEOUT_MESSAGE
from ..i18n import t

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff", ".webp"})
DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".doc", ".xlsx", ".xlsm", ".xls", ".pptx", ".ppt"})
TEXT_EXTENSIONS = frozenset({".md", ".txt", ".csv", ".json"})

_READ_AGENT_TYPES = frozenset({"explore", "worker"})
_MAX_SCAN_ENTRIES = 20000
_DECISION_KEY = "read_volume_decision"
_LOCK_KEY = "read_volume_lock"


def count_source_files(root: Path, max_entries: int = _MAX_SCAN_ENTRIES) -> dict:
    """root の下の読み取り対象ファイルを種類別に数える。

    セッションの一時フォルダ（_tmp_…）・隠しフォルダ・Office の一時ファイル（~$…）は
    数えない。ただし root 自体が _tmp_… でも数える（eval は一時フォルダが作業
    ディレクトリになる）。巨大なフォルダで止まらないよう、max_entries 件見たら打ち切る。

    Returns:
        total/images/documents/text/folders（対象ファイルがあるフォルダ数）/truncated。
    """
    counts = {"images": 0, "documents": 0, "text": 0}
    folders: set[str] = set()
    seen = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(("_tmp_", "."))]
        for name in filenames:
            seen += 1
            if seen > max_entries:
                truncated = True
                break
            if name.startswith("~$"):
                continue
            ext = os.path.splitext(name)[1].lower()
            kind = (
                "images"
                if ext in IMAGE_EXTENSIONS
                else "documents"
                if ext in DOCUMENT_EXTENSIONS
                else "text"
                if ext in TEXT_EXTENSIONS
                else None
            )
            if kind:
                counts[kind] += 1
                folders.add(dirpath)
        if truncated:
            break
    total = sum(counts.values())
    return {**counts, "total": total, "folders": len(folders), "truncated": truncated}


def _setting(name: str, default):
    return getattr(_state._LLM_CONFIG, name, default)


def _last_ai_message(state) -> AIMessage | None:
    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    for message in reversed(messages or []):
        if isinstance(message, AIMessage):
            return message
    return None


def _ai_key(ai: AIMessage | None) -> str | None:
    """同じ応答（AIMessage）の委譲を見分けるキー。id が無ければツール呼び出しの id の並びで代える。

    None（見分けられない）は、どの答えとも一致しないものとして扱う。id が None 同士で一致させると、
    構造調査の答え（ai_id=None）が「代表だけ」「中止」のとき、以後の委譲がすべて止まっていた。
    """
    if ai is None:
        return None
    if ai.id:
        return ai.id
    call_ids = [c.get("id") for c in ai.tool_calls if c.get("id")]
    return "calls:" + ",".join(call_ids) if call_ids else None


def _read_scope(call: dict, ai: AIMessage | None) -> int | str | None:
    """この委譲が大量読みになりうるか（作業ディレクトリを数える前に、呼び出しだけで決める）。

    Returns:
        None: 大量読みではない（数えない）。"inventory": 作業ディレクトリの件数で決める。
        int: 読むファイル数が呼び出しから分かる（dispatch_agent_batch の files）。
    """
    name = call.get("name")
    args = call.get("args") or {}
    if name == "dispatch_agent_batch":
        files = args.get("files")
        return len(files) if files else "inventory"
    if name != "dispatch_agent" or args.get("agent_type") not in _READ_AGENT_TYPES:
        return None
    siblings = [
        c
        for c in (ai.tool_calls if ai is not None else [])
        if c.get("name") == "dispatch_agent" and (c.get("args") or {}).get("agent_type") in _READ_AGENT_TYPES
    ]
    return "inventory" if len(siblings) >= 2 else None


def _files_to_read(call: dict, ai: AIMessage | None, inventory_total: int) -> int:
    """この委譲（と同じ応答の兄弟）が大量読みなら読むファイル数を、そうでなければ0を返す。"""
    scope = _read_scope(call, ai)
    if scope is None:
        return 0
    return inventory_total if scope == "inventory" else scope


def _total_text(total: int, truncated: bool) -> str:
    """質問に出す件数。走査を打ち切ったときは実際はそれ以上あるので「N+」にする。"""
    return f"{total}+" if truncated else str(total)


def _decision_note(decision: dict) -> str:
    """ユーザーの答えをメインエージェントへの指示文にする。"""
    kind, total, per_folder = decision["kind"], decision["total"], decision["per_folder"]
    if kind == "all":
        return f"the user chose to read all {total} files."
    if kind == "sample":
        return (
            f"the user chose to read only about {per_folder} representative files per folder instead of all {total}. "
            f"Delegate so that each sub-agent opens at most {per_folder} files per folder, picked to show the spread "
            "(e.g. different months or kinds), and state in your final answer that the result is based on these samples."
        )
    if kind == "stop":
        return (
            f"the user chose not to read the {total} files. Do not delegate reading them; tell the user what you can "
            "do without reading them (or what you need from them) and end your turn."
        )
    return f"asked how to read the {total} files, the user answered: {decision['answer']!r}. Follow that answer."


def _blocked_message(decision: dict) -> str:
    return "Not run: before any file was read, " + _decision_note(decision) + " Then delegate again."


def _survey_note(decision: dict) -> str:
    return (
        f"\n\n[Reading volume] The folders surveyed above hold {decision['total']} files to read "
        "(reading them all takes a long time). The code asked the user before any file "
        "was read: " + _decision_note(decision) + " Do not ask the user about the reading volume again."
    )


async def _ask(total: int, inventory: dict, per_folder: int, *, truncated: bool = False) -> dict:
    """ユーザーに確認し、{"kind": all|sample|stop|other, "answer": 文字列} を返す。

    所要時間の見積もりは出さない（読む中身で大きく変わり、数字はいつも短すぎた）。
    待ち時間は [read_volume_guard] ask_timeout_seconds（全体の ask_user_choice_seconds が
    無期限でも、この質問は時間切れで「全部読む」に進む）。
    """
    import importlib  # noqa: PLC0415

    # src.tools は同名のツールを公開しているため、モジュールは import_module で引く
    # （ツール登録順の循環を避けるため関数内で読む）。
    ask_module = importlib.import_module(f"{__package__}.ask_user_choice")

    breakdown = t("list.separator").join(
        t(f"read_volume.breakdown_{k}", n=inventory[k]) for k in ("images", "documents", "text") if inventory[k]
    )
    choice_all = t("read_volume.choice_all")
    choice_sample = t("read_volume.choice_sample", per_folder=per_folder)
    choice_stop = t("read_volume.choice_stop")
    question = t(
        "read_volume.question", total=_total_text(total, truncated), breakdown=breakdown, folders=inventory["folders"]
    )
    answer = await ask_module.ask_choice(
        question,
        [choice_all, choice_sample, choice_stop],
        timeout_seconds=int(_setting("read_volume_guard_ask_timeout_seconds", 30)),
    )
    if answer in (choice_all, ASK_TIMEOUT_MESSAGE):
        kind = "all"
    elif answer == choice_sample:
        kind = "sample"
    elif answer == choice_stop or answer.startswith("Error:"):
        kind = "stop"
    else:
        kind = "other"
    logger.info("read volume guard: total=%d answer=%r -> %s", total, answer, kind)
    return {"kind": kind, "answer": answer}


def _session_lock() -> asyncio.Lock:
    lock = cl.user_session.get(_LOCK_KEY)
    if lock is None:
        lock = asyncio.Lock()
        cl.user_session.set(_LOCK_KEY, lock)
    return lock


async def guard_read_volume(input) -> dict | None:  # noqa: A002
    """大量読みの委譲なら、実行前にユーザーへ確認する（ImageAwareToolNode.ainvoke から呼ぶ）。

    Returns:
        実行させない場合は ToolNode と同じ形の {"messages": [ToolMessage]}、通すなら None。
    """
    if not _setting("read_volume_guard_enabled", False):
        return None
    if not (isinstance(input, dict) and input.get("__type") == "tool_call_with_context"):
        return None
    call = input.get("tool_call") or {}
    if call.get("name") not in ("dispatch_agent", "dispatch_agent_batch"):
        return None
    ai = _last_ai_message(input.get("state") or {})
    ai_id = _ai_key(ai)
    decision = cl.user_session.get(_DECISION_KEY)
    if decision is not None and (ai_id is None or decision.get("ai_id") != ai_id):
        return None  # このセッションでは確認済み。後の委譲は通す。
    scope = _read_scope(call, ai)
    if decision is None and scope is None:
        return None  # 大量読みになりえない委譲（単発の verifier 等）。作業ディレクトリを数えない。
    asked_here = False
    async with _session_lock():
        decision = cl.user_session.get(_DECISION_KEY)
        if decision is None:
            from ._workdir import _resolve_workdir  # noqa: PLC0415

            try:
                root = _resolve_workdir()
            except RuntimeError:
                return None
            inventory = await asyncio.to_thread(count_source_files, root)
            threshold = int(_setting("read_volume_guard_threshold_files", 30))
            total = _files_to_read(call, ai, inventory["total"])
            if total <= threshold:
                return None  # 大量読みではない（この応答ではまだ聞かない）。
            per_folder = int(_setting("read_volume_guard_sample_files_per_folder", 2))
            truncated = scope == "inventory" and inventory["truncated"]
            decision = {**await _ask(total, inventory, per_folder, truncated=truncated), "ai_id": ai_id, "total": total}
            decision["per_folder"] = per_folder
            cl.user_session.set(_DECISION_KEY, decision)
            asked_here = True
    if decision["kind"] == "all":
        return None
    # 質問した当の呼び出しは答えどおりに止める。兄弟（同じ応答の委譲）は応答のキーが一致したときだけ止める。
    if not asked_here and (ai_id is None or decision.get("ai_id") != ai_id):
        return None
    content = _blocked_message(decision)
    return {
        "messages": [
            ToolMessage(content=content, name=call.get("name"), tool_call_id=call.get("id"), status="error")
        ]
    }


# --- 構造調査の直後に聞く（主経路） ---

_GLOB_BASE_RE = re.compile(r'"base":\s*"((?:[^"\\]|\\.)*)"')


class SurveyRecorder:
    """1本の explore が Glob で調べたフォルダ（Glob 結果の base）を集める観測者。

    src.subagent.observe_tool_calls で、この委譲の呼び出しの中で始まったサブエージェントだけに
    登録する（task 文の一致では見分けない。dispatch_agent が `@N` の解決などで task 文を書き換えると
    外れ、空の task 文では他のセッションの委譲まで拾っていた）。
    """

    def __init__(self, task: str = "") -> None:
        self.task = task
        self.bases: set[str] = set()

    def __call__(self, record: dict) -> None:
        if record.get("name") != "Glob" or record.get("is_error"):
            return
        match = _GLOB_BASE_RE.search(record.get("result") or "")
        if match:
            try:
                self.bases.add(json.loads(f'"{match.group(1)}"'))
            except json.JSONDecodeError:
                self.bases.add(match.group(1))


def surveyed_inventory(bases) -> dict:
    """bases（入れ子は親だけ残す）の下の読み取り対象ファイルを合計する。"""
    roots: list[Path] = []
    for base in sorted({Path(b) for b in bases}, key=lambda p: len(p.parts)):
        if base.is_dir() and not any(base == r or r in base.parents for r in roots):
            roots.append(base)
    total = {"images": 0, "documents": 0, "text": 0, "total": 0, "folders": 0, "truncated": False}
    for root in roots:
        inv = count_source_files(root)
        for key in ("images", "documents", "text", "total", "folders"):
            total[key] += inv[key]
        total["truncated"] = total["truncated"] or inv["truncated"]
    return total


def survey_recorder(input) -> SurveyRecorder | None:  # noqa: A002
    """まだ確認しておらず、これが explore の dispatch_agent なら観測者を返す（それ以外は None）。"""
    if not _setting("read_volume_guard_enabled", False):
        return None
    if not (isinstance(input, dict) and input.get("__type") == "tool_call_with_context"):
        return None
    call = input.get("tool_call") or {}
    args = call.get("args") or {}
    if call.get("name") != "dispatch_agent" or args.get("agent_type") != "explore":
        return None
    if cl.user_session.get(_DECISION_KEY) is not None:
        return None
    return SurveyRecorder(str(args.get("task") or ""))  # 観測の範囲は observe_tool_calls で絞る


async def ask_after_survey(recorder: SurveyRecorder, result):
    """explore が調べたフォルダのファイルが閾値を超えていれば、結果を返す前に聞き、答えを結果に付ける。"""
    if not recorder.bases:
        return result
    async with _session_lock():
        if cl.user_session.get(_DECISION_KEY) is not None:
            return result
        inventory = await asyncio.to_thread(surveyed_inventory, recorder.bases)
        if inventory["total"] <= int(_setting("read_volume_guard_threshold_files", 30)):
            return result
        per_folder = int(_setting("read_volume_guard_sample_files_per_folder", 2))
        decision = {**await _ask(inventory["total"], inventory, per_folder, truncated=inventory["truncated"]), "ai_id": None}
        decision.update(total=inventory["total"], per_folder=per_folder)
        cl.user_session.set(_DECISION_KEY, decision)
    note = _survey_note(decision)
    messages = result.get("messages") if isinstance(result, dict) else result
    for message in messages or []:
        if isinstance(message, ToolMessage) and isinstance(message.content, str):
            message.content += note
            break
    return result
