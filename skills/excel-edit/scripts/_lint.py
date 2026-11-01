"""edit_excel.py の保存後チェック（lint）と、触ったシートの適用後の状態要約。

検証役（verifier）が後から指摘していた「機械的に分かる不備」を、保存した直後に
作業役自身へ返して同じ実行のうちに直させるためのもの（2026-11-01 実害: 執行計画xlsxで
桁区切りの不揃い・折り返し漏れ・同じ値の未結合が検証NGになり、修正と再検証の往復に
45分以上かかった）。どれもエラーにはせず、結果JSONの `warnings` に載せるだけ。

run_script からは直接実行されない。edit_excel.py から import して使う。
"""

from __future__ import annotations

import sys
from pathlib import Path

_OFFICE_SHARED = Path(__file__).resolve().parent.parent.parent / "office_shared"
if str(_OFFICE_SHARED) not in sys.path:
    sys.path.append(str(_OFFICE_SHARED))
from excel_common import cell_column_width, exceeds_column_width  # noqa: E402

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
            'Apply set_style {"wrap_text": true} to them (or widen the column with set_column_width, up to 255)'
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
                'If they are the same kind of value, apply the same number_format with set_style'
            )

    # 結合されていない「同じ文字列が縦に2行以上続く」部分
    runs: list[str] = []
    for col in range(1, max_col + 1):
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
            f"{ws.title}: consecutive rows with the same value are not merged: {_join(runs)}. "
            "If the column is a grouping column (month, category, owner, etc.), merge each run with merge_cells "
            '+ set_style {"valign": "center"}; leave them as they are if each row is an independent value'
        )
    return warnings


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
    return summary
