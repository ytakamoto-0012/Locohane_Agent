"""src/thread_store.py（会話スレッド一覧・再開用の軽量ストア）の回帰テスト。"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from chainlit.context import ChainlitContext, context_var
from chainlit.session import WebsocketSession
from chainlit.user import User

from src import chat_log, thread_store


def _activate_fake_session(*, has_first_interaction: bool) -> WebsocketSession:
    """create_step等（@queue_until_user_message()でガードされる）のテスト用に、
    実際のChainlitリクエスト文脈を模倣する。本番ではこれらのメソッドは常に
    アクティブなセッション文脈内からしか呼ばれない。
    """
    session = WebsocketSession(
        id="test-session",
        socket_id="test-sid",
        emit=lambda *a: None,
        emit_call=lambda *a: None,
        client_type="webapp",
        user_env={},
        user=None,
        token=None,
        chat_profile=None,
        thread_id="unused",
        environ={},
    )
    session.has_first_interaction = has_first_interaction
    context_var.set(ChainlitContext(session))
    return session


@pytest.mark.asyncio
async def test_init_db_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "chat_threads.sqlite"
    conn1 = await thread_store.init_db(db_path)
    await conn1.close()
    # 2回目の呼び出し（既存ファイルに対する再初期化）でもエラーにならない。
    conn2 = await thread_store.init_db(db_path)
    await conn2.close()


@pytest.mark.asyncio
async def test_save_thread_upsert_and_metadata_merge(tmp_path) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        # 新規作成
        await thread_store.save_thread(conn, "t1", owner="anonymous", metadata={"work_dir": "/a"})
        detail = await thread_store.get_thread_detail(conn, "t1")
        assert detail is not None
        assert detail["metadata"] == {"work_dir": "/a"}
        assert detail["name"] is None

        # 既存行への name 更新 + metadata の統合（置き換えではなくmerge）
        await thread_store.save_thread(conn, "t1", name="Hello", metadata={"plan": [1, 2]})
        detail = await thread_store.get_thread_detail(conn, "t1")
        assert detail["name"] == "Hello"
        assert detail["metadata"] == {"work_dir": "/a", "plan": [1, 2]}

        # name=None を渡しても既存のnameは消えない（COALESCE）
        await thread_store.save_thread(conn, "t1", metadata={"work_dir": "/b"})
        detail = await thread_store.get_thread_detail(conn, "t1")
        assert detail["name"] == "Hello"
        assert detail["metadata"]["work_dir"] == "/b"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_save_thread_and_upsert_thread_stub_race_safely_on_new_thread(tmp_path) -> None:
    """新規スレッドの最初のメッセージでは、create_step（upsert_thread_stub）と
    Chainlit本体のinit_thread()（→save_thread、スレッド命名）がほぼ同時に
    呼ばれる（chainlit/emitter.py process_message が両方を asyncio.create_task
    で同時起動するため）。以前の実装は「SELECTして行が無ければINSERT」という
    非原子的な分岐だったため、両者がほぼ同時に呼ばれると片方のINSERTが
    `UNIQUE constraint failed: threads.id` で失敗し、スレッド名が永久に
    「無題の会話」のまま確定しなくなる不具合があった（2026-08-19 ユーザー報告、
    tests/test_ui_action_thread_naming.py で実際の呼び出し経路からも再現済み）。
    ここでは thread_store の2関数だけを使い、決定的に競合させて確認する。
    """
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        await asyncio.gather(
            thread_store.upsert_thread_stub(conn, "t1", "anonymous"),
            thread_store.save_thread(conn, "t1", owner="anonymous", name="hello world"),
        )
        detail = await thread_store.get_thread_detail(conn, "t1")
        assert detail is not None
        assert detail["name"] == "hello world"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_concurrent_save_thread_does_not_lose_metadata_keys(tmp_path) -> None:
    """save_thread のメタデータ統合（SELECT→update→UPDATE）が並行呼び出しで
    後勝ちにならないこと。トークン累計を LLM 呼び出しごとに保存するようにした
    （app.py _persist_token_usage）ことで plan 等の即時保存と並行しやすくなった。
    ロック無しでは20並行で1キーしか残らなかった（2026-10-08 再現）。
    """
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        await thread_store.save_thread(conn, "t1", owner="anonymous")
        await asyncio.gather(*(thread_store.save_thread(conn, "t1", metadata={f"k{i}": i}) for i in range(20)))
        meta = (await thread_store.get_thread_detail(conn, "t1"))["metadata"]
        assert meta == {f"k{i}": i for i in range(20)}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_get_thread_detail_id_always_matches_requested_id(tmp_path) -> None:
    """@chainlit/react-client の resume_thread ハンドラは thread.id が要求した
    thread_id と異なると window.location.href='/thread/<id>' へハードナビゲート
    する（このSPAには存在しないルート）ため、常に引数のIDを返すことの回帰テスト。
    """
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        await thread_store.save_thread(conn, "abc-123", owner="anonymous", name="x")
        detail = await thread_store.get_thread_detail(conn, "abc-123")
        assert detail["id"] == "abc-123"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_steps_are_ordered_by_created_at(tmp_path) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        await thread_store.save_thread(conn, "t1", owner="anonymous")
        await thread_store.upsert_step_row(
            conn, {"id": "s3", "threadId": "t1", "createdAt": "2024-01-01T00:00:03"}
        )
        await thread_store.upsert_step_row(
            conn, {"id": "s1", "threadId": "t1", "createdAt": "2024-01-01T00:00:01"}
        )
        await thread_store.upsert_step_row(
            conn, {"id": "s2", "threadId": "t1", "createdAt": "2024-01-01T00:00:02"}
        )
        detail = await thread_store.get_thread_detail(conn, "t1")
        assert [s["id"] for s in detail["steps"]] == ["s1", "s2", "s3"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_delete_thread_cascades_to_steps(tmp_path) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        await thread_store.save_thread(conn, "t1", owner="anonymous")
        await thread_store.upsert_step_row(conn, {"id": "s1", "threadId": "t1", "createdAt": "2024-01-01"})
        await thread_store.delete_thread_row(conn, "t1")
        assert await thread_store.get_thread_detail(conn, "t1") is None
        cursor = await conn.execute("SELECT COUNT(*) FROM steps WHERE thread_id = ?", ("t1",))
        row = await cursor.fetchone()
        assert row[0] == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_list_threads_summary_isolates_owners_and_sorts_desc(tmp_path) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        await thread_store.save_thread(conn, "a1", owner="anonymous", name="first")
        await thread_store.save_thread(conn, "a2", owner="anonymous", name="second")
        await thread_store.save_thread(conn, "b1", owner="bob", name="bobs thread")

        items, cursor = await thread_store.list_threads_summary(conn, "anonymous", limit=30)
        assert [i["id"] for i in items] == ["a2", "a1"]  # 更新が新しい順
        assert cursor is None

        items, _ = await thread_store.list_threads_summary(conn, "bob", limit=30)
        assert [i["id"] for i in items] == ["b1"]
    finally:
        await conn.close()


def test_resolve_owner_matches_chat_log_username_resolution() -> None:
    assert thread_store.resolve_owner(None) == chat_log.resolve_log_username(None) == "anonymous"
    user = User(identifier="alice/bob")
    assert thread_store.resolve_owner(user) == chat_log.resolve_log_username("alice/bob")


@pytest.mark.asyncio
async def test_get_or_create_user_round_trip(tmp_path) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        assert await thread_store.get_user_row(conn, "alice") is None
        created = await thread_store.create_user_row(conn, User(identifier="alice"))
        assert created.identifier == "alice"
        fetched = await thread_store.get_user_row(conn, "alice")
        assert fetched is not None
        assert fetched.id == created.id == "alice"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_cleanup_old_threads_noop_when_retention_non_positive(tmp_path) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        await thread_store.save_thread(conn, "t1", owner="anonymous")
        deleted = await thread_store.cleanup_old_threads(conn, 0)
        assert deleted == 0
        assert await thread_store.get_thread_detail(conn, "t1") is not None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_cleanup_old_threads_deletes_expired_and_cascades(tmp_path) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        await thread_store.save_thread(conn, "old", owner="anonymous")
        await thread_store.upsert_step_row(conn, {"id": "s1", "threadId": "old", "createdAt": "2024-01-01"})
        await thread_store.save_thread(conn, "recent", owner="anonymous")

        old_iso = "2000-01-01T00:00:00+00:00"
        await conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (old_iso, "old"))
        await conn.commit()

        deleted = await thread_store.cleanup_old_threads(conn, retention_days=7)
        assert deleted == 1
        assert await thread_store.get_thread_detail(conn, "old") is None
        assert await thread_store.get_thread_detail(conn, "recent") is not None
        cursor = await conn.execute("SELECT COUNT(*) FROM steps WHERE thread_id = ?", ("old",))
        row = await cursor.fetchone()
        assert row[0] == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_data_layer_create_step_creates_thread_stub_and_no_op_stubs_are_safe(tmp_path) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        dl = thread_store.ChatThreadDataLayer(conn, tmp_path / "elements")
        _activate_fake_session(has_first_interaction=True)

        # create_step より前に update_thread が走らなくても、FK制約を満たす
        # スレッド行が自動的に用意されること（Chainlit側の実行順序は保証されない）。
        await dl.create_step({"id": "s1", "threadId": "th1", "createdAt": "2024-01-01T00:00:00"})
        thread = await dl.get_thread("th1")
        assert thread is not None
        assert len(thread["steps"]) == 1

        await dl.update_thread("th1", name="first message", user_id=None)
        thread = await dl.get_thread("th1")
        assert thread["name"] == "first message"

        author = await dl.get_thread_author("th1")
        assert author == "anonymous"
        with pytest.raises(ValueError):
            await dl.get_thread_author("missing")

        # 添付ファイル・フィードバック系はv1では安全なno-opであること。
        assert await dl.get_element("th1", "e1") is None
        assert await dl.create_element(None) is None
        assert await dl.delete_element("e1") is None
        assert await dl.get_favorite_steps("anonymous") == []
        assert await dl.delete_feedback("x") is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_create_step_before_first_user_message_is_not_persisted(tmp_path) -> None:
    """新規タブを開いただけ（ユーザーが1文字も送信していない）状態で on_chat_start が
    送るウェルカムメッセージ等が create_step 経由で即座に永続化されてしまい、
    「新規チャット」を押すたびに無題のスレッドがサイドバーへ溜まり続けるバグの
    回帰テスト（2026-08-19 実機で確認）。

    `chainlit.data.base.BaseDataLayer` は create_step 等を
    `@queue_until_user_message()` で装飾した**抽象**メソッドとして定義しているが、
    このデコレータは抽象メソッド自体にしか付いておらず、オーバーライドした
    サブクラス（ChatThreadDataLayer）の実装には自動的には引き継がれない。
    ChatThreadDataLayer側で明示的に再度デコレートし直すことで、Chainlit本体が
    意図する「最初のユーザー発言まで永続化を保留する」動作を回復している。
    """
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        dl = thread_store.ChatThreadDataLayer(conn, tmp_path / "elements")
        _activate_fake_session(has_first_interaction=False)

        # on_chat_start 相当（ユーザーはまだ何も送信していない）。
        await dl.create_step({"id": "s1", "threadId": "th1", "createdAt": "2024-01-01T00:00:00"})
        assert await dl.get_thread("th1") is None

        cursor = await conn.execute("SELECT COUNT(*) FROM threads")
        row = await cursor.fetchone()
        assert row[0] == 0

        # ユーザーが最初のメッセージを送信した後（has_first_interaction=True）は
        # 即座に永続化される。
        session = _activate_fake_session(has_first_interaction=True)
        await dl.create_step({"id": "s2", "threadId": "th2", "createdAt": "2024-01-01T00:00:01"})
        assert await dl.get_thread("th2") is not None
        assert session.has_first_interaction is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_create_step_skips_ephemeral_progress_steps(tmp_path) -> None:
    """src/tools.py の _push_background_job_progress/_push_dispatch_agent_progress
    が送る「実行中です（経過N秒・job_id=xxx）。」等の進捗pushは
    metadata={"ephemeral_progress": True} を持つ。これが実際に永続化を
    スキップされ、スレッド再開時に古い進捗表示が復元されないことの回帰テスト
    （2026-08-21 ユーザー報告）。通常のステップ（サブエージェントの実際の発言等）
    は引き続き永続化されること、既にスレッド行が存在する場合はスキップされても
    エラーにならないことも確認する。
    """
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        dl = thread_store.ChatThreadDataLayer(conn, tmp_path / "elements")
        _activate_fake_session(has_first_interaction=True)

        await dl.create_step({"id": "s1", "threadId": "th1", "createdAt": "2024-01-01T00:00:00"})
        await dl.create_step(
            {
                "id": "s2",
                "threadId": "th1",
                "createdAt": "2024-01-01T00:00:01",
                "output": "実行中です（経過 20 秒・job_id=abc）。",
                "metadata": {"ephemeral_progress": True},
            }
        )
        thread = await dl.get_thread("th1")
        assert [s["id"] for s in thread["steps"]] == ["s1"]

        # 未作成のスレッドに対して進捗pushだけが最初に来ても、
        # スレッド行自体を勝手に作らない（FKスタブより先に弾く）。
        await dl.create_step(
            {
                "id": "s3",
                "threadId": "th2",
                "createdAt": "2024-01-01T00:00:00",
                "metadata": {"ephemeral_progress": True},
            }
        )
        assert await dl.get_thread("th2") is None
    finally:
        await conn.close()
