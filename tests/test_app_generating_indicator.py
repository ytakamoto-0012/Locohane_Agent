"""左サイドバー「生成中インジケーター」用の _generating_thread_ids 管理の回帰テスト。

背景: 会話の生成中に別スレッドをクリックすると、フロントは完全に別セッションと
して新規接続し直すため、元スレッドのターンはドキュメント通りバックグラウンドで
継続する（on_chat_end参照）にもかかわらず、それを他のセッションから知る手段が
無く「会話が終了したように見える」というユーザー報告（2026-08-19）があった。
on_message を薄いラッパー（マーキング）＋ _on_message_impl（実処理）に分割し、
プロセス全体の一時集合 _generating_thread_ids で追跡する。

同日、追加で2点の報告があった:
- 生成中スレッドを開き直しても画面が全く更新されない（思考ステップの
  ストリーミングが見えない） → session.emit の中継（_make_relayed_emit）で対応。
- 開き直した画面から停止できない → 別セッションから対象タスクを cancel() する
  cross-session停止（_generating_thread_tasks / _stop_thread_generating）で対応。
"""

import asyncio

import pytest

import app


class _FakeUserSession:
    def __init__(self, thread_id: str | None):
        self._data = {"thread_id": thread_id}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _FakeEmitSession:
    """cl.context.session の最小限のフェイク。on_message が参照するのは
    .emit（差し替え可能な callable）・.thread_id（_make_relayed_emit が
    中継先セッションを絞り込むのに使う）・.user（所有者解決に使う。Noneなら
    thread_store.resolve_owner が "anonymous" に解決する）・.id（session_id。
    /locohane/threads/{id}/status が「自分自身のターンかどうか」を判定する
    のに使う _generating_thread_session_ids のキー）のみ。
    """

    def __init__(self, thread_id: str, user=None, session_id: str = "test-sid"):
        self.thread_id = thread_id
        self.user = user
        self.id = session_id
        self.emit = lambda event, data: None


class _FakeContext:
    def __init__(self, session):
        self.session = session


def test_mark_and_unmark_are_idempotent() -> None:
    app._generating_thread_ids.clear()
    app._generating_owner_threads.clear()
    app._mark_thread_generating("t1")
    app._mark_thread_generating("t1")  # 二重マークしても壊れない
    assert app._generating_thread_ids == {"t1"}

    app._unmark_thread_generating("t1")
    app._unmark_thread_generating("t1")  # 未登録に対する解除もエラーにならない
    assert app._generating_thread_ids == set()


@pytest.mark.asyncio
async def test_on_message_marks_during_impl_and_unmarks_on_success(monkeypatch) -> None:
    app._generating_thread_ids.clear()
    app._generating_owner_threads.clear()
    monkeypatch.setattr(app.cl, "user_session", _FakeUserSession("t1"))
    monkeypatch.setattr(app.cl, "context", _FakeContext(_FakeEmitSession("t1")))

    seen_during_impl = {}

    async def fake_impl(message):
        seen_during_impl["marked"] = "t1" in app._generating_thread_ids

    monkeypatch.setattr(app, "_on_message_impl", fake_impl)

    await app.on_message(object())

    assert seen_during_impl["marked"] is True
    assert "t1" not in app._generating_thread_ids


@pytest.mark.asyncio
async def test_on_message_unmarks_even_when_impl_raises(monkeypatch) -> None:
    app._generating_thread_ids.clear()
    app._generating_owner_threads.clear()
    monkeypatch.setattr(app.cl, "user_session", _FakeUserSession("t1"))
    monkeypatch.setattr(app.cl, "context", _FakeContext(_FakeEmitSession("t1")))

    async def failing_impl(message):
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "_on_message_impl", failing_impl)

    with pytest.raises(RuntimeError):
        await app.on_message(object())

    assert "t1" not in app._generating_thread_ids


