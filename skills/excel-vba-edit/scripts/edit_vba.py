"""ops（操作）のリストを適用してxlsmのVBAマクロコードを追加・上書き・削除・実行する。

excel-vba-edit スキルの実行スクリプト（progressive disclosure 第3段階）。
run_script から
    python edit_vba.py <path> --ops-json "<JSON配列>" [--new] [--overwrite] [--output <別名保存先>]
または
    python edit_vba.py <path> --ops-file <JSONファイルのパス> [--new] [--overwrite] [--output ...]
の形で呼ばれる。

VBAプロジェクトオブジェクトモデルへの操作にはExcel本体をCOM経由（pywin32）で
実際に起動する必要がある（recalc_excel.pyと同じ理由・同じライフサイクル管理:
pythoncom.CoInitialize -> DispatchEx -> try/finallyでClose/Quit/CoUninitialize）。

前提条件（SKILL.mdにも記載）:
- ローカルにMicrosoft Excelがインストールされ、対話セッションから呼び出されて
  いる必要がある。
- Excelのトラストセンターで「VBA プロジェクト オブジェクト モデルへのアクセスを
  信頼する」が有効になっている必要がある（既定は無効。プログラムからは有効化
  できないユーザー側の手動設定）。未設定の場合は workbook.VBProject へのアクセス
  自体がCOMエラーになるため、分かりやすい日本語メッセージに変換して案内する。
- 対象は .xlsm のみ（.xls/.xlsxは非対応。UserFormの作成・編集も対象外）。

ops の各要素の形式や対応opの一覧はSKILL.mdを参照。実装（各opの処理）は
scripts/_vba_ops.py の OP_HANDLERS を参照。
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
    backup_before_overwrite,
    read_text_with_fallback,
    record_excel_pid,
    register_output_path,
    release_excel_pid,
    setup_utf8_stdio,
    terminate_tracked_processes,
    wait_for_process_exit,
)
from _vba_ops import apply_op  # noqa: E402

_XL_OPEN_XML_WORKBOOK_MACRO_ENABLED = 52


def _load_json_arg(args: argparse.Namespace) -> str:
    if args.ops_json is not None:
        return args.ops_json
    data_path = Path(args.ops_file)
    if not data_path.is_file():
        raise FileNotFoundError(f"ops file not found: {args.ops_file}")
    return read_text_with_fallback(data_path)


def _trust_center_error_message() -> str:
    return (
        "Could not access the VBA project. In Excel's Trust Center settings, "
        "\"Trust access to the VBA project object model\" "
        "may not be enabled.\n"
        "How to set it: open Excel -> File -> Options -> Trust Center -> "
        "\"Trust Center Settings\" -> \"Macro Settings\" -> "
        "check \"Trust access to the VBA project object model\".\n"
        "For security reasons this setting cannot be enabled automatically by a program, "
        "so the user needs to set it themselves."
    )


def _edit_vba(path: Path, output_path: Path, ops: list, is_new: bool, overwrite: bool) -> dict:
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
        # run_macroがハングしてrun_scriptの外部タイムアウトでPythonプロセスごと
        # 強制終了されると、下のfinally節が実行されずEXCEL.EXEだけが対象ファイルを
        # 開いたまま残留する。その際に `--recover-locks` で確実に後始末できるよう、
        # 起動直後にPIDを自セッションのサンドボックスへ記録しておく（正常終了時は
        # finallyで取り除く。詳細はSKILL.mdのrun_macro説明を参照）。
        recorded_pid = record_excel_pid(output_path, excel)
        # run_macro を含む場合のみマクロの実行を許可する（msoAutomationSecurityLow=1）。
        # ForceDisable(3)のままだと Application.Run 自体がセキュリティ設定によりブロック
        # されるため。run_macro を含まない場合（add_module/set_code/delete_module のみ）は
        # ForceDisable のままにし、Workbook_Open 等の自動マクロが意図せず実行されるのを防ぐ。
        has_run_macro = any(isinstance(op, dict) and op.get("op") == "run_macro" for op in ops)
        excel.AutomationSecurity = 1 if has_run_macro else 3

        if is_new:
            if output_path.exists() and not overwrite:
                raise FileExistsError(f"The output already exists (specify --overwrite to overwrite it): {output_path}")
            workbook = excel.Workbooks.Add()
        else:
            if not path.is_file():
                raise FileNotFoundError(f"File to edit not found (specify --new to create a new one): {path}")
            workbook = excel.Workbooks.Open(str(path), UpdateLinks=0, IgnoreReadOnlyRecommended=True)

        try:
            vb_project = workbook.VBProject
        except Exception as e:
            raise ValueError(_trust_center_error_message()) from e

        results = []
        for idx, op in enumerate(ops):
            if not isinstance(op, dict) or "op" not in op:
                raise ValueError(f"ops[{idx}] is invalid (it must be an object with an 'op' key): {op!r}")
            try:
                result = apply_op(workbook, vb_project, op)
            except KeyError as e:
                raise ValueError(f"ops[{idx}] (op={op.get('op')!r}) is missing the required key {e}")
            except (ValueError, TypeError) as e:
                raise ValueError(f"Failed to apply ops[{idx}] (op={op.get('op')!r}): {e}")
            if result is not None:
                results.append(result)

        backup_path = backup_before_overwrite(output_path)

        # 保存直前のmtimeを控えておき、保存後に実際に変化したか確認する。
        # 別のExcelプロセスが対象ファイルをロックしていると、workbook.Save()/
        # SaveAs()が例外を出さずに完了したように見えてもファイルへ実際には
        # 書き込まれないことがある（2026-08-23 issue: applied_ops付きで成功
        # 報告されたのに実体は更新されず、LLMが13分間原因不明のまま迷走した）。
        pre_save_mtime = output_path.stat().st_mtime if output_path.exists() else None

        if is_new or output_path != path:
            workbook.SaveAs(str(output_path), FileFormat=_XL_OPEN_XML_WORKBOOK_MACRO_ENABLED)
        else:
            workbook.Save()

        if pre_save_mtime is not None and output_path.exists() and output_path.stat().st_mtime == pre_save_mtime:
            raise ValueError(
                "Saving finished without an exception, but the file's modification time did not change. "
                "Another Excel process may be locking the target file. "
                "Check whether --recover-locks can terminate Excel processes left behind by this session, "
                "or check with tasklist whether an unrelated EXCEL.EXE is running."
            )

        result = {
            "path": str(output_path),
            "backup_path": str(backup_path) if backup_path else None,
            "applied_ops": len(ops),
            "results": results,
        }
        path_memory = register_output_path(output_path, description="file generated/updated by edit_vba")
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
        # Close()/Quit()が例外を出さなくても実プロセスが終了したとは限らない
        # ため、実終了を確認できた場合のみレジストリから除去する（2026-08-23
        # issue対応。無条件でreleaseしていたため、Quit()が実効を持たなかった
        # ケースでもPID記録だけが消え、以後追跡不能になっていた）。生存して
        # いれば記録を残し、次回 --recover-locks で発見できるようにする。
        if recorded_pid is not None and wait_for_process_exit(recorded_pid):
            release_excel_pid(recorded_pid)
        pythoncom.CoUninitialize()


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default=None, help="xlsm path to edit (the path to create with --new) (not needed with --recover-locks)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ops-json", help="pass the JSON array of operations to apply as is")
    group.add_argument("--ops-file", help="path of a file with the JSON array of operations to apply")
    parser.add_argument("--new", action="store_true", help="create a new macro-enabled workbook without reading an existing file")
    parser.add_argument("--overwrite", action="store_true", help="with --new, allow overwriting if the output already exists")
    parser.add_argument("--output", default=None, help="output path (if omitted, path is overwritten; only .xlsm can be specified)")
    parser.add_argument(
        "--recover-locks",
        action="store_true",
        help=(
            "without path/ops, terminate only the Excel processes started by this session that were left behind "
            "and are still alive. Does not take PID numbers "
            "(only targets recorded by this session are operated on)."
        ),
    )
    args = parser.parse_args()

    if args.recover_locks:
        recovered = terminate_tracked_processes()
        print(json.dumps({"recovered": recovered}, ensure_ascii=False))
        return 0

    if not args.path:
        print("path is required (except with --recover-locks)", file=sys.stderr)
        return 1
    # --newのみ（ops無しでVBAプロジェクトを持つ器だけ先に作りたい）は頻出の
    # 正当な使い方のため、--ops-json/--ops-file省略時はops=[]として扱う
    # （--newなし時は編集対象への操作が無いと無意味なので従来通り必須のまま）。
    if args.ops_json is None and args.ops_file is None and not args.new:
        print("Either --ops-json or --ops-file is required (except with --new)", file=sys.stderr)
        return 1

    path = Path(args.path)
    ext = path.suffix.lower()
    if ext != ".xlsm":
        print(f"Writing and running VBA supports only .xlsm: {ext}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else path
    if output_path.suffix.lower() != ".xlsm":
        print(f"The extension of the output must be .xlsm: {output_path.suffix}", file=sys.stderr)
        return 1

    if args.ops_json is None and args.ops_file is None:
        raw = "[]"
    else:
        try:
            raw = _load_json_arg(args)
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1

    try:
        ops = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Failed to parse the ops JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(ops, list):
        print("ops must be a JSON array", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = _edit_vba(path, output_path, ops, args.new, args.overwrite)
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    except ImportError as e:
        print(f"A required library was not found (pywin32 is required): {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - COMエラーはExcelのバージョン・状態により多様なため丸めて報告する
        print(f"Editing VBA failed: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
