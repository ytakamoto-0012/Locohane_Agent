"""月別のトークン使用量（全ユーザー合計・ユーザー別）と、手入力した AWS 利用料金からの金額計算。

設定ダッシュボードの上部タブ「利用量・料金」（全インスタンス）と、インスタンス画面の
「利用量・料金」サブタブ用。

- 使用量: 各インスタンスのスレッドDB（[thread_store] db）の token_usage_monthly
  （本体が LLM 呼び出しごとに加算する台帳。src/thread_store.py 参照）を読み取り専用で読む。
  本体を更新後まだ一度も起動していない（台帳が無い）DBでは、スレッドの累計を
  最終更新月へ振り分けた概算を返す（source="threads_estimate"）。
- 料金設定: 全インスタンス共通の <instances_dir>/aws_cost.json（このモジュールだけが読み書きする）。
  AWS の料金は次の3つの形態から選んで手入力する。
    monthly_bill: 月ごとの請求額（AWS 請求書・Cost Explorer の実績値）をそのまま入れる。
    hourly:       EC2 等の時間課金。時間単価 × 稼働時間（月ごとに上書き可、既定はその月の全時間）。
    per_token:    Amazon Bedrock のオンデマンド等のトークン課金。入力・出力の100万トークン単価。
  hourly / per_token には、EBS・データ転送等の月額固定費（fixed_monthly）を足せる。
- 按分: 請求額・時間課金・固定費は全インスタンスで1つの金額とし、インスタンス別・ユーザー別には
  その月の全インスタンス合計トークンに占める割合で按分する。per_token の従量分だけは、
  それぞれの入力・出力トークン×単価で求める（slice_cost）。
"""

from __future__ import annotations

import calendar
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from admin.i18n import t as _t

from . import instances as inst
from . import monitor

COST_FILENAME = "aws_cost.json"
PRICING_MODELS = ("monthly_bill", "hourly", "per_token")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

DEFAULT_SETTINGS: dict = {
    "pricing_model": "monthly_bill",
    # 表示通貨は米ドル。exchange_rate（1ドルあたりの円）を入れると円換算も出す。
    "exchange_rate": None,
    "hourly_rate": None,
    "input_per_million": None,
    "output_per_million": None,
    "fixed_monthly": None,
    # "YYYY-MM" -> {"bill": 請求額, "hours": 稼働時間}（どちらも省略可）
    "months": {},
    "note": "",
}


class UsageCostError(ValueError):
    """料金設定の値が不正。"""


def _usage_month(when: datetime) -> str:
    """src/thread_store.py の usage_month と同じ（ローカル時刻の "YYYY-MM"）。

    管理ツールから src.thread_store（chainlit に依存）を読み込まないよう複製している。
    """
    if when.tzinfo is not None:
        when = when.astimezone()
    return when.strftime("%Y-%m")


# ---------------------------------------------------------------------------
# 使用量
# ---------------------------------------------------------------------------


def _usage_rows_from_ledger(conn: sqlite3.Connection) -> list[dict] | None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'token_usage_monthly'"
    ).fetchone()
    if not exists:
        return None
    rows = conn.execute(
        "SELECT month, owner, input, output, total, calls, backfilled FROM token_usage_monthly"
    ).fetchall()
    return [dict(r) for r in rows]


def _usage_rows_from_threads(conn: sqlite3.Connection) -> list[dict]:
    """台帳が無いDB用の概算（src/thread_store.py の _backfill_monthly_usage と同じ振り分け）。"""
    buckets: dict[tuple[str, str], dict] = {}
    for r in conn.execute("SELECT owner, updated_at, metadata_json FROM threads").fetchall():
        try:
            cum = (json.loads(r["metadata_json"] or "{}") or {}).get("token_usage_cumulative") or {}
            month = _usage_month(datetime.fromisoformat(r["updated_at"]))
        except (ValueError, TypeError, AttributeError):
            continue
        b = buckets.setdefault(
            (month, r["owner"]),
            {"month": month, "owner": r["owner"], "input": 0, "output": 0, "total": 0, "calls": 0, "backfilled": 1},
        )
        for k in ("input", "output", "total"):
            b[k] += int(cum.get(k, 0) or 0)
    return [b for b in buckets.values() if b["total"] or b["input"] or b["output"]]


