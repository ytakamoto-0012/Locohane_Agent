"""スレッド再開（サイドバーの会話をクリックして開く）・その後のセッション
切断が、updated_at を書き換えてしまわないことの回帰テスト。

背景（2026-08-19 ユーザー報告）: 生成中でなくても会話履歴をクリックすると
（＝別スレッドへ切り替えるたびに）一覧の並び順が入れ替わるという報告が
あった。list_threads_summary は updated_at DESC でソートするため、
「開く」という読み取り専用のはずの操作がどこかで書き込みを起こしている
ことになる。

resume_thread() 自体（→ app.on_chat_resume()）は書き込みを行わない
（test_resuming_a_thread_does_not_change_its_sort_position で確認）。
真犯人は disconnect: goToThread() は window.location.href によるハード
ナビゲーションのため、スレッドを切り替えるたびに直前のセッションが
切断される。chainlit.socket.disconnect() は
`if session.thread_id and session.has_first_interaction:
 persist_user_session(thread_id, session.to_persistable())` を無条件で
実行し、persist_user_session() は data_layer.update_thread(thread_id,
metadata=...) を呼ぶ。ChatThreadDataLayer.update_thread → save_thread は
呼ばれるたびに無条件で updated_at を今の時刻に更新するため、「会話を見て
別のスレッドへ移動しただけ」でその直前に見ていたスレッドが一覧の先頭に
ジャンプしてしまう（手動の再現スクリプトで複数回確認済み。ただし
disconnect() 内の非同期タスクのスケジューリングに依存するタイミングが
絡み、pytest内で安定して再現させるテストコードを書くのは不安定だった
ため、ここでは「パッチ適用後は並び順が変わらないこと」
（test_patch_prevents_disconnect_from_bumping_thread_order）のみを
回帰テストとして残す）。

session.to_persistable() が持つ情報（chat_settings/chat_profile/
client_type + cl.user_session の中身）は、本アプリでは on_message が
ターン完了ごとに thread_store へ明示的にスナップショットする値
（work_dir/plan/token使用量。on_chat_resume 参照）と完全に重複しており、
このアプリ自身はこの汎用persist機構に一切依存していない。そのため
app._patch_chainlit_disable_disconnect_thread_touch（app.py）で
persist_user_session を no-op化し、実際の会話進行が無い限り
updated_at が動かないようにする。
"""

import chainlit.data as cl_data
import chainlit.socket as cl_socket
import pytest
from chainlit.context import ChainlitContext, context_var
from chainlit.session import WebsocketSession
from chainlit.user import User

import app
from src import thread_store


@pytest.mark.asyncio
async def test_resuming_a_thread_does_not_change_its_sort_position(tmp_path, monkeypatch) -> None:
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        # 2スレッド作る。t1 が先(古い) / t2 が後(新しい) = 一覧では t2, t1 の順。
        await thread_store.save_thread(conn, "t1", owner="anonymous", name="first")
        await thread_store.save_thread(conn, "t2", owner="anonymous", name="second")
        items_before, _ = await thread_store.list_threads_summary(conn, "anonymous", limit=10)
        assert [i["id"] for i in items_before] == ["t2", "t1"]

        layer = thread_store.ChatThreadDataLayer(conn, tmp_path / "elements")
        monkeypatch.setattr(app, "_thread_data_layer", layer)
        monkeypatch.setattr(app, "_thread_store_conn", conn)
        monkeypatch.setattr(cl_data, "_data_layer", layer)
        monkeypatch.setattr(cl_data, "_data_layer_initialized", True)
        # グラフ構築（LLMクライアント等）はこのテストの関心事ではないため無効化する。
        async def _noop_rebuild_graph(thread_id, **kwargs):
            return None

        monkeypatch.setattr(app, "_rebuild_graph", _noop_rebuild_graph)
        monkeypatch.setattr(app, "_setup", _noop_setup)

        session = WebsocketSession(
            id="resume-test-session",
            socket_id="resume-test-sid",
            emit=_noop_emit,
            emit_call=_noop_emit,
            client_type="webapp",
            user_env={},
            user=None,
            token=None,
            chat_profile=None,
            thread_id="t1",  # 古い方(=一覧の下側)を開く
            environ={},
        )
        # 匿名モード用パッチ（app._patch_chainlit_anonymous_resume）相当。
        session.user = User(identifier=thread_store.ANONYMOUS_OWNER)
        context_var.set(ChainlitContext(session))

        thread = await cl_socket.resume_thread(session)
        assert thread is not None, "resume_thread が None を返した（前提が崩れている）"

        await app.on_chat_resume(thread)

        items_after, _ = await thread_store.list_threads_summary(conn, "anonymous", limit=10)
        assert [i["id"] for i in items_after] == ["t2", "t1"], (
            "t1 を開いただけで並び順が変わった（updated_at が書き換わっている）"
        )
    finally:
        await conn.close()


async def _resume_and_disconnect(conn, thread_id: str, socket_id: str) -> None:
    """resume_thread() → disconnect() を、実際のブラウザナビゲーション
    （goToThread のハードリロード）と同じ順序で実行するヘルパー。
    """
    session = WebsocketSession(
        id=f"resume-test-session-{socket_id}",
        socket_id=socket_id,
        emit=_noop_emit,
        emit_call=_noop_emit,
        client_type="webapp",
        user_env={},
        user=None,
        token=None,
        chat_profile=None,
        thread_id=thread_id,
        environ={},
    )
    session.user = User(identifier=thread_store.ANONYMOUS_OWNER)
    context_var.set(ChainlitContext(session))

    thread = await cl_socket.resume_thread(session)
    assert thread is not None
    # connection_successful()（chainlit/socket.py）が resume_thread() 成功後に
    # 行う処理そのもの（このテストは on_message 等のsocketイベント配線を
    # 経由せず直接呼んでいるため、ここで明示的に再現する）。
    session.has_first_interaction = True
    await app.on_chat_resume(thread)

    await cl_socket.disconnect(socket_id)


@pytest.mark.asyncio
async def test_patch_prevents_disconnect_from_bumping_thread_order(tmp_path, monkeypatch) -> None:
    """_patch_chainlit_disable_disconnect_thread_touch 適用後は、開いて離れる
    だけでは並び順が変わらないことの確認（修正の効果そのもの）。
    """
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        await thread_store.save_thread(conn, "t1", owner="anonymous", name="first")
        await thread_store.save_thread(conn, "t2", owner="anonymous", name="second")

        layer = thread_store.ChatThreadDataLayer(conn, tmp_path / "elements")
        monkeypatch.setattr(app, "_thread_data_layer", layer)
        monkeypatch.setattr(app, "_thread_store_conn", conn)
        monkeypatch.setattr(cl_data, "_data_layer", layer)
        monkeypatch.setattr(cl_data, "_data_layer_initialized", True)
        async def _noop_rebuild_graph(thread_id, **kwargs):
            return None

        monkeypatch.setattr(app, "_rebuild_graph", _noop_rebuild_graph)
        monkeypatch.setattr(app, "_setup", _noop_setup)

        app._patch_chainlit_disable_disconnect_thread_touch()
        try:
            await _resume_and_disconnect(conn, "t1", "disconnect-test-sid-2")

            items_after, _ = await thread_store.list_threads_summary(conn, "anonymous", limit=10)
            assert [i["id"] for i in items_after] == ["t2", "t1"]
        finally:
            cl_socket.persist_user_session = _original_persist_user_session
    finally:
        await conn.close()


_original_persist_user_session = cl_socket.persist_user_session


async def _noop_setup() -> None:
    return None


async def _noop_emit(*args, **kwargs):
    return None
