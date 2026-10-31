"""stop_script_job ツール。"""

from __future__ import annotations

from langchain_core.tools import tool

from . import _script_job
from ._script_job import _format_job_result, _resolve_job


@tool
async def stop_script_job(job_id: str) -> str:
    """Forcibly stop a job started by run_script_background.

    Use it only when the user explicitly tells you to interrupt, cancel or stop. Taking a long time
    (check_script_job keeps returning "running") is not in itself a reason to stop it on your own
    judgment. Returns the result with stdout/stderr up to the forced stop and removes it from the
    registry. job_ids started by other sessions cannot be operated.

    Args:
        job_id: The ID included in the return value of run_script_background.

    Returns:
        A string with the result of the forced stop. If the job_id is unknown, belongs to another
        session, or has already finished, an "Error: ..." string.
    """
    resolved = _resolve_job(job_id)
    if isinstance(resolved, str):
        return resolved
    job = resolved

    if job.status != "running":
        return f"Error: job_id '{job_id}' has already finished (status={job.status}). " "Get the result with check_script_job."

    job.status = "killed"
    try:
        job.process.kill()
    except ProcessLookupError:
        pass
    if job.runner_task is not None:
        await job.runner_task

    result = _format_job_result(job)
    _script_job._BACKGROUND_JOBS.pop(job_id, None)
    return f"Forcibly stopped.\n{result}"
