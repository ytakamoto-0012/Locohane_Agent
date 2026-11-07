"""eval の作業フォルダに残った生成物を、一時フォルダが消える前に記録する。

作業フォルダ（run_case.py がフィクスチャを写した一時フォルダ）はケースの終了とともに
消えるため、生成された xlsx のフォント・行の高さ・印刷設定をファイル側から
確かめられなかった（2026-11-07 evals xlkit: transcript の報告文からしか判定できなかった）。
ここでは、実行前の一覧と比べて新しく作られた・変わったファイルを列挙し、xlsx は
判定に使う見た目の要約を付け、必要なら結果フォルダへ写す。
"""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

# 実行中の一時ファイル置き場（_tmp_<thread_id>/）の中は、中間の JSON 等が大量にあるので
# 文書ファイルだけを見る（worker が一時フォルダの写しを編集してから戻すことがある）
_TMP_DIR_PREFIX = "_tmp_"
_DOCUMENT_SUFFIXES = {".xlsx", ".xlsm", ".docx", ".pptx", ".pdf"}
_OFFICE_SUFFIXES = _DOCUMENT_SUFFIXES | {".csv", ".md", ".py"}


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """root 以下のファイルの {相対パス: (サイズ, 更新時刻ns)}。"""
    result: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        in_tmp = any(part.startswith(_TMP_DIR_PREFIX) for part in rel.parts[:-1])
        if in_tmp and path.suffix.lower() not in _DOCUMENT_SUFFIXES:
            continue
        stat = path.stat()
        result[rel.as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return result


def changed_files(root: Path, before: dict[str, tuple[int, int]]) -> list[str]:
    """before（実行前の snapshot）から新しく作られた・変わったファイルの相対パス。"""
    after = snapshot(root)
    return sorted(rel for rel, sig in after.items() if before.get(rel) != sig)


def describe_outputs(root: Path, rels: list[str], keep_dir: Path | None = None) -> list[dict]:
    """生成物の一覧。xlsx/xlsm には見た目の要約を付ける。keep_dir があればそこへ写す。"""
    out: list[dict] = []
    for rel in rels:
        path = root / rel
        entry: dict = {"path": rel, "size": path.stat().st_size}
        if path.suffix.lower() in {".xlsx", ".xlsm"} and ".bak_" not in path.name:
            try:
                entry["xlsx"] = summarize_xlsx(path)
            except Exception as e:  # noqa: BLE001 - 壊れたファイルも記録だけは残す
                entry["xlsx_error"] = f"{type(e).__name__}: {e}"
        if keep_dir is not None and path.suffix.lower() in _OFFICE_SUFFIXES:
            dest = keep_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            entry["kept"] = str(dest)
        out.append(entry)
    return out


def _display_width(text: str) -> int:
    """全角を2、半角を1と数えた幅（skills/office_shared/excel_common.py と同じ数え方）。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WFA" else 1 for ch in text)


def summarize_xlsx(path: Path) -> list[dict]:
    """シートごとのフォント名の内訳・高さ未設定の折返し行・印刷設定。"""
    from openpyxl import load_workbook

    wb = load_workbook(path)
    sheets = []
    for ws in wb.worksheets:
        fonts: Counter[str] = Counter()
        wrapped_rows_without_height: list[int] = []
        for row in ws.iter_rows():
            row_wraps = False
            for cell in row:
                if cell.value is None:
                    continue
                fonts[str(cell.font.name)] += 1
                if cell.alignment is not None and cell.alignment.wrap_text and isinstance(cell.value, str):
                    letter = cell.column_letter
                    width = ws.column_dimensions[letter].width or 8.43
                    # 1行に収まる文（見出し等）は高さ未設定でも見え方は変わらないので数えない
                    if "\n" in cell.value or _display_width(cell.value) > width:
                        row_wraps = True
            if row and row_wraps and ws.row_dimensions[row[0].row].height is None:
                wrapped_rows_without_height.append(row[0].row)
        props = ws.sheet_properties.pageSetUpPr
        sheets.append(
            {
                "sheet": ws.title,
                "max_row": ws.max_row,
                "max_column": ws.max_column,
                "fonts": dict(fonts.most_common()),
                "wrapped_rows_without_height": wrapped_rows_without_height[:30],
                "print": {
                    "orientation": ws.page_setup.orientation,
                    "fit_to_page": bool(props is not None and props.fitToPage),
                    "fit_to_width": ws.page_setup.fitToWidth,
                    "repeat_rows": ws.print_title_rows,
                },
                "merged_cells": len(ws.merged_cells.ranges),
            }
        )
    return sheets
