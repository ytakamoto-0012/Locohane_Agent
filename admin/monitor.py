"""インスタンスの稼働状況・会話・トークン推移・ログ・LLM接続先の閲覧（読み取り専用）。

設定ダッシュボードの「モニター」画面用。本体プロセス（app.py）とは通信せず、
そのインスタンスの実効設定（config.ini＋config_overrides.json）で解決した
データファイルを直接読む。

- 接続中セッション・生成中スレッド: <common_data_dir>/runtime_status.json
  （本体が数秒おきに書き出す。src/runtime_status.py 参照）
- スレッド一覧・会話内容: [thread_store] db（chat_threads.sqlite）
- トークン推移: [log] dir の app_*.log に本体が LLM 呼び出しごとに出す
  「トークン使用量 thread_id=...」行
- LLM接続先の状態: [llm] main_url/sub_url の各接続先へ問い合わせる
"""

from __future__ import annotations

import json
from contextlib import closing
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from admin.i18n import t as _t
from src.config import load_config
from src.runtime_status import RUNTIME_STATUS_FILENAME

from . import instances as inst

# 本体がUI制御用に送る（会話内容ではない）メッセージの先頭文字列（app.py の *_PREFIX）。
_CONTROL_PREFIXES = (
    "🔢 トークン使用量\n",
    "📁 作業ディレクトリ",
    "🚀 定型文\n",
    "📏 表示件数上限\n",
    "🧰 サイドパネル表示件数上限\n",
    "📝 入力上限\n",
    "📋 実行計画\n",
    "⏸ 中断\n",
)
_MESSAGE_TYPES = ("user_message", "assistant_message", "system_message")
# 詳細表示で追加するStep種別（ツール実行・思考・サブエージェント）。
_INTERNAL_TYPES = ("tool", "llm")
# 1フィールドあたりの返却上限（巨大なツール出力でブラウザが固まらないように）。
MAX_FIELD_CHARS = 20000

_LOG_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \[thread=([^\]]*)\] (\w+) (\S+): (.*)$")
# アプリログの書式（app.py・src/context_compaction.py）。v0.3 までの日本語の行も、
# ローテーションで消えるまでは読めるよう両方を受け付ける。
_TOKEN_USAGE_LABELS = ("token usage", "トークン使用量")
_COMPACTION_DONE_LABELS = ("context compacted", "コンテキスト圧縮を実行しました")
_TOKEN_RE = re.compile(
    r"(?:token usage|トークン使用量) thread_id=(\S+) "
    r"call\(in=(\d+),out=(\d+),total=(\d+)\) "
    r"turn\(in=(\d+),out=(\d+),total=(\d+)\) "
    r"cumulative\(in=(\d+),out=(\d+),total=(\d+)\) "
    r"cumulative_main\(in=(\d+),out=(\d+),total=(\d+)\)"
)
# src/context_compaction.py の _log_compaction_usage が出す行。
_COMPACTION_USAGE_RE = re.compile(
    r"(?:compaction token usage|圧縮処理トークン使用量) kind=(\S+) role=(\S+) call\(in=(\d+),out=(\d+),total=(\d+)\)"
)
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class MonitorError(Exception):
    """設定が読めない等で、モニター情報を取得できない。"""


def instance_config(instances_root: Path, config_ini_path: Path, name: str):
    try:
        return load_config(
            config_path=config_ini_path, overrides_path=inst.overrides_path(instances_root, name), instance_name=name
        )
    except Exception as exc:  # noqa: BLE001
        raise MonitorError(_t("インスタンス {0!r} の設定を読み込めません: {1}", name, exc)) from exc


# ---------------------------------------------------------------------------
# 接続中セッション・生成中スレッド
# ---------------------------------------------------------------------------


def read_runtime_status(common_data_dir: Path) -> dict | None:
    """runtime_status.json を読む。無い・壊れている場合は None。"""
    path = common_data_dir / RUNTIME_STATUS_FILENAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_live_runtime_status(common_data_dir: Path, *, is_live: bool) -> dict | None:
    """稼働中（is_live）のインスタンスについてだけ runtime_status.json を読む。

    停止中・異常終了なら前回プロセスの残骸ファイルがあっても読まない。稼働中なら
    本体が起動直後の最初の収集で必ず上書きする（src/runtime_status.py）ため、
    古い内容が見えるのは起動後の数秒だけ。

    ファイル内の pid を管理ツールが起動した子プロセスの pid と照合してはいけない
    （venv の python.exe はランチャーで、本物のインタプリタを別プロセスとして
    起動するため一致しない。照合していた初版では稼働中でも常に「取得できません」
    になっていた）。
    """
    return read_runtime_status(common_data_dir) if is_live else None


