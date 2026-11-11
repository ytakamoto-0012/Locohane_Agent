"""埋め込み（/v1/embeddings）のトークン使用量の台帳（設定ダッシュボードの「利用量・料金」用）。

生成（チャット）の台帳 token_usage_monthly（src/thread_store.py）とは単価が大きく違うため、別の表に記録する。

- 本体インスタンス: スレッドDB（[thread_store] db）の embedding_usage_monthly。
  source="search"（search_documents 等の検索語）・"ingest"（ナレッジの取り込み。管理ツールの取り込みワーカーが書く）。
- 管理ツール: <instances_dir>/admin_ai_usage.sqlite の同じ名前の表（source は管理アシスタントの索引等。admin/ai_usage.py）。

埋め込みの課金は入力トークンだけのため、input は持たず tokens（応答の usage.prompt_tokens、無ければ total_tokens）と
calls（リクエスト数）だけを加算する。特定のユーザーに結び付かない利用（取り込み・索引）は owner を SYSTEM_OWNER にする。

管理ツール（chainlit を読み込まない）からも使うため、標準ライブラリだけに依存する。
記録に失敗しても本来の処理は止めない（record_safely）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TABLE = "embedding_usage_monthly"
# 特定のユーザーに結び付かない埋め込み（ナレッジの取り込み・管理アシスタントの索引）の owner。
# ログインユーザー名は「_」始まりにならない（chat_log.resolve_log_username）ため衝突しない。
SYSTEM_OWNER = "_system"
SOURCE_SEARCH = "search"
SOURCE_INGEST = "ingest"

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  month TEXT NOT NULL,
  source TEXT NOT NULL,
  owner TEXT NOT NULL,
  tokens INTEGER NOT NULL DEFAULT 0,
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (month, source, owner)
);
"""


def _month(when: datetime | None) -> str:
    """src/thread_store.py の usage_month と同じ（ローカル時刻の "YYYY-MM"）。"""
    when = when or datetime.now()
    if when.tzinfo is not None:
        when = when.astimezone()
    return when.strftime("%Y-%m")


def tokens_of(usage: dict | None) -> int:
    """/v1/embeddings の応答の usage（{"prompt_tokens", "total_tokens"}）からトークン数を取り出す。"""
    if not isinstance(usage, dict):
        return 0
    for key in ("prompt_tokens", "total_tokens", "input_tokens"):
        try:
            value = int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


class EmbeddingMeter:
    """埋め込みのトークン数・リクエスト数を数える（スレッドから同時に add してよい）。

    embed_texts / embed_images の meter に渡すと、リクエストごとに応答の usage を加算する。
    取り込みのようにリクエストの多い処理は、終わったときに take() した分を1回だけ台帳へ書く。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tokens = 0
        self.calls = 0

    def add(self, tokens: int, calls: int = 1) -> None:
        with self._lock:
            self.tokens += max(int(tokens or 0), 0)
            self.calls += max(int(calls or 0), 0)

    def take(self) -> tuple[int, int]:
        """数えた (tokens, calls) を返して0に戻す。"""
        with self._lock:
            taken = (self.tokens, self.calls)
            self.tokens = self.calls = 0
            return taken


def record(db_path: Path, source: str, owner: str | None, tokens: int, calls: int = 1, *, when: datetime | None = None) -> None:
    """tokens・calls を当月・source・owner の行へ加算する（UPSERT 1文。別プロセスと同時に書いても取りこぼさない）。"""
    tokens, calls = max(int(tokens or 0), 0), max(int(calls or 0), 0)
    if not tokens and not calls:
        return
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path), timeout=30)) as conn:
        conn.executescript(_DDL)
        conn.execute(
            f"INSERT INTO {TABLE} (month, source, owner, tokens, calls) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(month, source, owner) DO UPDATE SET tokens = tokens + excluded.tokens, calls = calls + excluded.calls",
            (_month(when), source, owner or SYSTEM_OWNER, tokens, calls),
        )
        conn.commit()


def record_safely(db_path: Path, source: str, owner: str | None, tokens: int, calls: int = 1) -> None:
    """record() の失敗をログに残すだけにする（集計のために本来の処理を止めない）。"""
    try:
        record(db_path, source, owner, tokens, calls)
    except Exception:  # noqa: BLE001
        logger.warning("Could not record the embedding token usage (source=%s, db=%s)", source, db_path, exc_info=True)


def flush_meter(meter: EmbeddingMeter, db_path: Path, source: str, owner: str | None) -> None:
    """meter に数えた分を台帳へ書き、meter を0に戻す（失敗してもログだけ）。"""
    tokens, calls = meter.take()
    record_safely(db_path, source, owner, tokens, calls)


def read_rows(conn: sqlite3.Connection) -> list[dict]:
    """conn（row_factory = sqlite3.Row）の台帳の全行。表が無ければ空。"""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)).fetchone()
    if not exists:
        return []
    return [dict(r) for r in conn.execute(f"SELECT month, source, owner, tokens, calls FROM {TABLE}").fetchall()]
