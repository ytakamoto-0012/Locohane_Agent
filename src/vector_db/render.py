"""取り込み対象ファイルをページ単位の画像（PNG）にする（[vector_db].image_input = true のとき）。

render 系スキル（skills/docx-render 等の _render.py）と同じく、Office ファイルは
OLE（COM）で Word / PowerPoint / Excel をヘッドレス起動して PDF へ書き出し、
PDF のページを pypdfium2 で画像化する。スキルのスクリプトは CLI として作られていて
import できないため、取り込みに必要な部分だけの薄い実装をここに持つ。
画像ファイル（HEIC/HEIF を含む）は Pillow で開き、PNG にそろえる。

Office ファイルの画像化には Windows と Microsoft Office（pywin32）が必要。
"""

from __future__ import annotations

from contextlib import contextmanager
import csv
from dataclasses import dataclass
import io
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable
import uuid

from ..config import PAGE_IMAGE_EXTENSIONS
from .extract import PDFIUM_LOCK, ExtractError, ExtractStopped, check_stopped, normalize_newlines, stop_file

# ページごとに画像にできる文書の形式（実際に画像にする形式は [vector_db].image_input_extensions で選ぶ）。
PAGE_EXTENSIONS = PAGE_IMAGE_EXTENSIONS
# そのまま1枚の画像として取り込む画像ファイルの形式（image_input = true のときだけ取り込み対象になる）。
IMAGE_FILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif")
# image_input = true のときに画像で埋め込む形式。
IMAGE_EXTENSIONS = PAGE_EXTENSIONS + IMAGE_FILE_EXTENSIONS

# 複数ページを持てる画像形式（スキャンした文書の TIFF 等）。それ以外は先頭の1枚だけ使う。
_MULTI_PAGE_IMAGE_EXTENSIONS = (".tif", ".tiff")
# A4 の長辺（インチ）。画像ファイルは、A4 の文書を image_dpi で画像化したときの長辺まで縮小する。
_A4_LONG_SIDE_INCHES = 297 / 25.4

# 画像の PNG の圧縮レベル（0〜9）。埋め込みサーバーで展開されるだけで画質は変わらないため、
# サイズより速さを優先する（既定の 6 より大幅に速い）。
_PNG_COMPRESS_LEVEL = 1

_PROG_ID = {
    ".docx": "Word.Application",
    ".pptx": "PowerPoint.Application",
    ".xlsx": "Excel.Application",
    ".xlsm": "Excel.Application",
}


# PDF のページ番号（1始まり）と総ページ数から、ファイル内の位置（locator）を作る。
Locate = Callable[[int, int], str]


@dataclass(frozen=True)
class PageImage:
    """1ページ分の画像と、そのページから取り出した文字（検索結果の抜粋に使う）。

    Attributes:
        locator: ファイル内の位置（例: "p.3"、"スライド2"、"シート: 集計"）。
        text: ページの文字（PDF のテキスト層。無ければ空文字）。
        png: ページの PNG 画像（png_path に書き出したときは空）。
        png_path: render_pages() の out_dir に書き出した PNG のパス（書き出していなければ None）。
    """

    locator: str
    text: str
    png: bytes = b""
    png_path: Path | None = None

    def read_png(self) -> bytes:
        """PNG 画像（png_path に書き出していればファイルから読む）。"""
        return self.png_path.read_bytes() if self.png_path is not None else self.png


# PNG を書き出すときの通し番号を振る（同じ out_dir に複数の PDF（Excel のシートごと）から書くため）。
_PageWriter = Callable[[bytes], "tuple[bytes, Path | None]"]


def _page_writer(out_dir: Path | None) -> _PageWriter:
    """out_dir があれば PNG をそこへ書き出して (b"", パス) を、無ければ (PNG, None) を返す関数。"""
    counter = iter(range(1, 1_000_000_000))

    def write(png: bytes) -> tuple[bytes, Path | None]:
        if out_dir is None:
            return png, None
        png_path = out_dir / f"page_{next(counter):05d}.png"
        png_path.write_bytes(png)
        return b"", png_path

    return write


