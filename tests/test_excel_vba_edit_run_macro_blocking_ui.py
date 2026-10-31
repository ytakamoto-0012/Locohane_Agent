"""excel-vba-editスキル（skills/excel-vba-edit/scripts/_vba_ops.py）の
run_macro実行前MsgBox/InputBox検出。

背景: `run_macro`で呼び出すマクロがMsgBox/InputBoxを含んでいると、対話
セッションの無い自動化実行では誰もダイアログを閉じられず、run_scriptの
タイムアウト（既定300秒）までハングし、その後もExcelプロセスがファイルを
開いたまま残留して以後の編集操作を巻き込んで失敗させ続ける。2026-08-22に
同一セッション内で3回（CreateButtons/ImportCSVs/DeleteCharts、いずれも
末尾にMsgBoxを含む）実機で確認した。SKILL.mdでの回避誘導だけでは
既にSKILL.mdを読み込み済みの会話には効かないため、実行前の静的チェックで
機械的にブロックする。
"""

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "excel-vba-edit" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _vba_ops import _check_blocking_ui, _find_procedure_code, op_run_macro  # noqa: E402


class _FakeCodeModule:
    def __init__(self, procs: dict):
        self._procs = procs
        self._by_range = {(1, len(code.splitlines())): code for code in procs.values()}

    def ProcStartLine(self, name, kind):
        if name not in self._procs:
            raise RuntimeError("proc not found")
        return 1

    def ProcCountLines(self, name, kind):
        if name not in self._procs:
            raise RuntimeError("proc not found")
        return len(self._procs[name].splitlines())

    def Lines(self, start, count):
        return self._by_range[(start, count)]


class _FakeComponent:
    def __init__(self, module_name: str, procs: dict):
        self.Name = module_name
        self.CodeModule = _FakeCodeModule(procs)


class _FakeVBProject:
    def __init__(self, components: list[_FakeComponent]):
        self.VBComponents = components


class _FakeApplication:
    def __init__(self):
        self.run_called_with = None

    def Run(self, macro_ref, *args):
        self.run_called_with = (macro_ref, args)
        return None


class _FakeWorkbook:
    def __init__(self):
        self.Name = "book.xlsm"
        self.Application = _FakeApplication()


_SAFE_SUB = "Public Sub SafeSub()\n    Debug.Print \"done\"\nEnd Sub"
_MSGBOX_SUB = 'Public Sub CreateButtons()\n    Debug.Print "x"\n    MsgBox "done", vbInformation\nEnd Sub'
_INPUTBOX_SUB = 'Public Sub AskName()\n    Dim n As String\n    n = InputBox("name?")\nEnd Sub'


class TestCheckBlockingUi:
    def test_msgbox_raises(self):
        with pytest.raises(ValueError, match="MsgBox"):
            _check_blocking_ui(_MSGBOX_SUB, "modMain.CreateButtons")

    def test_inputbox_raises(self):
        with pytest.raises(ValueError, match="InputBox"):
            _check_blocking_ui(_INPUTBOX_SUB, "modMain.AskName")

    def test_clean_code_does_not_raise(self):
        _check_blocking_ui(_SAFE_SUB, "modMain.SafeSub")  # should not raise


class TestFindProcedureCode:
    def test_dotted_name_finds_code_in_named_module(self):
        vb_project = _FakeVBProject(
            [
                _FakeComponent("modMain", {"CreateButtons": _MSGBOX_SUB}),
                _FakeComponent("modOther", {"SafeSub": _SAFE_SUB}),
            ]
        )
        code = _find_procedure_code(vb_project, "modMain.CreateButtons")
        assert code == _MSGBOX_SUB

    def test_bare_name_searches_all_modules(self):
        vb_project = _FakeVBProject(
            [
                _FakeComponent("modMain", {"CreateButtons": _MSGBOX_SUB}),
                _FakeComponent("modOther", {"SafeSub": _SAFE_SUB}),
            ]
        )
        code = _find_procedure_code(vb_project, "SafeSub")
        assert code == _SAFE_SUB

    def test_unknown_module_returns_none(self):
        vb_project = _FakeVBProject([_FakeComponent("modMain", {"CreateButtons": _MSGBOX_SUB})])
        assert _find_procedure_code(vb_project, "modDoesNotExist.Foo") is None

    def test_unknown_procedure_returns_none(self):
        vb_project = _FakeVBProject([_FakeComponent("modMain", {"CreateButtons": _MSGBOX_SUB})])
        assert _find_procedure_code(vb_project, "modMain.DoesNotExist") is None


class TestOpRunMacroGuard:
    def test_msgbox_macro_is_blocked_before_excel_run_is_called(self):
        vb_project = _FakeVBProject([_FakeComponent("modMain", {"CreateButtons": _MSGBOX_SUB})])
        workbook = _FakeWorkbook()

        with pytest.raises(ValueError, match="MsgBox"):
            op_run_macro(workbook, vb_project, {"op": "run_macro", "name": "modMain.CreateButtons"})

        assert workbook.Application.run_called_with is None

    def test_clean_macro_reaches_excel_run(self):
        vb_project = _FakeVBProject([_FakeComponent("modMain", {"SafeSub": _SAFE_SUB})])
        workbook = _FakeWorkbook()

        op_run_macro(workbook, vb_project, {"op": "run_macro", "name": "modMain.SafeSub"})

        assert workbook.Application.run_called_with is not None
        assert workbook.Application.run_called_with[0] == "'book.xlsm'!modMain.SafeSub"

    def test_lookup_failure_fails_open_and_still_calls_excel_run(self):
        """プロシージャの特定に失敗した場合はチェックを諦めて実行を継続する
        （フェイルオープン。lookup自体の失敗で正当な実行までブロックしないため）。"""
        vb_project = _FakeVBProject([])  # モジュールが1つも無い＝lookup失敗
        workbook = _FakeWorkbook()

        op_run_macro(workbook, vb_project, {"op": "run_macro", "name": "modMain.Unknown"})

        assert workbook.Application.run_called_with is not None
