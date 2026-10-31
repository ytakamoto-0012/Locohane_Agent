"""read_tool/glob_tool/grep_tool/json_query の純粋ロジック部分の回帰テスト。

read_file/glob_search/grep_search/query_json は各対象ファイル
（src/tools/read_tool.py 等）に直接実装されており、Chainlit・パスメモリー・
作業ディレクトリ解決には依存しないため、tmp_path のみで完結するテストを書く。
"""

import pytest

from src.tools.read_tool import read_file
from src.tools.glob_tool import glob_search
from src.tools.grep_tool import grep_search
from src.tools.json_query import query_json


class TestReadFile:
    def test_reads_with_offset_and_limit(self, tmp_path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("\n".join(f"line{i}" for i in range(1, 21)), encoding="utf-8")

        result = read_file(target, offset=5, limit=3)

        assert result["total_lines"] == 20
        assert result["start_line"] == 6
        assert result["end_line"] == 8
        assert result["content"] == "6\tline6\n7\tline7\n8\tline8"

    def test_offset_past_eof_returns_empty_content(self, tmp_path) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("only one line", encoding="utf-8")

        result = read_file(target, offset=100, limit=10)

        assert result["start_line"] is None
        assert result["end_line"] is None
        assert result["content"] == ""

    def test_cp932_fallback(self, tmp_path) -> None:
        target = tmp_path / "sjis.txt"
        target.write_bytes("日本語".encode("cp932"))

        result = read_file(target)

        assert "日本語" in result["content"]

    def test_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="not found"):
            read_file(tmp_path / "nope.txt")

    def test_directory_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="directory"):
            read_file(tmp_path)

    def test_binary_raises(self, tmp_path) -> None:
        target = tmp_path / "bin.dat"
        target.write_bytes(b"\x00\x01\x02")

        with pytest.raises(ValueError, match="binary"):
            read_file(target)


