"""管理ツール側の AI 利用（スキル研究室・管理アシスタント）のトークン使用量の台帳。

本体の会話は各インスタンスのスレッドDB（token_usage_monthly）に記録されるが、管理ツールが
LLM を使う次の処理はチャットのスレッドを持たないため、そこには載らない。

- スキル研究室: スキル調整ワーカーが行う評価（evals/run_all.py の各ケース）・AI judge・AI 修正・下書き
- 管理アシスタント（AIに相談）

これらを全インスタンス共通の <instances_dir>/admin_ai_usage.sqlite に、月（ローカル時刻）・
使い道（source）・依頼したユーザー別に加算する。「利用量・料金」では source ごとに
1つのインスタンスのように並べる（admin/usage_cost.py の monthly_usage() と同じ形を返す）。

- source はスキル研究室ならスキル調整ワーカーのインスタンス名、管理アシスタントなら ASSISTANT_SOURCE。
- スキル調整ワーカー（別プロセス）と管理ツールの両方が書くため、SQLite の UPSERT 1文で加算する。
- 記録に失敗しても本来の処理は止めない（呼び出し側で握りつぶす）。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from src import embedding_usage

logger = logging.getLogger(__name__)

DB_FILENAME = "admin_ai_usage.sqlite"
# 管理アシスタント（AIに相談）の source 名。インスタンス名に使えない「_」始まりにして衝突させない。
ASSISTANT_SOURCE = "_admin_assistant"

_DDL = """
CREATE TABLE IF NOT EXISTS ai_usage_monthly (
  month TEXT NOT NULL,
  source TEXT NOT NULL,
  owner TEXT NOT NULL,
  input INTEGER NOT NULL DEFAULT 0,
  output INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (month, source, owner)
);
"""


def db_path(instances_root: Path) -> Path:
    return Path(instances_root) / DB_FILENAME


def _month(when: datetime | None) -> str:
    when = when or datetime.now()
    if when.tzinfo is not None:
        when = when.astimezone()
    return when.strftime("%Y-%m")


def normalize_usage(usage: dict | None) -> dict:
    """LangChain の usage_metadata（input_tokens 等）・台帳の形（input 等）のどちらも {"input","output","total"} へ。"""
    usage = usage or {}

    def _get(*keys: str) -> int:
        for k in keys:
            v = usage.get(k)
            if v:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
        return 0

    inp = _get("input", "input_tokens")
    out = _get("output", "output_tokens")
    total = _get("total", "total_tokens") or inp + out
    return {"input": inp, "output": out, "total": total}


def record(instances_root: Path, source: str, owner: str | None, usage: dict | None, *, calls: int = 1, when: datetime | None = None) -> None:
    """usage（LLM 呼び出し calls 回分の合計）を当月・source・owner の行へ加算する。トークンが0なら何もしない。"""
    u = normalize_usage(usage)
    if not any(u.values()):
        return
    path = db_path(instances_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(path), timeout=30)) as conn:
        conn.executescript(_DDL)
        conn.execute(
            "INSERT INTO ai_usage_monthly (month, source, owner, input, output, total, calls) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(month, source, owner) DO UPDATE SET input = input + excluded.input, "
            "output = output + excluded.output, total = total + excluded.total, calls = calls + excluded.calls",
            (_month(when), source, owner or "anonymous", u["input"], u["output"], u["total"], max(int(calls or 0), 0)),
        )
        conn.commit()


def record_safely(instances_root: Path, source: str, owner: str | None, usage: dict | None, *, calls: int = 1) -> None:
    """record() の失敗をログに残すだけにする（集計のために本来の処理を止めない）。"""
    try:
        record(instances_root, source, owner, usage, calls=calls)
    except Exception:  # noqa: BLE001
        logger.warning("Could not record the admin AI token usage (source=%s)", source, exc_info=True)


# 月・インスタンス・ユーザーごとの使用量の項目。total 等は生成（チャット）の LLM、emb_* は埋め込み
# （src/embedding_usage.py。課金は入力トークンだけのため、トークン数とリクエスト数だけ）。
USAGE_FIELDS = ("input", "output", "total", "calls", "emb_tokens", "emb_calls")


def zero_usage() -> dict:
    return {k: 0 for k in USAGE_FIELDS}


def build_monthly_usage(gen_rows: list[dict], emb_rows: list[dict], source: str | None) -> dict:
    """生成の台帳の行と埋め込みの台帳の行から、admin/usage_cost.py の monthly_usage() の形を作る。

    Args:
        gen_rows: {"month", "owner", "input", "output", "total", "calls", ("backfilled")} の並び。
        emb_rows: {"month", "owner", "tokens", "calls"} の並び（src/embedding_usage.read_rows）。

    Returns:
        {"source", "months": [{"month", **USAGE_FIELDS, "backfilled", "users": {owner: {USAGE_FIELDS}}}, ...]（古い順）,
         "users": [owner, ...]（全期間の生成＋埋め込みのトークンが多い順）}
    """
    months: dict[str, dict] = {}
    totals: dict[str, int] = {}

    def bucket(month: str, owner: str) -> tuple[dict, dict]:
        m = months.setdefault(month, {"month": month, **zero_usage(), "backfilled": False, "users": {}})
        return m, m["users"].setdefault(owner, zero_usage())

    for r in gen_rows:
        m, u = bucket(r["month"], r["owner"])
        for k in ("input", "output", "total", "calls"):
            v = int(r.get(k) or 0)
            m[k] += v
            u[k] += v
        m["backfilled"] = m["backfilled"] or bool(r.get("backfilled"))
        totals[r["owner"]] = totals.get(r["owner"], 0) + int(r.get("total") or 0)
    for r in emb_rows:
        m, u = bucket(r["month"], r["owner"])
        tokens, calls = int(r.get("tokens") or 0), int(r.get("calls") or 0)
        m["emb_tokens"] += tokens
        u["emb_tokens"] += tokens
        m["emb_calls"] += calls
        u["emb_calls"] += calls
        totals[r["owner"]] = totals.get(r["owner"], 0) + tokens
    return {
        "source": source,
        "months": [months[k] for k in sorted(months)],
        "users": sorted(totals, key=lambda o: (-totals[o], o)),
    }


def monthly_usage_by_source(instances_root: Path) -> dict[str, dict]:
    """source → admin/usage_cost.py の monthly_usage() と同じ形（source="admin_ledger"）。台帳が無ければ空。

    埋め込みの台帳（同じファイルの embedding_usage_monthly。管理アシスタントの索引等）も同じ source に合わせて載せる。
    """
    path = db_path(instances_root)
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ai_usage_monthly'").fetchone()
        rows = conn.execute("SELECT month, source, owner, input, output, total, calls FROM ai_usage_monthly").fetchall() if exists else []
        emb_rows = embedding_usage.read_rows(conn)
    gen_by_source: dict[str, list[dict]] = {}
    emb_by_source: dict[str, list[dict]] = {}
    for r in rows:
        gen_by_source.setdefault(r["source"], []).append(dict(r))
    for r in emb_rows:
        emb_by_source.setdefault(r["source"], []).append(r)
    return {
        name: build_monthly_usage(gen_by_source.get(name, []), emb_by_source.get(name, []), "admin_ledger")
        for name in sorted(set(gen_by_source) | set(emb_by_source))
    }
