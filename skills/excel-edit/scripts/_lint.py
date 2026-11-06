"""xlkit（excel-edit スキル）の `save()` が呼ぶ保存直前のチェック（lint）と、シートの状態要約。

検証役（verifier）が後から指摘していた「機械的に分かる不備」を、保存した直後に
作業役自身へ返して同じ実行のうちに直させるためのもの（2026-11-01 実害: 執行計画xlsxで
桁区切りの不揃い・折り返し漏れ・同じ値の未結合が検証NGになり、修正と再検証の往復に
45分以上かかった）。どれもエラーにはせず、結果JSONの `warnings` に載せるだけ。

run_script からは直接実行されない。xlkit から import して使う。
"""

from __future__ import annotations

import sys
from pathlib import Path

_OFFICE_SHARED = Path(__file__).resolve().parent.parent.parent / "office_shared"
if str(_OFFICE_SHARED) not in sys.path:
    sys.path.append(str(_OFFICE_SHARED))
from excel_common import _display_width, cell_column_width, exceeds_column_width  # noqa: E402

# 1種類の警告に並べる番地の最大数（多すぎると結果JSONが長くなり読み飛ばされるため）
_MAX_ITEMS = 15
# sheet_summary に載せる結合範囲の最大数
_MAX_MERGED = 60


def _compress(coords: list[tuple[int, int]]) -> list[str]:
    """(行, 列) の一覧を、同じ列で縦に連続する部分を "D4:D10" の形にまとめた番地の一覧にする。"""
    from openpyxl.utils import get_column_letter

    result: list[str] = []
    for col in sorted({c for _, c in coords}):
        rows = sorted(r for r, c in coords if c == col)
        start = prev = rows[0]
        for r in rows[1:] + [None]:
            if r is not None and r == prev + 1:
                prev = r
                continue
            letter = get_column_letter(col)
            result.append(f"{letter}{start}" if start == prev else f"{letter}{start}:{letter}{prev}")
            if r is not None:
                start = prev = r
    return result


def _join(items: list[str]) -> str:
    shown = ", ".join(items[:_MAX_ITEMS])
    return shown + (f" and {len(items) - _MAX_ITEMS} more" if len(items) > _MAX_ITEMS else "")


def _is_numeric_like(value: object) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float)) or (isinstance(value, str) and value.startswith("="))


def lint_sheet(ws) -> list[str]:
    """1シートを調べ、機械的に分かる不備を警告文の一覧で返す。"""
    from openpyxl.utils import get_column_letter

    merged_ranges = list(ws.merged_cells.ranges)
    merged_by_anchor = {(r.min_row, r.min_col): r for r in merged_ranges}
    in_merge = {(row, col) for r in merged_ranges for row in range(r.min_row, r.max_row + 1) for col in range(r.min_col, r.max_col + 1)}
    max_row, max_col = ws.max_row or 0, ws.max_column or 0
    warnings: list[str] = []

    overflow: list[tuple[int, int]] = []
    numeric_cells: dict[int, list] = {}
    for row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
        for cell in row:
            key = (cell.row, cell.column)
            if key in in_merge and key not in merged_by_anchor:
                continue
            value = cell.value
            if isinstance(value, str) and not cell.alignment.wrap_text:
                if exceeds_column_width(value, cell_column_width(ws, cell.row, cell.column, merged_by_anchor.get(key))):
                    overflow.append(key)
            if _is_numeric_like(value):
                numeric_cells.setdefault(cell.column, []).append(cell)

    if overflow:
        warnings.append(
            f"{ws.title}: text may be cut off (no wrap_text and wider than the column) in {_join(_compress(overflow))}. "
            "Set cell.alignment = Alignment(wrap_text=True) on them (xlkit sets the row height on save), "
            "or widen the column with ws.column_dimensions[letter].width (up to 255)"
        )

    # 同じ列の数値・数式セルで、表示形式（桁区切り等）が付いたセルと General のセルが混在している
    for col, cells in sorted(numeric_cells.items()):
        formats = {c.number_format for c in cells if c.number_format and c.number_format != "General"}
        general = [(c.row, c.column) for c in cells if not c.number_format or c.number_format == "General"]
        if formats and general:
            shown_formats = ", ".join(repr(f) for f in sorted(formats))
            warnings.append(
                f"{ws.title}: number formats are inconsistent in column {get_column_letter(col)} "
                f"(other numeric/formula cells use {shown_formats}, but {_join(_compress(general))} are General). "
                "If they are the same kind of value, give them the same cell.number_format "
                "(or pass number_formats= to write_table)"
            )

    # 結合されていない「同じ文字列が縦に2行以上続く」部分。既に縦の結合がある列（＝グループ列）だけを見る。
    # 全列を見ると、備考・行事名など行ごとに独立した値まで毎回拾い、作業役が「意図どおり」と読み飛ばす
    # ノイズになっていた（2026-11-01 evals: 毎回この警告だけが残り、判断に推論を使わせていた）。
    grouping_cols = sorted({r.min_col for r in merged_ranges if r.min_col == r.max_col and r.max_row > r.min_row})
    runs: list[str] = []
    for col in grouping_cols:
        run_start = None
        prev_value = None
        for r in range(1, max_row + 2):
            value = ws.cell(row=r, column=col).value if r <= max_row and (r, col) not in in_merge else None
            if isinstance(value, str) and value and value == prev_value:
                continue
            if run_start is not None and r - run_start >= 2:
                letter = get_column_letter(col)
                runs.append(f"{letter}{run_start}:{letter}{r - 1}={prev_value!r}")
            run_start = r if isinstance(value, str) and value else None
            prev_value = value
    if runs:
        warnings.append(
            f"{ws.title}: in a grouping column that has merged cells elsewhere, consecutive rows with the same value are not merged: "
            f"{_join(runs)}. Call merge_same(ws, column, first_row, last_row) on that column after writing the table"
        )

    tall_notes = _lone_tall_notes(ws, in_merge)
    if tall_notes:
        warnings.append(
            f"{ws.title}: a long line of text is wrapped inside one narrow cell and makes a tall block: {_join(tall_notes)}. "
            "Write it with write_note(ws, row, text, last_col=t.last_col) so it spans the table width"
        )
    return warnings


