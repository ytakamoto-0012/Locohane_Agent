"""excel-edit スキルの補助モジュール。`execute_python_code` で openpyxl を直接書くときに import する。

見た目の統一（見出し・罫線・縞模様・列幅・折り返し・グループ列の結合）と、保存時の
フォント名・折返し行の高さ・点検（lint）をここで引き受ける。モデルが書くのはデータの
組み立てとロジックだけなので、同じ内容を1セル1命令のJSONとして並べるより payload が
小さくなる（2026-11-06 決定論比較: 同一内容で script 766 chars / op list 5,391 chars）。

使い方（SKILL.md 参照）:

    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.environ["AGENT_SRC_DIR"]), "skills", "excel-edit", "scripts"))
    from xlkit import new_workbook, open_workbook, write_table, write_note, merge_same, save

保存時の点検（lint）は同じフォルダの _lint.py、配色テーマは
office_shared/office_theme.py を共有する（excel-read と同じ基準で警告を出すため）。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent
_OFFICE_SHARED = _SKILLS_DIR / "office_shared"
if str(_OFFICE_SHARED) not in sys.path:
    sys.path.append(str(_OFFICE_SHARED))

from excel_common import _display_width, backup_before_overwrite, exceeds_column_width, register_output_path  # noqa: E402
from office_theme import THEMES, resolve_theme  # noqa: E402

_CJK_FONT = "Yu Gothic"
_DEFAULT_LATIN_FONTS = {None, "Calibri"}
_CJK_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

__all__ = ["THEMES", "Table", "fill", "merge_same", "new_workbook", "open_workbook", "save", "write_note", "write_table"]

_THIN = Side(style="thin")
BORDER = Border(top=_THIN, bottom=_THIN, left=_THIN, right=_THIN)
BAND_FILL = "F2F2F2"
_MAX_AUTO_WIDTH = 60


def fill(color: str) -> PatternFill:
    """RRGGBB の単色塗り。`cell.fill = fill("FFF2CC")` のように使う。"""
    return PatternFill(fill_type="solid", start_color=color, end_color=color)


def new_workbook() -> Workbook:
    """シートが1枚も無い新しいブック。最初に `wb.create_sheet(名前)` でシートを作る。

    xlkit で一から組み立てたブックだと分かる印を付ける。保存時の日本語フォントの判定は
    save() の new ではなくこの印で決める（2026-11-07 evals xlkit run3: 修正のため同じ
    スクリプトを実行し直した worker が、既にあるファイルへ new=False で保存したところ
    Yu Gothic が付かず Calibri に戻った）。
    """
    wb = Workbook()
    wb.remove(wb.active)
    wb._xlkit_built_new = True
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
    table = Table(title_row, header_row, first_row, last_row, first_col, last_col)
    tables = getattr(ws, "_xlkit_tables", None)
    if tables is None:
        tables = []
        ws._xlkit_tables = tables
    tables.append(table)
    return table


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
    replaced_own_output = False
    if new and path.exists():
        # 同じ会話で xlkit が保存したファイルは、修正のために同じスクリプトを実行し直した
        # 結果なので、そのまま作り直す（バックアップも取らない）。ユーザーのファイルは今までどおり拒否する。
        if not _is_own_output(path):
            raise FileExistsError(f"{path} already exists. Open it with open_workbook() and save with new=False to edit it")
        replaced_own_output = True
    if path.suffix.lower() == ".xlsm" and getattr(wb, "vba_archive", None) is None:
        raise ValueError("Saving as .xlsm needs a workbook opened from an existing .xlsm with open_workbook() (keeps the macros)")
    if not wb.sheetnames:
        raise ValueError("The workbook has no sheets. Create one with wb.create_sheet(name)")
    backup = None if new else backup_before_overwrite(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    finalize_sheets(wb, new=new)
    wb.save(path)

    warnings: list[str] = []
    for ws in wb.worksheets:
        warnings.extend(lint_sheet(ws))
    result: dict = {"path": str(path), "sheets": [sheet_summary(ws) for ws in wb.worksheets]}
    if backup is not None:
        result["backup"] = str(backup)
    if replaced_own_output:
        result["replaced"] = "rebuilt the file this conversation saved before"
    ref = register_output_path(path, description=_OUTPUT_DESCRIPTION)
    if ref:
        result["ref"] = ref
    if warnings:
        result["warnings"] = warnings
    print(json.dumps(result, ensure_ascii=False))
    return result


def finalize_sheets(wb, *, new: bool) -> None:
    """保存直前に、フォント名・折返し行の高さ・印刷設定を確定させる（書かなかった分を埋める）。

    openpyxl はフォント名を書かなければ Calibri のまま、折返しセルがあっても行の高さを
    設定しない。どちらも開くアプリ・レンダリングで見え方が変わり、点検（lint）と検証役が
    NG にして修正往復が増えていた（2026-11-06 bench001: 臂3回で日本語フォント 0/3、
    折返し行の高さ欠落の中央値 37.5）。ここで固定すれば recipe を覚えていなくても出る。
    明示的に設定したフォント名・行の高さ・印刷設定は変えない。

    日本語フォントにするのは、xlkit で一から組み立てたブック（new_workbook() の印か new=True）と、
    既存ブックでも名前の付いたフォントが1つも無い（openpyxl 等で作られ、誰もフォントを
    選んでいない）ブックに日本語がある場合。既存ブックに名前の付いたフォントがあれば、
    シート（無ければブック）で一番多い名前に揃える（元ファイルの見た目を変えない）。
    """
    built_new = new or bool(getattr(wb, "_xlkit_built_new", False))
    has_cjk = _workbook_has_cjk(wb)
    workbook_names = [name for ws in wb.worksheets for name in _named_fonts(ws)]
    if built_new:
        fallback = _CJK_FONT if has_cjk else None
    elif workbook_names:
        fallback = max(set(workbook_names), key=workbook_names.count)
    else:
        fallback = _CJK_FONT if has_cjk else "Calibri"
    for ws in wb.worksheets:
        _finalize_fonts(ws, _CJK_FONT if built_new and has_cjk else None, fallback)
        _fit_wrapped_row_heights(ws)
        _fit_print_setup(ws)


_OUTPUT_DESCRIPTION = "excel-edit output"


def _is_own_output(path: Path) -> bool:
    """この会話のパスメモリーに、xlkit の save() が保存したファイルとして登録済みか。"""
    import os

    src_dir = os.environ.get("AGENT_SRC_DIR")
    if not src_dir:
        return False
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        import path_memory
    except ImportError:
        return False
    thread_id, pm_dir, _ = path_memory.env_params()
    target = str(path.resolve())
    return any(
        entry.get("path") == target and entry.get("description") == _OUTPUT_DESCRIPTION
        for entry in path_memory.list_entries(thread_id, pm_dir)
    )


# 用紙（A4・余白 0.7 インチ）に 100% で入る列幅の合計の目安（列幅の単位で）。
_PORTRAIT_WIDTH = 94
_LANDSCAPE_WIDTH = 140


def _fit_print_setup(ws) -> None:
    """write_table で表を書いたシートの印刷設定を、表が読める形にそろえる（設定済みなら変えない）。

    openpyxl の既定（縦・100%）のままだと、列幅の広い表は横に何ページにも分かれて
    印刷される（2026-11-07 evals xlkit run2: 列幅60の列が2つある表が月間3頁・週間6頁に
    分かれると検証役が指摘）。横幅を1ページに収め（縦は成り行き）、表が縦向きの用紙に
    収まらない幅なら横向きにし、最初の表の見出し行を各ページの先頭に繰り返す。
    """
    tables = getattr(ws, "_xlkit_tables", None)
    if not tables:
        return
    props = ws.sheet_properties.pageSetUpPr
    if ws.page_setup.orientation is not None or ws.page_setup.fitToWidth is not None or (props is not None and props.fitToPage):
        return
    first_col = min(t.first_col for t in tables)
    last_col = max(t.last_col for t in tables)
    width = _print_width(ws, first_col, last_col)
    ws.page_setup.orientation = "landscape" if width > _PORTRAIT_WIDTH else "portrait"
    if props is None:
        from openpyxl.worksheet.properties import PageSetupProperties

        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    else:
        props.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    header_row = tables[0].header_row
    if header_row is not None and not ws.print_title_rows:
        ws.print_title_rows = f"{header_row}:{header_row}"


def _print_width(ws, first_col: int, last_col: int) -> float:
    return sum(ws.column_dimensions[get_column_letter(c)].width or 8.43 for c in range(first_col, last_col + 1))


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


def _workbook_has_cjk(wb) -> bool:
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and _CJK_RE.search(cell.value):
                    return True
    return False


def _named_fonts(ws) -> list[str]:
    """シートのセルに明示的に付いたフォント名（既定の Calibri・名前なしを除く）。"""
    return [
        cell.font.name
        for row in ws.iter_rows()
        for cell in row
        if cell.font is not None and cell.font.name not in _DEFAULT_LATIN_FONTS
    ]


def _finalize_fonts(ws, cjk_font: str | None, fallback: str | None) -> None:
    """openpyxl が名前を書かなかったフォント（既定の Calibri）に名前を付ける。

    cjk_font（xlkit で組み立てた日本語のブック）があればそれへ、無ければそのシートで明示的に
    名前の付いたフォントの中で一番多い名前に、それも無ければ fallback（finalize_sheets が
    ブック全体から決めた名前）に揃える。名前の付いたフォント（元ファイル由来のもの）は変えない。
    既定フォントはモデルが選んだ名前ではないため、Calibri のまま残るとシート内でそのセルだけ
    違う名前に見える。
    """
    from copy import copy

    names = _named_fonts(ws)
    target = cjk_font or (max(set(names), key=names.count) if names else (fallback or "Calibri"))
    for row in ws.iter_rows():
        for cell in row:
            font = cell.font
            if font is None or font.name not in _DEFAULT_LATIN_FONTS or font.name == target:
                continue
            if cell.value is None and not cell.has_style:
                continue
            new_font = copy(font)
            new_font.name = target
            if new_font.size is None:
                new_font.size = 11
            cell.font = new_font


def _fit_wrapped_row_heights(ws) -> None:
    """高さ未設定の行のうち、折り返すセルがある行に、収まる行数分の高さを付ける（明示した高さは変えない）。"""
    import math

    merged_width: dict[tuple[int, int], int] = {}
    covered: set[tuple[int, int]] = set()
    for rng in ws.merged_cells.ranges:
        if rng.min_row != rng.max_row:
            # 縦に結合したセルは複数行に広がるので、1行の高さを決める材料にしない
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    covered.add((r, c))
            continue
        merged_width[(rng.min_row, rng.min_col)] = rng.max_col
        for c in range(rng.min_col + 1, rng.max_col + 1):
            covered.add((rng.min_row, c))

    def col_width(col: int) -> float:
        return ws.column_dimensions[get_column_letter(col)].width or 8.43

    for row in ws.iter_rows():
        if not row:
            continue
        r = row[0].row
        if ws.row_dimensions[r].height is not None:
            continue
        lines_needed = 1
        size = 11.0
        for cell in row:
            if (r, cell.column) in covered or not isinstance(cell.value, str) or not cell.alignment.wrap_text:
                continue
            end_col = merged_width.get((r, cell.column), cell.column)
            width = sum(col_width(c) for c in range(cell.column, end_col + 1))
            usable = max(width - 1, 1)
            lines = sum(max(1, math.ceil(_display_width(part) / usable)) for part in cell.value.split("\n"))
            if lines > lines_needed:
                lines_needed = lines
                size = float(cell.font.size or 11) if cell.font is not None else 11.0
        if lines_needed > 1:
            ws.row_dimensions[r].height = min(round(lines_needed * size * 1.35 + 3, 1), 409)
