"""ベクターDB の取り込み・検索の異常系（コードチェックで見つかった不具合）の回帰テスト。

- 複数ページの TIFF のページ画像・ページ画像の一時ファイル化（メモリー対策）
- 中身を埋め込めないファイル（image_input = false の画像・文字の無い PDF）のパスだけの登録
- 埋め込みを待つ間のコミット（書き込みのロックを持ち続けない）・全ファイルの準備後の停止
- 子プロセスの異常終了・シンボリックリンク／ジャンクションのループ
- Office のマクロ無効化・ダミーのパスワード・変換の時間制限
- 検索ツールの想定外の例外
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types

import httpx
import pytest

from _vector_db_fakes import FakeEmbeddingServer, FakeQdrant, make_config
from src.vector_db import catalog as vcat
from src.vector_db import embeddings, ingest, render, search
from src.vector_db.extract import ExtractError


@pytest.fixture(autouse=True)
def _reset_embedding_state(monkeypatch):
    monkeypatch.setattr(embeddings, "_sleep", lambda seconds: None)
    monkeypatch.setattr(embeddings, "_failed_until", {})
    monkeypatch.setattr(embeddings, "_float_only", set())


@pytest.fixture(params=["sqlite_vec", "qdrant"])
def env(request, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    cfg = make_config(tmp_path, vector_db_backend=request.param)
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    return {
        "cfg": cfg,
        "docs": docs,
        "embed": embed,
        "kwargs": {"embedding_transport": embed.transport, "store_transport": qdrant.transport},
    }


def _config(env, **changes):
    return make_config(env["cfg"].vector_db_dir.parent, vector_db_backend=env["cfg"].vector_db_backend, **changes)


# --- ページ画像 ---------------------------------------------------------------------------


def test_multi_page_tiff_keeps_each_page(tmp_path):
    from PIL import Image

    frames = [Image.new("RGB", (40, 40), color) for color in ("red", "green", "blue")]
    frames[0].save(tmp_path / "scan.tif", save_all=True, append_images=frames[1:])
    pages = render.render_pages(tmp_path / "scan.tif", dpi=50)
    colors = []
    for page in pages:
        with Image.open(io.BytesIO(page.png)) as img:
            colors.append(img.convert("RGB").getpixel((1, 1)))
    # ImageSequence.Iterator を list() で先に集めると、すべて最後のページ（青）になっていた
    assert [p.locator for p in pages] == ["p.1", "p.2", "p.3"]
    assert colors == [(255, 0, 0), (0, 128, 0), (0, 0, 255)]


def _write_pdf(path: Path, pages: list[str]) -> None:
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    for text in pages:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()


def test_render_pages_writes_pngs_to_out_dir(tmp_path):
    _write_pdf(tmp_path / "a.pdf", ["page one", "page two"])
    out = tmp_path / "out"
    out.mkdir()
    pages = render.render_pages(tmp_path / "a.pdf", dpi=50, out_dir=out)
    # メモリーには画像を持たず、ファイルに書き出す（400ページの PDF 等でもメモリーを使い切らないため）
    assert [p.png for p in pages] == [b"", b""]
    assert all(p.png_path is not None and p.png_path.parent == out for p in pages)
    assert all(p.read_png().startswith(b"\x89PNG") for p in pages)


def test_ingest_removes_page_images_after_writing(env, tmp_path, monkeypatch):
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    _write_pdf(env["docs"] / "manual.pdf", ["welding manual", "inspection"])
    cfg = _config(env, vector_db_image_input=True, vector_db_image_dpi=30)
    result = ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    assert (result.added, result.failed) == (1, [])
    images = [r for r in env["embed"].requests if isinstance(r["input"][0], dict)]
    assert len(images) == 2  # 1ページずつ、書き出した PNG を読んで送る
    assert list(temp_root.iterdir()) == []


# --- パスだけの登録 -----------------------------------------------------------------------


def test_images_only_collection_is_searchable_by_path(env):
    from PIL import Image

    (env["docs"] / "溶接部").mkdir()
    Image.new("RGB", (8, 8), "red").save(env["docs"] / "溶接部" / "外観_NG例.jpg")
    cfg = env["cfg"]
    assert not cfg.vector_db_image_input
    result = ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    assert (result.added, result.path_only, result.chunks, result.failed) == (1, 1, 0, [])
    # 中身は読まずにパスだけを埋め込む
    assert [r["input"] for r in env["embed"].requests] == [["溶接部/外観_NG例.jpg"]]
    hits = search.search_paths(cfg, "外観", **env["kwargs"]).hits
    assert [h.text for h in hits] == ["溶接部/外観_NG例.jpg"]
    # 本文の検索は、本文のある文書が無い旨を返す
    with pytest.raises(search.SearchError, match="search_document_paths"):
        search.search(cfg, "外観", **env["kwargs"])
    assert ingest.ingest_collection(cfg, "docs", **env["kwargs"]).unchanged == 1


# --- コミット・停止 -----------------------------------------------------------------------


def test_writes_are_committed_while_waiting_for_embeddings(env, monkeypatch):
    """埋め込みを待つ間も、期限が来たらコミットして書き込みのロックを手放す。"""
    monkeypatch.setattr(ingest._Writer, "_COMMIT_INTERVAL_SECONDS", 0.2)
    docs, cfg = env["docs"], env["cfg"]
    (docs / "a.md").write_text("溶接の手順", encoding="utf-8")
    (docs / "b.md").write_text("検査の手順", encoding="utf-8")
    embed = env["embed"]
    state = {"count": 0, "other_write": None}

    def handler(request):
        state["count"] += 1
        if state["count"] == 2:
            # 1つ目のファイルを書いた後、2つ目の埋め込みに時間がかかっている間に、別の接続（設定ダッシュボード等）が書き込む。
            time.sleep(0.6)
            conn = sqlite3.connect(cfg.vector_db_dir / vcat.CATALOG_FILENAME, timeout=0.5)
            try:
                with conn:
                    conn.execute("UPDATE collections SET description = 'edited' WHERE name = 'docs'")
                state["other_write"] = "ok"
            except sqlite3.OperationalError as e:
                state["other_write"] = str(e)
            finally:
                conn.close()
        return embed.handler(request)

    kwargs = {**env["kwargs"], "embedding_transport": httpx.MockTransport(handler)}
    result = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert result.added == 2
    assert state["other_write"] == "ok"
    assert search.list_collections(cfg)[0].description == "edited"


def test_stop_is_honored_after_all_files_started(env):
    """全ファイルの準備を始めた後でも、停止を求めたら残りの埋め込みを送らない。"""
    cfg = _config(env, vector_db_embedding_batch_size=1, vector_db_chunk_size=50, vector_db_chunk_overlap=0)
    (env["docs"] / "long.md").write_text("\n\n".join(f"段落{i}の本文。" * 3 for i in range(20)), encoding="utf-8")
    stop = {"flag": False}
    embed = env["embed"]

    def handler(request):
        stop["flag"] = True  # 最初の埋め込みのリクエストが届いた時点で「停止」
        return embed.handler(request)

    kwargs = {**env["kwargs"], "embedding_transport": httpx.MockTransport(handler)}
    result = ingest.ingest_collection(cfg, "docs", should_stop=lambda: stop["flag"], **kwargs)
    assert result.added == 0
    assert len(embed.requests) <= 2  # 動いていたリクエストの分だけ
    assert search.list_collections(cfg)[0].file_count == 0


def test_stop_does_not_wait_for_a_file_being_prepared(env, monkeypatch):
    """巨大なファイルの本文の抽出中に停止を求めたら、抽出の終わりを待たずに止め、抽出も途中でやめさせる。"""
    from src.vector_db.extract import ExtractStopped, check_stopped

    (env["docs"] / "huge.md").write_text("巨大な文書", encoding="utf-8")
    state = {"started": threading.Event(), "stopped": threading.Event()}

    def slow_chunks(options, path, data=None):
        state["started"].set()
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:  # 1ページずつ抽出している間
                check_stopped()
                time.sleep(0.01)
        except ExtractStopped:
            state["stopped"].set()
            raise
        return []

    real_chunks = ingest._chunk_segments
    monkeypatch.setattr(ingest, "_chunk_segments", slow_chunks)
    slots = ingest.IngestSlots(1)
    started = time.monotonic()
    result = ingest.ingest_collection(
        env["cfg"], "docs", should_stop=lambda: state["started"].is_set(), slots=slots, **env["kwargs"]
    )
    assert time.monotonic() - started < 5
    assert result.added == 0 and result.removed == 0
    # 抽出は停止の印で途中でやめ、準備の枠も返る（設定ダッシュボードの「準備中」が消える）。
    assert state["stopped"].wait(5)
    deadline = time.monotonic() + 5
    while slots.snapshot()["load"] != [None] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert slots.snapshot()["load"] == [None]
    # 次回の取り込みで処理する。
    monkeypatch.setattr(ingest, "_chunk_segments", real_chunks)
    assert ingest.ingest_collection(env["cfg"], "docs", **env["kwargs"]).added == 1


def test_scan_stops_when_requested(tmp_path):
    for i in range(3):
        sub = tmp_path / f"d{i}"
        sub.mkdir()
        (sub / "a.md").write_text("x", encoding="utf-8")
    for workers in (1, 2):
        scan = ingest.scan_source_files(tmp_path, True, (".md",), workers=workers, should_stop=lambda: True)
        assert scan.stopped
    assert not ingest.scan_source_files(tmp_path, True, (".md",), workers=2).stopped


# --- 子プロセスの異常終了 -----------------------------------------------------------------


class _CrashingPool:
    """ProcessPoolExecutor の偽物。"crash" を含むファイルの準備は子プロセスが落ちたことにする。"""

    created = 0

    def __init__(self, *args, **kwargs):
        type(self).created += 1

    def submit(self, fn, *args):
        from concurrent.futures import Future
        from concurrent.futures.process import BrokenProcessPool

        future = Future()
        if "crash" in args[2].name:
            future.set_exception(BrokenProcessPool("A process in the process pool was terminated abruptly"))
        else:
            future.set_result(fn(*args))
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def test_crashing_child_process_fails_only_that_file(env, monkeypatch):
    monkeypatch.setattr(ingest, "ProcessPoolExecutor", _CrashingPool)
    _CrashingPool.created = 0
    for name in ("a.md", "b_crash.md", "c.md"):
        (env["docs"] / name).write_text(f"{name} の本文", encoding="utf-8")
    cfg = _config(env, vector_db_ingest_processes=2)
    result = ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    assert result.added == 2
    assert [(Path(p).name, "child process for text extraction/rendering crashed" in r) for p, r in result.failed] == [("b_crash.md", True)]
    # 異常終了したら新しいプロセスプールで続ける（巻き込まれたファイルは1つずつ準備し直す）
    assert _CrashingPool.created >= 3


# --- リンクのループ -----------------------------------------------------------------------


def _make_junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        os.symlink(target, link, target_is_directory=True)
        return
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if completed.returncode != 0:
        pytest.skip("ジャンクションを作れない環境です")


def test_scan_does_not_loop_through_junctions(tmp_path):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.md").write_text("a", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "b.md").write_text("b", encoding="utf-8")
    _make_junction(root / "sub" / "back", root)  # 取り込み元自身を指すループ
    _make_junction(root / "sub" / "up", tmp_path)  # 取り込み元を含むフォルダ
    _make_junction(root / "ext1", outside)  # 取り込み元の外（たどる）
    _make_junction(root / "ext2", outside)  # 同じ実体へのもう1つのリンク（重複させない）
    try:
        for workers in (1, 4):
            files = ingest.scan_source_files(root, True, (".md",), workers=workers).files
            assert sorted(Path(p).name for p, _ in files) == ["a.md", "b.md"]
    finally:
        for link in (root / "sub" / "back", root / "sub" / "up", root / "ext1", root / "ext2"):
            if os.path.lexists(link):
                os.rmdir(link) if sys.platform == "win32" else os.unlink(link)


# --- Office ---------------------------------------------------------------------------------


class _FakeOfficeApp:
    def __init__(self, log, block: threading.Event | None = None):
        self.log, self.block = log, block
        self.Documents = types.SimpleNamespace(Open=self._open_doc)
        self.Workbooks = types.SimpleNamespace(Open=self._open_book)

    def _open_doc(self, *args, **kwargs):
        self.log.append(("word", args, kwargs))
        if self.block is not None:
            self.block.wait(5)
            raise RuntimeError("RPC server is unavailable")  # プロセスを終了されると COM の呼び出しはエラーで返る
        return types.SimpleNamespace(SaveAs2=lambda path, fmt: None, Close=lambda **kw: None)

    def _open_book(self, *args, **kwargs):
        self.log.append(("excel", args, kwargs))
        return types.SimpleNamespace(Worksheets=[], Close=lambda **kw: None)

    def Quit(self):
        self.log.append("quit")


@pytest.fixture
def fake_office(monkeypatch):
    log: list = []
    apps: list[_FakeOfficeApp] = []
    state = {"block": None}
    pythoncom = types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)

    def dispatch(prog_id):
        app = _FakeOfficeApp(log, state["block"])
        apps.append(app)
        return app

    client = types.SimpleNamespace(DispatchEx=dispatch)
    win32com = types.ModuleType("win32com")
    win32com.client = client
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(render, "_office", threading.local())
    return log, apps, state


def test_office_apps_disable_macros_and_pass_dummy_passwords(fake_office, tmp_path):
    log, apps, _state = fake_office
    render._export_office_to_pdf(tmp_path / "a.xlsm", tmp_path)
    render._export_office_to_pdf(tmp_path / "a.docx", tmp_path)
    excel, word = apps
    # 取り込み元のマクロ（Workbook_Open 等）を実行しない
    assert excel.AutomationSecurity == 3 and excel.EnableEvents is False
    assert word.AutomationSecurity == 3
    [(_, excel_args, excel_kwargs)] = [e for e in log if isinstance(e, tuple) and e[0] == "excel"]
    [(_, word_args, word_kwargs)] = [e for e in log if isinstance(e, tuple) and e[0] == "word"]
    # パスワード付きのファイルを入力画面で止めずにエラーにする（位置引数で渡す）
    assert excel_args[4] == render._DUMMY_PASSWORD and excel_args[5] is None and not excel_kwargs
    assert word_args[4] == render._DUMMY_PASSWORD and not word_kwargs


def test_office_export_is_killed_after_timeout(fake_office, tmp_path, monkeypatch):
    log, apps, state = fake_office
    block = threading.Event()
    state["block"] = block
    killed = []
    monkeypatch.setattr(render, "_app_pid", lambda prog_id, app: 4242)
    monkeypatch.setattr(render.os, "kill", lambda pid, sig: (killed.append(pid), block.set()))
    started = time.monotonic()
    with pytest.raises(render.OfficeTimeout, match="office_timeout_seconds"):
        render._export_office_to_pdf(tmp_path / "a.docx", tmp_path, timeout=0.3)
    assert time.monotonic() - started < 3
    # 終了させたアプリは使い回さず、やり直しもしない（同じ確認画面でまた止まるため）
    assert killed == [4242] and len(apps) == 1 and "quit" in log


def test_office_export_is_killed_when_ingestion_stops(fake_office, tmp_path, monkeypatch):
    """取り込みの停止の印ができたら、時間制限より前でも Office を終了して ExtractStopped にする。"""
    from src.vector_db.extract import ExtractStopped, watch_stop_file

    log, apps, state = fake_office
    block = threading.Event()
    state["block"] = block
    killed = []
    monkeypatch.setattr(render, "_app_pid", lambda prog_id, app: 4242)
    monkeypatch.setattr(render.os, "kill", lambda pid, sig: (killed.append(pid), block.set()))
    stop_file = tmp_path / "STOP"
    threading.Timer(0.3, stop_file.touch).start()
    started = time.monotonic()
    with watch_stop_file(str(stop_file)), pytest.raises(ExtractStopped):
        render._export_office_to_pdf(tmp_path / "a.docx", tmp_path, timeout=60)
    assert time.monotonic() - started < 3
    assert killed == [4242] and len(apps) == 1 and "quit" in log


def test_office_timeout_becomes_a_failed_file(env, monkeypatch):
    (env["docs"] / "stuck.docx").write_bytes(b"x")
    cfg = _config(env, vector_db_image_input=True, vector_db_image_input_extensions=(".pdf", ".docx"), vector_db_office_timeout_seconds=7)
    seen = {}

    def fake_render(path, dpi, *, out_dir=None, office_timeout=None, temp_dir=None):
        seen["timeout"] = office_timeout
        raise render.OfficeTimeout("Office の PDF への書き出しが 7 秒で終わらなかったため中止しました")

    monkeypatch.setattr(ingest, "render_pages", fake_render)
    monkeypatch.setattr(ingest, "_chunk_segments", lambda options, path, data=None: [])
    result = ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    assert seen["timeout"] == 7
    assert [(Path(p).name, "7 秒" in r) for p, r in result.failed] == [("stuck.docx", True)]


def test_ingest_temp_dir_holds_page_images_and_office_pdfs(env, monkeypatch, tmp_path):
    (env["docs"] / "a.docx").write_bytes(b"x")
    temp_root = tmp_path / "ingest_tmp" / "nested"
    cfg = _config(
        env, vector_db_image_input=True, vector_db_image_input_extensions=(".docx",), vector_db_ingest_temp_dir=str(temp_root)
    )
    seen = {}

    def fake_render(path, dpi, *, out_dir=None, office_timeout=None, temp_dir=None):
        seen["out_dir"], seen["temp_dir"] = out_dir, temp_dir
        return [render.PageImage("p.1", "", b"page")]

    monkeypatch.setattr(ingest, "render_pages", fake_render)
    monkeypatch.setattr(ingest, "_chunk_segments", lambda options, path, data=None: [])
    ingest.ingest_collection(cfg, "docs", **env["kwargs"])
    # 取り込み1回ごとのフォルダを ingest_temp_dir の下に作り（無ければ ingest_temp_dir も作る）、終わったら消す。
    assert Path(seen["temp_dir"]).parent == temp_root
    assert Path(seen["out_dir"]).parent == Path(seen["temp_dir"])
    assert temp_root.is_dir() and not any(temp_root.iterdir())


# --- 検索ツール -----------------------------------------------------------------------------


def test_search_tools_return_unexpected_errors_as_text(monkeypatch, tmp_path):
    from src.tools import _state, vector_db_tools

    monkeypatch.setattr(_state, "_LLM_CONFIG", make_config(tmp_path))

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: vec_c1")  # 「再構築」と同時に検索した等

    monkeypatch.setattr(vector_db_tools.vector_search, "search", broken)
    monkeypatch.setattr(vector_db_tools.vector_search, "search_paths", broken)
    monkeypatch.setattr(vector_db_tools.vector_search, "list_collections", broken)
    for tool, args in (
        (vector_db_tools.search_documents, {"query": "溶接"}),
        (vector_db_tools.search_document_paths, {"query": "溶接"}),
        (vector_db_tools.list_document_collections, {}),
    ):
        text = tool.invoke(args)
        assert text.startswith("Error:") and "no such table" in text


def test_extract_error_is_still_a_failed_file(env, monkeypatch):
    """読めないファイル（壊れたファイル等）は、パスだけの登録にせず失敗一覧に載せる（次回やり直す）。"""
    (env["docs"] / "broken.md").write_text("x", encoding="utf-8")

    def raise_extract(*args, **kwargs):
        raise ExtractError("壊れています")

    monkeypatch.setattr(ingest, "extract_segments", raise_extract)
    result = ingest.ingest_collection(env["cfg"], "docs", **env["kwargs"])
    assert (result.added, result.path_only, [r for _, r in result.failed]) == (0, 0, ["壊れています"])


def test_encrypted_office_file_has_clear_reason(tmp_path):
    # パスワードを付けた docx・xlsx・pptx は ZIP ではなく OLE の複合ファイルになる。
    from src.vector_db.extract import extract_segments

    (tmp_path / "secret.docx").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 512)
    with pytest.raises(ExtractError, match="password-protected"):
        extract_segments(tmp_path / "secret.docx")
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    # 単に壊れているファイルは、パスワード付きとは言わない
    with pytest.raises(ExtractError) as excinfo:
        extract_segments(tmp_path / "broken.docx")
    assert "password" not in str(excinfo.value)


def test_office_export_uses_absolute_output_path(fake_office, tmp_path, monkeypatch):
    log, _apps, _state = fake_office
    saved = []
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()
    app = _FakeOfficeApp(log)
    app.Documents = types.SimpleNamespace(
        Open=lambda *a, **k: types.SimpleNamespace(SaveAs2=lambda path, fmt: saved.append(path), Close=lambda **kw: None)
    )
    [(pdf_path, _locate)] = render._export_with(app, Path("a.docx"), Path("rel"))
    # 相対パスは Office の作業フォルダからの位置と解釈されて書き出せないため、絶対パスで渡す
    assert Path(saved[0]).is_absolute() and pdf_path.is_absolute()


def test_xlsx_stop_does_not_reread_with_openpyxl(tmp_path, monkeypatch):
    """calamine で読んでいる途中に停止を求められたら、「calamine で読めなかった」として openpyxl で読み直さずに止める。"""
    import openpyxl

    from src.vector_db import extract
    from src.vector_db.extract import ExtractStopped, watch_stop_file

    workbook = openpyxl.Workbook()
    for i in range(1, 30):
        workbook.active.append([f"品目{i}", f"説明{i}"])
    workbook.save(tmp_path / "book.xlsx")
    stop_file = tmp_path / "STOP"
    stop_file.write_text("", encoding="utf-8")
    reread = []
    monkeypatch.setattr(extract, "_openpyxl_sheets", lambda source: reread.append(source) or iter(()))
    with watch_stop_file(str(stop_file)), pytest.raises(ExtractStopped):
        extract.extract_segments(tmp_path / "book.xlsx")
    assert reread == []


def test_concurrent_open_of_new_sqlite_files_does_not_fail(tmp_path):
    """作ったばかりの SQLite（ingest_journal.sqlite 等）を複数の取り込みが同時に開いても、WAL への切り替えで
    「database is locked」にならない（同じインスタンスの複数のコレクションの取り込みを同時に始めたとき）。"""
    from src.vector_db.journal import open_journal

    for attempt in range(5):
        directory = tmp_path / f"db{attempt}"
        barrier = threading.Barrier(8)
        errors = []

        def open_one():
            barrier.wait()
            try:
                open_journal(directory).close()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=open_one) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == []
