"""src/path_memory.py の類似検索（similarity / search_entries / recent_entries）の回帰テスト。

文字bigram＋コサイン類似度で、全角半角・大文字小文字・区切り文字の揺れや
1文字の誤字を吸収して登録済みパスを見つけられることを固定化する。
あわせて以下を固定化する:
- 完全一致は部分一致より上に並ぶ（`report.xlsx` で `old_report.xlsx` を上にしない）。
- フルパス検索で、同じフォルダの無関係なファイルが別フォルダの同名ファイルより
  上に来ない（フォルダ部分の共通だけで高得点にしない）。
- 現在存在しないパスは結果から除外し、登録は削除せず `valid` だけ更新する
  （削除すると `@N` の番号がずれるため）。
"""

import json
from pathlib import Path

import pytest

from src import path_memory


@pytest.fixture
def registry(tmp_path):
    pm_dir = tmp_path / "pm"

    def _register(*paths: Path) -> Path:
        for p in paths:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
            path_memory.register("t1", str(p), pm_dir, 500)
        return pm_dir

    return _register


class TestSimilarity:
    def test_exact_match_is_full_score(self) -> None:
        assert path_memory.similarity("REPORT.XLSX", r"C:\data\report.xlsx") == 1.0
        assert path_memory.similarity("C:/data/report.xlsx", r"C:\data\report.xlsx") == 1.0

    def test_substring_ranks_below_exact_match(self) -> None:
        exact = path_memory.similarity("report.xlsx", r"C:\data\report.xlsx")
        partial = path_memory.similarity("report.xlsx", r"C:\data\old_report.xlsx")
        assert exact == 1.0
        assert 0.8 <= partial < 1.0

    def test_absorbs_width_difference(self) -> None:
        assert path_memory.similarity("ﾎｳｺｸ.txt", r"C:\data\ホウコク.txt") == 1.0

    def test_one_char_typo_scores_higher_than_unrelated(self) -> None:
        typo = path_memory.similarity("月次報告署.xlsx", r"C:\data\月次報告書.xlsx")
        unrelated = path_memory.similarity("月次報告署.xlsx", r"C:\data\notes.txt")
        assert typo >= 0.5
        assert unrelated < 0.3

    def test_full_path_query_prefers_same_name_in_other_folder_over_unrelated_sibling(self) -> None:
        query = r"C:\Users\taro\projects\sales\data\売上集計_2024.xlsx"
        sibling = path_memory.similarity(query, r"C:\Users\taro\projects\sales\data\readme.txt")
        same_name = path_memory.similarity(query, r"D:\other\売上集計_2024.xlsx")
        assert same_name > sibling
        assert sibling < 0.5

    def test_filename_weight_changes_score_for_full_path_query(self) -> None:
        query = r"C:\data\report.xlsx"
        path = r"D:\archive\report.xlsx"
        low = path_memory.similarity(query, path, filename_weight=0.0)
        high = path_memory.similarity(query, path, filename_weight=1.0)
        assert high == pytest.approx(1.0)
        assert low < high

    def test_name_only_query_matches_folder_segment(self) -> None:
        assert path_memory.similarity("月次報告署", r"C:\projects\月次報告書\data.xlsx") >= 0.3

    def test_empty_query_is_zero(self) -> None:
        assert path_memory.similarity("", r"C:\data\a.txt") == 0.0


class TestDifferenceLabel:
    def test_folder_differs(self) -> None:
        assert path_memory.difference_label(r"C:\data\a.txt", r"C:\old\a.txt") == "different folder"

    def test_name_differs(self) -> None:
        assert path_memory.difference_label(r"C:\data\a.txt", r"C:\data\b.txt") == "different file name"

    def test_both_differ(self) -> None:
        assert path_memory.difference_label(r"C:\data\a.txt", r"C:\old\b.txt") == "different folder and file name"


class TestSearchEntries:
    def test_ranks_by_similarity_and_excludes_below_threshold(self, registry, tmp_path) -> None:
        d = tmp_path / "data"
        pm_dir = registry(d / "notes.txt", d / "月次報告書.xlsx", d / "週次報告.xlsx")

        hits = path_memory.search_entries("t1", "月次報告署.xlsx", pm_dir)

        assert hits[0]["index"] == 2
        assert all(h["index"] != 1 for h in hits)
        assert all(0.3 <= h["score"] <= 1.0 for h in hits)

    def test_exact_match_ranks_above_earlier_registered_partial_match(self, registry, tmp_path) -> None:
        d = tmp_path / "data"
        pm_dir = registry(d / "old_report.xlsx", d / "report.xlsx")

        hits = path_memory.search_entries("t1", "report.xlsx", pm_dir, top_k=1)

        assert hits[0]["index"] == 2

    def test_top_k_limits_results(self, registry, tmp_path) -> None:
        pm_dir = registry(*[tmp_path / "data" / f"report_{i}.txt" for i in range(10)])

        hits = path_memory.search_entries("t1", "report", pm_dir, top_k=3)

        assert len(hits) == 3

    def test_missing_path_is_excluded_and_marked_invalid_without_deleting(self, registry, tmp_path) -> None:
        d = tmp_path / "data"
        gone = d / "report_old.xlsx"
        kept = d / "report_new.xlsx"
        pm_dir = registry(gone, kept)
        gone.unlink()

        hits = path_memory.search_entries("t1", "report", pm_dir)

        assert [h["index"] for h in hits] == [2]
        entries = path_memory.list_entries("t1", pm_dir)
        assert [e["path"] for e in entries] == [str(gone), str(kept)]
        assert [e["valid"] for e in entries] == [False, True]
        assert path_memory.resolve("t1", "@2", pm_dir) == str(kept)

    def test_recreated_path_is_found_again(self, registry, tmp_path) -> None:
        f = tmp_path / "data" / "report.xlsx"
        pm_dir = registry(f)
        f.unlink()
        assert path_memory.search_entries("t1", "report", pm_dir) == []

        f.write_text("x", encoding="utf-8")

        assert [h["index"] for h in path_memory.search_entries("t1", "report", pm_dir)] == [1]
        assert path_memory.list_entries("t1", pm_dir)[0]["valid"] is True

    def test_no_registry_returns_empty(self, tmp_path) -> None:
        assert path_memory.search_entries("t1", "x", tmp_path / "none") == []


class TestRecentEntries:
    def test_newest_first_excluding_missing(self, registry, tmp_path) -> None:
        d = tmp_path / "data"
        pm_dir = registry(d / "a.txt", d / "b.txt", d / "c.txt")
        (d / "c.txt").unlink()

        hits = path_memory.recent_entries("t1", pm_dir, top_k=5)

        assert [h["index"] for h in hits] == [2, 1]
        registry_json = json.loads((pm_dir / "t1.json").read_text(encoding="utf-8"))
        assert len(registry_json) == 3
