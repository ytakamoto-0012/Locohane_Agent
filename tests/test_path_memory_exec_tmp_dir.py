"""src/path_memory.py の exec_tmp_dir() が常に AGENT_DEFAULT_WORKDIR 基準で
`_tmp_<thread_id>/` を作ることの回帰テスト。

以前は Path.cwd()（= run_script の cwd。ユーザー指定 work_dir になりうる）を
基準にしていたため、work_dir を設定したセッションでは work_dir 配下に
`_tmp_<thread_id>/` が作られてしまい、config.ini [default_workdir].retention_days
の保持日数ベース自動削除（default_workdir のみが対象）が効かず消えずに溜まり
続ける回帰があった。cwd がどこであっても default_workdir 配下に固定されることを
検証する。
"""

import os

from src import path_memory


def test_exec_tmp_dir_uses_default_workdir_env_not_cwd(tmp_path, monkeypatch):
    default_workdir = tmp_path / "default_workdir"
    default_workdir.mkdir()
    work_dir = tmp_path / "user_work_dir"
    work_dir.mkdir()

    monkeypatch.setenv("AGENT_DEFAULT_WORKDIR", str(default_workdir))
    monkeypatch.setenv("AGENT_THREAD_ID", "thread-1")
    monkeypatch.chdir(work_dir)

    out_dir = path_memory.exec_tmp_dir("pdf_pages")

    assert out_dir == default_workdir / "_tmp_thread-1" / "pdf_pages"
    assert out_dir.is_dir()
    assert not (work_dir / "_tmp_thread-1").exists()


def test_exec_tmp_dir_without_category(tmp_path, monkeypatch):
    default_workdir = tmp_path / "default_workdir"
    default_workdir.mkdir()

    monkeypatch.setenv("AGENT_DEFAULT_WORKDIR", str(default_workdir))
    monkeypatch.setenv("AGENT_THREAD_ID", "thread-2")

    out_dir = path_memory.exec_tmp_dir()

    assert out_dir == default_workdir / "_tmp_thread-2"
    assert out_dir.is_dir()


def test_exec_tmp_dir_falls_back_when_thread_id_missing(tmp_path, monkeypatch):
    default_workdir = tmp_path / "default_workdir"
    default_workdir.mkdir()

    monkeypatch.setenv("AGENT_DEFAULT_WORKDIR", str(default_workdir))
    monkeypatch.delenv("AGENT_THREAD_ID", raising=False)

    out_dir = path_memory.exec_tmp_dir()

    assert out_dir == default_workdir / "_tmp__no_session"
