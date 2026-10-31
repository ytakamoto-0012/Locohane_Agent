"""dispatch_agent_batch ツール。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import InjectedToolCallId, tool
from pathlib import Path
from typing import Annotated
import asyncio
import chainlit as cl
import logging
import time
import uuid

from .. import skill_drafts
from ..i18n import current_lang, t
from . import _dispatch_agent_job
from . import _state
from ._dispatch_agent_job import _SUBAGENT_MESSAGE_AUTHOR, _DispatchAgentJob, _finalize_dispatch_agent_job_result
from ._dispatch_agent_job import _format_activity, _purge_stale_dispatch_agent_jobs, _run_dispatch_agent_job
from ._dispatch_agent_job import _wait_next_progress_push
from ._path_memory_helpers import _resolve_path_memory_tokens_in_text
from ._safe_path import _resolve_file_tools_path
from ._workdir import _foreign_tmp_dir_names, _resolve_workdir
from .dispatch_agent import _task_with_orchestrator_skill_hint, _task_with_plan_hint, _task_with_work_dir_hint
from .glob_tool import GLOB_PATTERN_ERRORS, _expand_braces, glob_pattern_error_message
from .thread_notes import write_thread_note
from .write_scratch_note import sanitize_run_id
from ..subagent import is_llm_unreachable_result, is_truncated_result

logger = logging.getLogger(__name__)

# 戻り値（メインエージェントの会話履歴に積まれる）へ載せる、グループごとの
# サブエージェント最終回答の最大文字数。全文は thread note 側へ保存する。
# workerが書き出したファイル名の一覧表まで返してくると、20グループ分で
# メインの1リクエストあたりのトークンが当時の目標（64000）を超えた実測があるため
# （evals/tuning_log.md par_iter01）、ここで機械的に切り詰める。
_GROUP_RESULT_PREVIEW_CHARS = 400

# 各グループのジョブを包む Runnable の run_name。app.py はこの名前の
# on_chain_start/on_chain_end でグループごとの中間Stepを作り、配下のサブエージェントの
# 思考・ツールStepをその下へ入れる（全グループが1つの「SUB: worker（一括）」の下に
# 区別なく並び、どのグループの処理か分からなかったため）。表示名は metadata の
# "batch_group_label"。
BATCH_GROUP_RUN_NAME = "dispatch_agent_batch_group"

# 安全上限超過で先にターンを終えた後も、全グループ完了まで動き続ける
# 集約進捗pushタスクへの参照（asyncio はタスクを弱参照しか持たないため、
# ここで保持しないと実行途中でGCされうる）。
_BATCH_PROGRESS_TASKS: set[asyncio.Task] = set()

# LLMに届かないグループが続いたため、起動しなかったグループに付ける理由。
_UNREACHABLE_SKIP_REASON = (
    "Not started because a group could not connect to the LLM server (timeouts/communication errors persisted after retries)."
)


def interrupted_note_topic(tool_call_id: str) -> str:
    """停止ボタン等で中断した時点の結果を保存する thread note の topic 名。

    app.py は孤立した tool_call の id からこの topic を求め、次のターンへの案内に載せる。
    """
    return f"dispatch_agent_batch results at interruption ({sanitize_run_id(tool_call_id)})"


@dataclass
class _UnreachableGuard:
    """LLMに届かずに打ち切られたグループ数を数え、閾値に達したら残りのグループの起動を止める。

    LLMサーバーが落ちたままだと、各グループが「タイムアウト×再試行回数」ずつ待ってから
    打ち切られ、結果が分かりきった待ち時間がグループ数ぶん続くため
    （[subagent].batch_stop_after_unreachable_groups。0以下なら止めない）。
    """

    threshold: int
    count: int = 0

    def skip_reason(self) -> str | None:
        if self.threshold > 0 and self.count >= self.threshold:
            return _UNREACHABLE_SKIP_REASON
        return None


def _is_subfolder_pattern(pattern: str) -> bool:
    """pattern がサブフォルダ階層を含むか（例: "*/ocr_md/*.md"、"**/*.jpg"）。"""
    return "/" in pattern


def _relative_sort_key(p: Path, resolved_base: Path) -> str:
    try:
        return p.relative_to(resolved_base).as_posix().casefold()
    except ValueError:
        return p.name.casefold()


def _relative_display(p: Path, base: Path) -> str:
    """結果の見出し用。サブフォルダ探索時は別フォルダの同名ファイルを区別できるよう相対パスで示す。"""
    try:
        return p.relative_to(base.resolve()).as_posix()
    except ValueError:
        return p.name


def _list_target_files(base: Path, pattern: str) -> list[Path]:
    """base 配下で pattern に一致するファイルを、base からの相対パス順で返す。

    Glob ツール（glob_search）は更新日時降順で返すため、メインエージェントが
    「1〜15件目」のような範囲で各サブエージェントへ指示すると、受け取った側が
    自分でGlobし直した並びと一致する保証が無い。ここではグループ分けまで
    ハーネス側で行い、各グループへは絶対パスそのものを渡すため、並び順は
    人間が追いやすく再現性のある相対パス順（大文字小文字を区別しない）に固定する
    （同じフォルダのファイルが同じグループにまとまる）。

    pattern が "/" を含まなければ Glob と同じく base 直下のみが対象。"/" を含む
    場合（"*/ocr_md/*.md"、"**/*.jpg" 等）はその階層まで探索する。年度フォルダ/
    ocr_md のような入れ子構成で "**/ocr_md/*.md" を渡され、直下のみの探索で
    0件エラーになった本番実例（2026-10-02）への対応。Glob が直下限定なのは
    LLMが何度も呼ぶツールの応答時間を抑えるためで、1回呼ぶだけの本ツールには
    当てはまらない（大量に一致した場合は [subagent].batch_max_groups が起動前に止める）。
    `{a,b}` のブレース展開と他セッションの一時ディレクトリ除外は Glob と揃える。

    pathlib の glob は pattern 中の ".." をそのまま辿る（"../other/*.jpg" で base の
    外が一致する）ため、解決後に base 配下でないもの（シンボリックリンク経由で外へ
    出るものを含む）は除く。
    """
    exclude_names = _foreign_tmp_dir_names()
    # 他ユーザーのドラフトスキル（読み取り不可のもの）は場所で除外する（src/skill_drafts.py）。
    exclude_dirs = skill_drafts.unreadable_dirs(_state._LLM_CONFIG, skill_drafts.current_draft_user())
    resolved_base = base.resolve()
    seen: set[Path] = set()
    files: list[Path] = []
    for expanded in _expand_braces(pattern.replace("\\", "/")):
        subfolders = _is_subfolder_pattern(expanded)
        for p in base.glob(expanded):
            if not p.is_file():
                continue
            if not subfolders and p.parent.resolve() != resolved_base:
                continue
            if exclude_names and set(p.parts) & exclude_names:
                continue
            resolved = p.resolve()
            if not resolved.is_relative_to(resolved_base):
                continue
            if any(resolved.is_relative_to(d) for d in exclude_dirs):
                continue
            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)
    files.sort(key=lambda p: _relative_sort_key(p, resolved_base))
    return files


def _workdir_folders_hint() -> str:
    """対象フォルダが見つからない時に、作業ディレクトリ直下のフォルダ名を添える。

    作業ディレクトリ自体の親フォルダ名（例: annual_schedule_large）を path に渡して
    「見つかりません」になり、もう1往復して正しいパスを探し直した実測があるため
    （evals/tuning_log.md batch_iter07）、相対パスでそのまま渡せる候補を示す。
    """
    try:
        workdir = _resolve_workdir()
        names = sorted((p.name for p in workdir.iterdir() if p.is_dir() and p.name not in _foreign_tmp_dir_names()), key=str.casefold)
    except (OSError, RuntimeError):
        return ""
    shown = ", ".join(names[:20]) + ("…" if len(names) > 20 else "")
    return (
        f"\nFolders directly under the working directory ({workdir}): {shown or '(none)'}. "
        "Pass a path relative to the working directory in path (e.g. one of the folder names above, or \".\" for the working directory itself)."
    )


def _file_listing(group: list[Path]) -> str:
    return "\n".join(f"- {p}" for p in group)


def _group_task(task: str, group: list[Path], index: int, group_count: int, start: int, total: int) -> str:
    """共通の指示文に、そのグループの担当ファイル（絶対パス）を機械的に付け足す。

    メインエージェントにファイル名を書き写させると、後半のグループで実在しない
    連番ファイル名を捏造した実測がある（evals/tuning_log.md par_iter01）ため、
    担当範囲はLLMを経由させずここで確定させる。
    """
    end = start + len(group) - 1
    return (
        f"{task}\n\n"
        f"[Assigned files (group {index}/{group_count}, {len(group)} files; items {start} to {end} of {total})]\n"
        f"{_file_listing(group)}\n"
        "Process only the assigned files above (other files are processed in parallel by other sub-agents). "
        "In your final answer, return only the number processed, the number written, and the failed targets with reasons, concisely (do not list every file name)."
    )


def _is_active(job: _DispatchAgentJob) -> bool:
    """ジョブがまだ実行中（または順番待ち）か。

    runner_task が status を書き換えずにキャンセルされた場合（停止経路の
    一部）も終了扱いにし、集約進捗pushが永久に回り続けないようにする。
    """
    return job.status == "running" and job.runner_task is not None and not job.runner_task.done()


def _group_status(job: _DispatchAgentJob, result: str) -> str:
    if job.run_started_at is None:
        # 順番待ちのまま停止された、または _UnreachableGuard により起動しなかった。
        return "not started"
    if job.status == "error" or result.startswith("Error:"):
        return "error"
    if job.status == "killed":
        return "killed"
    if is_truncated_result(job.result or ""):
        return "truncated"
    return "done"


def _format_batch_progress(jobs: list[tuple[str, _DispatchAgentJob]], started_at: float) -> str:
    """全グループをまとめた進捗表示（グループ数分のメッセージを並べないため1件に集約する）。"""
    active = [(index, job_id, job) for index, (job_id, job) in enumerate(jobs, start=1) if _is_active(job)]
    running = [(index, job_id, job) for index, job_id, job in active if job.run_started_at is not None]
    waiting = len(active) - len(running)
    elapsed = int(time.monotonic() - started_at)
    # 画面への進捗push 専用なので、セッションの表示言語で組み立てる。
    lang = current_lang()
    lines = [
        t(
            "progress.batch",
            lang,
            elapsed=elapsed,
            total=len(jobs),
            done=len(jobs) - len(active),
            running=len(running),
            waiting=waiting,
        )
    ]
    for index, job_id, job in running:
        lines.append(
            t(
                "progress.batch_group",
                lang,
                index=index,
                iteration=job.current_iteration,
                max_iterations=job.max_iterations,
                job_id=job_id,
            )
            + _format_activity(job, lang)
        )
    return "\n".join(lines)


async def _push_batch_progress(jobs: list[tuple[str, _DispatchAgentJob]], started_at: float) -> None:
    """_push_dispatch_agent_progress のバッチ版（同じく1件のメッセージを .update() で書き換える）。

    安全上限超過でツール自体が先に返った後も、全グループが終わるまで動き続ける。
    送信失敗（セッション切断等）は進捗表示を諦めるだけで、ジョブには影響させない。
    """
    message: "cl.Message | None" = None
    try:
        while any(_is_active(job) for _, job in jobs):
            await _wait_next_progress_push(
                [job for _, job in jobs],
                _state._DISPATCH_AGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS,
                early_on_activity=message is not None,
            )
            if not any(_is_active(job) for _, job in jobs):
                break
            for _, job in jobs:
                job.activity_changed.clear()
            content = _format_batch_progress(jobs, started_at)
            if message is None:
                message = cl.Message(content=content, author=_SUBAGENT_MESSAGE_AUTHOR, metadata={"ephemeral_progress": True})
                await message.send()
            else:
                message.content = content
                await message.update()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 進捗表示の失敗でfire-and-forgetタスクの例外警告を残さない
        logger.debug("dispatch_agent_batch: failed to push progress", exc_info=True)


async def _run_group_runnable(
    job: _DispatchAgentJob, job_id: str, text: str, resolved, label: str, guard: _UnreachableGuard
) -> None:
    """1グループ分のジョブを、名前付きの Runnable として実行する。

    asyncio.create_task はこのツール実行中のコンテキスト（LangChain のコールバック
    設定を含む）をコピーするため、Runnable の実行はこのツールの子runとして
    astream_events に現れ、配下のサブエージェントのイベントの parent_ids には
    このグループの run_id が入る（app.py の _resolve_parent_id/_stream_owner 参照）。
    戻り値（on_chain_end の output）はグループStepの出力表示用の短い文字列。
    """

    async def _run(_: object) -> str:
        await _run_dispatch_agent_job(job, job_id, text, resolved, should_skip=guard.skip_reason)
        if is_llm_unreachable_result(job.result):
            guard.count += 1
        if job.status == "error":
            return f"Error: {job.error_message or ''}"[:_GROUP_RESULT_PREVIEW_CHARS]
        return (job.result or "")[:_GROUP_RESULT_PREVIEW_CHARS]

    await RunnableLambda(_run).ainvoke(
        None,
        config={"run_name": BATCH_GROUP_RUN_NAME, "metadata": {"batch_group_label": label}},
    )


def _collect_group_results(
    jobs: list[tuple[str, _DispatchAgentJob]], groups: list[list[Path]], size: int, base: Path
) -> tuple[list[str], list[str], list[str], dict[str, int]]:
    """各グループの状態を集計し、終わったグループのジョブをレジストリから取り除く。

    Returns:
        (summary_lines, full_lines, pending, counts)。summary_lines は戻り値用
        （結果は先頭のみ）、full_lines は thread note 用（全文）、pending は
        まだ実行中のグループの job_id、counts は状態ごとのグループ数。
    """
    summary_lines: list[str] = []
    full_lines: list[str] = []
    pending: list[str] = []
    counts: dict[str, int] = {}
    for index, ((job_id, job), group) in enumerate(zip(jobs, groups), start=1):
        start = (index - 1) * size + 1
        first, last = (_relative_display(p, base) for p in (group[0], group[-1]))
        label = f"Group {index} (items {start} to {start + len(group) - 1}, {len(group)} files, {first} to {last})"
        if _is_active(job):
            # 安全上限超過でまだ終わっていないグループ。結果は check_dispatch_agent_job で取得する。
            pending.append(job_id)
            counts["running"] = counts.get("running", 0) + 1
            summary_lines.append(f"### {label}: running (job_id={job_id})\nAssigned files:\n{_file_listing(group)}")
            continue
        if job.status == "running":
            # status を書き換えられないままキャンセルで終わったジョブ。
            job.status = "killed"
        if job.run_started_at is None:
            # 起動していないので「強制終了されました」等の文言は付けない。
            _dispatch_agent_job._DISPATCH_AGENT_JOBS.pop(job_id, None)
            result = job.error_message or "Interrupted before starting because it was stopped."
        else:
            result = _finalize_dispatch_agent_job_result(job, job_id)
        status = _group_status(job, result)
        counts[status] = counts.get(status, 0) + 1
        header = f"### {label}: {status}"
        preview = result if len(result) <= _GROUP_RESULT_PREVIEW_CHARS else result[:_GROUP_RESULT_PREVIEW_CHARS] + "... (rest omitted)"
        if status == "done":
            summary_lines.append(f"{header}\n{preview}")
        else:
            # 再委任時にLLMがファイル名を推測で補わないよう、担当ファイルをそのまま返す。
            summary_lines.append(f"{header}\n{preview}\nAssigned files:\n{_file_listing(group)}")
        full_lines.append(f"{header}\nAssigned files:\n{_file_listing(group)}\n\n{result}")
    return summary_lines, full_lines, pending, counts


def _save_interrupted_note(
    tool_call_id: str, jobs: list[tuple[str, _DispatchAgentJob]], groups: list[list[Path]], size: int, base: Path
) -> None:
    """停止ボタン等で中断した時点の各グループの状態を thread note へ保存する。

    中断時はツールの戻り値がLLMへ届かず、完了済みグループの結果がどこにも残らない。
    次のターンで全件をやり直したり、どこまで終わったかを調べ直したりしないよう、
    完了済みグループの結果と未完了グループの担当ファイルを残す
    （topic は interrupted_note_topic。app.py が次のターンの案内に載せる）。
    保存の失敗は CancelledError の再送出を妨げない。
    """
    try:
        _, full_lines, _, counts = _collect_group_results(jobs, groups, size, base)
        count_text = ", ".join(f"{k} {v}" for k, v in counts.items())
        head = (
            f"State at the interruption of the batch delegation of {base} ({len(groups)} groups) ({count_text}). "
            "Groups marked \"done\" are finished; do not redo them. For the other groups, "
            "put their list of assigned files as is into task and re-delegate with dispatch_agent."
        )
        write_thread_note.invoke({"topic": interrupted_note_topic(tool_call_id), "content": "\n\n".join([head, *full_lines])})
    except Exception:  # noqa: BLE001 - 中断処理そのものは止めない
        logger.exception("dispatch_agent_batch: could not save the results at interruption to the thread note")


@tool
async def dispatch_agent_batch(
    task: str,
    agent_type: str,
    pattern: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    path: str = "",
    group_size: int = 15,
    orchestrator_skill: str | None = None,
    model: str | None = None,
) -> str:
    """Split work that applies the same processing to many files in a folder into groups automatically and delegate them in parallel to several sub-agents.

    When applying the same instruction to many files, such as "analyze every image in the images
    folder and write it out to md", call this once instead of calling dispatch_agent yourself many
    times. This tool gets the file list, splits it into groups of group_size, assigns the files to
    each sub-agent, runs them in parallel and collects the results. You do not need to list file
    names yourself or call dispatch_agent separately for each group.

    The task text passed to each sub-agent automatically gets "assigned files (a list of absolute
    paths)" appended after the common instruction. Write in task only the instruction common to
    all groups (output destination, output format, file naming rules, skip conditions, etc.).

    Choose agent_type by the same criteria as dispatch_agent. For example: explore if it only reads
    many files to extract or summarize their contents, worker if it reads and writes, verifier to
    verify many generated files.

    When using it with an agent_type that writes (worker, etc.), get approval first with
    create_plan -> approve_plan, as with dispatch_agent (one plan step such as "process them in
    bulk with this tool" is enough).

    Args:
        task: The instruction common to all groups. You may embed `@N` for target paths.
        agent_type: The name of the sub-agent type to use (same as dispatch_agent).
        pattern: A glob pattern for the target files (e.g. "*.{jpg,jpeg,png,heic}").
            Write it relative to path (do not write absolute paths; pass the folder in path).
            Without "/", only files directly under path are targeted. To include subfolders,
            write e.g. "*/ocr_md/*.md" (in the ocr_md folder one level down) or "**/*.jpg"
            (all levels). Writing either the upper- or lower-case extension matches both.
        path: The absolute path of the target folder (`@N` allowed). Defaults to the working
            directory.
        group_size: The number of files per sub-agent (default 15; 15 or fewer when image
            analysis is involved). Clamped to 1..[subagent].max_iterations.
            If the number of groups exceeds the configured limit, it fails before starting (the
            limit is shown in the error).
        orchestrator_skill: Same as dispatch_agent (normally omitted).
        model: Same as dispatch_agent (normally omitted).

    Returns:
        A text summarizing the result of each group (its status and the beginning of its final
        answer). Groups marked "error", "truncated", "killed" or "not started" also list their
        assigned files (when re-delegating, put that list as is into the task of
        dispatch_agent). The full final answer of each group is saved to a thread note, and the
        topic name is shown at the end. If the safety limit
        ([subagent].background_inline_wait_max_seconds) is reached, it returns the results of the
        finished groups plus the job_ids of the groups still running (get their results later
        with check_dispatch_agent_job). If there are no target files, agent_type is unknown, etc.,
        it returns "Error: ..." before starting (no job is created in that case).
    """
    if _state._LLM_CONFIG is None:
        return "Error: init_tools() has not been run"
    resolved = _state._AGENT_TYPES.get(agent_type)
    if resolved is None:
        available = ", ".join(sorted(_state._AGENT_TYPES)) or "(none registered)"
        return f"Error: unknown agent_type '{agent_type}'. Available: {available}"
    base, error = _resolve_file_tools_path(path)
    if error:
        return f"Error: {error}"
    if not base.is_dir():
        hint = "(`@N` is example notation. Pass an actual number such as `@12`, or a path relative to the working directory such as `images`)" if "@N" in path else ""
        return f"Error: target folder not found: {base}{hint}{_workdir_folders_hint()}"
    try:
        # "**" の再帰探索は大きなフォルダで時間がかかるため、イベントループ（他セッション・
        # 進捗表示を含むサーバー全体）を止めないよう別スレッドで行う。
        files = await asyncio.to_thread(_list_target_files, base, pattern)
    except GLOB_PATTERN_ERRORS as e:
        return f"Error: {glob_pattern_error_message(e)}"
    if not files:
        hint = "" if _is_subfolder_pattern(pattern) else "(For files in subfolders, include the levels, e.g. \"*/folder-name/*.md\" or \"**/*.jpg\")"
        return f"Error: no files under {base} match pattern '{pattern}'. {hint}"

    size = max(1, min(group_size, _state._SUBAGENT_MAX_ITERATIONS))
    groups = [files[i : i + size] for i in range(0, len(files), size)]
    # 1回の呼び出しで起動するグループ（=サブエージェントのジョブ）数の上限
    # （[subagent].batch_max_groups。0以下なら無制限）。pattern="*" で大きなフォルダを
    # 指定した場合等に、確認なしで数百件のジョブが起動されるのを防ぐ。
    max_groups = _state._DISPATCH_AGENT_BATCH_MAX_GROUPS
    if max_groups > 0 and len(groups) > max_groups:
        return (
            f"Error: the {len(files)} files under {base} matching pattern '{pattern}' would make "
            f"{len(groups)} groups ({size} each), exceeding the per-call limit ({max_groups} groups). "
            "Narrow the pattern, or split the targets into subfolders and call it several times (no jobs were started)."
        )

    common = _resolve_path_memory_tokens_in_text(task)
    task_texts: list[str] = []
    for index, group in enumerate(groups, start=1):
        start = (index - 1) * size + 1
        text = _group_task(common, group, index, len(groups), start, len(files))
        text = _task_with_work_dir_hint(text)
        text = _task_with_plan_hint(text)
        if orchestrator_skill is not None:
            text, error = _task_with_orchestrator_skill_hint(text, orchestrator_skill)
            if error:
                return error
        task_texts.append(text)

    model = (model or "").strip() or None
    logger.info(
        "dispatch_agent_batch: agent_type=%r base=%s pattern=%r files=%d groups=%d group_size=%d model=%r",
        agent_type,
        base,
        pattern,
        len(files),
        len(groups),
        size,
        model,
    )

    _purge_stale_dispatch_agent_jobs()

    thread_id = cl.user_session.get("thread_id") or ""
    batch_started_at = time.monotonic()
    jobs: list[tuple[str, _DispatchAgentJob]] = []
    guard = _UnreachableGuard(threshold=_state._DISPATCH_AGENT_BATCH_STOP_AFTER_UNREACHABLE_GROUPS)
    for index, text in enumerate(task_texts, start=1):
        job = _DispatchAgentJob(
            thread_id=thread_id,
            # dispatch_agent と同じく tool_call_id 由来にする（グループ番号で一意化）。
            # app.py の _dispatch_agent_rescue_note_paths はこの命名規則で退避ファイルを探す。
            run_id=sanitize_run_id(f"{tool_call_id}_g{index}"),
            agent_type=agent_type,
            task_preview=text[:200],
            started_at=batch_started_at,
            status="running",
            result=None,
            error_message=None,
            max_iterations=_state._SUBAGENT_MAX_ITERATIONS,
            # 進捗はこのツールが全グループ分を1件にまとめてpushする。
            push_progress=False,
            model=model,
        )
        job_id = uuid.uuid4().hex[:12]
        # 同時実行数は _run_dispatch_agent_job 内のセッション単位セマフォ
        # （[subagent].max_parallel）がそのまま制御する。
        group = groups[index - 1]
        label = f"SUB: {agent_type} (group {index}/{len(groups)}, {len(group)} files)"
        job.runner_task = asyncio.create_task(_run_group_runnable(job, job_id, text, resolved, label, guard))
        _dispatch_agent_job._DISPATCH_AGENT_JOBS[job_id] = job
        jobs.append((job_id, job))

    progress_task = asyncio.create_task(_push_batch_progress(jobs, batch_started_at))
    _BATCH_PROGRESS_TASKS.add(progress_task)
    progress_task.add_done_callback(_BATCH_PROGRESS_TASKS.discard)

    runner_tasks = [job.runner_task for _, job in jobs]
    inline_wait = _state._DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS
    wait_timeout = inline_wait if inline_wait > 0 else None
    timed_out = False
    try:
        # dispatch_agent と同じく shield で包み、安全上限超過時もジョブ自体は裏で続行させる。
        await asyncio.wait_for(asyncio.shield(asyncio.gather(*runner_tasks, return_exceptions=True)), timeout=wait_timeout)
    except asyncio.TimeoutError:
        timed_out = True
        for _, job in jobs:
            job.turn_still_waiting = False
        # 集約進捗pushは止めず、残りのグループが終わるまで人間向けの表示を続ける。
    except asyncio.CancelledError:
        # dispatch_agent と同じ理由（緊急退避の書き込み完了を待ってから再送出する）。
        progress_task.cancel()
        for _, job in jobs:
            job.turn_still_waiting = False
            if job.runner_task is not None:
                job.runner_task.cancel()
        await asyncio.gather(*runner_tasks, progress_task, return_exceptions=True)
        _save_interrupted_note(tool_call_id, jobs, groups, size, base)
        raise
    if not timed_out:
        progress_task.cancel()
        await asyncio.gather(progress_task, return_exceptions=True)

    summary_lines, full_lines, pending, counts = _collect_group_results(jobs, groups, size, base)

    note_line = ""
    if full_lines:
        topic = f"dispatch_agent_batch results ({agent_type}, {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
        try:
            note_result = write_thread_note.invoke({"topic": topic, "content": "\n\n".join(full_lines)})
        except Exception as e:  # noqa: BLE001 - 保存失敗で集計結果そのものを失わない
            logger.exception("dispatch_agent_batch: failed to save to the thread note")
            note_result = f"Error: failed to save to the thread note: {e}"
        note_line = f'The full final answers of the finished groups were saved to thread note "{topic}".' if not note_result.startswith("Error:") else note_result

    count_text = ", ".join(f"{k} {v}" for k, v in counts.items())
    head = (
        f"Split the {len(files)} files of {base} into {len(groups)} groups ({size} each) and "
        f"delegated them in parallel to {agent_type} ({count_text})."
    )
    tail: list[str] = []
    if set(counts) - {"done", "running"}:
        tail.append(
            "For groups marked \"error\", \"truncated\", \"killed\" or \"not started\", put the shown list of assigned files "
            "as is into task and re-delegate with dispatch_agent."
        )
    if "not started" in counts and guard.skip_reason():
        tail.append(
            "Because a group could not connect to the LLM server, the remaining groups were not started. "
            "Before re-delegating, ask the user to check the state of the LLM server."
        )
    if pending:
        logger.warning("dispatch_agent_batch: reached the safety limit (%s seconds), returning job_id: %s", wait_timeout, pending)
        head += f"The safety limit ({wait_timeout} s) was reached, so this turn ends and control is returned for now, leaving {len(pending)} groups still running (the jobs themselves keep running in the background)."
        tail.append(
            f"Running job_ids: {', '.join(pending)}\n"
            "Use check_dispatch_agent_job (with job_id) to check completion and get the results. "
            "Taking a long time is not in itself a reason to stop them."
        )
    return "\n\n".join(part for part in [head, *summary_lines, note_line, *tail] if part)
