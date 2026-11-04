"""excel-py スキルの補助モジュール。`execute_python_code` で openpyxl を直接書くときに import する。

JSON の op を組み立てて edit_excel.py へ渡す方式（excel-edit）では、op の仕様
（row_styles の要素数・header_style で縞模様が自動で付く等）を覚える負担と、
意図しない上書き（2026-11-04 evals 001: 種別の色分けが縞模様で消えた）が
修正の往復を生んでいた。こちらは openpyxl をそのまま使わせ、見た目の統一
（見出し・罫線・縞模様・列幅・折り返し）と保存時の点検だけをここで引き受ける。

使い方（SKILL.md 参照）:

    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.environ["AGENT_SRC_DIR"]), "skills", "excel-py", "scripts"))
    from xlkit import new_workbook, open_workbook, write_table, write_note, merge_same, save

保存時の点検（lint）と状態要約は excel-edit の _lint.py を、配色テーマは
office_shared/office_theme.py を共有する（同じ基準で警告を出すため）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent
for _p in (_SKILLS_DIR / "office_shared", _SKILLS_DIR / "excel-edit" / "scripts"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

from excel_common import _display_width, backup_before_overwrite, exceeds_column_width, register_output_path  # noqa: E402
from office_theme import THEMES, resolve_theme  # noqa: E402

__all__ = ["THEMES", "Table", "fill", "merge_same", "new_workbook", "open_workbook", "save", "write_note", "write_table"]

_THIN = Side(style="thin")
BORDER = Border(top=_THIN, bottom=_THIN, left=_THIN, right=_THIN)
BAND_FILL = "F2F2F2"
_MAX_AUTO_WIDTH = 60


def fill(color: str) -> PatternFill:
    """RRGGBB の単色塗り。`cell.fill = fill("FFF2CC")` のように使う。"""
    return PatternFill(fill_type="solid", start_color=color, end_color=color)


def new_workbook() -> Workbook:
    """シートが1枚も無い新しいブック。最初に `wb.create_sheet(名前)` でシートを作る。"""
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def open_workbook(path) -> Workbook:
    """既存ファイルを開く。`.xlsm` はマクロを保ったまま開く（keep_vba=True）。"""
    path = Path(path)
    return load_workbook(path, keep_vba=path.suffix.lower() == ".xlsm")


@dataclass
class Table:
    """write_table が書いた範囲（行・列は1始まり）。"""

    title_row: int | None
    header_row: int | None
    first_row: int  # 本体の最初の行
    last_row: int  # 本体の最後の行
    first_col: int
    last_col: int

    def column_letter(self, offset: int) -> str:
        """表の左から offset 番目（0始まり）の列の文字。"""
        return get_column_letter(self.first_col + offset)


def write_table(
    ws,
    rows: list[list[object]],
    top_left: str = "A1",
    *,
    title: str | None = None,
    header: bool = True,
    theme: str | None = None,
    banded: bool = True,
    row_fills: list[str | None] | None = None,
    number_formats: dict[int, str] | None = None,
    freeze: bool = True,
) -> Table:
    """見出し付きの表を書いて体裁を整え、書いた範囲を返す。

    Args:
        ws: 書き込み先のシート。
        rows: 行のリスト。header=True なら rows[0] が見出し行。
        top_left: 表（title があればタイトル行）の左上のセル。
        title: 表の上に1行置くタイトル（表の幅で結合・太字）。
        header: rows[0] を見出しとして装飾するか。
        theme: 配色テーマ名（THEMES のキー。既定は濃紺の見出し）。
        banded: 本体の1行おきに薄い灰色を付けるか。row_fills で色を付けた行には付けない。
        row_fills: 本体の各行の塗り（RRGGBB または None）。要素数は本体の行数（見出しを除く）。
        number_formats: {列の位置(0始まり): 表示形式} 例 {3: "#,##0"}。
        freeze: 見出しの下で枠を固定するか。
    """
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

    col_letter, start_row = coordinate_from_string(top_left)
    first_col = column_index_from_string(col_letter)
    width = max((len(r) for r in rows), default=1)
    last_col = first_col + width - 1
    body_count = len(rows) - (1 if header and rows else 0)
    if row_fills is not None and len(row_fills) != body_count:
        raise ValueError(f"row_fills has {len(row_fills)} items but the table body has {body_count} rows (header excluded)")

    row = start_row
    title_row = None
    if title is not None:
        title_row = row
        cell = ws.cell(row=row, column=first_col, value=title)
        cell.font = Font(bold=True, size=12)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        if last_col > first_col:
            ws.merge_cells(start_row=row, start_column=first_col, end_row=row, end_column=last_col)
        row += 1

    colors = resolve_theme(theme) if theme else {"primary": "1F4E78", "text_on_primary": "FFFFFF"}
    header_row = None
    body = rows
    if header and rows:
        header_row = row
        for offset, value in enumerate(rows[0]):
            cell = ws.cell(row=row, column=first_col + offset, value=value)
            cell.font = Font(bold=True, color=colors["text_on_primary"])
            cell.fill = fill(colors["primary"])
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = BORDER
        body = rows[1:]
        row += 1

    first_row = row
    for index, values in enumerate(body):
        color = row_fills[index] if row_fills is not None else None
        if color is None and banded and index % 2 == 1:
            color = BAND_FILL
        for offset in range(width):
            value = values[offset] if offset < len(values) else None
            cell = ws.cell(row=row, column=first_col + offset, value=value)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="center")
            if color:
                cell.fill = fill(color)
            if number_formats and offset in number_formats:
                cell.number_format = number_formats[offset]
        row += 1
    last_row = row - 1

    _autofit(ws, rows, first_col)
    _wrap_overflowing(ws, header_row or first_row, last_row, first_col, last_col)
    if title_row is not None:
        _fit_title_height(ws, title_row, first_col, last_col, title)
    if freeze and header_row is not None:
        ws.freeze_panes = ws.cell(row=first_row, column=first_col).coordinate
    return Table(title_row, header_row, first_row, last_row, first_col, last_col)


def write_note(ws, row: int, text: str, first_col: int = 1, last_col: int | None = None, *, bold: bool = False) -> None:
    """表の外に置く1行の文（前提・注記・凡例）を書く。

    表の幅（first_col〜last_col）で結合し、折り返して、全文が見える行の高さにする。
    1セルに書くだけだと、列幅を超えた部分が切れて見えなかった
    （2026-11-04 evals 001: 前提・注記の行が A 列だけに入り、表示が切れて検証NG）。
    """
    last_col = last_col or first_col
    cell = ws.cell(row=row, column=first_col, value=text)
    cell.alignment = Alignment(vertical="top", wrap_text=True)
    if bold:
        cell.font = Font(bold=True)
    if last_col > first_col:
        ws.merge_cells(start_row=row, start_column=first_col, end_row=row, end_column=last_col)
    _fit_title_height(ws, row, first_col, last_col, text)


def merge_same(ws, column: str | int, first_row: int, last_row: int) -> list[str]:
    """列の中で同じ値が縦に続く部分を結合し、上下中央に揃える。結合した範囲を返す。

    月・分類などのグループ列を、行を組み立てた後でまとめて結合するのに使う
    （結合範囲を手で数えない）。値の無いセルは前の値の続きとはみなさない。
    """
    col = column if isinstance(column, int) else _column_index(column)
    letter = get_column_letter(col)
    merged: list[str] = []
    start = first_row
    for r in range(first_row + 1, last_row + 2):
        value = ws.cell(row=r, column=col).value if r <= last_row else object()
        if value is not None and value == ws.cell(row=start, column=col).value:
            continue
        if r - 1 > start and ws.cell(row=start, column=col).value is not None:
            ws.merge_cells(f"{letter}{start}:{letter}{r - 1}")
            merged.append(f"{letter}{start}:{letter}{r - 1}")
        anchor = ws.cell(row=start, column=col)
        anchor.alignment = Alignment(
            horizontal=anchor.alignment.horizontal, vertical="center", wrap_text=bool(anchor.alignment.wrap_text)
        )
        start = r
    return merged


def save(wb, path, *, new: bool = False) -> dict:
    """保存して点検する。結果（パス・シートごとの要約・警告）を JSON で表示して返す。

    Args:
        wb: 保存するブック。
        path: 保存先（作業ディレクトリの中）。
        new: 新規作成なら True。既存ファイルを上書きする場合（new=False）は、
            保存前に同じフォルダへ `<名前>.bak_<日時>` としてバックアップを取る。
            new=True で既にファイルがあればエラー（取り違えて上書きしないため）。
    """
    from _lint import lint_sheet, sheet_summary

    path = Path(path).resolve()
    if new and path.exists():
        raise FileExistsError(f"{path} already exists. Open it with open_workbook() and save with new=False to edit it")
    if path.suffix.lower() == ".xlsm" and getattr(wb, "vba_archive", None) is None:
        raise ValueError("Saving as .xlsm needs a workbook opened from an existing .xlsm with open_workbook() (keeps the macros)")
    if not wb.sheetnames:
        raise ValueError("The workbook has no sheets. Create one with wb.create_sheet(name)")
    backup = None if new else backup_before_overwrite(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)

    warnings: list[str] = []
    for ws in wb.worksheets:
        warnings.extend(lint_sheet(ws))
    result: dict = {"path": str(path), "sheets": [sheet_summary(ws) for ws in wb.worksheets]}
    if backup is not None:
        result["backup"] = str(backup)
    ref = register_output_path(path, description="excel-py output")
    if ref:
        result["ref"] = ref
    if warnings:
        result["warnings"] = warnings
    print(json.dumps(result, ensure_ascii=False))
    return result


def _column_index(letter: str) -> int:
    from openpyxl.utils import column_index_from_string

    return column_index_from_string(letter)


def _autofit(ws, rows: list[list[object]], first_col: int) -> None:
    """全角を2文字と数えた幅に合わせて列幅を広げる（狭めない・上限60）。"""
    widths: dict[str, float] = {}
    for values in rows:
        for offset, value in enumerate(values):
            letter = get_column_letter(first_col + offset)
            widths[letter] = max(widths.get(letter, 0.0), _display_width(value))
    for letter, width in widths.items():
        target = min(max(width + 2, 8), _MAX_AUTO_WIDTH)
        current = ws.column_dimensions[letter].width
        if current is None or target > current:
            ws.column_dimensions[letter].width = target


def _wrap_overflowing(ws, min_row: int, max_row: int, min_col: int, max_col: int) -> None:
    """列幅の上限でも収まらない文字列のセルに折り返しを付ける。"""
    for col in range(min_col, max_col + 1):
        width = ws.column_dimensions[get_column_letter(col)].width
        for r in range(min_row, max_row + 1):
            cell = ws.cell(row=r, column=col)
            if isinstance(cell.value, str) and not cell.alignment.wrap_text and exceeds_column_width(cell.value, width):
                cell.alignment = Alignment(
                    horizontal=cell.alignment.horizontal, vertical=cell.alignment.vertical, wrap_text=True
                )


def _fit_title_height(ws, row: int, min_col: int, max_col: int, text: str) -> None:
    """折り返したタイトルが隠れない行の高さにする（縮めない）。"""
    import math

    total = sum(ws.column_dimensions[get_column_letter(c)].width or 8.43 for c in range(min_col, max_col + 1))
    lines = max(1, math.ceil(_display_width(text) / max(total, 1)))
    height = min(15.0 * lines + 4, 409)
    current = ws.row_dimensions[row].height
    if current is None or height > current:
        ws.row_dimensions[row].height = height
