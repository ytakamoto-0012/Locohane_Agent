"""取り込み対象ファイルからテキストを取り出す。

skills/ 配下の読み取りスクリプト（read_pdf.py 等）は CLI として main() 内に実装されて
いて import できないため、取り込みに必要な本文だけを取り出す薄い実装をここに持つ。
ページ・シート・スライド単位に分けて返し、検索結果の出典（locator）に使う。
"""

from __future__ import annotations

from contextlib import contextmanager
import csv
from dataclasses import dataclass
import io
import locale
import os
from pathlib import Path
import re
import threading
import time
from typing import Collection, Iterator

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".md", ".txt", ".csv")

# PDFium（pypdfium2）は同じプロセスの複数スレッドからの同時呼び出しに対応していないため、PDF の本文の抽出と
# ページの画像化（render.py）はこのロックで1つずつ行う（子プロセス（[vector_db].ingest_processes）同士は並列に動く）。
PDFIUM_LOCK = threading.Lock()


@dataclass(frozen=True)
class Segment:
    """ファイル内の1区切り（ページ・シート・スライド等）の本文。

    Attributes:
        locator: ファイル内の位置（例: "p.3"、"シート: 集計"、"スライド2"）。区切りが無い形式は空文字。
        text: 本文。
    """

    locator: str
    text: str


class ExtractError(Exception):
    """対応していない形式、またはファイルを読めなかった。"""


class NotTextError(ExtractError):
    """text_extensions の形式だが、中身がテキストではなかった（バイナリ形式で保存されたファイル等）。"""


class ExtractStopped(ExtractError):
    """取り込みの「停止」を求められたため、本文の抽出・画像化を途中でやめた。"""


# 取り込みの「停止」を、準備中のファイルの本文の抽出・画像化へ伝える。ワーカースレッドでも子プロセス
# （[vector_db].ingest_processes）でも同じように伝わるよう、停止の印はファイル（取り込みの一時フォルダの中）にし、
# そのパスをスレッドごとに持つ（watch_stop_file）。巨大なファイルでも停止までに長くかからないよう、
# ページ・行・画像ごとに check_stopped() で確かめる。
_stop = threading.local()
# 停止の印のファイルを確かめる間隔（秒）。行ごとに呼んでも遅くならないよう、間隔を空けて確かめる。
_STOP_CHECK_SECONDS = 0.2


@contextmanager
def watch_stop_file(path: str | None) -> Iterator[None]:
    """この中で動く本文の抽出・画像化は、path のファイルができたら ExtractStopped で止まる（None なら止まらない）。"""
    previous = getattr(_stop, "path", None), getattr(_stop, "checked_at", 0.0)
    _stop.path, _stop.checked_at = path, 0.0
    try:
        yield
    finally:
        _stop.path, _stop.checked_at = previous


def stop_file() -> str | None:
    """このスレッドの watch_stop_file() の停止の印のファイルのパス（無ければ None。別のスレッドで見張るとき用）。"""
    return getattr(_stop, "path", None)


def stop_requested() -> bool:
    """watch_stop_file() の停止の印のファイルができているか（間隔を空けずに確かめる）。"""
    path = stop_file()
    return bool(path) and os.path.exists(path)


def check_stopped() -> None:
    """停止を求められていれば ExtractStopped を投げる（_STOP_CHECK_SECONDS 秒に1回だけファイルを確かめる）。"""
    if not getattr(_stop, "path", None):
        return
    now = time.monotonic()
    if now - _stop.checked_at < _STOP_CHECK_SECONDS:
        return
    _stop.checked_at = now
    if stop_requested():
        raise ExtractStopped("Stopped because the ingestion was stopped")