def render_pages(
    path: Path, dpi: int, *, out_dir: Path | None = None, office_timeout: float | None = None, temp_dir: str | None = None
) -> list[PageImage]:
    """path の全ページを dpi の解像度で画像にする。

    Args:
        out_dir: 指定すると各ページの PNG をこのフォルダへ書き出し、PageImage.png_path で返す
            （400ページの PDF 等でも全ページの画像をメモリーに持たないため。取り込みが使う）。
            省略時は PageImage.png にそのまま持つ。
        office_timeout: Office ファイルを PDF に書き出す1回の上限秒数。超えたら（見えない確認画面で
            止まった等）そのアプリのプロセスを終了して ExtractError にする。None なら待ち続ける。
        temp_dir: Office ファイルから書き出す PDF の一時フォルダを作る場所（取り込みは [vector_db].ingest_temp_dir の
            下のフォルダを渡す）。None ならシステムの一時フォルダ。

    Raises:
        ExtractError: 画像化に対応していない拡張子、Office が使えない、
            またはファイルを開けなかった場合。
    """
    ext = path.suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        raise ExtractError(f"Unsupported format for rendering: {ext}")
    write = _page_writer(out_dir)
    try:
        if ext == ".pdf":
            return _render_pdf(path, dpi, _page_locator, write)
        if ext in IMAGE_FILE_EXTENSIONS:
            return _render_image_file(path, dpi, write)
        with tempfile.TemporaryDirectory(prefix="locohane_vdb_", dir=temp_dir) as tmp:
            pages: list[PageImage] = []
            for pdf_path, locate in _export_office_to_pdf(path, Path(tmp), office_timeout):
                pages.extend(_render_pdf(pdf_path, dpi, locate, write))
            return pages
    except ExtractError:
        raise
    except ImportError as e:
        raise ExtractError(f"Rendering Office files requires Windows and Microsoft Office (pywin32): {e}") from e
    except Exception as e:  # noqa: BLE001 - 壊れたファイルは1件の失敗として扱い、取り込み全体は止めない
        raise ExtractError(f"Rendering failed: {type(e).__name__}: {e}") from e


def _render_pdf(pdf_path: Path, dpi: int, locate: Locate, write: _PageWriter) -> list[PageImage]:
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            total = len(pdf)
            pages = []
            for i in range(total):
                check_stopped()
                page = pdf[i]
                try:
                    text = page.get_textpage().get_text_bounded() or ""
                    buffer = io.BytesIO()
                    page.render(scale=dpi / 72).to_pil().save(buffer, format="PNG", compress_level=_PNG_COMPRESS_LEVEL)
                finally:
                    page.close()
                png, png_path = write(buffer.getvalue())
                pages.append(PageImage(locate(i + 1, total), normalize_newlines(text), png, png_path))
            return pages
        finally:
            pdf.close()


def _render_image_file(path: Path, dpi: int, write: _PageWriter) -> list[PageImage]:
    """画像ファイルを PNG にする。EXIF の回転を反映し、透過部分は白で埋め、長辺が大きすぎれば縮小する。

    複数ページの TIFF はページごとに分ける（位置は p.N）。1枚だけなら位置は「画像」。
    """
    import pillow_heif
    from PIL import Image, ImageOps, ImageSequence

    # HEIC/HEIF は Pillow 標準では開けないため、オープナーの登録が必要（何度呼んでもよい）。
    pillow_heif.register_heif_opener()
    max_long_side = round(_A4_LONG_SIDE_INCHES * dpi)
    with Image.open(path) as img:
        # ImageSequence.Iterator は同じ Image を seek し直して返すため、ページごとにその場で PNG にする
        # （list() で先に集めると、すべて最後のページになる）。
        frames = ImageSequence.Iterator(img) if path.suffix.lower() in _MULTI_PAGE_IMAGE_EXTENSIONS else [img]
        written = []
        for frame in frames:
            check_stopped()
            written.append(write(_to_png(ImageOps.exif_transpose(frame), max_long_side)))
    if len(written) == 1:
        return [PageImage("image", "", *written[0])]
    return [PageImage(f"p.{i}", "", png, png_path) for i, (png, png_path) in enumerate(written, start=1)]


