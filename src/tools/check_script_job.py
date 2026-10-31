"""check_script_job ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import time

from . import _state
from ._script_job import _finalize_script_job_result, _format_background_job_progress, _resolve_job


@tool
async def check_script_job(job_id: str) -> str:
    """Check the status and result of a job started by run_script_background/execute_python_code_background.

    Normally run_script_background/execute_python_code_background itself waits until the job
    finishes and returns the final result directly, so you do not need to call this tool. Call it
    only when they reached the safety limit and returned a job_id
    (that is, a very long job).

    While running, it returns the elapsed seconds and the latest output (the tail of stdout and
    stderr, 4000 characters at most). If it ended as done, failed, timed out or killed, it returns
    the final result in the same form as run_script ("[exit code] N" followed by
    "[stdout]"/"[stderr]"), and the same job_id can no longer be used afterwards (it is removed
    from the registry). job_ids started by other sessions cannot be viewed.

    When it returns "running" ("Running (elapsed N s, ...)."), do not call it again every few
    seconds. Tell the user the progress in a word and end the turn to wait for the user's next
    message, or call it again after a sufficient interval (tens of seconds or more). Taking a long
    time is neither abnormal nor a reason to stop it. If you call it again too soon against this
    instruction, the server rejects it and returns a message like "Checked too soon" (the default
    minimum interval is 20 seconds).

    Args:
        job_id: The ID included in the return value of
            run_script_background/execute_python_code_background.

    Returns:
        A string with the status or the final result. If the job_id is unknown or belongs to
        another session, or less than background_min_poll_interval_seconds has passed since the
        previous "running" response, an "Error: ..." / rejection message
        ([scripts].background_min_poll_message, "Checked too soon..." by default) string.
    """
    resolved = _resolve_job(job_id)
    if isinstance(resolved, str):
        return resolved
    job = resolved

    if job.status == "running":
        now = time.monotonic()
        if (
            _state._SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS > 0
            and job.last_polled_at is not None
            and (now - job.last_polled_at) < _state._SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS
        ):
            wait_remaining = int(_state._SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS - (now - job.last_polled_at)) + 1
            return _state._SCRIPT_BACKGROUND_MIN_POLL_MESSAGE.format(
                wait_remaining=wait_remaining,
                job_id=job_id,
                min_interval=_state._SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS,
            )
        job.last_polled_at = now
        return _format_background_job_progress(job, job_id)

    return _finalize_script_job_result(job, job_id)