def extract_segments(path: Path, data: bytes | None = None, text_extensions: Collection[str] = ()) -> list[Segment]:
    """拡張子に応じてテキストを取り出す。本文が空の区切りは含めない。

    data にファイルの中身を渡すと、ファイルを読み直さずにそれを使う（取り込みで SHA-256 の計算と読み取りを1回にするため）。
    text_extensions（[vector_db].text_extensions）の拡張子は .txt と同じくテキストとして読む（対応形式の読み方が優先）。

    Raises:
        NotTextError: text_extensions の形式で、中身が NUL を含む（バイナリ）場合。
        ExtractError: 対応していない拡張子、またはファイルを読めなかった場合。
    """
    ext = path.suffix.lower()
    try:
        source = str(path) if data is None else io.BytesIO(data)
        if ext == ".pdf":
            segments = _extract_pdf(str(path) if data is None else data)
        elif ext == ".docx":
            segments = _extract_docx(source)
        elif ext in (".xlsx", ".xlsm"):
            segments = _extract_xlsx(source)
        elif ext == ".pptx":
            segments = _extract_pptx(source)
        elif ext == ".csv":
            segments = [Segment("", _csv_text(_decode_text(path.read_bytes() if data is None else data)))]
        elif ext in (".md", ".txt"):
            segments = [Segment("", _decode_text(path.read_bytes() if data is None else data))]
        elif ext in text_extensions:
            raw = path.read_bytes() if data is None else data
            # テキスト形式と同じ拡張子のバイナリ形式（保存時に形式を選べるアプリ）は、文字化けしたチャンクにしない。
            if b"\x00" in raw:
                raise NotTextError("Not a text file (contains binary data); only the file path is registered")
            segments = [Segment("", _decode_text(raw))]
        else:
            raise ExtractError(f"Unsupported format: {ext}")
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001 - 壊れたファイルは1件の失敗として扱い、取り込み全体は止めない
        if ext in _OOXML_EXTENSIONS and _is_encrypted_office(path, data):
            raise ExtractError("Cannot read because the file is password-protected (encrypted)") from e
        raise ExtractError(f"{type(e).__name__}: {e}") from e
    return [s for s in segments if s.text.strip()]


# Office Open XML（ZIP）の形式。パスワードを付けると ZIP ではなく OLE の複合ファイル（暗号化されたパッケージ）になる。
_OOXML_EXTENSIONS = (".docx", ".xlsx", ".xlsm", ".pptx")
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _is_encrypted_office(path: Path, data: bytes | None) -> bool:
    """docx・xlsx・pptx がパスワード付き（中身が OLE の複合ファイル）か。"""
    if data is not None:
        return data[:8] == _OLE_SIGNATURE
    try:
        with path.open("rb") as f:
            return f.read(8) == _OLE_SIGNATURE
    except OSError:
        return False


def _decode_text(data: bytes) -> str:
    """UTF-8（BOM 付き含む）→ OS の ANSI コードページ（日本語版 Windows なら cp932、英語版なら cp1252 等）
    → CP932 の順に読む（ANSI を先にする理由は src/tools/_file_tools_common.py の read_text_with_fallback）。"""
    for encoding in dict.fromkeys(("utf-8-sig", locale.getencoding(), "cp932")):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _csv_text(text: str) -> str:
    """CSV を Excel のセルと同じく、表の中の数値のセルを除いて（_without_table_numbers）1行ずつタブでつなぐ。

    CSV として読めない（csv モジュールの上限を超える長さのフィールド等）ときは、従来どおりテキストのまま返す。
    """
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error:
        return text
    return "\n".join(line for row in _without_table_numbers(rows) if (line := _row_text(row)))


def _extract_pdf(source: str | bytes) -> list[Segment]:
    """PDF のページごとの文字（PDFium は C の実装で、pypdf より大幅に速い）。

    空のパスワードで開ける暗号化 PDF（閲覧の制限だけのもの）はそのまま読める。
    """
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(source)
        try:
            segments = []
            for i in range(len(pdf)):
                check_stopped()
                page = pdf[i]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_bounded() or ""
                    finally:
                        textpage.close()
                finally:
                    page.close()
                segments.append(Segment(f"p.{i + 1}", normalize_newlines(text)))
            return segments
        finally:
            pdf.close()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


# Office Open XML の名前空間。
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_NS_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_THREADED = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
# 新しい形式（図形のテキストボックス等）と、古い Office 向けの代替（VML）を両方持つ要素。
# 代替（mc:Fallback）は同じ文字の複製のため読まない。
_NS_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_MC_FALLBACK = f"{{{_NS_MC}}}Fallback"
_MC_CHOICE = f"{{{_NS_MC}}}Choice"
_MC_ALTERNATE_CONTENT = f"{{{_NS_MC}}}AlternateContent"

# docx の本文以外で文字を読むパーツ（content type の末尾 → 本文に付ける見出し）。
_DOCX_EXTRA_PARTS = {
    "header+xml": "[Header]",
    "footer+xml": "[Footer]",
    "footnotes+xml": "[Footnote]",
    "endnotes+xml": "[Endnote]",
    "comments+xml": "[Comment]",
}


