"""コレクションを別のインスタンス（または同じインスタンスの別の名前）へコピーする（設定ダッシュボードの「ナレッジ」）。

ベクター・キーワード索引・取り込み済みファイルの記録まで複製するため、コピー先で埋め込み直さない。
保存先（sqlite_vec / qdrant）はコピー元とコピー先で違ってもよいが、ベクターを比べられるよう
埋め込みモデル・document_prefix・距離関数はコピー先の設定と同じである必要がある（check_compatible）。
合わないときは with_vectors=False で定義（説明・取り込み元フォルダ等）だけをコピーし、コピー先で取り込む。
"""

from __future__ import annotations

from collections.abc import Callable
import itertools
import logging
import time

import httpx

from ..config import Config
from .catalog import CatalogError, open_catalog
from .embeddings import embedding_model
from .gc_pacer import GcPacer
from .journal import open_journal
from .keyword_index import open_keyword_index
from .sqlite_batch import DeferredWrites, SqliteBatchMixin
from .store import StoreStopped, open_store, path_collection_name, storage_collection_names

logger = logging.getLogger(__name__)

# 書き込みをまとめてコミットする間隔（ingest._Writer と同じ考え方。保存先 → キーワード索引 → カタログの順にコミットする）。
_COMMIT_INTERVAL_SECONDS = 2.0
# コミットの前にためるチャンク・キーワード索引の行の数の上限（超えたらファイルの途中でもコミットする）。
_FLUSH_ROWS = 2048
# キーワード索引の行をコピー元から読んで足す1回の行の数。
_KEYWORD_PAGE_ROWS = 256


class CopyError(Exception):
    """コピーできない（コピー元が無い・コピー先の名前が重複・埋め込みモデル等が合わない等）。"""


def check_compatible(src_cfg: Config, src_name: str, dst_cfg: Config) -> list[str]:
    """コピー元のコレクションのベクターをコピー先で使えない理由の一覧（英語。空なら使える）。

    Raises:
        CopyError: コピー元のコレクションが無い場合。
    """
    with open_catalog(src_cfg.vector_db_dir) as catalog:
        try:
            c = catalog.get(src_name)
        except CatalogError as e:
            raise CopyError(str(e)) from e
    reasons = []
    if not c.model or not c.dim:
        reasons.append(f"collection {src_name} has not been ingested yet")
        return reasons
    try:
        model = embedding_model(dst_cfg)
    except Exception as e:  # noqa: BLE001 - embedding_url 未設定等
        reasons.append(f"the destination embedding model is not set ({e})")
        return reasons
    if c.model != model:
        reasons.append(f"embedding model differs (source {c.model}, destination {model})")
    if c.document_prefix is not None and c.document_prefix != dst_cfg.vector_db_document_prefix:
        reasons.append(f"document_prefix differs (source {c.document_prefix!r}, destination {dst_cfg.vector_db_document_prefix!r})")
    if src_cfg.vector_db_distance != dst_cfg.vector_db_distance:
        reasons.append(f"distance differs (source {src_cfg.vector_db_distance}, destination {dst_cfg.vector_db_distance})")
    return reasons


