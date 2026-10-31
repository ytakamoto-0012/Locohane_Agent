"""[vector_db] image_input（ページを画像にして埋め込む）のテスト。

Office ファイルの画像化（COM で Word 等を起動する）は環境に依存するため、取り込みのテストでは
render_pages を偽物に差し替える。PDF の画像化だけは pypdfium2 で実際に行う。
"""

from __future__ import annotations

import base64
import dataclasses
import io
import json
from pathlib import Path
import sqlite3

import pytest

from _vector_db_fakes import FakeEmbeddingServer, FakeQdrant, fake_vector, make_config
from src.vector_db import catalog as vcat
from src.vector_db import ingest, search
from src.vector_db.embeddings import embed_images
from src.vector_db.extract import ExtractError
from src.vector_db.render import PageImage, render_pages
from src.vector_db.store import image_collection_name, open_store

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# --- 画像化 -----------------------------------------------------------------------


def test_render_pages_renders_each_pdf_page_with_text(tmp_path):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(tmp_path / "a.pdf"))
    c.drawString(72, 720, "page one text")
    c.showPage()
    c.showPage()  # 文字の無いページ
    c.save()
    pages = render_pages(tmp_path / "a.pdf", dpi=50)
    assert [p.locator for p in pages] == ["p.1", "p.2"]
    assert "page one text" in pages[0].text and pages[1].text.strip() == ""
    assert all(p.png.startswith(_PNG_SIGNATURE) for p in pages)


