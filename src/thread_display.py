"""チャット画面へ送る会話履歴（Chainlit の Step）を、表示に要る分だけに絞る（表示専用。会話の文脈には影響しない）。

長時間の処理（数千万トークン級）をさせたスレッドは、ツール呼び出しの Step が数万件・本文が数百MBになる。
スレッドを開き直す（resume_thread）たびにそれを全部ブラウザへ送ると、読み込みに時間がかかり、画面も重くなる。
画面に描くのは [ui] max_display_messages / max_display_side_steps の件数だけなので、送る前に同じ件数へ絞り、
ツールの入出力の長い本文も [ui] max_step_content_chars で切る（frontend/src/utils/messageTree.ts の
pruneMessages が、実行中に増えていく分を同じ規則で間引く）。

残すもの:
- メインの会話（ユーザー発言・回答・システムメッセージ）の最後の max_messages 件と、最後のユーザー発言。
- サイドパネルの一番外側の Step（ツール呼び出し等）の最後の max_side_steps 件と、その中の Step の最後の
  max_side_steps 件（実行中のものは件数に関係なく残す）。
- 状態を伝えるメッセージ（トークン使用量・作業ディレクトリ・実行計画・定型文・表示件数上限・入力上限・中断）の
  種類ごとの最新の1件（画面は最新の1件しか使わない）。
- 残した Step の親（Chainlit の run 等）。親が無いと、画面側（@chainlit/react-client の addMessage）が
  子を捨ててしまうため。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

MESSAGE_TYPES = frozenset({"user_message", "assistant_message", "system_message"})
# Chainlit が on_message 等のハンドラ本体を包む合成 Step（画面では透過的に展開される）。
RUN_TYPE = "run"
_TRUNCATED_NOTE = "\n…(truncated for display: {0} more characters)"


@dataclass(frozen=True)
class DisplayLimits:
    """画面へ送る履歴の上限（0 は無制限）。

    Attributes:
        max_messages: メインの会話の件数（[ui] max_display_messages）。
        max_side_steps: サイドパネルの一番外側の Step の件数と、1つの Step の中の Step の件数（[ui] max_display_side_steps）。
        max_step_chars: ツール等の Step の入力・出力1つあたりの文字数（[ui] max_step_content_chars）。メインの会話は切らない。
        status_prefixes: 状態を伝えるメッセージの本文の先頭（app.py の *_PREFIX。種類ごとに最新の1件だけ残す）。
    """

    max_messages: int = 0
    max_side_steps: int = 0
    max_step_chars: int = 0
    status_prefixes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def trims(self) -> bool:
        return self.max_messages > 0 or self.max_side_steps > 0


def truncate_text(value: object, max_chars: int) -> object:
    """文字列が max_chars を超えていれば切り、切った文字数を書き添える（文字列以外・0 以下はそのまま）。"""
    if max_chars <= 0 or not isinstance(value, str) or len(value) <= max_chars:
        return value
    return value[:max_chars] + _TRUNCATED_NOTE.format(len(value) - max_chars)


def cap_step_content(step: dict, max_chars: int) -> dict:
    """メインの会話でない Step の input / output を max_chars で切った写しを返す（切る必要が無ければ step のまま）。"""
    if max_chars <= 0 or step.get("type") in MESSAGE_TYPES:
        return step
    changed = {k: truncate_text(step.get(k), max_chars) for k in ("input", "output") if k in step}
    if all(changed[k] is step.get(k) for k in changed):
        return step
    return {**step, **changed}


def _is_side(step: dict) -> bool:
    return step.get("type") not in MESSAGE_TYPES and step.get("type") != RUN_TYPE


def _is_running(step: dict) -> bool:
    """実行中の Step か（frontend/src/utils/messageTree.ts の isRunningStep と同じ規則）。

    止めた Step（metadata.stopped_reason）・裏で続く Step（metadata.background）は実行中に数えない
    （強制終了等で end の無い Step が溜まったスレッドで、上限に関係なく全部送らないため）。
    """
    metadata = step.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return (
        bool(step.get("start"))
        and not step.get("end")
        and not step.get("isError")
        and not metadata.get("stopped_reason")
        and not metadata.get("background")
    )


def _status_prefix(step: dict, prefixes: Iterable[str]) -> str | None:
    if step.get("type") != "assistant_message":
        return None
    output = step.get("output")
    if not isinstance(output, str):
        return None
    return next((p for p in prefixes if output.startswith(p)), None)


def select_steps(steps: list[dict], limits: DisplayLimits) -> list[dict]:
    """画面へ送る Step を選ぶ（順序は元のまま）。上限が無ければ文字数だけ切って全件返す。"""
    if not limits.trims:
        return [cap_step_content(s, limits.max_step_chars) for s in steps]
    by_id = {s.get("id"): s for s in steps}

    def parent_of(s: dict) -> dict | None:
        pid = s.get("parentId")
        return by_id.get(pid) if pid else None

    keep: set[object] = set()
    main: list[dict] = []
    latest_status: dict[str, dict] = {}
    top_side: list[dict] = []
    children_side: dict[object, list[dict]] = {}
    for s in steps:
        prefix = _status_prefix(s, limits.status_prefixes)
        if prefix is not None:
            latest_status[prefix] = s
            continue
        if s.get("type") in MESSAGE_TYPES:
            main.append(s)
            continue
        if not _is_side(s):
            continue
        # 一番近い「サイドパネルの Step」の祖先（run は透過）。無ければ一番外側。
        ancestor = parent_of(s)
        seen = 0
        while ancestor is not None and not _is_side(ancestor) and seen < len(steps):
            ancestor = parent_of(ancestor)
            seen += 1
        if ancestor is None:
            top_side.append(s)
        else:
            children_side.setdefault(ancestor.get("id"), []).append(s)

    keep.update(s.get("id") for s in (main[-limits.max_messages :] if limits.max_messages > 0 else main))
    # 最後のユーザー発言は件数に関係なく残す（画面は「送信済みか」「中断の後に発言があるか」「実行中の Step の鎖」を
    # 最後のユーザー発言から判断するため。1回の指示で回答が max_messages 件を超えても外さない）。
    last_user = next((s for s in reversed(main) if s.get("type") == "user_message"), None)
    if last_user is not None:
        keep.add(last_user.get("id"))
    keep.update(s.get("id") for s in latest_status.values())
    kept_side = top_side[-limits.max_side_steps :] if limits.max_side_steps > 0 else top_side
    keep.update(s.get("id") for s in top_side if _is_running(s))
    keep.update(s.get("id") for s in kept_side)
    # 残した Step の中の Step（入れ子）。各階層で最後の max_side_steps 件と実行中のものを残す。
    stack = [s.get("id") for s in steps if s.get("id") in keep and _is_side(s)]
    while stack:
        sid = stack.pop()
        kids = children_side.get(sid, [])
        chosen = kids[-limits.max_side_steps :] if limits.max_side_steps > 0 else kids
        for k in [*chosen, *(k for k in kids if _is_running(k))]:
            if k.get("id") not in keep:
                keep.add(k.get("id"))
                stack.append(k.get("id"))
    # 親の鎖（run・外側の Step）を残す。
    for s in steps:
        if s.get("id") not in keep:
            continue
        p = parent_of(s)
        guard = 0
        while p is not None and p.get("id") not in keep and guard < len(steps):
            keep.add(p.get("id"))
            p = parent_of(p)
            guard += 1
    return [cap_step_content(s, limits.max_step_chars) for s in steps if s.get("id") in keep]


def select_elements(elements: list[dict], kept_step_ids: set[object]) -> list[dict]:
    """残した Step に付いた要素（画像・ファイル等）だけを返す（どの Step にも付いていないもの・tasklist は残す）。"""
    return [
        e
        for e in elements
        if e.get("type") == "tasklist" or not e.get("forId") or e.get("forId") in kept_step_ids
    ]
