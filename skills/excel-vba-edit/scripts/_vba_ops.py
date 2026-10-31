"""edit_vba.py が適用する「操作（op）」のディスパッチ実装。

各関数は (workbook, vb_project, op_dict) を受け取り、COM経由でVBAプロジェクト
（vb_project = workbook.VBProject）へ副作用を適用する。openpyxl版の _ops.py と
同じ「OP_HANDLERS辞書 + apply_op」の構造だが、COM操作には Workbook と VBProject
の両方が要るためシグネチャが異なり、ファイルも分離している。

run_script からは直接実行されない。edit_vba.py から import して使う。
"""

from __future__ import annotations

import json
import re

# 既存データ・ファイルの破壊やシステムへの不正アクセスにほぼ確実に使われる
# VBAの低レベルAPI呼び出しを検出するためのパターン（大文字小文字を区別しない）。
# 正当な用途（一時ファイルの削除等）であっても、このスキル経由での生成は
# 一律禁止する（フォールバックのバックアップ保存では救えない種類の危険な
# 操作のため）。FileCopy はバックアップ作成に必要なため意図的に除外している。
_DANGEROUS_CODE_PATTERNS = [
    (re.compile(r"\bKill\b\s*\S", re.IGNORECASE), "Kill (deleting files)"),
    (re.compile(r"\bRmDir\b", re.IGNORECASE), "RmDir (deleting folders)"),
    (re.compile(r"\bShell\b\s*\S", re.IGNORECASE), "Shell (running external processes)"),
    (re.compile(r"wscript\.shell", re.IGNORECASE), "WScript.Shell (running external commands)"),
    (re.compile(r"\.DeleteFile\b", re.IGNORECASE), "DeleteFile (deleting files)"),
    (re.compile(r"\.DeleteFolder\b", re.IGNORECASE), "DeleteFolder (deleting folders)"),
]

# run_macro実行前チェック用: 対話セッションの無い自動化実行下でダイアログを
# 誰も閉じられずrun_scriptのタイムアウト（既定300秒）までハングし、
# Excelプロセスがファイルを開いたまま残留する原因になることを2026-08-22に
# 3件（CreateButtons/ImportCSVs/DeleteCharts、いずれも末尾にMsgBoxを含む）
# 実機で確認済みのブロッキングUI API。
_BLOCKING_UI_PATTERNS = [
    (re.compile(r"\bMsgBox\b", re.IGNORECASE), "MsgBox"),
    (re.compile(r"\bInputBox\b", re.IGNORECASE), "InputBox"),
]


def _check_dangerous_code(code: str) -> None:
    """既存データの破壊やシステムへの不正アクセスに使われる危険なVBA APIを
    検出したら書き込み自体を拒否する。

    完全な悪意判定はできない（技術的に不可能）ため、正当な理由が思いつき
    にくい低レベルAPI呼び出しのみを対象にした簡易ガード。バックアップ保存の
    フォールバックでは救えない種類の操作（ファイル/フォルダ削除、外部コマンド
    実行）に限定し、Save/Delete（ワークシート・セル等）のような正当な用途が
    多い操作は対象外にしている（そちらはSKILL.mdのポリシーで「事前にバック
    アップを取ってから実行する」ことを別途要求する）。
    """
    if not code:
        return
    for pattern, label in _DANGEROUS_CODE_PATTERNS:
        if pattern.search(code):
            raise ValueError(
                f"This code cannot be written because it contains a dangerous VBA API ({label}). "
                "This skill cannot generate code that deletes files/folders or runs external commands. "
                "Tell the user to handle it manually in Excel after confirming explicit permission and the purpose with them."
            )