def monthly_usage(db_path: Path) -> dict:
    """月別・ユーザー別のトークン使用量。

    Returns:
        {"source": "ledger" | "threads_estimate",
         "months": [{"month", "input", "output", "total", "calls", "backfilled",
                     "users": {owner: {"input","output","total","calls"}}}, ...]（古い順）,
         "users": [owner, ...]（全期間の合計トークンが多い順）}
    """
    with closing(monitor._connect(db_path)) as conn:  # noqa: SLF001 - 読み取り専用接続を共用する
        rows = _usage_rows_from_ledger(conn)
        source = "ledger"
        if rows is None:
            rows = _usage_rows_from_threads(conn)
            source = "threads_estimate"
    months: dict[str, dict] = {}
    user_totals: dict[str, int] = {}
    for r in rows:
        m = months.setdefault(
            r["month"], {"month": r["month"], "input": 0, "output": 0, "total": 0, "calls": 0, "backfilled": False, "users": {}}
        )
        u = m["users"].setdefault(r["owner"], {"input": 0, "output": 0, "total": 0, "calls": 0})
        for k in ("input", "output", "total", "calls"):
            v = int(r[k] or 0)
            m[k] += v
            u[k] += v
        m["backfilled"] = m["backfilled"] or bool(r["backfilled"])
        user_totals[r["owner"]] = user_totals.get(r["owner"], 0) + int(r["total"] or 0)
    return {
        "source": source,
        "months": [months[k] for k in sorted(months)],
        "users": sorted(user_totals, key=lambda u: (-user_totals[u], u)),
    }



# ---------------------------------------------------------------------------
# 料金設定（全インスタンス共通。<instances_dir>/aws_cost.json）
# ---------------------------------------------------------------------------


def cost_path(instances_root: Path) -> Path:
    return instances_root / COST_FILENAME