@pytest.mark.asyncio
async def test_on_message_rejects_when_owner_has_other_thread_generating(monkeypatch) -> None:
    """所有者（匿名モードでは"anonymous"に一元化）が別スレッドで既に生成中なら、
    新規チャット・他の会話履歴からの送信は実行せず拒否する（並列タスク禁止。
    2026-08-19 ユーザー要望: LangGraphのcheckpointerは1thread_idにつき1回の
    会話進行を前提としており、同じ所有者が複数スレッドで同時にLLM呼び出しを
    行うことを想定していない）。
    """
    app._generating_thread_ids.clear()
    app._generating_owner_threads.clear()
    app._generating_thread_tasks.clear()
    app._generating_owner_threads["anonymous"] = "other-thread"

    monkeypatch.setattr(app.cl, "user_session", _FakeUserSession("t1"))
    monkeypatch.setattr(app.cl, "context", _FakeContext(_FakeEmitSession("t1")))

    sent_messages = []

    class _FakeMessage:
        def __init__(self, content=None, type=None):
            sent_messages.append(content)

        async def send(self):
            return None

    monkeypatch.setattr(app.cl, "Message", _FakeMessage)

    called = []

    async def fake_impl(message):
        called.append(True)

    monkeypatch.setattr(app, "_on_message_impl", fake_impl)

    await app.on_message(object())

    assert called == []
    assert sent_messages  # 拒否理由を伝えるメッセージを送っている
    assert "t1" not in app._generating_thread_ids
    assert "t1" not in app._generating_thread_tasks
    # 競合先（other-thread側）の記録自体は書き換えない。
    assert app._generating_owner_threads == {"anonymous": "other-thread"}


@pytest.mark.asyncio
async def test_on_message_allows_when_conflicting_entry_is_the_same_thread(monkeypatch) -> None:
    """_generating_owner_threads[owner] が自分自身の thread_id と一致する場合
    （再入等の異常系）は拒否対象にしない — 別スレッドとの競合のみを防ぐ。
    """
    app._generating_thread_ids.clear()
    app._generating_owner_threads.clear()
    app._generating_thread_tasks.clear()
    app._generating_owner_threads["anonymous"] = "t1"

    monkeypatch.setattr(app.cl, "user_session", _FakeUserSession("t1"))
    monkeypatch.setattr(app.cl, "context", _FakeContext(_FakeEmitSession("t1")))

    called = []

    async def fake_impl(message):
        called.append(True)

    monkeypatch.setattr(app, "_on_message_impl", fake_impl)

    await app.on_message(object())

    assert called == [True]


@pytest.mark.asyncio
async def test_on_message_unmarks_on_cancellation() -> None:
    """停止ボタン押下（session.current_task.cancel()）相当のCancelledErrorでも
    finallyで確実に解除されること。
    """
    app._generating_thread_ids.clear()
    app._generating_owner_threads.clear()

    class _Session:
        def get(self, key, default=None):
            return "t1" if key == "thread_id" else default

        def set(self, key, value):
            pass

    orig_session = app.cl.user_session
    orig_context = app.cl.context
    app.cl.user_session = _Session()
    app.cl.context = _FakeContext(_FakeEmitSession("t1"))
    try:

        async def cancelled_impl(message):
            raise __import__("asyncio").CancelledError()

        orig_impl = app._on_message_impl
        app._on_message_impl = cancelled_impl
        try:
            with pytest.raises(__import__("asyncio").CancelledError):
                await app.on_message(object())
        finally:
            app._on_message_impl = orig_impl
    finally:
        app.cl.user_session = orig_session
        app.cl.context = orig_context

    assert "t1" not in app._generating_thread_ids


@pytest.mark.asyncio
async def test_on_message_noop_when_thread_id_missing(monkeypatch) -> None:
    """_setup()/on_chat_start完了前に何らかの理由でthread_id未設定のまま
    呼ばれた異常系でも、マーキング処理自体が例外にならないこと。
    """
    app._generating_thread_ids.clear()
    app._generating_owner_threads.clear()
    monkeypatch.setattr(app.cl, "user_session", _FakeUserSession(None))

    called = []

    async def fake_impl(message):
        called.append(True)

    monkeypatch.setattr(app, "_on_message_impl", fake_impl)

    await app.on_message(object())

    assert called == [True]
    assert app._generating_thread_ids == set()


@pytest.mark.asyncio
async def test_on_message_registers_and_clears_generating_task(monkeypatch) -> None:
    app._generating_thread_ids.clear()
    app._generating_owner_threads.clear()
    app._generating_thread_tasks.clear()
    monkeypatch.setattr(app.cl, "user_session", _FakeUserSession("t1"))
    monkeypatch.setattr(app.cl, "context", _FakeContext(_FakeEmitSession("t1")))

    seen = {}

    async def fake_impl(message):
        task = app._generating_thread_tasks.get("t1")
        seen["registered_as_current_task"] = task is asyncio.current_task()

    monkeypatch.setattr(app, "_on_message_impl", fake_impl)

    await app.on_message(object())

    assert seen["registered_as_current_task"] is True
    assert "t1" not in app._generating_thread_tasks


