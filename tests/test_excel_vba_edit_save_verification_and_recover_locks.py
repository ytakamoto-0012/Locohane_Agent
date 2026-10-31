"""edit_vba.pyの保存後mtime検証と`--recover-locks`CLIモードの回帰テスト。

背景（2026-08-23 issue/20260823_010100）: 別のExcelプロセスが対象ファイルを
ロックしていると、`workbook.Save()`/`SaveAs()`が例外を出さずに完了したように
見えてもファイルへ実際には書き込まれないことがあった。`edit_vba.py`は保存の
成否を一切検証していなかったため、`applied_ops`付きで成功報告される一方で
実体は更新されない、というサイレント失敗が起きていた（LLMが13分間原因不明の
まま迷走した）。この対策として保存直前後のmtime比較を追加した。

`win32com.client`/`pythoncom`をフェイクに差し替え、実Excelなしで
`_edit_vba()`のSave検証ロジックを検証する。ops適用ループ自体は本テストの
主題ではないため`ops=[]`（空）で通し、`_vba_ops.apply_op`のモックは行わない。

また同issueで、PID記録先を自セッション専用サンドボックスへ移した上で
「自セッションが記録した対象だけを安全に後始末する」`--recover-locks`
CLIモードを新設した（PID番号を一切受け取らない設計）。そのCLI配線自体の
回帰テストも合わせて置く。
"""

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "excel-vba-edit" / "scripts"
_OFFICE_SHARED = Path(__file__).resolve().parent.parent / "skills" / "office_shared"
for _d in (_SCRIPTS_DIR, _OFFICE_SHARED):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import edit_vba  # noqa: E402


class _FakeVBProject:
    pass


class _FakeWorkbook:
    def __init__(self, output_path: Path, touch_on_save: bool):
        self.VBProject = _FakeVBProject()
        self._output_path = output_path
        self._touch_on_save = touch_on_save
        self.closed_with = None

    def _touch(self) -> None:
        import os
        import time

        # 実ファイルシステムの時刻分解能に依存せず確実にmtimeを進める。
        current = self._output_path.stat().st_mtime if self._output_path.exists() else time.time()
        new_time = current + 10
        self._output_path.write_bytes(b"dummy xlsm bytes")
        os.utime(self._output_path, (new_time, new_time))

    def SaveAs(self, path, FileFormat=None):
        if self._touch_on_save:
            self._touch()

    def Save(self):
        if self._touch_on_save:
            self._touch()
        # touch_on_save=False の場合は何もしない
        # （ロック中の別プロセスにより保存が実効を持たない状況を模す）

    def Close(self, SaveChanges=False):
        self.closed_with = SaveChanges


class _FakeWorkbooksCollection:
    def __init__(self, workbook):
        self._workbook = workbook

    def Add(self):
        return self._workbook

    def Open(self, *args, **kwargs):
        return self._workbook


class _FakeExcel:
    def __init__(self, workbook):
        self.Visible = None
        self.DisplayAlerts = None
        self.AutomationSecurity = None
        self.Hwnd = 0  # PID記録は諦める（本テストの主題ではない）
        self.Workbooks = _FakeWorkbooksCollection(workbook)
        self.quit_called = False

    def Quit(self):
        self.quit_called = True


class _FakeWin32ClientModule:
    def __init__(self, workbook):
        self._workbook = workbook

    def DispatchEx(self, prog_id):
        return _FakeExcel(self._workbook)


class _FakePythoncomModule:
    def CoInitialize(self):
        pass

    def CoUninitialize(self):
        pass


def _install_fake_com(monkeypatch, workbook) -> None:
    monkeypatch.setitem(sys.modules, "pythoncom", _FakePythoncomModule())
    monkeypatch.setitem(sys.modules, "win32com.client", _FakeWin32ClientModule(workbook))


