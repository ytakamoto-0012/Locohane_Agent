"""cleanup_old_files の pattern 引数の回帰テスト（evals.log 巻き込み防止）。"""

from __future__ import annotations

import os
import time

from src.cleanup import cleanup_old_files


def test_pattern_filters_unrelated_files(tmp_path):
    old_app_log = tmp_path / "app_20260101_00.log"
    old_app_log.write_text("x", encoding="utf-8")
    unrelated = tmp_path / "evals.log"
    unrelated.write_text("x", encoding="utf-8")

    old_time = time.time() - 100 * 86400
    os.utime(old_app_log, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))

    deleted = cleanup_old_files(tmp_path, retention_days=7, pattern="app_*.log")

    assert deleted == 1
    assert not old_app_log.exists()
    assert unrelated.exists()


def test_default_pattern_preserves_previous_behavior(tmp_path):
    old_file = tmp_path / "anything.txt"
    old_file.write_text("x", encoding="utf-8")

    old_time = time.time() - 100 * 86400
    os.utime(old_file, (old_time, old_time))

    deleted = cleanup_old_files(tmp_path, retention_days=7)

    assert deleted == 1
    assert not old_file.exists()
