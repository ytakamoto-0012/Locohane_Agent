"""ベクターの保存先（バックエンド）の共通インターフェースと生成。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Protocol

import httpx

from ..config import Config


class StoreError(Exception):
    """ベクターの保存先を操作できなかった（接続できない・次元・距離関数が合わない等）。"""


class StoreStopped(StoreError):
    """保存先への送り直しを待っている間に should_stop() が True を返した（取り込み・コピーの「停止」）。"""


# 値が小さいほど近い距離関数（[vector_db].distance）。Hit.score を「大きいほど近い」に
# そろえるため、検索結果の距離を distance_to_score() で換算する。
DISTANCE_METRICS = frozenset({"Euclid", "Manhattan"})


def distance_to_score(distance: float) -> float:
    """Euclid・Manhattan の距離（0 以上、小さいほど近い）を (0, 1] の類似度にする（1 / (1 + 距離)）。"""
    return 1.0 / (1.0 + max(float(distance), 0.0))


# [vector_db].image_input = true でページ画像を埋め込んだベクターは、本文のベクターと
# 類似度の出方が違う（検索語はテキストのため、画像との類似度は低めに出る）ので、
# 同じ順位付けに混ぜないよう別のコレクションに入れる。コレクション名には "." を
# 使えない（catalog.validate_name）ため、利用者のコレクション名とは衝突しない。
IMAGE_COLLECTION_SUFFIX = ".image"


def image_collection_name(name: str) -> str:
    """コレクション name のページ画像のベクターを入れる保存先のコレクション名。"""
    return name + IMAGE_COLLECTION_SUFFIX


# ファイルのパス（取り込み元フォルダからの相対パス）を1ファイル1件で埋め込んだベクターの保存先。
# 写真・図面のように中身から内容を読み取れないファイルでも、ファイル名・フォルダ名に内容が
# 書かれていることが多いため、パスの文字列でも意味検索できるようにする（search.search_paths）。
PATH_COLLECTION_SUFFIX = ".path"


def path_collection_name(name: str) -> str:
    """コレクション name のファイルパスのベクターを入れる保存先のコレクション名。"""
    return name + PATH_COLLECTION_SUFFIX


def storage_collection_names(name: str) -> tuple[str, str, str]:
    """コレクション name が使う保存先のコレクション（本文・ページ画像・ファイルパス）の名前。"""
    return name, image_collection_name(name), path_collection_name(name)


@dataclass(frozen=True)
class ChunkRecord:
    """保存する1チャンク。locator はファイル内の位置（extract.Segment.locator）。

    ページ画像のチャンクの text は、検索結果の抜粋に使うそのページの文字。
    ファイルパスのチャンクは locator が空で、text が取り込み元フォルダからの相対パス。
    """

    locator: str
    text: str


@dataclass(frozen=True)
class Hit:
    """検索で見つかった1チャンク。score は大きいほど近い類似度（1.0 が完全一致）。

    コレクションの距離関数が Cosine ならコサイン類似度、Dot（Qdrant のみ）なら内積、
    Euclid・Manhattan なら distance_to_score() で換算した値。キーワード検索では 0.0。

    matched は一致した検索（"text" = 本文の埋め込み、"keyword" = 本文のキーワード、
    "image" = ページ画像、"path" = ファイルパスの埋め込み、"path_keyword" = ファイルパスのキーワード
    （この2つは search.search_paths()））。other_chunks は同じ位置で
    他に一致した本文のチャンクの数。どちらも保存先の search() は既定値のまま返し、search.search() が
    結果を統合するときに入れる。
    """

    collection: str
    score: float
    source_path: str
    locator: str
    text: str
    matched: tuple[str, ...] = ()
    other_chunks: int = 0


class VectorStore(Protocol):
    def ensure_collection(self, name: str, dim: int) -> None:
        """コレクションが無ければ [vector_db].distance の距離関数で作る。あって次元・距離関数が違えば StoreError（再構築が必要）。"""

    def apply_preset(self, name: str) -> None:
        """既存のコレクションの設定を [vector_db].qdrant_preset に合わせる（Qdrant のみ。sqlite-vec では何もしない）。"""

    def drop_collection(self, name: str) -> None:
        """コレクションのベクターをすべて消す（無ければ何もしない）。"""

    def replace_source(self, name: str, source_path: str, chunks: list[ChunkRecord], vectors: list[list[float]]) -> None:
        """source_path のチャンクを丸ごと入れ替える。"""

    def append_source(self, name: str, source_path: str, start: int, chunks: list[ChunkRecord], vectors: list[list[float]]) -> None:
        """source_path の start 番目からのチャンクを足す（既存のチャンクは消さない）。

        チャンクの多いファイルを、全部のベクターをメモリーにためずに分けて書き込む用（ingest._Writer.append_chunks）。
        先に delete_source() で古いチャンクを消し、start は 0 から順に続けて渡すこと。
        """

    def delete_source(self, name: str, source_path: str) -> None:
        """source_path のチャンクを消す。"""

    def search(self, name: str, vector: list[float], top_k: int) -> list[Hit]:
        """vector に近い順に最大 top_k 件返す（コレクションが無ければ空リスト）。"""

    def dim(self, name: str) -> int | None:
        """コレクションのベクターの次元（無ければ None）。"""

    def get_source(self, name: str, source_path: str) -> tuple[list[ChunkRecord], list[list[float]]]:
        """source_path のチャンクとベクターを入れた順に返す（無ければ空）。チャンクの多いファイルでは iter_source を使うこと。"""

    def iter_source(self, name: str, source_path: str) -> Iterator[tuple[list[ChunkRecord], list[list[float]]]]:
        """source_path のチャンクとベクターを、入れた順に数百件ずつ返す（インスタンス間のコピー用。無ければ何も返さない）。

        数十万チャンクの1ファイルでも、全部のベクターを一度にメモリーに読まない。
        """

    def close(self) -> None: ...


def open_store(
    cfg: Config,
    *,
    transport: httpx.BaseTransport | None = None,
    shared: bool = False,
    should_stop: Callable[[], bool] | None = None,
    unstoppable: bool = False,
) -> VectorStore:
    """[vector_db].backend に応じた保存先を開く。使い終わったら close() すること。

    Args:
        cfg: 設定。
        transport: backend=qdrant のとき、テスト用に差し替える httpx のトランスポート。
        shared: backend=qdrant のとき、Qdrant への接続をプロセス内で使い回す（検索用。
            qdrant_store.shared_client 参照）。transport を渡したときは使わない。
            検索は止める手段が無いため、unstoppable と同じく送り直しを抑え、1リクエストのタイムアウトも
            最大 qdrant_store.SEARCH_TIMEOUT_SECONDS 秒にする。
        should_stop: backend=qdrant のとき、送り直しを待つ間に確かめ、True を返したら StoreStopped を投げてやめる。
        unstoppable: 止める手段の無い処理（コレクションの削除等）。送り直しは [vector_db].qdrant_max_retries によらず
            最大 qdrant_store.SEARCH_MAX_RETRIES 回、間隔は最大 SEARCH_RETRY_INTERVAL_SECONDS 秒にする
            （-1（無制限）のままだと Qdrant が止まっている間いつまでも返らないため）。
    """
    if cfg.vector_db_backend == "qdrant":
        from .qdrant_store import SEARCH_MAX_RETRIES, SEARCH_RETRY_INTERVAL_SECONDS, SEARCH_TIMEOUT_SECONDS, QdrantStore

        max_retries, interval = cfg.vector_db_qdrant_max_retries, cfg.vector_db_qdrant_retry_interval_seconds
        timeout = cfg.vector_db_qdrant_timeout_seconds
        if shared or unstoppable:
            max_retries = SEARCH_MAX_RETRIES if max_retries < 0 else min(max_retries, SEARCH_MAX_RETRIES)
            interval = min(interval, SEARCH_RETRY_INTERVAL_SECONDS)
        if shared:
            timeout = min(timeout, SEARCH_TIMEOUT_SECONDS)
        return QdrantStore(
            cfg.vector_db_qdrant_url,
            prefix=cfg.vector_db_qdrant_collection_prefix,
            api_key=cfg.vector_db_qdrant_api_key,
            timeout=timeout,
            distance=cfg.vector_db_distance,
            preset=cfg.vector_db_qdrant_preset,
            transport=transport,
            shared=shared and transport is None,
            max_retries=max_retries,
            retry_interval=interval,
            should_stop=should_stop,
        )
    from .sqlite_vec_store import SqliteVecStore

    return SqliteVecStore(cfg.vector_db_dir, distance=cfg.vector_db_distance)