@pytest.mark.asyncio
async def test_on_message_registers_own_session_id_and_clears_it(monkeypatch) -> None:
    """/locohane/threads/{id}/status が「自分自身のターンかどうか」を判定できる
    よう、on_message は cl.context.session.id を _generating_thread_session_ids
    へ登録し、終了時に確実に消す（2026-08-19 ユーザー報告の並列送信検知バグの
    根本原因: これが無いと、フロントは useChatData().loading（Plan Mode承認待ち
    等の一時的な ask 中は false になりうる）でしか自分自身のターンを判別できず、
    誤って「他セッションで生成中」と判定して window.location.reload() を
    発火させ、自分自身のターンを見失って無題の会話が増殖していた）。
    """
    app._generating_thread_ids.clear()
    app._generating_owner_threads.clear()
    app._generating_thread_tasks.clear()
    app._generating_thread_session_ids.clear()
    monkeypatch.setattr(app.cl, "user_session", _FakeUserSession("t1"))
    monkeypatch.setattr(app.cl, "context", _FakeContext(_FakeEmitSession("t1", session_id="my-session")))

    seen = {}

    async def fake_impl(message):
        seen["session_id_during_impl"] = app._generating_thread_session_ids.get("t1")

    monkeypatch.setattr(app, "_on_message_impl", fake_impl)

    await app.on_message(object())

    assert seen["session_id_during_impl"] == "my-session"
    assert "t1" not in app._generating_thread_session_ids


@pytest.mark.asyncio
async def test_on_message_wraps_session_emit_during_impl_and_restores_after(monkeypatch) -> None:
    """閲覧側セッションへ思考ステップを中継するため、ターン処理中だけ
    session.emit を _make_relayed_emit でラップし、終了後は必ず元に戻す
    （でないと次のターンや他の処理が壊れたemitを使い続けてしまう）。
    """
    app._generating_thread_ids.clear()
    app._generating_owner_threads.clear()
    fake_session = _FakeEmitSession("t1")
    original_emit = fake_session.emit
    monkeypatch.setattr(app.cl, "user_session", _FakeUserSession("t1"))
    monkeypatch.setattr(app.cl, "context", _FakeContext(fake_session))

    seen = {}

    async def fake_impl(message):
        seen["wrapped_during_impl"] = fake_session.emit is not original_emit

    monkeypatch.setattr(app, "_on_message_impl", fake_impl)

    await app.on_message(object())

    assert seen["wrapped_during_impl"] is True
    assert fake_session.emit is original_emit


@pytest.mark.asyncio
async def test_make_relayed_emit_relays_allowlisted_events_to_same_thread_only() -> None:
    """_make_relayed_emit は許可リストのイベントのみ、同じ thread_id を開いている
    他セッションへ中継する。'ask'系（session.emit_call経由）はこの関数の対象外
    のため中継されず、閲覧側で二重の承認UIが出ることはない。task_start/task_end
    も意図的に対象外（Composerのremote停止導線と競合させないため）。
    """
    from chainlit.session import ws_sessions_sid

    async def _noop_coro():
        return None

    peer_calls: list[tuple[str, dict]] = []
    original_calls: list[tuple[str, dict]] = []

    class _Peer:
        def __init__(self, thread_id: str):
            self.thread_id = thread_id

        def emit(self, event, data):
            peer_calls.append((event, data))
            return _noop_coro()

    origin = _FakeEmitSession("t1")

    def original_emit(event, data):
        original_calls.append((event, data))
        return _noop_coro()

    saved = dict(ws_sessions_sid)
    ws_sessions_sid.clear()
    ws_sessions_sid["origin-sid"] = origin
    ws_sessions_sid["same-thread-sid"] = _Peer("t1")
    ws_sessions_sid["other-thread-sid"] = _Peer("t2")
    try:
        relayed = app._make_relayed_emit(origin, "t1", original_emit)
        await relayed("stream_token", {"id": "s1", "token": "hi"})
        await relayed("task_start", {})  # 許可リスト外
        await asyncio.sleep(0)  # asyncio.create_task で発火した中継を進める
    finally:
        ws_sessions_sid.clear()
        ws_sessions_sid.update(saved)

    # original_emit は許可リストに関わらず常に呼ばれる（通常の配信は妨げない）。
    assert original_calls == [("stream_token", {"id": "s1", "token": "hi"}), ("task_start", {})]
    # 中継は許可リストのイベントかつ同じ thread_id のセッションにのみ届く。
    assert peer_calls == [("stream_token", {"id": "s1", "token": "hi"})]


