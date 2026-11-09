"""設定ダッシュボードの軽量化のためのキャッシュ（admin/memories.py・admin/monitor.py）が、ファイルの変更に追随すること。"""

import os
import sqlite3

from admin import memories as memories_api
from admin import monitor
from src import memory as mem_store


def test_memory_summaries_follow_file_changes(tmp_path):
    root = tmp_path / "memory"
    mem_store.create_memory(root, "alpha", "first", "user", "body")
    first = memories_api._memory_summaries(root)
    assert [m["name"] for m in first] == ["alpha"]
    # 変化していなければキャッシュした要約をそのまま返す。
    assert memories_api._memory_summaries(root)[0] is first[0]

    mem_store.create_memory(root, "beta", "second", "feedback", "body")
    mem_store.create_memory(root, "gamma", "third", "user", "body", owner="user_a", visibility="private")
    names = {(m["name"], m["owner"]) for m in memories_api._memory_summaries(root)}
    assert names == {("alpha", None), ("beta", None), ("gamma", "user_a")}

    mem_store.delete_memory(root, "alpha")
    assert {m["name"] for m in memories_api._memory_summaries(root)} == {"beta", "gamma"}


def test_memory_summaries_same_as_all_memories(tmp_path):
    root = tmp_path / "memory"
    mem_store.create_memory(root, "alpha", "first", "user", "body")
    mem_store.create_memory(root, "gamma", "third", "project", "body", owner="user_a", visibility="shared")
    expected = [(m.name, m.owner, m.memory_type, m.visibility) for m in mem_store.all_memories(root)]
    actual = [(m["name"], m["owner"], m["type"], m["visibility"]) for m in memories_api._memory_summaries(root)]
    assert actual == expected


def test_parse_log_entries_cache_refreshes_on_change_and_is_not_polluted_by_tail(tmp_path):
    log = tmp_path / "app_1.log"
    log.write_text("2026-11-10 10:00:00,000 [thread=-] WARNING src.x: " + "a" * (monitor.MAX_FIELD_CHARS + 10) + "\n", encoding="utf-8")
    result = monitor.tail_log(tmp_path, min_level="WARNING")
    assert len(result[0]["message"]) == monitor.MAX_FIELD_CHARS
    # tail_log が切り詰めても、キャッシュしたエントリの本文は元のまま。
    assert len(monitor._parse_log_entries(log)[0]["message"]) == monitor.MAX_FIELD_CHARS + 10

    with log.open("a", encoding="utf-8") as f:
        f.write("2026-11-10 10:00:01,000 [thread=-] ERROR src.x: second\n")
    assert [e["level"] for e in monitor._parse_log_entries(log)] == ["WARNING", "ERROR"]


def test_instance_config_cache_follows_config_changes(tmp_path):
    ini = tmp_path / "config.ini"
    ini.write_text("[log]\nlevel = info\n", encoding="utf-8")
    instances_root = tmp_path / "instances"
    (instances_root / "inst_a").mkdir(parents=True)
    first = monitor.instance_config(instances_root, ini, "inst_a")
    assert monitor.instance_config(instances_root, ini, "inst_a") is first

    ini.write_text("[log]\nlevel = debug\n", encoding="utf-8")
    st = ini.stat()
    os.utime(ini, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert monitor.instance_config(instances_root, ini, "inst_a") is not first


def test_list_threads_counts_user_messages_per_page(tmp_path):
    db = tmp_path / "chat_threads.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE threads (id TEXT, owner TEXT, name TEXT, created_at TEXT, updated_at TEXT, metadata_json TEXT);"
        "CREATE TABLE steps (id TEXT, thread_id TEXT, created_at TEXT, data_json TEXT);"
    )
    conn.executemany(
        "INSERT INTO threads VALUES (?,?,?,?,?,?)",
        [("t1", "u", "n1", "2026-01-01", "2026-01-02", "{}"), ("t2", "u", "n2", "2026-01-01", "2026-01-03", "{}")],
    )
    conn.executemany(
        "INSERT INTO steps VALUES (?,?,?,?)",
        [
            ("s1", "t1", "1", '{"type": "user_message"}'),
            ("s2", "t1", "2", '{"type": "user_message"}'),
            ("s3", "t1", "3", '{"type": "assistant_message"}'),
        ],
    )
    conn.commit()
    conn.close()
    result = monitor.list_threads(db)
    assert [(t["id"], t["user_messages"]) for t in result["threads"]] == [("t2", 0), ("t1", 2)]
