"""stop_dispatch_agent_job ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import asyncio
import logging

from . import _dispatch_agent_job
from ._dispatch_agent_job import _resolve_dispatch_agent_job
from ._script_job import _JOB_OUTPUT_TAIL_CHARS
from .write_scratch_note import _scratch_notes_path_for_run

logger = logging.getLogger(__name__)


@tool
async def stop_dispatch_agent_job(job_id: str) -> str:
    """Forcibly stop a job started by dispatch_agent.

    Use it only when the user explicitly tells you to interrupt, cancel or stop. Taking a long time
    (check_dispatch_agent_job keeps returning "running") is not in itself a reason to stop it on
    your own judgment.
    job_ids started by other sessions cannot be operated.

    Args:
        job_id: The ID included in the return value of dispatch_agent.

    Returns:
        A string with the result of the forced stop. If the job_id is unknown, belongs to another
        session, or has already finished, an "Error: ..." string.
    """
    resolved = _resolve_dispatch_agent_job(job_id)
    if isinstance(resolved, str):
        return resolved
    job = resolved

    if job.status != "running":
        return f"Error: job_id '{job_id}' has already finished (status={job.status}). " "Get the result with check_dispatch_agent_job."

    job.status = "killed"
    logger.warning(
        "dispatch_agent: force-stopping on a stop instruction (run_id=%s, job_id=%s, iter=%d/%d)",
        job.run_id,
        job_id,
        job.current_iteration,
        job.max_iterations,
    )
    if job.runner_task is not None:
        job.runner_task.cancel()
        try:
            await job.runner_task
        except asyncio.CancelledError:
            pass

    note_path = _scratch_notes_path_for_run(job.run_id)
    tail = ""
    if note_path.is_file():
        tail = note_path.read_text(encoding="utf-8", errors="replace")[-_JOB_OUTPUT_TAIL_CHARS:].rstrip()
    _dispatch_agent_job._DISPATCH_AGENT_JOBS.pop(job_id, None)
    if tail:
        return f"Forcibly stopped. The sub-agent run was interrupted.\n[Progress notes at the forced stop (tail)]\n{tail}"
    return "Forcibly stopped. The sub-agent run was interrupted."
