"""src/tools/_workdir.py の回帰テスト。

2026-08-29に発見した以下のバグの修正を固定化する:
1. `_resolve_workdir()` が work_dir未設定時に default_workdir 自体（全スレッド
   共通の共有フォルダ）を返しており、読み取り側（Glob/Read/Grep等）が
   無関係な別スレッドのデータを拾ってしまう事故が起こりえた（書き込み側は
   `_restrict_default_workdir()` により既に隔離されていた）。
2. `check_work_dir_status` ツールと `app.py` の `_build_work_dir_notice()` が
   それぞれ独自に重複したロジックを実装しており、食い違った（かつ両方とも
   `state` の意味が矛盾する誤った）情報をLLMへ伝えていた。

`evals/tuning_log.md` iter55 参照。
"""

from __future__ import annotations

import importlib

import pytest

from src import tools
from src.tools import _workdir

# tools.check_work_dir_status は@toolオブジェクト（StructuredTool）で上書き
# 済みのため、WorkDirAccessStatus等の型定義を使うにはモジュール自体を
# importlib.import_module で sys.modules から直接取得する
# （tests/test_tools_write_scratch_note.py と同じ理由）。
check_work_dir_status_module = importlib.import_module("src.tools.check_work_dir_status")


class _FakeUserSession:
    def __init__(self, data: dict | None = None):
        self._data: dict = {"thread_id": "thread-1", **(data or {})}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


@pytest.fixture(autouse=True)
def _isolate_exec_tmp_name_cache(monkeypatch):
    """_EXEC_TMP_NAME_CACHE はプロセスグローバルなためテスト間で共有される。

    テストごとに新しい辞書へ差し替え、他テストのキャッシュ値が漏れて
    「初回作成かどうか」の判定が汚染されないようにする。
    """
    monkeypatch.setattr(tools._state, "_EXEC_TMP_NAME_CACHE", {})


@pytest.fixture
def workdir_env(tmp_path, monkeypatch):
    default_workdir = tmp_path / "default_workdir"
    default_workdir.mkdir()
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", default_workdir)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    return default_workdir


class TestResolveWorkdirFallback:
    def test_unset_returns_exec_workdir_not_default_workdir(self, workdir_env) -> None:
        resolved = _workdir._resolve_workdir()

        assert resolved != workdir_env
        assert resolved == _workdir._resolve_exec_workdir()

    def test_not_found_falls_back_to_exec_workdir(self, tmp_path, workdir_env, monkeypatch) -> None:
        missing = tmp_path / "does_not_exist"
        status = check_work_dir_status_module.WorkDirAccessStatus(
            str(missing), exists=False, readable=False, writable=False
        )
        monkeypatch.setattr(
            tools.cl,
            "user_session",
            _FakeUserSession({"work_dir": str(missing), "work_dir_access": status}),
        )

        resolved = _workdir._resolve_workdir()

        assert resolved == _workdir._resolve_exec_workdir()

    def test_valid_custom_workdir_is_used_directly(self, tmp_path, workdir_env, monkeypatch) -> None:
        custom = tmp_path / "custom"
        custom.mkdir()
        monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"work_dir": str(custom)}))

        resolved = _workdir._resolve_workdir()

        assert resolved == custom

    def test_readonly_custom_workdir_falls_back_for_write_only(self, tmp_path, workdir_env, monkeypatch) -> None:
        custom = tmp_path / "custom"
        custom.mkdir()
        status = check_work_dir_status_module.WorkDirAccessStatus(
            str(custom), exists=True, readable=True, writable=False
        )
        monkeypatch.setattr(
            tools.cl,
            "user_session",
            _FakeUserSession({"work_dir": str(custom), "work_dir_access": status}),
        )

        assert _workdir._resolve_workdir(need_write=False) == custom
        assert _workdir._resolve_workdir(need_write=True) == _workdir._resolve_exec_workdir()


class TestExecTmpNameLength:
    """`_tmp_<name>` のフォルダ名をパス長上限（260文字）対策で短く保つ。"""

    def test_new_name_is_short_and_does_not_contain_raw_thread_id(self, workdir_env, monkeypatch) -> None:
        thread_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
        monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"thread_id": thread_id}))

        name = _workdir._exec_tmp_name()

        assert thread_id not in name
        assert len(f"_tmp_{name}") == 28
        assert name.endswith(f"_{_workdir._short_thread_id(thread_id)}")

    def test_resume_reuses_existing_short_dir(self, workdir_env) -> None:
        existing = workdir_env / f"_tmp_260101000000_{_workdir._short_thread_id('thread-1')}"
        existing.mkdir()

        assert _workdir._resolve_exec_workdir() == existing

    def test_resume_reuses_legacy_dir_with_raw_thread_id(self, workdir_env) -> None:
        legacy = workdir_env / "_tmp_20260101_000000_000_thread-1"
        legacy.mkdir()

        assert _workdir._resolve_exec_workdir() == legacy