# 1セルに折り返した文が、この行数以上の縦長になったら警告する
_TALL_NOTE_LINES = 4


def _lone_tall_notes(ws, in_merge: set[tuple[int, int]]) -> list[str]:
    """行にそのセルしか値が無いのに、結合せず狭い1列の中で何行にも折り返している文（注記・前提の行）。

    2026-11-07 evals xlkit run2: 週間予定表の前提の注記を A1（幅13）だけに書いて折り返し、
    行の高さ289の縦長のブロックになったと検証役が指摘した（write_note を使えば表の幅に広がる）。
    """
    import math

    from openpyxl.utils import get_column_letter

    max_col = ws.max_column or 0
    found: list[str] = []
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row or 0, max_col=max_col):
        values = [c for c in row if c.value not in (None, "")]
        if len(values) != 1:
            continue
        cell = values[0]
        if (cell.row, cell.column) in in_merge or not isinstance(cell.value, str) or not cell.alignment.wrap_text:
            continue
        if cell.border is not None and cell.border.left is not None and cell.border.left.style:
            continue  # 罫線のある表の中のセル（他の列がたまたま空の行）は注記ではない
        if cell.column >= max_col:
            continue  # 右に列が無い（広げる先が無い）
        width = cell_column_width(ws, cell.row, cell.column, None)
        lines = sum(max(1, math.ceil(_display_width(part) / max(width - 1, 1))) for part in cell.value.split("\n"))
        if lines >= _TALL_NOTE_LINES:
            found.append(f"{get_column_letter(cell.column)}{cell.row} (about {lines} lines)")
    return found


def sheet_summary(ws) -> dict:
    """適用後のシートの構造（結合範囲・設定済みの列幅など）。確認のための読み直しを省くために返す。"""
    from openpyxl.utils import column_index_from_string

    merged = [str(r) for r in sorted(ws.merged_cells.ranges, key=lambda r: (r.min_col, r.min_row))]
    widths = {letter: dim.width for letter, dim in ws.column_dimensions.items() if dim.width is not None}
    summary = {
        "sheet": ws.title,
        "max_row": ws.max_row or 0,
        "max_column": ws.max_column or 0,
        "merged_cells": merged[:_MAX_MERGED],
        "column_widths": dict(sorted(widths.items(), key=lambda item: column_index_from_string(item[0]))),
    }
    if len(merged) > _MAX_MERGED:
        summary["merged_cells_omitted"] = len(merged) - _MAX_MERGED
    if ws.freeze_panes:
        summary["freeze_panes"] = ws.freeze_panes
    setup = ws.page_setup
    props = ws.sheet_properties.pageSetUpPr
    if setup.orientation or (props is not None and props.fitToPage):
        printing = {"orientation": setup.orientation or "portrait"}
        if props is not None and props.fitToPage:
            printing["fit_to_pages_wide"] = setup.fitToWidth
        if ws.print_title_rows:
            printing["repeat_rows"] = ws.print_title_rows
        summary["print"] = printing
    return summary
