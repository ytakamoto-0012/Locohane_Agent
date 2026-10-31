"""plan/plan_approved の変更を、ターン完了を待たずに即座に thread_store へ
永続化するためのフック。app.py（Chainlit エントリスクリプト）と src/tools.py
の双方から参照されるため、どちらにも属さないこの中立モジュールに置く
（src/ask_relay.py と同じ理由。`from app import ...` を tools.py 側からやると
chainlit run が app.py を未初期化の別モジュールとして再実行してしまい、
稼働中のハンドラを壊す。ask_relay.py 冒頭のdocstring参照）。

背景（2026-08-24 ユーザー報告）: 従来、plan/plan_approved はターン完了時
（_on_message_impl 末尾、work_dirと共に thread_store へまとめてスナップ
ショット）にしか永続化されておらず、work_dir と同じ「異常終了でスナップ
ショットに到達しないと保存されない」不具合を抱えていた
（app.py の _persist_work_dir docstring参照、2026-08-21に work_dir 側は
修正済みだった）。承認直後のターンが停止ボタン・通信エラー・思考ループ
上限・recursion_limit・計画却下等で異常終了すると、approve_plan で
承認済みになった最新状態が一度もDBへ書き込まれず、スレッド再開時に
古い（未承認の）metadataへ巻き戻ってPlan Modeへ戻ってしまっていた。
plan_approved を変更する各ツール（create_plan/approve_plan/
update_task_progress/lock_plan_mode/toggle_plan_mode_from_ui）が、
変更直後にここを呼んで即座に保存する。
"""

from __future__ import annotations

import uuid
from typing import Awaitable, Callable

_persist_fn: Callable[[], Awaitable[None]] | None = None

_PLAN_MESSAGE_ID_NAMESPACE = uuid.UUID("d9f4b6a0-3b8b-4f0e-9c3a-6e7c9f2a1b4d")


def plan_message_id(thread_id: str) -> str:
    """PlanCard用 cl.Message の id を thread_id から決定的に導出する。

    create_plan（新規送信）と app.py の on_chat_resume（再送信）が同じ
    thread_id に対して常に同じ id を使うことで、steps テーブルへの永続化が
    id をキーにした UPSERT（ON CONFLICT(id) DO UPDATE、src/thread_store.py
    upsert_step_row 参照）となり、常に1行に収束する。ランダムUUIDを都度
    発行してmetadataへ"最後に送った1件のid"を保存する方式だと、複数タブが
    同時に on_chat_resume した場合に片方の書き込みがもう片方を上書きし、
    孤立したstep行が残ってしまう（2026-09-04 レビュー指摘）。
    """
    return str(uuid.uuid5(_PLAN_MESSAGE_ID_NAMESPACE, thread_id))


def register_plan_persist(fn: Callable[[], Awaitable[None]] | None) -> None:
    """app.py の _on_app_startup 等から一度だけ呼び、実際の永続化処理
    （thread_store.save_thread を呼ぶ app.py 側の関数）を登録する。
    """
    global _persist_fn
    _persist_fn = fn


async def persist_plan_state() -> None:
    """登録済みの永続化処理を呼ぶ。未登録（app.py起動前・テスト環境等で
    register_plan_persist が一度も呼ばれていない場合）は何もしない
    （src/ask_relay.py の resolve_pending_ask 系と同じベストエフォート方針）。
    """
    if _persist_fn is not None:
        await _persist_fn()
