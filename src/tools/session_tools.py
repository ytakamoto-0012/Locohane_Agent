"""セッションごとに差し替えるツール（ユーザー別ドラフトスキルの read_skill 選択肢）。"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from .read_skill import session_read_skill


def _session_draft_names() -> list[str]:
    """このセッションで read_skill の選択肢へ足すドラフト名（app.py が設定）。

    cl.user_session はセッション文脈外で ChainlitContextException を送出するため、
    その場合は空（evals 等。ドラフトは見えない）。
    """
    try:
        import chainlit as cl  # noqa: PLC0415

        return list(cl.user_session.get("visible_draft_names") or [])
    except Exception:  # noqa: BLE001 (セッション文脈外)
        return []


def apply_session_tool_overrides(tools: list[BaseTool]) -> list[BaseTool]:
    """tools のうち read_skill を、このセッションのドラフト名を足した複製へ差し替える。

    メインエージェントのグラフ構築（src/graph.py）とサブエージェントのツール構築
    （src/subagent.py）の両方で通すため、委譲先も本人のドラフトを read_skill できる。
    ドラフトが無ければ tools をそのまま返す。
    """
    names = _session_draft_names()
    if not names:
        return tools
    replacement = session_read_skill(names)
    return [replacement if t.name == "read_skill" else t for t in tools]
