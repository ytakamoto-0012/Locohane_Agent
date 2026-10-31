"""src/tools.py の _run_script_guard_env() の回帰テスト。

run_script はスキル作者が書いた既存の scripts/ 配下のファイルをそのまま
実行するため、execute_python_code のようにコード文字列の先頭へガードの
ソースを連結する方法が使えない。代わりに sitecustomize.py を一時
ディレクトリへ書き出し、PYTHONPATH の先頭に追加することで、対象
スクリプトのソースを一切変更せずに書き込み・削除系呼び出しへガードを
差し込んでいることを、実際にサブプロセスを起動して検証する
（sitecustomize はサブプロセスの解釈系起動時に自動 import されるため、
in-process呼び出しでは検証できない）。
"""

import os
import shutil
import subprocess
import sys

import pytest

from src import tools


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {"thread_id": "thread-1"}

    def get(self, key, default=None):
        return self._data.get(key, default)


@pytest.fixture(autouse=True)
def _base_env(tmp_path, monkeypatch):
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", None)
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_MAX_ENTRIES", 500)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", None)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    default_workdir = tmp_path / "default_workdir"
    default_workdir.mkdir()
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", default_workdir)


def _run_script(workdir, script_body: str, env: dict) -> subprocess.CompletedProcess:
    script = workdir / "script.py"
    script.write_text(script_body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def test_guard_dir_created_with_sitecustomize(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")

    assert guard_dir is not None
    assert (guard_dir / "sitecustomize.py").exists()
    assert str(guard_dir) in env["PYTHONPATH"].split(os.pathsep)
    shutil.rmtree(guard_dir, ignore_errors=True)


def test_subprocess_write_outside_workdir_is_blocked(tmp_path):
    workdir = tmp_path / "workdir"
    outside = tmp_path / "outside"
    workdir.mkdir()
    outside.mkdir()
    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")
    target = outside / "leaked.txt"

    try:
        result = _run_script(workdir, f'open(r"{target}", "w", encoding="utf-8").write("x")\n', env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    assert result.returncode != 0
    assert "[write sandbox guard]" in result.stderr
    assert not target.exists()


def test_subprocess_write_inside_workdir_succeeds(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")
    target = workdir / "out.txt"

    try:
        result = _run_script(workdir, f'open(r"{target}", "w", encoding="utf-8").write("ok")\n', env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "ok"


def test_subprocess_write_directly_inside_default_workdir_is_blocked(tmp_path):
    """default_workdirはサーバー側の共有ディレクトリのため、直下への書き込みは
    他セッションが誤参照する事故を防ぐため許可しない（_run_script_guard_env の
    allowed_roots には workdir と `_tmp_<thread_id>` のみが渡る）。
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")
    target = tools._state._DEFAULT_WORKDIR / "out.txt"

    try:
        result = _run_script(workdir, f'open(r"{target}", "w", encoding="utf-8").write("ok")\n', env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    assert result.returncode != 0
    assert "[write sandbox guard]" in result.stderr
    assert not target.exists()


def test_subprocess_write_inside_default_workdir_own_tmp_dir_succeeds(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    tmp_name = tools._workdir._exec_tmp_name()
    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")
    target = tools._state._DEFAULT_WORKDIR / f"_tmp_{tmp_name}" / "out.txt"

    try:
        result = _run_script(workdir, f'open(r"{target}", "w", encoding="utf-8").write("ok")\n', env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "ok"


def test_subprocess_write_inside_path_memory_dir_succeeds(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    path_memory_dir = tmp_path / "path_memory"
    path_memory_dir.mkdir()
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", path_memory_dir)
    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")
    target = path_memory_dir / "thread-1.json.lock"

    try:
        result = _run_script(workdir, f'open(r"{target}", "a+b").write(b"\\0")\n', env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    assert result.returncode == 0, result.stderr
    assert target.exists()


def test_subprocess_read_outside_workdir_is_permitted(tmp_path):
    workdir = tmp_path / "workdir"
    outside = tmp_path / "outside"
    workdir.mkdir()
    outside.mkdir()
    existing = outside / "template.txt"
    existing.write_text("hello", encoding="utf-8")
    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")

    try:
        result = _run_script(workdir, f'print(open(r"{existing}", "r", encoding="utf-8").read())\n', env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "hello"


def test_subprocess_read_foreign_tmp_dir_is_blocked(tmp_path):
    """_FakeUserSession の thread_id は "thread-1"（自セッション）。"""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    foreign = workdir / "_tmp_thread-2"
    foreign.mkdir()
    leaked = foreign / "leaked.txt"
    leaked.write_text("secret", encoding="utf-8")
    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")

    try:
        result = _run_script(workdir, f'open(r"{leaked}", "r", encoding="utf-8").read()\n', env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    assert result.returncode != 0
    assert "[temporary directory guard]" in result.stderr


def test_subprocess_write_inside_allowed_sandbox_dir_succeeds(tmp_path, monkeypatch):
    """allow_sandbox_dir に登録済みで、かつ実行スキル/スクリプトが
    allow_entries に一致する場合のみ書き込みが許可される。
    """
    from src.config import SandboxDirEntry

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    monkeypatch.setattr(
        tools._state,
        "_ALLOW_SANDBOX_DIRS",
        (SandboxDirEntry(dir=sandbox_dir, allow_entries=frozenset({("dummy-skill", "dummy.py")})),),
    )
    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")
    target = sandbox_dir / "out.txt"

    try:
        result = _run_script(workdir, f'open(r"{target}", "w", encoding="utf-8").write("ok")\n', env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "ok"


def test_subprocess_write_inside_sandbox_dir_blocked_for_unlisted_script(tmp_path, monkeypatch):
    """allow_sandbox_dir に登録済みでも、allow_entries に一致しないスキル/
    スクリプトからは書き込めない。
    """
    from src.config import SandboxDirEntry

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    monkeypatch.setattr(
        tools._state,
        "_ALLOW_SANDBOX_DIRS",
        (SandboxDirEntry(dir=sandbox_dir, allow_entries=frozenset({("dummy-skill", "dummy.py")})),),
    )
    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "other-skill", "other.py")
    target = sandbox_dir / "out.txt"

    try:
        result = _run_script(workdir, f'open(r"{target}", "w", encoding="utf-8").write("ok")\n', env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    assert result.returncode != 0
    assert "[write sandbox guard]" in result.stderr
    assert not target.exists()


def test_subprocess_read_own_tmp_dir_is_permitted(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    tmp_name = tools._workdir._exec_tmp_name()
    own = workdir / f"_tmp_{tmp_name}"
    own.mkdir()
    mine = own / "mine.txt"
    mine.write_text("mine", encoding="utf-8")
    env, guard_dir = tools._subprocess_env._run_script_guard_env(workdir, "dummy-skill", "dummy.py")

    try:
        result = _run_script(workdir, f'print(open(r"{mine}", "r", encoding="utf-8").read())\n', env)
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "mine"
