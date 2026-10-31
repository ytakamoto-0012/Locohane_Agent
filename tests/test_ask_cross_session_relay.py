"""ask系ツール（approve_plan/ask_user_question/ask_user_choice）のクロス
セッション引き継ぎの回帰テスト。

背景（2026-08-21 ユーザー報告、2026-08-22 一般化）: 左サイドバーでの会話切り替え
（frontend/src/components/Sidebar.tsx の goToThread）はフルページリロードの
ため、ask系ツールの応答待ち（cl.AskActionMessage/AskUserMessage/AskElementMessage
.send()）を出したまま別スレッドへ移動して戻ってくると、Chainlit的には
元セッションとは完全な別セッションになり（sessionIdState が毎回 uuid4() で
新規発行され直すため）、ボタン・入力フォームがどこにも表示されなくなる。

当初 approve_plan だけに個別対応（_ask_action_with_cross_session_relay）していたが、
同じ問題が ask_user_question/ask_user_choice にもあったため、
src.tools._ask_relay_helper._ask_with_cross_session_relay（元セッション側、factory一般化版）と
app._relay_pending_ask（app.on_chat_resume から起動される、戻ってきた新セッション側）
が、src.ask_relay の asyncio.Future を介して連携する形に一般化した。
"""

import asyncio

import pytest

import app
import src.tools as tools


class _FakeUserSession:
    """cl.user_session の差し替え。thread_id だけ既定値として引ける最小限のスタブ。
    """

    def __init__(self, thread_id: str):
        self._data = {"thread_id": thread_id}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _ScriptedAsk:
    """cl.AskActionMessage/AskUserMessage/AskElementMessage の差し替え。呼び出し
    ごとに behaviors から1つずつ非同期の振る舞いを消費する（呼び出し順=クラス
    インスタンス化順ではなく実際に send() が実行された順、に一致させるため
    behaviors はテスト側で呼び出し順を意識して積む）。
    """

    behaviors: list = []

    def __init__(self, content=None, actions=None, element=None, timeout=90, **kwargs):
        self.content = content
        self.actions = actions
        self.element = element
        self.timeout = timeout

    async def send(self):
        behavior = _ScriptedAsk.behaviors.pop(0)
        return await behavior()


@pytest.mark.asyncio
async def test_local_session_wins_when_it_answers_first(monkeypatch) -> None:
    """通常時（スレッド切り替えが起きない）は今まで通り、送信元セッション自身の
    Ask*Message の応答がそのまま採用され、pending_asks も残らない。
    """
    thread_id = "local-wins-thread"
    app._pending_asks.pop(thread_id, None)

    response = {"name": "approve", "payload": {"value": "approve"}, "label": "approve", "tooltip": "", "forId": "x", "id": "y"}

    async def local_send_immediate():
        return response

    _ScriptedAsk.behaviors = [local_send_immediate]
    monkeypatch.setattr(tools.cl, "AskActionMessage", _ScriptedAsk)

    actions = [tools.cl.Action(name="approve", payload={"value": "approve"}, label="approve")]

    def factory():
        return tools.cl.AskActionMessage(content="content", actions=actions, timeout=30).send()

    res = await tools._ask_relay_helper._ask_with_cross_session_relay(thread_id, factory, 30)

    assert res == response
    assert thread_id not in app._pending_asks


@pytest.mark.asyncio
async def test_relayed_session_answer_unblocks_dead_original_session(monkeypatch) -> None:
    """元セッション側の Ask*Message.send() が（切断済みで）応答不能のまま
    ハングしていても、app._relay_pending_ask（=戻ってきた新セッション側が
    on_chat_resume 経由で呼ぶもの）が答えると、元セッション側の待機がそちらの
    回答で解決され、ハングしていた方はキャンセルされる。
    """
    thread_id = "relay-wins-thread"
    app._pending_asks.pop(thread_id, None)

    local_started = asyncio.Event()
    hang_forever = asyncio.Event()
    cancelled = {"value": False}

    async def local_send_hangs():
        local_started.set()
        try:
            await hang_forever.wait()
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise

    relay_response = {"name": "approve", "payload": {"value": "approve"}, "label": "approve", "tooltip": "", "forId": "x", "id": "y"}

    async def relay_send_immediate():
        return relay_response

    _ScriptedAsk.behaviors = [local_send_hangs, relay_send_immediate]
    monkeypatch.setattr(tools.cl, "AskActionMessage", _ScriptedAsk)
    monkeypatch.setattr(app.cl, "AskActionMessage", _ScriptedAsk)

    actions = [tools.cl.Action(name="approve", payload={"value": "approve"}, label="approve")]

    def factory():
        return tools.cl.AskActionMessage(content="content", actions=actions, timeout=30).send()

    local_call_task = asyncio.create_task(tools._ask_relay_helper._ask_with_cross_session_relay(thread_id, factory, 30))

    await asyncio.wait_for(local_started.wait(), timeout=1)
    assert thread_id in app._pending_asks

    # 戻ってきた新セッション（on_chat_resume）が同じ内容を出し直して回答する。
    await app._relay_pending_ask(thread_id)

    res = await asyncio.wait_for(local_call_task, timeout=1)

    assert res == relay_response
    assert cancelled["value"] is True, "応答不能になった元セッション側の待機がキャンセルされていない"
    assert thread_id not in app._pending_asks


