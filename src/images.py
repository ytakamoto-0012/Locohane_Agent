"""画像ファイル→OpenAI互換 data URL 変換の共通ヘルパー。

app.py（ユーザーアップロード画像）と src/tools.py（view_image ツール）の
両方から使う。画像をVision対応モデルへ渡す唯一の経路は、OpenAI互換APIの
`{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<...>"}}`
という content 要素なので、そこへの変換だけをここに集約する。

image_followup_message() は tools.py（ImageAwareToolNode）と subagent.py
（サブエージェントの独立ループ）の両方が、view_image の
response_format="content_and_artifact" が返す artifact から画像付き
HumanMessage を組み立てる際に使う共有ヘルパー。tools.py にも subagent.py
にも依存しない末端モジュールなので、ここに置いても循環 import にならない。
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

import pillow_heif
from langchain_core.messages import HumanMessage
from PIL import Image, ImageOps

# HEIC/HEIFはPillow標準では開けないため、Image.open()に渡す前にオープナー登録が必要。
pillow_heif.register_heif_opener()

logger = logging.getLogger(__name__)

# 対応拡張子→MIMEタイプ。mimetypes モジュールの環境依存を避けるため固定表で持つ。
_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

# Vision APIのdata URLとして解釈できないため、サイズに関わらず必ずJPEGへ変換するMIME。
_ALWAYS_CONVERT_MIMES = {"image/heic", "image/heif"}


def is_image_file(path: str | Path) -> bool:
    """拡張子が対応画像形式（png/jpg/jpeg/gif/webp/bmp/heic/heif）かどうかを返す。"""
    return Path(path).suffix.lower() in _MIME_BY_EXT


def _downscale_to_jpeg(
    data: bytes, max_long_side: int, jpeg_quality: int, *, force: bool = False, min_long_side: int = 0
) -> bytes | None:
    """長辺が max_long_side を超える画像を縮小し、JPEGバイト列として返す。

    Vision モデルが1枚の画像に費やすトークン量は画素数でほぼ決まるため、
    スマホ撮影のような 4032x3024 の画像をそのまま渡すと、数枚でサブエージェントの
    トークン上限に達してしまう（実測: レシピ写真297枚の中央値2.1MB・合計745MB）。

    Args:
        data: 元画像のバイト列。
        max_long_side: 縮小後の長辺のピクセル数。0以下ならリサイズはしない。
        jpeg_quality: 再エンコード時のJPEG品質（1-95）。
        force: True の場合、リサイズが不要でも必ずJPEGへ再エンコードする。
            HEIC/HEIFはVision APIのdata URLとして解釈できない形式のため、
            サイズ変更の要否に関わらず変換が必須（呼び出し元が指定する）。
        min_long_side: 拡大後の長辺のピクセル数の下限。0以下なら拡大はしない。
            アイコンサイズの小さい画像を回答本文へそのまま埋め込むと視認できない
            ため、`load_image_bytes` のインラインプレビュー用途でのみ使う
            （Vision向けの `to_data_url` では指定しない）。

    Returns:
        変換後のJPEGバイト列。force=Falseでリサイズも不要だった場合と、
        画像として読めなかった場合は None（呼び出し元は元のバイト列をそのまま使う）。
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            # 元バイト列をそのまま渡していたときはEXIFの回転情報も一緒に渡って
            # いたが、再エンコードすると失われる。ここで実際に回転させておかないと
            # 横倒しの写真をモデルに読ませることになる。
            img = ImageOps.exif_transpose(img)
            long_side = max(img.size)
            needs_shrink = max_long_side > 0 and long_side > max_long_side
            needs_enlarge = not needs_shrink and min_long_side > 0 and long_side < min_long_side
            if not needs_shrink and not needs_enlarge and not force:
                return None
            if needs_shrink:
                scale = max_long_side / long_side
                new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
                img = img.resize(new_size, Image.LANCZOS)
            elif needs_enlarge:
                scale = min_long_side / long_side
                new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
                img = img.resize(new_size, Image.LANCZOS)
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                # JPEGは透過を持てない。そのまま変換すると透過部分が黒く潰れて
                # 文字が読めなくなるため、白背景へ合成してから変換する。
                background = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                background.paste(rgba, mask=rgba.split()[-1])
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=jpeg_quality)
            return buffer.getvalue()
    except Exception:
        # 縮小の失敗で画像を渡せなくなる方が損失が大きいため、元画像へフォールバックする。
        logger.exception("Failed to shrink the image. Passing it at the original resolution")
        return None