# --- 簡易構文lint ---
# VBAオブジェクトモデルには「コンパイル成否を返すAPI」が存在せず、COM経由での
# 実コンパイルチェック（ダミーSub実行、CommandBars経由のCompile実行、保存後の
# 再オープン等）はいずれも実効性が確認できなかったため、機械的に判定できる
# 範囲（Sub/End Sub、If/End If等のブロック構文の対応関係）だけを正規表現で
# 検証する。取りこぼし（#If等の条件付きコンパイル、1行に複数ステートメントを
# 並べるコロン複文、未宣言変数の重複宣言や型不一致等の意味的エラー）はあるが、
# 正しいコードを誤って拒否する方向のリスクを避けることを優先した設計。
_STRING_LITERAL_RE = re.compile(r'"(?:[^"]|"")*"')
_LINE_CONTINUATION_RE = re.compile(r"[ \t]_[ \t]*$")

_BLOCK_OPENERS = [
    (re.compile(r"^(?:Public\s+|Private\s+|Friend\s+|Static\s+)*Sub\s+\w", re.IGNORECASE), "Sub", "End Sub"),
    (re.compile(r"^(?:Public\s+|Private\s+|Friend\s+|Static\s+)*Function\s+\w", re.IGNORECASE), "Function", "End Function"),
    (re.compile(r"^(?:Public\s+|Private\s+|Friend\s+)*Property\s+(?:Get|Let|Set)\s+\w", re.IGNORECASE), "Property", "End Property"),
    (re.compile(r"^If\b.*\bThen\s*$", re.IGNORECASE), "If", "End If"),
    (re.compile(r"^For\b", re.IGNORECASE), "For", "Next"),
    (re.compile(r"^Do\b", re.IGNORECASE), "Do", "Loop"),
    (re.compile(r"^With\b", re.IGNORECASE), "With", "End With"),
    (re.compile(r"^Select\s+Case\b", re.IGNORECASE), "SelectCase", "End Select"),
]

_TAG_DISPLAY_NAMES = {
    "Sub": "Sub",
    "Function": "Function",
    "Property": "Property",
    "If": "If",
    "For": "For",
    "Do": "Do",
    "With": "With",
    "SelectCase": "Select Case",
}

_BLOCK_CLOSERS = [
    (re.compile(r"^End\s+Sub\b", re.IGNORECASE), "Sub", "End Sub"),
    (re.compile(r"^End\s+Function\b", re.IGNORECASE), "Function", "End Function"),
    (re.compile(r"^End\s+Property\b", re.IGNORECASE), "Property", "End Property"),
    (re.compile(r"^End\s+If\b", re.IGNORECASE), "If", "End If"),
    (re.compile(r"^Next\b", re.IGNORECASE), "For", "Next"),
    (re.compile(r"^Loop\b", re.IGNORECASE), "Do", "Loop"),
    (re.compile(r"^End\s+With\b", re.IGNORECASE), "With", "End With"),
    (re.compile(r"^End\s+Select\b", re.IGNORECASE), "SelectCase", "End Select"),
]


def _sanitize_line(line: str) -> str:
    """1物理行から文字列リテラル・コメント・行継続記号を取り除いた判定用テキストを返す。

    VBAの文字列リテラルは物理行をまたげない言語仕様のため、行単位の除去で
    取りこぼしがない。
    """
    if re.match(r"^\s*rem\b", line, re.IGNORECASE):
        return ""
    text = _STRING_LITERAL_RE.sub(lambda m: " " * len(m.group()), line)
    comment_idx = text.find("'")
    if comment_idx != -1:
        text = text[:comment_idx]
    return _LINE_CONTINUATION_RE.sub("", text)


def _iter_logical_lines(code: str):
    """行継続（末尾" _"）で連結された論理行を (開始物理行番号, 判定用テキスト) で列挙する。"""
    physical_lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    buffer_parts: list[str] = []
    start_no = None
    for i, raw_line in enumerate(physical_lines, start=1):
        if start_no is None:
            start_no = i
        buffer_parts.append(_sanitize_line(raw_line))
        if _LINE_CONTINUATION_RE.search(raw_line):
            continue
        yield start_no, " ".join(buffer_parts).strip()
        buffer_parts = []
        start_no = None
    if buffer_parts:
        yield start_no, " ".join(buffer_parts).strip()


