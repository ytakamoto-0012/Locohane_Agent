"""ベクターDB のキーワード検索（BM25）と、検索結果の統合（重み・同じページのまとめ）のテスト。"""

from __future__ import annotations

import dataclasses

import pytest

from _vector_db_fakes import FakeEmbeddingServer, FakeQdrant, make_config
from src.vector_db import catalog as vcat
from src.vector_db import ingest, search
from src.vector_db.keyword_index import _tokens, open_keyword_index
from src.vector_db.store import ChunkRecord, Hit


def test_tokens_split_japanese_into_bigrams_and_ascii_into_words():
    assert _tokens("溶接ビードの幅は５ｍｍ以上とする。型番ABC-123") == [
        "5mm", "abc", "123", "溶接", "接ビ", "ビー", "ード", "ドの", "の幅", "幅は", "以上", "上と", "型番",
    ]  # ひらがなだけの2文字（「とす」「する」）は除く
    assert _tokens("ｶﾀｶﾅ") == ["カタ", "タカ", "カナ"]
    assert _tokens("の") == []


def test_keyword_index_ranks_exact_terms(tmp_path):
    with open_keyword_index(tmp_path) as index:
        index.replace_source("docs", "/a.md", [ChunkRecord("", "型番 XR-7741 の取付手順"), ChunkRecord("", "一般的な取付手順")])
        index.replace_source("docs", "/b.md", [])
        index.replace_source("other", "/c.md", [ChunkRecord("", "XR-7741")])
        assert index.has_source("docs", "/b.md")  # チャンクが無くても索引済みとして記録する
        hits = index.search("docs", "XR-7741 はどこ？", 5)
        assert [h.text for h in hits] == ["型番 XR-7741 の取付手順"]
        assert index.search("docs", "の", 5) == []
        index.delete_source("docs", "/a.md")
        assert index.search("docs", "XR-7741", 5) == [] and not index.has_source("docs", "/a.md")
        index.drop_collection("other")
        assert index.search("other", "XR-7741", 5) == []


def test_fuse_merges_chunks_of_the_same_page_and_counts_only_the_best_rank():
    page = ("docs", "/big.pdf", "p.1")
    many = [Hit(*page[:1], 0.9 - i * 0.01, page[1], page[2], f"chunk{i}") for i in range(30)]
    single = Hit("docs", 0.5, "/small.md", "", "small")
    fused = search._fuse([("text", 1.0, many + [single])])
    assert [h.source_path for h in fused] == ["/big.pdf", "/small.md"]
    assert (fused[0].text, fused[0].other_chunks) == ("chunk0", 29)
    # 1ページに大量のチャンクがあっても点数は増えない（2位以下を足さない）。小さい文書は2位として数える。
    keyword_first = search._fuse([("text", 1.0, many), ("keyword", 1.0, [single])])
    assert [h.source_path for h in keyword_first] == ["/big.pdf", "/small.md"]


def test_fuse_weights_change_the_order():
    a = [Hit("docs", 0.9, "/a.md", "", "a")]
    b = [Hit("docs", 0.0, "/b.md", "", "b")]
    assert [h.source_path for h in search._fuse([("text", 1.0, a), ("keyword", 2.0, b)])] == ["/b.md", "/a.md"]
    fused = search._fuse([("text", 1.0, a), ("keyword", 1.0, a)])
    assert len(fused) == 1 and fused[0].matched == ("text", "keyword")


@pytest.fixture(params=["sqlite_vec", "qdrant"])
def env(request, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "parts.md").write_text("交換部品の型番は XR-7741 です。", encoding="utf-8")
    (docs / "room.md").write_text("会議室の予約はポータルから行う。", encoding="utf-8")
    cfg = make_config(tmp_path, vector_db_backend=request.param, vector_db_min_score=0.99)
    kwargs = {"embedding_transport": FakeEmbeddingServer().transport, "store_transport": FakeQdrant().transport}
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    return {"cfg": cfg, "docs": docs, "kwargs": kwargs}


def test_search_finds_exact_terms_by_keyword(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    # 埋め込みの類似度は min_score に届かないが、キーワードで見つかる
    [hit] = search.search(cfg, "XR-7741", **kwargs).hits
    assert hit.source_path.endswith("parts.md") and hit.matched == ("keyword",)
    off = dataclasses.replace(cfg, vector_db_rrf_weight_keyword=0.0)
    assert search.search(off, "XR-7741", **kwargs).hits == []


def test_ingest_maintains_keyword_index(env):
    cfg, kwargs = env["cfg"], env["kwargs"]
    ingest.ingest_collection(cfg, "docs", **kwargs)
    # キーワード索引の導入前に取り込んだコレクションは、差分取り込みで埋め込み直さずに索引だけ作る
    with open_keyword_index(cfg.vector_db_dir) as index:
        index.drop_collection("docs")
    result = ingest.ingest_collection(cfg, "docs", **kwargs)
    assert result.unchanged == 2
    assert search.search(cfg, "XR-7741", **kwargs).hits
    # 消えたファイル・再構築は索引からも消す
    (env["docs"] / "parts.md").unlink()
    assert ingest.ingest_collection(cfg, "docs", **kwargs).removed == 1
    assert search.search(cfg, "XR-7741", **kwargs).hits == []
    ingest.ingest_collection(cfg, "docs", rebuild=True, **kwargs)
    with open_keyword_index(cfg.vector_db_dir) as index:
        assert [h.source_path for h in index.search("docs", "会議室", 5)] == [str(env["docs"] / "room.md")]


def test_config_reads_rrf_weights(tmp_path, monkeypatch):
    import json

    from src.config import load_config

    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))
    for key in ("TEXT", "KEYWORD", "IMAGE"):
        monkeypatch.delenv(f"VECTOR_DB_RRF_WEIGHT_{key}", raising=False)
    default = load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")
    assert (default.vector_db_rrf_weight_text, default.vector_db_rrf_weight_keyword, default.vector_db_rrf_weight_image) == (1.0, 1.0, 1.0)
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({"vector_db": {"rrf_weight_keyword": "-1"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="rrf_weight_keyword"):
        load_config(overrides_path=path, instance_name="inst1")