class TestGlobSearch:
    def test_head_limit_applies_independently_to_files_and_dirs(self, tmp_path) -> None:
        (tmp_path / "a.py").write_text("a", encoding="utf-8")
        (tmp_path / "b.py").write_text("b", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.py").write_text("c", encoding="utf-8")

        result = glob_search(tmp_path, "*.py", head_limit=1)

        assert result["total_matches"] == 2
        assert result["returned"] == 1
        assert result["truncated"] is True
        assert len(result["files"]) == 1

    def test_file_details_and_directories(self, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("line1\nline2\n", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested.py").write_text("x", encoding="utf-8")

        result = glob_search(tmp_path, "**/*", head_limit=200)

        detail = next(d for d in result["file_details"] if d["path"] == str(f.resolve()))
        assert detail["binary"] is False
        assert detail["total_lines"] == 2

        directory = next(d for d in result["directories"] if d["path"] == str((tmp_path / "sub").resolve()))
        assert directory["file_count"] == 1

    def test_missing_base_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="not found"):
            glob_search(tmp_path / "nope", "*.py")

    def test_undecodable_file_does_not_abort_search(self, tmp_path) -> None:
        (tmp_path / "a.py").write_text("line1\n", encoding="utf-8")
        (tmp_path / "bad.py").write_bytes(b"\x81\xff not decodable as utf-8 or cp932")

        result = glob_search(tmp_path, "*.py")

        assert result["total_matches"] == 2
        detail = next(d for d in result["file_details"] if d["path"] == str((tmp_path / "bad.py").resolve()))
        assert detail == {"path": str((tmp_path / "bad.py").resolve()), "binary": False, "total_lines": None}

    def test_base_not_a_directory_raises(self, tmp_path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")

        with pytest.raises(ValueError, match="is not a directory"):
            glob_search(f, "*.py")

    def test_brace_alternation_expands_to_union(self, tmp_path) -> None:
        (tmp_path / "a.jpg").write_text("a", encoding="utf-8")
        (tmp_path / "b.png").write_text("b", encoding="utf-8")
        (tmp_path / "c.txt").write_text("c", encoding="utf-8")

        result = glob_search(tmp_path, "*.{jpg,png,gif}")

        assert result["total_matches"] == 2
        assert str((tmp_path / "a.jpg").resolve()) in result["files"]
        assert str((tmp_path / "b.png").resolve()) in result["files"]
        assert not any("c.txt" in p for p in result["files"])

    def test_exclude_names_prunes_results_and_counts(self, tmp_path) -> None:
        (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
        excluded = tmp_path / "_tmp_other"
        excluded.mkdir()
        (excluded / "leaked.txt").write_text("secret", encoding="utf-8")

        result = glob_search(tmp_path, "**/*", exclude_names=frozenset({"_tmp_other"}))

        assert str((tmp_path / "notes.txt").resolve()) in result["files"]
        assert not any("leaked.txt" in p for p in result["files"])
        assert not any(d["path"] == str(excluded.resolve()) for d in result["directories"])
        assert result["base_contents"]["file_count"] == 1
        assert result["base_contents"]["directory_count"] == 0


class TestGrepSearch:
    def test_files_with_matches_mode(self, tmp_path) -> None:
        (tmp_path / "a.py").write_text("TODO: fix\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("nothing here\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", glob="*.py", output_mode="files_with_matches")

        assert result["matched"] is True
        assert result["total_files"] == 1

    def test_content_mode_with_context(self, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("before\nTODO: fix\nafter\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", output_mode="content", context=1)

        lines = [m["line"] for m in result["matches"]]
        assert lines == [1, 2, 3]

    def test_count_mode(self, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("TODO\nTODO\nother\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", output_mode="count")

        assert result["counts"][0]["count"] == 2

    def test_case_insensitive(self, tmp_path) -> None:
        f = tmp_path / "a.py"
        f.write_text("todo\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO", case_insensitive=True)

        assert result["matched"] is True

    def test_no_match_returns_matched_false(self, tmp_path) -> None:
        (tmp_path / "a.py").write_text("nothing\n", encoding="utf-8")

        result = grep_search(tmp_path, "TODO")

        assert result == {"matched": False, "files": [], "matches": [], "counts": []}

    def test_missing_base_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="not found"):
            grep_search(tmp_path / "nope", "TODO")

    def test_undecodable_file_is_skipped_not_fatal(self, tmp_path) -> None:
        (tmp_path / "a.py").write_text("TODO: fix\n", encoding="utf-8")
        (tmp_path / "bad.py").write_bytes(b"\x81\xff TODO but not decodable as utf-8 or cp932")

        result = grep_search(tmp_path, "TODO", glob="*.py", output_mode="files_with_matches")

        assert result["matched"] is True
        assert result["files"] == [str((tmp_path / "a.py").resolve())]

    def test_invalid_regex_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="regular expression"):
            grep_search(tmp_path, "(unterminated")

    def test_invalid_output_mode_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="output_mode"):
            grep_search(tmp_path, "TODO", output_mode="bogus")

    def test_exclude_names_prunes_matches(self, tmp_path) -> None:
        (tmp_path / "a.py").write_text("TODO: visible\n", encoding="utf-8")
        excluded = tmp_path / "_tmp_other"
        excluded.mkdir()
        (excluded / "b.py").write_text("TODO: hidden\n", encoding="utf-8")

        result = grep_search(
            tmp_path, "TODO", output_mode="files_with_matches", exclude_names=frozenset({"_tmp_other"})
        )

        assert result["matched"] is True
        assert result["files"] == [str((tmp_path / "a.py").resolve())]


class TestQueryJson:
    def test_query_from_file(self, tmp_path) -> None:
        f = tmp_path / "data.json"
        f.write_text('{"a": {"b": 1}}', encoding="utf-8")

        result = query_json("a.b", file_path=f)

        assert result == {"result": 1}

    def test_query_from_text(self) -> None:
        result = query_json("a.b", json_text='{"a": {"b": 2}}')

        assert result == {"result": 2}

    def test_both_specified_raises(self, tmp_path) -> None:
        f = tmp_path / "data.json"
        f.write_text("{}", encoding="utf-8")

        with pytest.raises(ValueError, match="cannot be specified together"):
            query_json("a", file_path=f, json_text="{}")

    def test_neither_specified_raises(self) -> None:
        with pytest.raises(ValueError, match="Specify either"):
            query_json("a")

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError, match="JSON"):
            query_json("a", json_text="{not json")

    def test_invalid_query_raises(self) -> None:
        with pytest.raises(ValueError, match="JMESPath"):
            query_json("a[?", json_text="{}")

    def test_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="not found"):
            query_json("a", file_path=tmp_path / "nope.json")