def _extract_docx(source) -> list[Segment]:
    """本文の段落・表に加えて、テキストボックス・ヘッダー/フッター・脚注/文末脚注・コメントの文字を取り出す。

    本文は文書の順に、コンテンツコントロール（w:sdt）の中や変更履歴で挿入した文字（w:ins）も読む
    （python-docx の Document.paragraphs・Paragraph.text はこれらを読まない）。
    本文以外の文字は本文の後ろに見出し（[Header] 等）を付けて並べる。同じヘッダー/フッターを複数の
    セクションで使っていても1回だけにする。
    """
    import docx
    from docx.oxml import parse_xml

    def paragraphs_text(element) -> list[str]:
        _drop_fallbacks(element)
        return [t for p in element.iter(f"{{{_NS_W}}}p") if (t := _docx_paragraph_text(p)).strip()]

    document = docx.Document(source)
    body = document.element.body
    _drop_fallbacks(body)
    lines = _docx_block_lines(body)
    seen: set[str] = set()

    def add(label: str, element) -> None:
        for text in paragraphs_text(element):
            line = f"{label} {text}"
            if line not in seen:
                seen.add(line)
                lines.append(line)

    # テキストボックスの中のテキストボックスは外側でも読むため、同じ行は seen で1回にする。
    for textbox in body.iter(f"{{{_NS_W}}}txbxContent"):
        add("[Text box]", textbox)
    for part in document.part.package.iter_parts():
        label = next((v for k, v in _DOCX_EXTRA_PARTS.items() if part.content_type.endswith("wordprocessingml." + k)), None)
        if label is not None:
            add(label, parse_xml(part.blob))
    return [Segment("", "\n".join(lines))]


def _w(name: str) -> str:
    return f"{{{_NS_W}}}{name}"


# 中身をそのまま親の位置にあるものとして読む要素（コンテンツコントロール・カスタム XML）。
_DOCX_WRAPPERS = (_w("sdt"), _w("sdtContent"), _w("customXml"))


def _docx_children(element):
    """element の子要素。コンテンツコントロール等の中は開いて、中の要素を子として返す。"""
    for child in element:
        if child.tag in _DOCX_WRAPPERS:
            yield from _docx_children(child)
        else:
            yield child


def _docx_block_lines(container) -> list[str]:
    """本文（またはセル）の段落と表の行を文書の順に並べる。表の行はセルをタブでつなぐ（入れ子の表はセルの中に改行で入れる）。"""
    lines = []
    for child in _docx_children(container):
        if child.tag == _w("p"):
            lines.append(_docx_paragraph_text(child))
        elif child.tag == _w("tbl"):
            for row in _docx_children(child):
                if row.tag == _w("tr"):
                    cells = ["\n".join(_docx_block_lines(cell)).strip() for cell in _docx_children(row) if cell.tag == _w("tc")]
                    lines.append("\t".join(cells))
    return lines


# 段落の文字として読む要素 → 文字（w:t は要素の文字）。改行（w:br）はページ・段区切りでなければ改行にする。
_DOCX_RUN_TEXT = {_w("tab"): "\t", _w("ptab"): "\t", _w("cr"): "\n", _w("noBreakHyphen"): "-"}
# 段落の文字として読まない要素（テキストボックスは [Text box] として別に読む。削除した文字は w:delText のため読まれない）。
_DOCX_SKIP = (_w("txbxContent"), _w("del"), _w("moveFrom"), _MC_FALLBACK)
# 画像の参照（DrawingML の a:blip と、古い形式（VML）の v:imagedata）。
_NS_VML = "urn:schemas-microsoft-com:vml"
_IMAGE_REFS = (f"{{{_NS_A}}}blip", f"{{{_NS_VML}}}imagedata")


def image_alt_text(element) -> str:
    """図形・画像（element）の代替テキスト（cNvPr・wp:docPr の descr、無ければ title。VML は alt）。無ければ空文字。"""
    for child in element.iter():
        if not isinstance(child.tag, str):
            continue  # lxml のコメント等
        local = child.tag.rsplit("}", 1)[-1]
        if local in ("docPr", "cNvPr"):
            text = (child.get("descr") or child.get("title") or "").strip()
            if text:
                return text
        elif local == "shape" and child.tag.startswith(f"{{{_NS_VML}}}"):
            text = (child.get("alt") or "").strip()
            if text:
                return text
    return ""


def has_image(element) -> bool:
    """element の中に画像の参照があるか。"""
    return any(child.tag in _IMAGE_REFS for child in element.iter())


def _docx_paragraph_text(paragraph) -> str:
    """段落（w:p）の文字。変更履歴で挿入した文字（w:ins）・段落内のコンテンツコントロール・フィールドの表示文字も読む。

    画像（w:drawing・w:pict）に代替テキストがあれば、その位置に "[Image: 代替テキスト]" を入れる。
    """
    parts: list[str] = []

    def walk(element) -> None:
        for child in element:
            tag = child.tag
            if tag == _w("t"):
                parts.append(child.text or "")
            elif tag == _w("br"):
                if child.get(_w("type")) in (None, "textWrapping"):
                    parts.append("\n")
            elif tag in _DOCX_RUN_TEXT:
                parts.append(_DOCX_RUN_TEXT[tag])
            elif tag not in _DOCX_SKIP:
                if tag in (_w("drawing"), _w("pict")) and has_image(child) and (alt := image_alt_text(child)):
                    parts.append(f"[Image: {alt}]")
                walk(child)

    walk(paragraph)
    return "".join(parts)


