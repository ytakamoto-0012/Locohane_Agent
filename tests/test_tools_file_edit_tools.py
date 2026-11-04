"""Write / Edit ツール（src/tools/file_edit_tools.py）のテスト。

- 計画承認が無いと書けない
- 相対パスは execute_python_code の cwd（このセッションの `_tmp_`）基準
- 作業ディレクトリ・`_tmp_` の外へは書けない
- Write は既存ファイルを overwrite なしでは上書きしない
- Edit は一意な一致だけを置き換え、文字コードと改行を保ち、変更箇所を返す
- サブエージェント（worker）専用で、メインの組み込みツールには入らない
"""

import json
from pathlib import Path

import pytest

from src import tools
from src.tools import registry


class _FakeUserSession:
    def __init__(self, approved: bool = True):
        self._data: dict = {"thread_id": "thread-1", "plan_approved": approved}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


@pytest.fixture
def env(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", tmp_path / "pm")
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_MAX_ENTRIES", 500)
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", None)
    session = _FakeUserSession()
    monkeypatch.setattr(tools.cl, "user_session", session)
    return session


def _write(**kwargs) -> str:
    return tools.write_tool.func(**kwargs)


def _edit(**kwargs) -> str:
    return tools.edit_tool.func(**kwargs)


def test_tools_are_subagent_only():
    names_sub = {t.name for t in registry._SUBAGENT_TOOLS}
    names_main = {t.name for t in registry._BASE_TOOLS}
    assert {"Write", "Edit"} <= names_sub
    assert not {"Write", "Edit"} & names_main


def test_write_needs_approved_plan(env):
    env.set("plan_approved", False)
    assert "plan is not approved" in _write(file_path="a.py", content="x = 1\n")
    assert "plan is not approved" in _edit(file_path="a.py", old_string="1", new_string="2")


def test_write_relative_path_goes_to_exec_tmp_and_registers(env):
    result = json.loads(_write(file_path="build.py", content="print('hi')\nprint('x')\n"))
    expected = tools._workdir._resolve_exec_workdir() / "build.py"
    assert result["path"] == str(expected.resolve())
    assert result["lines"] == 2
    assert result["path_memory"]
    assert expected.read_text(encoding="utf-8") == "print('hi')\nprint('x')\n"


def test_write_refuses_existing_without_overwrite(env):
    _write(file_path="a.txt", content="one\n")
    assert "already exists" in _write(file_path="a.txt", content="two\n")
    json.loads(_write(file_path="a.txt", content="two\n", overwrite=True))
    assert (tools._workdir._resolve_exec_workdir() / "a.txt").read_text(encoding="utf-8") == "two\n"


def test_write_outside_allowed_roots_is_refused(env, tmp_path):
    outside = tmp_path / "elsewhere" / "x.txt"
    result = _write(file_path=str(outside), content="x")
    assert result.startswith("Error:") and "only allowed" in result
    assert not outside.exists()


def test_edit_replaces_unique_match_and_returns_changed_lines(env):
    _write(file_path="b.py", content="a = 1\nb = round(2.5)\nc = 3\n")
    result = json.loads(_edit(file_path="b.py", old_string="b = round(2.5)", new_string="b = half_up(2.5)"))
    assert result["replacements"] == 1
    assert "2\tb = half_up(2.5)" in result["changed"]
    assert "1\ta = 1" in result["changed"]
    assert (tools._workdir._resolve_exec_workdir() / "b.py").read_text(encoding="utf-8") == "a = 1\nb = half_up(2.5)\nc = 3\n"


def test_edit_ambiguous_and_missing_and_replace_all(env):
    _write(file_path="c.py", content="x = 1\ny = 1\n")
    assert "appears 2 times" in _edit(file_path="c.py", old_string="= 1", new_string="= 2")
    missing = _edit(file_path="c.py", old_string="x =  1", new_string="x = 9")
    assert "not found" in missing and "closest line is 1" in missing
    result = json.loads(_edit(file_path="c.py", old_string="= 1", new_string="= 2", replace_all=True))
    assert result["replacements"] == 2
    assert (tools._workdir._resolve_exec_workdir() / "c.py").read_text(encoding="utf-8") == "x = 2\ny = 2\n"


def test_edit_keeps_crlf_and_cp932(env):
    target = tools._workdir._resolve_exec_workdir() / "s.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes("一行目\r\n二行目\r\n".encode("cp932"))
    json.loads(_edit(file_path=str(target), old_string="二行目", new_string="2行目"))
    assert target.read_bytes() == "一行目\r\n2行目\r\n".encode("cp932")


def test_edit_refuses_binary(env):
    target = tools._workdir._resolve_exec_workdir() / "b.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"PK\x00\x01abc")
    assert "binary" in _edit(file_path=str(target), old_string="abc", new_string="d")