class TestSaveVerification:
    def test_raises_when_mtime_unchanged_after_save(self, tmp_path, monkeypatch):
        target = tmp_path / "book.xlsm"
        target.write_bytes(b"original bytes")
        workbook = _FakeWorkbook(target, touch_on_save=False)
        _install_fake_com(monkeypatch, workbook)

        with pytest.raises(ValueError, match="modification time did not change"):
            edit_vba._edit_vba(target, target, ops=[], is_new=False, overwrite=False)

    def test_succeeds_when_mtime_actually_changes(self, tmp_path, monkeypatch):
        target = tmp_path / "book.xlsm"
        target.write_bytes(b"original bytes")
        workbook = _FakeWorkbook(target, touch_on_save=True)
        _install_fake_com(monkeypatch, workbook)

        result = edit_vba._edit_vba(target, target, ops=[], is_new=False, overwrite=False)

        assert result["path"] == str(target)
        assert result["applied_ops"] == 0

    def test_new_file_creation_skips_mtime_check(self, tmp_path, monkeypatch):
        """--new でのファイル新規作成時は保存前にファイルが存在しないため
        mtime比較自体をスキップする（誤検知しない）。"""
        target = tmp_path / "new_book.xlsm"
        # SaveAs自体は呼ばれるがファイルへは何も書かないフェイク
        # （新規作成のmtimeチェックスキップだけを見たいので、そもそも
        # チェック対象になり得ないことを確認する）。
        workbook = _FakeWorkbook(target, touch_on_save=False)
        _install_fake_com(monkeypatch, workbook)

        result = edit_vba._edit_vba(target, target, ops=[], is_new=True, overwrite=False)

        assert result["path"] == str(target)


class TestRecoverLocksCli:
    def test_recover_locks_flag_calls_terminate_and_prints_json(self, monkeypatch, capsys):
        canned = [{"pid": 4242, "target_path": "C:\\foo\\book.xlsm", "terminated": True}]
        called = {"count": 0}

        def _fake_terminate():
            called["count"] += 1
            return canned

        monkeypatch.setattr(edit_vba, "terminate_tracked_processes", _fake_terminate)
        monkeypatch.setattr(sys, "argv", ["edit_vba.py", "--recover-locks"])

        exit_code = edit_vba.main()

        assert exit_code == 0
        assert called["count"] == 1
        out = json.loads(capsys.readouterr().out)
        assert out == {"recovered": canned}

    def test_recover_locks_ignores_path_and_ops(self, monkeypatch, capsys):
        """--recover-locks指定時はpath/--ops-json等を渡しても無視され、
        _edit_vba（実際の編集処理）へは進まない。"""

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("_edit_vba が呼ばれてはならない")

        monkeypatch.setattr(edit_vba, "terminate_tracked_processes", lambda: [])
        monkeypatch.setattr(edit_vba, "_edit_vba", _fail_if_called)
        monkeypatch.setattr(
            sys,
            "argv",
            ["edit_vba.py", "C:\\foo\\book.xlsm", "--ops-json", "[]", "--recover-locks"],
        )

        exit_code = edit_vba.main()

        assert exit_code == 0

    def test_missing_path_without_recover_locks_is_an_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["edit_vba.py", "--ops-json", "[]"])

        exit_code = edit_vba.main()

        assert exit_code == 1
        assert "path is required" in capsys.readouterr().err

    def test_missing_ops_without_recover_locks_is_an_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["edit_vba.py", "C:\\foo\\book.xlsm"])

        exit_code = edit_vba.main()

        assert exit_code == 1
        assert "--ops-json" in capsys.readouterr().err

    def test_new_without_ops_defaults_to_empty_ops_list(self, tmp_path, monkeypatch, capsys):
        """2026-08-23実インシデント（issue/20260823_103204）: --newのみ（VBAコードは
        まだ書かず器だけ先に作りたい）でplannerがopsを指定しないケースが繰り返し
        発生し、workerが毎回--ops-json '[]'を自力で編み出して自己修復していた。
        --new指定時はops-json/ops-file省略でも[]扱いにして往復を無くす。"""
        target = tmp_path / "new_book.xlsm"
        workbook = _FakeWorkbook(target, touch_on_save=True)
        _install_fake_com(monkeypatch, workbook)
        monkeypatch.setattr(sys, "argv", ["edit_vba.py", str(target), "--new"])

        exit_code = edit_vba.main()

        assert exit_code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["applied_ops"] == 0

    def test_new_without_ops_but_with_output_flag_still_works(self, tmp_path, monkeypatch, capsys):
        target = tmp_path / "new_book.xlsm"
        workbook = _FakeWorkbook(target, touch_on_save=True)
        _install_fake_com(monkeypatch, workbook)
        monkeypatch.setattr(sys, "argv", ["edit_vba.py", str(target), "--new", "--overwrite"])

        exit_code = edit_vba.main()

        assert exit_code == 0
