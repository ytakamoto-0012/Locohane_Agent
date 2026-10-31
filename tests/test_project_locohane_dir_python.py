"""config.ini [paths].project_locohane_dir の {"dir": ..., "python": ...} 形式で、
ディレクトリごとに専用の Python 環境を指定できることの回帰テスト（2026-09-30追加）。
"""

import importlib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src import tools
from src.config import _parse_project_locohane_dirs

_script_job = importlib.import_module("src.tools._script_job")


@dataclass
class _FakeConfig:
    locohane_skills_pythons: dict = field(default_factory=dict)


def test_parse_plain_string_is_backward_compatible(tmp_path):
    assert _parse_project_locohane_dirs("./.locohane", tmp_path) == [((tmp_path / ".locohane").resolve(), None)]


def test_parse_mixed_list(tmp_path):
    value = """[
        "./a",
        {"dir": "./b", "python": "./b/.venv/Scripts/python.exe"},
        {"dir": "./c", "python": "python3"},
        {"dir": "./d"},
        ]"""
    assert _parse_project_locohane_dirs(value, tmp_path) == [
        ((tmp_path / "a").resolve(), None),
        ((tmp_path / "b").resolve(), str((tmp_path / "b/.venv/Scripts/python.exe").resolve())),
        ((tmp_path / "c").resolve(), "python3"),
        ((tmp_path / "d").resolve(), None),
    ]


@pytest.mark.parametrize("value", ['[123]', '[{"python": "python"}]', '[{"dir": "./a", "python": 1}]'])
def test_parse_invalid_raises(tmp_path, value):
    with pytest.raises(ValueError):
        _parse_project_locohane_dirs(value, tmp_path)


def test_script_python_for_uses_dedicated_python(tmp_path, monkeypatch):
    team_skills = tmp_path / ".locohane_team" / "skills"
    other_skills = tmp_path / "skills"
    monkeypatch.setattr(tools._state, "_SCRIPT_PYTHON", "default-python")
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", _FakeConfig({team_skills: "team-python"}))

    assert _script_job._script_python_for((team_skills / "s" / "scripts" / "x.py").resolve()) == "team-python"
    assert _script_job._script_python_for((other_skills / "s" / "scripts" / "x.py").resolve()) == "default-python"


def test_script_python_for_without_config(monkeypatch):
    monkeypatch.setattr(tools._state, "_SCRIPT_PYTHON", "default-python")
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", None)

    assert _script_job._script_python_for(Path("C:/x/scripts/x.py")) == "default-python"
