"""Office ファイル（docx・pptx・xlsx・xlsm）の中に入っている画像（写真・図）を取り出す（[vector_db].embedded_images）。

image_input = true でページ画像にしない Office ファイルは、Office を起動して PDF に書き出す代わりに、ファイル（ZIP）の中の
画像のパーツ（word/media/image1.png 等）を直接読み、1枚ずつ PNG にそろえて画像として埋め込む（render.py の画像ファイルと同じ扱い）。
本文・スライド・シートから参照されている画像だけを読む（スライドマスター・ヘッダー等のロゴは読まない）。
外部のファイルへのリンクの画像・Pillow で開けない形式（SVG 等。SVG には PNG の代替も入っているためそちらを読む）は飛ばす。
"""

from __future__ import annotations

import hashlib
import io
import logging
import posixpath
from pathlib import Path
from xml.etree import ElementTree
import zipfile

from . import extract
from .extract import _NS_PKG_REL, _NS_R, _NS_S, _NS_XDR, _cell_name, _drawing_anchors, _drop_fallbacks, _int_text, check_stopped
from .render import _A4_LONG_SIDE_INCHES, PageImage, _page_writer, _to_png

logger = logging.getLogger(__name__)

# 中の画像を取り出せる形式。
EMBEDDED_IMAGE_EXTENSIONS = (".docx", ".pptx", ".xlsx", ".xlsm")

_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
# 画像の参照（a:blip の r:embed、VML の v:imagedata の r:id）。r:link（外部のファイル）は読まない。
_IMAGE_REF_ATTRIBUTES = {
    f"{{{extract._NS_A}}}blip": f"{{{_NS_R}}}embed",
    f"{{{extract._NS_VML}}}imagedata": f"{{{_NS_R}}}id",
}
# 代替テキストを探す、画像を包む要素（内側から順に見る。Word は pic:pic の外の wp:inline・wp:anchor の docPr に書かれる）。
_CONTAINERS = ("pic", "sp", "inline", "anchor", "shape")
# 抜粋に入れる周りの文字の上限（検索結果の抜粋はさらに chunk_size 文字までにする。ingest._page_chunk_text）。
_CONTEXT_CHARS = 2000


