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


def _usage_from_rows(rows: list[tuple]) -> dict:
    """rows: (month, owner, input, output) のリストから monthly_usage() と同じ形を作る。"""
    months: dict[str, dict] = {}
    for month, owner, inp, out in rows:
        m = months.setdefault(month, {"month": month, "input": 0, "output": 0, "total": 0, "calls": 0, "backfilled": False, "users": {}})
        m["input"] += inp
        m["output"] += out
        m["total"] += inp + out
        m["calls"] += 1
        u = m["users"].setdefault(owner, {"input": 0, "output": 0, "total": 0, "calls": 0})
        u["input"] += inp
        u["output"] += out
        u["total"] += inp + out
        u["calls"] += 1
    return {"source": "ledger", "months": [months[k] for k in sorted(months)], "users": sorted({r[1] for r in rows})}


# inst_a: 10月 alice 1M（入力 0.6M・出力 0.4M）、11月 alice 0.1M
# inst_b: 10月 alice 1M・bob 2M（入力のみ）
ITEMS = [
    {"name": "inst_a", "usage": _usage_from_rows([("2026-10", "alice", 600_000, 400_000), ("2026-11", "alice", 100_000, 0)])},
    {"name": "inst_b", "usage": _usage_from_rows([("2026-10", "alice", 1_000_000, 0), ("2026-10", "bob", 2_000_000, 0)])},
]


def _settings(**kw) -> dict:
    return usage_cost.validate_settings(kw)


def test_combine_usage_merges_instances_and_same_named_users() -> None:
    combined = usage_cost.combine_usage(ITEMS)
    oct_ = combined["months"][0]
    assert oct_["total"] == 4_000_000
    assert oct_["instances"]["inst_b"]["total"] == 3_000_000
    assert oct_["users"]["alice"]["total"] == 2_000_000
    assert combined["instances"] == ["inst_b", "inst_a"]
    assert combined["users"] == ["alice", "bob"]
    assert combined["user_instances"]["alice"] == ["inst_a", "inst_b"]


def test_monthly_bill_is_one_shared_amount_split_by_share() -> None:
    settings = _settings(pricing_model="monthly_bill", exchange_rate=150, fixed_monthly=99, months={"2026-10": {"bill": 400}})
    view = usage_cost.overview(ITEMS, settings)
    by_month = {m["month"]: m for m in view["months"]}
    oct_ = by_month["2026-10"]
    # 請求額は全インスタンスで1つ（インスタンスごとに数えない）。請求額モードでは固定費を足さない
    assert oct_["cost"] == pytest.approx(400)
    assert oct_["cost_jpy"] == pytest.approx(60_000)
    assert oct_["cost_per_million"] == pytest.approx(100)
    assert oct_["instances"]["inst_a"]["cost"] == pytest.approx(100)
    assert oct_["instances"]["inst_b"]["cost"] == pytest.approx(300)
    assert oct_["users"]["alice"]["cost"] == pytest.approx(200)
    assert oct_["users"]["bob"]["cost"] == pytest.approx(200)
    assert by_month["2026-11"]["cost"] is None  # 請求額の未入力
    users = {u["user"]: u for u in view["users"]}
    assert users["alice"]["instances"] == ["inst_a", "inst_b"]
    assert users["alice"]["cost"] == pytest.approx(200)
    assert view["total_cost"] == pytest.approx(400)
    assert view["instances_by_usage"] == ["inst_b", "inst_a"]


def test_bill_month_without_usage_is_listed() -> None:
    view = usage_cost.overview(ITEMS, _settings(months={"2026-12": {"bill": 5}}))
    dec = {m["month"]: m for m in view["months"]}["2026-12"]
    assert dec["total"] == 0 and dec["cost"] == pytest.approx(5) and dec["instances"] == {}


def test_hourly_defaults_to_all_hours_and_adds_fixed_cost_once() -> None:
    settings = _settings(pricing_model="hourly", hourly_rate=1.0, fixed_monthly=10, months={"2026-11": {"hours": 100}})
    by_month = {m["month"]: m for m in usage_cost.overview(ITEMS, settings)["months"]}
    assert by_month["2026-10"]["hours"] == 31 * 24
    assert by_month["2026-10"]["cost"] == pytest.approx(31 * 24 + 10)
    assert by_month["2026-11"]["cost"] == pytest.approx(110)
    assert by_month["2026-11"]["instances"]["inst_a"]["cost"] == pytest.approx(110)


