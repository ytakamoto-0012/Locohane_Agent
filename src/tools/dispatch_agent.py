"""dispatch_agent ツール。"""

from __future__ import annotations

from langchain_core.tools import InjectedToolCallId, tool
from typing import Annotated
import asyncio
import chainlit as cl
import json
import logging
import time
import uuid

from . import _dispatch_agent_job
from . import _state
from ._dispatch_agent_job import _DispatchAgentJob, _dispatch_agent_job_started_message, _finalize_dispatch_agent_job_result, _purge_stale_dispatch_agent_jobs, _run_dispatch_agent_job
from ._path_memory_helpers import _resolve_path_memory_tokens_in_text
from ._plan_render import current_plan_status_text
from ._safe_path import _safe_path
from ._workdir import _resolve_workdir
from .write_scratch_note import sanitize_run_id

logger = logging.getLogger(__name__)


def _task_with_work_dir_hint(task: str) -> str:
    """dispatch_agent の task 文の先頭に、実際の作業ディレクトリを事実として付与する。

    委譲元（メインエージェント）が task 文中に書いたパスは、モデルの記憶からの
    書き起こしで誤っていたり未検証だったりしうる（本番実例: 存在しない
    "E:\\user_a\\レシピ\\md" を渡され、サブエージェントが自力でGlobして偶然
    見つけた無関係な "E:\\user_a\\md" を答えとして扱ってしまった）。サブエージェントは
    メインエージェントの会話文脈を一切持たないため、この事実を照合する独自の
    手掛かりを持たない。_resolve_workdir() が返す確認済みの作業ディレクトリを
    task 本文の先頭へ機械的に付与することで、サブエージェント自身のツール呼び出しの
    挙動やプロンプト内指示への追従性に頼らず、常に正しいground truthを与える。

    _resolve_workdir() は init_tools() 未実行時のみ RuntimeError を送出するが、
    ヒント注入1つの失敗で dispatch_agent 全体を失敗させないよう、ここで
    握りつぶして task をそのまま返す。

    absolute_path はラベル文字列と同じ行に連結せずJSONの独立したキーに
    分離する。低パラメータモデルは「作業ディレクトリ: D:\\x」のような
    ラベル+コロン+パスの自然文だと、ラベル込みで1つのパス文字列として
    誤認識しGlob等へそのまま渡してしまう事例があったため
    （_build_work_dir_notice と同じ理由、app.py 参照）。
    """
    try:
        work_dir = _resolve_workdir()
    except RuntimeError:
        return task
    info = json.dumps({"absolute_path": str(work_dir)}, ensure_ascii=False)
    return f"[Working directory]\n{info}\n\n{task}"

def _task_with_plan_hint(task: str) -> str:
    """dispatch_agent の task 文の先頭に、現在の実行計画を事実として付与する。

    本番インシデント: create_plan の detail_markdown（プロース）は1ファイル
    成果物を約束していたが、実際の steps は月間・週間版を別々の
    dispatch_agent 呼び出しへ分割委譲しており、委譲されたworkerは
    「月間版を作って」としか伝えられず計画全体の約束を知る術が無かった。
    _task_with_work_dir_hint と同じ「委譲元の書き起こしを信用せず、
    cl.user_session の ground truth を機械的に注入する」パターンをここにも
    適用し、委譲元がtask文に計画全体を書き忘れてもサブエージェントが
    常に計画全体・現在位置を認識できるようにする。
    """
    status = current_plan_status_text()
    if not status:
        return task
    return f"[Plan (in progress, highest priority task)]\n{status}\n\n{task}"

