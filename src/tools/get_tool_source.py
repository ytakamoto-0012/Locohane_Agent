"""get_tool_source ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import logging

from ._duplicate_guard import _check_file_tools_duplicate
from ._safe_path import _resolve_script_filename

logger = logging.getLogger(__name__)


@tool
def get_tool_source(skill_name: str, script_filename: str) -> str:
    """Return the absolute path of a script run with run_script (not its contents).

    Use it to investigate the cause when run_script returns an error (non-zero exit code, stack
    trace). Get the absolute path of the source file with this tool, then if needed check its
    contents with read_skill_file, or inside execute_python_code do something like
    `sys.path.insert(0, "<parent directory of this path>")` to import helper modules of the same
    skill such as _common.py directly, to investigate or run alternative code.

    Args:
        skill_name: The folder name of the skill that has the script.
        script_filename: The file name to look for (e.g. "read_file.py"). No path or scripts/
            prefix is needed; it is searched automatically under the scripts/ folder of the skill.

    Returns:
        The absolute path of the script. If skill_name has no scripts/ directory or the script is
        not found, it returns an "Error: ..." string instead of raising.
    """
    try:
        script_path = _resolve_script_filename(skill_name, script_filename)
    except ValueError as e:
        return f"Error: {e}"
    dup_error = _check_file_tools_duplicate("get_tool_source", f"get_tool_source\x00{skill_name}\x00{script_filename}")
    if dup_error:
        return dup_error
    logger.info("get_tool_source: %s/%s", skill_name, script_filename)
    return str(script_path)