def load_image_bytes(
    path: str | Path,
    *,
    max_long_side: int = 0,
    jpeg_quality: int = 85,
    min_long_side: int = 0,
) -> tuple[bytes, str]:
    """画像ファイルを読み込み、必要なら縮小・拡大・JPEG変換したバイト列とMIMEを返す。

    `to_data_url`（Vision向けdata URL化）と、`app.py` がチャット回答本文への
    画像埋め込みで使うセッションファイル化（Chainlitの `/project/file/...`
    配信経路）の両方が使う共通ロジック。

    Args:
        path: 画像ファイルの絶対パス。拡張子は _MIME_BY_EXT に含まれるもの
            であることを is_image_file() で事前に確認しておくこと。
        max_long_side: 縮小後の長辺のピクセル数の上限。0以下、または画像の
            長辺が既にこの値以下の場合は**再エンコードせず元のバイト列を
            そのまま使う**（劣化もPillowのコストも発生しない）。ただし
            HEIC/HEIFはこの設定に関わらず必ずJPEGへ変換する（多くの
            クライアント・ブラウザがHEIC/HEIFをそのまま解釈できないため）。
        jpeg_quality: 縮小・変換時のJPEG品質。
        min_long_side: 拡大後の長辺のピクセル数の下限。0以下、または画像の
            長辺が既にこの値以上の場合は拡大しない。回答本文へ埋め込む
            サムネイルが小さすぎて視認できない問題への対策
            （`_downscale_to_jpeg` 参照。Vision向け用途では通常0のまま使う）。

    Returns:
        (バイト列, MIMEタイプ) のタプル。縮小・拡大・変換した場合の mime は
        常に "image/jpeg" になる。

    Raises:
        KeyError: 拡張子が _MIME_BY_EXT にない場合。
    """
    p = Path(path)
    mime = _MIME_BY_EXT[p.suffix.lower()]
    data = p.read_bytes()
    force_convert = mime in _ALWAYS_CONVERT_MIMES
    if max_long_side > 0 or min_long_side > 0 or force_convert:
        converted = _downscale_to_jpeg(
            data, max_long_side, jpeg_quality, force=force_convert, min_long_side=min_long_side
        )
        if converted is not None:
            data = converted
            mime = "image/jpeg"
    return data, mime


def to_data_url(
    path: str | Path,
    *,
    max_long_side: int = 0,
    jpeg_quality: int = 85,
) -> str:
    """画像ファイルを base64 エンコードし、data URL 文字列として返す。

    Args:
        path: 画像ファイルの絶対パス。拡張子は _MIME_BY_EXT に含まれるもの
            であることを is_image_file() で事前に確認しておくこと。
        max_long_side: load_image_bytes 参照（config.ini
            [images].max_long_side_pixels）。
        jpeg_quality: load_image_bytes 参照（config.ini [images].jpeg_quality）。

    Returns:
        "data:<mime>;base64,<...>" 形式の文字列。

    Raises:
        KeyError: 拡張子が _MIME_BY_EXT にない場合。
    """
    data, mime = load_image_bytes(path, max_long_side=max_long_side, jpeg_quality=jpeg_quality)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


# 画像付き HumanMessage の additional_kwargs に、content 内の画像ブロックと同じ順で
# 元画像の参照（`@N 絶対パス` または絶対パス）を持たせるキー。HumanMessage の
# additional_kwargs は langchain-openai がリクエストへ含めないため LLM への入力は
# 変わらない。要約・緊急退避で画像をテキスト化する際に、base64 の代わりにこの
# 参照を書き出す（src/context_compaction.py の content_to_text）。
IMAGE_REFS_KEY = "locohane_image_refs"


def image_ref(path: Path | str, token: str | None = None) -> str:
    """画像の参照文字列（`@N 絶対パス`、token が無ければ絶対パスのみ）を返す。"""
    return f"{token} {path}" if token else str(path)


def image_followup_message(artifact: dict | None) -> HumanMessage | None:
    """artifact に画像URLがあれば、画像を content に持つ HumanMessage を返す。

    Args:
        artifact: ツール実行結果の artifact（例: analyze_image が返す
            {"image_url": "data:<mime>;base64,<...>", "ref": "@N 絶対パス"}）。
            None や image_url を持たない dict の場合は None を返す。
            "ref" があれば IMAGE_REFS_KEY として HumanMessage に持たせる。

    Returns:
        画像付き HumanMessage、または該当なしの場合は None。
    """
    if isinstance(artifact, dict) and "image_url" in artifact:
        ref = artifact.get("ref")
        return HumanMessage(
            content=[{"type": "image_url", "image_url": {"url": artifact["image_url"]}}],
            additional_kwargs={IMAGE_REFS_KEY: [ref]} if ref else {},
        )
    return None


def is_image_followup_message(message: object) -> bool:
    """image_followup_message() が作った（画像だけを content に持つ）HumanMessage か。

    ツール結果の画像をモデルへ見せるために ToolMessage の直後へ積まれるもので、
    ユーザー発言ではなくツール結果の続きにあたる。ユーザーが画像を添付した
    発言は、本文が空でも先頭にテキスト要素を持つ（app.py の _build_human_message）
    ため、これには当たらない。

    Args:
        message: 判定するメッセージ。

    Returns:
        画像のフォローアップなら True。
    """
    if not isinstance(message, HumanMessage) or not isinstance(message.content, list) or not message.content:
        return False
    return all(isinstance(part, dict) and part.get("type") == "image_url" for part in message.content)
