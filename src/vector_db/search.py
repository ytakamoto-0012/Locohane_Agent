"""検索語に近いチャンクを取り込み済みのコレクションから探す。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import httpx

from ..config import Config
from . import references
from .catalog import Collection, catalog_exists, open_catalog
from .embeddings import embed_texts, embedding_model, shared_client
from .keyword_index import KEYWORDS_FILENAME, open_keyword_index
from .store import Hit, image_collection_name, open_store, path_collection_name

# Reciprocal Rank Fusion の定数（順位 r の項目に 重み / (_RRF_K + r) を足す。一般的な既定値の 60）。
_RRF_K = 60
# 各一覧から取る候補の数（top_k の何倍か）。同じページのチャンクをまとめた後も top_k 件残るよう多めに取る。
_CANDIDATE_FACTOR = 4


def keyword_index_exists(directory) -> bool:
    return (directory / KEYWORDS_FILENAME).is_file()


class SearchError(Exception):
    """検索できない（指定したコレクションが無い・取り込み前等）。"""


@dataclass
class SearchResult:
    hits: list[Hit] = field(default_factory=list)
    # 検索対象から外したコレクションとその理由（埋め込みモデル違い等）。
    skipped: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _Target:
    """検索するコレクション。name は結果に出す名前、storage は保存先のコレクション名、cfg は読み先の設定
    （別のインスタンスの参照なら参照元のインスタンスの設定。references.py）。"""

    name: str
    storage: str
    cfg: Config


def list_collections(cfg: Config) -> list[Collection]:
    """コレクション一覧（catalog.sqlite がまだ無ければ空。別のインスタンスの参照は含まない）。"""
    if not catalog_exists(cfg.vector_db_dir):
        return []
    with open_catalog(cfg.vector_db_dir) as catalog:
        return catalog.list_collections()


def list_references(cfg: Config) -> list[references.ResolvedReference]:
    """別のインスタンスのコレクションの参照の一覧（参照元を解決したもの）。"""
    return [references.resolve(cfg, r) for r in references.list_references(cfg)]


def _by_location(targets: list[_Target]) -> list[tuple[Config, list[_Target]]]:
    """読み先の設定（自分のインスタンス・参照元のインスタンス）ごとにまとめる。"""
    groups: dict[int, tuple[Config, list[_Target]]] = {}
    for t in targets:
        groups.setdefault(id(t.cfg), (t.cfg, []))[1].append(t)
    return list(groups.values())


def search(
    cfg: Config,
    query: str,
    *,
    collection: str | None = None,
    top_k: int | None = None,
    embedding_transport: httpx.BaseTransport | None = None,
    store_transport: httpx.BaseTransport | None = None,
) -> SearchResult:
    """query に意味の近いチャンクを、関連の高い順に最大 top_k 件返す。

    collection を省略すると取り込み済みの全コレクションを横断して探す。
    今の埋め込みモデルと違うモデルで作られたコレクションは比べられないため外し、
    skipped に理由を載せる。

    本文の埋め込み・本文のキーワード（BM25）・ページ画像（[vector_db].image_input）は点数の
    尺度が違うため、コレクション・種類ごとに別々に上位を取り（本文は min_score、画像は
    image_min_score 未満を除く）、点数ではなく順位で統合する（_fuse。重みは rrf_weight_*）。
    同じファイル・同じ位置（ページ等）のチャンクは1件にまとめる。

    Raises:
        SearchError: query が空、指定したコレクションが無い・まだ取り込まれていない、
            または検索できるコレクションが1つも無い場合。
        EmbeddingError / StoreError: 埋め込みサーバー・保存先を使えない場合。
    """
    if not query.strip():
        raise SearchError("Specify a search query.")
    limit = top_k if top_k and top_k > 0 else cfg.vector_db_top_k
    result = SearchResult()
    targets = _targets(cfg, collection, result)

    # 検索のたびに接続し直さないよう、テスト用の transport が無ければプロセス内で共有するクライアントを使う。
    client = shared_client(cfg) if embedding_transport is None else None
    vector = embed_texts(cfg, [query], kind="query", transport=embedding_transport, client=client)[0]
    # 同じページのチャンクを1件にまとめると件数が減るため、各一覧は top_k より多めに取る。
    candidates = limit * _CANDIDATE_FACTOR
    lists: list[tuple[str, float, list[Hit]]] = []
    # 閾値・重みは自分の設定を使い、保存先・キーワード索引は読み先（参照元）の設定で開く。
    for location, group in _by_location(targets):
        store = open_store(location, transport=store_transport, shared=True)
        try:
            for t in group:
                texts = [replace(h, collection=t.name) for h in store.search(t.storage, vector, candidates)]
                lists.append(("text", cfg.vector_db_rrf_weight_text, [h for h in texts if h.score >= cfg.vector_db_min_score]))
                if cfg.vector_db_rrf_weight_image > 0:
                    images = store.search(image_collection_name(t.storage), vector, candidates)
                    # 保存先の名前（"<名前>.image"）ではなく利用者のコレクション名で返す。
                    images = [replace(h, collection=t.name) for h in images if h.score >= cfg.vector_db_image_min_score]
                    lists.append(("image", cfg.vector_db_rrf_weight_image, images))
        finally:
            store.close()
        if cfg.vector_db_rrf_weight_keyword > 0 and keyword_index_exists(location.vector_db_dir):
            with open_keyword_index(location.vector_db_dir) as keywords:
                for t in group:
                    hits = [replace(h, collection=t.name) for h in keywords.search(t.storage, query, candidates)]
                    lists.append(("keyword", cfg.vector_db_rrf_weight_keyword, hits))
    result.hits = _fuse(lists)[:limit]
    return result


def search_paths(
    cfg: Config,
    query: str,
    *,
    collection: str | None = None,
    top_k: int | None = None,
    embedding_transport: httpx.BaseTransport | None = None,
    store_transport: httpx.BaseTransport | None = None,
) -> SearchResult:
    """query に近いファイルパス（取り込み元フォルダからの相対パス）のファイルを、関連の高い順に最大 top_k 件返す。

    写真・図面のように中身から内容を読み取れないファイルを、ファイル名・フォルダ名で探すために使う。
    パスの埋め込み（path_min_score 未満を除く）とパスのキーワード（BM25。型番・日付等の完全一致を拾う）を
    コレクションごとに別々に検索し、search() と同じく順位で統合する（重みは埋め込みが 1.0、キーワードが
    rrf_weight_path_keyword）。1ファイル1件で、Hit.text は相対パス、Hit.locator は空、Hit.matched は
    "path"（埋め込み）・"path_keyword"（キーワード）。対象のコレクションの選び方・例外は search() と同じ。
    パスの導入前に取り込んだコレクションは、次の取り込み（差分）まで結果が出ない。
    """
    if not query.strip():
        raise SearchError("Specify a search query.")
    limit = top_k if top_k and top_k > 0 else cfg.vector_db_top_k
    result = SearchResult()
    targets = _targets(cfg, collection, result, paths=True)
    client = shared_client(cfg) if embedding_transport is None else None
    vector = embed_texts(cfg, [query], kind="query", transport=embedding_transport, client=client)[0]
    candidates = limit * _CANDIDATE_FACTOR
    lists: list[tuple[str, float, list[Hit]]] = []
    for location, group in _by_location(targets):
        store = open_store(location, transport=store_transport, shared=True)
        try:
            for t in group:
                hits = store.search(path_collection_name(t.storage), vector, candidates)
                # 保存先の名前（"<名前>.path"）ではなく利用者のコレクション名で返す。
                hits = [replace(h, collection=t.name) for h in hits if h.score >= cfg.vector_db_path_min_score]
                lists.append(("path", 1.0, hits))
        finally:
            store.close()
        if cfg.vector_db_rrf_weight_path_keyword > 0 and keyword_index_exists(location.vector_db_dir):
            with open_keyword_index(location.vector_db_dir) as keywords:
                for t in group:
                    hits = [replace(h, collection=t.name) for h in keywords.search(path_collection_name(t.storage), query, candidates)]
                    lists.append(("path_keyword", cfg.vector_db_rrf_weight_path_keyword, hits))
    result.hits = _fuse(lists)[:limit]
    return result


def _targets(cfg: Config, collection: str | None, result: SearchResult, *, paths: bool = False) -> list[_Target]:
    """検索するコレクション（collection が空なら全部。別のインスタンスの参照も含む）。比べられないものは外して
    result.skipped に理由を載せる。

    paths=True（search_paths）なら、本文のチャンクが無くてもファイルがあれば対象にする
    （image_input = false の画像ファイル等は、ファイルパスだけを登録しているため）。
    参照は検索語を自分の埋め込みモデルで埋め込むため、参照元のコレクションも自分の設定と同じモデル・document_prefix で
    取り込まれている必要がある。
    """
    model = embedding_model(cfg)
    # (結果に出す名前, コレクション, 読み先の設定, 使えない理由)
    entries: list[tuple[str, Collection | None, Config | None, str | None]] = [(c.name, c, cfg, None) for c in list_collections(cfg)]
    for r in references.list_references(cfg):
        if collection and r.name != collection:
            continue
        resolved = references.resolve(cfg, r)
        entries.append((r.name, resolved.collection, resolved.config, resolved.reason))
    if collection:
        entries = [e for e in entries if e[0] == collection]
        if not entries:
            raise SearchError(f"Collection {collection} does not exist. Check the list with list_document_collections.")
    targets = []
    for name, c, location, reason in entries:
        fix = "an administrator needs to rebuild it" if location is cfg else "an administrator needs to rebuild it in the source instance"
        if reason or c is None or location is None:
            result.skipped.append((name, f"the referenced collection of another instance cannot be used: {reason}"))
        elif not c.model or not c.file_count:
            result.skipped.append((name, "no documents ingested yet"))
        elif not paths and not c.chunk_count:
            result.skipped.append((name, "no documents with text (file and folder names can be searched with search_document_paths)"))
        elif c.model != model:
            result.skipped.append((name, f"built with embedding model {c.model} (current setting: {model}; {fix})"))
        elif c.document_prefix is not None and c.document_prefix != cfg.vector_db_document_prefix:
            result.skipped.append(
                (
                    name,
                    f"ingested with document_prefix {c.document_prefix!r} "
                    f"(current setting: {cfg.vector_db_document_prefix!r}; {fix})",
                )
            )
        else:
            targets.append(_Target(name=name, storage=c.name, cfg=location))
    if not targets:
        reasons = ", ".join(f"{n}: {r}" for n, r in result.skipped) or "no collections"
        raise SearchError(f"No searchable collections ({reasons}).")
    return targets


def _fuse(lists: list[tuple[str, float, list[Hit]]]) -> list[Hit]:
    """本文・キーワード・ページ画像の検索結果を、重み付きの Reciprocal Rank Fusion で1つの順位にまとめる。

    lists は (種類 "text"/"keyword"/"image", 重み, 関連の高い順の結果) の並び。結果は同じファイル・
    同じ位置（locator）ごとに1件にまとめ、各一覧での順位 r に 重み / (_RRF_K + r) を足した合計の高い順に並べる。

    1つの一覧の中では、同じ位置のチャンクは最上位の1つだけを数える（2位以下は Hit.other_chunks に
    数だけ残す）。1ページに大量のチャンクがあっても、点数はチャンク数ではなく一覧ごとの最上位の順位で決まる。
    抜粋は最初に出会ったチャンク（本文 → キーワード → 画像の順に並べた lists の中で最上位のもの）を使う。
    """
    # search_paths() のファイルパスの埋め込み（"path"）・キーワード（"path_keyword"）も同じ式で統合する。
    order = {"text": 0, "keyword": 1, "image": 2, "path": 3, "path_keyword": 4}
    entries: dict[tuple[str, str, str], Hit] = {}
    scores: dict[tuple[str, str, str], float] = {}
    others: dict[tuple[str, str, str], set[str]] = {}
    for kind, weight, hits in sorted(lists, key=lambda item: order[item[0]]):
        rank = 0
        seen: set[tuple[str, str, str]] = set()
        for hit in hits:
            key = (hit.collection, hit.source_path, hit.locator)
            if key in seen:
                if kind != "image":
                    others.setdefault(key, set()).add(hit.text)
                continue
            seen.add(key)
            rank += 1
            entry = entries.get(key)
            if entry is None:
                entries[key] = replace(hit, matched=(kind,))
                scores[key] = 0.0
            else:
                if kind != "image" and hit.text != entry.text:
                    others.setdefault(key, set()).add(hit.text)
                entries[key] = replace(entry, matched=entry.matched + (kind,), score=max(entry.score, hit.score))
            scores[key] += weight / (_RRF_K + rank)
    fused = [replace(entry, other_chunks=len(others.get(key, set()) - {entry.text})) for key, entry in entries.items()]
    fused.sort(key=lambda h: (scores[(h.collection, h.source_path, h.locator)], h.score), reverse=True)
    return fused