def _drop_fallbacks(root) -> None:
    """root の下の mc:Fallback（同じ内容の古い形式の複製）を取り除く（lxml・ElementTree のどちらの要素でも使える）。"""
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag == _MC_FALLBACK:
                parent.remove(child)


def _extract_xlsx(source) -> list[Segment]:
    """シートごとのセルの値に、図形（テキストボックス等）・グラフのタイトル・コメントの文字を加える。

    図形はアンカー（左上が乗っているセル）の行、コメントは付いているセルの行の後ろに、セル番地を付けて
    差し込む（例: "[Shape C5] 承認欄"）。近くのセルと同じチャンクに入り、検索結果でも位置が分かるようにするため。
    セルではなく位置で置いた図形はシートの末尾に付ける。グラフシートはグラフのタイトルだけの区切りにする。

    表の中に縦に並ぶ数値のセル（月別の数量・金額等）は検索の手がかりにならず、チャンクを増やすだけのため取り込まない
    （_without_table_numbers）。

    セルの値は python-calamine（Rust の実装で、openpyxl より大幅に速い）で読む。値の入った範囲が大きすぎるシートが
    あるブック（_fits_calamine）と、calamine で読めなかったブックは openpyxl で読む。
    """
    try:
        extras = _xlsx_sheet_extras(source)
    except Exception:  # noqa: BLE001 - 図形・コメントが読めなくても、セルの値は取り込む
        extras = {}
    if _fits_calamine(source):
        try:
            return _xlsx_segments(_calamine_sheets(source), extras)
        except BaseException as e:  # noqa: BLE001 - Rust 側の panic は BaseException の PanicException になる
            # 取り込みの停止（ExtractStopped）は読めなかったのではないため、openpyxl で読み直さずにそのまま止める。
            if isinstance(e, ExtractStopped) or (not isinstance(e, Exception) and type(e).__name__ != "PanicException"):
                raise
    return _xlsx_segments(_openpyxl_sheets(source), extras)


def _xlsx_segments(sheets, extras: dict[str, dict[int, list[str]]]) -> list[Segment]:
    """sheets（シート名, 1行目からの各行のセルの値）ごとに、セルの行と図形・コメントの行（extras）を並べた区切り。"""
    segments = []
    for name, rows in sheets:
        notes = dict(extras.get(name, {}))
        lines = []
        for row_number, row in enumerate(_without_table_numbers(rows), start=1):
            check_stopped()
            if text := _row_text(row):
                lines.append(text)
            lines.extend(notes.pop(row_number, []))
        # データの最後の行より下に置いた図形・コメントと、位置で置いた図形（行 0 として集めたもの）。
        for row_number in sorted(notes, key=lambda r: (r == 0, r)):
            lines.extend(notes[row_number])
        segments.append(Segment(f"Sheet: {name}", "\n".join(lines)))
    return segments


# 数値だけの文字列（"1,234"・"-12.5"・"50%"・"¥1,000"・"100円"）。先頭が 0 の整数（"007"）は ID として扱い、含めない。
_NUMBER_TEXT = re.compile(r"[+\-−]?[¥￥$]?(?:\d{1,3}(?:,\d{3})+|0|[1-9]\d*)(?:\.\d+)?(?:%|円)?")
# 数値でも残す列の見出し（No・ID・社員ID・品番・番号・図番・コード 等）。英字は前後が英字でないものだけ（"Note" は含めない）。
_ID_HEADER = re.compile(r"(?<![A-Za-z])(?:id|no|code|key)(?![A-Za-z])|#|番|コード|キー|識別|ＩＤ|Ｎｏ", re.IGNORECASE)


def _is_number(value) -> bool:
    """数値のセルか（bool・日付・時刻は含めない）。"""
    if isinstance(value, str):
        return _NUMBER_TEXT.fullmatch(value.strip()) is not None
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numeric_columns(values) -> tuple[int, list[bool]]:
    """1行の (末尾の空のセルを除いた列数, 各列が数値か)。"""
    end = len(values)
    while end and values[end - 1] in (None, ""):
        end -= 1
    return end, [_is_number(v) for v in values[:end]]


