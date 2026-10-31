"""app._persist_token_usage（トークン累計の即時保存）の回帰テスト。

累計はターン完了時のスナップショットでしか保存していなかったため、生成中に
切断されたスレッドを再開すると 0 から数え直しになっていた（2026-10-08）。
"""

import pytest

import app
from src import thread_store


class _FakeUserSession:
    def __init__(self, values: dict):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.mark.asyncio
async def test_persists_token_totals_without_touching_other_metadata(tmp_path, monkeypatch) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        await thread_store.save_thread(conn, "t1", owner="alice", metadata={"work_dir": "C:/w", "plan_approved": True})
        monkeypatch.setattr(app, "_thread_store_conn", conn)
        monkeypatch.setattr(
            app.cl,
            "user_session",
            _FakeUserSession(
                {
                    "thread_id": "t1",
                    "token_usage_cumulative": {"input": 90, "output": 10, "total": 100},
                    "token_usage_cumulative_main": {"input": 45, "output": 5, "total": 50},
                }
            ),
        )
        await app._persist_token_usage()
        detail = await thread_store.get_thread_detail(conn, "t1")
        meta = detail["metadata"]
        assert meta["token_usage_cumulative"]["total"] == 100
        assert meta["token_usage_cumulative_main"]["total"] == 50
        # 他のキー（ターン完了時スナップショット・作業ディレクトリ等）は消さない
        assert meta["work_dir"] == "C:/w"
        assert meta["plan_approved"] is True
        assert detail["userIdentifier"] == "alice"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_save_failure_does_not_break_the_turn(monkeypatch) -> None:
    async def _boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(app, "_thread_store_conn", object())
    monkeypatch.setattr(app.thread_store, "save_thread", _boom)
    monkeypatch.setattr(app.cl, "user_session", _FakeUserSession({"thread_id": "t1"}))
    # 例外を外へ出さない（進行中のターンを止めない）
    await app._persist_token_usage()


class _MutableUserSession(_FakeUserSession):
    def set(self, key, value):
        self._values[key] = value


@pytest.mark.asyncio
async def test_compaction_usage_is_added_to_conversation_total_only(monkeypatch) -> None:
    """圧縮処理の呼び出しは会話累計に加算し、圧縮発火判定用のメイン累計には加算しない。"""
    session = _MutableUserSession(
        {
            "thread_id": "t1",
            "token_usage_cumulative": {"input": 90, "output": 10, "total": 100},
            "token_usage_cumulative_main": {"input": 45, "output": 5, "total": 50},
        }
    )
    monkeypatch.setattr(app.cl, "user_session", session)
    persisted, sent = [], []

    async def _fake_persist():
        persisted.append(dict(session.get("token_usage_cumulative")))

    async def _fake_send(call_usage):
        sent.append(call_usage)

    monkeypatch.setattr(app, "_persist_token_usage", _fake_persist)
    monkeypatch.setattr(app, "_send_token_usage", _fake_send)

    await app._on_compaction_usage("summary", "main", {"input_tokens": 500, "output_tokens": 50, "total_tokens": 550})

    assert session.get("token_usage_cumulative") == {"input": 590, "output": 60, "total": 650}
    assert session.get("token_usage_cumulative_main")["total"] == 50
    assert persisted == [{"input": 590, "output": 60, "total": 650}]
    assert sent == [{"compaction": {"label": "圧縮処理（直近1回）", "input": 500, "output": 50, "total": 550}}]


@pytest.mark.asyncio
async def test_compaction_usage_outside_chainlit_context_is_ignored(monkeypatch) -> None:
    class _NoContext:
        def get(self, *args, **kwargs):
            raise app.ChainlitContextException()

    monkeypatch.setattr(app.cl, "user_session", _NoContext())
    await app._on_compaction_usage("summary", "main", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})


@pytest.mark.asyncio
async def test_listener_failure_does_not_break_compaction(monkeypatch) -> None:
    from types import SimpleNamespace

    from src import context_compaction

    async def _boom(kind, role, usage):
        raise RuntimeError("listener failed")

    monkeypatch.setattr(context_compaction, "_usage_listener", _boom)
    response = SimpleNamespace(usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})
    # 例外を外へ出さない（呼び出し元の except に捕まると要約自体が失敗扱いになる）
    await context_compaction._record_compaction_usage("summary", "main", response)
