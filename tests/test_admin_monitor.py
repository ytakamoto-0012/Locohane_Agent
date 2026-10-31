"""admin/monitor.py（モニター画面の読み取り処理）と src/runtime_status.py のテスト。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from admin import monitor
from src import runtime_status


def _token_line(ts: str, thread_id: str, call_in: int, cum: int, cum_main: int, label: str = "token usage") -> str:
    return (
        f"{ts},123 [thread={thread_id}] INFO app.py: {label} thread_id={thread_id} "
        f"call(in={call_in},out=10,total={call_in + 10}) turn(in=1,out=1,total=2) "
        f"cumulative(in=1,out=1,total={cum}) cumulative_main(in=1,out=1,total={cum_main})\n"
    )


# 現在の英語の書式と、v0.3 までの日本語の書式（ローテーションで消えるまで残るログ）。
LOG_LABELS = [
    ("token usage", "compaction token usage", "context compacted"),
    ("トークン使用量", "圧縮処理トークン使用量", "コンテキスト圧縮を実行しました"),
]


@pytest.mark.parametrize(("token", "compaction_usage", "compaction_done"), LOG_LABELS)
def test_token_history_cumulative_never_drops_and_marks_real_compaction(
    tmp_path: Path, token: str, compaction_usage: str, compaction_done: str
):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    tid = "t-1"
    (log_dir / "app_20261001_000000.log").write_text(
        # メイン（total=110）
        _token_line("2026-10-01 00:00:01", tid, 100, 110, 110, token)
        + _token_line("2026-10-01 00:00:02", "other", 999, 999, 999, token)
        # サブエージェント（total=60）: cumulative_main が増えない
        + _token_line("2026-10-01 00:00:03", tid, 50, 170, 110, token)
        # 生成中の切断→再開で本体のカウンタが 0 に戻った後のメイン（total=40）
        + _token_line("2026-10-01 00:00:04", tid, 30, 40, 40, token),
        encoding="utf-8",
    )
    # 後のファイル。圧縮処理自身の呼び出し → 圧縮実行 → 圧縮後のメイン（total=20）
    (log_dir / "app_20261001_120000.log").write_text(
        f"2026-10-01 12:00:00,1 [thread={tid}] INFO src.context_compaction: "
        f"{compaction_usage} kind=summary role=main call(in=500,out=50,total=550)\n"
        f"2026-10-01 12:00:01,1 [thread={tid}] WARNING app.py: "
        f"{compaction_done} thread_id={tid} messages=40->6\n"
        + _token_line("2026-10-01 12:00:02", tid, 10, 20, 20, token),
        encoding="utf-8",
    )
    points = monitor.token_history(log_dir, tid)
    assert [p["kind"] for p in points] == ["main", "sub", "main", "compaction", "main"]
    assert [p["cumulative_total"] for p in points] == [110, 170, 210, 760, 780]
    # メイン累計は圧縮処理・サブを含まず、圧縮でも 0 に戻らない
    assert [p["cumulative_main_total"] for p in points] == [110, 110, 150, 150, 170]
    # 再開によるカウンタのリセットは圧縮扱いしない。実際の圧縮ログの直後の点だけ
    assert [p["compacted"] for p in points] == [False, False, False, False, True]
    assert points[0]["ts"] == "2026-10-01T00:00:01"


def test_live_context_series_takes_max_of_main_and_subs_since_generation_start(tmp_path: Path):
    from datetime import datetime

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    tid = "t-1"
    (log_dir / "app_20261001_000000.log").write_text(
        # 生成開始より前（メイン／サブ判定の基準にだけ使い、点には出さない）
        _token_line("2026-10-01 00:00:01", tid, 100, 110, 110)
        # ここから今回の生成。メイン 300
        + _token_line("2026-10-01 00:00:10", tid, 300, 420, 420)
        # 並列サブ 2 本（cumulative_main は増えない）: 500, 200
        + _token_line("2026-10-01 00:00:11", tid, 500, 930, 420)
        + _token_line("2026-10-01 00:00:12", tid, 200, 1140, 420)
        + _token_line("2026-10-01 00:00:13", "other", 999, 999, 999)
        # メインに戻るとサブの値は捨てる
        + _token_line("2026-10-01 00:00:14", tid, 350, 1500, 780)
        + f"2026-10-01 00:00:15,1 [thread={tid}] INFO src.context_compaction: "
        "圧縮処理トークン使用量 kind=summary role=main call(in=900,out=50,total=950)\n",
        encoding="utf-8",
    )
    started_utc = datetime(2026, 10, 1, 0, 0, 5).astimezone().isoformat()
    series = monitor.live_context_series(
        log_dir,
        [{"thread_id": tid, "started_at": started_utc}, {"thread_id": "no-calls", "started_at": started_utc}],
    )
    by_id = {s["thread_id"]: s for s in series}
    points = by_id[tid]["points"]
    assert [p["kind"] for p in points] == ["main", "sub", "sub", "main", "compaction"]
    assert [p["call_in"] for p in points] == [300, 500, 200, 350, 900]
    assert [p["value"] for p in points] == [300, 500, 500, 350, 900]
    assert points[0]["ts"] == "2026-10-01T00:00:10"
    assert by_id["no-calls"]["points"] == []
    assert monitor.live_context_series(log_dir, []) == []


def test_token_parsers_ignore_lookalike_text_inside_other_log_messages(tmp_path: Path):
    # DEBUG ログに出た会話内容（ユーザーが貼ったログ等）に同じ文字列が含まれても拾わない。
    import os
    from datetime import datetime

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    tid = "t-1"
    fake_token = _token_line("2026-10-01 00:00:09", tid, 99999, 1, 1).split(": ", 1)[1].rstrip("\n")
    path = log_dir / "app_20261001_000000.log"
    path.write_text(
        _token_line("2026-10-01 00:00:10", tid, 300, 310, 310)
        + f"2026-10-01 00:00:11,1 [thread={tid}] DEBUG src.llm: payload={{'content': '{fake_token}'}}\n"
        + f"2026-10-01 00:00:12,1 [thread={tid}] DEBUG src.llm: payload=圧縮処理トークン使用量 kind=summary "
        "role=main call(in=88888,out=1,total=88889)\n"
        + f"2026-10-01 00:00:13,1 [thread={tid}] DEBUG src.llm: payload=コンテキスト圧縮を実行しました thread_id={tid} x\n"
        + _token_line("2026-10-01 00:00:14", tid, 320, 640, 640),
        encoding="utf-8",
    )
    history = monitor.token_history(log_dir, tid)
    assert [p["call_in"] for p in history] == [300, 320]
    assert [p["compacted"] for p in history] == [False, False]

    # 書き込み中の最新ファイルは mtime が生成開始より古く見えても読む。
    old = datetime(2026, 9, 1).timestamp()
    os.utime(path, (old, old))
    started_utc = datetime(2026, 10, 1, 0, 0, 5).astimezone().isoformat()
    (series,) = monitor.live_context_series(log_dir, [{"thread_id": tid, "started_at": started_utc}])
    assert [p["call_in"] for p in series["points"]] == [300, 320]


def test_compaction_usage_log_format_matches_monitor_parser(caplog):
    # src/context_compaction.py が出す行を admin/monitor.py が読めること（形式の取り決め）。
    from types import SimpleNamespace

    from src import context_compaction

    usage = {"input_tokens": 500, "output_tokens": 50, "total_tokens": 550}
    with caplog.at_level("INFO", logger="src.context_compaction"):
        context_compaction._log_compaction_usage("summary", "main", SimpleNamespace(usage_metadata=usage))
        context_compaction._log_compaction_usage("summary", "main", SimpleNamespace(usage_metadata=None))
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    m = monitor._COMPACTION_USAGE_RE.search(messages[0])
    assert m and m.groups() == ("summary", "main", "500", "50", "550")


def test_tail_log_filters_level_and_joins_multiline(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "app_20261001_000000.log").write_text(
        "2026-10-01 00:00:01,1 [thread=-] INFO x: info\n"
        "2026-10-01 00:00:02,1 [thread=abc] ERROR x: boom\n"
        "Traceback (most recent call last):\n"
        "  ValueError\n"
        "2026-10-01 00:00:03,1 [thread=-] WARNING x: warn\n",
        encoding="utf-8",
    )
    entries = monitor.tail_log(log_dir, min_level="WARNING")
    assert [e["level"] for e in entries] == ["WARNING", "ERROR"]
    assert "ValueError" in entries[1]["message"]
    assert entries[1]["thread_id"] == "abc"
    assert monitor.tail_log(log_dir, min_level="INFO", thread_id="abc")[0]["message"].startswith("boom")


@pytest.fixture
def thread_db(tmp_path: Path) -> Path:
    db = tmp_path / "chat_threads.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT, created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', tags_json TEXT);"
        "CREATE TABLE steps (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, created_at TEXT, data_json TEXT NOT NULL);"
    )
    meta = json.dumps({"token_usage_cumulative": {"input": 90, "output": 10, "total": 100}})
    conn.execute("INSERT INTO threads VALUES ('t1','alice','会話1','2026-10-01T00:00:00','2026-10-01T01:00:00',?,NULL)", (meta,))
    conn.execute("INSERT INTO threads VALUES ('t2','bob','会話2','2026-10-01T00:00:00','2026-10-01T02:00:00','{}',NULL)")
    steps = [
        {"id": "s1", "type": "assistant_message", "output": "📁 作業ディレクトリ: 未設定"},
        {"id": "s2", "type": "user_message", "output": "こんにちは"},
        {"id": "s3", "type": "tool", "name": "Read", "input": "{}", "output": "x" * (monitor.MAX_FIELD_CHARS + 5)},
        {"id": "s4", "type": "assistant_message", "output": "はい"},
    ]
    for i, s in enumerate(steps):
        conn.execute("INSERT INTO steps VALUES (?, 't1', ?, ?)", (s["id"], f"2026-10-01T00:00:0{i}", json.dumps(s)))
    conn.commit()
    conn.close()
    return db


def test_thread_detail_hides_control_and_internal_steps_by_default(thread_db: Path):
    detail = monitor.thread_detail(thread_db, "t1")
    assert [s["output"] for s in detail["steps"]] == ["こんにちは", "はい"]
    assert detail["token_usage_cumulative"]["total"] == 100

    full = monitor.thread_detail(thread_db, "t1", include_internal=True)
    assert [s["id"] for s in full["steps"]] == ["s1", "s2", "s3", "s4"]
    assert full["steps"][2]["truncated"] is True
    assert monitor.thread_detail(thread_db, "missing") is None


def test_list_threads_and_user_summary(thread_db: Path):
    result = monitor.list_threads(thread_db, owner="alice")
    assert result["total"] == 1
    assert result["threads"][0]["tokens_total"] == 100
    assert result["threads"][0]["user_messages"] == 1
    assert monitor.list_threads(thread_db, query="会話2")["threads"][0]["id"] == "t2"
    summary = {u["user"]: u for u in monitor.user_summary(thread_db)}
    assert summary["alice"]["tokens_total"] == 100
    assert summary["bob"]["threads"] == 1


def test_sqlite_ro_uri_keeps_authority_empty_for_unc_path():
    # Path.as_uri() の file://server/... は SQLite が「invalid uri authority」で拒否する。
    unc = monitor._sqlite_ro_uri(Path(r"\\fileserver\share$\data\chat_threads.sqlite"))
    assert unc == "file:////fileserver/share%24/data/chat_threads.sqlite?mode=ro"
    local = monitor._sqlite_ro_uri(Path(r"C:\data\a b#c.sqlite"))
    assert local == "file:///C:/data/a%20b%23c.sqlite?mode=ro"


def test_summarize_runtime_groups_by_user():
    status = {
        "pid": 1,
        "sessions": [
            {"session_id": "a", "user": "alice", "thread_id": "t1", "has_first_interaction": True},
            {"session_id": "b", "user": "alice", "thread_id": "t9", "has_first_interaction": False},
        ],
        "generating": [{"thread_id": "t2", "owner": "bob", "started_at": None, "waiting_for_user": True}],
        "updated_at": "x",
    }
    view = monitor.summarize_runtime(status, {"t1": "会話1", "t2": "会話2"})
    assert view["users"] == [
        {"user": "alice", "sessions": 2, "generating": 0},
        {"user": "bob", "sessions": 0, "generating": 1},
    ]
    assert view["generating"][0]["thread_name"] == "会話2"
    assert monitor.summarize_runtime(None, {})["available"] is False


def test_read_live_runtime_status_ignores_pid_and_skips_stopped(tmp_path: Path):
    # 書き出し元の pid は管理ツールが Popen した pid と一致しない（venv ランチャー経由）
    # ため、pid に関係なく稼働中なら読む。
    (tmp_path / runtime_status.RUNTIME_STATUS_FILENAME).write_text(
        json.dumps({"pid": 99999, "sessions": [], "generating": []}), encoding="utf-8"
    )
    assert monitor.read_live_runtime_status(tmp_path, is_live=True)["pid"] == 99999
    assert monitor.read_live_runtime_status(tmp_path, is_live=False) is None


def test_runtime_status_writer_writes_only_on_change(tmp_path: Path):
    path = tmp_path / runtime_status.RUNTIME_STATUS_FILENAME
    snapshots = [{"sessions": [1]}, {"sessions": [1]}, {"sessions": [2]}]
    writes: list[dict] = []
    original = runtime_status.write_atomic

    def spy(p, data):
        writes.append(data)
        original(p, data)

    def collect():
        if not snapshots:
            raise asyncio.CancelledError
        return snapshots.pop(0)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await runtime_status.run_writer_loop(path, collect, 0)

    import unittest.mock

    with unittest.mock.patch.object(runtime_status, "write_atomic", spy):
        asyncio.run(run())
    assert [w["sessions"] for w in writes] == [[1], [2]]
    assert json.loads(path.read_text(encoding="utf-8"))["sessions"] == [2]
    runtime_status.remove(path)
    assert not path.exists()


def test_runtime_status_writer_retries_transient_permission_error_without_warning(tmp_path: Path, caplog):
    # Windows では管理ツールが読んでいる瞬間の os.replace が PermissionError になる。
    # 一時的な衝突は警告せず、次の周期で同じ内容を書き直す。
    path = tmp_path / runtime_status.RUNTIME_STATUS_FILENAME
    snapshots = [{"sessions": [1]}, {"sessions": [1]}]
    attempts: list[dict] = []
    original = runtime_status.write_atomic

    def flaky(p, data):
        attempts.append(data)
        if len(attempts) == 1:
            raise PermissionError(13, "Access is denied")
        original(p, data)

    def collect():
        if not snapshots:
            raise asyncio.CancelledError
        return snapshots.pop(0)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await runtime_status.run_writer_loop(path, collect, 0)

    import logging
    import unittest.mock

    with caplog.at_level(logging.DEBUG, logger="src.runtime_status"), unittest.mock.patch.object(
        runtime_status, "write_atomic", flaky
    ):
        asyncio.run(run())
    assert len(attempts) == 2
    assert json.loads(path.read_text(encoding="utf-8"))["sessions"] == [1]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_runtime_status_writer_warns_on_persistent_permission_error(tmp_path: Path, caplog):
    path = tmp_path / runtime_status.RUNTIME_STATUS_FILENAME
    snapshots = [{"sessions": [1]}] * 5

    def always_denied(p, data):
        raise PermissionError(13, "Access is denied")

    def collect():
        if not snapshots:
            raise asyncio.CancelledError
        return snapshots.pop(0)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await runtime_status.run_writer_loop(path, collect, 0)

    import logging
    import unittest.mock

    with caplog.at_level(logging.DEBUG, logger="src.runtime_status"), unittest.mock.patch.object(
        runtime_status, "write_atomic", always_denied
    ):
        asyncio.run(run())
    # 連続失敗が閾値に達した1回だけ警告する（毎周期は出さない）。
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_list_threads_query_treats_wildcards_literally(thread_db: Path):
    conn = sqlite3.connect(thread_db)
    conn.execute("INSERT INTO threads VALUES ('t3','carol','a_b','2026-10-01T00:00:00','2026-10-01T03:00:00','{}',NULL)")
    conn.execute("INSERT INTO threads VALUES ('t4','carol','axb','2026-10-01T00:00:00','2026-10-01T04:00:00','{}',NULL)")
    conn.execute("INSERT INTO threads VALUES ('t5','carol','100%達成','2026-10-01T00:00:00','2026-10-01T05:00:00','{}',NULL)")
    conn.commit()
    conn.close()
    assert [t["id"] for t in monitor.list_threads(thread_db, query="a_b")["threads"]] == ["t3"]
    assert [t["id"] for t in monitor.list_threads(thread_db, query="%")["threads"]] == ["t5"]


def test_log_readers_skip_file_deleted_after_listing(tmp_path: Path, monkeypatch):
    # 一覧取得後に本体の retention_days 削除等でファイルが消えても、画面全体をエラーにしない。
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "app_20261001_000000.log").write_text(
        "2026-10-01 00:00:01,1 [thread=t1] WARNING x: warn\n", encoding="utf-8"
    )
    gone = log_dir / "app_20261002_000000.log"
    original = monitor._log_files
    monkeypatch.setattr(monitor, "_log_files", lambda d: [*original(d), gone])
    assert [e["message"] for e in monitor.tail_log(log_dir)] == ["warn"]
    assert monitor.token_history(log_dir, "t1") == []
    assert monitor.recent_level_counts(log_dir, hours=24 * 365 * 100)["WARNING"] == 1


class _FakeLlamaServer:
    """probe_endpoints 用の httpx.MockTransport ハンドラ。"""

    def __init__(self, slots_status: int, health_status: int = 200):
        self.slots_status = slots_status
        self.health_status = health_status

    def __call__(self, request):
        import httpx

        if request.url.path == "/slots":
            if self.slots_status != 200:
                return httpx.Response(self.slots_status, json={"error": "not supported"})
            return httpx.Response(200, json=[{"is_processing": True}, {"is_processing": False}])
        if request.url.path == "/health":
            return httpx.Response(self.health_status, json={"status": "ok"})
        return httpx.Response(404)


def _probe_with(handler):
    import types
    import unittest.mock

    import httpx

    from src.config import LLMEndpoint

    ep = LLMEndpoint(base_url="http://llm.local:8080/v1", model="m", api_key="x", start=None, end=None, provider="llama_cpp")
    cfg = types.SimpleNamespace(main_endpoints=(ep,), sub_endpoints=(ep,), sub_endpoints_inherit_main=True)
    real_client = httpx.Client
    with unittest.mock.patch.object(
        monitor.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    ):
        return monitor.probe_endpoints(cfg)[0]


def test_probe_endpoints_reports_slots():
    probe = _probe_with(_FakeLlamaServer(200))
    assert probe["reachable"] is True
    assert (probe["slots_busy"], probe["slots_total"]) == (1, 2)


def test_probe_endpoints_server_without_slots_endpoint_is_still_reachable():
    # --no-slots で起動した llama-server は /slots が 501 だが、サーバー自体は動いている。
    probe = _probe_with(_FakeLlamaServer(501))
    assert probe["reachable"] is True
    assert probe["slots_total"] is None
    assert "501" in probe["error"]


def test_probe_endpoints_unhealthy_server_is_unreachable():
    probe = _probe_with(_FakeLlamaServer(501, health_status=503))
    assert probe["reachable"] is False