def _task_with_orchestrator_skill_hint(task: str, orchestrator_skill: str) -> tuple[str | None, str | None]:
    """orchestrator_skill 指定時、その SKILL.md 全文を task の先頭に機械的に注入する。

    複数SKILLを横断的に指示するオーケストレーター役のSKILL.mdは、委譲元
    （メインエージェント）がその内容を要約してtask文へ詰め込もうとすると、
    必須ルール・禁止事項のような細部が委譲元自身の要約時に脱落しうる
    （LLMによる自由記述の要約は本質的にlossy）。_task_with_work_dir_hint /
    _task_with_plan_hint と同じ「委譲元の書き起こしを信用せず、ground truth
    を機械的に注入する」パターンをここにも適用し、SKILL.md本文そのものを
    システム側が直接読み込んで先頭に付与することで、LLMによる要約・
    コピーの工程を経由させない。

    Returns:
        (注入済みのtask, エラーメッセージ) のタプル。成功時は (task, None)、
        失敗時は (None, "エラー: ...")。空文字・空白のみは未指定扱いで task をそのまま返す
        （LLMが「省略」のつもりで "" を渡すことがあり、"/SKILL.md" 扱いでサンドボックス外エラーになっていた）。
    """
    orchestrator_skill = orchestrator_skill.strip()
    if not orchestrator_skill:
        return task, None
    try:
        skill_md = _safe_path(f"{orchestrator_skill}/SKILL.md")
    except ValueError as e:
        return None, f"Error: {e}"
    if not skill_md.is_file():
        return None, (
            f"Error: SKILL.md of orchestrator skill '{orchestrator_skill}' not found. "
            "The skill name is very likely wrong."
        )
    content = skill_md.read_text(encoding="utf-8")
    return f"[Orchestrator skill: {orchestrator_skill}]\n{content}\n\n{task}", None

