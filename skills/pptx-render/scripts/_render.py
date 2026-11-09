"""pptx/docx 共用のレンダリング共通モジュール。

動作概要:
  1. OLE（COM）で Office アプリ（PowerPoint/Word）をヘッドレス起動し、
     ファイルを高品質PDFへエクスポート。
  2. pypdfium2 で PDF ページを画像化（既定CAPTURE_DPI）。

前提条件:
  - Windows 環境かつ Microsoft Office がインストールされていること。
  - pywin32, pypdfium2, pillow がインストールされていること。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import pypdfium2 as pdfium

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_PROG_ID = {
    "excel": "Excel.Application",
    "pptx": "PowerPoint.Application",
    "docx": "Word.Application",
}

# PDFエクスポート定数
# Excel: xlTypePDF=0, PowerPoint: ppSaveAsPDF=32, Word: wdFormatPDF=17

# PDF→画像化キャプチャDPI
_CAPTURE_DPI = 300

# 目標DPI（余白除去後の縮尺。crop機能自体は常時OFFのため実質未使用だが、
# render_office_file()の戻り値JSONの`target_dpi`フィールドとして返す）
_TARGET_DPI = 150


# ---------------------------------------------------------------------------
# OLE→PDF 変換
# ---------------------------------------------------------------------------


def _convert_office_to_pdf(path: Path, tool: str, thread_id: str) -> Path:
    """OfficeファイルをOLE（COM）で開き、一時PDFへエクスポートする。

    tool: "excel" | "pptx" | "docx"
    戻り値: 生成されたPDFのパス
    """
    import pythoncom
    import win32com.client as win32

    # office_shared/excel_common.py（呼び出し元のrender_pptx.py等が既にsys.pathへ
    # 追加済み）から、Office COMプロセスのPID追跡ヘルパーをimportする。
    # 関数名はExcel由来だが、Hwnd取得の仕組み自体はWord/PowerPointのApplication
    # オブジェクトでも同様に動く（両方ともHwndプロパティを持つ）。
    from excel_common import record_excel_pid, release_excel_pid, wait_for_process_exit

    prog_id = _PROG_ID[tool]
    pythoncom.CoInitialize()
    app = None
    doc = None
    pdf_path = None
    recorded_pid = None
    try:
        app = win32.DispatchEx(prog_id)
        # run_scriptの外部タイムアウト等でPythonプロセスごと強制終了されると
        # 下のfinally節が実行されずCOMプロセスだけが残留することがあるため、
        # 起動直後に自セッションのPIDレジストリへ記録しておく（正常終了時は
        # 実終了を確認した上でfinallyで取り除く。残留した場合は
        # excel-vba-editスキルのedit_vba.py --recover-locksで後始末できる）。
        recorded_pid = record_excel_pid(path, app)
        # PowerPointは Presentations.Open 前後を問わず Application.Visible = False の
        # 設定自体がCOMエラーになる（ウィンドウ非表示は Open(WithWindow=False) 側で行う）。
        if tool != "pptx":
            app.Visible = False
        app.DisplayAlerts = 0  # wdAlertsNone
        # COM で起動した Office の既定はマクロ有効（msoAutomationSecurityLow）で、開いただけで
        # Document_Open 等が走る（MsgBox ならここでハングする）。描画には不要なので止める。
        try:
            app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
        except Exception:
            pass

        abs_path = os.path.abspath(path)

        # 中間生成物のPDFは元ファイルのフォルダを汚さないよう、他の一時出力
        # （_render_pdf_to_images の画像など）と同じ `_tmp_<thread_id>/` 配下に
        # 保存する。基準は run_script の cwd（ユーザー指定 work_dir になりうり
        # 自動削除対象外）ではなく、常に default_workdir（AGENT_DEFAULT_WORKDIR）。
        # default_workdir の保持日数ベースの自動削除で確実に消える。
        base_dir = Path(os.environ.get("AGENT_DEFAULT_WORKDIR") or "./data/temp").resolve()  # 返すパス・Office に渡すパスを絶対にする（MCP 経由では AGENT_DEFAULT_WORKDIR が無い）
        out_dir = base_dir / f"_tmp_{thread_id}" / "pdf_export"
        out_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:8]
        # 実行ごとに別の名前にする（画像名はこの PDF のパスから作るため、同じ名前だと編集前に描画した画像が
        # 編集後の描画で上書きされ、前後を比べられなかった）
        pdf_path = str(out_dir / f"{tool}_{digest}_{uuid.uuid4().hex[:8]}_export.pdf")

        if tool == "excel":
            # Workbook → PDF (xlTypePDF = 0)
            doc = app.Workbooks.Open(abs_path, UpdateLinks=0, ReadOnly=True)
            # Quality は XlFixedFormatQuality（xlQualityStandard=0）の整数指定が必要
            doc.ExportAsFixedFormat(0, pdf_path, Quality=0, IncludeDocProperties=True, IgnorePrintAreas=False, OpenAfterPublish=False)
            doc.Close(SaveChanges=False)

        elif tool == "pptx":
            # Presentation → PDF (ppSaveAsPDF = 32)
            doc = app.Presentations.Open(abs_path, ReadOnly=True, Untitled=False, WithWindow=False)
            doc.SaveAs(pdf_path, 32)
            doc.Close()

        elif tool == "docx":
            # Document → PDF (wdFormatPDF = 17)
            # 読み取り専用で開く（ユーザーが Word で開いている文書でも「使用中」の確認で止まらないため）
            doc = app.Documents.Open(abs_path, ReadOnly=True, AddToRecentFiles=False, Revert=True)
            doc.SaveAs2(pdf_path, 17)
            doc.Close(SaveChanges=False)

        else:
            raise ValueError(f"Unknown tool: {tool}")

        return Path(pdf_path)

    except Exception:
        raise
    finally:
        if doc is not None:
            try:
                doc.Close()
            except Exception:
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        if recorded_pid is not None and wait_for_process_exit(recorded_pid):
            release_excel_pid(recorded_pid)
        pythoncom.CoUninitialize()


# ---------------------------------------------------------------------------
# PDF→画像レンダリング（pypdfium2）
# ---------------------------------------------------------------------------


def _render_pdf_to_images(pdf_path: Path, dpi: int, thread_id: str) -> list[dict]:
    """PDFファイルの全ページを pypdfium2 で画像化し、リストとして返す。"""
    base_dir = Path(os.environ.get("AGENT_DEFAULT_WORKDIR") or "./data/temp").resolve()  # 返すパス・Office に渡すパスを絶対にする（MCP 経由では AGENT_DEFAULT_WORKDIR が無い）
    rendered_dir = base_dir / f"_tmp_{thread_id}" / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)

    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception:
        return []

    try:
        return _render_pages(pdf, pdf_path, dpi, rendered_dir)
    finally:
        # 開いたままだと Windows では一時 PDF を削除できない（WinError 32）
        pdf.close()


def _render_pages(pdf, pdf_path: Path, dpi: int, rendered_dir: Path) -> list[dict]:
    total_pages = len(pdf)
    scale = dpi / 72

    digest = hashlib.sha1(os.path.abspath(str(pdf_path)).encode("utf-8")).hexdigest()[:8]

    images = []
    for i in range(total_pages):
        page_num = i + 1
        filename = f"{digest}_p{page_num}.png"
        out_path = rendered_dir / filename
        try:
            bitmap = pdf[i].render(scale=scale)
            bitmap.to_pil().save(out_path)
        except Exception:
            continue
        images.append(
            {
                "page": page_num,
                "image_path": str(out_path),
                "original_dpi": dpi,
            }
        )

    return images


# ---------------------------------------------------------------------------
# メインエントリポイント
# ---------------------------------------------------------------------------


def render_office_file(
    path: Path,
    tool: str,
    target_dpi: int = _TARGET_DPI,
    thread_id: str | None = None,
) -> dict:
    """Officeファイル（pptx/docx）の全ページをレンダリングして画像化。

    Parameters
    ----------
    path : Path
        対象の Office ファイルパス
    tool : str
        "pptx" | "docx"
    target_dpi : int
        出力JSONの`target_dpi`フィールドとして返す（crop機能は常時OFF）
    thread_id : str | None
        AGENT_EXEC_TMP_NAME（無ければAGENT_THREAD_ID）。None なら "_no_session"

    Returns
    -------
    dict
        レンダリング結果
    """
    if thread_id is None:
        thread_id = os.environ.get("AGENT_EXEC_TMP_NAME") or os.environ.get("AGENT_THREAD_ID") or "_no_session"

    # 1. OLE → PDF 変換
    pdf_path = _convert_office_to_pdf(path, tool, thread_id)

    # PDFの総ページ数を取得（PDF→画像化の前に取得）
    try:
        tmp_pdf = pdfium.PdfDocument(str(pdf_path))
        total_pages = len(tmp_pdf)
        tmp_pdf.close()
    except Exception:
        total_pages = 0

    # 2. PDF → 画像化（全ページ）
    images = _render_pdf_to_images(pdf_path, _CAPTURE_DPI, thread_id)

    # 一時PDFを削除
    try:
        pdf_path.unlink(missing_ok=True)
    except Exception:
        pass

    if not images:
        return {
            "path": str(path),
            "tool": tool,
            "total_pages": total_pages,
            "start_page": None,
            "end_page": None,
            "dpi": _CAPTURE_DPI,
            "images": [],
            "crop_applied": False,
        }

    first_page = images[0]["page"]
    last_page = images[-1]["page"]

    return {
        "path": str(path),
        "tool": tool,
        "total_pages": total_pages,
        "start_page": first_page if images else None,
        "end_page": last_page if images else None,
        "dpi": _CAPTURE_DPI,
        "target_dpi": target_dpi,
        "images": images,
        "crop_applied": False,
    }
