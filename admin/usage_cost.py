"""月別のトークン使用量（全ユーザー合計・ユーザー別）と、手入力した AWS 利用料金からの金額計算。

設定ダッシュボードのインスタンス画面「利用量・料金」用。

- 使用量: そのインスタンスのスレッドDB（[thread_store] db）の token_usage_monthly
  （本体が LLM 呼び出しごとに加算する台帳。src/thread_store.py 参照）を読み取り専用で読む。
  本体を更新後まだ一度も起動していない（台帳が無い）DBでは、スレッドの累計を
  最終更新月へ振り分けた概算を返す（source="threads_estimate"）。
- 料金設定: instances/<インスタンス名>/aws_cost.json（このモジュールだけが読み書きする）。
  AWS の料金は次の3つの形態から選んで手入力する。
    monthly_bill: 月ごとの請求額（AWS 請求書・Cost Explorer の実績値）をそのまま入れる。
    hourly:       EC2 等の時間課金。時間単価 × 稼働時間（月ごとに上書き可、既定はその月の全時間）。
    per_token:    Amazon Bedrock のオンデマンド等のトークン課金。入力・出力の100万トークン単価。
  hourly / per_token には、EBS・データ転送等の月額固定費（fixed_monthly）を足せる。
  ユーザー別の金額は、per_token なら各ユーザーの入力・出力トークン×単価、それ以外は
  その月の合計トークンに占める割合で按分する（固定費は常に割合で按分）。
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
# 料金設定（instances/<name>/aws_cost.json）
# ---------------------------------------------------------------------------


def cost_path(instances_root: Path, name: str) -> Path:
    return inst.instance_dir(instances_root, name) / COST_FILENAME


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


def read_settings(instances_root: Path, name: str) -> dict:
    path = cost_path(instances_root, name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    except (OSError, ValueError) as exc:
        raise UsageCostError(_t("{0} を読めません: {1}", path.name, exc)) from exc
    return {**json.loads(json.dumps(DEFAULT_SETTINGS)), **validate_settings(data)}


def write_settings(instances_root: Path, name: str, data: dict) -> dict:
    clean = validate_settings(data)
    path = cost_path(instances_root, name)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return clean


# ---------------------------------------------------------------------------
# 金額の計算
# ---------------------------------------------------------------------------


def hours_in_month(month: str) -> int:
    year, mon = (int(x) for x in month.split("-"))
    return calendar.monthrange(year, mon)[1] * 24


def compute_costs(usage: dict, settings: dict) -> dict:
    """月ごとの金額（米ドル、為替レートがあれば円も）と、ユーザー別の内訳を計算する。

    金額を出せない月（請求額の未入力・単価の未入力）は cost=None。
    """
    model = settings.get("pricing_model") or "monthly_bill"
    rate = settings.get("exchange_rate")
    fixed = settings.get("fixed_monthly") or 0.0
    entries = settings.get("months") or {}
    usage_by_month = {m["month"]: m for m in usage.get("months", [])}
    month_keys = sorted(set(usage_by_month) | set(entries))

    def _jpy(v: float | None) -> float | None:
        return None if v is None or rate is None else v * rate

    result = []
    for month in month_keys:
        u = usage_by_month.get(month) or {"input": 0, "output": 0, "total": 0, "users": {}}
        entry = entries.get(month) or {}
        hours = None
        variable: float | None
        if model == "monthly_bill":
            variable = entry.get("bill")
            month_fixed = 0.0
        elif model == "hourly":
            hours = entry.get("hours", hours_in_month(month))
            variable = None if settings.get("hourly_rate") is None else settings["hourly_rate"] * hours
            month_fixed = fixed
        else:
            in_price, out_price = settings.get("input_per_million"), settings.get("output_per_million")
            if in_price is None and out_price is None:
                variable = None
            else:
                variable = u["input"] / 1e6 * (in_price or 0) + u["output"] / 1e6 * (out_price or 0)
            month_fixed = fixed
        cost = None if variable is None else variable + month_fixed
        users = []
        for owner, uu in u["users"].items():
            share = uu["total"] / u["total"] if u["total"] else 0.0
            if cost is None:
                user_cost = None
            elif model == "per_token":
                user_cost = (
                    uu["input"] / 1e6 * (settings.get("input_per_million") or 0)
                    + uu["output"] / 1e6 * (settings.get("output_per_million") or 0)
                    + month_fixed * share
                )
            else:
                user_cost = cost * share
            users.append(
                {"user": owner, "total": uu["total"], "share": share, "cost": user_cost, "cost_jpy": _jpy(user_cost)}
            )
        users.sort(key=lambda x: (-x["total"], x["user"]))
        result.append(
            {
                "month": month,
                "total": u["total"],
                "hours": hours,
                "cost": cost,
                "cost_jpy": _jpy(cost),
                "cost_per_million": None if cost is None or not u["total"] else cost / u["total"] * 1e6,
                "users": users,
            }
        )
    known = [m["cost"] for m in result if m["cost"] is not None]
    return {
        "pricing_model": model,
        "months": result,
        "total_cost": sum(known) if known else None,
        "total_cost_jpy": _jpy(sum(known)) if known else None,
    }