def summarize_runtime(status: dict | None, thread_names: dict[str, str | None]) -> dict:
    """runtime_status.json の内容を画面表示用に整形する。

    Args:
        status: read_runtime_status() の結果（停止中・古いファイルなら呼び出し側で None にする）。
        thread_names: thread_id -> スレッド名（thread_store から引いたもの）。
    """
    if status is None:
        return {"available": False, "users": [], "sessions": [], "generating": [], "updated_at": None}
    sessions = [{**s, "thread_name": thread_names.get(s.get("thread_id"))} for s in status.get("sessions", [])]
    generating = [{**g, "thread_name": thread_names.get(g.get("thread_id"))} for g in status.get("generating", [])]
    users: dict[str, dict] = {}
    for s in sessions:
        u = users.setdefault(s["user"], {"user": s["user"], "sessions": 0, "generating": 0})
        u["sessions"] += 1
    for g in generating:
        owner = g.get("owner") or _t("（不明）")
        u = users.setdefault(owner, {"user": owner, "sessions": 0, "generating": 0})
        u["generating"] += 1
    return {
        "available": True,
        "users": sorted(users.values(), key=lambda u: u["user"]),
        "sessions": sessions,
        "generating": generating,
        "updated_at": status.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# スレッド・会話内容（chat_threads.sqlite）
# ---------------------------------------------------------------------------


def _sqlite_ro_uri(db_path: Path) -> str:
    """SQLite に読み取り専用で開かせる file: URI を返す。

    Path.as_uri() は UNC パス（\\\\server\\share\\...）を file://server/share/... と
    ホスト名を authority に置いた形にするが、SQLite は authority に localhost
    以外を許さず「invalid uri authority」で開けない。authority を空にして
    パス側に //server/share/... を入れる file:////server/share/... の形にする。
    """
    posix = db_path.as_posix()
    prefix = "file://" if posix.startswith("/") else "file:///"
    return f"{prefix}{quote(posix, safe='/:')}?mode=ro"


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise MonitorError(_t("スレッドDBがありません: {0}（[thread_store] enabled=false、または未使用）", db_path))
    # 本体が書き込み中のDBを読むため、読み取り専用で開き、ロック待ちも許す。
    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


_TOKEN_TOTAL_SQL = "COALESCE(json_extract(metadata_json, '$.token_usage_cumulative.total'), 0)"


def thread_names(db_path: Path, thread_ids: list[str]) -> dict[str, str | None]:
    ids = [t for t in thread_ids if t]
    if not ids or not db_path.is_file():
        return {}
    with closing(_connect(db_path)) as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT id, name FROM threads WHERE id IN ({placeholders})", ids).fetchall()
    return {r["id"]: r["name"] for r in rows}


def list_threads(
    db_path: Path, *, owner: str | None = None, query: str | None = None, limit: int = 50, offset: int = 0
) -> dict:
    where, params = [], []
    if owner:
        where.append("owner = ?")
        params.append(owner)
    if query:
        # "_" や "%" を含む検索語がワイルドカードとして働かないようエスケープする。
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", query) + "%"
        where.append("(name LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')")
        params += [pattern, pattern]
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with closing(_connect(db_path)) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM threads {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, owner, name, created_at, updated_at, {_TOKEN_TOTAL_SQL} AS tokens_total, "
            "(SELECT COUNT(*) FROM steps s WHERE s.thread_id = threads.id "
            " AND json_extract(s.data_json, '$.type') = 'user_message') AS user_messages "
            f"FROM threads {where_sql} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"total": total, "threads": [dict(r) for r in rows]}


def user_summary(db_path: Path) -> list[dict]:
    """ユーザー（スレッド所有者）ごとのスレッド数・トークン累計・最終利用日時。"""
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT owner AS user, COUNT(*) AS threads, SUM({_TOKEN_TOTAL_SQL}) AS tokens_total, "
            "MAX(updated_at) AS last_active FROM threads GROUP BY owner ORDER BY last_active DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def _clip(text: object) -> tuple[str, bool]:
    s = "" if text is None else str(text)
    if len(s) > MAX_FIELD_CHARS:
        return s[:MAX_FIELD_CHARS], True
    return s, False


def thread_detail(db_path: Path, thread_id: str, *, include_internal: bool = False) -> dict | None:
    """スレッドの会話内容。include_internal=False ならユーザー発言とAIの応答のみ。"""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT id, owner, name, created_at, updated_at, metadata_json FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            return None
        step_rows = conn.execute(
            "SELECT data_json FROM steps WHERE thread_id = ? ORDER BY created_at", (thread_id,)
        ).fetchall()
    steps = []
    for (data_json,) in step_rows:
        try:
            step = json.loads(data_json)
        except ValueError:
            continue
        step_type = step.get("type")
        output = step.get("output") or ""
        is_control = step_type in _MESSAGE_TYPES and str(output).startswith(_CONTROL_PREFIXES)
        if step_type in _MESSAGE_TYPES and not is_control:
            pass
        elif include_internal and (step_type in _INTERNAL_TYPES or is_control):
            pass
        else:
            continue
        out, out_trunc = _clip(output)
        inp, inp_trunc = _clip(step.get("input") if step_type in _INTERNAL_TYPES else "")
        steps.append(
            {
                "id": step.get("id"),
                "type": step_type,
                "name": step.get("name"),
                "created_at": step.get("createdAt"),
                "input": inp,
                "output": out,
                "truncated": out_trunc or inp_trunc,
                "is_control": is_control,
            }
        )
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except ValueError:
        metadata = {}
    return {
        "id": row["id"],
        "owner": row["owner"],
        "name": row["name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "token_usage_cumulative": metadata.get("token_usage_cumulative"),
        "token_usage_cumulative_main": metadata.get("token_usage_cumulative_main"),
        "work_dir": metadata.get("work_dir"),
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# アプリログ（[log] dir の app_*.log）
# ---------------------------------------------------------------------------


def _log_files(log_dir: Path) -> list[Path]:
    """app_<起動日時>.log を古い順に返す（ファイル名の日時順＝時系列順）。"""
    if not log_dir.is_dir():
        return []
    return sorted(log_dir.glob("app_*.log"))


def _read_lines(path: Path):
    """ログファイルの行を返す。一覧取得後に消えた（本体の retention_days による
    削除等）・読めないファイルは空として扱う（1ファイルの失敗で画面全体を
    エラーにしないため）。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            yield from f
    except OSError:
        return


def _is_main_call(prev_main_logged: int | None, main_logged: int, call_total: int) -> bool:
    """「トークン使用量」行がメインエージェントの呼び出しか（サブエージェント内部なら False）。

    ログの cumulative_main が呼び出し分だけ増えていればメイン。圧縮・再開で
    カウンタが 0 に戻った直後は main_logged == call_total になる。
    """
    return prev_main_logged is None or main_logged in (prev_main_logged + call_total, call_total)


def _local_log_ts(iso: str | None) -> str | None:
    """runtime_status.json の UTC 時刻を、アプリログと同じローカル時刻の "YYYY-MM-DDTHH:MM:SS" へ。"""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def live_context_series(log_dir: Path, generating: list[dict]) -> list[dict]:
    """生成中スレッドごとに、今回の生成開始以降の LLM リクエストの入力トークンを返す。

    インスタンス一覧のカードのグラフ用。各点の value は「そのスレッドで今
    処理しているコンテキストのうち最大のもの」で、メインの直近の入力と、
    直近のメイン呼び出し以降のサブエージェント（並列委譲を含む）・圧縮処理の
    入力の最大値をとる。ログ行にはサブエージェントの識別子が無いため、
    メインが再び呼ばれた時点でサブ側はすべて終わったものとして捨てる。

    5秒おきに呼ばれるため、生成開始より前に更新が止まったログファイルは読まない。

    Args:
        generating: summarize_runtime() の generating（thread_id・started_at を使う）。
    """
    starts = {g["thread_id"]: _local_log_ts(g.get("started_at")) for g in generating if g.get("thread_id")}
    starts = {tid: ts for tid, ts in starts.items() if ts}
    if not starts:
        return []
    cutoff = datetime.fromisoformat(min(starts.values())).timestamp()
    state = {tid: {"prev_main_logged": None, "main_in": None, "sub_max": None, "points": []} for tid in starts}
    paths = _log_files(log_dir)
    for i, path in enumerate(paths):
        # 書き込み中の最新ファイルは mtime によらず必ず読む。
        if i < len(paths) - 1:
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
        for line in _read_lines(path):
            if not any(label in line for label in _TOKEN_USAGE_LABELS):
                continue
            head = _LOG_LINE_RE.match(line.rstrip("\n"))
            if not head:
                continue
            ts = head.group(1).replace(" ", "T")
            # メッセージ先頭で照合する（DEBUG ログに出た会話内容等に同じ文字列が含まれても拾わない）。
            message = head.group(5)
            m = _TOKEN_RE.match(message)
            if m:
                tid = m.group(1)
                st = state.get(tid)
                if st is None:
                    continue
                v = [int(x) for x in m.groups()[1:]]
                call_in, call_total, main_logged = v[0], v[2], v[11]
                # 生成開始より前の行も、メイン／サブの判定（直前の cumulative_main）のために読む。
                kind = "main" if _is_main_call(st["prev_main_logged"], main_logged, call_total) else "sub"
                st["prev_main_logged"] = main_logged
            else:
                m = _COMPACTION_USAGE_RE.match(message)
                tid = head.group(2)
                st = state.get(tid)
                if not m or st is None:
                    continue
                kind, call_in = "compaction", int(m.group(3))
            if ts < starts[tid]:
                continue
            if kind == "main":
                st["main_in"], st["sub_max"] = call_in, None
            else:
                st["sub_max"] = max(st["sub_max"] or 0, call_in)
            value = max(st["main_in"] or 0, st["sub_max"] or 0)
            st["points"].append({"ts": ts, "kind": kind, "call_in": call_in, "value": value})
    return [
        {"thread_id": tid, "started_at": starts[tid], "points": st["points"]} for tid, st in state.items()
    ]


def token_history(log_dir: Path, thread_id: str) -> list[dict]:
    """そのスレッドの LLM 呼び出しごとのトークン使用量を時系列で返す。

    累計（cumulative_total / cumulative_main_total）は、本体がログに出す
    cumulative(...) の値ではなく、ここで各呼び出しの total を足し上げて作る。
    本体のセッション内カウンタは、生成中に切断されたスレッドを再開すると
    0 に戻り（ターン完了時にしかスレッドへ保存されないため）、
    cumulative_main はコンテキスト圧縮のたびに 0 に戻る（圧縮の発火判定用）ため、
    そのまま描くと累計なのに途中で下がってしまう。

    kind:
        "main" / "sub": 会話本体の LLM 呼び出し（app.py の「トークン使用量」行）。
            ログの cumulative_main が呼び出し分だけ増えていればメイン、
            増えていなければサブエージェント内部の呼び出し。
        "compaction": 圧縮処理自身の呼び出し（要約・write_thread_note の強制実行。
            src/context_compaction.py の「圧縮処理トークン使用量」行）。
            累計にだけ加算し、メイン累計には含めない。
    compacted: この点の直前でメインエージェントのコンテキスト圧縮が実行された
        （app.py の「コンテキスト圧縮を実行しました」行）。
    """
    token_needles = tuple(f"{label} thread_id={thread_id} " for label in _TOKEN_USAGE_LABELS)
    compaction_done_needles = tuple(f"{label} thread_id={thread_id} " for label in _COMPACTION_DONE_LABELS)
    points: list[dict] = []
    prev_main_logged: int | None = None
    cum_all = 0
    cum_main = 0
    pending_compaction = False
    for path in _log_files(log_dir):
        for line in _read_lines(path):
            if thread_id not in line:
                continue
            head = _LOG_LINE_RE.match(line.rstrip("\n"))
            if not head:
                continue
            # 各行はメッセージ先頭で照合する（DEBUG ログに出た会話内容等に同じ文字列が
            # 含まれていても、偽の呼び出し・圧縮として拾わない）。
            message = head.group(5)
            if message.startswith(compaction_done_needles):
                pending_compaction = True
                continue
            ts = head.group(1).replace(" ", "T")
            if message.startswith(token_needles):
                m = _TOKEN_RE.match(message)
                if not m or m.group(1) != thread_id:
                    continue
                v = [int(x) for x in m.groups()[1:]]
                call_total, main_logged = v[2], v[11]
                kind = "main" if _is_main_call(prev_main_logged, main_logged, call_total) else "sub"
                prev_main_logged = main_logged
                call_in, call_out, turn_total = v[0], v[1], v[5]
            elif head.group(2) == thread_id:
                m = _COMPACTION_USAGE_RE.match(message)
                if not m:
                    continue
                kind = "compaction"
                call_in, call_out, call_total = int(m.group(3)), int(m.group(4)), int(m.group(5))
                turn_total = None
            else:
                continue
            cum_all += call_total
            if kind == "main":
                cum_main += call_total
            points.append(
                {
                    "ts": ts,
                    "kind": kind,
                    "is_sub": kind == "sub",
                    "call_in": call_in,
                    "call_out": call_out,
                    "call_total": call_total,
                    "turn_total": turn_total,
                    "cumulative_total": cum_all,
                    "cumulative_main_total": cum_main,
                    "compacted": pending_compaction,
                }
            )
            pending_compaction = False
    return points


def _parse_log_entries(path: Path) -> list[dict]:
    """1ファイル分のログを、複数行のエントリ（トレースバック等）をまとめた形で返す。"""
    entries: list[dict] = []
    for line in _read_lines(path):
        line = line.rstrip("\n")
        m = _LOG_LINE_RE.match(line)
        if m:
            entries.append(
                {
                    "ts": m.group(1).replace(" ", "T"),
                    "thread_id": None if m.group(2) == "-" else m.group(2),
                    "level": m.group(3),
                    "logger": m.group(4),
                    "message": m.group(5),
                    "file": path.name,
                }
            )
        elif entries:
            entries[-1]["message"] += "\n" + line
    return entries


def tail_log(
    log_dir: Path, *, min_level: str = "WARNING", limit: int = 200, query: str | None = None, thread_id: str | None = None
) -> list[dict]:
    """新しい順に、min_level 以上のログエントリを最大 limit 件返す。"""
    threshold = LOG_LEVELS.index(min_level) if min_level in LOG_LEVELS else 0
    result: list[dict] = []
    for path in reversed(_log_files(log_dir)):
        for entry in reversed(_parse_log_entries(path)):
            level = entry["level"]
            if level in LOG_LEVELS and LOG_LEVELS.index(level) < threshold:
                continue
            if thread_id and entry["thread_id"] != thread_id:
                continue
            if query and query not in entry["message"]:
                continue
            entry["message"], _ = _clip(entry["message"])
            result.append(entry)
            if len(result) >= limit:
                return result
    return result


def recent_level_counts(log_dir: Path, hours: int = 24) -> dict[str, int]:
    """直近 hours 時間の WARNING/ERROR/CRITICAL 件数。"""
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    cutoff = time.time() - hours * 3600
    counts = {"WARNING": 0, "ERROR": 0, "CRITICAL": 0}
    for path in _log_files(log_dir):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        for entry in _parse_log_entries(path):
            if entry["level"] in counts and entry["ts"] >= since:
                counts[entry["level"]] += 1
    return counts


# ---------------------------------------------------------------------------
# LLM接続先
# ---------------------------------------------------------------------------


@dataclass
class EndpointProbe:
    role: str
    base_url: str
    model: str
    provider: str
    reachable: bool
    slots_busy: int | None = None
    slots_total: int | None = None
    error: str | None = None
    latency_ms: int | None = None


def _server_root(base_url: str) -> str:
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def probe_endpoints(cfg, timeout_seconds: float = 2.0) -> list[dict]:
    """[llm] の各接続先へ問い合わせ、到達可否と（llama.cpp なら）スロット使用状況を返す。

    api_key は返さない。llama_cpp は GET /slots（src/llm/routing.py の
    _probe_llama_cpp_slots_available と同じ判定）、それ以外は GET <base_url>/models。
    """
    targets: list[tuple[str, object]] = [("main", ep) for ep in cfg.main_endpoints]
    if not cfg.sub_endpoints_inherit_main:
        targets += [("sub", ep) for ep in cfg.sub_endpoints]
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=min(2.0, timeout_seconds))) as client:
        for role, ep in targets:
            key = (ep.base_url, ep.model)
            if key in seen:
                continue
            seen.add(key)
            probe = EndpointProbe(role=role, base_url=ep.base_url, model=ep.model, provider=ep.provider, reachable=False)
            started = time.perf_counter()
            try:
                if ep.provider == "llama_cpp":
                    resp = client.get(f"{_server_root(ep.base_url)}/slots")
                    if resp.is_error:
                        # --no-slots で起動したサーバーは /slots が 501 になるが、サーバー自体は
                        # 動いている。到達可否は /health で判断する（スロット数は不明のまま）。
                        slots_status = resp.status_code
                        client.get(f"{_server_root(ep.base_url)}/health").raise_for_status()
                        probe.error = _t("GET /slots が HTTP {0} のためスロット使用状況は不明", slots_status)
                    else:
                        slots = resp.json()
                        if isinstance(slots, list):
                            probe.slots_total = len(slots)
                            probe.slots_busy = sum(1 for s in slots if isinstance(s, dict) and s.get("is_processing"))
                else:
                    resp = client.get(f"{ep.base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {ep.api_key}"})
                    resp.raise_for_status()
                probe.reachable = True
            except (httpx.HTTPError, ValueError) as exc:
                probe.error = f"{type(exc).__name__}: {exc}"[:300]
            probe.latency_ms = int((time.perf_counter() - started) * 1000)
            results.append(probe.__dict__)
    return results
