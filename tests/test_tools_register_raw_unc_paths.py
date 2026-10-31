"""register_raw_unc_paths_in_text の回帰テスト。

低パラメータモデルはツール呼び出しのJSON argsにUNCパスを書き起こす際に
バックスラッシュのエスケープを誤りやすい（ISSUE-002）。ユーザーがチャット
本文に直接書いた生のUNCパスを、LLMに渡す前に path_memory へ事前登録し
`@N` へ置換する挙動を固定化する。
"""

import json
from pathlib import Path

import pytest

from src import tools


@pytest.fixture
def path_memory_env(tmp_path, monkeypatch):
    path_memory_dir = tmp_path / "path_memory_data"

    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", path_memory_dir)
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_MAX_ENTRIES", 500)

    class _FakeUserSession:
        def get(self, key):
            return "thread-1" if key == "thread_id" else None

    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())

    return path_memory_dir


def _registry_entries(path_memory_dir: Path) -> list[dict]:
    registry_file = path_memory_dir / "thread-1.json"
    return json.loads(registry_file.read_text(encoding="utf-8"))


def test_registers_unc_path_and_replaces_with_token(path_memory_env) -> None:
    text = r"\\cadstr0\DT_CAE\Datas\model.prt を確認して"

    result = tools.register_raw_unc_paths_in_text(text)

    assert result == "@1 を確認して"
    entries = _registry_entries(path_memory_env)
    assert entries[0]["path"] == r"\\cadstr0\DT_CAE\Datas\model.prt"
    assert entries[0]["description"] == "user input"


def test_same_path_reuses_same_token(path_memory_env) -> None:
    text = (
        r"\\cadstr0\DT_CAE\Datas\model.prt を開いて、"
        r"\\cadstr0\DT_CAE\Datas\model.prt も確認して"
    )

    result = tools.register_raw_unc_paths_in_text(text)

    assert result == "@1 を開いて、@1 も確認して"
    assert len(_registry_entries(path_memory_env)) == 1


def test_plain_japanese_text_is_unchanged(path_memory_env) -> None:
    text = "今日の予定を教えて"

    result = tools.register_raw_unc_paths_in_text(text)

    assert result == text
    assert not (path_memory_env / "thread-1.json").exists()


def test_trailing_japanese_is_not_captured_into_path(path_memory_env) -> None:
    text = r"\\cadstr0\share\file.xlsxを開いて"

    result = tools.register_raw_unc_paths_in_text(text)

    assert result == "@1を開いて"
    entries = _registry_entries(path_memory_env)
    assert entries[0]["path"] == r"\\cadstr0\share\file.xlsx"


def test_returns_text_unchanged_when_path_memory_dir_unset(monkeypatch) -> None:
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", None)

    text = r"\\cadstr0\DT_CAE\Datas\model.prt を確認して"

    result = tools.register_raw_unc_paths_in_text(text)

    assert result == text
