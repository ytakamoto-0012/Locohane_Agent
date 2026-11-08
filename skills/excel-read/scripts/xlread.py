"""excel-read スキルの補助モジュール。点検のために表を `execute_python_code_readonly` で読むときに import する。

検証役（verifier）は「全行がルールどおりか」を自分で openpyxl のコードを書いて確かめるが、
結合セルの2行目以降が None になる・見出し行の位置を数え違える、で点検コード自体が
何度も落ちていた（2026-11-07 evals xlkit: `regex.match(None)`・`parse_range(None)` の
TypeError が verifier の失敗の大半）。ここで表を「見出し → 値」の dict の列として返し、
結合セルの値は結合範囲のすべての行に入れる。

使い方（SKILL.md 参照）:

    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.environ["AGENT_SRC_DIR"]), "skills", "excel-read", "scripts"))
    from xlread import read_table
    for r in read_table(path, "週間予定表"):
        print(r["_row"], r["月"], r["週"], r["_fill"])
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["read_table"]


def read_table(path, sheet: str | int = 0, header_row: int | None = None, *, data_only: bool = False) -> list[dict]:
    """シートの表を、見出しをキーにした dict の列で返す。

    Args:
        path: xlsx/xlsm のパス。
        sheet: シート名、または 0 始まりの番号。
        header_row: 見出し行の行番号（1 始まり）。省略すると、値が2つ以上あり、1つの結合範囲
            （表のタイトル）ではない最初の行を見出しとみなす。
        data_only: 数式セルを、Excel が最後に計算した値で返す。

    Returns:
        本体の1行ごとに {見出し: 値, ..., "_row": 行番号, "_fill": 先頭セルの塗り "RRGGBB" か None}。
        結合セルの値は、結合範囲に含まれるすべての行に入る（2行目以降も None にならない）。
        同じ見出しが2つあれば、2つ目以降は "見出し_2" のように番号を付ける。
        見出しの下で、表の列がすべて空の行か、表の幅で1つに結合された行（注記）に来たら終わる。
    """
    from openpyxl import load_workbook

    wb = load_workbook(Path(path), data_only=data_only)
    ws = wb[sheet] if isinstance(sheet, str) else wb.worksheets[sheet]
    merged_value: dict[tuple[int, int], object] = {}
    single_row_spans: dict[int, list[tuple[int, int]]] = {}
    for rng in ws.merged_cells.ranges:
        value = ws.cell(row=rng.min_row, column=rng.min_col).value
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                merged_value[(r, c)] = value
        if rng.min_row == rng.max_row:
            single_row_spans.setdefault(rng.min_row, []).append((rng.min_col, rng.max_col))

    def value_at(r: int, c: int):
        return merged_value.get((r, c), ws.cell(row=r, column=c).value)

    max_row, max_col = ws.max_row or 0, ws.max_column or 0
    if header_row is None:
        header_row = _find_header_row(ws, max_row, max_col, single_row_spans)
        if header_row is None:
            return []

    columns = [c for c in range(1, max_col + 1) if ws.cell(row=header_row, column=c).value not in (None, "")]
    names: list[str] = []
    for c in columns:
        name = str(ws.cell(row=header_row, column=c).value).strip()
        base, n = name, 2
        while name in names:
            name = f"{base}_{n}"
            n += 1
        names.append(name)

    first, last = columns[0], columns[-1]
    records: list[dict] = []
    for r in range(header_row + 1, max_row + 1):
        values = [value_at(r, c) for c in columns]
        if all(v in (None, "") for v in values):
            break
        if any(start <= first and end >= last for start, end in single_row_spans.get(r, [])):
            break  # 表の幅で結合された行 = 表の後の注記
        record = dict(zip(names, values))
        record["_row"] = r
        record["_fill"] = _fill_rgb(ws.cell(row=r, column=first))
        records.append(record)
    return records


def _find_header_row(ws, max_row: int, max_col: int, single_row_spans: dict[int, list[tuple[int, int]]]) -> int | None:
    for r in range(1, max_row + 1):
        filled = [c for c in range(1, max_col + 1) if ws.cell(row=r, column=c).value not in (None, "")]
        if len(filled) < 2:
            continue
        if any(start <= filled[0] and end >= filled[-1] for start, end in single_row_spans.get(r, [])):
            continue
        return r
    return None


def _fill_rgb(cell) -> str | None:
    fill = cell.fill
    if fill is None or fill.fill_type != "solid":
        return None
    rgb = fill.fgColor.rgb if fill.fgColor is not None else None
    return str(rgb)[-6:] if isinstance(rgb, str) else None