def _without_table_numbers(rows):
    """表の中の数値のセル（同じ列の真上か真下のセルも数値）を None にした行を、行の数を変えずに返す。

    1行だけの「項目名＋値」（"品番 | 10"）は表ではないため残す。ID らしい見出し（_ID_HEADER）の文字のセルより下の
    数値も、検索の手がかりになるため残す（空の行で表が切れるまで。途中に "A100" のような数値でない値が混じっても
    ID の列のままにする）。日付・時刻は数値として扱わない。
    """
    # ID らしい見出しが出てきた列（空の行で表が切れたら忘れる）。
    id_columns: set[int] = set()
    above: list[bool] = []
    rows = iter(rows)
    row = next(rows, None)
    current = _numeric_columns(row) if row is not None else (0, [])
    while row is not None:
        below_row = next(rows, None)
        below = _numeric_columns(below_row) if below_row is not None else (0, [])
        end, numeric = current
        if all(v is None or (isinstance(v, str) and not v.strip()) for v in row[:end]):
            id_columns.clear()
        kept = None
        for col in range(end):
            if not numeric[col]:
                if isinstance(row[col], str) and _ID_HEADER.search(row[col]):
                    id_columns.add(col)
                continue
            in_table = (col < len(above) and above[col]) or (col < below[0] and below[1][col])
            if in_table and col not in id_columns:
                if kept is None:
                    kept = list(row)
                kept[col] = None
        yield row if kept is None else kept
        above, row, current = numeric, below_row, below


def _row_text(values) -> str:
    """1行のセルの値をタブでつなぐ。空のセル（空白だけを含む）は空にし、末尾の空のセルは付けない。値の無い行は空文字。

    途中の空のセルは、列の位置が分かるようにタブを残す。
    """
    end = len(values)
    while end and values[end - 1] in (None, ""):  # 文字列にする前に、末尾の空のセルを落とす
        end -= 1
    cells = [_cell_text(v) for v in values[:end]]
    while cells and not cells[-1]:
        cells.pop()
    return "\t".join(cells)


def _cell_text(value) -> str:
    if value is None:
        return ""
    # calamine は数値を float で返すため、整数は openpyxl と同じく "1.0" ではなく "1" にする
    # （1e15 以上は Excel が指数表記で保存する桁のため float のまま）。
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    text = str(value)
    return text if text.strip() else ""


def _calamine_sheets(source):
    """python-calamine でシートごとに (シート名, 1行目・A列からの各行のセルの値) を返す（空のセルは ""）。
    グラフシート等のセルの無いシートは行を返さない。"""
    from python_calamine import CalamineWorkbook, SheetTypeEnum

    if isinstance(source, io.BytesIO):
        source.seek(0)
        workbook = CalamineWorkbook.from_filelike(source)
    else:
        workbook = CalamineWorkbook.from_path(source)
    try:
        for sheet in workbook.sheets_metadata:
            if sheet.typ != SheetTypeEnum.WorkSheet:
                yield sheet.name, ()
                continue
            # skip_empty_area=False で A1 から並べる（行の位置で図形・コメントを差し込むため）。
            yield sheet.name, workbook.get_sheet_by_name(sheet.name).to_python(skip_empty_area=False)
    finally:
        workbook.close()


def _openpyxl_sheets(source):
    """openpyxl の読み取り専用モードでシートごとに (シート名, 1行目からの各行のセルの値) を返す。"""
    from openpyxl.chartsheet import Chartsheet

    workbook = _load_workbook(source)
    try:
        for name in workbook.sheetnames:
            sheet = workbook[name]
            if isinstance(sheet, Chartsheet):  # グラフシートにはセルが無い
                yield name, ()
                continue
            # 使用範囲（<dimension ref>）を使わない。読み取り専用モードは各行を使用範囲の最大列まで埋めて返すため、
            # 書式を最終行・最終列まで付けたブック（A1:XFD1048576）では 105万行 × 16384列 を回って終わらなくなる。
            # 使わなければ、各行は実際にあるセルの列までになる（途中の抜けた行は空になる）。
            sheet.reset_dimensions()
            yield name, sheet.iter_rows(min_row=1, values_only=True)
    finally:
        workbook.close()


# calamine で読むシートの大きさの上限（A1 から値の入った最後の行・列までのセル数）。calamine はシートを
# 値の入った範囲の長方形で持つため（1セル 32 バイト程度）、離れたセル（例: A1 と XFD100000）に値があると
# 巨大なメモリーを確保しようとし、確保できないとプロセスごと終了する。上限を超えるブックは openpyxl で読む。
_CALAMINE_MAX_CELLS = 10_000_000
# 値（v）・数式（f）・インライン文字列（is）を持つセル（<c r="C5" t="s"><v>…）の列の文字・行番号。
# 書式だけのセル（<c r="C5" s="1"/>）は含めない。セルごとに Python の処理を挟まないよう、findall で片方だけを取り出す。
_VALUE_CELL = rb'<(?:\w+:)?c\s[^>]*?\br="{}"[^>]*>\s*<(?:\w+:)?(?:v|f|is)[\s>/]'
_VALUE_CELL_COLUMN = re.compile(_VALUE_CELL.replace(b"{}", rb"([A-Z]{1,3})\d+"))
_VALUE_CELL_ROW = re.compile(_VALUE_CELL.replace(b"{}", rb"[A-Z]{1,3}(\d+)"))


