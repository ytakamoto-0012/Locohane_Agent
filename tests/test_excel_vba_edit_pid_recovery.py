"""excel-vba-editスキルのrun_macroハング時プロセス強制終了サポート
（skills/office_shared/excel_common.py の record_excel_pid/release_excel_pid/
is_process_running/wait_for_process_exit/terminate_tracked_processes）。

背景: `run_macro`がハングしてrun_scriptの外部タイムアウト（既定300秒）で
Pythonプロセスごと強制終了されると、edit_vba.py側のtry/finallyが実行される
機会が無くEXCEL.EXEだけが対象ファイルを開いたまま残留する
（issue/20260822_212800_run_macro_msgbox_hang_and_excel_lock.md参照）。

2026-08-23、この対策自体に2つの問題があったことが実インシデントで判明した。
1. `release_excel_pid`がプロセスの実終了を確認せず無条件に呼ばれていたため、
   `Close()`/`Quit()`が実効を持たなくてもPID記録だけが消え、実在するオーファン
   プロセスを追跡できなくなっていた
   （issue/20260823_010100_edit_vba_set_code_silent_save_failure_stale_excel_lock.md）。
2. PID記録先が対象ファイルの隣（ユーザーの作業ディレクトリ、LLMが自由に
   読み書きできる場所）だったため、「追跡対象PIDのみを終了する」という
   ガイダンスがプローズの注意書きに留まり、低パラメータモデルには確実に
   守らせられなかった（実際、無差別`taskkill`が実行された）。

このテストファイルは、これらを踏まえた再設計（PID記録を自セッション専用の
サンドボックス`_tmp_<thread_id>/excel_locks/pids.json`へ移し、プロセスの
実終了を確認できた場合のみレジストリから除去し、終了操作自体もPID番号を
一切受け取らない`terminate_tracked_processes()`に限定する）を検証する。
"""

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "office_shared"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"

import excel_common  # noqa: E402


class _FakeWin32Process:
    def __init__(self, pid: int):
        self._pid = pid

    def GetWindowThreadProcessId(self, hwnd):
        return (111, self._pid)


class _FakeExcel:
    def __init__(self, hwnd):
        self.Hwnd = hwnd


class _RaisingHwndExcel:
    @property
    def Hwnd(self):
        raise RuntimeError("COM error")


def _install_fake_win32process(monkeypatch, pid: int):
    monkeypatch.setitem(sys.modules, "win32process", _FakeWin32Process(pid))


def _setup_sandbox_env(monkeypatch, tmp_path, thread_id: str = "thread-1"):
    """record_excel_pid等がAGENT_SRC_DIR経由でpath_memoryをimportし、
    _tmp_<thread_id>配下のレジストリを解決できるよう環境変数を用意する。
    """
    default_workdir = tmp_path / "default_workdir"
    default_workdir.mkdir(exist_ok=True)
    monkeypatch.setenv("AGENT_SRC_DIR", str(_SRC_DIR))
    monkeypatch.setenv("AGENT_DEFAULT_WORKDIR", str(default_workdir))
    monkeypatch.setenv("AGENT_THREAD_ID", thread_id)
    return default_workdir


def _registry_path(default_workdir: Path, thread_id: str = "thread-1") -> Path:
    return default_workdir / f"_tmp_{thread_id}" / "excel_locks" / "pids.json"


def _read_registry(default_workdir: Path, thread_id: str = "thread-1") -> list[dict]:
    path = _registry_path(default_workdir, thread_id)
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


