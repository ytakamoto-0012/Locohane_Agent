"""admin/usage_cost.py（月別トークン使用量・AWS 利用料金の計算）のテスト。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from admin import usage_cost


def _make_db(path, *, ledger: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, owner TEXT, name TEXT, created_at TEXT, updated_at TEXT,"
        " metadata_json TEXT, tags_json TEXT);"
    )
    if ledger:
        conn.executescript(
            "CREATE TABLE token_usage_monthly (month TEXT, owner TEXT, input INTEGER, output INTEGER, total INTEGER,"
            " calls INTEGER, backfilled INTEGER, PRIMARY KEY (month, owner));"
        )
        conn.executemany(
            "INSERT INTO token_usage_monthly VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-09", "alice", 800_000, 200_000, 1_000_000, 10, 1),
                ("2026-10", "alice", 1_500_000, 500_000, 2_000_000, 20, 0),
                ("2026-10", "bob", 1_000_000, 0, 1_000_000, 5, 0),
            ],
        )
    else:
        updated = datetime(2026, 10, 3, 12).astimezone().isoformat()
        conn.execute(
            "INSERT INTO threads VALUES ('t1', 'alice', NULL, ?, ?, ?, NULL)",
            (updated, updated, json.dumps({"token_usage_cumulative": {"input": 7, "output": 3, "total": 10}})),
        )
    conn.commit()
    conn.close()


def test_monthly_usage_reads_ledger(tmp_path) -> None:
    db = tmp_path / "chat_threads.sqlite"
    _make_db(db)
    usage = usage_cost.monthly_usage(db)
    assert usage["source"] == "ledger"
    assert [m["month"] for m in usage["months"]] == ["2026-09", "2026-10"]
    oct_ = usage["months"][1]
    assert oct_["total"] == 3_000_000 and oct_["backfilled"] is False
    assert oct_["users"]["bob"]["total"] == 1_000_000
    assert usage["months"][0]["backfilled"] is True
    # 全期間の使用量が多い順
    assert usage["users"] == ["alice", "bob"]


def test_monthly_usage_falls_back_to_thread_totals(tmp_path) -> None:
    db = tmp_path / "chat_threads.sqlite"
    _make_db(db, ledger=False)
    usage = usage_cost.monthly_usage(db)
    assert usage["source"] == "threads_estimate"
    assert usage["months"][0]["month"] == "2026-10"
    assert usage["months"][0]["users"]["alice"] == {"input": 7, "output": 3, "total": 10, "calls": 0}


def _usage(tmp_path):
    db = tmp_path / "chat_threads.sqlite"
    _make_db(db)
    return usage_cost.monthly_usage(db)


def test_monthly_bill_is_split_by_token_share(tmp_path) -> None:
    settings = usage_cost.validate_settings(
        {"pricing_model": "monthly_bill", "exchange_rate": "150", "fixed_monthly": "99", "months": {"2026-10": {"bill": "300"}}}
    )
    costs = usage_cost.compute_costs(_usage(tmp_path), settings)
    by_month = {m["month"]: m for m in costs["months"]}
    assert by_month["2026-09"]["cost"] is None  # 請求額の未入力
    oct_ = by_month["2026-10"]
    assert oct_["cost"] == pytest.approx(300)  # 請求額モードでは固定費を足さない
    assert oct_["cost_jpy"] == pytest.approx(45_000)
    assert oct_["cost_per_million"] == pytest.approx(100)
    users = {u["user"]: u for u in oct_["users"]}
    assert users["alice"]["cost"] == pytest.approx(200)
    assert users["bob"]["cost"] == pytest.approx(100)
    assert costs["total_cost"] == pytest.approx(300)


def test_hourly_defaults_to_all_hours_and_adds_fixed_cost(tmp_path) -> None:
    settings = usage_cost.validate_settings(
        {"pricing_model": "hourly", "hourly_rate": 1.0, "fixed_monthly": 10, "months": {"2026-10": {"hours": 100}}}
    )
    by_month = {m["month"]: m for m in usage_cost.compute_costs(_usage(tmp_path), settings)["months"]}
    assert by_month["2026-09"]["hours"] == 30 * 24
    assert by_month["2026-09"]["cost"] == pytest.approx(30 * 24 + 10)
    assert by_month["2026-10"]["cost"] == pytest.approx(110)


def test_per_token_charges_each_user_their_own_tokens(tmp_path) -> None:
    settings = usage_cost.validate_settings(
        {"pricing_model": "per_token", "input_per_million": 3, "output_per_million": 15, "fixed_monthly": 30}
    )
    oct_ = {m["month"]: m for m in usage_cost.compute_costs(_usage(tmp_path), settings)["months"]}["2026-10"]
    # 入力 2.5M × $3 + 出力 0.5M × $15 + 固定費 $30
    assert oct_["cost"] == pytest.approx(7.5 + 7.5 + 30)
    users = {u["user"]: u for u in oct_["users"]}
    assert users["alice"]["cost"] == pytest.approx(1.5 * 3 + 0.5 * 15 + 30 * 2 / 3)
    assert users["bob"]["cost"] == pytest.approx(1.0 * 3 + 30 / 3)
    assert sum(u["cost"] for u in oct_["users"]) == pytest.approx(oct_["cost"])


@pytest.mark.parametrize(
    "data",
    [
        {"pricing_model": "free"},
        {"hourly_rate": "-1"},
        {"hourly_rate": "abc"},
        {"months": {"2026-13": {"bill": 1}}},
        {"months": {"2026-10": {"hours": 1000}}},
    ],
)
def test_invalid_settings_are_rejected(data) -> None:
    with pytest.raises(usage_cost.UsageCostError):
        usage_cost.validate_settings(data)


def test_settings_round_trip(tmp_path) -> None:
    (tmp_path / "default").mkdir()
    assert usage_cost.read_settings(tmp_path, "default")["pricing_model"] == "monthly_bill"
    saved = usage_cost.write_settings(
        tmp_path, "default", {"pricing_model": "hourly", "hourly_rate": "1.2", "months": {"2026-10": {"hours": "", "bill": ""}}}
    )
    assert saved["months"] == {}  # 空欄だけの月は保存しない
    loaded = usage_cost.read_settings(tmp_path, "default")
    assert loaded["pricing_model"] == "hourly" and loaded["hourly_rate"] == 1.2