def _fits_calamine(source) -> bool:
    """どのシートも、値の入った範囲が _CALAMINE_MAX_CELLS 以下か（調べられなければ False）。

    使用範囲（<dimension>）は使わず、値のあるセルから数える。書式を最終行・最終列まで付けたブックは使用範囲が
    A1:XFD1048576 と大きすぎ、逆に使用範囲を "A1" とだけ書くツールで作ったブックは小さすぎて、離れたセルを
    calamine に渡してしまうため。
    """
    import posixpath
    from xml.etree import ElementTree
    import zipfile

    try:
        with zipfile.ZipFile(source) as package:
            names = set(package.namelist())
            rels_name = "xl/_rels/workbook.xml.rels"
            if rels_name not in names:
                return False
            parts = [
                posixpath.normpath(posixpath.join("xl", target)) if not target.startswith("/") else target.lstrip("/")
                for rel in ElementTree.fromstring(package.read(rels_name)).iter(f"{{{_NS_PKG_REL}}}Relationship")
                if rel.get("Type", "").endswith("/worksheet") and (target := rel.get("Target", ""))
            ]
            for part in parts:
                if part in names and _value_area(package, part) > _CALAMINE_MAX_CELLS:
                    return False
        return True
    except Exception:  # noqa: BLE001 - 調べられないブックは openpyxl で読む
        return False


def _value_area(package, part: str) -> int:
    """シート（part）の A1 から値の入った最後の行・列までのセル数。"""
    xml = package.read(part)
    columns = set(_VALUE_CELL_COLUMN.findall(xml))
    if not columns:
        return 0
    last_col = max(_cell_position(c.decode() + "1")[1] for c in columns)
    # 行は上から順に並ぶため、末尾から広げながら探した最後の値のセルの行を使う（書式だけの行が末尾に続くブックもある）。
    window = 1 << 20
    while True:
        start = max(len(xml) - window, 0)
        rows = _VALUE_CELL_ROW.findall(xml, start)
        if rows or start == 0:
            last_row = max((int(r) for r in rows), default=0)
            return last_row * (last_col + 1)
        window *= 4


def _load_workbook(source):
    """openpyxl の読み取り専用モードでブックを開く。

    openpyxl は関係（_rels）を持たないグラフシート（グラフを置いていないもの等）で AttributeError になるため、
    そのときは空の関係を足した複製で開き直す。
    """
    import openpyxl
    import zipfile

    if isinstance(source, io.BytesIO):
        source.seek(0)
    try:
        return openpyxl.load_workbook(source, read_only=True, data_only=True)
    except AttributeError:
        if isinstance(source, io.BytesIO):
            source.seek(0)
        with zipfile.ZipFile(source) as package:
            names = package.namelist()
            missing = [
                f"xl/chartsheets/_rels/{name.rsplit('/', 1)[1]}.rels"
                for name in names
                if name.startswith("xl/chartsheets/") and name.count("/") == 2 and name.endswith(".xml")
            ]
            missing = [name for name in missing if name not in names]
            if not missing:
                raise
            patched = io.BytesIO()
            with zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as copy:
                for item in package.infolist():
                    copy.writestr(item, package.read(item.filename))
                for name in missing:
                    copy.writestr(name, f'<Relationships xmlns="{_NS_PKG_REL}"/>')
    return openpyxl.load_workbook(patched, read_only=True, data_only=True)


