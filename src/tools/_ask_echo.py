"""ask 系ツールでユーザーが送信した内容の表示（チャット窓へのユーザー発言）とキャンセル判定のヘルパー。"""

from __future__ import annotations

import logging

import chainlit as cl

logger = logging.getLogger(__name__)

# frontend/src/components/Composer.tsx・AskTextBar.tsx が送る user_message の name と一致させる。
_ANSWER_AUTHOR = "あなた"

# frontend/src/components/AskTextBar.tsx のキャンセルボタンが user_message の metadata に付けるキー。
_TEXT_ASK_CANCEL_KEY = "askCancel"


def _is_text_ask_cancelled(res: dict) -> bool:
    """AskUserMessage の応答が、AskTextBar のキャンセルボタンによるものかを判定する。

    AskUserMessage の応答は user_message 形式（StepDict）しか返せないため、
    キャンセルボタンは本文「❌ キャンセル」に metadata.askCancel=true を付けて返す。
    本文だけで判定すると、ユーザーが同じ文字列を入力した場合と区別できないため
    metadata で判定する。
    """
    return bool((res.get("metadata") or {}).get(_TEXT_ASK_CANCEL_KEY))


async def _echo_ask_answer(ask_message, question: str, answer: str) -> None:
    """ask 系メッセージへの回答を、ユーザー発言としてチャット窓に表示する。

    Chainlit の AskActionMessage / AskElementMessage は回答を受け取ると、
    質問の吹き出しを「**Selected:** ラベル」「Thanks for submitting」等へ
    書き換えるだけで、何を送信したかがチャットに残らない（AskUserMessage は
    AskTextBar の replyMessage が user_message として表示・永続化するため対象外）。
    そこで質問の吹き出しを質問文に戻し、送信内容を type="user_message" の
    メッセージとして送る（データレイヤーにも保存され、スレッド再開時も残る）。

    _ask_with_cross_session_relay の factory の中、つまり実際に回答を受け取った
    セッションの文脈で呼ぶこと（中継先のセッションで回答された場合も、その
    セッションの画面に表示される）。表示は付随的な処理のため、失敗しても
    ツールの戻り値には影響させず警告ログだけ残す。
    """
    try:
        ask_message.content = question
        await ask_message.update()
        await cl.Message(content=answer, author=_ANSWER_AUTHOR, type="user_message").send()
    except Exception:  # noqa: BLE001 - 表示の失敗でツールの結果を失わせない
        logger.warning("Could not show the answer of the ask tool in the chat", exc_info=True)
