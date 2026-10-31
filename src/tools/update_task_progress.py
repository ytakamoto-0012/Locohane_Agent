"""update_task_progress ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import chainlit as cl
import logging

from ..plan_persist import persist_plan_state

from ._plan_render import _render_plan_payload

logger = logging.getLogger(__name__)


_VALID_TASK_STATUSES = ("pending", "in_progress", "completed")

@tool
async def update_task_progress(step_index: int, status: str) -> str:
    """Update the progress status of a plan step and reflect it in the checklist shown.

    Set "in_progress" before running a step and "completed" after it finishes, to show the user the
    progress. While "in_progress", the checklist shows the activeForm passed to create_plan instead
    of content. For steps that issue dispatch_agent in parallel, you may set all of them to
    "in_progress" and issue them together in the same response (several "in_progress" at once are
    allowed). When every step is completed, the plan is considered finished and the approval is
    lifted (unrelated run_script/execute_python_code calls after that are blocked again).

    Do not mark steps for run_script_background/execute_python_code_background completed just
    because the job was started. check_script_job itself is read-only and can be called any time,
    but marking the step completed right after starting lifts the approval and shows Plan Mode, so
    you may wrongly think "the plan must be redone" while the job is still running, causing
    unnecessary repeated create_plan/approve_plan. Mark it completed only after you confirmed the
    final result (a state other than running) with check_script_job.

    Args:
        step_index: The index of steps passed to create_plan (0-based).
        status: One of "pending" | "in_progress" | "completed".

    Returns:
        A short text describing the update. If there is no plan, step_index is out of range, or
        status is invalid, it returns an "Error: ..." string instead of raising.
    """
    plan = cl.user_session.get("plan")
    if not plan:
        return "Error: there is no plan. Call create_plan first."
    if status not in _VALID_TASK_STATUSES:
        return f"Error: status must be one of {_VALID_TASK_STATUSES}: {status}"
    if not (0 <= step_index < len(plan)):
        return f"Error: step_index is out of range (0 to {len(plan) - 1}): {step_index}"

    plan[step_index]["status"] = status
    cl.user_session.set("plan", plan)
    finished = all(s["status"] == "completed" for s in plan)
    if finished:
        cl.user_session.set("plan_approved", False)
    # ステップ進捗も plan_approved と同じ理由で即時persistする。完了時に
    # 限定すると、途中のステップ更新後にターンが異常終了した場合（停止
    # ボタン・通信エラー等）、再開時のチェックリスト表示が前回persist時点
    # （create_plan直後など）まで巻き戻ってしまう。
    await persist_plan_state()

    message: cl.Message | None = cl.user_session.get("plan_message")
    if message is not None:
        message.content = _render_plan_payload(plan, finished=finished, approved=cl.user_session.get("plan_approved", False))
        await message.update()

    logger.info("update_task_progress: step=%d status=%s finished=%s", step_index, status, finished)
    label = plan[step_index]["content"]
    suffix = "\nAll steps of the plan are completed." if finished else ""
    return f"Updated step {step_index} \"{label}\" to {status}.{suffix}"