class _Package:
    """Office Open XML（ZIP）のパーツと関係（_rels）を読む。"""

    def __init__(self, package: zipfile.ZipFile):
        self.package = package
        self.names = set(package.namelist())

    def xml(self, name: str):
        if name not in self.names:
            return None
        try:
            return ElementTree.fromstring(self.package.read(name))
        except ElementTree.ParseError:
            return None

    def rels(self, part: str) -> dict[str, tuple[str, str]]:
        """part の関係 ID → (種類の末尾（"image" 等）, 参照先のパーツ名)。外部のファイルへの関係は含めない。"""
        directory, base = posixpath.split(part)
        root = self.xml(posixpath.join(directory, "_rels", base + ".rels"))
        if root is None:
            return {}
        result = {}
        for rel in root.iter(f"{{{_NS_PKG_REL}}}Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            name = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(directory, target))
            result[rel.get("Id", "")] = (rel.get("Type", "").rsplit("/", 1)[-1], name)
        return result


class _Collector:
    """見つけた画像の参照を文書の順に集める（同じパーツは最初の1回だけ）。"""

    def __init__(self):
        # (位置, パーツ名, 抜粋)
        self.items: list[tuple[str, str, str]] = []
        self._seen: set[str] = set()

    def add(self, prefix: str, part: str, alt: str, context: str) -> None:
        if part in self._seen:
            return
        self._seen.add(part)
        lines = [f"[Image] {alt}"] if alt else []
        if context.strip():
            lines.append(context.strip()[:_CONTEXT_CHARS])
        self.items.append((f"{prefix} image ({part})".strip(), part, "\n".join(lines)))


def _image_refs(root, rels: dict[str, tuple[str, str]]):
    """root の下の画像の参照ごとに (参照している要素, 画像のパーツ名) を返す。"""
    for element in root.iter():
        attribute = _IMAGE_REF_ATTRIBUTES.get(element.tag)
        if attribute is None:
            continue
        kind, part = rels.get(element.get(attribute, ""), ("", ""))
        if kind == "image" and part:
            yield element, part


def _alt_of(element, parents: dict) -> str:
    """画像の参照（element）を包む図形の代替テキスト（内側の図形から順に、最初に見つかったもの）。"""
    current = parents.get(element)
    while current is not None:
        if current.tag.rsplit("}", 1)[-1] in _CONTAINERS and (alt := extract.image_alt_text(current)):
            return alt
        current = parents.get(current)
    return ""


def _parents(root) -> dict:
    return {child: parent for parent in root.iter() for child in parent}


def _collect_docx(pkg: _Package, collector: _Collector) -> None:
    """本文の画像。抜粋には画像のある段落と、その前後の文字のある段落（見出し・図の説明）を入れる。"""
    part = "word/document.xml"
    root = pkg.xml(part)
    if root is None:
        return
    _drop_fallbacks(root)
    rels = pkg.rels(part)
    paragraphs = list(root.iter(extract._w("p")))
    texts = [extract._docx_paragraph_text(p).strip() for p in paragraphs]
    parents = _parents(root)
    for i, paragraph in enumerate(paragraphs):
        refs = list(_image_refs(paragraph, rels))
        if not refs:
            continue
        before = next((t for t in reversed(texts[:i]) if t), "")
        after = next((t for t in texts[i + 1 :] if t), "")
        context = "\n".join(t for t in dict.fromkeys((before, texts[i], after)) if t)
        for element, image in refs:
            collector.add("", image, _alt_of(element, parents), context)


def _collect_pptx(pkg: _Package, collector: _Collector) -> None:
    """スライドの画像（背景・図形の塗りつぶしの画像も）。抜粋にはスライドの文字を入れる。位置は extract と同じ「Slide N」。"""
    presentation = pkg.xml("ppt/presentation.xml")
    if presentation is None:
        return
    targets = pkg.rels("ppt/presentation.xml")
    for number, slide_id in enumerate(presentation.iter(f"{{{_NS_P}}}sldId"), start=1):
        kind, part = targets.get(slide_id.get(f"{{{_NS_R}}}id", ""), ("", ""))
        root = pkg.xml(part) if kind == "slide" else None
        if root is None:
            continue
        _drop_fallbacks(root)
        context = extract._paragraphs_text(root)
        parents = _parents(root)
        for element, image in _image_refs(root, pkg.rels(part)):
            collector.add(f"Slide {number}", image, _alt_of(element, parents), context)


def _collect_xlsx(pkg: _Package, collector: _Collector) -> None:
    """シートに置いた画像。位置はシート名と、画像の左上が乗っているセル（extract の図形と同じ）。"""
    workbook = pkg.xml("xl/workbook.xml")
    if workbook is None:
        return
    targets = pkg.rels("xl/workbook.xml")
    for sheet in workbook.iter(f"{{{_NS_S}}}sheet"):
        _kind, part = targets.get(sheet.get(f"{{{_NS_R}}}id", ""), ("", ""))
        name = sheet.get("name", "")
        for kind, drawing in pkg.rels(part).values() if part else ():
            root = pkg.xml(drawing) if kind == "drawing" else None
            if root is None:
                continue
            _drop_fallbacks(root)
            rels = pkg.rels(drawing)
            parents = _parents(root)
            for anchor in _drawing_anchors(root):
                start = anchor.find(f"{{{_NS_XDR}}}from")
                row = col = None
                if start is not None:
                    row, col = _int_text(start, f"{{{_NS_XDR}}}row"), _int_text(start, f"{{{_NS_XDR}}}col")
                cell = f" {_cell_name(row, col)}" if row is not None and col is not None else ""
                for element, image in _image_refs(anchor, rels):
                    collector.add(f"Sheet: {name}{cell}", image, _alt_of(element, parents), "")


_COLLECTORS = {".docx": _collect_docx, ".pptx": _collect_pptx, ".xlsx": _collect_xlsx, ".xlsm": _collect_xlsx}


def extract_embedded_images(
    path: Path, data: bytes | None = None, *, dpi: int, min_px: int = 0, out_dir: Path | None = None
) -> list[PageImage]:
    """path（Office ファイル）の中の画像を、文書の順に1枚ずつ PNG にして返す。

    PageImage.locator は「スライド・シートのセル＋image (パーツ名)」（例: "Slide 3 image (ppt/media/image2.png)"）、
    PageImage.text は代替テキストと周りの文字（検索結果の抜粋）。画像は render.py の画像ファイルと同じく、長辺を A4 を dpi で
    画像化したときの長辺まで縮小する。幅か高さが min_px 未満の画像（アイコン等）と、同じ中身の画像の2枚目以降は含めない。
    開けない画像は飛ばす（1枚読めなくても、そのファイルの取り込みは続ける）。

    Raises:
        zipfile.BadZipFile 等: ファイル自体を読めない場合（本文の抽出で先に失敗一覧へ載るため、ふつうは起きない）。
    """
    from PIL import Image, ImageOps

    collect = _COLLECTORS.get(path.suffix.lower())
    if collect is None:
        return []
    write = _page_writer(out_dir)
    max_long_side = round(_A4_LONG_SIDE_INCHES * dpi)
    pages: list[PageImage] = []
    with zipfile.ZipFile(str(path) if data is None else io.BytesIO(data)) as package:
        pkg = _Package(package)
        collector = _Collector()
        collect(pkg, collector)
        digests: set[str] = set()
        for locator, part, text in collector.items:
            check_stopped()
            if part not in pkg.names:
                continue
            raw = package.read(part)
            digest = hashlib.sha256(raw).hexdigest()
            if digest in digests:
                continue
            digests.add(digest)
            try:
                with Image.open(io.BytesIO(raw)) as img:
                    if min(img.size) < min_px:
                        continue
                    png = _to_png(ImageOps.exif_transpose(img), max_long_side)
            except Exception as e:  # noqa: BLE001 - SVG・壊れた画像等。この1枚だけ飛ばす
                logger.info("vector_db ingest: skipping an embedded image that cannot be opened: %s %s: %s", path, part, e)
                continue
            pages.append(PageImage(locator, text, *write(png)))
    return pages
