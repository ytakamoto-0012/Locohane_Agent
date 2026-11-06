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


def _item(name: str, rows: list[tuple], settings: dict) -> dict:
    """rows: (month, owner, input, output) のリストから aggregate_instances の入力を作る。"""
    months: dict[str, dict] = {}
    for month, owner, inp, out in rows:
        m = months.setdefault(month, {"month": month, "input": 0, "output": 0, "total": 0, "calls": 1, "backfilled": False, "users": {}})
        m["input"] += inp
        m["output"] += out
        m["total"] += inp + out
        m["users"][owner] = {"input": inp, "output": out, "total": inp + out, "calls": 1}
    usage = {"source": "ledger", "months": [months[k] for k in sorted(months)], "users": sorted({r[1] for r in rows})}
    return {"name": name, "usage": usage, "costs": usage_cost.compute_costs(usage, usage_cost.validate_settings(settings))}


def test_aggregate_instances_sums_usage_and_costs() -> None:
    a = _item(
        "inst_a",
        [("2026-10", "alice", 600, 400), ("2026-11", "alice", 100, 0)],
        {"pricing_model": "monthly_bill", "exchange_rate": 150, "months": {"2026-10": {"bill": 10}, "2026-11": {"bill": 2}}},
    )
    b = _item("inst_b", [("2026-10", "alice", 1000, 0), ("2026-10", "bob", 2000, 0)], {"pricing_model": "monthly_bill"})
    c = _item("inst_c", [("2026-10", "bob", 500, 500)], {"pricing_model": "monthly_bill", "months": {"2026-10": {"bill": 4}}})
    agg = usage_cost.aggregate_instances([a, b, c])
    oct_, nov = agg["months"]
    assert oct_["month"] == "2026-10" and oct_["total"] == 1000 + 3000 + 1000
    # 料金は分かるインスタンスの分だけ足し、出せないインスタンスを記録する
    assert oct_["cost"] == pytest.approx(14)
    assert oct_["cost_incomplete"] == ["inst_b"]
    # 円は為替レートを入れたインスタンスの分だけ
    assert oct_["cost_jpy"] == pytest.approx(1500)
    assert oct_["jpy_incomplete"] == ["inst_c"]
    assert oct_["instances"]["inst_c"] == {"total": 1000, "cost": 4.0, "cost_jpy": None}
    # 同じ名前のユーザーはインスタンスをまたいで合算する
    assert oct_["users"]["alice"]["total"] == 2000
    assert oct_["users"]["alice"]["cost"] == pytest.approx(10)
    assert nov["total"] == 100 and nov["cost"] == pytest.approx(2)
    assert agg["instances"] == ["inst_b", "inst_a", "inst_c"]
    users = {u["user"]: u for u in agg["users"]}
    assert users["alice"]["instances"] == ["inst_a", "inst_b"]
    assert users["alice"]["total"] == 2100
    assert users["bob"]["cost"] == pytest.approx(4)
    assert agg["total_cost"] == pytest.approx(16)
    assert agg["total_cost_jpy"] == pytest.approx(1800)


def test_aggregate_instances_without_any_cost() -> None:
    agg = usage_cost.aggregate_instances([_item("inst_a", [("2026-10", "alice", 1, 1)], {})])
    assert agg["total_cost"] is None and agg["months"][0]["cost"] is None
    assert agg["months"][0]["cost_incomplete"] == ["inst_a"]


def test_usage_overview_endpoint_skips_skill_lab_and_reports_errors(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from admin import instances as inst
    from admin import monitor, server

    root = tmp_path / "instances"
    for name, kind in (("inst_a", "app"), ("inst_b", "app"), ("lab1", "skill_lab")):
        (root / name).mkdir(parents=True)
        (root / name / "instance.json").write_text(json.dumps({"display_name": name.upper(), "kind": kind}), encoding="utf-8")
    db_a = tmp_path / "a.sqlite"
    _make_db(db_a)
    usage_cost.write_settings(root, "inst_a", {"pricing_model": "monthly_bill", "months": {"2026-10": {"bill": 30}}})

    def _fake_config(instances_root, config_ini_path, name):
        if name == "inst_b":
            raise monitor.MonitorError("broken config")
        return SimpleNamespace(thread_store_db=db_a)

    monkeypatch.setattr(server, "INSTANCES_ROOT", root)
    monkeypatch.setattr(monitor, "instance_config", _fake_config)
    server.app.dependency_overrides[server.require_login] = lambda: "admin"
    try:
        data = TestClient(server.app).get("/api/usage/overview").json()
    finally:
        server.app.dependency_overrides.pop(server.require_login, None)
    assert [i["name"] for i in data["instances"]] == ["inst_a", "inst_b"]
    assert data["instances"][0]["display_name"] == "INST_A"
    assert data["errors"] == [{"name": "inst_b", "error": "broken config"}]
    assert data["instances_by_usage"][0] == "inst_a"
    oct_ = {m["month"]: m for m in data["months"]}["2026-10"]
    assert oct_["total"] == 3_000_000 and oct_["cost"] == pytest.approx(30)
    assert inst.KIND_SKILL_LAB == "skill_lab"
