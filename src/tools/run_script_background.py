"""run_script_background ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import asyncio
import chainlit as cl
import logging
import shutil
import time
import uuid

from . import _script_job
from . import _state
from ._script_job import _BackgroundJob, _background_job_started_message, _finalize_script_job_result, _purge_stale_background_jobs, _run_background_job
from ._subprocess_env import _run_script_guard_env

logger = logging.getLogger(__name__)


@tool
async def run_script_background(skill_name: str, script_filename: str, script_args: list[str] | None = None) -> str:
    """Start a script under the skill's scripts/ folder in the background.

    For scripts expected to take a long time. Unlike run_script, this turn itself blocks until
    completion (unless the safety limit below is reached) (the Chainlit UI keeps showing
    "running"). While waiting, progress (elapsed seconds, the tail of stdout/stderr) is
    automatically posted to the chat for humans, so you do not need to poll by calling
    check_script_job repeatedly (the code posts progress to the chat directly, so the number of
    LLM calls and token usage do not increase). One call returns the final result in the same form
    as run_script.

    Only if it still has not finished after the configured safety limit
    ([scripts].background_inline_wait_max_seconds), it returns a notice including the job_id and
    ends this turn (the job itself keeps running in the background). Only in that case, use
    check_script_job (to get the result) and stop_script_job (only when explicitly told to
    interrupt) in later turns.

    Argument resolution, working directory resolution, the plan approval check (including the
    exceptions) and the write sandbox guard (output limited to the working directory /
    default_workdir; see the docstring of run_script for details) are the same as run_script.
    The limit before a background job is forcibly stopped is 3600 seconds by default.

    Args:
        skill_name: The folder name of the skill that has the script.
        script_filename: The file name of the script to run (e.g. "count.py").
        script_args: A list of extra arguments for the script (optional). Put each option and its
            value in separate elements (e.g. ["--file", "C:\\aaa.png"]).

    Returns:
        Normally the final result string in the same form as run_script. Only when the safety limit
        is reached, a notice string including the job_id. If the arguments are invalid, the script
        is not found, the plan is not approved, or starting it failed, it returns an "Error: ..."
        string like run_script (no job is created in that case).
    """
    prepared = _script_job._prepare_script_execution(skill_name, script_filename, script_args)
    if isinstance(prepared, str):
        return prepared
    cmd, workdir = prepared
    env, guard_dir = _run_script_guard_env(workdir, skill_name, script_filename)

    _purge_stale_background_jobs()

    logger.info("run_script_background: %s %s cwd=%s", skill_name, script_filename, workdir)
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as e:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)
        return f"Error: could not start the script: {e}"

    job_id = uuid.uuid4().hex[:12]
    job = _BackgroundJob(
        process=process,
        thread_id=cl.user_session.get("thread_id") or "",
        skill_name=skill_name,
        script_filename=script_filename,
        started_at=time.monotonic(),
        stdout_chunks=[],
        stderr_chunks=[],
        status="running",
        returncode=None,
        error_message=None,
        guard_dir=guard_dir,
    )
    job.runner_task = asyncio.create_task(_run_background_job(job, job_id))
    _script_job._BACKGROUND_JOBS[job_id] = job

    # asyncio.shield: このwait_forがタイムアウトして例外を送出しても、
    # job.runner_task 自体はキャンセルされず裏側で動き続ける
    # （安全上限超過時のフォールバック挙動の要。dispatch_agent と同じ設計）。
    # 0以下は無期限待ち。
    wait_timeout = _state._SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS if _state._SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS > 0 else None
    try:
        await asyncio.wait_for(asyncio.shield(job.runner_task), timeout=wait_timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "run_script_background: reached the safety limit (%s seconds), returning job_id and ending the turn: job_id=%s",
            wait_timeout,
            job_id,
        )
        return _background_job_started_message(job_id)

    return _finalize_script_job_result(job, job_id)
