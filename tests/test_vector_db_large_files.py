"""チャンクの多いファイル（業務データの数十万チャンクの1ファイル等）の取り込み・コピーと、強制終了後の整合性、
Qdrant への送り直しのテスト。

- 分割書き込み（ingest._Job.stream）: ベクターをためずに番号順にそろった分から保存先へ足す。
- 取り込みジャーナル（journal.py）: 書きかけのまま止まったファイルを、次の取り込みが片付ける。
- Qdrant の送り直し（[vector_db] qdrant_max_retries。-1 は無制限）。
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from _vector_db_fakes import FakeEmbeddingServer, FakeQdrant, make_config
from src.vector_db import catalog as vcat
from src.vector_db import collection_copy, embeddings, ingest, qdrant_store, search
from src.vector_db.journal import open_journal
from src.vector_db.keyword_index import KeywordIndex, open_keyword_index
from src.vector_db.store import StoreError, StoreStopped, open_store, path_collection_name


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(embeddings, "_sleep", lambda seconds: None)
    monkeypatch.setattr(embeddings, "_failed_until", {})
    monkeypatch.setattr(embeddings, "_float_only", set())
    monkeypatch.setattr(qdrant_store, "_sleep", lambda seconds: None)


@pytest.fixture(params=["sqlite_vec", "qdrant"])
def env(request, tmp_path, monkeypatch):
    # 小さいファイルでも分割書き込みになるようにする（1段落 = 1チャンク）。
    monkeypatch.setattr(ingest, "_STREAM_MIN_CHUNKS", 4)
    docs = tmp_path / "docs"
    docs.mkdir()
    cfg = make_config(
        tmp_path, vector_db_backend=request.param, vector_db_chunk_size=200, vector_db_chunk_overlap=0, vector_db_embedding_batch_size=4
    )
    embed, qdrant = FakeEmbeddingServer(), FakeQdrant()
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    return {
        "cfg": cfg,
        "docs": docs,
        "embed": embed,
        "qdrant": qdrant,
        "kwargs": {"embedding_transport": embed.transport, "store_transport": qdrant.transport},
    }


def _text(n: int, tag: str = "") -> str:
    return "\n\n".join(f"第{i}条{tag} 項目{i}の基準は{i}mmとする。" + "あ" * 150 for i in range(n))


def _config(env, **changes):
    cfg = env["cfg"]
    base = {
        "vector_db_backend": cfg.vector_db_backend,
        "vector_db_chunk_size": cfg.vector_db_chunk_size,
        "vector_db_chunk_overlap": cfg.vector_db_chunk_overlap,
        "vector_db_embedding_batch_size": cfg.vector_db_embedding_batch_size,
    }
    return make_config(cfg.vector_db_dir.parent, **{**base, **changes})


def _ingest(env, cfg=None, **kwargs):
    return ingest.ingest_collection(cfg or env["cfg"], "docs", **{**env["kwargs"], **kwargs})


def _stored(env, source, cfg=None, name="docs"):
    store = open_store(cfg or env["cfg"], transport=env["qdrant"].transport)
    try:
        return store.get_source(name, str(source))
    finally:
        store.close()


def _record(env, source):
    with vcat.open_catalog(env["cfg"].vector_db_dir) as catalog:
        return catalog.files("docs").get(str(source))


def _journal(env):
    with open_journal(env["cfg"].vector_db_dir) as journal:
        return journal.sources("docs")


def _stop_after(env, requests: int):
    """埋め込みのリクエストを requests 回受けたら停止を求めるトランスポートと should_stop。"""
    count = {"n": 0}

    def handler(request):
        count["n"] += 1
        return env["embed"].handler(request)

    return httpx.MockTransport(handler), (lambda: count["n"] >= requests)


# --- 分割書き込み ----------------------------------------------------------------------


def test_file_with_many_chunks_is_written_in_parts(env, monkeypatch):
    path = env["docs"] / "big.md"
    path.write_text(_text(30), encoding="utf-8")
    appended: list[tuple[int, int]] = []
    real_append = ingest._Writer.append_chunks

    def spy(self, prepared, start, chunks, vectors):
        real_append(self, prepared, start, chunks, vectors)
        if chunks:
            appended.append((start, start + len(chunks)))

    monkeypatch.setattr(ingest._Writer, "append_chunks", spy)
    result = _ingest(env, _config(env, vector_db_ingest_workers=3))
    assert result.added == 1 and result.chunks == 30
    # 何回にも分けて、番号の続きから書き込む
    assert len(appended) > 1 and appended[0][0] == 0 and appended[-1][1] == 30
    assert all(prev[1] == cur[0] for prev, cur in zip(appended, appended[1:]))
    chunks, vectors = _stored(env, path)
    assert len(vectors) == 30
    # 入れた順（チャンクの番号順）のまま
    assert chunks == ingest._chunk_segments(ingest._LoadOptions.from_config(env["cfg"]), path)
    record = _record(env, path)
    assert record.size == path.stat().st_size and record.chunk_count == 30
    assert _journal(env) == []
    with open_keyword_index(env["cfg"].vector_db_dir) as keywords:
        assert len(keywords.source_rows("docs", str(path))) == 30
    hits = search.search(env["cfg"], "第7条 項目7の基準は7mmとする。" + "あ" * 150, top_k=1, **env["kwargs"]).hits
    assert hits[0].text.startswith("第7条")

    # 内容を変えて取り込み直すと、古いチャンクは残らない
    path.write_text(_text(10, "改"), encoding="utf-8")
    keyword_commits = {"n": 0}
    real_apply = KeywordIndex.apply_deferred

    def count_commits(self, ops, **kwargs):
        if ops:
            keyword_commits["n"] += 1
        return real_apply(self, ops, **kwargs)

    # キーワード索引は、チャンクの多いファイルなら数行ずつ足し、ためた行が多ければファイルの途中でもコミットする。
    monkeypatch.setattr(ingest, "_KEYWORD_PAGE_ROWS", 3)
    monkeypatch.setattr(ingest, "_KEYWORD_FLUSH_ROWS", 3)
    monkeypatch.setattr(KeywordIndex, "apply_deferred", count_commits)
    again = _ingest(env)
    assert again.updated == 1
    chunks, _ = _stored(env, path)
    assert len(chunks) == 10 and all("改" in c.text for c in chunks)
    # 10 行を 3 行ずつコミットしたうえで、最後のコミットで残りとカタログを書く。
    assert keyword_commits["n"] >= 4
    with open_keyword_index(env["cfg"].vector_db_dir) as keywords:
        rows = keywords.source_rows("docs", str(path))
    assert len(rows) == 10 and all("改" in text for _, _, text in rows)
    assert _journal(env) == []


def test_vectors_waiting_for_earlier_numbers_are_bounded(env, monkeypatch):
    """前の番号のリクエストが返らない間は、そのファイルの先の本文を _STREAM_WINDOW 件より先まで送らない。"""
    monkeypatch.setattr(ingest, "_STREAM_WINDOW", 8)
    (env["docs"] / "big.md").write_text(_text(40), encoding="utf-8")
    first = threading.Event()
    lock = threading.Lock()
    state = {"first": True, "peak": 0}

    def handler(request):
        with lock:
            is_first, state["first"] = state["first"], False
        if is_first:
            first.wait(1.0)  # 最初のバッチ（チャンク 0〜3）だけ遅らせる
        return env["embed"].handler(request)

    real_receive = ingest._Job.receive

    def receive(self, kind, index, vector):
        real_receive(self, kind, index, vector)
        state["peak"] = max(state["peak"], len(self._stream_buffer))
        if len(self._stream_buffer) >= 8:
            first.set()

    monkeypatch.setattr(ingest._Job, "receive", receive)
    cfg = _config(env, vector_db_ingest_workers=4)
    result = _ingest(env, cfg, embedding_transport=httpx.MockTransport(handler))
    assert result.chunks == 40 and len(_stored(env, env["docs"] / "big.md")[0]) == 40
    # 窓（8件）と、窓の端をまたいで送ったバッチ（4件）の分まで
    assert state["peak"] <= 8 + 4


# --- 強制終了・停止の後の整合性（取り込みジャーナル） ------------------------------------------


def test_stop_while_writing_in_parts_reingests_next_time(env):
    path = env["docs"] / "big.md"
    path.write_text(_text(30), encoding="utf-8")
    assert _ingest(env).chunks == 30
    before = _record(env, path)
    path.write_text(_text(30, "改"), encoding="utf-8")
    transport, should_stop = _stop_after(env, 2)
    _ingest(env, embedding_transport=transport, should_stop=should_stop)
    # 古いチャンクは消え、書きかけのチャンクが残る。カタログは前の記録のまま、ジャーナルに書きかけとして残る
    chunks, _ = _stored(env, path)
    assert 0 < len(chunks) < 30 and all("改" in c.text for c in chunks)
    assert _record(env, path) == before
    assert _journal(env) == [str(path)]

    again = _ingest(env)
    assert again.updated == 1 and again.chunks == 30
    chunks, _ = _stored(env, path)
    assert len(chunks) == 30 and all("改" in c.text for c in chunks)
    assert _journal(env) == []


def test_reverted_file_is_reingested_after_stop(env):
    """書き換えの途中で止まった後に内容を元に戻しても、「内容が同じ」として書きかけのチャンクを残さない。"""
    path = env["docs"] / "big.md"
    original = _text(30)
    path.write_text(original, encoding="utf-8")
    _ingest(env)
    path.write_text(_text(30, "改"), encoding="utf-8")
    transport, should_stop = _stop_after(env, 2)
    _ingest(env, embedding_transport=transport, should_stop=should_stop)
    path.write_text(original, encoding="utf-8")
    again = _ingest(env)
    assert again.updated == 1
    chunks, _ = _stored(env, path)
    assert len(chunks) == 30 and not any("改" in c.text for c in chunks)


def test_half_written_new_file_is_removed_when_the_file_is_gone(env):
    path = env["docs"] / "big.md"
    path.write_text(_text(30), encoding="utf-8")
    transport, should_stop = _stop_after(env, 2)
    _ingest(env, embedding_transport=transport, should_stop=should_stop)
    assert _record(env, path) is None and len(_stored(env, path)[0]) > 0
    path.unlink()
    _ingest(env)
    assert _stored(env, path)[0] == [] and _journal(env) == []
    with open_keyword_index(env["cfg"].vector_db_dir) as keywords:
        assert keywords.source_rows("docs", str(path)) is None


class _Killed(BaseException):
    """プロセスの強制終了の代わり（except Exception で捕まらない）。"""


def test_kill_before_the_catalog_commit_is_repaired(env, monkeypatch):
    """保存先へ書いた後・カタログのコミット前に落ちた小さいファイルも、次の取り込みで片付ける。"""
    for i in range(3):
        (env["docs"] / f"note{i}.md").write_text(f"手順{i}を守る。", encoding="utf-8")

    def killed(self, ops, **kwargs):
        raise _Killed()

    with monkeypatch.context() as m:
        # 取り込みはカタログへの書き込みをためておき、コミットのときに apply_deferred でまとめて書く
        m.setattr(vcat.Catalog, "apply_deferred", killed)
        with pytest.raises(_Killed):
            _ingest(env)
    with vcat.open_catalog(env["cfg"].vector_db_dir) as catalog:
        assert catalog.files("docs") == {}
    # 保存先には書けている（Qdrant は送信済み・sqlite-vec はコミット済み）が、ジャーナルに書きかけとして残っている
    assert all(_stored(env, env["docs"] / f"note{i}.md")[0] for i in range(3))
    assert len(_journal(env)) == 3

    (env["docs"] / "note0.md").unlink()
    again = _ingest(env)
    assert (again.added, again.removed) == (2, 0)
    assert _stored(env, env["docs"] / "note0.md")[0] == []
    assert _journal(env) == []
    assert search.list_collections(env["cfg"])[0].chunk_count == 2


def test_interrupted_rebuild_is_redone_by_the_next_ingest(env, monkeypatch):
    for i in range(3):
        (env["docs"] / f"note{i}.md").write_text(f"手順{i}を守る。", encoding="utf-8")
    _ingest(env)

    def killed(self, name):
        raise _Killed()

    real_clear = vcat.Catalog.clear_files
    monkeypatch.setattr(vcat.Catalog, "clear_files", killed)
    with pytest.raises(_Killed):
        _ingest(env, rebuild=True)
    monkeypatch.setattr(vcat.Catalog, "clear_files", real_clear)
    # 保存先は消えたのにカタログの記録は残っている。次の差分取り込みは「変わっていない」とせず作り直す
    assert len(_record(env, env["docs"] / "note0.md").sha256) == 64
    again = _ingest(env)
    assert again.added == 3 and again.unchanged == 0
    assert all(_stored(env, env["docs"] / f"note{i}.md")[0] for i in range(3))
    with open_journal(env["cfg"].vector_db_dir) as journal:
        assert not journal.drop_pending("docs")


# --- コピー --------------------------------------------------------------------------


def test_copy_of_a_file_with_many_chunks_is_read_in_pages(env, tmp_path, monkeypatch):
    path = env["docs"] / "big.md"
    path.write_text(_text(600), encoding="utf-8")
    assert _ingest(env).chunks == 600
    dst = make_config(tmp_path / "dst", vector_db_backend=env["cfg"].vector_db_backend, vector_db_qdrant_collection_prefix="locohane_dst_")

    def no_get_source(self, name, source_path):
        raise AssertionError("copy must not read a whole file at once")

    store_cls = type(open_store(env["cfg"], transport=env["qdrant"].transport))
    monkeypatch.setattr(store_cls, "get_source", no_get_source)
    t = env["qdrant"].transport
    result = collection_copy.copy_collection(env["cfg"], "docs", dst, "copied", src_store_transport=t, dst_store_transport=t)
    monkeypatch.undo()
    assert (result["files"], result["chunks"]) == (1, 600)
    assert _stored(env, path, dst, "copied") == _stored(env, path)
    with open_keyword_index(env["cfg"].vector_db_dir) as a, open_keyword_index(dst.vector_db_dir) as b:
        assert b.source_rows("copied", str(path)) == a.source_rows("docs", str(path))
    with open_journal(dst.vector_db_dir) as journal:
        assert journal.sources("copied") == []


def test_copy_commits_in_the_middle_of_a_large_file(env, tmp_path, monkeypatch):
    """コピーは SQLite へ書く分をためるが、多くなればファイルの途中でもコミットし、1ファイル分をため込まない。"""
    monkeypatch.setattr(collection_copy, "_FLUSH_ROWS", 100)
    path = env["docs"] / "big.md"
    path.write_text(_text(300), encoding="utf-8")
    (env["docs"] / "small.md").write_text("手順を守る。", encoding="utf-8")
    assert _ingest(env).chunks == 301
    dst = make_config(tmp_path / "dst", vector_db_backend=env["cfg"].vector_db_backend, vector_db_qdrant_collection_prefix="locohane_dst_")
    # コミットごとにキーワード索引へ書いた行の数
    applied: list[int] = []
    real_apply = KeywordIndex.apply_deferred

    def spy(self, ops, **kwargs):
        applied.append(sum(len(op.args[2]) for op in ops if op.func.__name__ == "append_source_rows"))
        real_apply(self, ops, **kwargs)

    monkeypatch.setattr(KeywordIndex, "apply_deferred", spy)
    t = env["qdrant"].transport
    result = collection_copy.copy_collection(env["cfg"], "docs", dst, "copied", src_store_transport=t, dst_store_transport=t)
    monkeypatch.undo()
    assert (result["files"], result["chunks"]) == (2, 301)
    # 1ファイルの300行の索引をまとめて書かず、数百件ずつに分けてコミットした
    assert max(applied) <= collection_copy._KEYWORD_PAGE_ROWS and len([n for n in applied if n]) >= 2
    for source in (path, env["docs"] / "small.md"):
        assert _stored(env, source, dst, "copied") == _stored(env, source)
    with open_keyword_index(env["cfg"].vector_db_dir) as a, open_keyword_index(dst.vector_db_dir) as b:
        assert b.source_rows("copied", str(path)) == a.source_rows("docs", str(path))
        assert b.source_rows(path_collection_name("copied"), str(path)) == a.source_rows(path_collection_name("docs"), str(path))
    with vcat.open_catalog(dst.vector_db_dir) as catalog:
        assert set(catalog.files("copied")) == {str(path), str(env["docs"] / "small.md")}
    with open_journal(dst.vector_db_dir) as journal:
        assert journal.sources("copied") == []


# --- Qdrant への送り直し ------------------------------------------------------------------


def _flaky(qdrant: FakeQdrant, fail: int, error=None):
    """最初の fail 回のリクエストを失敗させる（error が None なら接続を切る、数値ならその HTTP ステータス）。"""
    state = {"left": fail, "calls": 0}

    def handler(request):
        state["calls"] += 1
        if state["left"] > 0:
            state["left"] -= 1
            if error is None:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
            return httpx.Response(error, json={"status": {"error": "temporary"}})
        return qdrant.handler(request)

    return httpx.MockTransport(handler), state


def _qdrant_env(tmp_path, **changes):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "weld.md").write_text("溶接ビードの幅は5mm以上とする。", encoding="utf-8")
    cfg = make_config(tmp_path, vector_db_backend="qdrant", **changes)
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        catalog.create("docs", str(docs))
    return cfg, FakeEmbeddingServer(), FakeQdrant()


@pytest.mark.parametrize("error", [None, 503, 429])
def test_qdrant_requests_are_retried(tmp_path, error):
    cfg, embed, qdrant = _qdrant_env(tmp_path, vector_db_qdrant_max_retries=3)
    transport, state = _flaky(qdrant, 3, error)
    result = ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport, store_transport=transport)
    assert result.added == 1 and state["left"] == 0
    assert len(qdrant.collections["locohane_test_docs"]["points"]) == 1


def test_qdrant_gives_up_after_max_retries(tmp_path):
    cfg, embed, qdrant = _qdrant_env(tmp_path, vector_db_qdrant_max_retries=2)
    transport, state = _flaky(qdrant, 100)
    with pytest.raises(StoreError, match="retried 2 times"):
        ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport, store_transport=transport)
    assert state["calls"] == 3


def test_qdrant_zero_retries_and_client_errors_are_not_retried(tmp_path):
    cfg, _embed, qdrant = _qdrant_env(tmp_path, vector_db_qdrant_max_retries=0)
    transport, state = _flaky(qdrant, 1)
    with pytest.raises(StoreError):
        open_store(cfg, transport=transport).dim("docs")
    assert state["calls"] == 1
    cfg = make_config(tmp_path / "b", vector_db_backend="qdrant", vector_db_qdrant_max_retries=-1)
    transport, state = _flaky(qdrant, 1, 400)
    with pytest.raises(StoreError, match="HTTP 400"):
        open_store(cfg, transport=transport).dim("docs")
    assert state["calls"] == 1


def test_unlimited_qdrant_retries_stop_when_requested(tmp_path):
    cfg, embed, qdrant = _qdrant_env(tmp_path, vector_db_qdrant_max_retries=-1)
    transport, state = _flaky(qdrant, 10**9)
    stop = {"at": 50}
    result = ingest.ingest_collection(
        cfg, "docs", embedding_transport=embed.transport, store_transport=transport, should_stop=lambda: state["calls"] >= stop["at"]
    )
    # エラーにはせず停止として終える（何も記録しない）
    assert result.added == 0 and state["calls"] >= 50
    with vcat.open_catalog(cfg.vector_db_dir) as catalog:
        assert catalog.files("docs") == {}
    again = ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport, store_transport=qdrant.transport)
    assert again.added == 1


def test_store_stopped_is_raised_while_waiting(tmp_path):
    cfg, _embed, qdrant = _qdrant_env(tmp_path, vector_db_qdrant_max_retries=-1)
    transport, _state = _flaky(qdrant, 10**9)
    store = open_store(cfg, transport=transport, should_stop=lambda: True)
    with pytest.raises(StoreStopped):
        store.dim("docs")


def test_search_retries_at_most_once(tmp_path, monkeypatch):
    cfg, embed, qdrant = _qdrant_env(tmp_path, vector_db_qdrant_max_retries=-1, vector_db_qdrant_retry_interval_seconds=30)
    ingest.ingest_collection(cfg, "docs", embedding_transport=embed.transport, store_transport=qdrant.transport)
    waits: list[float] = []
    monkeypatch.setattr(qdrant_store, "_sleep", lambda seconds: waits.append(seconds))
    transport, state = _flaky(qdrant, 1)
    store = open_store(cfg, transport=transport, shared=True)
    assert store.dim("docs") is not None and state["calls"] == 2
    assert sum(waits) <= qdrant_store.SEARCH_RETRY_INTERVAL_SECONDS
    transport, state = _flaky(qdrant, 100)
    with pytest.raises(StoreError):
        open_store(cfg, transport=transport, shared=True).dim("docs")
    assert state["calls"] == 1 + qdrant_store.SEARCH_MAX_RETRIES


def test_unstoppable_store_retries_at_most_once(tmp_path, monkeypatch):
    """止める手段の無い処理（コレクションの削除）は、qdrant_max_retries = -1 でも送り直しを1回までにする。"""
    cfg, _embed, qdrant = _qdrant_env(tmp_path, vector_db_qdrant_max_retries=-1, vector_db_qdrant_retry_interval_seconds=30)
    waits: list[float] = []
    monkeypatch.setattr(qdrant_store, "_sleep", lambda seconds: waits.append(seconds))
    transport, state = _flaky(qdrant, 100)
    with pytest.raises(StoreError):
        open_store(cfg, transport=transport, unstoppable=True).drop_collection("docs")
    assert state["calls"] == 1 + qdrant_store.SEARCH_MAX_RETRIES
    assert sum(waits) <= qdrant_store.SEARCH_RETRY_INTERVAL_SECONDS


def test_search_timeout_is_capped(tmp_path):
    """検索は qdrant_timeout_seconds を長くしても SEARCH_TIMEOUT_SECONDS 秒までしか待たず、接続は CONNECT_TIMEOUT_SECONDS 秒まで。"""
    cfg = make_config(tmp_path, vector_db_backend="qdrant", vector_db_qdrant_url="http://qdrant.invalid:6333", vector_db_qdrant_timeout_seconds=120)
    search = open_store(cfg, shared=True)
    ingest_store = open_store(cfg)
    try:
        assert search._client.timeout.read == qdrant_store.SEARCH_TIMEOUT_SECONDS
        assert ingest_store._client.timeout.read == 120
        assert ingest_store._client.timeout.connect == qdrant_store.CONNECT_TIMEOUT_SECONDS
    finally:
        ingest_store.close()


def test_collection_created_by_a_timed_out_request_is_accepted(tmp_path):
    """タイムアウトしたが Qdrant 側では作れていたコレクションの作成を送り直しても、エラーにしない。"""
    cfg, _embed, qdrant = _qdrant_env(tmp_path, vector_db_qdrant_max_retries=3)
    state = {"timed_out": False}

    def handler(request):
        if request.method == "PUT" and request.url.path.endswith("/collections/locohane_test_docs"):
            if not state["timed_out"]:
                state["timed_out"] = True
                qdrant.handler(request)  # 作れたが、応答が返る前にタイムアウトした
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(409, json={"status": {"error": "Collection already exists"}})
        return qdrant.handler(request)

    store = open_store(cfg, transport=httpx.MockTransport(handler))
    store.ensure_collection("docs", 8)
    assert qdrant.collections["locohane_test_docs"]["size"] == 8
    with pytest.raises(StoreError, match="dimension"):
        store.ensure_collection("docs", 4)


def test_qdrant_timeout_default_is_120(tmp_path):
    from src.config import load_config

    cfg = load_config()
    assert cfg.vector_db_qdrant_timeout_seconds == 120
    assert cfg.vector_db_qdrant_max_retries == 5


def test_wait_retry_polls_should_stop(tmp_path, monkeypatch):
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 3

    started = time.monotonic()
    store = qdrant_store.QdrantStore("http://q", prefix="p_", max_retries=-1, retry_interval=60, should_stop=should_stop)
    with pytest.raises(StoreStopped):
        store._wait_retry()
    assert time.monotonic() - started < 5