def copy_collection(
    src_cfg: Config,
    src_name: str,
    dst_cfg: Config,
    dst_name: str,
    *,
    with_vectors: bool = True,
    include_schedule: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    src_store_transport: httpx.BaseTransport | None = None,
    dst_store_transport: httpx.BaseTransport | None = None,
) -> dict:
    """コレクション src_name をコピー先の dst_name として作る。

    with_vectors=True ならファイルごとに、保存先の3つのコレクション（本文・ページ画像・ファイルパス）のベクター、
    キーワード索引（本文・ファイルパス）、取り込み済みファイルの記録の順に複製する。should_stop() が True を返したら
    そこまでのファイルを残して終える（残りはコピー先での「取り込み（差分）」で補われる）。

    Returns:
        {"kind": "copy", "with_vectors", "files": 複製したファイル数, "chunks": 複製したチャンク数, "total": コピー元のファイル数,
         "stopped": 途中で止めたか}

    Raises:
        CopyError: コピー元が無い、コピー先の名前が既にある、またはベクターを使えない（check_compatible）場合。
        StoreError: 保存先を使えない場合。
    """
    if with_vectors:
        reasons = check_compatible(src_cfg, src_name, dst_cfg)
        if reasons:
            raise CopyError("; ".join(reasons))
    with open_catalog(src_cfg.vector_db_dir) as src_catalog:
        try:
            src = src_catalog.get(src_name)
        except CatalogError as e:
            raise CopyError(str(e)) from e
        records = src_catalog.files(src_name) if with_vectors else {}

    with open_catalog(dst_cfg.vector_db_dir) as dst_catalog:
        try:
            dst_catalog.create(dst_name, src.source_folders, src.description, src.recursive, src.exclude_keywords, src.include_keywords)
            if include_schedule:
                dst_catalog.update(
                    dst_name,
                    schedule_enabled=src.schedule_enabled,
                    schedule_times=src.schedule_times,
                    schedule_weekdays=src.schedule_weekdays,
                )
        except CatalogError as e:
            raise CopyError(str(e)) from e
        result = {"kind": "copy", "with_vectors": with_vectors, "files": 0, "chunks": 0, "total": len(records), "stopped": False}
        if not with_vectors:
            return result
        dst_catalog.set_model(dst_name, src.model, src.dim, src.document_prefix)

        # Qdrant へ送り直しを待つ間も「停止」に応じる（[vector_db].qdrant_max_retries = -1 なら無制限に待つため）。
        src_store = open_store(src_cfg, transport=src_store_transport, should_stop=should_stop)
        dst_store = open_store(dst_cfg, transport=dst_store_transport, should_stop=should_stop)
        src_keywords = open_keyword_index(src_cfg.vector_db_dir)
        dst_keywords = open_keyword_index(dst_cfg.vector_db_dir)
        # 強制終了等で書きかけになったファイルは、コピー先での次の取り込みが片付ける（journal 参照）。
        journal = open_journal(dst_cfg.vector_db_dir)
        # 同じ名前の前のコレクションの削除が途中で止まった印が残っていても、今作ったコレクションには関係しない。
        journal.clear(dst_name)
        # SQLite の DB（キーワード索引・カタログ・sqlite-vec）へはその場では書かず、コミットのときに短いトランザクションで
        # まとめて書く（ingest._Writer と同じ。コピー元の読み出しや Qdrant への送信を待つ間に書き込みのロックを持たない）。
        # Qdrant は自分で送る更新をためる（begin_batch）ため、そのまま使う。DeferredWrites も同じ形
        # （begin_batch・commit・rollback・end_batch）で使える。別のプロセスのロックを待つ間も「停止」に応じる。
        store_part = DeferredWrites(dst_store, "store", should_stop=should_stop) if isinstance(dst_store, SqliteBatchMixin) else dst_store
        keywords_part = DeferredWrites(dst_keywords, "keyword index", should_stop=should_stop)
        catalog_part = DeferredWrites(dst_catalog, "catalog", should_stop=should_stop)
        # コミットの順（保存先 → キーワード索引 → カタログ）。
        parts = (store_part, keywords_part, catalog_part)
        ready: set[str] = set()
        # カタログへ記録したが、まだコミットしていないファイルと、そのチャンク数。
        recorded: list[str] = []
        recorded_chunks = 0
        # 前回のコミットからためたチャンク・索引の行の数（_FLUSH_ROWS を超えたら、ファイルの途中でもコミットする）。
        pending_rows = 0
        # Qdrant の応答（httpx）の循環参照のゴミがたまり続けないよう、間隔を空けて回収する（gc_pacer 参照）。
        pacer = GcPacer()

        def commit() -> None:
            nonlocal recorded, recorded_chunks, pending_rows, last_commit
            for part in parts:
                part.commit()
            # カタログまでコミットした。journal から消す前に止まっても（StoreStopped）、コピーした件数からは引かない
            # （journal に残ったファイルは、コピー先の次の取り込みが取り込み直す）。
            committed, recorded, recorded_chunks, pending_rows = recorded, [], 0, 0
            last_commit = time.monotonic()
            journal.discard(dst_name, committed, should_stop=should_stop)

        def added(rows: int) -> None:
            """rows 件をためた。数十万チャンクの1ファイルでもメモリーにため込まないよう、多ければその場でコミットする
            （書きかけのファイルは journal に記録してあるため、途中で止まってもコピー先の取り込みが片付ける）。"""
            nonlocal pending_rows
            pending_rows += rows
            if pending_rows >= _FLUSH_ROWS:
                commit()

        last_commit = time.monotonic()
        try:
            store_part.begin_batch()
            for i, (source, record) in enumerate(sorted(records.items()), start=1):
                if should_stop and should_stop():
                    result["stopped"] = True
                    break
                if progress:
                    progress(i - 1, len(records), source)
                journal.add(dst_name, source, should_stop=should_stop)
                chunk_count = 0
                for src_storage, dst_storage in zip(storage_collection_names(src_name), storage_collection_names(dst_name), strict=True):
                    # 数十万チャンクの1ファイルでもメモリーを使い切らないよう、数百件ずつ読んで足す。
                    start = 0
                    for chunks, vectors in src_store.iter_source(src_storage, source):
                        pacer.tick()
                        if dst_storage not in ready:
                            store_part.ensure_collection(dst_storage, len(vectors[0]))
                            ready.add(dst_storage)
                        if start == 0:
                            store_part.delete_source(dst_storage, source)
                        store_part.append_source(dst_storage, source, start, chunks, vectors)
                        start += len(chunks)
                        added(len(chunks))
                    if src_storage == src_name:
                        chunk_count = start
                for src_kw, dst_kw in ((src_name, dst_name), (path_collection_name(src_name), path_collection_name(dst_name))):
                    rows = src_keywords.iter_source_rows(src_kw, source)
                    if rows is None:
                        continue
                    # 丸ごと入れ替える代わりに、消してから数百件ずつ足す（ためる行を抑えるため）。
                    keywords_part.delete_source(dst_kw, source)
                    first = True
                    while True:
                        batch = list(itertools.islice(rows, _KEYWORD_PAGE_ROWS))
                        if batch or first:
                            keywords_part.append_source_rows(dst_kw, source, batch)
                            added(len(batch))
                        first = False
                        if len(batch) < _KEYWORD_PAGE_ROWS:
                            break
                catalog_part.upsert_file(dst_name, record)
                recorded.append(source)
                recorded_chunks += chunk_count
                result["files"] += 1
                result["chunks"] += chunk_count
                if time.monotonic() - last_commit >= _COMMIT_INTERVAL_SECONDS:
                    commit()
            commit()
            store_part.end_batch()
        except BaseException as exc:
            for part in parts:
                try:
                    part.rollback()
                except Exception:  # noqa: BLE001 - 元のエラーを優先する
                    logger.exception("vector_db copy: rollback failed")
                part._batching = False
            if not isinstance(exc, StoreStopped):
                raise
            # Qdrant への送り直しを待つ間に停止を求められた。コミットしていない分は記録しない（コピー先の取り込みで補われる）。
            result["stopped"] = True
            result["files"] -= len(recorded)
            result["chunks"] -= recorded_chunks
        finally:
            src_store.close()
            dst_store.close()
            src_keywords.close()
            dst_keywords.close()
            journal.close()
        if progress:
            progress(result["files"], len(records), "")
        if not result["stopped"]:
            dst_catalog.set_last_ingested(dst_name, src.last_ingested_at)
    logger.info("vector_db copy: %s -> %s (%d files, %d chunks)", src_name, dst_name, result["files"], result["chunks"])
    return result


__all__ = ["CopyError", "check_compatible", "copy_collection"]