class TestResolveExecWorkdirSeeding:
    def test_first_creation_copies_default_workdir_contents(self, workdir_env) -> None:
        (workdir_env / "existing.xlsx").write_text("data", encoding="utf-8")
        (workdir_env / "sub").mkdir()
        (workdir_env / "sub" / "inner.txt").write_text("inner", encoding="utf-8")

        exec_dir = _workdir._resolve_exec_workdir()

        assert (exec_dir / "existing.xlsx").read_text(encoding="utf-8") == "data"
        assert (exec_dir / "sub" / "inner.txt").read_text(encoding="utf-8") == "inner"

    def test_sibling_tmp_dirs_are_excluded_from_seeding(self, workdir_env) -> None:
        foreign = workdir_env / "_tmp_other-thread"
        foreign.mkdir()
        (foreign / "leaked.txt").write_text("secret", encoding="utf-8")

        exec_dir = _workdir._resolve_exec_workdir()

        assert not (exec_dir / "_tmp_other-thread").exists()

    def test_reuse_does_not_reseed_or_overwrite_changes(self, workdir_env) -> None:
        (workdir_env / "existing.txt").write_text("original", encoding="utf-8")
        exec_dir = _workdir._resolve_exec_workdir()
        (exec_dir / "existing.txt").write_text("edited by thread", encoding="utf-8")

        # 既定フォルダ側がその後変わっても、既存のスレッド専用フォルダを
        # 再利用する場合は再シードされない（このスレッドの変更を上書きしない）。
        (workdir_env / "existing.txt").write_text("changed elsewhere", encoding="utf-8")
        (workdir_env / "new_file.txt").write_text("new", encoding="utf-8")

        exec_dir_again = _workdir._resolve_exec_workdir()

        assert exec_dir_again == exec_dir
        assert (exec_dir / "existing.txt").read_text(encoding="utf-8") == "edited by thread"
        assert not (exec_dir / "new_file.txt").exists()


class TestBuildWorkdirStatusInfo:
    def test_unset_is_read_write_via_exec_workdir(self, workdir_env) -> None:
        info = _workdir._build_workdir_status_info(None, None)

        exec_dir = str(_workdir._resolve_exec_workdir())
        assert info["state"] == "read_write"
        assert info["source"] == "default"
        assert info["absolute_path"] == exec_dir
        assert info["write_dir"] == exec_dir

    def test_valid_custom_workdir_is_read_write_direct(self, tmp_path, workdir_env) -> None:
        custom = tmp_path / "custom"
        custom.mkdir()
        status = check_work_dir_status_module.WorkDirAccessStatus(
            str(custom), exists=True, readable=True, writable=True
        )

        info = _workdir._build_workdir_status_info(str(custom), status)

        assert info["state"] == "read_write"
        assert info["absolute_path"] == str(custom)
        assert info["write_dir"] == str(custom)

    def test_readonly_custom_workdir_redirects_write_dir_only(self, tmp_path, workdir_env) -> None:
        custom = tmp_path / "custom"
        custom.mkdir()
        status = check_work_dir_status_module.WorkDirAccessStatus(
            str(custom), exists=True, readable=True, writable=False
        )

        info = _workdir._build_workdir_status_info(str(custom), status)

        assert info["state"] == "read_only"
        assert info["absolute_path"] == str(custom)
        assert info["write_dir"] == str(_workdir._resolve_exec_workdir())
        assert "read_dir" not in info

    def test_not_found_custom_workdir_redirects_both(self, tmp_path, workdir_env) -> None:
        missing = tmp_path / "missing"
        status = check_work_dir_status_module.WorkDirAccessStatus(
            str(missing), exists=False, readable=False, writable=False
        )

        info = _workdir._build_workdir_status_info(str(missing), status)

        assert info["state"] == "not_found"
        assert info["read_dir"] == str(workdir_env)
        assert info["write_dir"] == str(_workdir._resolve_exec_workdir())


class TestCheckWorkDirStatusAndNoticeAgree:
    """check_work_dir_status ツールと _build_work_dir_notice が共通ヘルパーを
    経由することで、食い違わない一貫した値を返すことの回帰テスト。"""

    def test_check_work_dir_status_matches_shared_helper(self, workdir_env) -> None:
        import json

        result = json.loads(tools.check_work_dir_status.func())
        expected = _workdir._build_workdir_status_info(None, None)

        assert result["state"] == expected["state"] == "read_write"
        assert result["write_dir"] == expected["write_dir"]
        assert result["absolute_path"] == expected["absolute_path"]