@tool
async def dispatch_agent(
    task: str,
    agent_type: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    orchestrator_skill: str | None = None,
    model: str | None = None,
) -> str:
    """Delegate a task to an independent sub-agent and receive only its final answer.

    Use it for work whose detailed reasoning and tool call history do not need to stay in your
    own conversation history, such as investigation or multi-step research. The sub-agent works
    autonomously with the tool set of the type chosen by agent_type, but its internal reasoning and
    tool calls do not remain in your conversation history at all; only the text of its final
    answer is returned (internal records remain in the log file). If run_script is called, its
    approval prompt is shown to the user as is while the sub-agent runs. For investigations
    involving dozens to hundreds of files (images, etc.), it is efficient to split the work by
    year, subfolder, etc. and delegate each part to this tool.
    A sub-agent cannot delegate further to another sub-agent.

    This turn itself blocks until completion (unless the safety limit below is reached).
    While waiting, progress (elapsed time, iteration count, progress notes) is automatically
    posted to the chat for humans, so you do not need to poll by calling it repeatedly.

    Only if it still has not finished after the configured safety limit
    ([subagent].background_inline_wait_max_seconds), it returns a notice including the job_id and
    ends this turn (the job itself keeps running in the background). Only in that case, use
    check_dispatch_agent_job (to get the result) and stop_dispatch_agent_job (only when explicitly
    told to interrupt) in later turns.

    Args:
        task: A description of the task for the sub-agent. Include the necessary background and
            the expected output format, neither too much nor too little (the sub-agent knows
            nothing about this conversation). Do not write target paths from memory; you may embed
            the `@N` (path memory reference) obtained from a previous glob etc. as is in the text
            (resolvable ones are replaced with the real paths automatically).
        agent_type: The name of the sub-agent type to use (required; there is no implicit
            default). See the list in the system prompt for the available types and their uses,
            and explicitly choose the type that fits the task every time.
        orchestrator_skill: Specify the skill name only when you are working in the context of
            an orchestrator SKILL.md that instructs using several skills across the board (one
            that declares itself, e.g. "This skill is an orchestrator skill", at the beginning of
            its body). The whole body of that SKILL.md is then embedded as is at the top of task,
            so the mandatory rules and prohibitions reach the sub-agent without being summarized.
            Omit it for normal delegation. If the skill name does not exist, it returns
            "Error: ..." before starting (no job is created in that case).
        model: The model name for the sub-agent to use. Specify it only when the user specified
            a model; normally omit it (the agent_type default is used). Unknown model names are
            ignored.

    Returns:
        Normally the sub-agent's final answer text. Only when the safety limit is reached, a
        notice string including the job_id. If init_tools() has not been run or agent_type is
        unknown, it returns "Error: ..." before starting (no job is created in that case).
        If running the sub-agent itself fails, it also returns an "Error: ..." string instead
        of raising.
    """
    if _state._LLM_CONFIG is None:
        return "Error: init_tools() has not been run"
    resolved = _state._AGENT_TYPES.get(agent_type)
    if resolved is None:
        available = ", ".join(sorted(_state._AGENT_TYPES)) or "(none registered)"
        return f"Error: unknown agent_type '{agent_type}'. Available: {available}"
    task = _resolve_path_memory_tokens_in_text(task)
    task = _task_with_work_dir_hint(task)
    task = _task_with_plan_hint(task)
    orchestrator_skill = (orchestrator_skill or "").strip() or None
    if orchestrator_skill is not None:
        injected_task, error = _task_with_orchestrator_skill_hint(task, orchestrator_skill)
        if error:
            return error
        task = injected_task
    model = (model or "").strip() or None
    logger.info(
        "dispatch_agent: task=%r agent_type=%r orchestrator_skill=%r model=%r",
        task,
        agent_type,
        orchestrator_skill,
        model,
    )

    _purge_stale_dispatch_agent_jobs()

    job = _DispatchAgentJob(
        thread_id=cl.user_session.get("thread_id") or "",
        # run_id はこの呼び出し自身の tool_call_id から作る（uuid4の新規発行
        # ではなく）。強制停止で孤立tool_callになった際、app.py側がtc["id"]
        # から同じ sanitize_run_id() で run_id を再計算でき、退避先
        # scratch_notesファイルの絶対パスを案内文に埋め込めるようにするため
        # （_build_orphaned_placeholder_messages 参照）。
        run_id=sanitize_run_id(tool_call_id),
        agent_type=agent_type,
        task_preview=task[:200],
        started_at=time.monotonic(),
        status="running",
        result=None,
        error_message=None,
        max_iterations=_state._SUBAGENT_MAX_ITERATIONS,
        model=model,
    )
    job_id = uuid.uuid4().hex[:12]
    job.runner_task = asyncio.create_task(_run_dispatch_agent_job(job, job_id, task, resolved))
    _dispatch_agent_job._DISPATCH_AGENT_JOBS[job_id] = job

    # asyncio.shield: このwait_forがタイムアウトして例外を送出しても、
    # job.runner_task 自体はキャンセルされず裏側で動き続ける
    # （安全上限超過時のフォールバック挙動の要）。0以下は無期限待ち。
    wait_timeout = _state._DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS if _state._DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS > 0 else None
    try:
        await asyncio.wait_for(asyncio.shield(job.runner_task), timeout=wait_timeout)
    except asyncio.TimeoutError:
        # job.runner_task はこの時点でまだ完了していない（完了済みならTimeoutErrorは
        # 発生しない）ため、ジョブ側のfinally節が実行されるより確実に前にこの
        # 代入が間に合う。
        job.turn_still_waiting = False
        logger.warning(
            "dispatch_agent: reached the safety limit (%s seconds), returning job_id and ending the turn: job_id=%s",
            wait_timeout,
            job_id,
        )
        return _dispatch_agent_job_started_message(job_id)
    except asyncio.CancelledError:
        # 停止ボタン等でこのターン自体がキャンセルされた場合。job.runner_task は
        # shieldにより生き続けるため、TimeoutError時と同様にリセットを禁止する。
        job.turn_still_waiting = False
        # このCancelledErrorは常に「セッション全体の停止」要求
        # （app.py の on_stop / _stop_thread_generating）と同時に発生し、
        # それらは cancel_dispatch_agent_jobs_for_thread 経由で job.runner_task も
        # 独立に cancel() する（そちらが実際に run_subagent の on_cancelled を
        # 発火させ、緊急退避ファイルを書き込む）。しかし単一イベントループ上では
        # 「メイングラフのタスクへ届くこのCancelledError」の方が
        # 「job.runner_task 自身のキャンセル処理」より確実に先にスケジュール
        # される（stop()ハンドラがsession.current_task.cancel()を呼んでから
        # on_stop()内でjob.runner_task.cancel()を呼ぶ順序のため）。この
        # 例外をそのまま伝播させると、app.py側の孤立tool_call検出が退避
        # ファイル作成より先に走ってしまい、フルパス案内ではなく「退避を
        # 確認できませんでした」のフォールバック文言が常に選ばれてしまう
        # （2026-09-09 実測で確認済みのレース）。ここで job.runner_task 自身の
        # キャンセル・後始末（緊急退避の書き込み含む）が終わるのを待ってから
        # 再送出し、案内文組み立て時には必ず退避ファイルが存在する状態にする。
        if job.runner_task is not None:
            job.runner_task.cancel()
            try:
                await job.runner_task
            except BaseException:  # noqa: BLE001 - job側の結果に関わらずこのCancelledErrorの伝播を優先する
                pass
        raise

    return _finalize_dispatch_agent_job_result(job, job_id)