def _syntax_error_message(detail: str) -> str:
    return (
        f"The VBA code may have invalid syntax ({detail}). "
        "Check that blocks such as Sub/End Sub, If/End If and For/Next still match up, "
        "review the whole code you plan to write with read_vba.py, and send it again. "
        "Note that this check is a simple one that looks only at matching block syntax "
        "(conditional compilation such as #If, and several statements on one line separated by colons, "
        "are not covered)."
    )


_INVALID_CONTINUE_RE = re.compile(r"\bContinue\s+(Do|For|While)\b", re.IGNORECASE)


def _lint_vba_syntax(code: str) -> None:
    """Sub/End Sub、If/End If等のブロック構文の対応関係だけを検証する簡易lint。

    実コンパイルチェック相当の検証はCOM経由では実現できなかったため、正規表現で
    機械的に判定できる範囲（ブロック構文の対応関係）に留める。
    """
    if not code:
        return
    stack: list[tuple[str, str, int]] = []
    for line_no, text in _iter_logical_lines(code):
        if not text:
            continue
        if _INVALID_CONTINUE_RE.search(text):
            raise ValueError(
                f"Line {line_no}: 'Continue Do/For/While' is not VBA syntax "
                "(confused with VB.NET etc.). To skip the rest of a loop, wrap the rest of the processing in an If "
                "with the condition inverted, or use GoTo and a label."
            )
        closer = next((c for c in _BLOCK_CLOSERS if c[0].search(text)), None)
        if closer is not None:
            _, expected_tag, closer_label = closer
            if not stack:
                raise ValueError(_syntax_error_message(
                    f"There is a '{closer_label}' with no matching opening block near line {line_no}"
                ))
            top_tag, _, top_line = stack[-1]
            if top_tag != expected_tag:
                raise ValueError(_syntax_error_message(
                    f"The '{_TAG_DISPLAY_NAMES[top_tag]}' block started near line {top_line} "
                    f"was not closed before '{closer_label}' appeared near line {line_no}"
                ))
            stack.pop()
            continue
        opener = next((o for o in _BLOCK_OPENERS if o[0].search(text)), None)
        if opener is not None:
            _, tag, closer_label = opener
            stack.append((tag, closer_label, line_no))

    if stack:
        top_tag, _, top_line = stack[-1]
        raise ValueError(_syntax_error_message(
            f"No closing keyword matches the '{_TAG_DISPLAY_NAMES[top_tag]}' block started near line {top_line}"
        ))


def _project_replacement(code_module, start_line: int, count_lines: int, new_code: str) -> str:
    """既存コードの[start_line, start_line+count_lines)をnew_codeに置き換えた
    場合の全文を、実際には書き込まずに文字列として組み立てる（lint用）。
    """
    total_lines = code_module.CountOfLines
    full_code = code_module.Lines(1, total_lines) if total_lines else ""
    full_lines = full_code.split("\r\n") if full_code else []
    before = full_lines[: start_line - 1]
    after = full_lines[start_line - 1 + count_lines :]
    new_lines = new_code.split("\n") if new_code else [""]
    return "\r\n".join(before + new_lines + after)


# vbext_ComponentType（遅延バインディングのため定数名でなく整数値を直書きする）
_VBEXT_CT_STD_MODULE = 1
_VBEXT_CT_CLASS_MODULE = 2
_VBEXT_CT_MSFORM = 3
_VBEXT_CT_DOCUMENT = 100

_ADD_MODULE_TYPES = {
    "standard": _VBEXT_CT_STD_MODULE,
    "class": _VBEXT_CT_CLASS_MODULE,
}


def _find_component(vb_project, name: str):
    for comp in vb_project.VBComponents:
        if comp.Name == name:
            return comp
    names = [comp.Name for comp in vb_project.VBComponents]
    raise KeyError(f"Module not found: {name!r} (existing modules: {names})")


