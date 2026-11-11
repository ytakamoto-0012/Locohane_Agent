"""search_documents / search_document_paths / list_document_collections ツールのテスト。"""

from __future__ import annotations

import dataclasses
import importlib

import pytest

from _vector_db_fakes import FakeEmbeddingServer, make_config
from src import tools
from src.vector_db import catalog as vcat
from src.vector_db import ingest

vector_db_tools = importlib.import_module("src.tools.vector_db_tools")


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {"thread_id": "thread-1"}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


@pytest.fixture
def env(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "weld.md").write_text("溶接ビードの幅は5mm以上とする。", encoding="utf-8")
    (docs / "room.md").write_text("会議室の予約はポータルから行う。" * 20, encoding="utf-8")
    cfg = make_config(tmp_path, vector_db_chunk_size=400, vector_db_chunk_overlap=0)
    embed = FakeEmbeddingServer()
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs), "品質文書")
    ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport)

    # ツールは埋め込みサーバーへ実際に接続するため、偽物のトランスポートを差し込む。
    real_search = vector_db_tools.vector_search.search
    monkeypatch.setattr(
        vector_db_tools.vector_search, "search", lambda *a, **k: real_search(*a, embedding_transport=embed.transport, **k)
    )
    real_search_paths = vector_db_tools.vector_search.search_paths
    monkeypatch.setattr(
        vector_db_tools.vector_search,
        "search_paths",
        lambda *a, **k: real_search_paths(*a, embedding_transport=embed.transport, **k),
    )
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", cfg)
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", tmp_path / "path_memory")
    (tmp_path / "path_memory").mkdir()
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    return {"cfg": cfg, "docs": docs}


def test_tools_report_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", make_config(tmp_path, vector_db_enabled=False))
    assert "disabled" in tools.search_documents.func(query="溶接")
    assert "disabled" in tools.list_document_collections.func()
    assert "disabled" in tools.search_document_paths.func(query="溶接")


def test_list_document_collections(env):
    text = tools.list_document_collections.func()
    assert "docs - 品質文書" in text and "2 documents" in text


def test_render_document_collections_block(env):
    assert vector_db_tools.render_document_collections_block(env["cfg"]) == "- docs - 品質文書 (2 documents)"


def test_render_document_collections_block_disabled_or_empty(tmp_path):
    disabled = vector_db_tools.render_document_collections_block(make_config(tmp_path, vector_db_enabled=False))
    assert disabled.startswith("(none: ") and "disabled" in disabled
    assert vector_db_tools.render_document_collections_block(make_config(tmp_path)).startswith("(none: ")


def test_render_document_collections_block_truncates(env, monkeypatch):
    monkeypatch.setattr(vector_db_tools, "_PROMPT_COLLECTIONS_MAX", 0)
    assert vector_db_tools.render_document_collections_block(env["cfg"]) == "- ... and 1 more (see list_document_collections)"


def test_search_documents_returns_sources_with_path_memory_refs(env):
    text = tools.search_documents.func(query="溶接ビードの幅", top_k=1)
    assert text.startswith("1 found.")
    assert "@1 " + str(env["docs"] / "weld.md") in text
    assert "溶接ビードの幅は5mm以上とする。" in text
    # 出典はファイル。コレクション名（docs）を載せると「出典: docs」と書き写される。
    assert "/ docs /" not in text
    assert "/ docs /" not in tools.search_document_paths.func(query="weld.md", top_k=1)


