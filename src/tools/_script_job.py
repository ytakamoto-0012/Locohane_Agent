"""run_script_background/execute_python_code_background が共有するバックグラウンドジョブ基盤。"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import chainlit as cl

from . import _state
from ._duplicate_guard import _track_failure_streak
from ._path_memory_helpers import _resolve_path_memory_token
from ._python_fs_guard import _register_exec_output_files
from ._safe_path import _is_guard_exempt_script, _resolve_script_filename
from ._state import _SUBAGENT_AGENT_TYPE
from ._subprocess_env import _run_script_guard_env
from ._workdir import _resolve_workdir

logger = logging.getLogger(__name__)

# script_args の分割ミス（例: "--file C:\\aaa.png" を1要素にまとめてしまい、
# スクリプト側のargparseが値を認識できない）は非ゼロ終了として現れる。
# ヒントは終了コードが0以外の場合のみ結果へ付与する（正常終了時は不要）。
_SCRIPT_ARGS_HINT = 'Put each option and its value in separate elements of script_args (e.g. ["--file", "C:\\\\aaa.png"]; do not combine them into one element like ["--file C:\\\\aaa.png"]).'
# script_args が空のまま失敗した場合のヒント。上の分割ミスのヒントだと、引数を渡し忘れた
# （script_args の書き忘れ等）のに @N の解決が壊れていると誤解して呼び方を変え続けた
# （2026-10-31 eval）。
_EMPTY_SCRIPT_ARGS_HINT = (
    "script_args was empty, so the script ran with no arguments. "
    'Pass the script\'s arguments (e.g. the input file "@N") in script_args.'
)


def _script_args_hint(script_args: list[str] | None) -> str:
    """非ゼロ終了時に結果へ付けるヒント。"""
    return _SCRIPT_ARGS_HINT if script_args else _EMPTY_SCRIPT_ARGS_HINT


def _resolve_run_script_command(skill_name: str, script_filename: str, script_args: list[str] | None = None) -> list[str] | str:
    """run_script の前段処理。

    agent_type ごとのスキル/スクリプト制限チェック（_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST）→
    引数のパスメモリー解決 → スクリプトパス解決 → 実行コマンド組み立て、までを行う。
    計画承認チェック・作業ディレクトリ解決は呼び出し側（書き込み版/読み取り専用版で
    要件が異なる）が個別に行う。

    Returns:
        検証に成功すれば実行コマンド（list[str]）。失敗すれば
        「エラー: ...」形式の文字列（呼び出し側はそのまま返せばよい）。
    """
    current_agent_type = _SUBAGENT_AGENT_TYPE.get()
    allowed = _state._AGENT_TYPE_RUN_SCRIPT_ALLOWLIST.get(current_agent_type) if current_agent_type else None
    if allowed is not None and skill_name not in allowed and (skill_name, script_filename) not in allowed:
        return (
            f"Error: the run_script skills/scripts callable from agent_type=\"{current_agent_type}\" are limited to "
            f"{sorted(str(a) for a in allowed)} "
            f"(skill={skill_name}, script={script_filename} is not allowed). "
            "Tell the delegator to delegate again to the matching sub-agent (explore for reading office documents/PDFs, "
            "worker if writing, web search or other skill execution is needed)."
        )

    args = script_args or []
    # args 内の各要素で `@N`（パスメモリー参照）を実パスへ解決する。
    # 対象外の文字列（トークン形式でない）はそのまま通す。
    resolved_args = []
    for a in args:
        resolved, error = _resolve_path_memory_token(a)
        if error:
            return f"Error: {error}"
        resolved_args.append(resolved)
    args = resolved_args
    try:
        script_path = _resolve_script_filename(skill_name, script_filename)
    except ValueError as e:
        return f"Error: {e}"

    # .py は設定の Python で、それ以外はそのまま実行を試みる。
    if script_path.suffix == ".py":
        return [_script_python_for(script_path), str(script_path), *args]
    return [str(script_path), *args]


def _script_python_for(script_path: Path) -> str:
    """スクリプトを起動する Python 実行ファイルを返す。

    config.ini の [paths].project_locohane_dir で {"dir": ..., "python": ...}
    形式により専用 Python が指定されたディレクトリの skills/ 配下にある
    スクリプトはその Python を、それ以外は [scripts].python を使う。
    """
    cfg = _state._LLM_CONFIG
    if cfg is not None:
        for skills_dir, python in cfg.locohane_skills_pythons.items():
            if script_path.is_relative_to(skills_dir.resolve()):
                return python
    return _state._SCRIPT_PYTHON


def _prepare_script_execution(skill_name: str, script_filename: str, script_args: list[str] | None = None) -> tuple[list[str], Path] | str:
    """run_script / run_script_background 共通の前処理。

    _resolve_run_script_command に加え、作業ディレクトリ解決 → 計画承認
    チェックを行う。(skill_name, script_filename) が config.ini の
    [plan].plan_approval_exempt_scripts に登録されている場合は
    計画未承認でも実行できる。

    メインエージェント自身の直接呼び出し回数ガード（[main_agent_tool_guard]）は
    ここではなく ImageAwareToolNode.invoke/ainvoke（_guard_main_agent_tool_limit）
    側でツール実行前にかかるため、ここには現れない。

    Returns:
        検証に成功すれば (cmd, workdir) のタプル。失敗すれば
        「エラー: ...」形式の文字列（呼び出し側はそのまま返せばよい）。
    """
    cmd = _resolve_run_script_command(skill_name, script_filename, script_args)
    if isinstance(cmd, str):
        return cmd

    # work_dir未設定・書き込み不可等の場合、_resolve_workdir()自体が
    # cwdを`_tmp_<thread_id>`へフォールバックする（サーバー側共有ディレクトリ
    # である default_workdir 直下に、相対パス書き込みの既定の置き場として
    # 全セッション共通で成果物が積まれるのを防ぐ）。
    workdir = _resolve_workdir(need_write=True)

    # ドラフトスキル・skill-creator 本体のスクリプトは plan_approval_exempt_scripts の
    # 登録に関係なく常に免除する（作成者はスキル開発の専門家とは限らず、
    # config.ini を自分で編集できないため。書き込み先はガードで制限される）。
    is_plan_exempt = (skill_name, script_filename) in _state._PLAN_APPROVAL_EXEMPT_SCRIPTS or _is_guard_exempt_script(
        skill_name, script_filename
    )
    if not is_plan_exempt and not cl.user_session.get("plan_approved"):
        logger.info("run_script: blocked because the plan is not approved skill=%s script=%s", skill_name, script_filename)
        return (
            "Error: cannot run because the plan is not approved "
            f"(skill={skill_name}, script={script_filename}). "
            "Create a plan with create_plan, get the user's approval with approve_plan, "
            "and then run it. If your tools do not include create_plan/approve_plan "
            "(you are a sub-agent), do not try again; report this error "
            "as is to the delegator as your final answer."
        )

    return cmd, workdir


async def _run_script_impl(skill_name: str, script_filename: str, script_args: list[str] | None = None) -> str:
    """run_script の実行本体。

    公開ツールの引数名は "args" ではなく "script_args"（"args"/"kwargs" は
    pydantic の ValidatedFunction が *args/**kwargs 用プレースホルダとして
    予約している名前と衝突し、生成されるスキーマのフィールド名が
    "v__args" に化けて run_script() 呼び出しが TypeError になるため使えない）。
    """
    prepared = _prepare_script_execution(skill_name, script_filename, script_args)
    if isinstance(prepared, str):
        return prepared
    cmd, workdir = prepared
    env, guard_dir = _run_script_guard_env(workdir, skill_name, script_filename)

    logger.info("run_script: %s %s cwd=%s", skill_name, script_filename, workdir)
    try:
        # 承認待ちの await 済みで別スレッドの必要はないが、subprocess.run 自体は
        # ブロッキング呼び出しのため、to_thread でイベントループのブロックを避ける。
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=_state._SCRIPT_TIMEOUT,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"Error: the script timed out after {_state._SCRIPT_TIMEOUT} seconds."
    except OSError as e:
        return f"Error: could not run the script: {e}"
    finally:
        if guard_dir is not None:
            shutil.rmtree(guard_dir, ignore_errors=True)

    # stdout / stderr / 終了コードをまとめて返す（LLM が結果を解釈できるように）。
    parts = [f"[exit code] {proc.returncode}"]
    if proc.stdout:
        parts.append(f"[stdout]\n{proc.stdout.rstrip()}")
    if proc.stderr:
        parts.append(f"[stderr]\n{proc.stderr.rstrip()}")
    if proc.returncode != 0:
        parts.append(_script_args_hint(script_args))
    warning = _track_failure_streak("run_script_failure_streak", proc.returncode != 0, "run_script")
    if warning:
        parts.append(warning)
    return "\n".join(parts)


@dataclass
class _BackgroundJob:
    """run_script_background で起動したジョブの状態。

    モジュールレベルの _BACKGROUND_JOBS に job_id をキーとして保持する。
    """

    process: asyncio.subprocess.Process
    thread_id: str
    skill_name: str
    script_filename: str
    started_at: float
    stdout_chunks: list[str]
    stderr_chunks: list[str]
    status: str  # "running" | "completed" | "failed" | "timeout" | "killed" | "error"
    returncode: int | None
    error_message: str | None
    runner_task: "asyncio.Task | None" = None
    # execute_python_code_background 由来のジョブのみ設定される
    # （run_script_background 由来のジョブでは None のまま）。
    tmp_path: "Path | None" = None
    workdir: "Path | None" = None
    before_snapshot: "dict[Path, float] | None" = None
    # run_script_background / execute_python_code_background 由来のジョブの
    # 書き込みガード用一時ディレクトリ（_run_script_guard_env が作成）。
    # ジョブ終了後に _run_background_job の finally で削除する。ガード注入に
    # 失敗した場合や execute_python_code_background 由来の場合は None のまま
    # （execute_python_code_background 側はコード先頭連結方式のためこの
    # フィールドを使わない）。
    guard_dir: "Path | None" = None
    # check_script_job が直前に「実行中」ステータスを返した時刻
    # （time.monotonic()）。None は未確認（起動直後でまだ一度も
    # check_script_job が呼ばれていない）ことを表す。連続呼び出しの
    # 最短間隔（_state._SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS）を
    # サーバー側で強制するために使う。
    last_polled_at: float | None = None


# run_script_background のジョブレジストリ。プロセス内メモリのみで永続化は
# しない（アプリ再起動でジョブは失われるが、そもそも実行中プロセスも
# 再起動で失われるため実害はない）。
_BACKGROUND_JOBS: dict[str, _BackgroundJob] = {}

# check_script_job が「実行中」ステータスで返す標準出力/標準エラーの末尾の
# 最大文字数（全量を返すとコンテキストを圧迫するため切り詰める）。
# 値は config.ini の [scripts].background_job_output_tail_chars（=
# _state._SCRIPT_BACKGROUND_JOB_OUTPUT_TAIL_CHARS）で管理する。
_JOB_OUTPUT_TAIL_CHARS: int = _state._SCRIPT_BACKGROUND_JOB_OUTPUT_TAIL_CHARS


async def _read_stream_into(stream: "asyncio.StreamReader | None", chunks: list[str]) -> None:
    """サブプロセスの stdout/stderr を EOF まで読み、行単位で chunks に追記する。"""
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        chunks.append(line.decode("utf-8", errors="replace"))


def _format_background_job_progress(job: "_BackgroundJob", job_id: str) -> str:
    """run_script_background/execute_python_code_background ジョブの実行中の
    状況を表す文字列を組み立てる。

    _push_background_job_progress（人間向けのUI直接push）と check_script_job
    の running 分岐（フォールバック経路でのLLM向け応答）の両方から呼ぶ、
    表示フォーマット共通化のためのヘルパー（dispatch_agent の
    _format_dispatch_agent_progress と同じ役割）。

    stdout/stderr の末尾は表示しない（完了時の結果には引き続き含まれる。
    _format_job_result 参照。2026-08-27 ユーザー要望により実行中表示からのみ除去）。
    """
    elapsed = int(time.monotonic() - job.started_at)
    return f"Running (elapsed {elapsed} s, job_id={job_id})."


async def _push_background_job_progress(job: "_BackgroundJob", job_id: str) -> None:
    """run_script_background/execute_python_code_background の実行中、人間向けに
    進捗をチャットへ直接pushする。

    cl.Message送信のみでLLM呼び出しを一切伴わないためトークンを消費しない
    （dispatch_agent の _push_dispatch_agent_progress と同じ設計）。
    author=_SUBAGENT_MESSAGE_AUTHOR は使わない（サブエージェントの最終回答
    専用の識別子のため）。代わりに type="system_message" を使う
    （app.py がコンテキスト圧縮の通知等、既存の一時的なシステム通知に
    使っている慣習と同じ）。

    _run_background_job の finally で確実にキャンセルされる想定
    （_push_dispatch_agent_progress と同じ理由）。

    metadata={"ephemeral_progress": True} を付ける（_push_dispatch_agent_progress
    と同じ）。ChatThreadDataLayer.create_step（src/thread_store.py）がこの
    フラグを見て永続化をスキップするため、スレッド再開時に「経過N秒・
    job_id=xxx」という実行時点でしか意味を持たない古い進捗表示が復元されない
    （2026-08-21 ユーザー報告）。ライブ表示（emitter.send_step）には影響しない。

    2回目以降のpushは新規 cl.Message を送らず、1回目に送ったメッセージの
    content を書き換えて .update() で送る。以前は毎回 .send() していたため、
    長時間ジョブでは「実行中です」が会話に大量に積み上がり、他のやり取りが
    流れて見えなくなる問題があった（2026-09-07 ユーザー報告）。.update() は
    同一 id の StepDict を emitter.update_step 経由でフロントエンドへ送る
    （Chainlit側のソケットイベント名は "update_message"）。フロントエンドは
    @chainlit/react-client 標準の useChatSession/useChatMessages をそのまま
    使っており、"update_message" 受信時に同一idのメッセージを上書きする
    （新規追加しない）ため、会話上は1件のまま内容だけが更新される。
    """
    message: "cl.Message | None" = None
    while job.status == "running":
        await asyncio.sleep(_state._SCRIPT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS)
        if job.status != "running":
            break
        content = _format_background_job_progress(job, job_id)
        if message is None:
            message = cl.Message(
                content=content,
                type="system_message",
                metadata={"ephemeral_progress": True},
            )
            await message.send()
        else:
            message.content = content
            await message.update()


async def _run_background_job(job: "_BackgroundJob", job_id: str) -> None:
    """バックグラウンドジョブのランナータスク本体。

    stdout/stderr の読み取りと終了コード取得を並行して行い、
    background_max_runtime_seconds を超えたら強制終了する。
    stop_script_job が先に status を "killed" にしていた場合はそれを
    上書きしない。

    進捗push タスク（_push_background_job_progress）をここで生成・管理する。
    run_script_background/execute_python_code_background 自身が安全上限
    フォールバックでターンを終えた後も、このタスク（＝ジョブ本体）が生きて
    いる限り進捗pushは動き続ける（dispatch_agent の _run_dispatch_agent_job
    と同じ設計）。
    """
    progress_task = asyncio.create_task(_push_background_job_progress(job, job_id))
    try:
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _read_stream_into(job.process.stdout, job.stdout_chunks),
                    _read_stream_into(job.process.stderr, job.stderr_chunks),
                    job.process.wait(),
                ),
                timeout=_state._SCRIPT_BACKGROUND_MAX_RUNTIME_SECONDS,
            )
        except asyncio.TimeoutError:
            job.process.kill()
            await job.process.wait()
            if job.status != "killed":
                job.status = "timeout"
            job.returncode = job.process.returncode
            return
        except Exception as e:  # noqa: BLE001 - ストリーム読み取り自体の異常はエラー扱いで返す
            if job.status != "killed":
                job.status = "error"
                job.error_message = f"{e}\n{traceback.format_exc()}"
            return

        job.returncode = job.process.returncode
        if job.status == "killed":
            return
        job.status = "completed" if job.returncode == 0 else "failed"
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass
        # execute_python_code_background が書き出した一時 .py ファイルの後始末。
        # run_script_background 由来のジョブでは tmp_path が None のため何もしない。
        if job.tmp_path is not None:
            job.tmp_path.unlink(missing_ok=True)
        # run_script_background 由来のジョブの書き込みガード用一時ディレクトリの後始末
        # （_run_script_guard_env が作成。ガード注入に失敗した場合は None のため何もしない）。
        if job.guard_dir is not None:
            shutil.rmtree(job.guard_dir, ignore_errors=True)


def _purge_stale_background_jobs() -> None:
    """完了済みのまま check_script_job で回収されなかったジョブを掃除する。

    専用のクリーンアップループは持たず、run_script_background の呼び出しの
    度に opportunistic に走らせる。
    """
    now = time.monotonic()
    stale = [
        job_id
        for job_id, job in _BACKGROUND_JOBS.items()
        if job.status != "running" and now - job.started_at > _state._SCRIPT_BACKGROUND_JOB_RETENTION_SECONDS
    ]
    for job_id in stale:
        _BACKGROUND_JOBS.pop(job_id, None)


def _format_job_result(job: "_BackgroundJob") -> str:
    """終了済みジョブの結果を run_script と同じ表示形式に整形する。"""
    returncode_label = job.returncode if job.returncode is not None else "unknown"
    parts = [f"[exit code] {returncode_label}"]
    stdout = "".join(job.stdout_chunks).rstrip()
    stderr = "".join(job.stderr_chunks).rstrip()
    if stdout:
        parts.append(f"[stdout]\n{stdout}")
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    # execute_python_code_background 由来のジョブのみ workdir/before_snapshot が
    # 設定されている（run_script_background 由来では None のためスキップされる）。
    if job.workdir is not None and job.before_snapshot is not None:
        path_memory_note = _register_exec_output_files(job.workdir, job.before_snapshot, job.thread_id)
        if path_memory_note:
            parts.append(path_memory_note)
    # tmp_path is None の場合のみ run_script_background 由来（execute_python_code_background は
    # script_args を持たないため対象外）。
    if job.tmp_path is None and job.returncode not in (0, None):
        parts.append(_SCRIPT_ARGS_HINT)
    return "\n".join(parts)


def _background_job_started_message(job_id: str) -> str:
    """run_script_background/execute_python_code_background が安全上限
    （[scripts].background_inline_wait_max_seconds）に達してもなおジョブが
    終わっていない場合に、フォールバックとしてLLMへ返す案内文。

    通常はこの文言に到達しない（run_script_background/
    execute_python_code_background 自身がジョブ完了までツール呼び出し内で
    待ち続け、最終結果を直接返すため）。到達するのは、設定された安全上限を
    超えるほど長時間のジョブだけである（dispatch_agent の
    _dispatch_agent_job_started_message と同じ設計）。

    以前は「途中で打ち切る場合は stop_script_job を使ってください」という
    表現だけだったが、これが「長時間かかる処理は打ち切るべきもの」という
    誤読を誘発し、ユーザーが完走を求めているのにモデルが自発的に
    stop_script_job を呼んで途中終了させてしまう事例が確認された。
    処理時間の長さ自体は打ち切る理由にならないことを明記する。
    """
    return (
        f"Running in the background (job_id={job_id}). It is taking a long time, "
        "so this turn ends and control is returned for now (the job itself keeps running in the background).\n"
        "Use check_script_job (with job_id) to check completion and get the result. "
        "Taking a long time is not in itself a reason to stop it. "
        "Use stop_script_job (with job_id) only when the user explicitly asks to interrupt or cancel."
    )


def _resolve_job(job_id: str) -> "_BackgroundJob | str":
    """job_id を現在のセッション所有のジョブへ解決する（他セッションは拒否）。"""
    job = _BACKGROUND_JOBS.get(job_id)
    if job is None:
        return f"Error: job_id '{job_id}' not found (already retrieved, or an invalid ID)."
    thread_id = cl.user_session.get("thread_id") or ""
    if job.thread_id != thread_id:
        return f"Error: job_id '{job_id}' does not belong to the current session."
    return job


async def cancel_background_script_jobs_for_thread(thread_id: str) -> bool:
    """指定スレッドで実行中の run_script_background/execute_python_code_background
    ジョブを全て強制終了する。

    app.py の on_stop（自セッション停止）・_stop_thread_generating
    （閲覧側からのcross-session停止）から、_dispatch_agent_job.py の
    cancel_dispatch_agent_jobs_for_thread と並べて呼ぶ。dispatch_agent と
    同じ理由（job.runner_task が asyncio.shield() で保護されており、
    メイングラフのタスクをcancel()するだけでは止まらない）で、これらの
    ジョブも停止ボタンだけでは止まらないまま残っていた。

    stop_script_job ツールと同じ「process.kill() → runner_task の自然終了を
    待つ」方式を使う（runner_task.cancel() を使わないのは、runner_task が
    サブプロセスのstdout/stderr読み取り・完了待ちのループであり、これを
    cancel() すると読み取り途中の出力が失われるうえプロセス自体が孤児
    として残ってしまうため。stop_script_job参照）。

    Returns:
        1件以上のジョブを強制終了した場合 True。
    """
    targets = [
        (job_id, job)
        for job_id, job in _BACKGROUND_JOBS.items()
        if job.thread_id == thread_id and job.status == "running"
    ]
    for job_id, job in targets:
        job.status = "killed"
        logger.warning(
            "run_script_background: force-stopping because of the stop button (job_id=%s, skill=%s, script=%s)",
            job_id,
            job.skill_name,
            job.script_filename,
        )
        try:
            job.process.kill()
        except ProcessLookupError:
            pass
    runner_tasks = [(job_id, job.runner_task) for job_id, job in targets if job.runner_task is not None]
    if runner_tasks:
        results = await asyncio.gather(*(task for _, task in runner_tasks), return_exceptions=True)
        for (job_id, _), result in zip(runner_tasks, results):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.debug(
                    "Exception while force-stopping run_script_background (job_id=%s)",
                    job_id,
                    exc_info=result,
                )
    for job_id, _ in targets:
        _BACKGROUND_JOBS.pop(job_id, None)
    return bool(targets)


def _finalize_script_job_result(job: "_BackgroundJob", job_id: str) -> str:
    """終端状態（completed/failed/timeout/killed/error）のジョブを最終結果
    文字列へ整形し、レジストリから取り除く。

    run_script_background/execute_python_code_background（安全上限内に完了
    した場合）と check_script_job（終端状態を取得した場合）の両方から呼ぶ、
    ワンショット取得（一度取得したら同じ job_id は再利用できない）の共通処理
    （dispatch_agent の _finalize_dispatch_agent_job_result と同じ役割）。
    stop_script_job は自身がジョブを終端させた直後に独自の「強制終了しました。」
    接頭辞で結果を返すため、こちらは使わない。
    """
    result = _format_job_result(job)
    if job.status == "timeout":
        result = (
            f"Error: the background run reached the limit of {_state._SCRIPT_BACKGROUND_MAX_RUNTIME_SECONDS} " f"seconds and was forcibly stopped.\n{result}"
        )
    elif job.status == "killed":
        result = f"Forcibly stopped by stop_script_job.\n{result}"
    elif job.status == "error":
        result = f"Error: a problem occurred during the background run: {job.error_message}"
    _BACKGROUND_JOBS.pop(job_id, None)
    return result
