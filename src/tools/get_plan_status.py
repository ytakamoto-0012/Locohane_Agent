"""get_plan_status ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import chainlit as cl

from ._plan_render import _render_plan


@tool
async def get_plan_status() -> str:
    """A read-only tool that checks whether you are currently in Plan Mode (write tools locked) or
    Edit Automatically (allowed to execute an approved plan).

    Call it any time you are not sure about your state before calling the write tools
    (run_script, execute_python_code) (it is never blocked, whether or not there is a plan or it is
    approved).

    Returns:
        A text with the current mode ("Plan Mode" or "Edit Automatically") and, if a plan exists,
        its list of steps with each status. If there is no plan, a text saying so.
    """
    plan = cl.user_session.get("plan")
    approved = bool(cl.user_session.get("plan_approved"))
    if not plan:
        return "Current state: Plan Mode (no plan yet). The write tools are blocked."
    mode = "Edit Automatically" if approved else "Plan Mode"
    return f"Current state: {mode}\n\n" + _render_plan(plan)