_LEADING_ATTRIBUTE_RE = re.compile(r"^(?:Attribute\s+\S+\s*=.*\r?\n)+")


def _strip_leading_attribute_lines(code: str) -> str:
    """モジュール冒頭の`Attribute VB_Name = "..."`等の宣言行を取り除く。

    read_vba.py（oletools）はこの行を`code`に含めて返すが、AddFromStringは
    Attribute行を特別扱いせず生テキストとして挿入するため、そのまま書き戻すと
    VBEが自動生成するAttribute（comp.Name由来）と重複してしまう
    （例: `Attribute VB_Name = "X"`が2行になる）。
    """
    return _LEADING_ATTRIBUTE_RE.sub("", code, count=1)


def _replace_code(code_module, new_code: str) -> None:
    """CodeModuleの中身を新しいコードで全文置換する。

    AddFromStringは既存コードがある状態で呼ぶと末尾に追記されてしまうため、
    全文置換には使えない。既存行を全削除してから新しいコードを流し込む。
    """
    line_count = code_module.CountOfLines
    if line_count > 0:
        code_module.DeleteLines(1, line_count)
    if new_code:
        code_module.AddFromString(_strip_leading_attribute_lines(new_code))


def _to_json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def op_add_module(workbook, vb_project, op: dict):
    name = op["name"]
    mtype = op.get("type", "standard")
    if mtype not in _ADD_MODULE_TYPES:
        raise ValueError(
            f"type supports only standard/class (use set_code on an existing module for document; "
            f"form is out of scope for this skill): {mtype!r}"
        )
    if any(comp.Name == name for comp in vb_project.VBComponents):
        raise ValueError(f"Module '{name}' already exists (overwrite it with set_code or delete it with delete_module)")
    code = op.get("code", "")
    _check_dangerous_code(code)
    _lint_vba_syntax(code)
    comp = vb_project.VBComponents.Add(_ADD_MODULE_TYPES[mtype])
    comp.Name = name
    _replace_code(comp.CodeModule, code)
    return None


def op_set_code(workbook, vb_project, op: dict):
    comp = _find_component(vb_project, op["name"])
    if comp.Type == _VBEXT_CT_MSFORM:
        raise ValueError(f"'{op['name']}' is a UserForm (form module), so this skill cannot edit it")
    _check_dangerous_code(op["code"])
    _lint_vba_syntax(op["code"])
    _replace_code(comp.CodeModule, op["code"])
    return None


def op_find_replace(workbook, vb_project, op: dict):
    """モジュール内の一部コード（old_code）をnew_codeへ置換する。

    set_code と違いモジュール全文を渡す必要がなく、変更したい断片だけを
    やり取りするため、長いモジュールを低パラメータのLLMが扱う際の取りこぼし・
    誤生成のリスクを避けられる。old_codeがモジュール内で一意に一致しない
    （0件・複数件）場合はエラーにする（Editツールのold_string/new_stringと
    同じ考え方）。改行コードの違い（CRLF/LF）は吸収する。
    """
    comp = _find_component(vb_project, op["name"])
    if comp.Type == _VBEXT_CT_MSFORM:
        raise ValueError(f"'{op['name']}' is a UserForm (form module), so this skill cannot edit it")
    code_module = comp.CodeModule
    full_code = code_module.Lines(1, code_module.CountOfLines) if code_module.CountOfLines else ""
    full_normalized = full_code.replace("\r\n", "\n")
    old_normalized = str(op["old_code"]).replace("\r\n", "\n")
    if not old_normalized:
        raise ValueError("old_code cannot be an empty string")
    count = full_normalized.count(old_normalized)
    if count == 0:
        raise ValueError(
            f"old_code not found in module '{op['name']}'"
            " (copy it exactly, including indentation and line breaks, from the actual code obtained with read_vba.py)"
        )
    if count > 1:
        raise ValueError(
            f"old_code matched {count} places in module '{op['name']}'. "
            "Widen the range to include the surrounding lines until it identifies a single place"
        )
    new_normalized = str(op["new_code"]).replace("\r\n", "\n")
    _check_dangerous_code(new_normalized)
    new_full_code = full_normalized.replace(old_normalized, new_normalized, 1)
    _lint_vba_syntax(new_full_code)
    _replace_code(code_module, new_full_code)
    return None


