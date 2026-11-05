"""生成中のターンへユーザーの追加指示を割り込ませるためのキュー。

ClaudeCode で処理中に入力した指示が次のステップから反映されるのと同じ仕組み。
フロントエンド（Composer.tsx）は生成中に送信された本文を通常の送信ではなく
POST /locohane/threads/{id}/inject（app.py）へ送り、ここでスレッドごとのキューへ積む。

取り出すのは2か所:
- グラフの agent ノード（src/graph.py の call_model / pre_model_hook）。モデル呼び出しの
  直前は、直前のツール呼び出しの結果がすべて揃った安全な区切りなので、ここで
  HumanMessage として履歴へ足す（tool_calls と ToolMessage の対応を壊さない）。
- ターンの終わり（app.py の _on_message_impl）。最終回答で終わる直前に残っていた分は、
  そのまま次のユーザー発言としてグラフを続けて回す。

受付の開始・終了（open_thread / close_thread）は on_message が管理する。終了処理は
最後の取り出しと await を挟まずに続けて行うため（単一のイベントループ上）、受け付けた
指示が取り出されないまま捨てられることは、停止・エラーでターンが中断した場合を除き無い。
受付が閉じている間の送信は拒否し（enqueue が None を返す）、フロントエンドがターンの
終了後に通常のメッセージとして送り直す。

チャット画面への表示は、取り出した側（グラフなら custom event
INJECTION_APPLIED_EVENT 経由で app.py）が行う。グラフ側は Chainlit に依存しない
（evals/ のヘッドレス実行でも同じグラフを使うため。キューが空なら何もしない）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

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


# thread_id → 受付中のキュー。キーが存在する間だけ受け付ける。
_queues: dict[str, list[Injection]] = {}


def open_thread(thread_id: str) -> None:
    """thread_id の受付を始める（ターン開始時）。既に開いていれば何もしない。"""
    _queues.setdefault(thread_id, [])


def close_thread(thread_id: str) -> list[Injection]:
    """thread_id の受付を終え、取り出されずに残っていた指示を返す。"""
    return _queues.pop(thread_id, None) or []


def is_open(thread_id: str) -> bool:
    """thread_id が受付中か。"""
    return thread_id in _queues


def enqueue(thread_id: str, text: str) -> Injection | None:
    """指示をキューへ積む。

    Returns:
        積んだ Injection。受付が閉じている（ターンが動いていない・終わりかけ）場合は None。
    """
    queue = _queues.get(thread_id)
    if queue is None:
        return None
    injection = Injection(id=str(uuid.uuid4()), text=text)
    queue.append(injection)
    return injection


def drain(thread_id: str | None) -> list[Injection]:
    """thread_id のキューに積まれた指示をすべて取り出す（受付は開いたまま）。"""
    if thread_id is None:
        return []
    queue = _queues.get(thread_id)
    if not queue:
        return []
    taken = list(queue)
    queue.clear()
    return taken


def to_human_message(injection: Injection) -> HumanMessage:
    """割り込み指示を LLM へ渡す HumanMessage にする。"""
    return HumanMessage(
        content=INJECTION_PREFIX + injection.text,
        id=injection.id,
        additional_kwargs={INJECTION_KWARG: True},
    )


def event_payload(injections: list[Injection]) -> dict:
    """INJECTION_APPLIED_EVENT に載せるデータ。"""
    return {"injections": [{"id": i.id, "text": i.text} for i in injections]}