def test_per_token_charges_each_part_its_own_tokens() -> None:
    settings = _settings(pricing_model="per_token", input_per_million=3, output_per_million=15, fixed_monthly=40)
    oct_ = {m["month"]: m for m in usage_cost.overview(ITEMS, settings)["months"]}["2026-10"]
    # 入力 3.6M × $3 + 出力 0.4M × $15 + 固定費 $40
    assert oct_["cost"] == pytest.approx(10.8 + 6 + 40)
    assert oct_["instances"]["inst_a"]["cost"] == pytest.approx(0.6 * 3 + 0.4 * 15 + 40 * 0.25)
    assert oct_["users"]["bob"]["cost"] == pytest.approx(2 * 3 + 40 * 0.5)
    assert sum(i["cost"] for i in oct_["instances"].values()) == pytest.approx(oct_["cost"])
    assert sum(u["cost"] for u in oct_["users"].values()) == pytest.approx(oct_["cost"])


def test_instance_costs_are_its_share_of_the_shared_cost() -> None:
    settings = _settings(pricing_model="monthly_bill", exchange_rate=100, months={"2026-10": {"bill": 400}})
    costs = usage_cost.instance_costs(ITEMS[1]["usage"], ITEMS, settings)
    oct_ = costs["months"][0]
    assert oct_["share_of_all"] == pytest.approx(0.75)
    assert oct_["cost"] == pytest.approx(300)
    assert oct_["cost_jpy"] == pytest.approx(30_000)
    assert oct_["cost_per_million"] == pytest.approx(100)
    users = {u["user"]: u for u in oct_["users"]}
    # share はインスタンス内の割合、金額は全体からの按分
    assert users["bob"]["share"] == pytest.approx(2 / 3)
    assert users["bob"]["cost"] == pytest.approx(200)
    assert costs["total_cost"] == pytest.approx(300)


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


def test_settings_are_shared_and_round_trip(tmp_path) -> None:
    assert usage_cost.read_settings(tmp_path)["pricing_model"] == "monthly_bill"
    saved = usage_cost.write_settings(
        tmp_path, {"pricing_model": "hourly", "hourly_rate": "1.2", "months": {"2026-10": {"hours": "", "bill": ""}}}
    )
    assert saved["months"] == {}  # 空欄だけの月は保存しない
    assert (tmp_path / "aws_cost.json").is_file()  # インスタンス別ではなく instances_dir 直下
    loaded = usage_cost.read_settings(tmp_path)
    assert loaded["pricing_model"] == "hourly" and loaded["hourly_rate"] == 1.2


def _api_setup(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from admin import monitor, server

    root = tmp_path / "instances"
    for name, kind in (("inst_a", "app"), ("inst_b", "app"), ("lab1", "skill_lab")):
        (root / name).mkdir(parents=True)
        (root / name / "instance.json").write_text(json.dumps({"display_name": name.upper(), "kind": kind}), encoding="utf-8")
    db_a = tmp_path / "a.sqlite"
    _make_db(db_a)

    def _fake_config(instances_root, config_ini_path, name):
        if name == "inst_b":
            raise monitor.MonitorError("broken config")
        return SimpleNamespace(thread_store_db=db_a)

    monkeypatch.setattr(server, "INSTANCES_ROOT", root)
    monkeypatch.setattr(server, "AUDIT_LOG_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(monitor, "instance_config", _fake_config)
    monkeypatch.setitem(server.app.dependency_overrides, server.require_login, lambda: "admin")
    monkeypatch.setitem(server.app.dependency_overrides, server.require_csrf, lambda: None)
    return root, server


def test_usage_api_saves_shared_settings_and_allocates(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    root, server = _api_setup(tmp_path, monkeypatch)
    client = TestClient(server.app)
    res = client.put("/api/usage/cost-settings", json={"pricing_model": "monthly_bill", "months": {"2026-10": {"bill": 30}}})
    assert res.status_code == 200
    assert (root / "aws_cost.json").is_file()
    data = res.json()
    # スキル調整ワーカーは対象外。読めないインスタンスは errors に出し、使用量0として扱う
    assert [i["name"] for i in data["instances"]] == ["inst_a", "inst_b"]
    assert data["errors"] == [{"name": "inst_b", "error": "broken config"}]
    assert data["settings"]["months"] == {"2026-10": {"bill": 30.0}}
    oct_ = {m["month"]: m for m in data["months"]}["2026-10"]
    assert oct_["total"] == 3_000_000 and oct_["cost"] == pytest.approx(30)
    assert oct_["instances"]["inst_a"]["cost"] == pytest.approx(30)

    detail = client.get("/api/instances/inst_a/usage").json()
    assert detail["settings"]["pricing_model"] == "monthly_bill"
    inst_oct = {m["month"]: m for m in detail["costs"]["months"]}["2026-10"]
    assert inst_oct["share_of_all"] == pytest.approx(1.0) and inst_oct["cost"] == pytest.approx(30)

    assert client.put("/api/usage/cost-settings", json={"pricing_model": "x"}).status_code == 400
    assert json.loads((tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[0])["action"] == "usage_cost_settings_update"
