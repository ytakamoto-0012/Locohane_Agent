"""check_dispatch_agent_job ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import time

from . import _state
from ._dispatch_agent_job import _finalize_dispatch_agent_job_result, _format_dispatch_agent_progress, _resolve_dispatch_agent_job


@tool
async def check_dispatch_agent_job(job_id: str) -> str:
    """Check the status and result of a job started by dispatch_agent.

    Normally dispatch_agent itself waits until the job finishes and returns the final answer
    directly, so you do not need to call this tool. Call it only when dispatch_agent reached the
    safety limit and returned a job_id (that is, a very long job).

    While running, it returns the elapsed seconds, the iteration count and the progress notes
    written with write_scratch_note (if any, the last 4000 characters at most). If it ended as
    done, killed or error, it returns the final answer (text in the same form as dispatch_agent),
    and the same job_id can no longer be used afterwards (it is removed from the registry).
    job_ids started by other sessions cannot be viewed.

    Scratch notes are the only way to get the sub-agent's internal progress. Depending on the
    agent_type (a type without write_scratch_note, or if it never wrote any), the progress part
    stays empty while running; this is not abnormal.

    When it returns "running", do not call it again every few seconds. Tell the user the progress
    in a word and end the turn to wait for the user's next message, or call it again after a
    sufficient interval (tens of seconds or more). Taking a long time is neither abnormal nor a
    reason to stop it. If you call it again too soon against this instruction, the server rejects
    it and returns a message like "Checked too soon".

    Args:
        job_id: The ID included in the return value of dispatch_agent.

    Returns:
        A string with the status or the final result. If the job_id is unknown or belongs to
        another session, or less than [subagent].background_min_poll_interval_seconds has passed
        since the previous "running" response, an "Error: ..." / rejection message string.
    """
    resolved = _resolve_dispatch_agent_job(job_id)
    if isinstance(resolved, str):
        return resolved
    job = resolved

    if job.status == "running":
        now = time.monotonic()
        if (
            _state._DISPATCH_AGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS > 0
            and job.last_polled_at is not None
            and (now - job.last_polled_at) < _state._DISPATCH_AGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS
        ):
            wait_remaining = int(_state._DISPATCH_AGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS - (now - job.last_polled_at)) + 1
            return _state._DISPATCH_AGENT_BACKGROUND_MIN_POLL_MESSAGE.format(
                wait_remaining=wait_remaining,
                job_id=job_id,
                min_interval=_state._DISPATCH_AGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS,
            )
        job.last_polled_at = now
        return _format_dispatch_agent_progress(job, job_id)

    return _finalize_dispatch_agent_job_result(job, job_id)