@pytest.mark.asyncio
async def test_relay_answer_still_wins_after_local_socket_times_out(monkeypatch) -> None:
    """元セッション側の Ask*Message.send() が「本物の回答」ではなく無応答
    タイムアウト（None）で先に終わっても、その後に届くrelayの正しい回答が
    握りつぶされてはいけない。

    死んだソケット（切断済みの元セッション）宛の送信は相手が絶対に応答
    できないため、そのtimeoutは「ユーザーが実際に無視した」ことを意味しない。
    離脱してからask系ツールが呼ばれるまで長く待ち、戻ってきて正しく回答した
    のに、死んだソケット側のtimeoutだけが先に発火してLLMには「応答なし」が
    返っていた（2026-08-22 実機テストで発覚）。
    """
    thread_id = "local-times-out-then-relay-wins-thread"
    app._pending_asks.pop(thread_id, None)

    local_timed_out = asyncio.Event()

    async def local_send_times_out():
        # 死んだソケット宛の送信を模す: 誰にも応答されないまま
        # Ask*Message自身のtimeout相当でNoneに解決する。
        local_timed_out.set()
        return None

    relay_response = {"name": "approve", "payload": {"value": "approve"}, "label": "approve", "tooltip": "", "forId": "x", "id": "y"}

    async def relay_send_after_local_timeout():
        await local_timed_out.wait()
        return relay_response

    _ScriptedAsk.behaviors = [local_send_times_out, relay_send_after_local_timeout]
    monkeypatch.setattr(tools.cl, "AskActionMessage", _ScriptedAsk)
    monkeypatch.setattr(app.cl, "AskActionMessage", _ScriptedAsk)

    actions = [tools.cl.Action(name="approve", payload={"value": "approve"}, label="approve")]

    def factory():
        return tools.cl.AskActionMessage(content="content", actions=actions, timeout=30).send()

    local_call_task = asyncio.create_task(tools._ask_relay_helper._ask_with_cross_session_relay(thread_id, factory, 30))

    await asyncio.wait_for(local_timed_out.wait(), timeout=1)
    # local_ask_task が None で先に終わった直後に、戻ってきた新セッションが回答する。
    await app._relay_pending_ask(thread_id)

    res = await asyncio.wait_for(local_call_task, timeout=1)

    assert res == relay_response
    assert thread_id not in app._pending_asks


@pytest.mark.asyncio
async def test_relay_pending_ask_is_noop_without_registration() -> None:
    """未登録・解決済みのスレッドに対しては何もしない（呼び出しても安全）。"""
    thread_id = "no-such-thread"
    app._pending_asks.pop(thread_id, None)
    await app._relay_pending_ask(thread_id)  # 例外にならないこと
    assert thread_id not in app._pending_asks


@pytest.mark.asyncio
async def test_ask_user_question_relay_reconstructs_element_per_call(monkeypatch) -> None:
    """ask_user_question（labels指定=AskElementMessage）でも、approve_planと
    同じ引き継ぎが機能する。factoryが呼ばれるたびに cl.CustomElement を
    新規生成すること（中継時に別セッション文脈で再構築する必要があるため）。
    """
    thread_id = "ask-user-question-thread"
    app._pending_asks.pop(thread_id, None)

    local_started = asyncio.Event()
    hang_forever = asyncio.Event()
    element_build_count = {"value": 0}

    async def local_send_hangs():
        local_started.set()
        await hang_forever.wait()

    relay_response = {"values": ["山田太郎", "PDF"]}

    async def relay_send_immediate():
        return relay_response

    _ScriptedAsk.behaviors = [local_send_hangs, relay_send_immediate]
    monkeypatch.setattr(tools.cl, "AskElementMessage", _ScriptedAsk)

    class _CountingCustomElement:
        def __init__(self, *args, **kwargs):
            element_build_count["value"] += 1

    monkeypatch.setattr(tools.cl, "CustomElement", _CountingCustomElement)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession(thread_id))

    call_task = asyncio.create_task(
        tools.ask_user_question.ainvoke({"question": "お名前と形式は？", "labels": ["名前", "形式"]})
    )

    await asyncio.wait_for(local_started.wait(), timeout=1)
    assert thread_id in app._pending_asks

    await app._relay_pending_ask(thread_id)

    result = await asyncio.wait_for(call_task, timeout=1)

    assert result == "名前: 山田太郎\n形式: PDF"
    assert element_build_count["value"] == 2, "factory呼び出しのたびにCustomElementを新規生成していない"
    assert thread_id not in app._pending_asks