def _number(value, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise UsageCostError(_t("{0} には数値を入れてください。", field)) from exc
    if number != number or number < 0 or number == float("inf"):
        raise UsageCostError(_t("{0} には0以上の数値を入れてください。", field))
    return number


def validate_settings(data: dict) -> dict:
    """画面から受け取った料金設定を検証し、保存する形へ正規化する。"""
    if not isinstance(data, dict):
        raise UsageCostError(_t("料金設定の形式が正しくありません。"))
    model = data.get("pricing_model") or "monthly_bill"
    if model not in PRICING_MODELS:
        raise UsageCostError(_t("料金形態は {0} のいずれかにしてください。", " / ".join(PRICING_MODELS)))
    result = {
        "pricing_model": model,
        "exchange_rate": _number(data.get("exchange_rate"), "exchange_rate"),
        "hourly_rate": _number(data.get("hourly_rate"), "hourly_rate"),
        "input_per_million": _number(data.get("input_per_million"), "input_per_million"),
        "output_per_million": _number(data.get("output_per_million"), "output_per_million"),
        "fixed_monthly": _number(data.get("fixed_monthly"), "fixed_monthly"),
        "months": {},
        "note": str(data.get("note") or "")[:2000],
    }
    months = data.get("months") or {}
    if not isinstance(months, dict):
        raise UsageCostError(_t("料金設定の形式が正しくありません。"))
    for month, entry in months.items():
        if not _MONTH_RE.match(str(month)):
            raise UsageCostError(_t("月は YYYY-MM の形式で指定してください: {0}", month))
        entry = entry if isinstance(entry, dict) else {}
        hours = _number(entry.get("hours"), f"{month} hours")
        if hours is not None and hours > 24 * 31:
            raise UsageCostError(_t("{0} の稼働時間が1か月の時間数を超えています。", month))
        clean = {"bill": _number(entry.get("bill"), f"{month} bill"), "hours": hours}
        clean = {k: v for k, v in clean.items() if v is not None}
        if clean:
            result["months"][str(month)] = clean
    return result


def read_settings_default() -> dict:
    return json.loads(json.dumps(DEFAULT_SETTINGS))


def read_settings(instances_root: Path) -> dict:
    path = cost_path(instances_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return read_settings_default()
    except (OSError, ValueError) as exc:
        raise UsageCostError(_t("{0} を読めません: {1}", path.name, exc)) from exc
    return {**read_settings_default(), **validate_settings(data)}


def write_settings(instances_root: Path, data: dict) -> dict:
    clean = validate_settings(data)
    path = cost_path(instances_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return clean


# ---------------------------------------------------------------------------
# 全インスタンスの使用量の合算
# ---------------------------------------------------------------------------

_USAGE_FIELDS = ("input", "output", "total", "calls")


def _zero() -> dict:
    return {k: 0 for k in _USAGE_FIELDS}


def _add_usage(dst: dict, src: dict) -> None:
    for k in _USAGE_FIELDS:
        dst[k] += int(src.get(k, 0) or 0)


def combine_usage(items: list[dict]) -> dict:
    """インスタンスごとの monthly_usage() を、月別に全インスタンス合計へまとめる。

    Args:
        items: [{"name", "usage": monthly_usage()}, ...]（読めなかったインスタンスは usage を空にして渡す）。

    Returns:
        {"months": [{"month", "input", "output", "total", "calls", "backfilled",
                     "instances": {name: {"input","output","total","calls"}},
                     "users": {owner: {"input","output","total","calls"}}}, ...]（古い順）,
         "instances": [name, ...]・"users": [owner, ...]（全期間の合計トークンが多い順）,
         "user_instances": {owner: [name, ...]}}
        ユーザーはインスタンスをまたいで同じ名前を同じユーザーとして合算する。
    """
    months: dict[str, dict] = {}
    instance_totals: dict[str, int] = {}
    user_totals: dict[str, int] = {}
    user_instances: dict[str, set] = {}
    for item in items:
        name = item["name"]
        instance_totals.setdefault(name, 0)
        for um in item["usage"].get("months", []):
            m = months.setdefault(um["month"], {"month": um["month"], **_zero(), "backfilled": False, "instances": {}, "users": {}})
            _add_usage(m, um)
            m["backfilled"] = m["backfilled"] or bool(um["backfilled"])
            _add_usage(m["instances"].setdefault(name, _zero()), um)
            instance_totals[name] += um["total"]
            for owner, uu in um["users"].items():
                _add_usage(m["users"].setdefault(owner, _zero()), uu)
                user_totals[owner] = user_totals.get(owner, 0) + uu["total"]
                user_instances.setdefault(owner, set()).add(name)
    return {
        "months": [months[k] for k in sorted(months)],
        "instances": sorted(instance_totals, key=lambda n: (-instance_totals[n], n)),
        "users": sorted(user_totals, key=lambda u: (-user_totals[u], u)),
        "user_instances": {u: sorted(v) for u, v in user_instances.items()},
    }


# ---------------------------------------------------------------------------
# 金額の計算
# ---------------------------------------------------------------------------


def hours_in_month(month: str) -> int:
    year, mon = (int(x) for x in month.split("-"))
    return calendar.monthrange(year, mon)[1] * 24


def month_costs(total_months: list[dict], settings: dict) -> dict[str, dict]:
    """全インスタンス合計の使用量から、月ごとの料金（全体で1つの金額）を求める。

    請求額・時間課金・固定費は全インスタンスで共有する1つの金額。金額を出せない月
    （請求額・単価の未入力）は cost=None。

    Returns: month -> {"month", "input", "output", "total", "hours", "cost", "fixed", "cost_jpy", "cost_per_million"}
    """
    model = settings.get("pricing_model") or "monthly_bill"
    rate = settings.get("exchange_rate")
    fixed = settings.get("fixed_monthly") or 0.0
    entries = settings.get("months") or {}
    by_month = {m["month"]: m for m in total_months}
    result: dict[str, dict] = {}
    for month in sorted(set(by_month) | set(entries)):
        u = by_month.get(month) or _zero()
        entry = entries.get(month) or {}
        hours = None
        if model == "monthly_bill":
            variable, month_fixed = entry.get("bill"), 0.0
        elif model == "hourly":
            hours = entry.get("hours", hours_in_month(month))
            variable = None if settings.get("hourly_rate") is None else settings["hourly_rate"] * hours
            month_fixed = fixed
        else:
            in_price, out_price = settings.get("input_per_million"), settings.get("output_per_million")
            variable = (
                None
                if in_price is None and out_price is None
                else u["input"] / 1e6 * (in_price or 0) + u["output"] / 1e6 * (out_price or 0)
            )
            month_fixed = fixed
        cost = None if variable is None else variable + month_fixed
        result[month] = {
            "month": month,
            "input": u["input"],
            "output": u["output"],
            "total": u["total"],
            "hours": hours,
            "cost": cost,
            "fixed": month_fixed,
            "cost_jpy": None if cost is None or rate is None else cost * rate,
            "cost_per_million": None if cost is None or not u["total"] else cost / u["total"] * 1e6,
        }
    return result


def slice_cost(settings: dict, mc: dict, part: dict) -> dict:
    """その月の料金 mc のうち、使用量 part（インスタンス・ユーザー等の一部分）が負担する金額。

    トークン課金なら part の入力・出力トークン×単価＋固定費の割合分、それ以外は
    その月の全インスタンス合計トークンに占める割合で按分する。
    """
    share = part["total"] / mc["total"] if mc["total"] else 0.0
    cost = None
    if mc["cost"] is not None:
        if (settings.get("pricing_model") or "monthly_bill") == "per_token":
            cost = (
                part["input"] / 1e6 * (settings.get("input_per_million") or 0)
                + part["output"] / 1e6 * (settings.get("output_per_million") or 0)
                + mc["fixed"] * share
            )
        else:
            cost = mc["cost"] * share
    rate = settings.get("exchange_rate")
    return {"total": part["total"], "share": share, "cost": cost, "cost_jpy": None if cost is None or rate is None else cost * rate}


def _sum_optional(values) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def overview(items: list[dict], settings: dict) -> dict:
    """全インスタンスの月別使用量・料金と、インスタンス別・ユーザー別の按分。"""
    combined = combine_usage(items)
    mcs = month_costs(combined["months"], settings)
    usage_by_month = {m["month"]: m for m in combined["months"]}
    months = []
    users: dict[str, dict] = {}
    for month, mc in mcs.items():
        u = usage_by_month.get(month) or {**_zero(), "backfilled": False, "instances": {}, "users": {}}
        user_costs = {owner: slice_cost(settings, mc, uu) for owner, uu in u["users"].items()}
        for owner, sc in user_costs.items():
            agg = users.setdefault(owner, {"user": owner, "total": 0, "costs": [], "jpy": []})
            agg["total"] += sc["total"]
            agg["costs"].append(sc["cost"])
            agg["jpy"].append(sc["cost_jpy"])
        months.append(
            {
                **mc,
                "calls": u["calls"],
                "backfilled": u["backfilled"],
                "instances": {n: slice_cost(settings, mc, iu) for n, iu in u["instances"].items()},
                "users": user_costs,
            }
        )
    user_list = [
        {
            "user": a["user"],
            "total": a["total"],
            "cost": _sum_optional(a["costs"]),
            "cost_jpy": _sum_optional(a["jpy"]),
            "instances": combined["user_instances"].get(a["user"], []),
        }
        for a in users.values()
    ]
    user_list.sort(key=lambda x: (-x["total"], x["user"]))
    return {
        "pricing_model": settings.get("pricing_model") or "monthly_bill",
        "months": months,
        "instances_by_usage": combined["instances"],
        "users": user_list,
        "total_cost": _sum_optional(m["cost"] for m in months),
        "total_cost_jpy": _sum_optional(m["cost_jpy"] for m in months),
    }


def instance_costs(instance_usage: dict, all_items: list[dict], settings: dict) -> dict:
    """1インスタンスの月ごとの負担額（全インスタンス共通の料金からの按分）と、その中のユーザー別内訳。

    Args:
        instance_usage: そのインスタンスの monthly_usage()。
        all_items: 按分の分母にする全インスタンスの [{"name", "usage"}, ...]（このインスタンスを含む）。
    """
    mcs = month_costs(combine_usage(all_items)["months"], settings)
    months = []
    for um in instance_usage.get("months", []):
        mc = mcs.get(um["month"])
        if mc is None:  # 分母に入っていない（集計対象外のインスタンス等）
            mc = month_costs([um], settings)[um["month"]]
        own = slice_cost(settings, mc, um)
        users = []
        for owner, uu in um["users"].items():
            sc = slice_cost(settings, mc, uu)
            users.append({"user": owner, **sc, "share": uu["total"] / um["total"] if um["total"] else 0.0})
        users.sort(key=lambda x: (-x["total"], x["user"]))
        months.append(
            {
                "month": um["month"],
                "total": um["total"],
                "share_of_all": own["share"],
                "cost": own["cost"],
                "cost_jpy": own["cost_jpy"],
                "cost_per_million": None if own["cost"] is None or not um["total"] else own["cost"] / um["total"] * 1e6,
                "users": users,
            }
        )
    return {
        "pricing_model": settings.get("pricing_model") or "monthly_bill",
        "months": months,
        "total_cost": _sum_optional(m["cost"] for m in months),
        "total_cost_jpy": _sum_optional(m["cost_jpy"] for m in months),
    }
