"""write_scratch_note と、打ち切り時の案内追記（_append_scratch_note_hint）の回帰テスト。

大量ファイル調査中にサブエージェント自身がトークン上限に達して打ち切られると、
書き残していない内容は失われ、委譲元には未整理のツール結果しか渡らない
（evals/tuning_log.md 2026-08-01 追記参照）。write_scratch_note で逐次書き残せば
打ち切られても内容が残り、委譲元はそのパスから続きを判断できることを固定化する。
"""

import importlib

import pytest

from src import tools

# tools.write_scratch_note は@toolオブジェクト（StructuredTool）で上書き済みのため、
# モジュール自体は importlib.import_module で sys.modules から直接取得する
# （import src.tools.write_scratch_note as x は src.tools.write_scratch_note 属性
# 経由の解決になり、同名の@toolオブジェクトを拾ってしまうため使えない）。
write_scratch_note_module = importlib.import_module("src.tools.write_scratch_note")
dispatch_agent_job_module = importlib.import_module("src.tools._dispatch_agent_job")


@pytest.fixture(autouse=True)
def _reset_subagent_run_id():
    """_SUBAGENT_RUN_ID は contextvar でテスト間をまたいで残るため、必ず戻す。

    このテストファイルの複数のテストが _SUBAGENT_RUN_ID.set() を呼ぶが、
    リセットを怠ると後続テストへ値が漏れて _scratch_notes_path() の判定が
    汚染される（実際に発生した回帰）。
    """
    token = tools._state._SUBAGENT_RUN_ID.set(None)
    yield
    tools._state._SUBAGENT_RUN_ID.reset(token)


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {"thread_id": "thread-1"}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def _setup(tmp_path, monkeypatch, run_id="run-1"):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    token = tools._state._SUBAGENT_RUN_ID.set(run_id)
    return workdir, token


class TestWriteScratchNote:
    def test_writes_and_appends(self, tmp_path, monkeypatch) -> None:
        _setup(tmp_path, monkeypatch)

        result1 = tools.write_scratch_note.func(content="1行目")
        assert "Written" in result1
        result2 = tools.write_scratch_note.func(content="2行目")

        path = write_scratch_note_module._scratch_notes_path()
        text = path.read_text(encoding="utf-8")
        assert text == "1行目\n2行目\n"
        assert str(path) in result2

    def test_empty_content_is_rejected(self, tmp_path, monkeypatch) -> None:
        _setup(tmp_path, monkeypatch)
        result = tools.write_scratch_note.func(content="   ")
        assert result.startswith("Error:")
        assert not write_scratch_note_module._scratch_notes_path().exists()

    def test_scoped_per_subagent_run_id(self, tmp_path, monkeypatch) -> None:
        workdir, token = _setup(tmp_path, monkeypatch, run_id="run-a")
        tools.write_scratch_note.func(content="from run-a")
        path_a = write_scratch_note_module._scratch_notes_path()

        tools._state._SUBAGENT_RUN_ID.reset(token)
        tools._state._SUBAGENT_RUN_ID.set("run-b")
        tools.write_scratch_note.func(content="from run-b")
        path_b = write_scratch_note_module._scratch_notes_path()

        assert path_a != path_b
        assert path_a.read_text(encoding="utf-8") == "from run-a\n"
        assert path_b.read_text(encoding="utf-8") == "from run-b\n"

    def test_no_run_id_uses_main_fallback(self, tmp_path, monkeypatch) -> None:
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)
        monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
        # _SUBAGENT_RUN_ID を明示的に設定しない（既定値 None のまま）。
        path = write_scratch_note_module._scratch_notes_path()
        assert "_main" in path.name


class TestAppendScratchNoteHint:
    def test_appends_hint_when_truncated_and_file_exists(self, tmp_path, monkeypatch) -> None:
        _setup(tmp_path, monkeypatch)
        tools.write_scratch_note.func(content="途中経過")
        truncated = "[Sub-agent: stopped because token usage reached the limit (64000 tokens)]"

        hinted = dispatch_agent_job_module._append_scratch_note_hint(truncated)

        assert hinted != truncated
        assert str(write_scratch_note_module._scratch_notes_path()) in hinted
        assert hinted.startswith(truncated)

    def test_no_hint_when_not_truncated(self, tmp_path, monkeypatch) -> None:
        _setup(tmp_path, monkeypatch)
        tools.write_scratch_note.func(content="途中経過")
        normal = "調査が完了しました。"

        assert dispatch_agent_job_module._append_scratch_note_hint(normal) == normal

    def test_no_hint_when_no_scratch_file_written(self, tmp_path, monkeypatch) -> None:
        _setup(tmp_path, monkeypatch)
        truncated = "[サブエージェント: 最大反復回数(30)に達したため打ち切りました]"

        assert dispatch_agent_job_module._append_scratch_note_hint(truncated) == truncated
