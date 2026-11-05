"""生成中のターンへユーザーの追加指示を割り込ませるためのキュー。

ClaudeCode で処理中に入力した指示が次のステップから反映されるのと同じ仕組み。
フロントエンド（Composer.tsx）は生成中に送信された本文を通常の送信ではなく
POST /locohane/threads/{id}/inject（app.py）へ送り、ここでスレッドごとのキューへ積む。

取り出すのは2か所:
- グラフのモデル呼び出しの直前（src/graph.py の inject ノード / pre_model_hook）。直前の
  ツール呼び出しの結果がすべて揃った安全な区切りなので、ここで HumanMessage として
  履歴へ足す（tool_calls と ToolMessage の対応を壊さない）。
- ターンの終わり（app.py の _on_message_impl）。最終回答で終わる直前に残っていた分は、
  そのまま次のユーザー発言としてグラフを続けて回す。

受付の開始・終了（open_thread / close_thread）は on_message が管理する。終了処理は
最後の取り出しと await を挟まずに続けて行うため（単一のイベントループ上）、受け付けた
指示が取り出されないまま捨てられることは、停止・エラーでターンが中断した場合を除き無い。
停止ボタン（app.py の on_stop / _stop_thread_generating）は、タスクのキャンセルが実際に
効くのを待たずにその場で受付を閉じる（停止後に指示が会話へ入り、フロントエンドが入力欄へ
戻した指示と二重になるのを防ぐ）。受付が閉じている間の送信は拒否し（enqueue が None を
返す）、フロントエンドがターンの終了後に通常のメッセージとして送り直す。

受付はターンごとのトークンで区別する。停止後に古いターンの後始末（finally）が終わる前に
同じスレッドで次のターンが始まっても、古いターンが新しいターンの受付を閉じないようにするため。

チャット画面への表示は、取り出した側（グラフなら custom event
INJECTION_APPLIED_EVENT 経由で app.py）が行う。グラフ側は Chainlit に依存しない
（evals/ のヘッドレス実行でも同じグラフを使うため。キューが空なら何もしない）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage

# グラフが取り出した指示を app.py へ知らせる custom event の名前。
INJECTION_APPLIED_EVENT = "user_injection_applied"

# 割り込みの HumanMessage に付ける前置き（LLM 向け。画面には本文だけを出す）。
# 処理の途中で届いた指示であることを伝え、作業をやり直すのではなく次の手から
# 反映させる。
INJECTION_PREFIX = (
    "(Additional instruction from the user, sent while you were working. "
    "Take it into account from your next step on; if it changes the task, adjust your plan accordingly.)\n"
)

# HumanMessage.additional_kwargs に付ける目印（割り込みで入った発言の判別用）。
INJECTION_KWARG = "locohane_user_injection"


@dataclass(frozen=True)
class Injection:
    """キューに積まれた1件の割り込み指示。

    Attributes:
        id: フロントエンドが反映済みかを突き合わせるための ID。
        text: ユーザーが入力した本文。
    """

    id: str
    text: str


@dataclass
class _TurnQueue:
    token: str
    items: list[Injection] = field(default_factory=list)
    # True の間は drain が何も返さない（app.py がグラフの実行を途中で止めて状態を書き換える間、
    # 先へ進んだグラフが指示を取り出してから打ち切られ、指示が失われるのを防ぐ）。
    held: bool = False


# thread_id → 受付中のターンのキュー。キーが存在する間だけ受け付ける。
_queues: dict[str, _TurnQueue] = {}


def open_thread(thread_id: str) -> str:
    """thread_id の受付を始める（ターン開始時）。

    Returns:
        このターンのトークン（文字列）。close_thread に渡すと、このターンの受付だけを閉じる。
        会話へ入れた HumanMessage にも記録し、このターンに入った指示を見分けるのに使う。
        既に受付中だった場合（通常は起きない）は、積まれていた指示を引き継いで新しいターンにする。
    """
    previous = _queues.get(thread_id)
    queue = _TurnQueue(token=str(uuid.uuid4()), items=list(previous.items) if previous else [])
    _queues[thread_id] = queue
    return queue.token


def set_held(thread_id: str | None, held: bool) -> None:
    """取り出しの保留を切り替える（受付中の場合のみ。受け付け自体は続ける）。"""
    queue = _queues.get(thread_id) if thread_id is not None else None
    if queue is not None:
        queue.held = held


def close_thread(thread_id: str | None, token: str | None = None) -> list[Injection]:
    """thread_id の受付を終え、取り出されずに残っていた指示を返す。

    Args:
        thread_id: 対象スレッド。
        token: open_thread が返したトークン。渡すと、そのターンの受付が開いているときだけ閉じる
            （別のターンの受付には触れず、空リストを返す）。None なら今の受付を無条件に閉じる（停止時）。
    """
    if thread_id is None:
        return []
    queue = _queues.get(thread_id)
    if queue is None or (token is not None and queue.token != token):
        return []
    del _queues[thread_id]
    return queue.items


def current_token(thread_id: str | None) -> str | None:
    """thread_id の今の受付のトークン（受付中でなければ None）。"""
    queue = _queues.get(thread_id) if thread_id is not None else None
    return queue.token if queue is not None else None


def is_open(thread_id: str) -> bool:
    """thread_id が受付中か。"""
    return thread_id in _queues


def enqueue(thread_id: str, text: str, injection_id: str | None = None) -> Injection | None:
    """指示をキューへ積む。

    Args:
        thread_id: 対象スレッド。
        text: 指示の本文。
        injection_id: フロントエンドが決めた ID（通信の失敗・タイムアウトで応答を受け取れなくても、
            チャット欄に届いた発言と突き合わせられるようにするため）。省略時は採番する。
            同じ ID が既に積まれていれば積み直さない（再送による二重登録の防止）。

    Returns:
        積んだ（または既に積まれていた）Injection。受付が閉じている（ターンが動いていない・
        終わりかけ・停止済み）場合は None。
    """
    queue = _queues.get(thread_id)
    if queue is None:
        return None
    if injection_id:
        for existing in queue.items:
            if existing.id == injection_id:
                return existing
    injection = Injection(id=injection_id or str(uuid.uuid4()), text=text)
    queue.items.append(injection)
    return injection


def requeue(thread_id: str | None, injections: list[Injection]) -> None:
    """取り出した指示を、会話へ入れられなかったときにキューの先頭へ戻す（受付中の場合のみ）。"""
    if thread_id is None or not injections:
        return
    queue = _queues.get(thread_id)
    if queue is not None:
        queue.items[0:0] = injections


def drain(thread_id: str | None) -> list[Injection]:
    """thread_id のキューに積まれた指示をすべて取り出す（受付は開いたまま）。"""
    if thread_id is None:
        return []
    queue = _queues.get(thread_id)
    if queue is None or queue.held or not queue.items:
        return []
    taken = list(queue.items)
    queue.items.clear()
    return taken


def to_human_message(injections: list[Injection], turn: str | None = None) -> HumanMessage:
    """割り込み指示（1件以上）を、LLM へ渡す1件の HumanMessage にする。

    まとめて届いた複数の指示を1件にするのは、user ロールのメッセージが連続すると
    チャットテンプレートによっては扱いが不安定になるため（画面表示は1件ずつ）。
    additional_kwargs にはターンのトークンと元の各指示（id・本文）を残す。app.py が、
    会話へ入ったのに画面へ出ていない指示を見つけて表示する（またはその逆を取り除く）ため。
    langchain_openai は HumanMessage の additional_kwargs を送らないので、LLM への入力には影響しない。

    Args:
        injections: 取り出した指示。
        turn: 取り出したときの受付のトークン（current_token）。
    """
    if not injections:
        raise ValueError("injections must not be empty")
    body = "\n\n".join(i.text for i in injections)
    return HumanMessage(
        content=INJECTION_PREFIX + body,
        id=injections[0].id,
        additional_kwargs={
            INJECTION_KWARG: {"turn": turn, "items": [{"id": i.id, "text": i.text} for i in injections]},
        },
    )


def is_injection_message(message) -> bool:
    """割り込み指示を会話へ入れた HumanMessage か。"""
    return isinstance(message, HumanMessage) and bool(message.additional_kwargs.get(INJECTION_KWARG))


def injection_items(message, turn: str | None = None) -> list[dict]:
    """割り込み指示の HumanMessage に入っている元の指示（{"id", "text"}）。

    Args:
        message: 調べるメッセージ。
        turn: 指定すると、そのターンに入った指示のときだけ返す。

    Returns:
        割り込み指示でない（または別のターンの）場合は空リスト。
    """
    if not is_injection_message(message):
        return []
    meta = message.additional_kwargs.get(INJECTION_KWARG)
    if not isinstance(meta, dict):
        return []
    if turn is not None and meta.get("turn") != turn:
        return []
    return [i for i in meta.get("items") or [] if isinstance(i, dict) and "id" in i and "text" in i]


def event_payload(injections: list[Injection]) -> dict:
    """INJECTION_APPLIED_EVENT に載せるデータ。"""
    return {"injections": [{"id": i.id, "text": i.text} for i in injections]}
