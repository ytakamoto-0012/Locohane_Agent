"""create_plan ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
import chainlit as cl
import logging
import uuid

from ..plan_persist import persist_plan_state, plan_message_id

from . import _state
from ._plan_render import _render_plan_payload, _write_plan_detail

logger = logging.getLogger(__name__)


@tool
async def create_plan(steps: list[dict[str, str]], detail_markdown: str | None = None) -> str:
    """Create a multi-step plan and show it to the user as a checklist.

    Before starting any task that uses a write tool (run_script, run_script_background,
    execute_python_code, execute_python_code_background) even once, first present the list of
    steps with this tool. Creating it does not lift the block on the write tools.
    To get approval, call approve_plan next.

    steps and detail_markdown are shown to the user as is, so write them in the language specified in
    the "Response language" section of the system prompt (not necessarily English like the examples
    below).

    Write steps/detail_markdown based on concrete facts found in your investigation.
    Do not make up step text (including concrete numbers such as line numbers or cell positions)
    from memory or guesswork alone.

    If you call this tool again to replace steps while already approved by approve_plan
    (Edit Automatically), depending on [plan].reset_approval_on_recreate in config.ini the approval
    may be lost and it returns to Plan Mode (not approved) (lost by default). In that case the write
    tools are blocked again unless you call approve_plan again.

    When handling background jobs with run_script_background/execute_python_code_background, do not
    combine "start" and "check completion" in the same step. Starting a job does not mean the work
    is finished, so mark the start step completed only after you got the final result (a state other
    than running) with check_script_job. Add a separate "check the result" step besides the start
    step.

    If you pass detail_markdown, it is shown as is in the main chat separately from steps,
    and is also saved as a detailed plan Markdown file under data/plans/
    (one file per conversation thread, overwritten on every call; update_task_progress does not
    update it). For complex tasks where you want to show the user background, design decisions or
    findings that do not fit in the checklist panel, passing this argument is recommended
    (omitting it is not an error).

    Args:
        steps: A list of dicts, one per plan step (at least one, in execution order).
            Each dict must have these two keys.
            - content: What the step does (e.g. "Read the settings file").
            - activeForm: A present-progressive description shown in the checklist only while
              the step is in_progress (e.g. "Reading the settings file").
        detail_markdown: An optional Markdown body describing the detailed plan
            (background, design decisions, findings, etc., free form). If omitted (None or an
            empty string), no file is saved.

    Returns:
        A text saying the plan was created and how many steps it has. If detail_markdown was
        passed, the absolute path where it was saved is included too. If steps is empty, or any
        element lacks content / activeForm, it returns an "Error: ..." string instead of raising.
    """
    if not steps:
        return "Error: steps is empty. Specify at least one step."
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or not s.get("content") or not s.get("activeForm"):
            return f"Error: steps[{i}] needs both content and activeForm " f"as strings: {s!r}"
    plan = [{"content": s["content"], "activeForm": s["activeForm"], "status": "pending"} for s in steps]
    # 既に承認済み（Edit Automatically）だった場合にこの呼び出しでも承認状態を
    # 維持するかは config.ini の [plan].reset_approval_on_recreate で切り替え可能
    # （既定 True＝従来通り常にリセットして Plan Mode へ戻す）。未承認状態からの
    # 呼び出しは元々 False なので、この設定に関わらず結果は変わらない。
    still_approved = bool(cl.user_session.get("plan_approved")) and not _state._PLAN_RESET_APPROVAL_ON_RECREATE
    cl.user_session.set("plan", plan)
    cl.user_session.set("plan_approved", still_approved)
    cl.user_session.set("awaiting_approve_plan_call", not still_approved)
    # send() 失敗（通信エラー等）でも plan/plan_approved の永続化自体は
    # 効かせるため、send() より前に呼ぶ（src/plan_persist.py docstring、
    # 2026-08-24 ユーザー報告参照）。
    await persist_plan_state()
    # id は thread_id から決定的に導出する（app.py の on_chat_resume と
    # 同じ id を使うことで steps テーブルへの永続化がUPSERTとして働き、
    # 重複行が残らない。src/plan_persist.py plan_message_id docstring参照）。
    # thread_id が取れない異常系（テスト/evalハーネスからの直接呼び出し等、
    # on_chat_start/on_chat_resumeを経由していないセッション）では、固定の
    # ダミー文字列にフォールバックすると全セッションが同じidに収束し、
    # 無関係なセッション同士が同じsteps行を奪い合って上書きし合う
    # （2026-09-04 レビュー指摘）。ランダムUUIDにフォールバックして衝突を防ぐ。
    _thread_id = cl.user_session.get("thread_id")
    message = cl.Message(
        id=plan_message_id(_thread_id) if _thread_id else str(uuid.uuid4()),
        content=_render_plan_payload(plan, approved=still_approved),
    )
    await message.send()
    cl.user_session.set("plan_message", message)
    logger.info(
        "create_plan: %d steps, detail_markdown=%d chars, still_approved=%s",
        len(steps),
        len(detail_markdown or ""),
        still_approved,
    )

    if still_approved:
        result = f"Updated the plan ({len(steps)} steps). It stays approved, so you can continue with the write tools without calling approve_plan."
    else:
        result = f"Created the plan ({len(steps)} steps). Get the user's approval with approve_plan."
    if detail_markdown and detail_markdown.strip():
        # プレフィックス無しの通常メッセージとして送る（PLAN_PREFIX 付きの
        # チェックリストと違い、messageTree.ts の selectMainThread でサイドパネル
        # 側へ除外されず、メインチャットにそのまま表示される）。
        await cl.Message(content=detail_markdown.rstrip()).send()
        try:
            plan_path = _write_plan_detail(plan, detail_markdown)
        except OSError as e:
            result += f"\n(Failed to save the detailed plan: {e})"
        else:
            if plan_path is not None:
                result += f"\nSaved the detailed plan: {plan_path}"
    return result
