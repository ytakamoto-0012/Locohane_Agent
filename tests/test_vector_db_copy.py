"""インスタンス間のコレクションのコピー（collection_copy.py）と、別のインスタンスの参照（references.py・search.py）のテスト。"""

from __future__ import annotations

import json

import pytest

from _vector_db_fakes import FakeEmbeddingServer, FakeQdrant, make_config
from src.config import LLMEndpoint
from src.tools import vector_db_tools
from src.tools import _state
from src.vector_db import catalog as vcat
from src.vector_db import collection_copy, ingest, references, search
from src.vector_db.keyword_index import open_keyword_index
from src.vector_db.store import open_store, path_collection_name


def _write_instance(root, name, kind="app"):
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "instance.json").write_text(json.dumps({"display_name": name, "kind": kind}), encoding="utf-8")


@pytest.fixture(params=[("sqlite_vec", "sqlite_vec"), ("sqlite_vec", "qdrant"), ("qdrant", "sqlite_vec")], ids=lambda p: f"{p[0]}-{p[1]}")
def env(request, tmp_path):
    docs = tmp_path / "docs"
    (docs / "写真").mkdir(parents=True)
    (docs / "weld.md").write_text("溶接ビードの幅は5mm以上とする。型番 ABC-123。", encoding="utf-8")
    (docs / "paint.md").write_text("塗装の膜厚は30μm以上とする。", encoding="utf-8")
    (docs / "写真" / "外観.png").write_bytes(b"not an image")
    root = tmp_path / "instances"
    for name in ("org_a", "org_b"):
        _write_instance(root, name)
    src_backend, dst_backend = request.param
    src, dst = (
        make_config(tmp_path / n, vector_db_backend=b, instance_dir=root / n, vector_db_qdrant_collection_prefix=f"locohane_{n}_")
        for n, b in (("org_a", src_backend), ("org_b", dst_backend))
    )
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    with vcat.open_catalog(src.vector_db_dir) as catalog:
        catalog.create("docs", str(docs), "品質規程")
    ingest.ingest_collection(src, "docs", embedding_transport=embed.transport, store_transport=qdrant.transport)
    return {"src": src, "dst": dst, "docs": docs, "embed": embed, "qdrant": qdrant, "root": root}


def _copy(env, dst_name="manuals", **kwargs):
    t = env["qdrant"].transport
    return collection_copy.copy_collection(env["src"], "docs", env["dst"], dst_name, src_store_transport=t, dst_store_transport=t, **kwargs)


def _sources(cfg, name, transport):
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        records = catalog.files(name)
    store = open_store(cfg, transport=transport)

    def _get(storage, p):
        # sqlite-vec は float32 で持つため、Qdrant（JSON の float）との比較は丸めてそろえる。
        chunks, vectors = store.get_source(storage, p)
        return chunks, [[round(x, 5) for x in v] for v in vectors]

    try:
        return records, {p: _get(name, p) for p in records}, {p: _get(path_collection_name(name), p) for p in records}
    finally:
        store.close()


def test_copy_duplicates_vectors_keywords_and_file_records(env):
    result = _copy(env)
    assert (result["files"], result["total"], result["stopped"]) == (3, 3, False)
    t = env["qdrant"].transport
    src_records, src_text, src_paths = _sources(env["src"], "docs", t)
    dst_records, dst_text, dst_paths = _sources(env["dst"], "manuals", t)
    assert dst_records == src_records
    assert dst_text == src_text and dst_paths == src_paths
    with open_keyword_index(env["src"].vector_db_dir) as src_kw, open_keyword_index(env["dst"].vector_db_dir) as dst_kw:
        for p in src_records:
            assert dst_kw.source_rows("manuals", p) == src_kw.source_rows("docs", p)
            assert dst_kw.source_rows(path_collection_name("manuals"), p) == src_kw.source_rows(path_collection_name("docs"), p)
    with vcat.open_catalog(env["src"].vector_db_dir) as a, vcat.open_catalog(env["dst"].vector_db_dir) as b:
        c_src, c_dst = a.get("docs"), b.get("manuals")
    assert (c_dst.model, c_dst.dim, c_dst.description, c_dst.last_ingested_at) == (c_src.model, c_src.dim, "品質規程", c_src.last_ingested_at)
    assert c_dst.chunk_count == c_src.chunk_count == result["chunks"]

    hits = search.search(env["dst"], "溶接ビードの幅", embedding_transport=env["embed"].transport, store_transport=t).hits
    assert hits and hits[0].collection == "manuals" and hits[0].source_path.endswith("weld.md")
    # 埋め込み直さずに済んでいる（コピー先での差分取り込みは「変更なし」で埋め込みを送らない）。
    sent = len(env["embed"].requests)
    again = ingest.ingest_collection(env["dst"], "manuals", embedding_transport=env["embed"].transport, store_transport=t)
    assert (again.added, again.updated, again.unchanged) == (0, 0, 3)
    assert len(env["embed"].requests) == sent


