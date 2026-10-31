"""provide_download ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import json
import logging

from .. import skill_drafts
from . import _state
from ._workdir import _foreign_tmp_dir_error, _resolve_workdir

logger = logging.getLogger(__name__)


@tool
def provide_download(file_paths: list[str]) -> str:
    """Offer existing files in the chat as download buttons.

    Use it when you want the user to be able to download uploaded files, existing files found with
    Read/Glob etc., or files generated in earlier work again. Like Read, it does not restrict paths
    (any absolute path on the local file system can be specified, except other sessions'
    `_tmp_<created time>_<short ID>` folders, which cannot be offered). If several are specified,
    the download buttons are shown together in one message (for just one file, pass a list with one
    entry).

    Args:
        file_paths: A list of absolute paths of the files to offer for download
            (relative paths are resolved against the session's working directory).

    Returns:
        On success: a JSON string of the form {"output_paths": ["...", ...]}
            (download buttons are shown together in the chat automatically).
        If even one file is not found, it returns an "Error: ..." string
            (no partial success).
    """
    resolved: list[Path] = []
    for file_path in file_paths:
        path = Path(file_path)
        if not path.is_absolute():
            path = _resolve_workdir() / path
        path = path.resolve()
        tmp_error = _foreign_tmp_dir_error(path) or skill_drafts.access_error(
            path, "read", _state._LLM_CONFIG, skill_drafts.current_draft_user()
        )
        if tmp_error:
            return tmp_error
        if not path.is_file():
            return f"Error: file not found: {file_path}"
        resolved.append(path)
    logger.info("provide_download: %s", resolved)
    return json.dumps({"output_paths": [str(p) for p in resolved]}, ensure_ascii=False)
