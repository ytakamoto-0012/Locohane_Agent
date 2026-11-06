"""src/thread_store.py の月別トークン台帳（token_usage_monthly）のテスト。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from src import thread_store


async def _rows(conn):
    cursor = await conn.execute(
        "SELECT month, owner, input, output, total, calls, backfilled FROM token_usage_monthly ORDER BY month, owner"
    )
    return [tuple(r) for r in await cursor.fetchall()]


@pytest.mark.asyncio
async def test_add_monthly_usage_accumulates_per_month_and_owner(tmp_path) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        oct_ = datetime(2026, 10, 15, 12, 0)
        nov = datetime(2026, 11, 2, 9, 0)
        await thread_store.add_monthly_usage(conn, "alice", {"input": 90, "output": 10, "total": 100}, when=oct_)
        await thread_store.add_monthly_usage(conn, "alice", {"input": 9, "output": 1, "total": 10}, when=oct_)
        await thread_store.add_monthly_usage(conn, "bob", {"input": 5, "output": 5, "total": 10}, when=oct_)
        await thread_store.add_monthly_usage(conn, "alice", {"input": 1, "output": 1, "total": 2}, when=nov)
        # 0 トークンの呼び出しは数えない
        await thread_store.add_monthly_usage(conn, "alice", {"input": 0, "output": 0, "total": 0}, when=nov)
        assert await _rows(conn) == [
            ("2026-10", "alice", 99, 11, 110, 2, 0),
            ("2026-10", "bob", 5, 5, 10, 1, 0),
            ("2026-11", "alice", 1, 1, 2, 1, 0),
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_concurrent_adds_are_not_lost(tmp_path) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        when = datetime(2026, 10, 1)
        await asyncio.gather(
            *(thread_store.add_monthly_usage(conn, "alice", {"input": 1, "output": 1, "total": 2}, when=when) for _ in range(20))
        )
        assert await _rows(conn) == [("2026-10", "alice", 20, 20, 40, 20, 0)]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_existing_thread_totals_are_backfilled_once(tmp_path) -> None:
    db = tmp_path / "chat_threads.sqlite"
    conn = await thread_store.init_db(db)
    # 台帳の導入前に作られたDBを再現する（台帳と補完済みの印を消す）
    await conn.execute("DROP TABLE token_usage_monthly")
    await conn.execute("DELETE FROM store_meta")
    await conn.commit()
    updated = datetime(2026, 9, 20, 12, 0).astimezone().isoformat()
    for tid, owner, total in (("t1", "alice", 100), ("t2", "alice", 50), ("t3", "bob", 30), ("t4", "bob", 0)):
        await conn.execute(
            "INSERT INTO threads (id, owner, name, created_at, updated_at, metadata_json) VALUES (?, ?, NULL, ?, ?, ?)",
            (tid, owner, updated, updated, json.dumps({"token_usage_cumulative": {"input": total, "output": 0, "total": total}})),
        )
    await conn.commit()
    await conn.close()

    conn = await thread_store.init_db(db)
    try:
        assert await _rows(conn) == [("2026-09", "alice", 150, 0, 150, 0, 1), ("2026-09", "bob", 30, 0, 30, 0, 1)]
    finally:
        await conn.close()
    # 2回目の起動では二重に足さない
    conn = await thread_store.init_db(db)
    try:
        assert await _rows(conn) == [("2026-09", "alice", 150, 0, 150, 0, 1), ("2026-09", "bob", 30, 0, 30, 0, 1)]
    finally:
        await conn.close()


def test_usage_month_converts_aware_times_to_local() -> None:
    local = datetime(2026, 10, 31, 23, 30).astimezone()
    assert thread_store.usage_month(local) == "2026-10"
    assert thread_store.usage_month(datetime(2026, 1, 5)) == "2026-01"