def test_copy_rejects_incompatible_destination_but_definition_only_works(env):
    other = make_config(
        env["dst"].vector_db_dir.parent,
        vector_db_backend=env["dst"].vector_db_backend,
        instance_dir=env["dst"].instance_dir,
        vector_db_embedding_endpoints=(LLMEndpoint(base_url="http://embed/v1", api_key="k", model="other-embed"),),
    )
    env["dst"] = other
    assert collection_copy.check_compatible(env["src"], "docs", other) == ["embedding model differs (source fake-embed, destination other-embed)"]
    with pytest.raises(collection_copy.CopyError, match="embedding model differs"):
        _copy(env)
    with vcat.open_catalog(other.vector_db_dir) as catalog:
        assert catalog.list_collections() == []

    with vcat.open_catalog(env["src"].vector_db_dir) as catalog:
        catalog.update("docs", schedule_enabled=True, schedule_times=["02:00"], schedule_weekdays=[0])
    result = _copy(env, with_vectors=False, include_schedule=True)
    assert (result["files"], result["with_vectors"]) == (0, False)
    with vcat.open_catalog(other.vector_db_dir) as catalog:
        c = catalog.get("manuals")
    assert (c.model, c.file_count, c.source_folder) == (None, 0, str(env["docs"]))
    assert (c.schedule_enabled, c.schedule_times, c.schedule_weekdays) == (True, ("02:00",), (0,))


def test_copy_rejects_existing_name_and_stops_midway(env):
    with vcat.open_catalog(env["dst"].vector_db_dir) as catalog:
        catalog.create("taken", "C:/x")
    with pytest.raises(collection_copy.CopyError, match="already exists"):
        _copy(env, "taken")
    calls = iter([False, True])
    result = _copy(env, should_stop=lambda: next(calls, True))
    assert (result["files"], result["stopped"]) == (1, True)
    with vcat.open_catalog(env["dst"].vector_db_dir) as catalog:
        c = catalog.get("manuals")
    # 途中で止めたコピーは最終取り込み日時を写さない（残りは差分取り込みで補う）。
    assert (c.file_count, c.schedule_enabled, c.last_ingested_at) == (1, False, None)


def test_reference_searches_source_instance_without_copying(env, monkeypatch):
    src, dst = env["src"], env["dst"]
    t = env["qdrant"].transport
    monkeypatch.setattr(references, "load_instance_config", lambda cfg, name: {"org_a": src}[name])
    with vcat.open_catalog(dst.vector_db_dir) as catalog:
        catalog.add_reference("a_docs", "org_a", "docs")
        catalog.add_reference("gone", "org_a", "deleted")
        catalog.add_reference("self", "org_b", "docs")
        with pytest.raises(vcat.CatalogError, match="already exists"):
            catalog.create("a_docs", "C:/x")
        with pytest.raises(vcat.CatalogError, match="already exists"):
            catalog.add_reference("a_docs", "org_a", "docs")

    result = search.search(dst, "溶接ビードの幅", embedding_transport=env["embed"].transport, store_transport=t)
    assert result.hits and {h.collection for h in result.hits} == {"a_docs"}
    assert "keyword" in next(h for h in result.hits if h.source_path.endswith("weld.md")).matched
    skipped = dict(result.skipped)
    assert "collection deleted does not exist in instance org_a" in skipped["gone"]
    assert "it is this instance itself" in skipped["self"]

    paths = search.search_paths(dst, "外観", collection="a_docs", embedding_transport=env["embed"].transport, store_transport=t)
    assert any(h.text == "写真/外観.png" and h.collection == "a_docs" for h in paths.hits)

    monkeypatch.setattr(_state, "_LLM_CONFIG", dst)
    listing = vector_db_tools.list_document_collections.invoke({})
    assert "- a_docs - 品質規程 (references collection docs of instance org_a / 3 documents" in listing
    assert "- gone (references collection deleted of instance org_a; cannot be used:" in listing


def test_reference_skips_incompatible_or_skill_lab_source(env, monkeypatch):
    src, dst = env["src"], env["dst"]
    _write_instance(env["root"], "lab1", kind="skill_lab")
    other_model = make_config(
        dst.vector_db_dir.parent,
        vector_db_backend=dst.vector_db_backend,
        instance_dir=dst.instance_dir,
        vector_db_embedding_endpoints=(LLMEndpoint(base_url="http://embed/v1", api_key="k", model="other-embed"),),
    )
    monkeypatch.setattr(references, "load_instance_config", lambda cfg, name: src)
    with vcat.open_catalog(dst.vector_db_dir) as catalog:
        catalog.add_reference("a_docs", "org_a", "docs")
        catalog.add_reference("lab", "lab1", "docs")
    with pytest.raises(search.SearchError) as excinfo:
        search.search(other_model, "溶接", embedding_transport=env["embed"].transport, store_transport=env["qdrant"].transport)
    message = str(excinfo.value)
    assert "a_docs: built with embedding model fake-embed (current setting: other-embed; " in message
    assert "an administrator needs to rebuild it in the source instance" in message
    assert "lab: the referenced collection of another instance cannot be used: instance lab1 is a skill lab instance" in message