# vbext_ProcKind（Property Get/Let/Set の判別に使う。標準Sub/Functionはpk_Proc）
_PROC_KIND_MAP = {
    "sub": 0,  # vbext_pk_Proc
    "property_let": 1,  # vbext_pk_Let
    "property_set": 2,  # vbext_pk_Set
    "property_get": 3,  # vbext_pk_Get
}
_PROC_KIND_AUTO_ORDER = (0, 3, 1, 2)


def op_replace_procedure(workbook, vb_project, op: dict):
    """モジュール内の指定したSub/Function/Propertyプロシージャ1つだけを丸ごと
    置き換える。COMのCodeModule.ProcStartLine/ProcCountLinesでプロシージャの
    正確な開始行・行数を取得してから、その範囲だけをDeleteLines+InsertLinesで
    差し替えるため、モジュールの他の部分には一切触れない。set_codeよりも
    LLMが用意するコード量を大きく減らせる。
    """
    comp = _find_component(vb_project, op["name"])
    if comp.Type == _VBEXT_CT_MSFORM:
        raise ValueError(f"'{op['name']}' is a UserForm (form module), so this skill cannot edit it")
    code_module = comp.CodeModule
    proc_name = op["procedure"]

    kind_arg = op.get("kind")
    if kind_arg is not None:
        if kind_arg not in _PROC_KIND_MAP:
            raise ValueError(f"kind supports only sub/property_get/property_let/property_set: {kind_arg!r}")
        kinds_to_try = (_PROC_KIND_MAP[kind_arg],)
    else:
        kinds_to_try = _PROC_KIND_AUTO_ORDER

    start_line = None
    count_lines = None
    for kind in kinds_to_try:
        try:
            start_line = code_module.ProcStartLine(proc_name, kind)
            count_lines = code_module.ProcCountLines(proc_name, kind)
            break
        except Exception:
            continue
    if start_line is None:
        raise ValueError(
            f"Procedure '{proc_name}' not found in module '{op['name']}'"
            " (for Property Get/Let/Set, specify it explicitly with kind)"
        )
    _check_dangerous_code(op["code"])
    assembled = _project_replacement(code_module, start_line, count_lines, op["code"])
    _lint_vba_syntax(assembled)
    code_module.DeleteLines(start_line, count_lines)
    code_module.InsertLines(start_line, op["code"])
    return None


def op_insert_code(workbook, vb_project, op: dict):
    """モジュールの既存コードには触れず、新しいコード（通常は新規のSub/Function）
    を末尾・先頭・指定行のいずれかに追加する。既存コードを一切やり取りしなくて
    よいため、モジュールへ新しいプロシージャを1つ追加したいだけの場合に最も
    LLMの負担が小さい。
    """
    comp = _find_component(vb_project, op["name"])
    if comp.Type == _VBEXT_CT_MSFORM:
        raise ValueError(f"'{op['name']}' is a UserForm (form module), so this skill cannot edit it")
    code_module = comp.CodeModule
    position = op.get("position", "end")
    if position == "end":
        line = code_module.CountOfLines + 1
    elif position == "start":
        line = 1
    elif isinstance(position, int) and position >= 1:
        line = position
    else:
        raise ValueError(f"position must be 'end'/'start' or a line number (integer) of 1 or more: {position!r}")
    _check_dangerous_code(op["code"])
    assembled = _project_replacement(code_module, line, 0, op["code"])
    _lint_vba_syntax(assembled)
    code_module.InsertLines(line, op["code"])
    return None


