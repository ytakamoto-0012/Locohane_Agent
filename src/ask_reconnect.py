"""ask系プロンプト（approve_plan/AskUserQuestion/ask_user_choice の
Ask*Message）を、同じセッションの WebSocket 再接続後も出し直すためのパッチ。

背景（2026-10-16 ユーザー報告: 質問のボタン・入力欄をしばらく放置すると
消え、会話履歴を開き直すと表示される）:
Chainlit の Ask*Message.send() は emitter.send_ask_user() の中で
`sio.call("ask", ..., to=<その時点のsid>)` を1回だけ送り、そのsidからの
確認応答を待つ。ブラウザがスリープ・バックグラウンドでの間引き・
ネットワーク瞬断等で ping_timeout を超えると Socket.IO は同じ
sessionId のまま自動で繋ぎ直す（chainlit/socket.py の
restore_existing_session。sid だけが新しくなる）。このとき:

- connection_successful が無条件に clear_ask を送るため、フロントの
  askUser 状態（ボタン・入力欄）が消える。
- サーバー側の sio.call は切断済みの古い sid 宛のまま待ち続けるため、
  新しいソケットへは二度と ask が届かない（無期限待ちの設定では永久に）。

スレッド切り替え（フルページリロード）で別セッションになる場合は
src/ask_relay.py の引き継ぎ（on_chat_resume 経由）が拾うが、同じセッションの
再接続は on_chat_resume を通らないため対象外だった。会話履歴を開き直すと
表示されるのは、再読み込みで別セッションになりそちらの経路に乗るため。

対策として ChainlitEmitter.emit_call を差し替え、"ask" の呼び出しだけを
「確認応答」と「そのセッションの再接続通知」の早い方で待つようにする。
再接続が先なら、同じ step・spec のまま新しい sid へ ask を送り直す
（フロントの ask ハンドラはメッセージを step id で upsert するため、
質問メッセージは重複しない）。ask が切断中に送られていた場合に備え、
ボタン（action）・フォーム（カスタム要素）も送り直す。
再接続の通知は app.py の connection_successful パッチが、Chainlit 標準の
clear_ask 送信より後に notify_reconnected() を呼んで行う
（先に送り直すと直後の clear_ask で消されるため）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from typing import Any

logger = logging.getLogger(__name__)

# session.id -> そのセッションの次の再接続で set される Event。
# 待機中の ask だけが強参照を持つので、待つ者がいなくなれば自動で消える。
_reconnect_events: "weakref.WeakValueDictionary[str, asyncio.Event]" = weakref.WeakValueDictionary()

# 送信済みで、まだ回答待ちの可能性があるボタン（action.id -> action dict）と
# カスタム要素（element.id -> element dict）。再接続時の送り直し用。
# ask が終わったら _resilient_ask_call の finally で消す。
_sent_actions: dict[str, dict] = {}
_sent_custom_elements: dict[str, dict] = {}

_installed = False


def notify_reconnected(session) -> None:
    """session が再接続した（connection_successful を受けた）ことを、
    そのセッションで回答待ちの ask へ知らせる。待機中の ask が無ければ何もしない。
    """
    event = _reconnect_events.pop(session.id, None)
    if event is not None:
        event.set()


def _reconnect_event(session_id: str) -> asyncio.Event:
    event = _reconnect_events.get(session_id)
    if event is None:
        event = asyncio.Event()
        _reconnect_events[session_id] = event
    return event


async def _resend_ask_assets(session, spec: dict) -> None:
    """ask が参照するボタン・カスタム要素を新しいソケットへ送り直す。
    action はフロントで追記されるだけなので、先に remove_action して重複させない。
    """
    if spec.get("type") == "action":
        for key in spec.get("keys") or []:
            action = _sent_actions.get(key)
            if action is not None:
                await session.emit("remove_action", action)
                await session.emit("action", action)
    elif spec.get("type") == "element":
        element = _sent_custom_elements.get(spec.get("element_id") or "")
        if element is not None:
            await session.emit("element", element)


async def _resilient_ask_call(session, data: dict, timeout: float) -> Any:
    """session.emit_call("ask", ...) を、再接続のたびに新しい sid へ送り直しながら待つ。

    timeout は再接続をまたいだ通算で扱い、超えたら sio.call と同じ
    socketio.exceptions.TimeoutError を送出する（呼び出し元の扱いを変えないため）。
    """
    from socketio.exceptions import TimeoutError as SocketIOTimeoutError

    spec = data.get("spec") or {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    resend = False
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SocketIOTimeoutError()
            reconnected = _reconnect_event(session.id)
            if resend:
                await _resend_ask_assets(session, spec)
            call_task = asyncio.ensure_future(session.emit_call("ask", data, remaining))
            wait_task = asyncio.ensure_future(reconnected.wait())
            try:
                await asyncio.wait({call_task, wait_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                wait_task.cancel()
                if not call_task.done():
                    call_task.cancel()
                    with contextlib.suppress(BaseException):
                        await call_task
            if not call_task.cancelled():
                return call_task.result()
            logger.info("WebSocket reconnect: resending pending questions to the new connection [session_id=%s]", session.id)
            resend = True
    finally:
        for key in spec.get("keys") or []:
            _sent_actions.pop(key, None)
        _sent_custom_elements.pop(spec.get("element_id") or "", None)


def install() -> None:
    """ChainlitEmitter.emit_call・send_element と Action.send/remove を差し替える。
    何度呼んでも1回だけ適用する。
    """
    global _installed
    if _installed:
        return
    from chainlit.action import Action
    from chainlit.emitter import ChainlitEmitter

    original_emit_call = ChainlitEmitter.emit_call
    original_send_element = ChainlitEmitter.send_element
    original_action_send = Action.send
    original_action_remove = Action.remove

    def _emit_call(self):
        raw = original_emit_call.fget(self)
        session = self.session

        async def call(event, data, timeout):
            if event != "ask":
                return await raw(event, data, timeout)
            return await _resilient_ask_call(session, data, timeout)

        return call

    async def _send_element(self, element_dict):
        if element_dict.get("type") == "custom" and element_dict.get("id"):
            _sent_custom_elements[element_dict["id"]] = element_dict
        return await original_send_element(self, element_dict)

    async def _action_send(self, for_id: str):
        await original_action_send(self, for_id)
        _sent_actions[self.id] = self.to_dict()

    async def _action_remove(self):
        _sent_actions.pop(self.id, None)
        await original_action_remove(self)

    ChainlitEmitter.emit_call = property(_emit_call)
    ChainlitEmitter.send_element = _send_element
    Action.send = _action_send
    Action.remove = _action_remove
    _installed = True
    logger.info("Applied the patch resending ask prompts after a WebSocket reconnect.")
