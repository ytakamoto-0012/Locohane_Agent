"""ファイルパスのベクター（<名前>.path）と search_paths のテスト。"""

from __future__ import annotations

import json
import sqlite3

import pytest

from _vector_db_fakes import FakeEmbeddingServer, FakeQdrant, fake_vector, make_config
from src.vector_db import catalog as vcat
from src.vector_db import ingest, search
from src.vector_db.keyword_index import open_keyword_index
from src.vector_db.store import open_store, path_collection_name


@pytest.fixture(params=["sqlite_vec", "qdrant"])
def env(request, tmp_path):
    docs = tmp_path / "docs"
    (docs / "配管図面").mkdir(parents=True)
    (docs / "会議資料").mkdir()
    (docs / "配管図面" / "系統A.md").write_text("xyz", encoding="utf-8")
    (docs / "会議資料" / "議事録.md").write_text("xyz", encoding="utf-8")
    cfg = make_config(tmp_path, vector_db_backend=request.param)
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    kwargs = {"embedding_transport": embed.transport, "store_transport": qdrant.transport}
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    return {"cfg": cfg, "docs": docs, "kwargs": kwargs, "embed": embed, "qdrant": qdrant}


def _path_points(env) -> list[str]:
    store = open_store(env["cfg"], transport=env["kwargs"]["store_transport"])
    try:
        return sorted(h.text for h in store.search(path_collection_name("docs"), fake_vector("x"), 100))
    finally:
        store.close()


