"""xlsm/xls ファイルに埋め込まれたVBAマクロのコードを読み込みJSONで出力する。

excel-vba-read スキルの実行スクリプト（progressive disclosure 第3段階）。
run_script から
    python read_vba.py <file_path> [--module <モジュール名>]
の形で呼ばれる。

oletools（olevba の VBA_Parser）でファイルのバイト列から直接VBAソースコードを
抽出する。Excel本体・COMは一切使わないため、Excel未インストール環境でも動く
（edit_vba.py とは対照的に前提条件が緩い）。

--module を省略した場合はモジュール名・種別・行数の一覧のみを返す。
--module を指定した場合はそのモジュールのソースコード全文を返す。

モジュール種別（standard/class/document/form）は名前パターン・コード内容から
推測する簡易ヒューリスティックであり、完全な保証はない（正確な種別が必要な
場合はExcel上のVBEで確認するようユーザーに促すこと）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

# office_shared/excel_common.py から共有ヘルパーを import する（1-B 相互import方式）
_OFFICE_SHARED = Path(__file__).resolve().parent.parent.parent / "office_shared"
if str(_OFFICE_SHARED) not in sys.path:
    sys.path.append(str(_OFFICE_SHARED))
from excel_common import setup_utf8_stdio, summarize_result, write_json_result  # noqa: E402

_SHEET_CODENAME_RE = re.compile(r"^Sheet\d+$")


def _classify_module_type(vba_filename: str, vba_code: str) -> str:
    stem = Path(vba_filename).stem
    if stem == "ThisWorkbook":
        return "document"
    if _SHEET_CODENAME_RE.match(stem) or "Private Sub Worksheet_" in vba_code:
        return "document"
    if "Attribute VB_Base = \"0{" in vba_code or "Attribute VB_Exposed" in vba_code:
        return "class"
    if "Attribute VB_Base = \"0000" in vba_code and "Begin {" in vba_code:
        return "form"
    return "standard"


def _read_vba(path: Path, module_arg: str | None) -> dict:
    from oletools.olevba import VBA_Parser

    vba = VBA_Parser(str(path))
    try:
        if not vba.detect_vba_macros():
            return {"path": str(path), "has_vba": False, "modules": []}

        entries = []
        for _filename, _stream_path, vba_filename, vba_code in vba.extract_all_macros():
            name = Path(vba_filename).stem
            entries.append(
                {
                    "name": name,
                    "type": _classify_module_type(vba_filename, vba_code),
                    "line_count": vba_code.count("\n") + 1 if vba_code else 0,
                    "code": vba_code,
                }
            )

        if module_arg is None:
            return {
                "path": str(path),
                "has_vba": True,
                "modules": [{k: v for k, v in e.items() if k != "code"} for e in entries],
            }

        for e in entries:
            if e["name"] == module_arg:
                return {"path": str(path), "module": e["name"], "type": e["type"], "code": e["code"]}
        names = [e["name"] for e in entries]
        raise ValueError(f"Module not found: {module_arg!r} (existing modules: {names})")
    finally:
        vba.close()


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("file_path")
    parser.add_argument("--module", default=None, help="name of the module whose source code to get (if omitted, only the list is returned)")
    args = parser.parse_args()

    path = Path(args.file_path)
    if not path.is_file():
        print(f"File not found: {args.file_path}", file=sys.stderr)
        return 1

    ext = path.suffix.lower()
    if ext == ".xlsx":
        print(
            "This extension does not contain VBA code (.xlsx cannot store macros). "
            "Specify .xlsm or .xls.",
            file=sys.stderr,
        )
        return 1
    if ext not in (".xlsm", ".xls"):
        print(f"Only the .xlsm/.xls extensions are supported: {ext}", file=sys.stderr)
        return 1

    try:
        result = _read_vba(path, args.module)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    except ImportError as e:
        print(f"A required library was not found (oletools is required): {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - 破損ファイル等、oletoolsが送出する多様な例外を丸めて報告する
        print(f"Failed to read the VBA code: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1

    summary = summarize_result(result, ["modules", "code"])
    summary.update(write_json_result(result, "excel_vba_read", path))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
