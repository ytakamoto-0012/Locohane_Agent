"""show_help(help) ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import logging

from . import _state

logger = logging.getLogger(__name__)


@tool("help")
def show_help() -> str:
    """Return the help text about how to use this system.

    Call it when the user asks for help, how to use it, or where to send feedback.
    The text is written in the configured help Markdown file, and this tool is a thin wrapper that
    just reads and returns it as is (do not make up help content). Relay it to the user in the
    user's language.

    Returns:
        The help text (UTF-8 text, Markdown). If init_tools() has not been run or the help_path
        file does not exist, it returns an "Error: ..." string instead of raising.
    """
    if _state._HELP_PATH is None:
        return "Error: init_tools() has not been run"
    if not _state._HELP_PATH.is_file():
        return f"Error: help file not found: {_state._HELP_PATH}"
    logger.info("show_help")
    return _state._HELP_PATH.read_text(encoding="utf-8")


# init_tools() の _resolve_agent_types() がこのリストを実行時に参照するだけなので、
# 定義順は init_tools()/dispatch_agent より後でもよい（analyze_image・メモリー系
# ツールを含めるため、それらの定義後に置く）。