def test_render_pages_rejects_unsupported_and_broken_files(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    with pytest.raises(ExtractError, match="Unsupported format"):
        render_pages(tmp_path / "a.md", dpi=50)
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")
    with pytest.raises(ExtractError):
        render_pages(tmp_path / "broken.pdf", dpi=50)


# --- 埋め込み ---------------------------------------------------------------------


def test_embed_images_sends_one_image_per_request_in_llama_server_format(tmp_path):
    cfg = make_config(tmp_path, vector_db_document_prefix="passage: ")
    server = FakeEmbeddingServer()
    vectors = embed_images(cfg, ["溶接".encode(), "検査".encode()], transport=server.transport)
    assert len(vectors) == 2
    assert [len(r["input"]) for r in server.requests] == [1, 1]
    [item] = server.requests[0]["input"]
    part = item["content"][0]
    assert part["type"] == "image_url"
    # 画像には document_prefix（テキスト用）を付けない
    assert base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == "溶接".encode()


# --- 取り込みと検索 ---------------------------------------------------------------


@pytest.fixture(params=["sqlite_vec", "qdrant"])
def env(request, tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    from reportlab.pdfgen import canvas

    # 中身の画像化は下の fake_render が代わりに行う（テキストとして取り込むときは実際の文字を読む）。
    c = canvas.Canvas(str(docs / "manual.pdf"))
    c.drawString(72, 720, "welding manual")
    c.showPage()
    c.save()
    (docs / "note.md").write_text("会議室の予約はポータルから行う。", encoding="utf-8")
    rendered = []

    def fake_render(path, dpi, **_):
        rendered.append((path.name, dpi))
        # 「画像」の代わりに文字列のバイト列を渡す（偽の埋め込みサーバーはそれを読んでベクターにする）。
        return [PageImage("p.1", "溶接ビードの幅は5mm以上", "溶接ビードの幅の図".encode()), PageImage("p.2", "", "外観検査の写真".encode())]

    monkeypatch.setattr(ingest, "render_pages", fake_render)
    # 本文の埋め込みとページ画像の統合を確かめるため、キーワード検索は使わない（tests/test_vector_db_keyword.py で確かめる）。
    cfg = make_config(
        tmp_path, vector_db_backend=request.param, vector_db_image_input=True, vector_db_image_dpi=72, vector_db_rrf_weight_keyword=0.0
    )
    embed = FakeEmbeddingServer()
    qdrant = FakeQdrant()
    kwargs = {"embedding_transport": embed.transport, "store_transport": qdrant.transport}
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    return {"cfg": cfg, "docs": docs, "kwargs": kwargs, "embed": embed, "rendered": rendered}


def _image_store_sources(cfg, kwargs) -> list[str]:
    """ページ画像のコレクション（docs.image）に入っているファイル名の一覧。"""
    store = open_store(cfg, transport=kwargs["store_transport"])
    try:
        hits = store.search(image_collection_name("docs"), fake_vector("x"), 100)
    finally:
        store.close()
    return sorted({Path(h.source_path).name for h in hits})


def test_ingest_embeds_text_and_page_images(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    result = ingest.ingest_collection(cfg, "docs", **kwargs)
    # manual.pdf は本文1チャンク＋ページ画像2枚、note.md は本文1チャンク
    assert (result.added, result.chunks, result.failed) == (2, 4, [])
    assert env["rendered"] == [("manual.pdf", 72)]
    image_requests = [r for r in env["embed"].requests if isinstance(r["input"][0], dict)]
    assert len(image_requests) == 2
    assert _image_store_sources(cfg, kwargs) == ["manual.pdf"]
    # ページ画像のファイルは保存しない（元のファイルを参照する）
    assert not any(cfg.vector_db_dir.rglob("*.png"))

    # 無関係なチャンクを落とすしきい値を効かせて検索する（テスト用設定の既定は 0.0）
    scored = dataclasses.replace(cfg, vector_db_min_score=0.5, vector_db_image_min_score=0.5)
    [hit] = search.search(scored, "外観検査の写真", top_k=1, **kwargs).hits
    assert (hit.locator, hit.matched) == ("p.2", ("image",))
    assert hit.text.startswith("(This page has no text") and hit.source_path.endswith("manual.pdf")
    text_hit = search.search(scored, "会議室の予約はポータルから行う。", top_k=1, **kwargs).hits[0]
    assert text_hit.source_path.endswith("note.md") and text_hit.matched == ("text",)

    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        modes = {Path(p).name: r.mode for p, r in catalog.files("docs").items()}
    assert modes == {"manual.pdf": "text+image", "note.md": "text"}

    # 変更が無ければ画像化し直さない
    assert ingest.ingest_collection(cfg, "docs", **kwargs).unchanged == 2
    assert len(env["rendered"]) == 1


def test_page_matching_both_text_and_image_is_merged_and_ranked_first(env, monkeypatch):
    cfg, kwargs = env["cfg"], env["kwargs"]
    # p.1 は本文（welding manual）と画像の両方が「welding」に近い。p.2 は画像だけ。
    monkeypatch.setattr(
        ingest, "render_pages", lambda path, dpi, **_: [PageImage("p.1", "welding manual", b"welding"), PageImage("p.2", "", b"wel")]
    )
    ingest.ingest_collection(cfg, "docs", **kwargs)
    hits = search.search(cfg, "welding", collection="docs", top_k=5, **kwargs).hits
    assert (hits[0].locator, hits[0].matched, hits[0].text) == ("p.1", ("text", "image"), "welding manual")
    assert [(h.locator, h.matched) for h in hits].count(("p.1", ("image",))) == 0  # 合算して1件にする


def test_image_hits_use_image_min_score(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    strict = dataclasses.replace(cfg, vector_db_image_min_score=1.0)
    hits = search.search(strict, "外観検査", collection="docs", top_k=5, **kwargs).hits
    assert hits  # 本文の結果は出る
    assert all("image" not in h.matched for h in hits)


def test_fuse_ranks_by_reciprocal_rank_not_raw_score():
    from src.vector_db.store import Hit

    # 画像の類似度は低めに出るが、画像の一覧で1位なら本文の2位より上に来る。
    text = [Hit("docs", 0.9, "/a.md", "", "a"), Hit("docs", 0.8, "/b.md", "", "b")]
    image = [Hit("docs", 0.2, "/c.pdf", "p.1", "c")]
    fused = search._fuse([("text", 1.0, text), ("image", 1.0, image)])
    assert [h.source_path for h in fused] == ["/a.md", "/c.pdf", "/b.md"]


def test_switching_image_input_reingests_only_affected_files(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    text_cfg = make_config(cfg.vector_db_dir.parent, vector_db_backend=cfg.vector_db_backend, vector_db_image_input=False, vector_db_rrf_weight_keyword=0.0)
    result = ingest.ingest_collection(text_cfg, "docs", **kwargs)
    # 内容の変わっていない PDF も本文だけで取り込み直す（画像のベクターは消える）。note.md は取り込み直さない
    assert (result.updated, result.unchanged, result.failed) == (1, 1, [])
    assert _image_store_sources(cfg, kwargs) == []
    [hit] = search.search(text_cfg, "welding manual", collection="docs", top_k=1, **kwargs).hits
    assert (hit.text, hit.matched) == ("welding manual", ("text",))
    # 画像に戻すと、また画像も埋め込む
    again = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert (again.updated, again.unchanged) == (1, 1)
    assert _image_store_sources(cfg, kwargs) == ["manual.pdf"]


def test_image_input_extensions_selects_documents_to_render(env):
    import openpyxl

    cfg, kwargs, docs = env["cfg"], env["kwargs"], env["docs"]
    wb = openpyxl.Workbook()
    wb.active.append(["品番", "A-100"])
    wb.save(docs / "parts.xlsx")
    # .pdf だけなら Excel はページ画像にしない（中の画像を取り出す。画像の無いブックは本文だけ）。
    default = dataclasses.replace(cfg, vector_db_image_input_extensions=(".pdf",))
    assert (ingest.file_mode(default, docs / "parts.xlsx"), ingest.file_mode(default, docs / "manual.pdf")) == ("text+embedded", "text+image")
    result = ingest.ingest_collection(default, "docs", **kwargs)
    assert (result.added, result.failed) == (3, [])
    assert env["rendered"] == [("manual.pdf", 72)]
    assert _image_store_sources(default, kwargs) == ["manual.pdf"]

    # Excel を加えると、内容の変わっていない Excel も画像を足して取り込み直す（PDF はそのまま）。
    with_excel = dataclasses.replace(cfg, vector_db_image_input_extensions=(".pdf", ".xlsx"))
    result = ingest.ingest_collection(with_excel, "docs", **kwargs)
    assert (result.updated, result.unchanged) == (1, 2)
    assert env["rendered"][1:] == [("parts.xlsx", 72)]
    assert _image_store_sources(with_excel, kwargs) == ["manual.pdf", "parts.xlsx"]

    # 空にすると、どの文書もページ画像にしない（画像のベクターを消して本文だけにする）。
    none = dataclasses.replace(cfg, vector_db_image_input_extensions=())
    result = ingest.ingest_collection(none, "docs", **kwargs)
    assert (result.updated, result.unchanged) == (2, 1)
    assert _image_store_sources(none, kwargs) == []


def test_text_less_pdf_is_registered_by_path_until_image_input(env):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(env["docs"] / "manual.pdf"))
    c.showPage()  # 文字の無い（スキャンした図面のような）PDF
    c.save()
    text_cfg = make_config(env["cfg"].vector_db_dir.parent, vector_db_backend=env["cfg"].vector_db_backend, vector_db_image_input=False, vector_db_rrf_weight_keyword=0.0)
    result = ingest.ingest_collection(text_cfg, "docs", **env["kwargs"])
    # 本文を取り出せなくても失敗にせず、ファイル名・フォルダ名で探せるようパスだけを登録する。
    assert (result.path_only, result.failed) == (1, [])
    hits = search.search_paths(text_cfg, "manual", **env["kwargs"]).hits
    assert [Path(h.source_path).name for h in hits][:1] == ["manual.pdf"]
    # image_input = true にすると、パスだけのファイルも画像として取り込み直す
    again = ingest.ingest_collection(env["cfg"], "docs", **env["kwargs"])
    assert (again.updated, again.path_only, again.failed) == (1, 0, [])
    assert _image_store_sources(env["cfg"], env["kwargs"]) == ["manual.pdf"]


def test_removed_files_and_rebuild_delete_image_vectors(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    ingest.ingest_collection(cfg, "docs", rebuild=True, **kwargs)
    assert _image_store_sources(cfg, kwargs) == ["manual.pdf"]
    (env["docs"] / "manual.pdf").unlink()
    assert ingest.ingest_collection(cfg, "docs", **kwargs).removed == 1
    assert _image_store_sources(cfg, kwargs) == []


def test_long_page_text_is_cut_to_chunk_size(env, monkeypatch):
    monkeypatch.setattr(ingest, "render_pages", lambda path, dpi, **_: [PageImage("p.9", "あ" * 500, b"x")])
    cfg = env["cfg"]
    ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    hit = search.search(cfg, "x", collection="docs", top_k=5, **env["kwargs"]).hits
    page = next(h for h in hit if h.locator == "p.9")
    assert page.text == "あ" * cfg.vector_db_chunk_size + "…"


def test_old_catalog_gets_mode_column(tmp_path):
    old = sqlite3.connect(tmp_path / "catalog.sqlite")
    old.executescript(
        """
        CREATE TABLE files (collection TEXT NOT NULL, source_path TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL,
            sha256 TEXT NOT NULL, chunk_count INTEGER NOT NULL, PRIMARY KEY (collection, source_path));
        INSERT INTO files VALUES ('docs', '/a.pdf', 1, 1.0, 'h', 2);
        """
    )
    old.close()
    with vcat.open_catalog(tmp_path) as catalog:
        assert catalog.files("docs")["/a.pdf"].mode == "text"


def test_config_reads_image_input(tmp_path, monkeypatch):
    from src.config import load_config

    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))
    for key in (
        "VECTOR_DB_IMAGE_INPUT", "VECTOR_DB_IMAGE_INPUT_EXTENSIONS", "VECTOR_DB_IMAGE_DPI", "VECTOR_DB_IMAGE_MIN_SCORE",
        "VECTOR_DB_EMBEDDED_IMAGES", "VECTOR_DB_EMBEDDED_IMAGE_MIN_PX",
    ):
        monkeypatch.delenv(key, raising=False)
    default = load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    assert (default.vector_db_image_input, default.vector_db_image_dpi, default.vector_db_image_min_score) == (False, 100, 0.1)
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"vector_db": {"image_input": "true", "image_dpi": "150"}}), encoding="utf-8")
    cfg = load_config(overrides_path=path, instance_name="inst1")
    assert (cfg.vector_db_image_input, cfg.vector_db_image_dpi) == (True, 150)
    assert default.vector_db_image_input_extensions == (".pdf",)
    assert (default.vector_db_embedded_images, default.vector_db_embedded_image_min_px) == (True, 128)
    path.write_text(json.dumps({"vector_db": {"image_input_extensions": "PDF, .xlsx"}}), encoding="utf-8")
    assert load_config(overrides_path=path, instance_name="inst1").vector_db_image_input_extensions == (".pdf", ".xlsx")
    path.write_text(json.dumps({"vector_db": {"image_input_extensions": ""}}), encoding="utf-8")
    assert load_config(overrides_path=path, instance_name="inst1").vector_db_image_input_extensions == ()
    path.write_text(json.dumps({"vector_db": {"image_input_extensions": ".pdf, .md"}}), encoding="utf-8")
    with pytest.raises(ValueError, match=r"image_input_extensions.*\.md"):
        load_config(overrides_path=path, instance_name="inst1")
    path.write_text(json.dumps({"vector_db": {"embedded_images": "false", "embedded_image_min_px": "0"}}), encoding="utf-8")
    cfg = load_config(overrides_path=path, instance_name="inst1")
    assert (cfg.vector_db_embedded_images, cfg.vector_db_embedded_image_min_px) == (False, 0)
    path.write_text(json.dumps({"vector_db": {"embedded_image_min_px": "-1"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="embedded_image_min_px"):
        load_config(overrides_path=path, instance_name="inst1")
    path.write_text(json.dumps({"vector_db": {"image_dpi": "0"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="image_dpi"):
        load_config(overrides_path=path, instance_name="inst1")
    path.write_text(json.dumps({"vector_db": {"image_min_score": "1.5"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="image_min_score"):
        load_config(overrides_path=path, instance_name="inst1")


# --- 画像ファイル -----------------------------------------------------------------


def test_render_pages_converts_image_files_to_png(tmp_path):
    import io

    import pillow_heif
    from PIL import Image

    # 透過 PNG は白で埋め、大きすぎる画像は A4 を dpi で画像化したときの長辺まで縮小する。
    Image.new("RGBA", (3000, 1000), (255, 0, 0, 0)).save(tmp_path / "big.png")
    [page] = render_pages(tmp_path / "big.png", dpi=50)
    assert (page.locator, page.text) == ("image", "")
    with Image.open(io.BytesIO(page.png)) as img:
        assert img.format == "PNG" and img.mode == "RGB"
        assert img.size == (585, 195)  # 297mm / 25.4 * 50dpi ≒ 585px
        assert img.getpixel((0, 0)) == (255, 255, 255)

    # 小さい画像は拡大しない。EXIF の回転（Orientation=6: 時計回りに90度）を反映する。
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (40, 20), "blue").save(tmp_path / "photo.jpg", exif=exif)
    [page] = render_pages(tmp_path / "photo.jpg", dpi=50)
    with Image.open(io.BytesIO(page.png)) as img:
        assert img.size == (20, 40)

    # 複数ページの TIFF はページごとに分ける。
    frames = [Image.new("RGB", (10, 10), c) for c in ("red", "green")]
    frames[0].save(tmp_path / "scan.tif", save_all=True, append_images=frames[1:])
    assert [p.locator for p in render_pages(tmp_path / "scan.tif", dpi=50)] == ["p.1", "p.2"]

    # HEIC も開ける。
    pillow_heif.register_heif_opener()
    Image.new("RGB", (16, 16), "green").save(tmp_path / "phone.heic")
    [page] = render_pages(tmp_path / "phone.heic", dpi=50)
    assert page.png.startswith(_PNG_SIGNATURE)

    (tmp_path / "broken.jpg").write_bytes(b"not an image")
    with pytest.raises(ExtractError):
        render_pages(tmp_path / "broken.jpg", dpi=50)


def test_image_files_are_embedded_as_images_only_when_image_input_is_true(env):
    from PIL import Image

    cfg, kwargs, docs = env["cfg"], env["kwargs"], env["docs"]
    Image.new("RGB", (8, 8), "red").save(docs / "photo.png")
    (docs / "phone.heic").write_bytes(b"heic")
    assert {ingest.file_mode(cfg, docs / n) for n in ("photo.png", "phone.heic")} == {"text+image"}
    names = [p.name for p in ingest.list_source_files(docs, True, cfg.vector_db_include_extensions)]
    assert names == ["manual.pdf", "note.md", "phone.heic", "photo.png"]

    result = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert (result.added, result.failed) == (4, [])
    assert sorted(n for n, _ in env["rendered"]) == ["manual.pdf", "phone.heic", "photo.png"]

    # image_input = false でも画像ファイルは対象のまま、画像のベクターを消してファイルパスだけを残す。
    text_cfg = make_config(cfg.vector_db_dir.parent, vector_db_backend=cfg.vector_db_backend, vector_db_image_input=False, vector_db_rrf_weight_keyword=0.0)
    assert {ingest.file_mode(text_cfg, docs / n) for n in ("photo.png", "phone.heic")} == {"path"}
    result = ingest.ingest_collection(text_cfg, "docs", **kwargs)
    assert (result.removed, result.path_only, result.failed) == (0, 2, [])
    with vcat.open_catalog(text_cfg.vector_db_dir) as catalog:
        records = catalog.files("docs")
    assert {(Path(s).name, r.mode, r.chunk_count) for s, r in records.items() if Path(s).suffix != ".md" and Path(s).suffix != ".pdf"} == {
        ("photo.png", "path", 0),
        ("phone.heic", "path", 0),
    }
    hits = search.search_paths(text_cfg, "photo", embedding_transport=kwargs["embedding_transport"], store_transport=kwargs.get("store_transport")).hits
    assert "photo.png" in {Path(h.source_path).name for h in hits}
    # 画像のベクターは消えている（本文の検索に画像の一致が出ない）。
    hits = search.search(text_cfg, "photo", embedding_transport=kwargs["embedding_transport"], store_transport=kwargs.get("store_transport")).hits
    assert all("image" not in h.matched for h in hits)


def test_old_catalog_gets_schedule_columns(tmp_path):
    old = sqlite3.connect(tmp_path / "catalog.sqlite")
    old.executescript(
        """
        CREATE TABLE collections (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', source_folder TEXT NOT NULL,
            recursive INTEGER NOT NULL DEFAULT 1, model TEXT, dim INTEGER, created_at TEXT NOT NULL, last_ingested_at TEXT);
        INSERT INTO collections (name, source_folder, created_at) VALUES ('docs', '/d', '2026-01-01T00:00:00');
        """
    )
    old.close()
    with vcat.open_catalog(tmp_path) as catalog:
        c = catalog.get("docs")
        assert (c.schedule_enabled, c.schedule_times, c.schedule_weekdays, c.document_prefix) == (False, (), (), None)
        c = catalog.update("docs", schedule_enabled=True, schedule_times=["9:05"], schedule_weekdays=[6, 5, 5])
        assert (c.schedule_enabled, c.schedule_times, c.schedule_weekdays) == (True, ("09:05",), (5, 6))
        with pytest.raises(vcat.CatalogError):
            catalog.update("docs", schedule_times=[])


# --- Office ファイルの中の画像（embedded_images） ----------------------------------


def _save_png(path: Path, size: tuple[int, int], color: str) -> Path:
    from PIL import Image

    Image.new("RGB", size, color).save(path)
    return path


def _office_files_with_images(folder: Path) -> None:
    """中に画像のある manual.docx・slides.pptx・parts.xlsx を folder に作る。"""
    import docx
    import openpyxl
    from openpyxl.drawing.image import Image as XLImage
    import pptx
    from pptx.util import Inches

    tmp = folder.parent / "images"
    tmp.mkdir(exist_ok=True)
    photo = _save_png(tmp / "photo.png", (300, 200), "red")
    chart = _save_png(tmp / "chart.png", (200, 300), "green")
    icon = _save_png(tmp / "icon.png", (16, 16), "blue")

    document = docx.Document()
    document.add_paragraph("溶接の手順")
    document.add_picture(str(photo))
    document.inline_shapes[0]._inline.docPr.set("descr", "溶接部の写真")
    document.add_paragraph("図1 溶接ビード")
    document.add_picture(str(icon))  # 小さい画像は埋め込まない
    document.add_picture(str(chart))
    document.save(folder / "manual.docx")

    presentation = pptx.Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "検査手順"
    picture = slide.shapes.add_picture(str(photo), Inches(1), Inches(2))
    picture._element.nvPicPr.cNvPr.set("descr", "外観検査の様子")
    slide.shapes.add_picture(str(photo), Inches(4), Inches(2))  # 同じ画像は1回だけ
    presentation.save(folder / "slides.pptx")

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "集計"
    sheet["A1"] = "品番"
    sheet.add_image(XLImage(str(chart)), "C5")
    workbook.save(folder / "parts.xlsx")


def test_extract_embedded_images_from_office_files(tmp_path):
    from PIL import Image

    from src.vector_db.embedded_images import extract_embedded_images

    _office_files_with_images(tmp_path)
    docx_images = extract_embedded_images(tmp_path / "manual.docx", dpi=100, min_px=64)
    assert [p.locator for p in docx_images] == ["image (word/media/image1.png)", "image (word/media/image3.png)"]
    first = docx_images[0].text.splitlines()
    # 代替テキストと、前後の段落（見出し・図の説明）。
    assert first[0] == "[Image] 溶接部の写真" and "溶接の手順" in first and "図1 溶接ビード" in first
    with Image.open(io.BytesIO(docx_images[0].png)) as img:
        assert (img.format, img.size) == ("PNG", (300, 200))
    # min_px = 0 なら小さい画像も含める。
    assert len(extract_embedded_images(tmp_path / "manual.docx", dpi=100, min_px=0)) == 3

    [slide_image] = extract_embedded_images(tmp_path / "slides.pptx", dpi=100, min_px=64)
    assert slide_image.locator == "Slide 2 image (ppt/media/image1.png)"
    assert slide_image.text.splitlines()[:2] == ["[Image] 外観検査の様子", "検査手順"]

    [sheet_image] = extract_embedded_images(tmp_path / "parts.xlsx", dpi=100, min_px=64, out_dir=tmp_path)
    assert sheet_image.locator == "Sheet: 集計 C5 image (xl/media/image1.png)"
    assert sheet_image.png == b"" and sheet_image.read_png().startswith(_PNG_SIGNATURE)


def test_extract_embedded_images_skips_images_that_cannot_be_opened(tmp_path):
    import zipfile

    from src.vector_db.embedded_images import extract_embedded_images

    _office_files_with_images(tmp_path)
    broken = tmp_path / "broken.docx"
    with zipfile.ZipFile(tmp_path / "manual.docx") as source, zipfile.ZipFile(broken, "w") as copy:
        for item in source.infolist():
            data = source.read(item.filename)
            copy.writestr(item, b"<svg/>" if item.filename == "word/media/image1.png" else data)
    assert [p.locator for p in extract_embedded_images(broken, dpi=100, min_px=64)] == ["image (word/media/image3.png)"]


def test_extract_segments_include_alt_text_of_images(tmp_path):
    from src.vector_db.extract import extract_segments

    _office_files_with_images(tmp_path)
    [document] = extract_segments(tmp_path / "manual.docx")
    assert document.text.splitlines()[:3] == ["溶接の手順", "[Image: 溶接部の写真]", "図1 溶接ビード"]
    slides = extract_segments(tmp_path / "slides.pptx")
    assert "[Image] 外観検査の様子" in slides[0].text.splitlines()
    [sheet] = extract_segments(tmp_path / "parts.xlsx")
    assert sheet.text.splitlines()[1].startswith("[Image C5] ")


def test_office_files_embed_their_images_instead_of_page_images(env):
    cfg, kwargs, docs = env["cfg"], env["kwargs"], env["docs"]
    _office_files_with_images(docs)
    cfg = dataclasses.replace(
        cfg, vector_db_image_input_extensions=(".pdf",), vector_db_embedded_images=True, vector_db_embedded_image_min_px=64
    )
    assert {ingest.file_mode(cfg, docs / n) for n in ("manual.docx", "slides.pptx", "parts.xlsx")} == {"text+embedded"}
    result = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert (result.added, result.failed) == (5, [])
    # Office は画像化しない（ページ画像は PDF だけ）。
    assert env["rendered"] == [("manual.pdf", 72)]
    assert _image_store_sources(cfg, kwargs) == ["manual.docx", "manual.pdf", "parts.xlsx", "slides.pptx"]
    hits = search.search(cfg, "外観検査の様子", collection="docs", top_k=20, **kwargs).hits
    slide = next(h for h in hits if h.locator == "Slide 2 image (ppt/media/image1.png)")
    assert slide.matched == ("image",) and slide.text.startswith("[Image] 外観検査の様子")
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        records = {Path(p).name: r for p, r in catalog.files("docs").items()}
    # manual.docx は本文1チャンク＋画像2枚。
    assert (records["manual.docx"].mode, records["manual.docx"].chunk_count) == ("text+embedded", 3)

    # embedded_images = false にすると、Office ファイルだけ本文だけで取り込み直す（中の画像のベクターは消える）。
    off = dataclasses.replace(cfg, vector_db_embedded_images=False)
    result = ingest.ingest_collection(off, "docs", **kwargs)
    assert (result.updated, result.unchanged) == (3, 2)
    assert _image_store_sources(off, kwargs) == ["manual.pdf"]

    # image_input_extensions に加えた形式は、従来どおりページ画像にする。
    pages = dataclasses.replace(cfg, vector_db_image_input_extensions=(".pdf", ".pptx"))
    assert ingest.file_mode(pages, docs / "slides.pptx") == "text+image"
