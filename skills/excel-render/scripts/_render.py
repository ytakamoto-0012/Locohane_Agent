"""excel/pptx/docx 共用のレンダリング共通モジュール。

動作概要:
  1. OLE（COM）で Office アプリ（Excel/PowerPoint/Word）をヘッドレス起動し、
     ファイルを高品質PDFへエクスポート。
  2. pypdfium2 で PDF ページを画像化。
  3. 白黒境界判定で余白を除去（コンテンツ領域のbboxを算出）。
  4. bbox でクロップ後、目標 DPI に縮尺を合わせる。

前提条件:
  - Windows 環境かつ Microsoft Office がインストールされていること。
  - pywin32, pypdfium2, pillow がインストールされていること。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
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

# PDF→画像化キャプチャDPI（docx-render/pptx-renderと同じ基準値に統一）
_CAPTURE_DPI = 300

# 目標DPI（クロップ後の最終出力解像度）。キャプチャDPIの半分とし、
# _crop_imageで高解像度キャプチャからダウンスケールして容量を抑える。
_TARGET_DPI = 150

# 白黒境界判定の閾値（ピクセル値。これ未満なら黒＝コンテンツとみなす）
_DARK_PIXEL_THRESHOLD = 128

# --- フィット印刷の縮尺補正（3.2節で使用） ---
# 用紙サイズ(pt)の対応表。xlPaperSize定数のうち代表的なもののみ。
# 未対応の値の場合はスケール計算をスキップする（=A4等を勝手に仮定しない）。
_PAPER_SIZE_PT: dict[int, tuple[float, float]] = {
    1: (612.0, 792.0),  # xlPaperLetter
    5: (612.0, 1008.0),  # xlPaperLegal
    8: (841.89, 1190.55),  # xlPaperA3
    9: (595.28, 841.89),  # xlPaperA4
    11: (419.53, 595.28),  # xlPaperA5
    12: (728.50, 1031.81),  # xlPaperB4
    13: (516.22, 728.50),  # xlPaperB5
}

_FIT_SCALE_FLOOR = 0.10  # Excel自体の縮小印刷下限（これ未満は指定不可でページが分割される）
_BOOST_TRIGGER_SCALE = 0.5  # この値を下回ったらDPIブーストを検討する
_BOOST_DPI_CEIL = 900  # ブースト後キャプチャDPIの上限（暴走防止）


# ---------------------------------------------------------------------------
# OLE→PDF 変換
# ---------------------------------------------------------------------------


def _sheet_required_scale(ws) -> float:
    """シートの内容（セル使用範囲＋図形）が、フィット印刷でどこまで縮小を要求
    されるか（Excel自体の10%下限を適用する前の理論値）を返す。1.0なら縮小不要。
    用紙サイズが対応表に無い場合や算出できない場合は1.0（＝補正なし）を返す。
    """
    try:
        used = ws.UsedRange
        content_left = used.Left
        content_top = used.Top
        content_right = used.Left + used.Width
        content_bottom = used.Top + used.Height

        # 図形（オートシェイプ・グラフ等）はUsedRangeに含まれないため、
        # 存在する場合はbboxをUsedRangeとの和集合に広げる。
        shapes = ws.Shapes
        for i in range(1, shapes.Count + 1):
            shp = shapes.Item(i)
            content_left = min(content_left, shp.Left)
            content_top = min(content_top, shp.Top)
            content_right = max(content_right, shp.Left + shp.Width)
            content_bottom = max(content_bottom, shp.Top + shp.Height)

        content_w = content_right - content_left
        content_h = content_bottom - content_top
        if content_w <= 0 or content_h <= 0:
            return 1.0

        ps = ws.PageSetup
        paper = _PAPER_SIZE_PT.get(int(ps.PaperSize))
        if paper is None:
            return 1.0  # 未対応の用紙サイズは補正をスキップ（A4等を仮定しない）
        paper_w, paper_h = paper
        if int(ps.Orientation) == 2:  # xlLandscape
            paper_w, paper_h = paper_h, paper_w

        printable_w = paper_w - ps.LeftMargin - ps.RightMargin
        printable_h = paper_h - ps.TopMargin - ps.BottomMargin
        if printable_w <= 0 or printable_h <= 0:
            return 1.0

        return min(printable_w / content_w, printable_h / content_h, 1.0)
    except Exception:
        return 1.0


# Excelシート名はWindowsファイル名として不正な文字を含みうる（`<>:"|`など。
# `\/?*[]:`はExcel自体が既にシート名に使用禁止のため実質重複チェック）。
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_sheet_filename(name: str) -> str:
    safe = _UNSAFE_FILENAME_CHARS.sub("_", name).strip()
    return safe or "sheet"


def _convert_office_to_pdf(
    path: Path, tool: str, thread_id: str, force_fit_to_page: bool = True
) -> tuple[list[tuple[str | None, Path]], dict[str, float]]:
    """OfficeファイルをOLE（COM）で開き、一時PDFへエクスポートする。

    tool: "excel" | "pptx" | "docx"
    force_fit_to_page: Trueなら各シートの印刷設定を横1ページ×縦1ページの
        フィット印刷へ強制する（既定）。Falseならシートに既に設定されている
        印刷設定（Zoom/FitToPages/PrintArea等）をそのまま使う（excelのみ意味を持つ）。
    戻り値: ((シート名 | None, PDFパス)のリスト, シート名→required_scaleの辞書)。
        excelは非表示シートを除く可視シート1枚につきPDF1つをエクスポートする
        （出力画像ファイル名にシート名を使うため）。pptx/docxは(None, 単一PDF)の
        1要素リスト。scale辞書はexcel以外、またforce_fit_to_page=Falseの場合は空dict。
    """
    import pythoncom
    import win32com.client as win32

    # office_shared/excel_common.py（呼び出し元のrender_excel.py等が既にsys.pathへ
    # 追加済み）から、Office COMプロセスのPID追跡ヘルパーをimportする。
    # 関数名はExcel由来だが、Hwnd取得の仕組み自体はWord/PowerPointのApplication
    # オブジェクトでも同様に動く（両方ともHwndプロパティを持つ）。
    from excel_common import record_excel_pid, release_excel_pid, wait_for_process_exit

    prog_id = _PROG_ID[tool]
    pythoncom.CoInitialize()
    app = None
    doc = None
    recorded_pid = None
    scale_by_sheet: dict[str, float] = {}
    pdf_entries: list[tuple[str | None, Path]] = []
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

        abs_path = os.path.abspath(path)

        # 中間生成物のPDFは元ファイルのフォルダを汚さないよう、他の一時出力
        # （_render_pdf_to_images の画像など）と同じ `_tmp_<thread_id>/` 配下に
        # 保存する。基準は run_script の cwd（ユーザー指定 work_dir になりうり
        # 自動削除対象外）ではなく、常に default_workdir（AGENT_DEFAULT_WORKDIR）。
        # default_workdir の保持日数ベースの自動削除で確実に消える。
        base_dir = Path(os.environ.get("AGENT_DEFAULT_WORKDIR") or "./data/temp")
        out_dir = base_dir / f"_tmp_{thread_id}" / "pdf_export"
        out_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:8]

        if tool == "excel":
            # シートごとに個別PDFへエクスポートする（xlTypePDF = 0）。
            # ワークブック一括エクスポートだとPDFページとシートの対応が
            # 複数ページ分割時に取れなくなるため、Worksheet.ExportAsFixedFormat
            # で1シート=1PDFにし、出力画像ファイル名にシート名をそのまま使えるようにする。
            doc = app.Workbooks.Open(abs_path)
            for idx, ws in enumerate(doc.Worksheets):
                try:
                    if ws.Visible != -1:  # xlSheetVisible以外（非表示/最非表示）は印刷対象外
                        continue
                except Exception:
                    pass
                if force_fit_to_page:
                    try:
                        scale_by_sheet[ws.Name] = _sheet_required_scale(ws)
                    except Exception:
                        scale_by_sheet[ws.Name] = 1.0
                    try:
                        page_setup = ws.PageSetup
                        page_setup.Zoom = False
                        page_setup.FitToPagesWide = 1
                        page_setup.FitToPagesTall = 1
                    except Exception:
                        pass
                sheet_pdf_path = out_dir / f"excel_{digest}_{idx}_export.pdf"
                try:
                    ws.ExportAsFixedFormat(
                        0, str(sheet_pdf_path), Quality=0, IncludeDocProperties=True, IgnorePrintAreas=False, OpenAfterPublish=False
                    )
                except Exception:
                    continue
                pdf_entries.append((ws.Name, sheet_pdf_path))
            doc.Close(SaveChanges=False)
            doc = None

        elif tool == "pptx":
            # Presentation → PDF (ppSaveAsPDF = 32)
            pdf_path = out_dir / f"{tool}_{digest}_export.pdf"
            doc = app.Presentations.Open(abs_path, ReadOnly=True, Untitled=False, WithWindow=False)
            doc.SaveAs(str(pdf_path), 32)
            doc.Close()
            doc = None
            pdf_entries.append((None, pdf_path))

        elif tool == "docx":
            # Document → PDF (wdFormatPDF = 17)
            pdf_path = out_dir / f"{tool}_{digest}_export.pdf"
            doc = app.Documents.Open(abs_path, ReadOnly=False, Revert=True)
            doc.SaveAs2(str(pdf_path), 17)
            doc.Close(SaveChanges=False)
            doc = None
            pdf_entries.append((None, pdf_path))

        else:
            raise ValueError(f"Unknown tool: {tool}")

        return pdf_entries, scale_by_sheet

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
# 余白除去（白黒境界判定）
# ---------------------------------------------------------------------------


def _detect_content_bbox(image_path: Path) -> tuple[int, int, int, int] | None:
    """PNG画像からコンテンツ領域のbbox（left, top, right, bottom）を算出する。

    グレースケール化し、閾値未満（暗い）ピクセルの外接矩形をPILの
    getbbox()で直接求める。

    旧実装は上下・左右を別々に「黒画素数 ÷ 画像の幅（上下判定）/高さ
    （左右判定）」の比率で判定していたため、コンテンツがシートの一部
    （例: 上部のみ）に偏っている場合、左右判定の比率が画像全高で薄まって
    しまい、実際のコンテンツ幅よりはるかに狭いbboxになるバグがあった。
    getbbox()は行・列を独立に判定しないため、コンテンツの偏りに影響
    されず正確な外接矩形を返す。

    完全に白（コンテンツなし）の場合は None を返す。
    """
    from PIL import Image

    img = Image.open(image_path).convert("L")  # グレースケール
    mask = img.point(lambda p: 255 if p < _DARK_PIXEL_THRESHOLD else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return None  # 全面白（コンテンツなし）

    left, top, right, bottom = bbox
    # getbbox()は半開区間 [left, right) x [top, bottom) を返す。呼び出し側
    # _crop_image の crop((left, top, right+1, bottom+1)) と整合させるため、
    # 包含的な最終インデックス（right-1, bottom-1）に変換して返す。
    return (left, top, right - 1, bottom - 1)


def _crop_image(image_path: Path, bbox: tuple[int, int, int, int], target_dpi: int, source_dpi: int) -> Path:
    """bbox で画像をクロップし、目標 DPI に縮尺して保存。

    target_dpi: 目標DPI（画像の解像度を調整）
    source_dpi: 元画像の実際の描画DPI（_render_pdf_to_images に渡した dpi）
    戻り値: 保存先パス
    """
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    left, top, right, bottom = bbox

    if right <= left or bottom <= top:
        return image_path

    cropped = img.crop((left, top, right + 1, bottom + 1))

    # 目標 DPI に縮尺（元画像は source_dpi で描画済み）
    scale = target_dpi / source_dpi

    if abs(scale - 1.0) > 0.01:
        new_size = (
            max(int(cropped.width * scale), 1),
            max(int(cropped.height * scale), 1),
        )
        cropped = cropped.resize(new_size, Image.LANCZOS)

    out_path = image_path.with_name(image_path.stem + "_cropped.png")
    cropped.save(out_path, dpi=(target_dpi, target_dpi))
    return out_path


# ---------------------------------------------------------------------------
# PDF→画像レンダリング（pypdfium2）
# ---------------------------------------------------------------------------


def _render_pdf_to_images(pdf_entries: list[tuple[str | None, Path]], dpi: int, thread_id: str) -> list[dict]:
    """PDF群を pypdfium2 で画像化し、リストとして返す。

    pdf_entries: (シート名 | None, PDFパス)のリスト。
    シート名が付いている場合（excel）はファイル名に使う（`{digest}_{シート名}.png`。
    シートが複数ページに分割された場合のみ`_p{ローカルページ番号}`を付与）。
    シート名が無い場合（pptx/docx）は従来通り`{digest}_p{通し番号}.png`。
    """
    base_dir = Path(os.environ.get("AGENT_DEFAULT_WORKDIR") or "./data/temp")
    rendered_dir = base_dir / f"_tmp_{thread_id}" / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)

    scale = dpi / 72
    images: list[dict] = []
    global_page = 0

    for sheet_name, pdf_path in pdf_entries:
        try:
            pdf = pdfium.PdfDocument(str(pdf_path))
        except Exception:
            continue

        total_pages = len(pdf)
        digest = hashlib.sha1(os.path.abspath(str(pdf_path)).encode("utf-8")).hexdigest()[:8]
        safe_name = _sanitize_sheet_filename(sheet_name) if sheet_name else None

        for i in range(total_pages):
            global_page += 1
            local_page = i + 1
            if safe_name:
                filename = f"{digest}_{safe_name}.png" if total_pages == 1 else f"{digest}_{safe_name}_p{local_page}.png"
            else:
                filename = f"{digest}_p{global_page}.png"
            out_path = rendered_dir / filename
            try:
                bitmap = pdf[i].render(scale=scale)
                bitmap.to_pil().save(out_path)
            except Exception:
                continue
            img_info = {
                "page": global_page,
                "image_path": str(out_path),
                "original_dpi": dpi,
            }
            if sheet_name:
                img_info["sheet"] = sheet_name
            images.append(img_info)

    return images


# ---------------------------------------------------------------------------
# メインエントリポイント
# ---------------------------------------------------------------------------


def render_office_file(
    path: Path,
    tool: str,
    crop: bool = True,
    target_dpi: int = _TARGET_DPI,
    thread_id: str | None = None,
    force_fit_to_page: bool = True,
) -> dict:
    """Officeファイル（excel/pptx/docx）の全ページをレンダリングして画像化。

    Parameters
    ----------
    path : Path
        対象の Office ファイルパス
    tool : str
        "excel" | "pptx" | "docx"
    crop : bool
        True なら白黒境界判定で余白を除去
    target_dpi : int
        余白除去後の目標DPI
    thread_id : str | None
        AGENT_EXEC_TMP_NAME（無ければAGENT_THREAD_ID）。None なら "_no_session"
    force_fit_to_page : bool
        True（既定）なら各シートの印刷設定を横1ページ×縦1ページのフィット印刷へ
        強制する。Falseならシートに既に設定されている印刷範囲・フィット設定を
        そのまま使う（excelのみ意味を持つ。この場合DPIブースト・分割警告は行わない）。

    Returns
    -------
    dict
        レンダリング結果
    """
    if thread_id is None:
        thread_id = os.environ.get("AGENT_EXEC_TMP_NAME") or os.environ.get("AGENT_THREAD_ID") or "_no_session"

    # 1. OLE → PDF 変換（excelはシートごとに個別PDF、シートごとの必要縮尺も同時に取得）
    pdf_entries, scale_by_sheet = _convert_office_to_pdf(path, tool, thread_id, force_fit_to_page)

    # 1.5 縮尺に応じたキャプチャDPI・目標DPIの動的ブーストと、分割警告の生成
    capture_dpi = _CAPTURE_DPI
    effective_target_dpi = target_dpi
    warnings: list[str] = []
    if scale_by_sheet:
        worst_scale = min(scale_by_sheet.values())
        if worst_scale < _BOOST_TRIGGER_SCALE:
            boost_factor = _BOOST_TRIGGER_SCALE / max(worst_scale, 0.01)
            capture_dpi = min(round(_CAPTURE_DPI * boost_factor), _BOOST_DPI_CEIL)
            effective_target_dpi = min(round(target_dpi * boost_factor), _BOOST_DPI_CEIL)
        split_sheets = [name for name, s in scale_by_sheet.items() if s < _FIT_SCALE_FLOOR]
        if split_sheets:
            msg = (
                "Warning: sheet " + ", ".join(split_sheets) + " has a total of column widths and row heights below the minimum scale for fitting it on one page"
                " (10%). It does not fit on one page and is split into several pages. "
                "Review the column widths with set_column_width of excel-edit (the unit is character width, roughly 1-60)."
            )
            warnings.append(msg)
            print(msg, file=sys.stderr)

    # PDFの総ページ数を取得（PDF→画像化の前に取得、全PDFの合算）
    total_pages = 0
    for _sheet_name, pdf_path in pdf_entries:
        try:
            tmp_pdf = pdfium.PdfDocument(str(pdf_path))
            total_pages += len(tmp_pdf)
        except Exception:
            continue

    # 2. PDF → 画像化（全ページ）
    images = _render_pdf_to_images(pdf_entries, capture_dpi, thread_id)

    if not images:
        result = {
            "path": str(path),
            "tool": tool,
            "total_pages": total_pages,
            "start_page": None,
            "end_page": None,
            "dpi": capture_dpi,
            "images": [],
            "crop_applied": False,
        }
        if warnings:
            result["warnings"] = warnings
        return result

    # 3. 余白除去（crop=True の場合）
    crop_applied = False
    if crop:
        for img_info in images:
            img_path = Path(img_info["image_path"])
            bbox = _detect_content_bbox(img_path)
            if bbox is not None:
                cropped_path = _crop_image(img_path, bbox, effective_target_dpi, capture_dpi)
                if cropped_path != img_path:
                    try:
                        img_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                img_info["image_path"] = str(cropped_path)
                img_info["cropped"] = True
                crop_applied = True
            else:
                img_info["cropped"] = False

    # 一時PDFを削除
    for _sheet_name, pdf_path in pdf_entries:
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            pass

    first_page = images[0]["page"]
    last_page = images[-1]["page"]

    result = {
        "path": str(path),
        "tool": tool,
        "total_pages": total_pages,
        "start_page": first_page if images else None,
        "end_page": last_page if images else None,
        "dpi": capture_dpi,
        "target_dpi": effective_target_dpi,
        "images": images,
        "crop_applied": crop_applied,
    }
    if warnings:
        result["warnings"] = warnings
    return result