def test_ingest_embeds_relative_path_with_chunks_and_search_paths_finds_it(env):
    cfg, docs, kwargs = env["cfg"], env["docs"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    # パスは本文のチャンクと同じリクエストの末尾で埋め込む（ファイルごとに1リクエスト）
    inputs = [r["input"] for r in env["embed"].requests]
    assert ["xyz", "配管図面/系統A.md"] in inputs and ["xyz", "会議資料/議事録.md"] in inputs
    assert _path_points(env) == ["会議資料/議事録.md", "配管図面/系統A.md"]

    result = search.search_paths(cfg, "配管図面", top_k=1, **kwargs)
    [hit] = result.hits
    assert (hit.collection, hit.source_path, hit.text, hit.locator, hit.matched) == (
        "docs", str(docs / "配管図面" / "系統A.md"), "配管図面/系統A.md", "", ("path", "path_keyword")
    )
    # 本文のチャンクの数にはパスを数えない
    [collection] = search.list_collections(cfg)
    assert collection.chunk_count == 2

    # フォルダから消えたファイルはパスのベクターも消す
    (docs / "配管図面" / "系統A.md").unlink()
    assert ingest.ingest_collection(cfg, "docs", **kwargs).removed == 1
    assert _path_points(env) == ["会議資料/議事録.md"]


def test_ingest_backfills_paths_for_files_ingested_before_path_vectors(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    # パスのベクターの導入前に取り込んだ状態を再現する
    store = open_store(cfg, transport=kwargs["store_transport"])
    store.drop_collection(path_collection_name("docs"))
    store.close()
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog._conn.execute("UPDATE files SET path_indexed = 0")
        catalog._conn.commit()
    before = len(env["embed"].requests)

    result = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert result.unchanged == 2
    # 本文は埋め込み直さず、パスだけを埋め込む（複数のファイルのパスを1リクエストにまとめる）
    [request] = env["embed"].requests[before:]
    assert sorted(request["input"]) == ["会議資料/議事録.md", "配管図面/系統A.md"]
    assert _path_points(env) == ["会議資料/議事録.md", "配管図面/系統A.md"]
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        assert all(r.path_indexed for r in catalog.files("docs").values())

    after = len(env["embed"].requests)
    ingest.ingest_collection(cfg, "docs", **kwargs)
    assert len(env["embed"].requests) == after  # 埋め込み済みなら2回目以降は何もしない


def test_rebuild_recreates_path_vectors(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    ingest.ingest_collection(cfg, "docs", rebuild=True, **kwargs)
    assert _path_points(env) == ["会議資料/議事録.md", "配管図面/系統A.md"]


def test_search_paths_filters_by_path_min_score_and_collection(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    strict = make_config(cfg.vector_db_dir.parent, vector_db_backend=cfg.vector_db_backend, vector_db_path_min_score=0.99)
    # 埋め込みで残るのは完全に一致したパスだけ。拡張子は索引に入れないため、"md" で他のファイルは一致しない
    hits = search.search_paths(strict, "会議資料/議事録.md", **kwargs).hits
    assert [(h.text, h.matched) for h in hits] == [("会議資料/議事録.md", ("path", "path_keyword"))]
    assert search.search_paths(strict, "md", **kwargs).hits == []
    with pytest.raises(search.SearchError):
        search.search_paths(cfg, "x", collection="missing", **kwargs)
    with pytest.raises(search.SearchError):
        search.search_paths(cfg, "  ", **kwargs)


def _path_keyword_sources(cfg) -> list[str]:
    with open_keyword_index(cfg.vector_db_dir) as keywords:
        texts = keywords.search(path_collection_name("docs"), "配管図面 会議資料", 100)
    return sorted(h.text for h in texts)


def test_search_paths_finds_exact_words_by_keyword(env):
    cfg, docs, kwargs = env["cfg"], env["docs"], env["kwargs"]
    (docs / "配管図面" / "XR-7741_外観.md").write_text("xyz", encoding="utf-8")
    ingest.ingest_collection(cfg, "docs", **kwargs)
    # 埋め込みでは何も残らないほど下限を上げても、キーワードの完全一致で見つかる
    strict = make_config(cfg.vector_db_dir.parent, vector_db_backend=cfg.vector_db_backend, vector_db_path_min_score=0.99)
    [hit] = search.search_paths(strict, "XR-7741", **kwargs).hits
    assert (hit.collection, hit.text, hit.matched) == ("docs", "配管図面/XR-7741_外観.md", ("path_keyword",))
    # 埋め込みとキーワードの両方で一致したファイルは1件にまとめる
    [top, *_] = search.search_paths(cfg, "XR-7741_外観", **kwargs).hits
    assert (top.text, top.matched) == ("配管図面/XR-7741_外観.md", ("path", "path_keyword"))
    # 重み 0 ならキーワードを使わない
    no_keyword = make_config(
        cfg.vector_db_dir.parent, vector_db_backend=cfg.vector_db_backend, vector_db_path_min_score=0.99,
        vector_db_rrf_weight_path_keyword=0.0,
    )
    assert search.search_paths(no_keyword, "XR-7741", **kwargs).hits == []
    # 本文のキーワード検索にはパスが混ざらない
    assert all(h.matched == ("text",) for h in search.search(cfg, "配管図面", **kwargs).hits)


def test_path_keywords_follow_removal_and_backfill(env):
    cfg, docs, kwargs = env["cfg"], env["docs"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    assert _path_keyword_sources(cfg) == ["会議資料/議事録.md", "配管図面/系統A.md"]
    # パスのキーワード検索の導入前に取り込んだ状態を再現すると、埋め込み直さずに索引だけ補う
    with open_keyword_index(cfg.vector_db_dir) as keywords:
        keywords.drop_collection(path_collection_name("docs"))
    before = len(env["embed"].requests)
    ingest.ingest_collection(cfg, "docs", **kwargs)
    assert len(env["embed"].requests) == before
    assert _path_keyword_sources(cfg) == ["会議資料/議事録.md", "配管図面/系統A.md"]
    (docs / "配管図面" / "系統A.md").unlink()
    ingest.ingest_collection(cfg, "docs", **kwargs)
    assert _path_keyword_sources(cfg) == ["会議資料/議事録.md"]
    ingest.ingest_collection(cfg, "docs", rebuild=True, **kwargs)
    assert _path_keyword_sources(cfg) == ["会議資料/議事録.md"]


def test_relative_path_text(tmp_path):
    assert ingest.relative_path_text(tmp_path, tmp_path / "a" / "b.png") == "a/b.png"
    # 取り込み元フォルダの外（通常は起きない）はパス全体
    assert ingest.relative_path_text(tmp_path / "x", tmp_path / "b.png") == (tmp_path / "b.png").as_posix()


def test_catalog_adds_path_indexed_column_to_old_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / vcat.CATALOG_FILENAME))
    conn.executescript(
        """
        CREATE TABLE files (collection TEXT NOT NULL, source_path TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL,
            sha256 TEXT NOT NULL, chunk_count INTEGER NOT NULL, mode TEXT NOT NULL DEFAULT 'text',
            PRIMARY KEY (collection, source_path));
        INSERT INTO files VALUES ('old', 'C:/docs/a.md', 1, 1.0, 'h', 1, 'text');
        """
    )
    conn.close()
    with vcat.open_catalog(tmp_path) as catalog:
        assert catalog.files("old")["C:/docs/a.md"].path_indexed is False


def test_config_reads_path_min_score(tmp_path, monkeypatch):
    from src.config import load_config

    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))
    monkeypatch.delenv("VECTOR_DB_PATH_MIN_SCORE", raising=False)
    assert load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1").vector_db_path_min_score == 0.3
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"vector_db": {"path_min_score": "0.5"}}), encoding="utf-8")
    assert load_config(overrides_path=path, instance_name="inst1").vector_db_path_min_score == 0.5
    path.write_text(json.dumps({"vector_db": {"path_min_score": "1.5"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="path_min_score"):
        load_config(overrides_path=path, instance_name="inst1")
    monkeypatch.delenv("VECTOR_DB_RRF_WEIGHT_PATH_KEYWORD", raising=False)
    assert load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1").vector_db_rrf_weight_path_keyword == 1.0
    path.write_text(json.dumps({"vector_db": {"rrf_weight_path_keyword": "-1"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="rrf_weight_path_keyword"):
        load_config(overrides_path=path, instance_name="inst1")
