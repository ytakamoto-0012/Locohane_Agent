"""既存pptx(PowerPoint)テンプレートに操作列を適用し、別ファイルとして保存する。

pptx-tools スキルの実行スクリプト（progressive disclosure 第3段階）。
run_script ツールから
    python edit_pptx.py <template_path> <output_path> (--data JSON | --data-file PATH) [--overwrite]
の形で呼ばれる。

このプロジェクトのローカルエージェントは任意パスを自由にRead/Editできる
汎用ツールを持たない（run_script はこのスキル配下のスクリプト実行のみ）ため、
Anthropic公式pptxスキルのような「生XMLをエージェントが手で編集する」方式は
取れない。代わりに、構造化されたJSON操作列（テンプレートの何をどう変えるか）を
1回のスクリプト実行で丸ごと適用する方式を取る。テンプレートの意匠
（テーマ・マスター・レイアウト）は変更せず、既存スライドの中身だけを
書き換える／複製・削除・並び替えする。
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import sys
import traceback
from pathlib import Path

# office_shared/ から共有ヘルパー・THEMES/resolve_theme を import する（1-B 相互import方式）
_OFFICE_SHARED = Path(__file__).resolve().parent.parent.parent / "office_shared"
if str(_OFFICE_SHARED) not in sys.path:
    sys.path.append(str(_OFFICE_SHARED))
from pptx_common import backup_before_overwrite, read_text_with_fallback, register_output_path, setup_utf8_stdio  # noqa: E402
from office_theme import CHART_PALETTE, THEMES, resolve_theme  # noqa: E402
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.enum.text import PP_ALIGN
from pptx.exc import PackageNotFoundError
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Cm, Pt

# duplicate_slide で単純なXML deep copyでは正しく複製できないshape種別。
# （チャート/OLE/動画/SmartArt/グループはリレーションシップや埋め込みデータの
# 再登録が必要で、単純コピーだと壊れたpptxになるため明示的にエラーにする。）
UNSUPPORTED_DUPLICATE_TYPES = {
    MSO_SHAPE_TYPE.CHART,
    MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT,
    MSO_SHAPE_TYPE.LINKED_OLE_OBJECT,
    MSO_SHAPE_TYPE.MEDIA,
    MSO_SHAPE_TYPE.WEB_VIDEO,
    MSO_SHAPE_TYPE.OLE_CONTROL_OBJECT,
    MSO_SHAPE_TYPE.DIAGRAM,
    MSO_SHAPE_TYPE.IGX_GRAPHIC,
    MSO_SHAPE_TYPE.GROUP,
}


def _require(op: dict, key: str):
    value = op.get(key)
    if value is None:
        raise ValueError(f"'{key}' is required")
    return value


class EditContext:
    """opsバッチ全体で共有する状態。

    `slide`番号は常に「このopsバッチを開始した時点（＝直前のpptx-inspectが
    見せていたはずの状態）のスライド番号」として解決する。`duplicate_slide`/
    `delete_slide`/`reorder_slides`が後続スライド番号をずらしても、それより
    後のopで毎回シフト量を手計算する必要が無い（Excel編集での同種の事故
    ＝行挿入後の絶対行番号ズレ、と同根の問題への対応）。

    対して`duplicate_slide`の`insert_after`と`reorder_slides`の`order`は、
    「新しい内容をどこに置くか／並べ替えるか」という現在時点の配置に関する
    指定なので、現在のライブ位置基準のまま扱う（詳細はSKILL.md参照）。
    """

    def __init__(self, prs: Presentation):
        self.prs = prs
        # sldId要素はスライドの並び替え・複製では再生成されず同一オブジェクトの
        # ままなので、Python識別性でのlist.index()による生存追跡が可能。
        # 削除された要素だけがこのリストの走査から消える。
        self._original_slide_elements = list(prs.slides._sldIdLst)

    def _resolve_element(self, slide_num: int):
        idx = int(slide_num) - 1
        total = len(self._original_slide_elements)
        if idx < 0 or idx >= total:
            raise ValueError(f"Slide number does not exist: {slide_num} (total slides at the start of this ops batch: {total})")
        element = self._original_slide_elements[idx]
        if element not in list(self.prs.slides._sldIdLst):
            raise ValueError(f"Slide {slide_num} was already deleted by an earlier operation in this call (delete_slide, etc.)")
        return element

    def get_slide(self, slide_num: int):
        element = self._resolve_element(slide_num)
        live_idx = list(self.prs.slides._sldIdLst).index(element)
        return self.prs.slides[live_idx]

    def get_slide_element(self, slide_num: int):
        return self._resolve_element(slide_num)

    def current_position(self, slide_num: int) -> int:
        """スライド番号（バッチ開始時点基準）を、現在のライブな1始まり位置へ変換する。"""
        element = self._resolve_element(slide_num)
        return list(self.prs.slides._sldIdLst).index(element) + 1


def _get_shape(slide, shape_index: int):
    shapes = list(slide.shapes)
    idx = int(shape_index)
    if idx < 0 or idx >= len(shapes):
        raise ValueError(f"shape_index does not exist: {shape_index} (number of shapes on this slide: {len(shapes)})")
    return shapes[idx]


def _set_bullets(text_frame, bullets: list) -> None:
    tf = text_frame
    tf.clear()
    first = True
    for item in bullets:
        if isinstance(item, dict):
            text = str(item.get("text", ""))
            level = int(item.get("level", 0))
        else:
            text = str(item)
            level = 0
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.text = text
        p.level = level


def op_set_title(ctx: EditContext, op: dict) -> None:
    slide = ctx.get_slide(_require(op, "slide"))
    if slide.shapes.title is None:
        raise ValueError("This slide has no title placeholder")
    slide.shapes.title.text = str(op.get("text", ""))


def op_set_text(ctx: EditContext, op: dict) -> None:
    slide = ctx.get_slide(_require(op, "slide"))
    shape = _get_shape(slide, _require(op, "shape_index"))
    if not shape.has_text_frame:
        raise ValueError(f"shape_index {op['shape_index']} is a shape that cannot hold text")
    bullets = _require(op, "bullets")
    _set_bullets(shape.text_frame, bullets)


def op_set_table_cell(ctx: EditContext, op: dict) -> None:
    slide = ctx.get_slide(_require(op, "slide"))
    shape = _get_shape(slide, _require(op, "shape_index"))
    if not shape.has_table:
        raise ValueError(f"shape_index {op['shape_index']} is not a table")
    table = shape.table
    row = int(_require(op, "row"))
    col = int(_require(op, "col"))
    if row < 0 or row >= len(table.rows) or col < 0 or col >= len(table.columns):
        raise ValueError(f"Out of the table's range (the existing table has {len(table.rows)} rows and {len(table.columns)} columns; specified row={row}, col={col})")
    table.cell(row, col).text = str(op.get("text", ""))


def op_set_table(ctx: EditContext, op: dict) -> None:
    slide = ctx.get_slide(_require(op, "slide"))
    shape = _get_shape(slide, _require(op, "shape_index"))
    if not shape.has_table:
        raise ValueError(f"shape_index {op['shape_index']} is not a table")
    table = shape.table
    headers = op.get("headers") or []
    rows = op.get("rows") or []
    n_cols_needed = len(headers) if headers else (len(rows[0]) if rows else 0)
    n_rows_needed = (1 if headers else 0) + len(rows)
    if n_rows_needed != len(table.rows) or n_cols_needed != len(table.columns):
        raise ValueError(
            "Does not match the number of rows and columns of the existing table"
            f" (existing: {len(table.rows)} rows, {len(table.columns)} columns / specified: {n_rows_needed} rows, {n_cols_needed} columns). "
            "python-pptx cannot add or remove rows/columns of an existing table, "
            "so to change them, recreate it as a new slide with create_pptx.py."
        )
    for i, row_values in enumerate(rows):
        if len(row_values) != n_cols_needed:
            raise ValueError(f"rows[{i}] of table has a different number of columns (columns: {n_cols_needed}, rows[{i}]: {len(row_values)} columns)")
    r = 0
    if headers:
        for c, h in enumerate(headers):
            table.cell(0, c).text = str(h)
        r = 1
    for row_values in rows:
        for c, val in enumerate(row_values):
            table.cell(r, c).text = str(val)
        r += 1


def op_set_notes(ctx: EditContext, op: dict) -> None:
    slide = ctx.get_slide(_require(op, "slide"))
    slide.notes_slide.notes_text_frame.text = str(op.get("text", ""))


def op_replace_picture(ctx: EditContext, op: dict) -> None:
    slide = ctx.get_slide(_require(op, "slide"))
    shape = _get_shape(slide, _require(op, "shape_index"))
    if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
        raise ValueError(f"shape_index {op['shape_index']} is not an image")
    image_path = _require(op, "image_path")
    image_file = Path(str(image_path))
    if not image_file.is_file():
        raise ValueError(f"image_path not found: {image_path}")
    left, top, width, height = shape.left, shape.top, shape.width, shape.height
    old_element = shape._element
    parent = old_element.getparent()
    old_index = list(parent).index(old_element)
    parent.remove(old_element)
    new_shape = slide.shapes.add_picture(str(image_file), left, top, width=width, height=height)
    # add_pictureはspTreeの末尾に追加するため、元のz順序・shape_indexを保つよう
    # 差し替え対象があった位置へ挿し直す（他shapeのshape_indexがずれるのを防ぐ）。
    new_element = new_shape._element
    parent.remove(new_element)
    parent.insert(old_index, new_element)


def op_add_picture(ctx: EditContext, op: dict) -> None:
    """新規画像をスライドへ追加する。追加後のshape_indexはpptx-inspectで確認する。"""
    slide = ctx.get_slide(_require(op, "slide"))
    image_path = _require(op, "image_path")
    image_file = Path(str(image_path))
    if not image_file.is_file():
        raise ValueError(f"image_path not found: {image_path}")
    left = Cm(float(_require(op, "left_cm")))
    top = Cm(float(_require(op, "top_cm")))
    width = Cm(float(op["width_cm"])) if op.get("width_cm") else None
    height = Cm(float(op["height_cm"])) if op.get("height_cm") else None
    slide.shapes.add_picture(str(image_file), left, top, width=width, height=height)


_CROP_KEYS = ("crop_left", "crop_top", "crop_right", "crop_bottom")


def op_crop_picture(ctx: EditContext, op: dict) -> None:
    """既存画像shapeをトリミングする（`add_picture`/`replace_picture`で追加した
    画像、元テンプレートの画像のどちらにも使える）。python-pptxのcrop_*は
    画像の各辺から切り取る割合（0.0=切り取らない、1.0=その辺を完全に切り取る）
    で、元画像データ自体は保持したまま表示範囲だけを変える（Office側で
    トリミングを解除・再調整できる）。
    """
    slide = ctx.get_slide(_require(op, "slide"))
    shape = _get_shape(slide, _require(op, "shape_index"))
    if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
        raise ValueError(f"shape_index {op['shape_index']} is not an image")

    values = {key: op.get(key) for key in _CROP_KEYS}
    if all(v is None for v in values.values()):
        raise ValueError("At least one of crop_left/crop_top/crop_right/crop_bottom is required")
    for key, value in values.items():
        if value is not None and not (0.0 <= float(value) <= 1.0):
            raise ValueError(f"Specify {key} in the range 0.0-1.0: {value}")

    if values["crop_left"] is not None:
        shape.crop_left = float(values["crop_left"])
    if values["crop_top"] is not None:
        shape.crop_top = float(values["crop_top"])
    if values["crop_right"] is not None:
        shape.crop_right = float(values["crop_right"])
    if values["crop_bottom"] is not None:
        shape.crop_bottom = float(values["crop_bottom"])


_CHART_TYPE_MAP = {
    "bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "line": XL_CHART_TYPE.LINE,
    "pie": XL_CHART_TYPE.PIE,
}


def op_add_chart(ctx: EditContext, op: dict) -> None:
    """新規グラフをスライドへ追加する。categories/seriesをJSONで直書きする
    （pptxにはExcelのようなセル参照が無いため、set_tableのheaders/rowsと同じ発想）。
    """
    slide = ctx.get_slide(_require(op, "slide"))
    type_key = _require(op, "type")
    chart_type = _CHART_TYPE_MAP.get(type_key)
    if chart_type is None:
        raise ValueError(f"Unsupported chart type (only bar/line/pie are supported): {type_key}")
    categories = _require(op, "categories")
    series = _require(op, "series")
    if not isinstance(series, list) or not series:
        raise ValueError("series needs at least 1 object (name, values)")

    chart_data = CategoryChartData()
    chart_data.categories = categories
    for s in series:
        values = s.get("values") or []
        if len(values) != len(categories):
            raise ValueError(
                f"The number of values ({len(values)}) of series {s.get('name')!r}"
                f" does not match the number of categories ({len(categories)})"
            )
        chart_data.add_series(str(s.get("name", "")), values)

    left = Cm(float(_require(op, "left_cm")))
    top = Cm(float(_require(op, "top_cm")))
    width = Cm(float(_require(op, "width_cm")))
    height = Cm(float(_require(op, "height_cm")))
    graphic_frame = slide.shapes.add_chart(chart_type, left, top, width, height, chart_data)
    chart = graphic_frame.chart

    if op.get("title"):
        chart.has_title = True
        chart.chart_title.text_frame.text = str(op["title"])

    if op.get("show_data_labels", True):
        plot = chart.plots[0]
        plot.has_data_labels = True
        plot.data_labels.show_value = type_key != "pie"
        plot.data_labels.show_percentage = type_key == "pie"

    if op.get("theme"):
        resolve_theme(op["theme"])  # themeの存在検証のみ（系列色は常にCHART_PALETTE）
        if type_key != "pie":
            for i, chart_series in enumerate(chart.series):
                fill = chart_series.format.fill
                fill.solid()
                fill.fore_color.rgb = RGBColor.from_string(CHART_PALETTE[i % len(CHART_PALETTE)])


_ALIGN_MAP = {
    "left": PP_ALIGN.LEFT,
    "center": PP_ALIGN.CENTER,
    "right": PP_ALIGN.RIGHT,
    "justify": PP_ALIGN.JUSTIFY,
}


def _set_run_font_name(run, font_name: str) -> None:
    # run.font.name はlatin書体のみを設定するため、日本語等の実際の描画に
    # 使われる east-asian書体（a:ea）もoxmlで直接設定する（docx-editの
    # _set_east_asian_font と同じ理由）。
    run.font.name = font_name
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = OxmlElement("a:ea")
        rPr.append(ea)
    ea.set("typeface", font_name)


def _style_text_frame(
    text_frame,
    text_color: str | None,
    bold: bool | None,
    font_size_pt,
    italic: bool | None = None,
    underline: bool | None = None,
    font_name: str | None = None,
    align: str | None = None,
) -> None:
    for paragraph in text_frame.paragraphs:
        if align:
            paragraph.alignment = _ALIGN_MAP[align]
        for run in paragraph.runs:
            if text_color:
                run.font.color.rgb = RGBColor.from_string(text_color)
            if bold is not None:
                run.font.bold = bold
            if italic is not None:
                run.font.italic = italic
            if underline is not None:
                run.font.underline = underline
            if font_size_pt:
                run.font.size = Pt(float(font_size_pt))
            if font_name:
                _set_run_font_name(run, font_name)


def op_set_shape_style(ctx: EditContext, op: dict) -> None:
    """既存shape（テキストボックス・オートシェイプ・表）を明示的に再配色・再配置する。

    ユーザーが「もっと格好よく／見やすくして」「文字を中央寄せに」等、既存
    デザインの変更を明示的に頼んできた場合にのみ使う（set_title等の中身
    差し替えopは見た目に一切触れない設計なので、再配色・配置変更が必要な
    場面はこのopに限定する）。図形自体の位置/サイズ移動は set_shape_position
    を使う。
    """
    slide = ctx.get_slide(_require(op, "slide"))
    shape = _get_shape(slide, _require(op, "shape_index"))

    theme = resolve_theme(op["theme"]) if op.get("theme") else None
    role = op.get("role")
    text_color = op.get("text_color")
    bold = op.get("bold")
    italic = op.get("italic")
    underline = op.get("underline")
    font_size_pt = op.get("font_size_pt")
    font_name = op.get("font_name")
    fill_color = op.get("fill_color")
    border_color = op.get("border_color")
    align = op.get("align")

    if align is not None and align not in _ALIGN_MAP:
        raise ValueError(f"Unsupported align: {align!r} (supported: {', '.join(_ALIGN_MAP)})")

    if role == "heading":
        text_color = text_color or (theme["primary"] if theme else None)
        bold = True if bold is None else bold
    elif role == "table_header":
        fill_color = fill_color or (theme["primary"] if theme else None)
        text_color = text_color or (theme["text_on_primary"] if theme else None)
        bold = True if bold is None else bold
    elif role is not None:
        raise ValueError(f"Unsupported role: {role!r} (supported: heading, table_header)")

    if not any([text_color, bold is not None, italic is not None, underline is not None, font_size_pt, font_name, fill_color, border_color, align]):
        raise ValueError(
            "one of text_color/bold/italic/underline/font_size_pt/font_name/fill_color/"
            "border_color/align, or role (heading/table_header) + theme, is required"
        )

    if shape.has_table:
        if border_color:
            raise ValueError("Border color (border_color) of table shapes is not supported yet (only cell fill and text decoration are supported)")
        table = shape.table
        if role == "table_header":
            target_rows = [0]
        elif op.get("all_rows"):
            target_rows = range(len(table.rows))
        else:
            row = op.get("row")
            if row is None:
                raise ValueError(
                    "set_shape_style on a table shape needs the target rows"
                    " (role='table_header' for the header row, 'row' for a row number, 'all_rows':true for all rows)"
                )
            target_rows = [int(row)]
        for r in target_rows:
            if r < 0 or r >= len(table.rows):
                raise ValueError(f"Row does not exist: {r} (this table has {len(table.rows)} rows)")
            for c in range(len(table.columns)):
                cell = table.cell(r, c)
                if fill_color:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = RGBColor.from_string(fill_color)
                _style_text_frame(
                    cell.text_frame, text_color, bold, font_size_pt, italic=italic, underline=underline, font_name=font_name, align=align
                )
    else:
        if fill_color:
            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor.from_string(fill_color)
        if border_color:
            shape.line.color.rgb = RGBColor.from_string(border_color)
        if any([text_color, bold is not None, italic is not None, underline is not None, font_size_pt, font_name, align]):
            if not shape.has_text_frame:
                raise ValueError(f"shape_index {op['shape_index']} is a shape that cannot hold text")
            _style_text_frame(shape.text_frame, text_color, bold, font_size_pt, italic=italic, underline=underline, font_name=font_name, align=align)


def op_set_shape_position(ctx: EditContext, op: dict) -> None:
    """既存shapeの位置・サイズを変更する（cm単位）。

    ユーザーが「もっと左に」「大きくして」等、配置の変更を明示的に頼んできた
    場合にのみ使う。left/top/width/height のうち指定されたものだけを変更し、
    省略したものは元の値を維持する。
    """
    slide = ctx.get_slide(_require(op, "slide"))
    shape = _get_shape(slide, _require(op, "shape_index"))

    left = op.get("left_cm")
    top = op.get("top_cm")
    width = op.get("width_cm")
    height = op.get("height_cm")

    if left is None and top is None and width is None and height is None:
        raise ValueError("At least one of left_cm/top_cm/width_cm/height_cm is required")

    if left is not None:
        shape.left = Cm(float(left))
    if top is not None:
        shape.top = Cm(float(top))
    if width is not None:
        shape.width = Cm(float(width))
    if height is not None:
        shape.height = Cm(float(height))


def _copy_picture_into(new_slide, shape) -> None:
    image = shape.image
    _, rid = new_slide.part.get_or_add_image_part(io.BytesIO(image.blob))
    new_el = copy.deepcopy(shape._element)
    blip = new_el.find(f".//{qn('a:blip')}")
    if blip is not None:
        blip.set(qn("r:embed"), rid)
    new_slide.shapes._spTree.append(new_el)


def _duplicate_slide_once(prs: Presentation, source_slide):
    layout = source_slide.slide_layout
    # python-pptxのPresentationPart.add_slideは新規パート名を
    # 「/ppt/slides/slide<現在のスライド数+1>.xml」という単純な個数ベースで
    # 決めており、実際に空いているか（package.next_partnameのような
    # 網羅チェック）を確認しない。そのため delete_slide で歯抜けができた後に
    # duplicate_slide すると、末尾より手前に残っている既存スライドと
    # パート名が衝突し、保存時にZIP内で同名エントリが重複する
    # （python-pptx側の既知の制限）。add_slide前に衝突しない正しい空き
    # パート名を計算しておき、add_slide後に付け替えて回避する。
    safe_partname = prs.part.package.next_partname("/ppt/slides/slide%d.xml")
    new_slide = prs.slides.add_slide(layout)
    if new_slide.part.partname != safe_partname:
        new_slide.part.partname = safe_partname
    # add_slide が layout に合わせて自動生成する空プレースホルダは、
    # source_slide の内容をそのままコピーし直すため一旦すべて取り除く。
    for shape in list(new_slide.shapes):
        shape._element.getparent().remove(shape._element)

    for shape in source_slide.shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            _copy_picture_into(new_slide, shape)
        elif shape.shape_type in UNSUPPORTED_DUPLICATE_TYPES:
            raise ValueError(
                f"The source slide contains an unsupported shape type ({shape.shape_type}, name={shape.name!r})"
                ", so it cannot be duplicated (charts/OLE/video/SmartArt/groups are not supported)"
            )
        else:
            # プレースホルダ・表・テキストボックス等はXML要素をそのままdeep copyすれば
            # 書式・placeholder紐付け（layout側のidx/type一致）ともに維持される。
            new_slide.shapes._spTree.append(copy.deepcopy(shape._element))

    if source_slide.has_notes_slide:
        notes_text = source_slide.notes_slide.notes_text_frame.text
        if notes_text.strip():
            new_slide.notes_slide.notes_text_frame.text = notes_text

    return new_slide


def _move_slide_to(prs: Presentation, position: int) -> None:
    """add_slide で末尾に追加された直後のスライドを0始まりposition位置へ移動する。"""
    xml_slides = prs.slides._sldIdLst
    slides = list(xml_slides)
    last = slides[-1]
    xml_slides.remove(last)
    xml_slides.insert(position, last)


def op_duplicate_slide(ctx: EditContext, op: dict) -> None:
    source_num = int(_require(op, "slide"))
    source_slide = ctx.get_slide(source_num)
    # insert_after省略時は「複製元スライドの“現在の”ライブ位置の直後」に置く。
    # source_numをそのまま使うと、このバッチ内で先行opによりスライドが
    # 既にずれている場合に誤った位置へ挿入してしまう。
    insert_after = int(op["insert_after"]) if "insert_after" in op else ctx.current_position(source_num)
    count = int(op.get("count", 1))
    if count < 1:
        raise ValueError("Specify count as 1 or more")
    total = len(ctx.prs.slides)
    if insert_after < 0 or insert_after > total:
        raise ValueError(f"insert_after is invalid (specify it in the range 0-{total}): {insert_after}")

    position = insert_after
    for _ in range(count):
        _duplicate_slide_once(ctx.prs, source_slide)
        _move_slide_to(ctx.prs, position)
        position += 1


def op_delete_slide(ctx: EditContext, op: dict) -> None:
    slide_num = int(_require(op, "slide"))
    element = ctx.get_slide_element(slide_num)
    xml_slides = ctx.prs.slides._sldIdLst
    rid = element.get(qn("r:id"))
    ctx.prs.part.drop_rel(rid)
    xml_slides.remove(element)


def op_reorder_slides(ctx: EditContext, op: dict) -> None:
    """全スライドの並び順を指定順に変更する。

    `order`は他のopと異なり「バッチ開始時点の番号」ではなく、この操作を
    適用する時点でのライブなスライド番号(1始まり)の順列を指定する
    （このopの目的自体が「今存在する全スライドをどう並べるか」であり、
    かつ先行opでduplicate_slideが作った新規スライドにはバッチ開始時点の
    番号が存在しないため）。delete_slide/duplicate_slideと同一バッチで
    併用する場合は、reorder_slidesをそれらより後・かつ操作列の最後に
    置くことを推奨する（詳細はSKILL.md参照）。
    """
    order = op.get("order")
    total = len(ctx.prs.slides)
    if not isinstance(order, list) or sorted(order) != list(range(1, total + 1)):
        raise ValueError(f"Specify order as a permutation of all current slide numbers (1-{total}): {order!r}")
    xml_slides = ctx.prs.slides._sldIdLst
    slides = list(xml_slides)
    new_order_elms = [slides[i - 1] for i in order]
    for elm in new_order_elms:
        xml_slides.remove(elm)
    for elm in new_order_elms:
        xml_slides.append(elm)


OP_HANDLERS = {
    "set_title": op_set_title,
    "set_text": op_set_text,
    "set_table_cell": op_set_table_cell,
    "set_table": op_set_table,
    "set_notes": op_set_notes,
    "replace_picture": op_replace_picture,
    "add_picture": op_add_picture,
    "crop_picture": op_crop_picture,
    "add_chart": op_add_chart,
    "set_shape_style": op_set_shape_style,
    "set_shape_position": op_set_shape_position,
    "duplicate_slide": op_duplicate_slide,
    "delete_slide": op_delete_slide,
    "reorder_slides": op_reorder_slides,
}


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("template_path")
    parser.add_argument("output_path")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--data")
    group.add_argument("--data-file")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    template_path = Path(args.template_path)
    output_path = Path(args.output_path)

    if not template_path.exists():
        print(f"Template file not found: {args.template_path}", file=sys.stderr)
        return 1
    if template_path.is_dir():
        print(f"The specified path is a directory (files only): {args.template_path}", file=sys.stderr)
        return 1
    if template_path.resolve() == output_path.resolve() and not args.overwrite:
        print(
            "Saving to the same path as the template is allowed only with --overwrite. "
            "Specify a different output_path, or add --overwrite after confirming with the user that overwriting is OK.",
            file=sys.stderr,
        )
        return 1

    if args.data_file:
        data_file = Path(args.data_file)
        if not data_file.exists():
            print(f"Data file not found: {args.data_file}", file=sys.stderr)
            return 1
        raw = read_text_with_fallback(data_file)
    else:
        raw = args.data

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Could not parse it as JSON: {e}", file=sys.stderr)
        return 1

    operations = data.get("operations") if isinstance(data, dict) else None
    if not operations:
        print("The 'operations' key of data needs at least 1 operation definition.", file=sys.stderr)
        return 1

    try:
        prs = Presentation(str(template_path))
    except PackageNotFoundError:
        print(f"Could not load it as pptx (it may be broken or an unsupported format): {args.template_path}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - python-pptx側の想定外エラーもエラー終了に統一する
        print(f"Failed to read the pptx: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1

    ctx = EditContext(prs)
    for i, op in enumerate(operations):
        op_name = op.get("op") if isinstance(op, dict) else None
        handler = OP_HANDLERS.get(op_name)
        if handler is None:
            supported = ", ".join(sorted(OP_HANDLERS))
            print(f"Operation {i + 1}: unsupported op: {op_name!r} (supported: {supported})", file=sys.stderr)
            return 1
        try:
            handler(ctx, op)
        except ValueError as e:
            print(f"Failed to apply operation {i + 1} ({op_name}): {e}", file=sys.stderr)
            return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = backup_before_overwrite(output_path)
    try:
        prs.save(str(output_path))
    except OSError as e:
        print(f"Failed to save the file: {e}", file=sys.stderr)
        return 1

    result = {
        "output_path": str(output_path),
        "backup_path": str(backup_path) if backup_path else None,
        "total_slides": len(prs.slides),
        "size_bytes": output_path.stat().st_size,
        "applied_operations": len(operations),
    }
    path_memory = register_output_path(output_path, description="PPTX generated/updated by edit_pptx")
    if path_memory:
        result["path_memory"] = path_memory
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
