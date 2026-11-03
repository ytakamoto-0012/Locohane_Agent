"""祖先フォルダから検索したとき、自セッションの `_tmp_<own>` を除外することの回帰テスト。

`_tmp_<own>` は初回作成時に default_workdir 直下の中身がまるごとコピーされる
（_seed_exec_workdir）。以前は他セッションの `_tmp_*` だけを除外していたため、
default_workdir 本体から `**/*.png` で探すと元のファイルとそのコピーの両方が一致し、
dispatch_agent_batch が同じ写真を2回ずつ別グループへ配っていた
（2026-11-03 evals system_prompt_scale/001: 写真40枚が80枚になり、読み取りの
食い違いの確認に約15分余計にかかった）。
"""

import sys
from pathlib import Path

import pytest

from src import tools
from src.tools.glob_tool import glob_search
from src.tools.grep_tool import grep_search

_WORKDIR_MODULE = sys.modules["src.tools._workdir"]
_BATCH_MODULE = sys.modules["src.tools.dispatch_agent_batch"]


@pytest.fixture()
def seeded(monkeypatch, tmp_path) -> tuple[Path, Path]:
    """default_workdir に 2021/photo.png を置き、自セッションの _tmp_ を作ってシードする。"""
    default_workdir = tmp_path / "default_workdir"
    (default_workdir / "2021").mkdir(parents=True)
    (default_workdir / "2021" / "photo.png").write_bytes(b"x")
    (default_workdir / "2021" / "note.md").write_text("クリスマス会", encoding="utf-8")
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", default_workdir)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", object())
    monkeypatch.setattr(_WORKDIR_MODULE, "_exec_tmp_name", lambda: "own")
    own_tmp = _WORKDIR_MODULE._resolve_exec_workdir()
    assert (own_tmp / "2021" / "photo.png").is_file()  # シードでコピーされている前提
    (default_workdir / "_tmp_other").mkdir()
    return default_workdir, own_tmp


def test_excludes_own_tmp_when_searching_from_ancestor(seeded) -> None:
    default_workdir, _ = seeded
    assert _WORKDIR_MODULE._search_excluded_tmp_dir_names(default_workdir) == frozenset({"_tmp_own", "_tmp_other"})


def test_keeps_own_tmp_when_searching_inside_it(seeded) -> None:
    _, own_tmp = seeded
    assert _WORKDIR_MODULE._search_excluded_tmp_dir_names(own_tmp) == frozenset({"_tmp_other"})
    assert _WORKDIR_MODULE._search_excluded_tmp_dir_names(own_tmp / "2021") == frozenset({"_tmp_other"})


def test_batch_lists_each_file_once_from_ancestor(seeded) -> None:
    default_workdir, own_tmp = seeded
    assert _BATCH_MODULE._list_target_files(default_workdir, "**/*.png") == [default_workdir / "2021" / "photo.png"]
    assert _BATCH_MODULE._list_target_files(own_tmp, "**/*.png") == [own_tmp / "2021" / "photo.png"]


def test_glob_and_grep_skip_own_tmp_copy_from_ancestor(seeded) -> None:
    default_workdir, own_tmp = seeded
    excluded = _WORKDIR_MODULE._search_excluded_tmp_dir_names(default_workdir)
    # Glob は base 直下のみを見る。祖先から見たフォルダ一覧に自分の _tmp_ が出ないこと。
    globbed = glob_search(default_workdir, "*", exclude_names=excluded)
    assert [Path(d["path"]).name for d in globbed["directories"]] == ["2021"]
    grepped = grep_search(default_workdir, "クリスマス", output_mode="content", exclude_names=excluded)
    assert "_tmp_own" not in str(grepped)
    assert "note.md" in str(grepped)
    # 自分の _tmp_ の中から探すときは、その中身が見えること。
    inside = glob_search(own_tmp / "2021", "*.png", exclude_names=_WORKDIR_MODULE._search_excluded_tmp_dir_names(own_tmp / "2021"))
    assert len(inside["files"]) == 1


def test_batch_dedupes_files_with_identical_content(tmp_path) -> None:
    a = tmp_path / "a" / "photo.png"
    b = tmp_path / "backup" / "photo.png"
    c = tmp_path / "a" / "other.png"
    for p, data in ((a, b"same"), (b, b"same"), (c, b"diff")):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    kept, duplicates = _BATCH_MODULE._dedupe_identical_files([a, b, c])
    assert kept == [a, c]
    assert duplicates == [(b, a)]