def _xlsx_sheet_extras(source) -> dict[str, dict[int, list[str]]]:
    """シート名 → 行番号（1始まり。位置で置いた図形は 0）→ その行の後ろに差し込む図形・コメントの行。

    openpyxl の読み取り専用モードは図形・コメントを読まないため、xlsx（ZIP）の XML を直接読む。
    読めないパーツは飛ばす（セルの値の取り込みは続ける）。
    """
    import posixpath
    from xml.etree import ElementTree
    import zipfile

    with zipfile.ZipFile(source) as package:
        names = set(package.namelist())

        def read(name: str):
            if name not in names:
                return None
            try:
                return ElementTree.fromstring(package.read(name))
            except ElementTree.ParseError:
                return None

        def rels(part: str) -> list[tuple[str, str, str]]:
            """part の関係（種類の末尾（"drawing" 等）, 参照先のパーツ名, 関係の ID）。"""
            directory, base = posixpath.split(part)
            root = read(posixpath.join(directory, "_rels", base + ".rels"))
            if root is None:
                return []
            result = []
            for rel in root.iter(f"{{{_NS_PKG_REL}}}Relationship"):
                if rel.get("TargetMode") == "External":
                    continue
                target = rel.get("Target", "")
                name = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(directory, target))
                result.append((rel.get("Type", "").rsplit("/", 1)[-1], name, rel.get("Id", "")))
            return result

        workbook = read("xl/workbook.xml")
        if workbook is None:
            return {}
        targets = {rel_id: name for _kind, name, rel_id in rels("xl/workbook.xml")}
        extras: dict[str, dict[int, list[str]]] = {}
        for sheet in workbook.iter(f"{{{_NS_S}}}sheet"):
            part = targets.get(sheet.get(f"{{{_NS_R}}}id", ""))
            if part is None:
                continue
            notes: dict[int, list[tuple[int, str]]] = {}
            sheet_rels = rels(part)
            threaded_tag = f"{{{_NS_THREADED}}}threadedComment"
            threaded = [read(name) for kind, name, _ in sheet_rels if kind == "threadedComment"]
            # Excel はスレッド形式のコメントに、古いバージョン向けの複製を古い形式のコメントとしても書くため、
            # スレッド形式のコメントがあるセルの古い形式のコメントは読まない（それ以外のセルの古い形式は「メモ」として読む）。
            threaded_refs = {c.get("ref", "").upper() for root in threaded if root is not None for c in root.iter(threaded_tag)}
            for kind, name, _ in sheet_rels:
                if kind == "drawing":
                    _xlsx_drawing_notes(read, rels, name, notes)
                elif kind == "comments":
                    # 古い形式の本文は <text><r><t>…</t></r></text> または <text><t>…</t></text>。
                    _xlsx_comment_notes(
                        read(name), f"{{{_NS_S}}}comment", lambda c: "".join(t.text or "" for t in c.iter(f"{{{_NS_S}}}t")), notes,
                        skip_refs=threaded_refs,
                    )
            for root in threaded:
                _xlsx_comment_notes(root, threaded_tag, lambda c: c.findtext(f"{{{_NS_THREADED}}}text", ""), notes)
            if notes:
                extras[sheet.get("name", "")] = {row: [line for _col, line in sorted(items)] for row, items in notes.items()}
        return extras


