"""UIボタン（作業フォルダ選択・Plan Modeバッジ）クリックが、会話履歴に
"pick_work_dir" 等という名前のスレッドを作ってしまう不具合の回帰テスト。

背景（2026-08-19 ユーザー報告）: Chainlit本体の /project/action ハンドラは、
action_callback を呼ぶ前に無条件で session.has_first_interaction を True にし、
ChainlitEmitter.init_thread(action.name) を呼ぶ。これはチャット入力欄への
最初の発言を検知する仕組みだが、UIボタン全般にも同じ判定が働いてしまうため、
一度もチャットしていなくても "pick_work_dir" という名前の会話が左サイドバーに
現れてしまっていた（app.py の _patch_chainlit_ignore_ui_action_first_interaction
参照）。
"""

import chainlit.data as cl_data
import pytest
from chainlit.context import ChainlitContext, context_var
from chainlit.session import WebsocketSession

import app
from src import thread_store


def _activate_fake_session() -> WebsocketSession:
    session = WebsocketSession(
        id="test-session-ui-action",
        socket_id="test-sid-ui-action",
        emit=lambda *a: None,
        emit_call=lambda *a: None,
        client_type="webapp",
        user_env={},
        user=None,
        token=None,
        chat_profile=None,
        thread_id="t1",
        environ={},
    )
    context_var.set(ChainlitContext(session))
    return session


@pytest.mark.asyncio
async def test_ui_action_click_resets_first_interaction_instead_of_naming_thread() -> None:
    import chainlit.emitter as cl_emitter

    original = cl_emitter.ChainlitEmitter.flush_thread_queues
    app._patch_chainlit_ignore_ui_action_first_interaction()
    try:
        session = _activate_fake_session()
        # server.py の call_action() が action_callback 呼び出し前に立てる状態を模倣する。
        session.has_first_interaction = True
        emitter = cl_emitter.ChainlitEmitter(session)

        await emitter.flush_thread_queues("pick_work_dir")

        # 「まだ最初の発言をしていない」状態に復元される
        # （実際の最初のチャットメッセージが来るまでスレッド名は確定しない）。
        assert session.has_first_interaction is False
    finally:
        cl_emitter.ChainlitEmitter.flush_thread_queues = original


@pytest.mark.asyncio
async def test_ui_action_click_does_not_flush_queued_steps() -> None:
    """作業フォルダ選択等のクリックでは、on_chat_start でキュー済みの
    ステップ（welcomeメッセージ等）が実際の会話開始前に永続化されないこと。
    """
    import chainlit.emitter as cl_emitter

    original = cl_emitter.ChainlitEmitter.flush_thread_queues
    app._patch_chainlit_ignore_ui_action_first_interaction()
    try:
        session = _activate_fake_session()
        session.has_first_interaction = True

        flushed = []

        async def fake_flush_method_queue():
            flushed.append(True)

        session.flush_method_queue = fake_flush_method_queue

        emitter = cl_emitter.ChainlitEmitter(session)
        await emitter.flush_thread_queues("toggle_plan_mode")

        assert flushed == []
    finally:
        cl_emitter.ChainlitEmitter.flush_thread_queues = original


@pytest.mark.asyncio
async def test_real_chat_interaction_is_unaffected_by_the_patch(monkeypatch) -> None:
    """実際のチャット本文（action名と一致しない文字列）については、従来通りの
    flush_thread_queues の挙動（has_first_interactionを変更しない）を通す。
    """
    import chainlit.emitter as cl_emitter

    original = cl_emitter.ChainlitEmitter.flush_thread_queues
    app._patch_chainlit_ignore_ui_action_first_interaction()
    # データレイヤー未登録相当にする（chainlit/emitter.py の
    # get_data_layer はこのモジュール自身の名前空間にimport済みの名前なので、
    # chainlit.data.get_data_layer ではなく chainlit.emitter.get_data_layer を
    # 差し替える必要がある）。
    monkeypatch.setattr(cl_emitter, "get_data_layer", lambda: None)
    try:
        session = _activate_fake_session()
        session.has_first_interaction = True

        emitter = cl_emitter.ChainlitEmitter(session)
        # データレイヤー未登録環境では元の flush_thread_queues は何もしない
        # （chainlit/emitter.py: `if data_layer := get_data_layer():` の内側でしか
        # has_first_interaction 等に触れないため、この状態のまま変化が無いことが
        # 「パッチがreal messageの経路をそのまま素通りさせている」ことの確認になる）。
        await emitter.flush_thread_queues("こんにちは、覚えておいてください")

        assert session.has_first_interaction is True
    finally:
        cl_emitter.ChainlitEmitter.flush_thread_queues = original


