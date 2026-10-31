"""Excel COM経由でxlsx/xlsm/xlsの数式を再計算・保存し、エラーセルを検出する。

excel-recalc スキルの実行スクリプト（progressive disclosure 第3段階）。
run_script から
    python recalc_excel.py <path>
の形で呼ばれる。

pure-Pythonの数式評価ライブラリは対応関数が限定的で正確性に欠けるため、
ローカルにインストール済みのMicrosoft Excelを非表示（ヘッドレス）起動して
実際に計算させる（pywin32のCOM経由）。処理後は必ずExcelを終了させる
（try/finally）。

前提・制約（SKILL.mdにも記載）:
- ローカルにMicrosoft Excelがインストールされ、対話セッションから
  呼び出されている必要がある（サービスとしての実行は不可）。
- 万一途中でスクリプトが強制終了された場合、EXCEL.EXEプロセスが
  残留する可能性がある（タスクマネージャーでの終了が必要）。
- インターネットからダウンロードしたとマークされたファイル（保護された
  ビュー対象）は正しく開けない場合がある。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

# office_shared/excel_common.py から共有ヘルパーを import する（1-B 相互import方式）
_OFFICE_SHARED = Path(__file__).resolve().parent.parent.parent / "office_shared"
if str(_OFFICE_SHARED) not in sys.path:
    sys.path.append(str(_OFFICE_SHARED))
from excel_common import (  # noqa: E402
    record_excel_pid,
    register_output_path,
    release_excel_pid,
    setup_utf8_stdio,
    terminate_tracked_processes,
    wait_for_process_exit,
)

_XL_CELL_TYPE_FORMULAS = -4123
_XL_ERRORS = 16


def _collect_errors(workbook) -> list[dict]:
    errors: list[dict] = []
    for sheet in workbook.Worksheets:
        used = sheet.UsedRange
        if used is None:
            continue
        try:
            error_cells = used.SpecialCells(_XL_CELL_TYPE_FORMULAS, _XL_ERRORS)
        except Exception:
            # 対象セルが1つも無い場合、SpecialCellsはCOMエラーを送出する。
            continue
        for area in error_cells.Areas:
            for cell in area.Cells:
                # 遅延バインディング(DispatchEx)では Address はパラメータ付きメソッドとして
                # 呼び出せない（文字列プロパティとして返る）ため、絶対参照形式から "$" を除去する。
                address = cell.Address.replace("$", "")
                errors.append({"sheet": sheet.Name, "cell": address, "value": str(cell.Text)})
    return errors


def _recalc(path: Path) -> dict:
    import pythoncom
    import win32com.client as win32

    pythoncom.CoInitialize()
    excel = None
    workbook = None
    recorded_pid = None
    try:
        excel = win32.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        excel.AutomationSecurity = 3  # msoAutomationSecurityForceDisable（マクロ確認ダイアログの抑止）
        # edit_vba.pyと同じ理由でExcel起動直後にPIDを自セッションのサンドボックスへ
        # 記録しておく（正常終了時は実終了を確認した上でfinallyで取り除く。
        # 万一残留した場合は edit_vba.py --recover-locks で後始末できる）。
        recorded_pid = record_excel_pid(path, excel)
        workbook = excel.Workbooks.Open(str(path), UpdateLinks=0, IgnoreReadOnlyRecommended=True)
        excel.CalculateFullRebuild()
        errors = _collect_errors(workbook)
        workbook.Save()
        result = {"path": str(path), "recalculated": True, "errors": errors}
        path_memory = register_output_path(path, description="file recalculated and saved by recalc_excel")
        if path_memory:
            result["path_memory"] = path_memory
        return result
    finally:
        if workbook is not None:
            try:
                workbook.Close(SaveChanges=False)
            except Exception:
                pass
        if excel is not None:
            try:
                excel.Quit()
            except Exception:
                pass
        if recorded_pid is not None and wait_for_process_exit(recorded_pid):
            release_excel_pid(recorded_pid)
        pythoncom.CoUninitialize()


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default=None, help="xlsx/xlsm/xls path to recalculate (not needed with --recover-locks)")
    parser.add_argument(
        "--recover-locks",
        action="store_true",
        help=(
            "ignore path, and terminate only the Excel processes started by this session that were left behind "
            "and are still alive. Does not take PID numbers."
        ),
    )
    args = parser.parse_args()

    if args.recover_locks:
        recovered = terminate_tracked_processes()
        print(json.dumps({"recovered": recovered}, ensure_ascii=False))
        return 0

    if not args.path:
        print("usage: recalc_excel.py <path> (or --recover-locks)", file=sys.stderr)
        return 1

    path = Path(args.path)
    if not path.is_file():
        print(f"File not found: {args.path}", file=sys.stderr)
        return 1
    if path.suffix.lower() not in (".xlsx", ".xlsm", ".xls"):
        print(f"Only the .xlsx/.xlsm/.xls extensions are supported: {path.suffix}", file=sys.stderr)
        return 1

    try:
        result = _recalc(path)
    except ImportError as e:
        print(f"A required library was not found (pywin32 is required): {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - COMエラーはExcelのバージョン・状態により多様なため丸めて報告する
        print(f"Recalculation with Excel failed: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
