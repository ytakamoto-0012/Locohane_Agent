"""Read/Glob/Grep が大きなフォルダ・巨大なファイルで止まらないための上限の回帰テスト。

10万件のフォルダや GB 級のファイルを作る代わりに、上限値（FileToolsLimits）を
小さく差し替えて、打ち切り・飛ばす処理が働くことを確かめる。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from src.tools import _file_tools_limits
from src.tools._file_tools_common import iter_text_lines
from src.tools._file_tools_limits import FileToolsLimits
from src.tools.glob_tool import glob_search
from src.tools.grep_tool import grep_search
from src.tools.read_tool import read_file


class TestGlobDirectChildrenOnly:
    def test_recursive_prefix_does_not_walk_subfolders(self, tmp_path, monkeypatch) -> None:
        (tmp_path / "a.jpg").write_text("a", encoding="utf-8")
        deep = tmp_path / "sub" / "deeper"
        deep.mkdir(parents=True)
        (deep / "b.jpg").write_text("b", encoding="utf-8")
        scanned: list[str] = []
        real_scandir = os.scandir

        def _spy(path):
            scanned.append(str(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", _spy)

        result = glob_search(tmp_path, "**/*.jpg")

        assert result["files"] == [str((tmp_path / "a.jpg").resolve())]
        assert "pattern_note" in result
        # 直下の一覧と、直下のフォルダ sub の件数カウントだけ。sub/deeper の中は見ない。
        assert str((deep).resolve()) not in scanned

    def test_double_star_alone_lists_direct_folders(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "sub").mkdir()

        result = glob_search(tmp_path, "**")

        assert result["files"] == []
        assert [d["path"] for d in result["directories"]] == [str((tmp_path / "sub").resolve())]

    def test_pattern_with_folder_levels_is_error(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="cannot contain folder levels"):
            glob_search(tmp_path, "sub/*.py")

    def test_absolute_pattern_is_error(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="absolute path"):
            glob_search(tmp_path, "C:/images/*.jpg")

    def test_case_insensitive_on_windows(self, tmp_path) -> None:
        (tmp_path / "PHOTO.JPG").write_text("a", encoding="utf-8")

        result = glob_search(tmp_path, "*.jpg")

        if sys.platform == "win32":
            assert result["total_matches"] == 1


class TestGlobLimits:
    def test_large_file_lines_not_counted_with_reason(self, tmp_path) -> None:
        big = tmp_path / "big.log"
        big.write_text("x\n" * 2000, encoding="utf-8")

        result = glob_search(tmp_path, "*.log", limits=FileToolsLimits(full_read_max_bytes=100))

        detail = result["file_details"][0]
        assert detail["binary"] is False
        assert detail["size_bytes"] == big.stat().st_size
        assert detail["total_lines"].startswith("not counted: file too large")
        assert "Read with offset/limit still works" in detail["total_lines"]

    def test_time_limit_marks_counts_not_counted(self, tmp_path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")

        result = glob_search(tmp_path, "*", limits=FileToolsLimits(search_time_limit_seconds=0))

        assert "scan_stopped" in result
        assert result["base_contents"]["file_count"] == "not counted: time limit reached"


class TestGrepLimits:
    def test_content_mode_stops_after_head_limit(self, tmp_path) -> None:
        for i in range(20):
            (tmp_path / f"f{i:02d}.txt").write_text("TODO\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", head_limit=3)

        assert result["returned"] == 3
        assert result["truncated"] is True
        assert isinstance(result["total_matches"], str)
        assert result["total_matches"].startswith("more than 3")

    def test_complete_search_keeps_numeric_total(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("TODO\nTODO\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", head_limit=50)

        assert result["total_matches"] == 2
        assert "scan_stopped" not in result

    def test_large_files_are_skipped_and_reported(self, tmp_path) -> None:
        (tmp_path / "small.txt").write_text("TODO\n", encoding="utf-8")
        (tmp_path / "big.txt").write_text("TODO\n" * 100, encoding="utf-8")

        result = grep_search(tmp_path, "TODO", limits=FileToolsLimits(grep_max_file_bytes=50))

        assert {m["path"] for m in result["matches"]} == {str((tmp_path / "small.txt").resolve())}
        assert result["skipped_large_files"]["count"] == 1
        assert result["skipped_large_files"]["examples"] == [str((tmp_path / "big.txt").resolve())]

    def test_large_binary_files_are_not_reported_as_skipped(self, tmp_path) -> None:
        (tmp_path / "movie.mp4").write_bytes(b"\x00\x01" * 100)
        (tmp_path / "a.txt").write_text("TODO\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", limits=FileToolsLimits(grep_max_file_bytes=50))

        assert "skipped_large_files" not in result
        assert result["total_matches"] == 1

    @pytest.mark.parametrize("glob", ["**/*.py", "*.py", "sub/*.py", "**/sub/*.py"])
    def test_glob_forms_llms_write(self, tmp_path, glob) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.py").write_text("TODO\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("TODO\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", glob=glob)

        assert [m["path"] for m in result["matches"]] == [str((tmp_path / "sub" / "a.py").resolve())]

    def test_file_count_limit_reports_partial_even_without_matches(self, tmp_path) -> None:
        for i in range(5):
            (tmp_path / f"f{i}.txt").write_text("nothing\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", limits=FileToolsLimits(grep_max_files=2))

        assert result["matched"] is False
        assert "2 files" in result["scan_stopped"]

    def test_time_limit_reports_partial(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("TODO\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", limits=FileToolsLimits(search_time_limit_seconds=0))

        assert result["matched"] is False
        assert "time limit" in result["scan_stopped"]

    def test_catastrophic_regex_times_out_per_file(self, tmp_path) -> None:
        (tmp_path / "evil.txt").write_text("a" * 40 + "!\n", encoding="utf-8")
        (tmp_path / "ok.txt").write_text("fine\n", encoding="utf-8")

        started = time.monotonic()
        result = grep_search(tmp_path, r"(a|aa)+$", limits=FileToolsLimits(regex_timeout_seconds=0.2))

        assert time.monotonic() - started < 10
        assert result["skipped_regex_timeout_files"]["count"] == 1
        assert result["skipped_regex_timeout_files"]["examples"] == [str((tmp_path / "evil.txt").resolve())]

    def test_exclude_paths_prunes_subtree(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("TODO\n", encoding="utf-8")
        hidden = tmp_path / "drafts" / "other"
        hidden.mkdir(parents=True)
        (hidden / "b.txt").write_text("TODO\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", exclude_paths=frozenset({hidden.resolve()}))

        assert [m["path"] for m in result["matches"]] == [str((tmp_path / "a.txt").resolve())]

    def test_walk_order_matches_os_walk(self, tmp_path) -> None:
        for rel in ("a.txt", "x/b.txt", "x/y/c.txt", "z/d.txt"):
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("TODO\n", encoding="utf-8")
        expected = [
            os.path.join(root, name) for root, _, files in os.walk(tmp_path.resolve()) for name in files
        ]

        result = grep_search(tmp_path, "TODO")

        assert [m["path"] for m in result["matches"]] == expected

    @pytest.mark.skipif(sys.platform != "win32", reason="ジャンクションは Windows のみ")
    def test_junction_loop_is_not_followed(self, tmp_path) -> None:
        (tmp_path / "a.txt").write_text("TODO\n", encoding="utf-8")
        loop = tmp_path / "loop"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(loop), str(tmp_path)], check=True, capture_output=True)
        try:
            result = grep_search(tmp_path, "TODO")
        finally:
            os.rmdir(loop)

        assert [m["path"] for m in result["matches"]] == [str((tmp_path / "a.txt").resolve())]


class TestReadLargeFile:
    def test_streams_only_needed_lines(self, tmp_path) -> None:
        big = tmp_path / "big.log"
        big.write_text("".join(f"line{i}\n" for i in range(1, 1001)), encoding="utf-8")

        result = read_file(big, offset=10, limit=3, full_read_max_bytes=100)

        assert result["content"] == "11\tline11\n12\tline12\n13\tline13"
        assert result["start_line"] == 11
        assert result["end_line"] == 13
        assert result["size_bytes"] == big.stat().st_size
        assert result["total_lines"].startswith("not counted: file too large")

    def test_reports_total_when_end_is_reached(self, tmp_path) -> None:
        big = tmp_path / "big.log"
        big.write_text("".join(f"line{i}\n" for i in range(1, 51)), encoding="utf-8")

        result = read_file(big, offset=45, limit=10, full_read_max_bytes=100)

        assert result["total_lines"] == 50
        assert result["end_line"] == 50

    def test_streaming_stops_at_content_limit(self, tmp_path, monkeypatch) -> None:
        # limit に巨大な値を渡されても、本文を上限までで打ち切り、続きの読み方を返す
        monkeypatch.setattr(sys.modules["src.tools.read_tool"], "_STREAM_CONTENT_MAX_CHARS", 50)
        big = tmp_path / "big.log"
        big.write_text("".join(f"line{i:03d}\n" for i in range(1, 1001)), encoding="utf-8")

        result = read_file(big, offset=0, limit=1_000_000, full_read_max_bytes=100)

        assert result["end_line"] == 6
        assert "content limit" in result["stopped"] and "offset=6" in result["stopped"]
        assert result["total_lines"].startswith("not counted")

    def test_streaming_stops_at_time_limit(self, tmp_path) -> None:
        big = tmp_path / "big.log"
        big.write_text("".join(f"line{i}\n" for i in range(1, 5001)), encoding="utf-8")

        result = read_file(big, offset=4000, limit=10, full_read_max_bytes=100, time_limit_seconds=0)

        assert result["content"] == ""
        assert result["stopped"].startswith("time limit")

    def test_streaming_detects_cp932_after_long_ascii_head(self, tmp_path, monkeypatch) -> None:
        # 先頭が ASCII だけの Shift-JIS ファイルを UTF-8 と誤認しない
        from src.tools import _file_tools_common as common

        monkeypatch.setattr(common, "_STREAM_ENCODING_PROBE_BYTES", 64)
        f = tmp_path / "sjis.csv"
        f.write_bytes(("header,value\n" * 20 + "東京,1\n大阪,2\n").encode("cp932"))

        assert common.detect_stream_encoding(f) == "cp932"
        result = read_file(f, offset=20, limit=2, full_read_max_bytes=10)
        assert result["content"] == "21\t東京,1\n22\t大阪,2"

    def test_form_feed_does_not_split_lines_in_any_mode(self, tmp_path) -> None:
        # 全体読み・逐次読み・Grep の行番号がそろう（\x0c や U+2028 では区切らない）
        f = tmp_path / "report.txt"
        f.write_text("page1\x0cstill1\nline2 same2\nTODO\n", encoding="utf-8")

        full = read_file(f, offset=0, limit=10)
        streamed = read_file(f, offset=0, limit=10, full_read_max_bytes=1)
        grep = grep_search(f, "TODO")

        assert full["total_lines"] == streamed["total_lines"] == 3
        assert full["content"] == streamed["content"]
        assert grep["matches"][0]["line"] == 3

    def test_streaming_matches_full_read_numbering(self, tmp_path) -> None:
        f = tmp_path / "mixed.txt"
        f.write_bytes("あ\r\nい\rう\nえ".encode("cp932"))

        full = read_file(f, offset=0, limit=10)
        streamed = read_file(f, offset=0, limit=10, full_read_max_bytes=1)

        assert streamed["content"] == full["content"]
        assert streamed["total_lines"] == full["total_lines"] == 4

    def test_long_line_is_cut(self, tmp_path) -> None:
        f = tmp_path / "one_line.txt"
        f.write_text("x" * 50 + "\nnext\n", encoding="utf-8")

        lines = list(iter_text_lines(f, "utf-8", max_line_chars=10))

        assert lines == ["x" * 10 + "…", "next"]


def test_current_limits_reads_config(monkeypatch) -> None:
    class _Cfg:
        file_tools_search_time_limit_seconds = 5.0
        file_tools_grep_max_files = 7
        file_tools_grep_max_file_mb = 1.5
        file_tools_regex_timeout_seconds = 0.5
        file_tools_full_read_max_mb = 2.0

    monkeypatch.setattr(_file_tools_limits._state, "_LLM_CONFIG", _Cfg())

    limits = _file_tools_limits.current_limits()

    assert limits == FileToolsLimits(
        search_time_limit_seconds=5.0,
        grep_max_files=7,
        grep_max_file_bytes=int(1.5 * 1024 * 1024),
        regex_timeout_seconds=0.5,
        full_read_max_bytes=2 * 1024 * 1024,
    )