@pytest.mark.asyncio
async def test_ask_user_choice_relay_single_select(monkeypatch) -> None:
    """ask_user_choice（単一選択=AskActionMessage）でも同じ引き継ぎが機能する。"""
    thread_id = "ask-user-choice-thread"
    app._pending_asks.pop(thread_id, None)

    local_started = asyncio.Event()
    hang_forever = asyncio.Event()

    async def local_send_hangs():
        local_started.set()
        await hang_forever.wait()

    relay_response = {"payload": {"value": "赤"}, "label": "赤"}

    async def relay_send_immediate():
        return relay_response

    _ScriptedAsk.behaviors = [local_send_hangs, relay_send_immediate]
    monkeypatch.setattr(tools.cl, "AskActionMessage", _ScriptedAsk)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession(thread_id))

    call_task = asyncio.create_task(tools.ask_user_choice.ainvoke({"question": "色は？", "choices": ["赤", "青"]}))

    await asyncio.wait_for(local_started.wait(), timeout=1)
    assert thread_id in app._pending_asks

    await app._relay_pending_ask(thread_id)

    result = await asyncio.wait_for(call_task, timeout=1)

    assert result == "赤"
    assert thread_id not in app._pending_asks


@pytest.mark.asyncio
async def test_already_connected_viewer_receives_ask_without_reconnect(monkeypatch) -> None:
    """2026-08-22 ユーザー実機テスト報告: 離脱→復帰済みで、画面を見たまま
    留まっている間に ask系ツールが呼ばれても、入力ボックス・ボタンが一切
    表示されなかった。on_chat_resume 経由の引き継ぎ（_relay_pending_ask）は
    「新しいセッションが接続してきた瞬間」にしか発火しないため、この
    「既に接続済みのまま留まっているセッション」のケースはカバーできない。
    dispatch_to_live_viewers（register_pending_ask直後に同期的に呼ばれる）が
    このケースを別途カバーすることを検証する。
    """
    import importlib

    from chainlit.session import ws_sessions_sid

    # `import chainlit.context as X` は `chainlit/__init__.py` の
    # `from .context import context`（LazyProxyインスタンスをパッケージ属性
    # `chainlit.context` として再バインドする）に引きずられ、属性アクセス経由で
    # サブモジュールではなくそのインスタンスを掴んでしまう。
    # importlib.import_module で sys.modules から直接サブモジュールを取る。
    chainlit_context_module = importlib.import_module("chainlit.context")

    thread_id = "already-connected-thread"
    app._pending_asks.pop(thread_id, None)

    class _FakeSession:
        def __init__(self, thread_id: str):
            self.thread_id = thread_id

    origin = _FakeSession(thread_id)
    viewer = _FakeSession(thread_id)
    other_thread_viewer = _FakeSession("other-thread")

    class _FakeContext:
        def __init__(self, session):
            self.session = session

    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession(thread_id))
    monkeypatch.setattr(tools.cl, "context", _FakeContext(origin))

    init_ws_context_calls: list = []

    def fake_init_ws_context(session):
        # 本番では実際にcl.contextを対象セッションへ束縛するが、このテストの
        # 関心は「誰に対して呼ばれたか」（=フィルタリングの正しさ）なので、
        # 呼ばれたことだけを記録するダミーに差し替える。
        init_ws_context_calls.append(session)

    monkeypatch.setattr(chainlit_context_module, "init_ws_context", fake_init_ws_context)

    saved_sessions = dict(ws_sessions_sid)
    ws_sessions_sid.clear()
    ws_sessions_sid["origin-sid"] = origin
    ws_sessions_sid["viewer-sid"] = viewer
    ws_sessions_sid["other-thread-sid"] = other_thread_viewer

    local_started = asyncio.Event()
    hang_forever = asyncio.Event()

    async def local_send_hangs():
        local_started.set()
        await hang_forever.wait()

    viewer_response = {"output": "テスト回答です"}

    async def viewer_send_immediate():
        return viewer_response

    _ScriptedAsk.behaviors = [local_send_hangs, viewer_send_immediate]
    monkeypatch.setattr(tools.cl, "AskUserMessage", _ScriptedAsk)

    try:
        # goToThread によるフルページリロード（on_chat_resume）は一切起きない
        # 前提のテストなので、app._relay_pending_ask は意図的に呼ばない。
        call_task = asyncio.create_task(tools.ask_user_question.ainvoke({"question": "テスト質問"}))

        result = await asyncio.wait_for(call_task, timeout=1)
    finally:
        ws_sessions_sid.clear()
        ws_sessions_sid.update(saved_sessions)

    assert result == "テスト回答です"
    assert viewer in init_ws_context_calls
    assert origin not in init_ws_context_calls, "生成元セッション自身へ中継する必要は無い"
    assert other_thread_viewer not in init_ws_context_calls, "無関係なスレッドを見ている閲覧者へ誤送信してはいけない"
    assert thread_id not in app._pending_asks
