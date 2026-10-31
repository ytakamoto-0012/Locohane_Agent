"""lock_plan_mode ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import chainlit as cl
import logging

from ..plan_persist import persist_plan_state

from . import _state
from ._plan_render import _render_plan_payload

logger = logging.getLogger(__name__)


@tool
async def lock_plan_mode() -> str:
    """Manually return from Edit Automatically to Plan Mode (revoke the approval).

    You may call it on your own judgment, without the user's approval, when you want to stop the
    automatic execution midway and lock the write tools again without waiting for all steps to
    finish. The plan (list of steps) itself is not deleted. To use the write tools again, get
    approval again with approve_plan.

    Returns:
        A text telling the result of the state change.
    """
    was_approved = bool(cl.user_session.get("plan_approved"))
    cl.user_session.set("plan_approved", False)
    if was_approved:
        await persist_plan_state()
    plan = cl.user_session.get("plan")
    message: cl.Message | None = cl.user_session.get("plan_message")
    if message is not None and plan is not None:
        finished = all(s["status"] == "completed" for s in plan)
        message.content = _render_plan_payload(plan, finished=finished, approved=False)
        await message.update()
    logger.info("lock_plan_mode: called (previous state: %s)", "approved" if was_approved else "not approved")
    if not was_approved:
        return "Already in Plan Mode (no change)."
    return "Returned to Plan Mode. The write tools are blocked again. " "To resume, get approval again with approve_plan."


async def toggle_plan_mode_from_ui() -> None:
    """送信ボタン付近の Plan Mode / Edit Automatically バッジをユーザーが
    クリックした際に呼ばれる（app.py の action_callback("toggle_plan_mode")
    経由）。LLMツールではなく、ユーザーがUIから直接操作するための関数。

    計画が存在しない場合は何もしない（切り替える対象が無いため）。また
    config.ini の [plan].allow_badge_unlock が False の場合、Plan Mode →
    Edit Automatically 方向（ロック解除）のクリックは無視する（Edit
    Automatically → Plan Mode 方向のクリックは常に許可する）。
    """
    plan = cl.user_session.get("plan")
    if not plan:
        return
    currently_approved = bool(cl.user_session.get("plan_approved"))
    if not currently_approved and not _state._PLAN_BADGE_ALLOW_UNLOCK:
        logger.info("toggle_plan_mode_from_ui: ignored a click in the unlock direction because allow_badge_unlock=False")
        return
    approved = not currently_approved
    cl.user_session.set("plan_approved", approved)
    await persist_plan_state()
    message: cl.Message | None = cl.user_session.get("plan_message")
    if message is not None:
        finished = all(s["status"] == "completed" for s in plan)
        message.content = _render_plan_payload(plan, finished=finished, approved=approved)
        await message.update()
    logger.info(
        "toggle_plan_mode_from_ui: the user clicked the badge (new state: %s)",
        "approved" if approved else "not approved",
    )