class TestRecordExcelPid:
    def test_records_pid_to_sandboxed_registry(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path)
        _install_fake_win32process(monkeypatch, 12345)
        target = tmp_path / "work_dir" / "book.xlsm"

        pid = excel_common.record_excel_pid(target, _FakeExcel(hwnd=999))

        assert pid == 12345
        entries = _read_registry(default_workdir)
        assert [e["pid"] for e in entries] == [12345]
        assert entries[0]["target_path"] == str(target.resolve())
        # 対象ファイルの隣には何も作られない（旧方式からの回帰確認）
        assert not (target.parent / "book.vba_pid").exists()

    def test_appends_without_duplicating_same_pid(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path)
        _install_fake_win32process(monkeypatch, 12345)
        target = tmp_path / "book.xlsm"

        excel_common.record_excel_pid(target, _FakeExcel(hwnd=999))
        excel_common.record_excel_pid(target, _FakeExcel(hwnd=999))

        entries = _read_registry(default_workdir)
        assert [e["pid"] for e in entries] == [12345]

    def test_accumulates_multiple_pids_across_runs(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path)
        target = tmp_path / "book.xlsm"

        _install_fake_win32process(monkeypatch, 111)
        excel_common.record_excel_pid(target, _FakeExcel(hwnd=1))
        _install_fake_win32process(monkeypatch, 222)
        excel_common.record_excel_pid(target, _FakeExcel(hwnd=2))

        entries = _read_registry(default_workdir)
        assert [e["pid"] for e in entries] == [111, 222]

    def test_different_thread_ids_get_isolated_registries(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path, thread_id="thread-A")
        _install_fake_win32process(monkeypatch, 111)
        excel_common.record_excel_pid(tmp_path / "a.xlsm", _FakeExcel(hwnd=1))

        monkeypatch.setenv("AGENT_THREAD_ID", "thread-B")
        _install_fake_win32process(monkeypatch, 222)
        excel_common.record_excel_pid(tmp_path / "b.xlsm", _FakeExcel(hwnd=2))

        assert [e["pid"] for e in _read_registry(default_workdir, "thread-A")] == [111]
        assert [e["pid"] for e in _read_registry(default_workdir, "thread-B")] == [222]

    def test_zero_hwnd_returns_none_and_writes_nothing(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path)
        _install_fake_win32process(monkeypatch, 12345)
        target = tmp_path / "book.xlsm"

        pid = excel_common.record_excel_pid(target, _FakeExcel(hwnd=0))

        assert pid is None
        assert _read_registry(default_workdir) == []

    def test_com_error_fails_open_and_returns_none(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path)
        _install_fake_win32process(monkeypatch, 12345)
        target = tmp_path / "book.xlsm"

        pid = excel_common.record_excel_pid(target, _RaisingHwndExcel())

        assert pid is None
        assert _read_registry(default_workdir) == []

    def test_missing_agent_src_dir_fails_open_but_still_returns_pid(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_SRC_DIR", raising=False)
        _install_fake_win32process(monkeypatch, 12345)
        target = tmp_path / "book.xlsm"

        pid = excel_common.record_excel_pid(target, _FakeExcel(hwnd=999))

        assert pid == 12345  # レジストリへは書けないがPID自体は返す


class TestReleaseExcelPid:
    def test_removes_pid_and_deletes_registry_when_empty(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path)
        _install_fake_win32process(monkeypatch, 12345)
        target = tmp_path / "book.xlsm"
        excel_common.record_excel_pid(target, _FakeExcel(hwnd=999))

        excel_common.release_excel_pid(12345)

        assert _read_registry(default_workdir) == []
        assert not _registry_path(default_workdir).exists()

    def test_removes_only_matching_pid_leaving_others(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path)
        target = tmp_path / "book.xlsm"
        _install_fake_win32process(monkeypatch, 111)
        excel_common.record_excel_pid(target, _FakeExcel(hwnd=1))
        _install_fake_win32process(monkeypatch, 222)
        excel_common.record_excel_pid(target, _FakeExcel(hwnd=2))

        excel_common.release_excel_pid(111)

        entries = _read_registry(default_workdir)
        assert [e["pid"] for e in entries] == [222]

    def test_none_pid_is_noop(self, tmp_path, monkeypatch):
        _setup_sandbox_env(monkeypatch, tmp_path)

        excel_common.release_excel_pid(None)  # should not raise

    def test_missing_registry_is_noop(self, tmp_path, monkeypatch):
        _setup_sandbox_env(monkeypatch, tmp_path)

        excel_common.release_excel_pid(12345)  # should not raise


class TestIsProcessRunning:
    def test_still_active_returns_true(self, monkeypatch):
        class _FakeWin32Api:
            def OpenProcess(self, access, inherit, pid):
                return "handle"

            def CloseHandle(self, handle):
                pass

        class _FakeWin32ProcessMod:
            def GetExitCodeProcess(self, handle):
                return 259  # STILL_ACTIVE

        class _FakeWin32Con:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259

        monkeypatch.setitem(sys.modules, "win32api", _FakeWin32Api())
        monkeypatch.setitem(sys.modules, "win32process", _FakeWin32ProcessMod())
        monkeypatch.setitem(sys.modules, "win32con", _FakeWin32Con())

        assert excel_common.is_process_running(12345) is True

    def test_exited_process_returns_false(self, monkeypatch):
        class _FakeWin32Api:
            def OpenProcess(self, access, inherit, pid):
                return "handle"

            def CloseHandle(self, handle):
                pass

        class _FakeWin32ProcessMod:
            def GetExitCodeProcess(self, handle):
                return 0  # 終了コード0（STILL_ACTIVEではない）

        class _FakeWin32Con:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259

        monkeypatch.setitem(sys.modules, "win32api", _FakeWin32Api())
        monkeypatch.setitem(sys.modules, "win32process", _FakeWin32ProcessMod())
        monkeypatch.setitem(sys.modules, "win32con", _FakeWin32Con())

        assert excel_common.is_process_running(12345) is False

    def test_confirmation_failure_defaults_to_running_true(self, monkeypatch):
        class _RaisingWin32Api:
            def OpenProcess(self, access, inherit, pid):
                raise OSError("access denied")

        monkeypatch.setitem(sys.modules, "win32api", _RaisingWin32Api())

        # 確認自体に失敗した場合は安全側（生存扱い）でTrue
        assert excel_common.is_process_running(12345) is True


class TestTerminateTrackedProcesses:
    def test_terminates_only_registered_pids_and_clears_registry(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path)
        target = tmp_path / "book.xlsm"
        _install_fake_win32process(monkeypatch, 111)
        excel_common.record_excel_pid(target, _FakeExcel(hwnd=1))

        terminated_pids = []

        def _fake_is_running(pid):
            return True

        class _FakeWin32Api:
            def OpenProcess(self, access, inherit, pid):
                return pid

            def TerminateProcess(self, handle, exit_code):
                terminated_pids.append(handle)

            def CloseHandle(self, handle):
                pass

        class _FakeWin32Con:
            PROCESS_TERMINATE = 0x0001

        monkeypatch.setattr(excel_common, "is_process_running", _fake_is_running)
        monkeypatch.setattr(excel_common, "wait_for_process_exit", lambda pid, timeout_seconds=3.0: True)
        monkeypatch.setitem(sys.modules, "win32api", _FakeWin32Api())
        monkeypatch.setitem(sys.modules, "win32con", _FakeWin32Con())

        results = excel_common.terminate_tracked_processes()

        assert terminated_pids == [111]
        assert results == [{"pid": 111, "target_path": str(target.resolve()), "terminated": True}]
        assert _read_registry(default_workdir) == []

    def test_already_dead_pid_is_cleared_without_terminate_call(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path)
        target = tmp_path / "book.xlsm"
        _install_fake_win32process(monkeypatch, 111)
        excel_common.record_excel_pid(target, _FakeExcel(hwnd=1))

        monkeypatch.setattr(excel_common, "is_process_running", lambda pid: False)

        results = excel_common.terminate_tracked_processes()

        assert results == [{"pid": 111, "target_path": str(target.resolve()), "terminated": True}]
        assert _read_registry(default_workdir) == []

    def test_termination_failure_keeps_entry_in_registry(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path)
        target = tmp_path / "book.xlsm"
        _install_fake_win32process(monkeypatch, 111)
        excel_common.record_excel_pid(target, _FakeExcel(hwnd=1))

        class _RaisingWin32Api:
            def OpenProcess(self, access, inherit, pid):
                raise OSError("access denied")

        monkeypatch.setattr(excel_common, "is_process_running", lambda pid: True)
        monkeypatch.setitem(sys.modules, "win32api", _RaisingWin32Api())

        results = excel_common.terminate_tracked_processes()

        assert results == [{"pid": 111, "target_path": str(target.resolve()), "terminated": False}]
        # 終了に失敗したのでレジストリには残る（次回の--recover-locksで再挑戦できる）
        assert [e["pid"] for e in _read_registry(default_workdir)] == [111]

    def test_empty_registry_returns_empty_list(self, tmp_path, monkeypatch):
        _setup_sandbox_env(monkeypatch, tmp_path)

        assert excel_common.terminate_tracked_processes() == []

    def test_never_operates_on_other_sessions_registry(self, tmp_path, monkeypatch):
        default_workdir = _setup_sandbox_env(monkeypatch, tmp_path, thread_id="thread-A")
        _install_fake_win32process(monkeypatch, 111)
        excel_common.record_excel_pid(tmp_path / "a.xlsm", _FakeExcel(hwnd=1))

        # 別セッション（thread-B）から見ると thread-A のレジストリは対象にならない
        monkeypatch.setenv("AGENT_THREAD_ID", "thread-B")
        monkeypatch.setattr(excel_common, "is_process_running", lambda pid: True)

        results = excel_common.terminate_tracked_processes()

        assert results == []
        # thread-A側の記録はそのまま残っている
        assert [e["pid"] for e in _read_registry(default_workdir, "thread-A")] == [111]
