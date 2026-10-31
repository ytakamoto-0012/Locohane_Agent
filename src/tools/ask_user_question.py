"""ask_user_question(AskUserQuestion) ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import chainlit as cl
import logging

from . import _state
from ._ask_echo import _echo_ask_answer
from ._ask_echo import _is_text_ask_cancelled
from ._ask_relay_helper import _ask_with_cross_session_relay
from ._state import _resolve_ask_timeout
from ..i18n import t

logger = logging.getLogger(__name__)

_ASK_QUESTION_CANCEL_MESSAGE = "Error: the user cancelled answering the question."


@tool("AskUserQuestion")
async def ask_user_question(question: str, labels: list[str] | None = None) -> str:
    """Ask the user, in free text, for additional information needed to continue the conversation.

    Use it when you need a free-text answer (proper nouns, file paths, detailed requests, etc.),
    for example because the request is ambiguous or information is missing. If you want the user
    to choose from options, use ask_user_choice.

    For a single question, omit labels. Only when you want several items (e.g. a file name and an
    output format) answered in free text at once, list a label for each input field in labels.
    You do not need to repeat this tool for each item.

    Args:
        question: The question shown to the user (when labels is given, it is shown as the
            heading of the whole form).
        labels: When asking several items at once, a list of label strings, one per input
            field. If omitted (None or an empty list), a single free-text input is shown.

    Returns:
        If labels is omitted, the answer text the user entered as is.
        If labels is given, a string with "label: value" lines separated by newlines.
        A "Cancel" button is always shown, and if the user cancels it returns
        "Error: the user cancelled answering the question."
        If there is no response within the configured timeout, it returns
        "Error: there was no response from the user (timeout)." instead of raising.
    """
    timeout = _resolve_ask_timeout(_state._ASK_USER_QUESTION_TIMEOUT_SECONDS)
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    if not labels:
        logger.info("ask_user_question: %s", question)

        def _factory():
            return cl.AskUserMessage(content=question, timeout=timeout).send()

        res = await _ask_with_cross_session_relay(thread_id, _factory, timeout)
        if res is None:
            return "Error: there was no response from the user (timeout)."
        if _is_text_ask_cancelled(res):
            return _ASK_QUESTION_CANCEL_MESSAGE
        return res.get("output", "")
    logger.info("ask_user_question: %s labels=%s", question, labels)

    async def _factory_element():
        # cl.CustomElement はセッション文脈に依存するため、中継時に別セッションで
        # 作り直せるよう毎回ここで新規生成する（_ask_with_cross_session_relay の
        # factory 引数のdocstring参照）。
        element = cl.CustomElement(name="MultiTextForm", props={"question": question, "labels": labels})
        ask_message = cl.AskElementMessage(content=question, element=element, timeout=timeout)
        res = await ask_message.send()
        if res is not None:
            if not res.get("submitted", True):
                answer = t("ask.cancel")
            else:
                # 改行区切りのままだと Markdown 表示で1行に連結されるため、箇条書きで表示する。
                values = res.get("values") or []
                answer = "\n".join(f"- {label}: {value}" for label, value in zip(labels, values))
            await _echo_ask_answer(ask_message, question, answer)
        return res

    res = await _ask_with_cross_session_relay(thread_id, _factory_element, timeout)
    if res is None:
        return "Error: there was no response from the user (timeout)."
    if not res.get("submitted", True):
        return _ASK_QUESTION_CANCEL_MESSAGE
    values = res.get("values") or []
    return "\n".join(f"{label}: {value}" for label, value in zip(labels, values))