def _to_png(img, max_long_side: int) -> bytes:
    from PIL import Image

    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, (255, 255, 255))
        img.paste(rgba, mask=rgba.getchannel("A"))
    elif img.mode != "RGB":
        img = img.convert("RGB")
    if max(img.size) > max_long_side:
        img = img.copy()
        img.thumbnail((max_long_side, max_long_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", compress_level=_PNG_COMPRESS_LEVEL)
    return buffer.getvalue()


def _page_locator(page: int, total: int) -> str:
    return f"p.{page}"


def _sheet_locator(sheet: str) -> Locate:
    """Excel のシート1枚分の PDF の位置。1ページに収まれば「シート: 名前」だけにする。"""
    return lambda page, total: f"Sheet: {sheet}" if total == 1 else f"Sheet: {sheet} p.{page}"


# Office のアプリ（Word / PowerPoint / Excel）をスレッドごとに起動したまま使い回すための状態。
# 1ファイルごとに起動・終了すると、それだけで数秒かかる。COM のオブジェクトは作ったスレッドでしか使えないため、
# スレッドごとに持つ。keep_office_apps() を呼んだスレッドだけ使い回し、それ以外は従来どおり毎回終了する。
_office = threading.local()


def keep_office_apps() -> None:
    """このスレッドで起動した Office のアプリを、close_office_apps() を呼ぶまで終了せずに使い回す。"""
    _office.keep = True


def close_office_apps() -> None:
    """このスレッドで起動したままの Office のアプリを終了する（無ければ何もしない）。"""
    apps = getattr(_office, "apps", None)
    _office.keep = False
    if apps is None:
        return
    import pythoncom

    for app in apps.values():
        _quit(app)
    _office.apps = None
    pythoncom.CoUninitialize()


def _quit(app) -> None:
    try:
        app.Quit()
    except Exception:  # noqa: BLE001 - 既に終了している等
        pass


def _office_app(prog_id: str):
    """prog_id の Office のアプリ（このスレッドで起動済みならそれ）と、新しく起動したかを返す。"""
    import pythoncom
    import win32com.client as win32

    apps = getattr(_office, "apps", None)
    if apps is None:
        pythoncom.CoInitialize()
        apps = _office.apps = {}
    app = apps.get(prog_id)
    if app is not None:
        return app, False
    app = win32.DispatchEx(prog_id)
    apps[prog_id] = app
    # PowerPoint は Application.Visible = False の設定自体が COM エラーになる（Open(WithWindow=False) で隠す）。
    if prog_id != "PowerPoint.Application":
        app.Visible = False
    app.DisplayAlerts = 0
    # 取り込み元のファイルのマクロ（.xlsm の Workbook_Open 等）を実行しない（msoAutomationSecurityForceDisable）。
    # COM で開いたときの既定はマクロを有効にする（msoAutomationSecurityLow）ため、明示して止める。
    app.AutomationSecurity = 3
    if prog_id == "Excel.Application":
        app.EnableEvents = False
    return app, True


# パスワード付きのファイルを開くときに渡すダミーのパスワード。渡さないと、見えないパスワード入力画面で
# 止まったままになる（パスワードの無いファイルでは無視される）。
_DUMMY_PASSWORD = "locohane-no-password"


class OfficeTimeout(ExtractError):
    """Office の PDF への書き出しが office_timeout 秒で終わらず、アプリのプロセスを終了した。"""


def _app_pid(prog_id: str, app) -> int | None:
    """app（DispatchEx で起動した Office のアプリ）のプロセス ID（分からなければ None）。

    Excel は Application.Hwnd、Word は一意のキャプションを付けてウィンドウを探す。PowerPoint は
    ウィンドウが無いと HWND を返さないが、1つの PC で1プロセスしか動かない（DispatchEx でも同じプロセスに
    つながる）ため、POWERPNT.EXE をプロセス名で探す。
    """
    import win32gui
    import win32process

    try:
        if prog_id == "Excel.Application":
            hwnd = int(app.Hwnd)
        elif prog_id == "Word.Application":
            caption = f"locohane-{uuid.uuid4().hex}"
            app.Caption = caption
            hwnd = win32gui.FindWindow("OpusApp", caption)
        else:
            return _find_process("POWERPNT.EXE")
        return win32process.GetWindowThreadProcessId(hwnd)[1] if hwnd else None
    except Exception:  # noqa: BLE001 - 分からなければ時間制限をかけない
        return None


def _find_process(image_name: str) -> int | None:
    """image_name（例: "POWERPNT.EXE"）のプロセス ID（無ければ None）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            # tasklist の出力は OEM コードページ（言語により異なる）。UTF-8 モードでも落ちないよう明示する。
            capture_output=True, text=True, encoding="oem", errors="replace", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for row in csv.reader(out.splitlines()):
        if len(row) >= 2 and row[0].lower() == image_name.lower() and row[1].isdigit():
            return int(row[1])
    return None


# Office の書き出しの間に、取り込みの停止の印（extract.watch_stop_file）を確かめる間隔（秒）。
_STOP_POLL_SECONDS = 0.5


@contextmanager
def _watchdog(pid: int | None, seconds: float | None, path: Path, stop_path: str | None = None):
    """seconds 秒を過ぎても抜けなければ pid のプロセスを終了し、抜けるときに OfficeTimeout を投げる。

    見えない確認画面（「前回開いた時に重大なエラーが発生しました」・パスワード入力等）で Office が止まると、
    COM の呼び出しが返らず取り込みが進まなくなるため。プロセスを終了すると止まっていた呼び出しはエラーで返る。
    stop_path（取り込みの停止の印のファイル）ができたときも pid のプロセスを終了し、ExtractStopped を投げる
    （巨大なファイルの書き出しを、取り込みの停止後も最後まで待たないため）。
    """
    if pid is None or not (seconds or stop_path):
        yield
        return
    done = threading.Event()
    fired: list[str] = []

    def watch() -> None:
        deadline = time.monotonic() + seconds if seconds else None
        while not done.wait(_STOP_POLL_SECONDS if stop_path else max(0.0, deadline - time.monotonic())):
            if stop_path and os.path.exists(stop_path):
                fired.append("stop")
            elif deadline is not None and time.monotonic() >= deadline:
                fired.append("timeout")
            else:
                continue
            try:
                os.kill(pid, signal.SIGTERM)  # Windows では TerminateProcess
            except OSError:
                pass
            return

    watcher = threading.Thread(target=watch, name="vector_db_office_watchdog", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        done.set()
        watcher.join()
        if fired == ["stop"]:
            raise ExtractStopped(f"Office export to PDF was aborted because the ingestion was stopped: {path.name}")
        if fired:
            raise OfficeTimeout(
                f"Office export to PDF did not finish within {seconds:g} seconds and was aborted (stopped at a confirmation dialog, etc.; "
                f"the limit can be changed with [vector_db] office_timeout_seconds in \"Admin settings\"): {path.name}"
            )


def _release_office_app(prog_id: str, *, broken: bool) -> None:
    """使い回さない（keep_office_apps() を呼んでいない）か、エラーで状態が分からなくなったアプリを終了する。"""
    apps = getattr(_office, "apps", None)
    if apps is None:
        return
    if prog_id in apps and (broken or not getattr(_office, "keep", False)):
        _quit(apps.pop(prog_id))
    if not apps and not getattr(_office, "keep", False):
        import pythoncom

        _office.apps = None
        pythoncom.CoUninitialize()


def _export_office_to_pdf(path: Path, out_dir: Path, timeout: float | None = None) -> list[tuple[Path, Locate]]:
    """Office ファイルを COM で PDF に書き出し、(PDF パス, ページ位置の付け方) のリストを返す。

    Excel は可視シートごとに、render 系スキルと同じく横1×縦1ページのフィット印刷にして
    1シート=1PDF で書き出す（印刷設定の変更は保存しない）。PowerPoint は非表示スライドを
    PDF に含めないため、ページ番号を表示スライドの番号へ対応させる。

    使い回しているアプリでエラーになったら（利用者が閉じた・応答しなくなった等）、そのアプリを終了し、
    新しく起動して1回だけやり直す。timeout 秒で終わらなければアプリのプロセスを終了し、やり直さずに
    OfficeTimeout にする（_watchdog）。取り込みの停止を求められたときも同じく終了し、ExtractStopped にする。
    """
    prog_id = _PROG_ID[path.suffix.lower()]
    stop_path = stop_file()
    while True:
        check_stopped()
        fresh = True
        try:
            app, fresh = _office_app(prog_id)
            with _watchdog(_app_pid(prog_id, app) if timeout or stop_path else None, timeout, path, stop_path):
                entries = _export_with(app, path, out_dir)
        except (OfficeTimeout, ExtractStopped):
            _release_office_app(prog_id, broken=True)
            raise
        except Exception:
            _release_office_app(prog_id, broken=True)
            if fresh:
                raise
            continue
        _release_office_app(prog_id, broken=False)
        return entries


def _export_with(app, path: Path, out_dir: Path) -> list[tuple[Path, Locate]]:
    """起動済みの app で path を PDF に書き出す（_export_office_to_pdf 参照）。開いた文書は閉じ、アプリは終了しない。"""
    ext = path.suffix.lower()
    abs_path = os.path.abspath(path)
    # 書き出し先も絶対パスにする（相対パスは Office の作業フォルダからの位置と解釈され、書き出せない）。
    out_dir = Path(os.path.abspath(out_dir))
    doc = None
    # ダミーのパスワード（_DUMMY_PASSWORD）は、パスワード付きのファイルを入力画面で止めずにエラーにするため。
    # Word・Excel は位置引数で渡す（Excel はキーワード引数の Password や、Format・WriteResPassword に値・Missing を
    # 渡すと入力画面で止まるか開けなくなる。Microsoft 365 の Excel で確認した組み合わせ）。PowerPoint は
    # パスワード付きのファイルで止まることがあり、そのときは _watchdog で打ち切る。
    try:
        if ext == ".docx":
            pdf_path = out_dir / "document.pdf"
            # FileName, ConfirmConversions, ReadOnly, AddToRecentFiles, PasswordDocument, PasswordTemplate
            doc = app.Documents.Open(abs_path, False, True, False, _DUMMY_PASSWORD, _DUMMY_PASSWORD)
            doc.SaveAs2(str(pdf_path), 17)  # wdFormatPDF
            return [(pdf_path, _page_locator)]

        if ext == ".pptx":
            pdf_path = out_dir / "presentation.pdf"
            # PowerPoint はファイル名の後ろに "::パスワード::" を付けて渡す。
            doc = app.Presentations.Open(f"{abs_path}::{_DUMMY_PASSWORD}::", ReadOnly=True, Untitled=False, WithWindow=False)
            visible = [s.SlideIndex for s in doc.Slides if not s.SlideShowTransition.Hidden]
            doc.SaveAs(str(pdf_path), 32)  # ppSaveAsPDF
            return [(pdf_path, lambda page, total: f"Slide {visible[page - 1]}" if page <= len(visible) else f"p.{page}")]

        # Filename, UpdateLinks, ReadOnly, Format, Password, WriteResPassword, IgnoreReadOnlyRecommended
        doc = app.Workbooks.Open(abs_path, 0, True, None, _DUMMY_PASSWORD, None, True)
        entries: list[tuple[Path, Locate]] = []
        for idx, ws in enumerate(doc.Worksheets):
            if ws.Visible != -1:  # xlSheetVisible 以外（非表示・最非表示）は印刷対象外
                continue
            try:
                page_setup = ws.PageSetup
                page_setup.Zoom = False
                page_setup.FitToPagesWide = 1
                page_setup.FitToPagesTall = 1
            except Exception:  # noqa: BLE001 - 印刷設定を変えられないシートはそのままの設定で書き出す
                pass
            sheet_pdf = out_dir / f"sheet_{idx}.pdf"
            try:
                ws.ExportAsFixedFormat(0, str(sheet_pdf), Quality=0, IgnorePrintAreas=False, OpenAfterPublish=False)  # xlTypePDF
            except Exception:  # noqa: BLE001 - 空のシートは書き出せない（印刷するものが無い）ため飛ばす
                continue
            entries.append((sheet_pdf, _sheet_locator(ws.Name)))
        return entries
    finally:
        if doc is not None:
            try:
                if ext == ".pptx":
                    doc.Close()
                else:
                    doc.Close(SaveChanges=False)
            except Exception:  # noqa: BLE001
                pass
