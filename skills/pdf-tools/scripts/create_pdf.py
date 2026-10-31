"""セマンティックHTMLからPDFファイルを生成する（日本語対応）。

pdf-tools スキルの実行スクリプト（progressive disclosure 第3段階）。
run_script ツールから
    python create_pdf.py <output_path> [--title T] (--html HTML | --html-file PATH)
        [--accent-color RRGGBB] [--page-size a4|letter] [--orientation portrait|landscape]
        [--margin-cm N] [--header-text T] [--footer-text T] [--page-number]
の形で呼ばれる。

呼び出し側（LLM）が見出し(h1-h4)・段落(p)・表(table)・箇条書き(ul/ol)・強調ボックス
(div.callout)等のセマンティックなHTMLを直接書き、reportlab（platypus）のFlowableへ
変換して同梱テーマ（見出し=ゴシック、本文=明朝、指定アクセント色）でPDF化する。

日本語はWindows同梱のTTFフォント（Yu Gothic=見出し用ゴシック体、
Yu Mincho=本文用明朝体）を`pdfmetrics.registerFont(TTFont(...))`で実グリフごと
PDFへ埋め込んで使う。

以前はxhtml2pdf（HTML/CSSをそのままレンダリングするライブラリ）経由で
reportlab内蔵のCIDフォント（HeiseiMin-W3/HeiseiKakuGo-W5、グリフを埋め込まず
ビューア側のCJKフォント代替表示に依存する方式）を使っていたが、tune-prompt
system_prompt_scale/004実行時にpypdfium2でのレンダリング結果を実際に画像で
確認したところ日本語がすべて「豆腐（表示不可能グリフ）」化けする不具合を
確認した。CIDフォントをTTF実埋め込みに変えても同じくxhtml2pdf経由では
tofu化けが再現し（xhtml2pdfのCSS `@font-face` 経由・Python側`registerFont`
事前登録のどちらの方式でも同様）、原因を切り分けたところ「reportlab本体の
Platypus（`reportlab.platypus.Paragraph`等）へ直接TTFontを渡す分には正しく
描画される一方、xhtml2pdfが内部でフォークしている
`xhtml2pdf.reportlab_paragraph`経由だと同じ登録済みフォントでもtofu化けする」
ことを実験で確認した。そのためxhtml2pdf依存を廃し、本スクリプトが直接HTML
断片を解析してreportlab platypusのFlowableへ変換する方式に書き換えた。
フォントファイルが見つからない環境（Windows以外、または日本語フォント
未導入）では明示的にエラー終了する（tofu化けしたPDFを正常終了として
返さないため）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from _common import read_text_with_fallback, register_output_path, setup_utf8_stdio

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    HRFlowable,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ACCENT_DEFAULT = "1F4E78"
PAGE_SIZES = {"a4": A4, "letter": LETTER}
ORIENTATIONS = {"portrait", "landscape"}

# 本文=明朝(JPMincho)・見出し表見出し=ゴシック(JPGothic)の2役割のみ使う。
# 候補は先頭から順に存在確認し、最初に見つかったファイルを使う
# （Windowsのエディション・言語パックにより同梱フォントが異なるため）。
# 太字（`<b>`/`<strong>`）は各family専用の同梱ボールド体があれば使い、
# 無ければレギュラー体を代用する（フェイクボールドの合成はしない）。
_WINDOWS_FONTS_DIR = Path(r"C:\Windows\Fonts")
_JP_GOTHIC_CANDIDATES = ["YuGothR.ttc", "meiryo.ttc", "msgothic.ttc"]
_JP_GOTHIC_BOLD_CANDIDATES = ["YuGothB.ttc", "meiryob.ttc", "msgothic.ttc"]
_JP_MINCHO_CANDIDATES = ["yumin.ttf", "msmincho.ttc"]
_JP_MINCHO_BOLD_CANDIDATES = ["yumindb.ttf", "msmincho.ttc"]
# 日本語フォントが無い環境（英語版 Windows で日本語の追加フォントを入れていない等）で、
# 文書に CJK 文字が無いときだけ使う欧文フォント（見出し=サンセリフ、本文=セリフ）。
_LATIN_SANS_CANDIDATES = ["arial.ttf", "segoeui.ttf"]
_LATIN_SANS_BOLD_CANDIDATES = ["arialbd.ttf", "segoeuib.ttf"]
_LATIN_SERIF_CANDIDATES = ["times.ttf", "georgia.ttf"]
_LATIN_SERIF_BOLD_CANDIDATES = ["timesbd.ttf", "georgiab.ttf"]
# CJK の文字（ひらがな・カタカナ・CJK 統合漢字・全角記号）。
_CJK_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

JP_GOTHIC = "JPGothic"
JP_GOTHIC_BOLD = "JPGothic-Bold"
JP_MINCHO = "JPMincho"
JP_MINCHO_BOLD = "JPMincho-Bold"


def _find_font_file(candidates: list[str]) -> Path | None:
    for filename in candidates:
        path = _WINDOWS_FONTS_DIR / filename
        if path.exists():
            return path
    return None


def register_jp_fonts(text: str = "") -> None:
    """本文用・見出し用のTTFフォント（レギュラー/ボールド×ゴシック/明朝）をreportlabへ登録する。

    日本語フォントがあればそれを使う。無い場合、text（文書に出る全テキスト）に CJK 文字が
    無ければ欧文フォント（Arial/Times New Roman 等）で代用する。CJK 文字があるのに日本語
    フォントが無い場合は例外を送出する（豆腐化けした PDF を正常終了として返さないため）。
    レギュラー体が見つからない場合のみ例外にし、ボールド体が無ければレギュラー体で代用する。
    """
    gothic_path = _find_font_file(_JP_GOTHIC_CANDIDATES)
    mincho_path = _find_font_file(_JP_MINCHO_CANDIDATES)
    gothic_bold_candidates, mincho_bold_candidates = _JP_GOTHIC_BOLD_CANDIDATES, _JP_MINCHO_BOLD_CANDIDATES
    if (gothic_path is None or mincho_path is None) and not _CJK_RE.search(text):
        gothic_path = _find_font_file(_LATIN_SANS_CANDIDATES)
        mincho_path = _find_font_file(_LATIN_SERIF_CANDIDATES)
        gothic_bold_candidates, mincho_bold_candidates = _LATIN_SANS_BOLD_CANDIDATES, _LATIN_SERIF_BOLD_CANDIDATES
    if gothic_path is None or mincho_path is None:
        raise RuntimeError(
            f"No Japanese font found (gothic candidates: {_JP_GOTHIC_CANDIDATES}, "
            f"mincho candidates: {_JP_MINCHO_CANDIDATES}, searched in {_WINDOWS_FONTS_DIR}). "
            "A PDF containing Japanese/CJK text cannot be created without the Windows Japanese fonts "
            "(install the Japanese Supplemental Fonts optional feature)."
        )

    gothic_bold_path = _find_font_file(gothic_bold_candidates) or gothic_path
    mincho_bold_path = _find_font_file(mincho_bold_candidates) or mincho_path

    pdfmetrics.registerFont(TTFont(JP_GOTHIC, str(gothic_path)))
    pdfmetrics.registerFont(TTFont(JP_GOTHIC_BOLD, str(gothic_bold_path)))
    pdfmetrics.registerFont(TTFont(JP_MINCHO, str(mincho_path)))
    pdfmetrics.registerFont(TTFont(JP_MINCHO_BOLD, str(mincho_bold_path)))

    # <b>/<strong> markup の太字解決（Paragraphが内部でfontName+bold/italicから
    # 実フォント名を引く際に使う）。斜体はCJKフォントに無いためレギュラー体で代用する。
    pdfmetrics.registerFontFamily(
        JP_GOTHIC, normal=JP_GOTHIC, bold=JP_GOTHIC_BOLD, italic=JP_GOTHIC, boldItalic=JP_GOTHIC_BOLD
    )
    pdfmetrics.registerFontFamily(
        JP_MINCHO, normal=JP_MINCHO, bold=JP_MINCHO_BOLD, italic=JP_MINCHO, boldItalic=JP_MINCHO_BOLD
    )


# ---------------------------------------------------------------------------
# HTML断片 → 簡易DOM
# ---------------------------------------------------------------------------

# インライン装飾として認識するタグ（それ以外の未対応タグは中身のテキストのみ通す）。
_INLINE_TAGS = {"b", "strong", "i", "em", "br"}
# 中身を持たない（閉じタグを期待しない）ブロックタグ。
_VOID_TAGS = {"hr", "img", "br"}


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: list[tuple[str, str | None]] | None = None):
        self.tag = tag
        self.attrs: dict[str, str] = {k: (v or "") for k, v in (attrs or [])}
        self.children: list[_Node | str] = []


class _DOMBuilder(HTMLParser):
    """`--html`で渡されるHTML断片を、対応タグのみの簡易木構造へ変換する。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, attrs)
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(_Node(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def _serialize_inline(children: list[_Node | str]) -> str:
    """インライン要素（b/strong/i/em/br + テキスト）をreportlab Paragraph markupへ変換する。"""
    parts: list[str] = []
    for child in children:
        if isinstance(child, str):
            parts.append(xml_escape(child))
            continue
        if child.tag == "br":
            parts.append("<br/>")
        elif child.tag in ("b", "strong"):
            parts.append(f"<b>{_serialize_inline(child.children)}</b>")
        elif child.tag in ("i", "em"):
            parts.append(f"<i>{_serialize_inline(child.children)}</i>")
        else:
            # 未対応の入れ子タグは中身のテキストだけ引き継ぐ。
            parts.append(_serialize_inline(child.children))
    return "".join(parts)


# ---------------------------------------------------------------------------
# DOM → reportlab Flowable
# ---------------------------------------------------------------------------


def _build_styles(accent: colors.Color) -> dict[str, ParagraphStyle]:
    return {
        "h1": ParagraphStyle(
            "h1", fontName=JP_GOTHIC, fontSize=20, leading=26, textColor=accent,
            spaceBefore=0, spaceAfter=4,
        ),
        "h2": ParagraphStyle(
            "h2", fontName=JP_GOTHIC, fontSize=15, leading=20, textColor=accent,
            spaceBefore=16, spaceAfter=8,
        ),
        "h3": ParagraphStyle(
            "h3", fontName=JP_GOTHIC, fontSize=12.5, leading=17, textColor=accent,
            spaceBefore=12, spaceAfter=6,
        ),
        "h4": ParagraphStyle(
            "h4", fontName=JP_GOTHIC, fontSize=11, leading=15, textColor=accent,
            spaceBefore=10, spaceAfter=4,
        ),
        "p": ParagraphStyle(
            "p", fontName=JP_MINCHO, fontSize=10.5, leading=17.85, textColor=colors.HexColor("#1a1a1a"),
            spaceAfter=8,
        ),
        "li": ParagraphStyle(
            "li", fontName=JP_MINCHO, fontSize=10.5, leading=17.85, textColor=colors.HexColor("#1a1a1a"),
            spaceAfter=3, leftIndent=18, bulletIndent=4,
        ),
        "th": ParagraphStyle(
            "th", fontName=JP_GOTHIC, fontSize=9.5, leading=13, textColor=colors.white,
        ),
        "td": ParagraphStyle(
            "td", fontName=JP_MINCHO, fontSize=9.5, leading=13, textColor=colors.HexColor("#1a1a1a"),
        ),
        "callout": ParagraphStyle(
            "callout", fontName=JP_MINCHO, fontSize=10.5, leading=17.85, textColor=colors.HexColor("#1a1a1a"),
        ),
        "header": ParagraphStyle("header", fontName=JP_GOTHIC, fontSize=8, textColor=colors.HexColor("#888888")),
        "footer": ParagraphStyle(
            "footer", fontName=JP_GOTHIC, fontSize=8, textColor=colors.HexColor("#888888"), alignment=1,
        ),
    }


def _build_table(node: _Node, styles: dict[str, ParagraphStyle], accent: colors.Color, avail_width: float):
    rows: list[list] = []
    has_header = False
    for tr in node.children:
        if not isinstance(tr, _Node) or tr.tag != "tr":
            continue
        row = []
        for cell in tr.children:
            if not isinstance(cell, _Node) or cell.tag not in ("th", "td"):
                continue
            style = styles["th"] if cell.tag == "th" else styles["td"]
            row.append(Paragraph(_serialize_inline(cell.children) or "&nbsp;", style))
            if cell.tag == "th":
                has_header = True
        if row:
            rows.append(row)
    if not rows:
        return None

    n_cols = max(len(r) for r in rows)
    for row in rows:
        while len(row) < n_cols:
            row.append(Paragraph("", styles["td"]))

    col_width = avail_width / n_cols
    table = Table(rows, colWidths=[col_width] * n_cols, repeatRows=1 if has_header else 0)
    style_cmds = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if has_header:
        style_cmds += [
            ("BACKGROUND", (0, 0), (-1, 0), accent),
            ("GRID", (0, 0), (-1, 0), 0.5, accent),
        ]
    table.setStyle(TableStyle(style_cmds))
    return table


def _build_callout(node: _Node, styles: dict[str, ParagraphStyle], accent: colors.Color):
    inner = Paragraph(_serialize_inline(node.children), styles["callout"])
    table = Table([[inner]], colWidths=[None])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EDF2F8")),
                ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def _build_image(node: _Node, avail_width: float):
    src = node.attrs.get("src")
    if not src:
        return None
    path = Path(src)
    if not path.exists():
        raise ValueError(f"Image file not found: {src}")
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as im:
            natural_w, natural_h = im.size
    except Exception:  # noqa: BLE001 - サイズ取得できなければ幅指定 or 既定値に委ねる
        natural_w, natural_h = None, None

    width_attr = node.attrs.get("width")
    width = float(width_attr) if width_attr else None
    if width and width > avail_width:
        width = avail_width
    if width is None:
        width = avail_width
    if natural_w and natural_h:
        height = width * (natural_h / natural_w)
    else:
        height = width * 0.75
    return Image(str(path), width=width, height=height)


def _build_list(node: _Node, styles: dict[str, ParagraphStyle], ordered: bool) -> list:
    flowables = []
    for i, li in enumerate([c for c in node.children if isinstance(c, _Node) and c.tag == "li"], start=1):
        prefix = f"{i}. " if ordered else "•  "
        text = prefix + _serialize_inline(li.children)
        flowables.append(Paragraph(text, styles["li"]))
    return flowables


def _build_flowables(
    nodes: list[_Node | str], styles: dict[str, ParagraphStyle], accent: colors.Color, avail_width: float
) -> list:
    story: list = []
    for node in nodes:
        if isinstance(node, str):
            continue
        tag = node.tag
        if tag in ("h1", "h2", "h3", "h4"):
            story.append(Paragraph(_serialize_inline(node.children), styles[tag]))
            if tag == "h1":
                story.append(HRFlowable(width="100%", thickness=1.4, color=accent, spaceBefore=0, spaceAfter=10))
        elif tag == "p":
            story.append(Paragraph(_serialize_inline(node.children) or "&nbsp;", styles["p"]))
        elif tag == "table":
            table = _build_table(node, styles, accent, avail_width)
            if table is not None:
                story.append(table)
                story.append(Spacer(1, 10))
        elif tag in ("ul", "ol"):
            story.extend(_build_list(node, styles, ordered=(tag == "ol")))
            story.append(Spacer(1, 4))
        elif tag == "hr":
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CCCCCC"), spaceBefore=6, spaceAfter=6))
        elif tag == "div":
            classes = node.attrs.get("class", "").split()
            if "callout" in classes:
                story.append(_build_callout(node, styles, accent))
                story.append(Spacer(1, 8))
            elif "page-break" in classes:
                story.append(PageBreak())
            else:
                story.extend(_build_flowables(node.children, styles, accent, avail_width))
        elif tag == "img":
            image = _build_image(node, avail_width)
            if image is not None:
                story.append(image)
                story.append(Spacer(1, 8))
        else:
            # 未対応タグは透過して中身だけ処理する。
            story.extend(_build_flowables(node.children, styles, accent, avail_width))
    return story


# ---------------------------------------------------------------------------
# ヘッダー/フッター/ページ番号（合計ページ数が要るため2パス方式のCanvasを使う）
# ---------------------------------------------------------------------------


class _HeaderFooterCanvas(Canvas):
    def __init__(self, *args, header_text=None, footer_text=None, page_number=False,
                 margin=0.0, page_width=0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._hf_header_text = header_text
        self._hf_footer_text = footer_text
        self._hf_page_number = page_number
        self._hf_margin = margin
        self._hf_page_width = page_width
        self._hf_saved_states: list[dict] = []

    def showPage(self) -> None:  # noqa: N802 - reportlabのAPI名に合わせる
        self._hf_saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total_pages = len(self._hf_saved_states)
        for state in self._hf_saved_states:
            self.__dict__.update(state)
            self._draw_header_footer(total_pages)
            super().showPage()
        super().save()

    def _draw_header_footer(self, total_pages: int) -> None:
        if self._hf_header_text:
            self.setFont(JP_GOTHIC, 8)
            self.setFillColor(colors.HexColor("#888888"))
            self.drawString(self._hf_margin, self._page_height_hint() - self._hf_margin * 0.6, self._hf_header_text)

        footer_parts = []
        if self._hf_footer_text:
            footer_parts.append(self._hf_footer_text)
        if self._hf_page_number:
            footer_parts.append(f"{self.getPageNumber()} / {total_pages}")
        if footer_parts:
            self.setFont(JP_GOTHIC, 8)
            self.setFillColor(colors.HexColor("#888888"))
            self.drawCentredString(
                self._hf_page_width / 2, self._hf_margin * 0.5, "　|　".join(footer_parts)
            )

    def _page_height_hint(self) -> float:
        return self._pagesize[1]


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("output_path")
    parser.add_argument("--title")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--html")
    group.add_argument("--html-file")
    parser.add_argument("--accent-color", default=ACCENT_DEFAULT)
    parser.add_argument("--page-size", default="a4", choices=sorted(PAGE_SIZES))
    parser.add_argument("--orientation", default="portrait", choices=sorted(ORIENTATIONS))
    parser.add_argument("--margin-cm", type=float, default=2.0)
    parser.add_argument("--header-text")
    parser.add_argument("--footer-text")
    parser.add_argument("--page-number", action="store_true")
    args = parser.parse_args()

    if args.html_file:
        html_file = Path(args.html_file)
        if not html_file.exists():
            print(f"Body HTML file not found: {args.html_file}", file=sys.stderr)
            return 1
        body_html = read_text_with_fallback(html_file)
    else:
        body_html = args.html or ""

    if not body_html.strip():
        print("The body HTML is empty.", file=sys.stderr)
        return 1

    accent_hex = args.accent_color.lstrip("#").upper()
    if len(accent_hex) != 6 or any(c not in "0123456789ABCDEF" for c in accent_hex):
        print(f"Specify --accent-color as a hexadecimal RRGGBB value: {args.accent_color}", file=sys.stderr)
        return 1
    accent = colors.HexColor(f"#{accent_hex}")

    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        register_jp_fonts(" ".join([body_html, args.title or "", args.header_text or "", args.footer_text or ""]))
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    page_size = PAGE_SIZES[args.page_size]
    if args.orientation == "landscape":
        page_size = landscape(page_size)
    margin = args.margin_cm * cm

    try:
        builder = _DOMBuilder()
        builder.feed(body_html)
        builder.close()

        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=page_size,
            leftMargin=margin,
            rightMargin=margin,
            topMargin=margin,
            bottomMargin=margin,
            title=args.title or "",
        )
        avail_width = page_size[0] - 2 * margin
        styles = _build_styles(accent)
        story = _build_flowables(builder.root.children, styles, accent, avail_width)
        if not story:
            print("Could not extract valid content from the body HTML.", file=sys.stderr)
            return 1

        def _canvasmaker(*c_args, **c_kwargs):
            return _HeaderFooterCanvas(
                *c_args,
                header_text=args.header_text,
                footer_text=args.footer_text,
                page_number=args.page_number,
                margin=margin,
                page_width=page_size[0],
                **c_kwargs,
            )

        doc.build(story, canvasmaker=_canvasmaker)
    except Exception as e:  # noqa: BLE001 - PDF生成失敗はすべてエラー扱いにする
        print(f"PDF generation failed: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1

    result_json = {"output_path": str(output_path), "size_bytes": output_path.stat().st_size}
    path_memory = register_output_path(output_path, description="PDF generated by create_pdf")
    if path_memory:
        result_json["path_memory"] = path_memory
    print(json.dumps(result_json, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