@pytest.mark.asyncio
async def test_real_first_message_still_names_the_thread_with_real_data_layer(tmp_path, monkeypatch) -> None:
    """パッチ適用後も、実際のチャット本文による通常の命名（実データレイヤー越し）
    が壊れていないことのエンドツーエンド確認（2026-08-19 ユーザー報告:
    UIボタンの不具合修正後、新規会話が「無題」のまま変わらなくなった疑い）。
    """
    import chainlit.emitter as cl_emitter

    original = cl_emitter.ChainlitEmitter.flush_thread_queues
    app._patch_chainlit_ignore_ui_action_first_interaction()
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        layer = thread_store.ChatThreadDataLayer(conn, tmp_path / "elements")
        monkeypatch.setattr(cl_data, "_data_layer", layer)
        monkeypatch.setattr(cl_data, "_data_layer_initialized", True)

        session = _activate_fake_session()
        session.has_first_interaction = True  # process_message() が呼ぶ直前の状態

        emitter = cl_emitter.ChainlitEmitter(session)
        await emitter.flush_thread_queues("annual_schedule.xlsx を作って")
        # flush_thread_queues 内の data_layer.update_thread は
        # asyncio.create_task(...) で即実行ではなく非同期に発火するため、
        # 完了まで1周待つ。
        import asyncio

        await asyncio.sleep(0)

        detail = await thread_store.get_thread_detail(conn, "t1")
        assert detail is not None
        assert detail["name"] == "annual_schedule.xlsx を作って"
    finally:
        cl_emitter.ChainlitEmitter.flush_thread_queues = original
        await conn.close()


@pytest.mark.asyncio
async def test_ui_action_click_then_real_first_message_still_names_thread(tmp_path, monkeypatch) -> None:
    """実際に多い操作順序（送信前に作業フォルダアイコンをクリックしてから、
    最初のメッセージを送る）を、chainlit.emitter.ChainlitEmitter.process_message
    （client_messageソケットイベントの実処理そのもの）まで通して再現する。

    UIボタンのクリックで has_first_interaction が一旦 False に戻された後、
    実際の最初のメッセージ送信で正しく再度 True になり、スレッド名がその
    メッセージ本文で確定することを確認する（2026-08-19 ユーザー報告:
    UIボタンの不具合修正後、新規会話が「無題」のまま変わらなくなった疑い
    への回帰テスト）。
    """
    import asyncio
    import uuid

    import chainlit.emitter as cl_emitter

    original = cl_emitter.ChainlitEmitter.flush_thread_queues
    app._patch_chainlit_ignore_ui_action_first_interaction()
    conn = await thread_store.init_db(tmp_path / "chat_threads.sqlite")
    try:
        layer = thread_store.ChatThreadDataLayer(conn, tmp_path / "elements")
        monkeypatch.setattr(cl_data, "_data_layer", layer)
        monkeypatch.setattr(cl_data, "_data_layer_initialized", True)

        session = _activate_fake_session()
        emitter = cl_emitter.ChainlitEmitter(session)

        # 1) 送信前に作業フォルダアイコンをクリック（server.py call_action() が
        #    action_callback 呼び出し前に立てる has_first_interaction=True を模倣）。
        session.has_first_interaction = True
        await emitter.flush_thread_queues("pick_work_dir")
        assert session.has_first_interaction is False

        # 2) 実際の最初のメッセージを送信する
        #    （chainlit.socket.process_message が行うのと同じ呼び出し）。
        payload = {
            "message": {
                "id": str(uuid.uuid4()),
                "threadId": "t1",
                "name": "あなた",
                "type": "user_message",
                "output": "annual_schedule.xlsx を作って",
                "createdAt": "2026-08-19T00:00:00",
                "metadata": {},
            },
            "fileReferences": None,
        }
        await emitter.process_message(payload)
        # process_message 内の message._create()/init_thread() は
        # asyncio.create_task による非同期実行（かつ内部でaiosqlite経由の
        # 複数回のDB往復を伴う）のため、pure sleep(0)の数回yieldでは
        # 完了を待ちきれないことがある。実時間で待つ。
        await asyncio.sleep(0.1)

        detail = await thread_store.get_thread_detail(conn, "t1")
        assert detail is not None
        assert detail["name"] == "annual_schedule.xlsx を作って"
    finally:
        cl_emitter.ChainlitEmitter.flush_thread_queues = original
        await conn.close()
