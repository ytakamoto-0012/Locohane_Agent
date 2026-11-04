"""Read/Glob/Grep の処理上限（config.ini [file_tools_limits]）。

これらのツールは同期関数として別スレッドで動き、外から止められない。
10万件超のフォルダや GB 級のファイルを渡されると、終わるまでスレッドを
占有し続けてアプリ全体が固まったように見えるため、ツール自身がこの上限で
処理を打ち切る。
"""

from __future__ import annotations

from dataclasses import dataclass
import time

from . import _state

_MB = 1024 * 1024


@dataclass(frozen=True)
class FileToolsLimits:
    """Read/Glob/Grep の上限値。既定値は config.ini [file_tools_limits] の既定と同じ。"""

    search_time_limit_seconds: float = 120.0
    grep_max_files: int = 20000
    grep_max_file_bytes: int = 10 * _MB
    regex_timeout_seconds: float = 2.0
    full_read_max_bytes: int = 20 * _MB


def current_limits() -> FileToolsLimits:
    """現在の設定から上限値を返す（設定未注入のテスト等では既定値）。"""
    cfg = _state._LLM_CONFIG
    if cfg is None:
        return FileToolsLimits()
    default = FileToolsLimits()
    return FileToolsLimits(
        search_time_limit_seconds=getattr(cfg, "file_tools_search_time_limit_seconds", default.search_time_limit_seconds),
        grep_max_files=getattr(cfg, "file_tools_grep_max_files", default.grep_max_files),
        grep_max_file_bytes=int(getattr(cfg, "file_tools_grep_max_file_mb", default.grep_max_file_bytes / _MB) * _MB),
        regex_timeout_seconds=getattr(cfg, "file_tools_regex_timeout_seconds", default.regex_timeout_seconds),
        full_read_max_bytes=int(getattr(cfg, "file_tools_full_read_max_mb", default.full_read_max_bytes / _MB) * _MB),
    )


class Deadline:
    """処理時間の上限。ループの中で expired() を見て自分で抜けるために使う。"""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._end = time.monotonic() + seconds

    def expired(self) -> bool:
        return time.monotonic() >= self._end

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())
