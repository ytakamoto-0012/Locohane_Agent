"""書き込みガード（src/tools/_python_fs_guard.py）の、キーワード引数・低水準 API・pip 起動の扱いのテスト。

- open(p, mode="w")・open(file=p) が TypeError にならない（以前は位置引数しか想定しておらず、必ず失敗した）。
- os.open の書き込み系フラグ・rename/copy/move の dst= でも書き込み先を確かめる（以前は素通しだった）。
- tempfile は書き込み先（このセッションの一時フォルダ）に作られ、NamedTemporaryFile が使える。
- python -m pip は git/pip と同じく止める（先頭の名前しか見ていなかったため止まらなかった）。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from src.tools._python_fs_guard import _python_fs_guard_preamble


def _run(code: str, allowed) -> subprocess.CompletedProcess:
    preamble = _python_fs_guard_preamble([allowed], deny_roots=[], write_deny_roots=[], list_deny_roots=[])
    return subprocess.run(
        [sys.executable, "-c", preamble + code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


@pytest.fixture
def dirs(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    return allowed, tmp_path / "outside.txt"


@pytest.mark.parametrize(
    "statement",
    [
        "open(A / 'a.txt', 'w').close()",
        "open(A / 'b.txt', mode='w', encoding='utf-8').close()",
        "open(A / 'c.txt', 'w').close(); open(file=A / 'c.txt').close()",
        "open(A / 'd.txt', encoding='utf-8', mode='a').close()",
        "os.close(os.open(A / 'e.txt', os.O_WRONLY | os.O_CREAT))",
        "open(A / 'f.txt', 'w').close(); os.rename(A / 'f.txt', dst=A / 'g.txt')",
        "open(A / 'h.txt', 'w').close(); shutil.copy(A / 'h.txt', dst=A / 'i.txt')",
        "f = tempfile.NamedTemporaryFile(); f.write(b'x'); f.close()",
        "subprocess.run([sys.executable, '-c', 'pass'], check=True)",
    ],
)
def test_allowed_operations(dirs, statement):
    allowed, _ = dirs
    result = _run(f"import os, shutil, subprocess, sys, tempfile, pathlib\nA = pathlib.Path(r'{allowed}')\n{statement}\n", allowed)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "statement",
    [
        "open(OUT, mode='w')",
        "open(file=OUT, mode='a')",
        "os.open(OUT, os.O_WRONLY | os.O_CREAT)",
        "open(A / 'x.txt', 'w').close(); os.rename(A / 'x.txt', dst=OUT)",
        "open(A / 'y.txt', 'w').close(); shutil.copy(A / 'y.txt', dst=OUT)",
        "open(A / 'z.txt', 'w').close(); shutil.move(A / 'z.txt', dst=OUT)",
    ],
)
def test_writes_outside_are_blocked(dirs, statement):
    allowed, outside = dirs
    code = f"import os, shutil, pathlib\nA = pathlib.Path(r'{allowed}')\nOUT = r'{outside}'\n{statement}\n"
    result = _run(code, allowed)
    assert result.returncode != 0 and "write sandbox guard" in result.stderr, result.stderr
    assert not outside.exists()


@pytest.mark.parametrize(
    "command",
    ["[sys.executable, '-m', 'pip', '--version']", "'python -m pip --version'", "['py', '-mpip', 'list']"],
)
def test_python_dash_m_pip_is_blocked(dirs, command):
    allowed, _ = dirs
    result = _run(f"import subprocess, sys\nsubprocess.run({command}, capture_output=True)\n", allowed)
    assert result.returncode != 0 and "Running git/npm/pip commands is forbidden" in result.stderr, result.stderr


def test_tempfile_goes_to_the_allowed_folder(dirs):
    allowed, _ = dirs
    result = _run("import tempfile\nprint(tempfile.gettempdir())\n", allowed)
    assert result.returncode == 0, result.stderr
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(allowed)
