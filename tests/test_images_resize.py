"""src/images.py の画像縮小（to_data_url の max_long_side/jpeg_quality）の回帰テスト。

4032x3024 のような高解像度写真をVisionモデルへそのまま渡すと、数枚で
サブエージェントのトークン上限に達してしまう実測結果を受けて縮小機能を
追加した。既定は無効（max_long_side=0）で、縮小しない場合は元のバイト列を
一切加工しないことを保証する（劣化・Pillowコストの両方を避けるため）。
"""

import base64
import io

import pillow_heif
from PIL import Image, ImageOps

from src.images import is_image_file, to_data_url


def _make_png_bytes(size: tuple[int, int], mode: str = "RGB", color=(200, 50, 50)) -> bytes:
    img = Image.new(mode, size, color)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _make_heic_bytes(size: tuple[int, int], color=(200, 50, 50)) -> bytes:
    img = Image.new("RGB", size, color)
    heif_file = pillow_heif.from_pillow(img)
    buffer = io.BytesIO()
    heif_file.save(buffer, format="HEIF")
    return buffer.getvalue()


def _decode(data_url: str) -> tuple[str, bytes]:
    header, b64 = data_url.split(",", 1)
    mime = header.split(";")[0].removeprefix("data:")
    return mime, base64.b64decode(b64)


def test_no_resize_when_max_long_side_is_zero(tmp_path) -> None:
    data = _make_png_bytes((4000, 3000))
    path = tmp_path / "photo.png"
    path.write_bytes(data)

    url = to_data_url(path, max_long_side=0)
    mime, decoded = _decode(url)

    assert mime == "image/png"
    assert decoded == data


def test_no_resize_when_already_within_limit(tmp_path) -> None:
    data = _make_png_bytes((1000, 800))
    path = tmp_path / "photo.png"
    path.write_bytes(data)

    url = to_data_url(path, max_long_side=2048)
    mime, decoded = _decode(url)

    assert mime == "image/png"
    assert decoded == data


def test_resize_when_exceeding_limit(tmp_path) -> None:
    data = _make_png_bytes((4032, 3024))
    path = tmp_path / "photo.png"
    path.write_bytes(data)

    url = to_data_url(path, max_long_side=1024, jpeg_quality=85)
    mime, decoded = _decode(url)

    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(decoded)) as img:
        assert max(img.size) == 1024
        assert img.size[0] / img.size[1] == 4032 / 3024


def test_exif_orientation_is_applied_before_resize(tmp_path) -> None:
    # 縦長画像に「時計回り90度回転してから表示せよ」という Orientation=6 を付ける。
    # これを無視して縮小すると、表示上は横倒しの写真をモデルに読ませることになる。
    img = Image.new("RGB", (600, 800), (10, 20, 30))
    exif = img.getexif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", exif=exif)
    path = tmp_path / "rotated.jpg"
    path.write_bytes(buffer.getvalue())

    with Image.open(path) as original:
        expected = ImageOps.exif_transpose(original)
        expected_size = expected.size

    url = to_data_url(path, max_long_side=400)
    _, decoded = _decode(url)
    with Image.open(io.BytesIO(decoded)) as resized:
        # Orientation=6 は 600x800 を 800x600 相当へ回転させる想定。
        assert (resized.width > resized.height) == (expected_size[0] > expected_size[1])


def test_rgba_png_converts_to_jpeg_without_error(tmp_path) -> None:
    data = _make_png_bytes((2000, 1500), mode="RGBA", color=(255, 0, 0, 128))
    path = tmp_path / "transparent.png"
    path.write_bytes(data)

    url = to_data_url(path, max_long_side=500)
    mime, decoded = _decode(url)

    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(decoded)) as img:
        assert img.mode == "RGB"
        assert max(img.size) == 500


def test_heic_extension_is_recognized_as_image_file(tmp_path) -> None:
    path = tmp_path / "photo.heic"
    path.write_bytes(b"")

    assert is_image_file(path)


def test_heic_is_always_converted_to_jpeg_even_within_limit(tmp_path) -> None:
    # HEIC/HEIFはVision APIのdata URLとして解釈できないため、
    # max_long_side=0（縮小オフ）でも必ずJPEGへ変換されなければならない。
    data = _make_heic_bytes((800, 600))
    path = tmp_path / "photo.heic"
    path.write_bytes(data)

    url = to_data_url(path, max_long_side=0)
    mime, decoded = _decode(url)

    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(decoded)) as img:
        assert img.format == "JPEG"
        assert img.size == (800, 600)


def test_heic_is_resized_when_exceeding_limit(tmp_path) -> None:
    data = _make_heic_bytes((4032, 3024))
    path = tmp_path / "photo.heic"
    path.write_bytes(data)

    url = to_data_url(path, max_long_side=1024, jpeg_quality=85)
    mime, decoded = _decode(url)

    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(decoded)) as img:
        assert max(img.size) == 1024