def op_delete_module(workbook, vb_project, op: dict):
    comp = _find_component(vb_project, op["name"])
    if comp.Type == _VBEXT_CT_DOCUMENT:
        raise ValueError(
            f"'{op['name']}' is a document module (ThisWorkbook/sheet module), so it cannot be deleted"
            " (to empty its code, pass an empty string with set_code)"
        )
    vb_project.VBComponents.Remove(comp)
    return None


def _find_procedure_code(vb_project, name: str) -> str | None:
    """run_macroの`name`（"Module.Proc" または "Proc"）からプロシージャ本文の
    ソースコードを取り出す。

    見つからない場合（該当モジュール・プロシージャなし、COM呼び出し失敗等）は
    Noneを返し、呼び出し側はブロッキングUIチェックを諦めて実行を続行する
    （フェイルオープン。ここでの目的はMsgBox/InputBoxによる既知のハングを
    防ぐことであり、lookup自体の失敗で正当な実行までブロックしないため）。
    """
    if "." in name:
        module_name, proc_name = name.split(".", 1)
        try:
            components = [_find_component(vb_project, module_name)]
        except KeyError:
            return None
    else:
        proc_name = name
        components = list(vb_project.VBComponents)

    for comp in components:
        code_module = comp.CodeModule
        for kind in _PROC_KIND_AUTO_ORDER:
            try:
                start_line = code_module.ProcStartLine(proc_name, kind)
                count_lines = code_module.ProcCountLines(proc_name, kind)
                return code_module.Lines(start_line, count_lines)
            except Exception:
                continue
    return None


def _check_blocking_ui(code: str, macro_name: str) -> None:
    """run_macroで呼ぶプロシージャにMsgBox/InputBoxが含まれていたら実行前に拒否する。

    含まれたまま実行すると、対話セッションの無い自動化実行下では誰もダイアログを
    閉じられずrun_scriptのタイムアウト（既定300秒）までハングし、その後も
    Excelプロセスがファイルを開いたまま残留して以後の編集操作を巻き込んで
    失敗させ続ける（2026-08-22に3件実機で確認、詳細はSKILL.md参照）。
    """
    for pattern, label in _BLOCKING_UI_PATTERNS:
        if pattern.search(code):
            raise ValueError(
                f"Running was aborted because macro '{macro_name}' contains {label}. "
                "In automated runs without an interactive session nobody can close the dialog, "
                "so it would hang until the run_script timeout (default 300 seconds) and leave the Excel process behind. "
                "Remove MsgBox/InputBox, or replace them with Debug.Print or a return value (for a Function), and run it again."
            )


def op_run_macro(workbook, vb_project, op: dict):
    name = op["name"]
    call_args = op.get("args", [])
    proc_code = _find_procedure_code(vb_project, name)
    if proc_code:
        _check_blocking_ui(proc_code, name)
    excel = workbook.Application
    try:
        result = excel.Run(f"'{workbook.Name}'!{name}", *call_args)
    except Exception as e:  # noqa: BLE001 - COMエラーはマクロ側のエラー内容により多様なため丸めて報告する
        raise ValueError(f"Failed to run macro '{name}': {e}")
    return _to_json_safe(result)


OP_HANDLERS = {
    "add_module": op_add_module,
    "set_code": op_set_code,
    "find_replace": op_find_replace,
    "replace_procedure": op_replace_procedure,
    "insert_code": op_insert_code,
    "delete_module": op_delete_module,
    "run_macro": op_run_macro,
}


def apply_op(workbook, vb_project, op: dict):
    op_name = op.get("op")
    handler = OP_HANDLERS.get(op_name)
    if handler is None:
        raise ValueError(f"Unsupported op: {op_name!r} (supported ops: {sorted(OP_HANDLERS)})")
    return handler(workbook, vb_project, op)
