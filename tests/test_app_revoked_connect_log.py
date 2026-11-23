"""削除・パスワード変更したユーザーの開いたままのタブが再接続するたびに、Chainlit が ERROR（トレースバック付き）を
並べないことの回帰テスト（app.py の _RevokedConnectLogFilter）。"""

from __future__ import annotations

import contextvars
import logging
import sys

from fastapi import HTTPException

import app


def _record(level: int, msg: str, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord("chainlit", level, __file__, 1, msg, (), exc_info)


def _run_in_new_context(fn):
    return contextvars.Context().run(fn)


def test_revoked_connect_errors_become_one_info_line() -> None:
    def run():
        f = app._RevokedConnectLogFilter()
        first = _record(logging.ERROR, "Exception authenticating connection: %s", HTTPException(401, app._REVOKED_DETAIL))
        assert f.filter(first)
        assert first.levelno == logging.INFO and first.exc_info is None
        assert "no longer valid" in first.getMessage()
        # 続く "Authentication failed" は出さない。
        assert not f.filter(_record(logging.ERROR, "Authentication failed in websocket connect."))

    _run_in_new_context(run)


def test_other_authentication_errors_stay_errors() -> None:
    def run():
        f = app._RevokedConnectLogFilter()
        try:
            raise ValueError("bad token")
        except ValueError:
            exc = sys.exc_info()[1]
        other = _record(logging.ERROR, "Exception authenticating connection: %s", exc)
        assert f.filter(other) and other.levelno == logging.ERROR and other.exc_info is not None
        failed = _record(logging.ERROR, "Authentication failed in websocket connect.")
        assert f.filter(failed) and failed.levelno == logging.ERROR
        # 別の理由の 401 も ERROR のまま。
        other_401 = _record(logging.ERROR, "x", HTTPException(401, "Invalid authentication token"))
        assert f.filter(other_401) and other_401.levelno == logging.ERROR

    _run_in_new_context(run)
