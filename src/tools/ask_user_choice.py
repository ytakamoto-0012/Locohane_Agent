"""ask_user_choice ツール。"""

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


_ASK_CHOICE_CANCEL_VALUE = "__cancel__"
_ASK_CHOICE_OTHER_VALUE = "__other__"
_ASK_CHOICE_CANCEL_MESSAGE = "Error: the user cancelled the choice."


def _multi_select_answer(res: dict) -> str:
    """MultiChoiceForm の送信結果を、ツールの戻り値（兼チャット表示）の文字列にする。"""
    selected = list(res.get("values") or [])
    other = (res.get("other") or "").strip()
    if other:
        selected.append(other)
    return t("list.separator").join(selected) if selected else t("ask.none_selected")


@tool
async def ask_user_choice(question: str, choices: list[str], multi_select: bool = False) -> str:
    """Ask the user to make a choice needed to continue the conversation, in multiple-choice form.

    Use it when you want the user to pick one (or several) of multiple ways or policies.
    If you need a free-text answer, use AskUserQuestion.

    "Other (free text)" and "Cancel" options are always added automatically to the choices shown.
    If "Other" is chosen, a free-text input is shown next and its answer is returned. "Cancel"
    works as a way out when the user wants to give an instruction that is not in choices.

    Args:
        question: The question shown to the user.
        choices: A list of choice strings (at least one).
        multi_select: If True, the choices are shown as checkboxes so several can be selected,
            and the selected choices are returned together (if submitted with nothing selected,
            a "nothing selected" marker is returned). If False (default), as before, clicking a
            choice button immediately selects and returns that one choice
            (use this when a single answer must be fixed).

    Returns:
        With multi_select=False (default), the chosen choice string; with True, the selected
        choices joined into one string (a "nothing selected" marker if none).
        In either case, if it went through "Other", it is the free-text answer.
        If the user cancels, it returns "Error: the user cancelled the choice."
        If choices is empty or there is no response within the configured timeout,
        it returns an "Error: ..." string instead of raising.
    """
    if not choices:
        return "Error: choices is empty. Specify at least one choice."
    logger.info("ask_user_choice: %s choices=%s multi_select=%s", question, choices, multi_select)
    timeout = _resolve_ask_timeout(_state._ASK_USER_CHOICE_TIMEOUT_SECONDS)
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    if multi_select:

        async def _factory_multi():
            # cl.CustomElement はセッション文脈に依存するため、中継時に別セッションで
            # 作り直せるよう毎回ここで新規生成する（_ask_with_cross_session_relay の
            # factory 引数のdocstring参照）。
            element = cl.CustomElement(name="MultiChoiceForm", props={"question": question, "choices": choices})
            ask_message = cl.AskElementMessage(content=question, element=element, timeout=timeout)
            res = await ask_message.send()
            if res is not None:
                submitted = res.get("submitted", True)
                answer = _multi_select_answer(res) if submitted else t("ask.cancel")
                await _echo_ask_answer(ask_message, question, answer)
            return res

        res = await _ask_with_cross_session_relay(thread_id, _factory_multi, timeout)
        if res is None:
            return "Error: there was no response from the user (timeout)."
        if not res.get("submitted", True):
            return _ASK_CHOICE_CANCEL_MESSAGE
        return _multi_select_answer(res)

    async def _factory_single():
        actions = [cl.Action(name=f"choice_{i}", payload={"value": c}, label=c) for i, c in enumerate(choices)]
        actions.append(cl.Action(name="other", payload={"value": _ASK_CHOICE_OTHER_VALUE}, label=t("ask.other")))
        actions.append(cl.Action(name="cancel", payload={"value": _ASK_CHOICE_CANCEL_VALUE}, label=t("ask.cancel")))
        ask_message = cl.AskActionMessage(content=question, actions=actions, timeout=timeout)
        res = await ask_message.send()
        if res is not None:
            # 「その他」の場合も押したボタンを表示し、続く自由記述の回答は
            # AskTextBar が user_message として表示する。
            await _echo_ask_answer(ask_message, question, res.get("label") or res["payload"].get("value", ""))
        return res

    res = await _ask_with_cross_session_relay(thread_id, _factory_single, timeout)
    if res is None:
        return "Error: there was no response from the user (timeout)."
    value = res["payload"].get("value") or res.get("label", "")
    if value == _ASK_CHOICE_CANCEL_VALUE:
        return _ASK_CHOICE_CANCEL_MESSAGE
    if value == _ASK_CHOICE_OTHER_VALUE:

        def _factory_other():
            return cl.AskUserMessage(content=question, timeout=timeout).send()

        other_res = await _ask_with_cross_session_relay(thread_id, _factory_other, timeout)
        if other_res is None:
            return "Error: there was no response from the user (timeout)."
        if _is_text_ask_cancelled(other_res):
            return _ASK_CHOICE_CANCEL_MESSAGE
        return other_res.get("output", "")
    return value
