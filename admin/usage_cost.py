"""月別のトークン使用量（全ユーザー合計・ユーザー別）と、手入力した AWS 利用料金からの金額計算。

設定ダッシュボードの上部タブ「利用量・料金」（全インスタンス）と、インスタンス画面の
「利用量・料金」サブタブ用。

- 使用量: 各インスタンスのスレッドDB（[thread_store] db）の token_usage_monthly
  （本体が LLM 呼び出しごとに加算する台帳。src/thread_store.py 参照）を読み取り専用で読む。
  本体を更新後まだ一度も起動していない（台帳が無い）DBでは、スレッドの累計を
  最終更新月へ振り分けた概算を返す（source="threads_estimate"）。
  埋め込み（/v1/embeddings。ナレッジの検索・取り込み）は同じDBの embedding_usage_monthly
  （src/embedding_usage.py）から読み、生成とは別の項目（emb_tokens・emb_calls）に持つ。
- 料金設定: 全インスタンス共通の <instances_dir>/aws_cost.json（このモジュールだけが読み書きする）。
  AWS の料金は次の3つの形態から選んで手入力する。
    monthly_bill: 月ごとの請求額（AWS 請求書・Cost Explorer の実績値）をそのまま入れる。
    hourly:       EC2 等の時間課金。時間単価 × 稼働時間（月ごとに上書き可、既定はその月の全時間）。
    per_token:    Amazon Bedrock のオンデマンド等のトークン課金。入力・出力の100万トークン単価。
  hourly / per_token には、EBS・データ転送等の月額固定費（fixed_monthly）を足せる。
- 按分: 請求額・時間課金・固定費は全インスタンスで1つの金額とし、インスタンス別・ユーザー別には
  その月の全インスタンス合計トークンに占める割合で按分する。per_token の従量分だけは、
  それぞれの入力・出力トークン×単価で求める（slice_cost）。
- 埋め込み: 単価が生成よりずっと安いため、埋め込みの100万トークン単価（embedding_per_million）・
  月ごとの請求額のうち埋め込みの分（emb_bill）で別に金額を出し、埋め込みのトークン数の割合で按分する（month_costs）。
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
from src import embedding_usage

from . import ai_usage
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
    # 埋め込みの100万トークン単価（入力のみ）。month_costs 参照。
    "embedding_per_million": None,
    # 埋め込みの金額が決まらない月（単価も「うち埋め込み」も無い）に、埋め込みのトークンも按分の割合に含めるか。
    "embedding_in_share": False,
    # "YYYY-MM" -> {"bill": 請求額, "hours": 稼働時間, "emb_bill": 請求額のうち埋め込みの分}（どれも省略可）
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
    """生成（チャット）の台帳 token_usage_monthly の全行。表が無ければ None。"""
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
    """月別・ユーザー別のトークン使用量（生成と埋め込み）。

    生成は token_usage_monthly、埋め込みは embedding_usage_monthly（src/embedding_usage.py。検索と取り込み）から読む。
    ナレッジの取り込みのように特定のユーザーに結び付かない埋め込みは、owner が embedding_usage.SYSTEM_OWNER。

    Returns:
        {"source": "ledger" | "threads_estimate",
         "months": [{"month", "input", "output", "total", "calls", "emb_tokens", "emb_calls", "backfilled",
                     "users": {owner: {同じ項目}}}, ...]（古い順）,
         "users": [owner, ...]（全期間の生成＋埋め込みのトークンが多い順）}
        total 等は生成だけの値（埋め込みは emb_tokens に分けて持つ）。
    """
    with closing(monitor._connect(db_path)) as conn:  # noqa: SLF001 - 読み取り専用接続を共用する
        rows = _usage_rows_from_ledger(conn)
        source = "ledger"
        if rows is None:
            rows = _usage_rows_from_threads(conn)
            source = "threads_estimate"
        emb_rows = embedding_usage.read_rows(conn)
    return ai_usage.build_monthly_usage(rows, emb_rows, source)



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
        "embedding_per_million": _number(data.get("embedding_per_million"), "embedding_per_million"),
        "embedding_in_share": data.get("embedding_in_share") in (True, "true", "1", 1),
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
        if hours is not None and hours > hours_in_month(str(month)):
            raise UsageCostError(_t("{0} の稼働時間が1か月の時間数を超えています。", month))
        clean = {
            "bill": _number(entry.get("bill"), f"{month} bill"),
            "hours": hours,
            "emb_bill": _number(entry.get("emb_bill"), f"{month} emb_bill"),
        }
        if clean["bill"] is not None and clean["emb_bill"] is not None and clean["emb_bill"] > clean["bill"]:
            raise UsageCostError(_t("{0} の「うち埋め込み」が請求額を超えています。", month))
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

_USAGE_FIELDS = ai_usage.USAGE_FIELDS
_zero = ai_usage.zero_usage


def _add_usage(dst: dict, src: dict) -> None:
    for k in _USAGE_FIELDS:
        dst[k] += int(src.get(k, 0) or 0)


def combine_usage(items: list[dict]) -> dict:
    """インスタンスごとの monthly_usage() を、月別に全インスタンス合計へまとめる。

    Args:
        items: [{"name", "usage": monthly_usage()}, ...]（読めなかったインスタンスは usage を空にして渡す）。

    Returns:
        {"months": [{"month", USAGE_FIELDS（ai_usage）, "backfilled",
                     "instances": {name: {USAGE_FIELDS}},
                     "users": {owner: {USAGE_FIELDS}}}, ...]（古い順）,
         "instances": [name, ...]・"users": [owner, ...]（全期間の生成＋埋め込みのトークンが多い順）,
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
            instance_totals[name] += um["total"] + um.get("emb_tokens", 0)
            for owner, uu in um["users"].items():
                _add_usage(m["users"].setdefault(owner, _zero()), uu)
                user_totals[owner] = user_totals.get(owner, 0) + uu["total"] + uu.get("emb_tokens", 0)
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

    埋め込み（emb_tokens）は生成より単価がずっと安いため、生成と分けて扱う（emb_cost）。
    - monthly_bill: 請求額に埋め込みの分も含まれている前提で、そのうちの埋め込みの分を
      月ごとの「うち埋め込み」（emb_bill）、無ければ embedding_per_million × 埋め込みトークンで見積もって
      請求額から切り分ける（請求額を超えない）。
    - hourly / per_token: embedding_per_million × 埋め込みトークンを料金に足す（Bedrock の埋め込み等）。
    埋め込みの金額は埋め込みのトークン数の割合で、残りは生成のトークン数の割合で按分する（slice_cost）。
    埋め込みの金額が決まらない月（単価も emb_bill も無い）は、embedding_in_share が true なら
    埋め込みのトークンも生成と同じ重みで按分の割合に含め、false（既定）なら割合に含めない。

    Returns: month -> {"month", "input", "output", "total", "emb_tokens", "hours", "cost", "fixed", "emb_cost",
                       "emb_in_share", "weight", "cost_jpy", "cost_per_million"}
        weight は按分の分母（生成のトークン数。emb_in_share なら埋め込みのトークン数も足す）。
    """
    model = settings.get("pricing_model") or "monthly_bill"
    rate = settings.get("exchange_rate")
    fixed = settings.get("fixed_monthly") or 0.0
    emb_price = settings.get("embedding_per_million")
    entries = settings.get("months") or {}
    by_month = {m["month"]: m for m in total_months}
    result: dict[str, dict] = {}
    for month in sorted(set(by_month) | set(entries)):
        u = {**_zero(), **(by_month.get(month) or {})}
        entry = entries.get(month) or {}
        emb_tokens = u["emb_tokens"]
        emb_estimate = None if emb_price is None else emb_tokens / 1e6 * emb_price
        hours = None
        if model == "monthly_bill":
            month_fixed = 0.0
            bill = entry.get("bill")
            emb_cost = entry.get("emb_bill", emb_estimate)
            if bill is not None and emb_cost is not None:
                emb_cost = min(emb_cost, bill)
            cost = bill
        else:
            if model == "hourly":
                hours = entry.get("hours", hours_in_month(month))
                variable = None if settings.get("hourly_rate") is None else settings["hourly_rate"] * hours
            else:
                in_price, out_price = settings.get("input_per_million"), settings.get("output_per_million")
                variable = (
                    None
                    if in_price is None and out_price is None
                    else u["input"] / 1e6 * (in_price or 0) + u["output"] / 1e6 * (out_price or 0)
                )
            month_fixed = fixed
            emb_cost = emb_estimate
            cost = None if variable is None else variable + month_fixed + (emb_cost or 0.0)
        emb_in_share = emb_cost is None and bool(settings.get("embedding_in_share"))
        weight = u["total"] + (emb_tokens if emb_in_share else 0)
        all_tokens = u["total"] + emb_tokens
        result[month] = {
            "month": month,
            "input": u["input"],
            "output": u["output"],
            "total": u["total"],
            "emb_tokens": emb_tokens,
            "hours": hours,
            "cost": cost,
            "fixed": month_fixed,
            "emb_cost": emb_cost,
            "emb_in_share": emb_in_share,
            "weight": weight,
            "cost_jpy": None if cost is None or rate is None else cost * rate,
            "cost_per_million": None if cost is None or not all_tokens else cost / all_tokens * 1e6,
        }
    return result


def slice_cost(settings: dict, mc: dict, part: dict) -> dict:
    """その月の料金 mc のうち、使用量 part（インスタンス・ユーザー等の一部分）が負担する金額。

    埋め込みの金額（mc["emb_cost"]）は part の埋め込みトークンの割合で負担する。残りは、トークン課金なら
    part の入力・出力トークン×単価＋固定費の割合分、それ以外はその月の按分の分母（mc["weight"]）に占める割合で按分する。
    """
    emb_tokens = int(part.get("emb_tokens", 0) or 0)
    weight = part["total"] + (emb_tokens if mc.get("emb_in_share") else 0)
    share = weight / mc["weight"] if mc.get("weight") else 0.0
    emb_cost = mc.get("emb_cost")
    emb_part = emb_cost * emb_tokens / mc["emb_tokens"] if emb_cost and mc.get("emb_tokens") else 0.0
    cost = None
    if mc["cost"] is not None:
        if (settings.get("pricing_model") or "monthly_bill") == "per_token":
            cost = (
                part["input"] / 1e6 * (settings.get("input_per_million") or 0)
                + part["output"] / 1e6 * (settings.get("output_per_million") or 0)
                + mc["fixed"] * share
                + emb_part
            )
        else:
            cost = (mc["cost"] - (emb_cost or 0.0)) * share + emb_part
    rate = settings.get("exchange_rate")
    return {
        "total": part["total"],
        "emb_tokens": emb_tokens,
        "share": share,
        "cost": cost,
        "cost_jpy": None if cost is None or rate is None else cost * rate,
    }


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
        instance_costs_ = {n: slice_cost(settings, mc, iu) for n, iu in u["instances"].items()}
        user_costs = {owner: slice_cost(settings, mc, uu) for owner, uu in u["users"].items()}
        for owner, sc in user_costs.items():
            agg = users.setdefault(owner, {"user": owner, "total": 0, "emb_tokens": 0, "costs": [], "jpy": []})
            agg["total"] += sc["total"]
            agg["emb_tokens"] += sc["emb_tokens"]
            agg["costs"].append(sc["cost"])
            agg["jpy"].append(sc["cost_jpy"])
        months.append(
            {
                **mc,
                "calls": u["calls"],
                "emb_calls": u.get("emb_calls", 0),
                "backfilled": u["backfilled"],
                "instances": instance_costs_,
                "users": user_costs,
                "unallocated": _unallocated(mc, instance_costs_),
            }
        )
    user_list = [
        {
            "user": a["user"],
            "total": a["total"],
            "emb_tokens": a["emb_tokens"],
            "cost": _sum_optional(a["costs"]),
            "cost_jpy": _sum_optional(a["jpy"]),
            "instances": combined["user_instances"].get(a["user"], []),
        }
        for a in users.values()
    ]
    user_list.sort(key=lambda x: (-(x["total"] + x["emb_tokens"]), x["user"]))
    return {
        "pricing_model": settings.get("pricing_model") or "monthly_bill",
        "months": months,
        "instances_by_usage": combined["instances"],
        "users": user_list,
        "total_cost": _sum_optional(m["cost"] for m in months),
        "total_cost_jpy": _sum_optional(m["cost_jpy"] for m in months),
        "unallocated_cost": _sum_optional(m["unallocated"] for m in months),
    }


def _unallocated(mc: dict, parts: dict[str, dict]) -> float | None:
    """その月の料金のうち、どのインスタンスにも按分されなかった金額（使用量0の月の請求額・固定費等）。

    按分の割合はトークン数なので、全体のトークンが0の月は誰にも割り振れない。
    合計と内訳の差を画面で「未配分」として見せるために返す。金額を出せない月は None。
    """
    if mc["cost"] is None:
        return None
    rest = mc["cost"] - sum(p["cost"] or 0.0 for p in parts.values())
    return rest if abs(rest) > 1e-9 else 0.0


def instance_costs(instance_usage: dict, all_items: list[dict], settings: dict) -> dict:
    """1インスタンスの月ごとの負担額（全インスタンス共通の料金からの按分）と、その中のユーザー別内訳。

    Args:
        instance_usage: そのインスタンスの monthly_usage()。
        all_items: 按分の分母にする全インスタンスの [{"name", "usage"}, ...]。このインスタンスを
            必ず含めること（含めないと割合が 100% を超えうる）。
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
            own_tokens = um["total"] + um.get("emb_tokens", 0)
            share = (uu["total"] + uu.get("emb_tokens", 0)) / own_tokens if own_tokens else 0.0
            users.append({"user": owner, **sc, "share": share})
        users.sort(key=lambda x: (-(x["total"] + x["emb_tokens"]), x["user"]))
        all_tokens = um["total"] + um.get("emb_tokens", 0)
        months.append(
            {
                "month": um["month"],
                "total": um["total"],
                "emb_tokens": um.get("emb_tokens", 0),
                "share_of_all": own["share"],
                "cost": own["cost"],
                "cost_jpy": own["cost_jpy"],
                "cost_per_million": None if own["cost"] is None or not all_tokens else own["cost"] / all_tokens * 1e6,
                "users": users,
            }
        )
    return {
        "pricing_model": settings.get("pricing_model") or "monthly_bill",
        "months": months,
        "total_cost": _sum_optional(m["cost"] for m in months),
        "total_cost_jpy": _sum_optional(m["cost_jpy"] for m in months),
    }
