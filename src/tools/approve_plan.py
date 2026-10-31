"""approve_plan ツールと Plan Mode バッジの手動切り替え。"""

from __future__ import annotations

from langchain_core.tools import tool
import chainlit as cl
import logging

from ..plan_persist import persist_plan_state

from . import _state
from ._ask_relay_helper import _ask_with_cross_session_relay
from ._plan_render import _render_plan, _render_plan_payload
from ._state import _resolve_ask_timeout
from ..i18n import t

logger = logging.getLogger(__name__)


@tool
async def approve_plan() -> str:
    """Get the user's approval for the plan you created.

    Shows the plan and lets the user approve or reject it. Once approved, the hard block on
    run_script/run_script_background/execute_python_code/
    execute_python_code_background is lifted and they can run. A timeout (no response) is treated
    as not approved for safety, but the returned text distinguishes it from an explicit rejection
    (no response most likely just means the user is busy, so the plan itself does not need to be
    recreated).

    If [plan].auto_approve is true in config.ini, no approve/reject buttons are shown and nothing is
    awaited; the plan is treated as approved immediately (for unattended automation).

    Returns:
        A text telling whether it was approved, explicitly rejected, or timed out. If there is no
        plan, it returns an "Error: ..." string instead of raising.
    """
    plan = cl.user_session.get("plan")
    if not plan:
        return "Error: there is no plan. Call create_plan first."
    if _state._PLAN_AUTO_APPROVE:
        cl.user_session.set("plan_approved", True)
        cl.user_session.set("plan_denied_just_now", False)
        await persist_plan_state()
        message: cl.Message | None = cl.user_session.get("plan_message")
        if message is not None:
            message.content = _render_plan_payload(plan, approved=True)
            await message.update()
        await cl.Message(content=t("plan.auto_approved")).send()
        logger.info("approve_plan: approved automatically because auto_approve=true")
        return "[plan].auto_approve is enabled in config.ini, so the plan was approved automatically without asking the user. You can now run the write tools (run_script/execute_python_code)."
    content = _render_plan(plan, heading=t("plan.heading")) + "\n\n" + t("plan.approve_question")
    actions = [
        cl.Action(name="approve", payload={"value": "approve"}, label=t("plan.approve_button")),
        cl.Action(name="deny", payload={"value": "deny"}, label=t("plan.deny_button")),
    ]
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    timeout = _resolve_ask_timeout(_state._APPROVAL_TIMEOUT_SECONDS)

    def _factory():
        return cl.AskActionMessage(content=content, actions=actions, timeout=timeout).send()

    res = await _ask_with_cross_session_relay(thread_id, _factory, timeout)
    approved = res is not None and res["payload"].get("value") == "approve"
    cl.user_session.set("plan_approved", approved)
    # 前回却下時に立てたフラグが誤って残らないよう、承認・タイムアウト時は
    # 明示的にクリアする（このターンは却下ではないため）。
    cl.user_session.set("plan_denied_just_now", False)
    await persist_plan_state()
    message: cl.Message | None = cl.user_session.get("plan_message")
    if message is not None:
        message.content = _render_plan_payload(plan, approved=approved)
        await message.update()
    if approved:
        logger.info("approve_plan: approved")
        return "The user approved the plan. You can now run the write tools (run_script/execute_python_code)."
    if res is None:
        logger.info("approve_plan: no response (timeout)")
        return (
            f"There was no response from the user for {_state._APPROVAL_TIMEOUT_SECONDS} seconds "
            "(they may be away). The plan itself is kept as is, so "
            "there is no need to recreate it. Wait a while and then call approve_plan "
            "again."
        )
    logger.info("approve_plan: explicitly rejected")
    # app.py の on_tool_end がこのフラグを見て、ツール呼び出しを続けさせず
    # このターンの処理を強制的に打ち切る（LLMが「計画を微修正して続行しよう」
    # と自己判断してしまうのを、プロンプト指示だけに頼らずコード側で確実に防ぐため）。
    cl.user_session.set("plan_denied_just_now", True)
    return "The user rejected the plan. Do not call any more tools; " "state that it was rejected as your final answer and finish."