def _cell_name(row: int, col: int) -> str:
    """0始まりの行・列からセル番地（例: 0, 2 → "C1"）。"""
    letters = ""
    col += 1
    while col:
        col, rest = divmod(col - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return f"{letters}{row + 1}"


def _cell_position(ref: str) -> tuple[int, int]:
    """セル番地（例: "C5"）→ (行 1始まり, 列 0始まり)。読めなければ (0, 0)。"""
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    digits = "".join(ch for ch in ref if ch.isdigit())
    col = 0
    for ch in letters:
        col = col * 26 + ord(ch) - ord("A") + 1
    return (int(digits), max(col - 1, 0)) if digits else (0, 0)


def _xlsx_drawing_notes(read, rels, drawing_part: str, notes: dict[int, list[tuple[int, str]]]) -> None:
    """図形（テキストボックス・オートシェイプ・グループ内の図形）の文字とグラフのタイトルを notes へ加える。"""
    root = read(drawing_part)
    if root is None:
        return
    _drop_fallbacks(root)
    charts = {rel_id: name for kind, name, rel_id in rels(drawing_part) if kind == "chart"}
    for anchor in _drawing_anchors(root):
        start = anchor.find(f"{{{_NS_XDR}}}from")
        row = col = None
        if start is not None:
            row, col = _int_text(start, f"{{{_NS_XDR}}}row"), _int_text(start, f"{{{_NS_XDR}}}col")
        if row is not None and col is not None:
            row_number, label = row + 1, f"[Shape {_cell_name(row, col)}]"
        else:
            # xdr:absoluteAnchor（セルではなく位置で置いた図形）か、セルの位置が読めない図形。
            row_number, col, label = 0, 0, "[Shape]"
        texts = [_paragraphs_text(body) for body in anchor.iter(f"{{{_NS_XDR}}}txBody")]
        for chart_ref in anchor.iter(f"{{{_NS_C}}}chart"):
            chart = read(charts.get(chart_ref.get(f"{{{_NS_R}}}id", ""), ""))
            if chart is not None:
                texts.extend(_chart_titles(chart))
        for text in texts:
            if text.strip():
                notes.setdefault(row_number, []).append((col, f"{label} {text}"))
        # 画像（xdr:pic）の代替テキスト（例: "[Image C5] 製品の外観写真"）。
        for picture in anchor.iter(f"{{{_NS_XDR}}}pic"):
            if alt := image_alt_text(picture):
                notes.setdefault(row_number, []).append((col, f"{label.replace('Shape', 'Image', 1)} {alt}"))


def _drawing_anchors(root):
    """図形の一覧（xdr:wsDr）の各アンカー。新しい形式の図形（スライサー・フォームコントロール等）は
    アンカーごと mc:AlternateContent に包まれるため、その mc:Choice の中のアンカーを返す（mc:Fallback は取り除き済み）。"""
    for child in root:
        if child.tag == _MC_ALTERNATE_CONTENT:
            choice = child.find(_MC_CHOICE)
            if choice is not None:
                yield from choice
        else:
            yield child


def _int_text(element, tag: str) -> int | None:
    """element の子要素 tag の文字を 0 以上の整数として読む（無い・読めなければ None）。"""
    try:
        value = int((element.findtext(tag) or "").strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _chart_titles(chart) -> list[str]:
    """グラフ（c:chartSpace）のタイトル・軸のタイトルの文字（lxml・ElementTree のどちらの要素でも使える）。

    直接入力したタイトルは a:t、セルを参照するタイトル（c:strRef）は保存されているセルの値（c:strCache の c:v）から読む。
    """
    _drop_fallbacks(chart)
    titles = []
    for title in chart.iter(f"{{{_NS_C}}}title"):
        text = _paragraphs_text(title) or "".join(v.text or "" for v in title.iter(f"{{{_NS_C}}}v"))
        if text.strip():
            titles.append(text)
    return titles


def _paragraphs_text(element) -> str:
    """DrawingML の段落（a:p）ごとの文字を改行でつなぐ。"""
    lines = ["".join(t.text or "" for t in p.iter(f"{{{_NS_A}}}t")) for p in element.iter(f"{{{_NS_A}}}p")]
    return "\n".join(line for line in lines if line.strip())


def _xlsx_comment_notes(
    root, tag: str, text_of, notes: dict[int, list[tuple[int, str]]], skip_refs: set[str] = frozenset()
) -> None:
    """コメント（tag の要素。本文は text_of(要素) で取り出す）の文字を、付いているセルの行の notes へ加える。

    skip_refs のセル（大文字のセル番地）のコメントは読まない。
    """
    if root is None:
        return
    for comment in root.iter(tag):
        ref = comment.get("ref", "")
        if ref.upper() in skip_refs:
            continue
        text = text_of(comment)
        if text.strip():
            row_number, col = _cell_position(ref)
            notes.setdefault(row_number, []).append((col, f"[Comment {ref}] {text.strip()}"))


def _extract_pptx(source) -> list[Segment]:
    from pptx import Presentation

    presentation = Presentation(source)
    segments = []
    for i, slide in enumerate(presentation.slides, start=1):
        check_stopped()
        lines = _pptx_shape_lines(slide.shapes)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text
            if notes.strip():
                lines.append(f"[Notes] {notes}")
        segments.append(Segment(f"Slide {i}", "\n".join(lines)))
    return segments


def _pptx_shape_lines(shapes) -> list[str]:
    """図形の文字・表のセル・グラフのタイトル・画像の代替テキストを並べる。グループ化した図形は中の図形も（入れ子でも）読む。"""
    from xml.etree import ElementTree

    from pptx.shapes.graphfrm import GraphicFrame
    from pptx.shapes.group import GroupShape
    from pptx.shapes.picture import Picture

    lines = []
    for shape in shapes:
        # shape_type は種類の分からない図形で例外になるため、型で判定する。
        if isinstance(shape, GroupShape):
            lines.extend(_pptx_shape_lines(shape.shapes))
            continue
        if isinstance(shape, Picture) and (alt := image_alt_text(shape._element)):
            lines.append(f"[Image] {alt}")
        if shape.has_text_frame:
            lines.append(shape.text_frame.text)
        if isinstance(shape, GraphicFrame) and shape.has_table:
            for row in shape.table.rows:
                lines.append("\t".join(cell.text.strip() for cell in row.cells))
        if isinstance(shape, GraphicFrame) and shape.has_chart:
            try:
                chart = ElementTree.fromstring(shape.chart_part.blob)
            except Exception:  # noqa: BLE001 - グラフのパーツが壊れていても、スライドの文字は取り込む
                continue
            lines.extend(_chart_titles(chart))
    return lines