def test_search_tools_record_query_embedding_usage_per_user(env):
    """検索語の埋め込みは、会話のユーザー（セッション外は anonymous）の source="search" の行に記録する。"""
    import sqlite3

    from src import embedding_usage

    cfg = env["cfg"]
    vector_db_tools.search_documents.invoke({"query": "溶接"})
    vector_db_tools.search_document_paths.invoke({"query": "weld"})
    with sqlite3.connect(cfg.thread_store_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = {r["source"]: r for r in embedding_usage.read_rows(conn)}
    search_row = rows[embedding_usage.SOURCE_SEARCH]
    assert search_row["owner"] == "anonymous" and search_row["calls"] == 2 and search_row["tokens"] > 0
    assert rows[embedding_usage.SOURCE_INGEST]["owner"] == embedding_usage.SYSTEM_OWNER


def test_search_documents_truncates_to_max_result_chars(env, monkeypatch):
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", dataclasses.replace(env["cfg"], vector_db_max_result_chars=150))
    text = tools.search_documents.func(query="会議室の予約")
    assert "omitted" in text
    assert len(text) < 400


def test_search_documents_returns_error_text(env):
    assert tools.search_documents.func(query="x", collection="missing").startswith("Error: ")


def test_tools_are_registered_for_main_and_subagents():
    from src.agent_types import scan_agent_types
    from src.config import load_config

    names = {t.name for t in tools.get_all_tools()}
    assert {"search_documents", "search_document_paths", "list_document_collections"} <= names
    cfg = load_config()
    by_name = {a.name: a for a in scan_agent_types(cfg.agents_dir)}
    for agent in ("explore", "worker", "verifier"):
        resolved = {t.name for t in tools._resolve_agent_types([by_name[agent]])[agent].tools}
        assert {"search_documents", "search_document_paths", "list_document_collections"} <= resolved, agent
    # メインエージェントには一覧だけを許可する（検索結果で会話履歴が膨らむため、検索はサブエージェントに任せる）
    allowed = {entry for entry, _ in cfg.main_agent_tool_guard_allow_entries}
    assert "list_document_collections" in allowed
    assert not {"search_documents", "search_document_paths"} & allowed


def test_search_documents_shows_image_matches(env, monkeypatch, tmp_path):
    from reportlab.pdfgen import canvas

    from src.vector_db.render import PageImage

    c = canvas.Canvas(str(env["docs"] / "drawing.pdf"))
    c.showPage()
    c.save()
    monkeypatch.setattr(ingest, "render_pages", lambda path, dpi, **_: [PageImage("p.1", "", "配管の図面".encode())])
    cfg = dataclasses.replace(env["cfg"], vector_db_image_input=True)
    embed = FakeEmbeddingServer()
    ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", cfg)
    text = tools.search_documents.func(query="配管の図面", top_k=1)
    assert "(matched: image)" in text and str(env["docs"] / "drawing.pdf") + " p.1" in text
    assert ".png" not in text  # ページ画像のファイルは作らず、元のファイルを出典にする


def test_search_documents_shows_keyword_matches_and_merged_chunks(env, monkeypatch):
    (env["docs"] / "parts.md").write_text("交換部品の型番は XR-7741 です。", encoding="utf-8")
    (env["docs"] / "room.md").write_text("会議室の予約はポータルから行う。" * 30, encoding="utf-8")  # 480文字 → 2チャンク
    ingest.ingest_collection(env["cfg"], "docs", embedding_transport=FakeEmbeddingServer().transport)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", dataclasses.replace(env["cfg"], vector_db_min_score=0.99))
    text = tools.search_documents.func(query="XR-7741", top_k=1)
    assert "similarity - (matched: keyword)" in text and "parts.md" in text
    # room.md の2チャンクは1件にまとめて、他のチャンク数を添える
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", env["cfg"])
    text = tools.search_documents.func(query="会議室の予約はポータルから行う。", top_k=1)
    assert "room.md" in text and "(1 more chunks matched at the same location)" in text


def test_search_document_paths_returns_files_with_path_memory_refs(env):
    text = tools.search_document_paths.func(query="weld.md", top_k=1)
    assert text.startswith("1 found.")
    assert "@1 " + str(env["docs"] / "weld.md") in text and "relative path: weld.md" in text
    assert "(matched: meaning/keyword)" in text


def test_search_document_paths_shows_keyword_only_matches(env, monkeypatch):
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", dataclasses.replace(env["cfg"], vector_db_path_min_score=1.0))
    text = tools.search_document_paths.func(query="weld", top_k=1)
    assert "similarity - (matched: keyword)" in text and "relative path: weld.md" in text


def test_search_document_paths_reports_no_match_and_errors(env, monkeypatch):
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", dataclasses.replace(env["cfg"], vector_db_path_min_score=1.0))
    assert "were found" in tools.search_document_paths.func(query="配管の写真")
    assert tools.search_document_paths.func(query="x", collection="missing").startswith("Error: ")
