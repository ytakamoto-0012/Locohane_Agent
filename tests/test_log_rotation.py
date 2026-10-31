"""LineCountRotatingFileHandler の回帰テスト（src/log_rotation.py）。"""

from __future__ import annotations

import datetime
import logging

from src import log_rotation as lr


class _FixedDatetime(datetime.datetime):
    """datetime.now() を固定値に差し替えるためのテスト用サブクラス。"""

    _fixed = datetime.datetime(2026, 7, 25, 14, 30, 0)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def _make_record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, None, None)


def _new_handler(tmp_path, max_lines, clear_on_startup=True):
    handler = lr.LineCountRotatingFileHandler(
        tmp_path, max_lines, clear_on_startup=clear_on_startup
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def test_creates_timestamped_file(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "datetime", _FixedDatetime)
    handler = _new_handler(tmp_path, max_lines=10)
    handler.emit(_make_record("hello"))
    handler.close()

    assert (tmp_path / "app_20260725_143000.log").exists()
    assert (tmp_path / "app_20260725_143000.log").read_text(encoding="utf-8") == "hello\n"


def test_rotates_on_line_count_and_appends_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "datetime", _FixedDatetime)
    handler = _new_handler(tmp_path, max_lines=2)

    handler.emit(_make_record("line1"))
    handler.emit(_make_record("line2"))  # ここで max_lines(2) に到達しローテーション
    handler.emit(_make_record("line3"))
    handler.close()

    assert (tmp_path / "app_20260725_143000.log").exists()
    assert (tmp_path / "app_20260725_143000_1.log").exists()
    assert (tmp_path / "app_20260725_143000_1.log").read_text(encoding="utf-8") == "line3\n"


def test_multiline_record_counts_all_newlines(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "datetime", _FixedDatetime)
    handler = _new_handler(tmp_path, max_lines=2)

    handler.emit(_make_record("line1\nline2\nline3"))
    handler.close()

    # 1レコードでも改行3つ（4行相当のテキスト）を書けば max_lines(2) 超過でローテーション済み。
    assert (tmp_path / "app_20260725_143000.log").exists()
    assert (tmp_path / "app_20260725_143000_1.log").exists()


def test_clear_on_startup_always_creates_new_file(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "datetime", _FixedDatetime)
    existing = tmp_path / "app_20260725_143000.log"
    existing.write_text("old1\nold2\n", encoding="utf-8")

    handler = _new_handler(tmp_path, max_lines=10, clear_on_startup=True)
    handler.emit(_make_record("new"))
    handler.close()

    assert (tmp_path / "app_20260725_143000_1.log").read_text(encoding="utf-8") == "new\n"
    assert existing.read_text(encoding="utf-8") == "old1\nold2\n"


def test_resume_appending_when_under_max_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "datetime", _FixedDatetime)
    existing = tmp_path / "app_20260725_143000.log"
    existing.write_text("old1\nold2\n", encoding="utf-8")

    handler = _new_handler(tmp_path, max_lines=10, clear_on_startup=False)
    handler.emit(_make_record("new"))
    handler.close()

    assert existing.read_text(encoding="utf-8") == "old1\nold2\nnew\n"


def test_resume_rotates_when_existing_file_already_over_max_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "datetime", _FixedDatetime)
    existing = tmp_path / "app_20260725_143000.log"
    existing.write_text("old1\nold2\n", encoding="utf-8")

    handler = _new_handler(tmp_path, max_lines=2, clear_on_startup=False)
    handler.emit(_make_record("new"))
    handler.close()

    assert (tmp_path / "app_20260725_143000_1.log").read_text(encoding="utf-8") == "new\n"
    assert existing.read_text(encoding="utf-8") == "old1\nold2\n"
