"""run_script ツール。"""

from __future__ import annotations

from langchain_core.tools import tool

from ._script_job import _run_script_impl


@tool
async def run_script(skill_name: str, script_filename: str, script_args: list[str] | None = None) -> str:
    """Run a script under the skill's scripts/ folder and return its stdout/stderr.

    The working directory is the directory the user set for the session with the working folder
    icon, if any. If it is not set, not accessible, or not writable, this session's own temporary
    folder (`_tmp_<created time>_<short ID>`) is used (the same place as `write_dir` of
    `check_work_dir_status`; this avoids accidents where artifacts written directly in a shared
    folder visible to several sessions under the same path become visible to other sessions).
    **In that case, the artifacts are not directly visible to the user. If you have
    `provide_download`, offer them with it. If you do not (sub-agents such as `worker`), report the
    absolute paths of the artifacts in your final answer and leave offering them to the delegator
    (the main agent, which has `provide_download`). Do not try workarounds yourself.**
    The timeout is the configured value (60 seconds by default). This tool call itself blocks until
    completion, so for scripts expected to run close to the timeout, use run_script_background
    instead.
    .py scripts are started with the configured Python executable.
    It corresponds to the third stage (Execute) of progressive disclosure in the Agent Skills
    standard. As a write tool, it cannot run unless a plan has been approved with
    create_plan/approve_plan (it returns an error if not approved). However, read-only scripts
    without side effects (some are registered as exceptions in advance, e.g. read_vba.py of
    excel-vba-read) are exempt from this approval check.

    **Important: writes are limited to the working directory (sandbox)**
    A write guard is injected into the subprocess, so if open() in a write mode or
    os.remove/os.rename/shutil.move etc. target anything outside the working directory and
    `_tmp_<created time>_<short ID>` (including other drives and the Locohane project itself), it
    fails with PermissionError. Always specify output paths (output_path/--output etc.) under the
    working directory.
    Reading existing files is not covered by this guard and is unrestricted.

    Args:
        skill_name: The folder name of the skill that has the script.
        script_filename: The file name of the script to run (e.g. "count.py").
            No path or scripts/ prefix is needed; it is searched automatically under the scripts/
            folder of the skill. If files with the same name exist at several levels, the
            shallowest one is used.
        script_args: A list of extra arguments for the script (optional). Put each option and its
            value in separate elements (e.g. ["--file", "C:\\aaa.png"]). Specify write
            destinations under the working directory (see the sandbox above).

    Returns:
        A string with "[exit code] N" followed by stdout and stderr, if any, under the headings
        "[stdout]" and "[stderr]". If the skill has no scripts/, the script is not found, the plan
        is not approved, it timed out, or starting it failed, it returns an "Error: ..." string
        instead of raising.
    """
    return await _run_script_impl(skill_name, script_filename, script_args)