@pytest.mark.asyncio
async def test_stop_thread_generating_returns_false_when_no_task() -> None:
    app._generating_thread_tasks.clear()
    assert await app._stop_thread_generating("missing") is False


@pytest.mark.asyncio
async def test_stop_thread_generating_returns_false_when_task_already_done() -> None:
    app._generating_thread_tasks.clear()

    async def already_done():
        return None

    task = asyncio.create_task(already_done())
    await task
    app._generating_thread_tasks["t1"] = task
    try:
        assert await app._stop_thread_generating("t1") is False
    finally:
        app._generating_thread_tasks.pop("t1", None)


@pytest.mark.asyncio
async def test_stop_thread_generating_cancels_task_and_closes_llm_clients(monkeypatch) -> None:
    app._generating_thread_tasks.clear()
    aclose_calls = []

    async def fake_aclose(thread_id):
        aclose_calls.append(thread_id)

    monkeypatch.setattr(app, "aclose_active_llm_clients", fake_aclose)

    async def never_ends():
        await asyncio.sleep(10)

    task = asyncio.create_task(never_ends())
    app._generating_thread_tasks["t1"] = task
    try:
        assert await app._stop_thread_generating("t1") is True
        with pytest.raises(asyncio.CancelledError):
            await task
        assert aclose_calls == ["t1"]
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_stop_thread_generating_cancels_background_jobs_even_when_task_done(monkeypatch) -> None:
    """2026-08-26レビューで発見した不具合の回帰防止。

    メイングラフのタスクが既に None/done() であっても、dispatch_agent・
    run_script_background/execute_python_code_background のバックグラウンド
    ジョブ（いずれも asyncio.shield() で保護されている）はまだ動いている
    可能性がある（dispatch_agentが安全上限超過でjob_idを返してターンを
    終えた直後、あるいは1回目の停止ボタンでメインタスク自体は既に
    cancelled=doneになった後に別タブ/再度の停止リクエストが来た場合等）。
    旧実装は task.done() で即 return False しており、この最も典型的な
    シナリオでバックグラウンドジョブの強制終了が一度も行われなかった。
    """
    app._generating_thread_tasks.clear()
    dispatch_calls = []
    script_calls = []
    aclose_calls = []

    async def fake_cancel_dispatch(thread_id):
        dispatch_calls.append(thread_id)
        return True

    async def fake_cancel_script(thread_id):
        script_calls.append(thread_id)
        return False

    async def fake_aclose(thread_id):
        aclose_calls.append(thread_id)

    monkeypatch.setattr(app, "cancel_dispatch_agent_jobs_for_thread", fake_cancel_dispatch)
    monkeypatch.setattr(app, "cancel_background_script_jobs_for_thread", fake_cancel_script)
    monkeypatch.setattr(app, "aclose_active_llm_clients", fake_aclose)

    # メインタスクは未登録（task is None のケース）。
    assert await app._stop_thread_generating("t1") is True

    assert dispatch_calls == ["t1"]
    assert script_calls == ["t1"]
    assert aclose_calls == ["t1"]


@pytest.mark.asyncio
async def test_stop_thread_generating_returns_false_when_nothing_to_stop(monkeypatch) -> None:
    """メインタスクが無く、バックグラウンドジョブも1件も無ければ False のまま。"""
    app._generating_thread_tasks.clear()

    async def fake_cancel_none(thread_id):
        return False

    async def fake_aclose(thread_id):
        pass

    monkeypatch.setattr(app, "cancel_dispatch_agent_jobs_for_thread", fake_cancel_none)
    monkeypatch.setattr(app, "cancel_background_script_jobs_for_thread", fake_cancel_none)
    monkeypatch.setattr(app, "aclose_active_llm_clients", fake_aclose)

    assert await app._stop_thread_generating("t1") is False
