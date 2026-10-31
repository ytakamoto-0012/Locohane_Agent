"""dispatch_agent が使うバックグラウンドジョブ基盤。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import asyncio
import chainlit as cl
import logging
import time
import traceback

from .. import subagent
from ..config import with_reasoning_effort
from ..i18n import current_lang, t
from ..tool_labels import tool_label
from ..llm import reset_preferred_sub_model, set_preferred_sub_model
from ..subagent import dump_messages_for_cancelled_rescue, is_truncated_result

from . import _state
from ._script_job import _JOB_OUTPUT_TAIL_CHARS
from ._state import _IN_SUBAGENT, _SUBAGENT_AGENT_TYPE, _SUBAGENT_RUN_ID, ResolvedAgentType, _get_session_semaphore
from .write_scratch_note import _scratch_notes_path, _scratch_notes_path_for_run

logger = logging.getLogger(__name__)


def _append_scratch_note_hint(result: str) -> str:
    """打ち切られたサブエージェントの結果に、スクラッチノートの案内を追記する。

    write_scratch_note で途中経過が書き残されていれば、そのパスを案内する。
    委譲元はそちらを Read すれば、打ち切りにより未整理のまま返ってくる
    ツール結果の生データより、サブエージェント自身が構造化して書き残した
    内容を優先して参照できる。呼び出し時点で _SUBAGENT_RUN_ID がまだ
    現在の実行を指している必要があるため、dispatch_agent の finally で
    リセットする前に呼ぶこと。
    """
    if not is_truncated_result(result):
        return result
    path = _scratch_notes_path()
    if not path.is_file():
        return result
    return (
        f"{result}\n\n[This sub-agent wrote progress notes with write_scratch_note. "
        f"Read {path} to get what it organized before it was stopped.]"
    )


def _make_rescue_on_cancelled(job: "_DispatchAgentJob"):
    """run_subagent の on_cancelled 引数へ渡すコールバックを組み立てる。

    停止ボタン等でこのジョブが強制終了された場合、run_subagent はここまでの
    会話履歴と理由を渡してこのコールバックを同期的に呼ぶ（CancelledError の
    再送出前）。また [subagent].token_guard_hard_threshold 到達時（急速な
    トークン爆発でソフト警告を飛び越えて一気にhardへ到達したケース含む）にも、
    CancelledErrorは使わず同じコールバックが同期的に呼ばれる（この場合は
    run_subagentが通常のreturnで打ち切りメッセージを返す前に呼ばれる。
    src/subagent.py の run_subagent docstring参照）。理由は退避テキストの
    見出しへそのまま出るため、後から読む人が停止ボタンとトークン超過を
    取り違えずに済む。
    write_scratch_note と同じファイル（_scratch_notes_path_for_run(job.run_id)）
    へ追記することで、check_dispatch_agent_job の進捗表示や
    _append_scratch_note_hint など既存の案内導線がそのまま拾える。

    書き込み失敗（OSError）に加え、パス解決自体の失敗も送出せずログに留める。
    _scratch_notes_path_for_run は内部で _resolve_exec_workdir() 経由
    cl.user_session からthread_idを読むため、Chainlitセッション文脈が
    解決できない状況（cross-session停止でタスク生成元のタブが既に
    閉じられている場合等）では ChainlitContextException 等、OSError以外の
    例外を送出しうる。run_subagent 側は on_cancelled の例外を最終的に
    捕捉して握りつぶす設計だが、CancelledError の伝播をここでも確実に
    妨げないようにするため、パス解決から書き込みまでを1つのtryで囲み
    Exception全体を捕捉する。
    """

    def _on_cancelled(messages: list, reason: str) -> None:
        dump = dump_messages_for_cancelled_rescue(messages, reason)
        if not dump:
            return
        try:
            path = _scratch_notes_path_for_run(job.run_id)
            with path.open("a", encoding="utf-8") as f:
                f.write(dump)
                f.write("\n")
        except Exception:  # noqa: BLE001 - パス解決・書き込みいずれの失敗でもCancelledErrorの伝播を妨げない
            logger.exception(
                "dispatch_agent: failed to save the conversation history on forced stop (run_id=%s)",
                job.run_id,
            )

    return _on_cancelled


@dataclass
class _DispatchAgentJob:
    """dispatch_agent で起動したジョブの状態。

    モジュールレベルの _DISPATCH_AGENT_JOBS に job_id をキーとして保持する。
    _BackgroundJob（run_script_background 用）とは別のデータクラスにする。
    サブプロセスを持たず（process/stdout_chunks 等が不要）、代わりに
    run_subagent 専用の run_id（_SUBAGENT_RUN_ID に相当）を持つ点が異なる。
    """

    thread_id: str
    run_id: str  # このジョブ専用の _SUBAGENT_RUN_ID・_IN_SUBAGENT の値
    agent_type: str
    task_preview: str  # ログ・状況確認表示専用（task 先頭一部。全文は保持しない）
    started_at: float  # time.monotonic()
    status: str  # "running" | "completed" | "killed" | "error"
    result: str | None
    error_message: str | None
    max_iterations: int  # run_subagent に渡した反復上限（進捗表示用に保持）
    runner_task: "asyncio.Task | None" = None
    # run_subagent の on_iteration コールバックが更新する、現在の反復回数
    # （進捗push・check_dispatch_agent_job の running 分岐の表示に使う）。
    current_iteration: int = 0
    # run_subagent の on_activity コールバックが更新する、今している動作の表示名
    # （"思考中"・"ツール: Read" 等。UIのStepバッジと同じ表記）。進捗表示で
    # 「何をしているか」をひと目で分かるようにするために使う。未開始は空文字列。
    current_activity: tuple[str, ...] = ()
    # current_activity が変わるたびに set する。進捗push（_push_dispatch_agent_progress・
    # dispatch_agent_batch の _push_batch_progress）が、push 間隔を待たずに表示を
    # 書き換えるために待つ（_wait_next_progress_push 参照）。
    activity_changed: asyncio.Event = field(default_factory=asyncio.Event)
    # check_dispatch_agent_job が直前に「実行中」ステータスを返した時刻
    # （time.monotonic()）。_BackgroundJob.last_polled_at と同じ理由づけ
    # （連続呼び出しの最短間隔をサーバー側で強制するため）。
    last_polled_at: float | None = None
    # dispatch_agent() がこのジョブの完了をまだ（安全上限内で）待っているとみなせる
    # 間は True。安全上限超過・停止ボタン等でジョブの完了を待たずに呼び出し元の
    # ターンが先に終わった場合、dispatch_agent() 側がジョブ完了より前に
    # （wait_forの例外捕捉時に同期的に）False へ変える。
    # _run_dispatch_agent_job の finally 節が job.runner_task の完了処理の
    # 一部として実行されるのは asyncio.wait_for が制御を戻すより必ず前になる
    # （wait_forは対象タスクの完了＝finally込みの完了を待って初めて返るため）。
    # そのため「dispatch_agent()側で完了後にTrueにする」という実装は間に合わず、
    # 逆に「初期値Trueで、フォールバック検知時にのみ即座にFalseへ落とす」という
    # 向きでなければ正しく機能しない。True のときのみ、_run_dispatch_agent_job の
    # finally節でmain_agent_tool_guard_call_countのリセットを行ってよい（結果を
    # 受け取るのがまだ同じターンだと保証できるため）。False の場合にリセットすると、
    # その間に同一セッションで始まった別の新しいターンのツールガードカウンタを
    # 横から無効化してしまう。
    turn_still_waiting: bool = True
    # セマフォ（[subagent].max_parallel）を確保して run_subagent を実際に開始した
    # 時刻（time.monotonic()）。None の間は順番待ち。started_at は順番待ちの
    # 時間も含むため、進捗表示の経過秒数はこちらを基準にする（順番待ちの
    # ジョブが「経過1500秒・反復0回」と止まっているように見えるのを防ぐ）。
    run_started_at: float | None = None
    # False の場合、このジョブ自身の進捗push（_push_dispatch_agent_progress）を
    # 行わない。dispatch_agent_batch はグループ数分のメッセージが並ぶのを
    # 避けるため、全グループをまとめた進捗を自前で1件だけpushする。
    push_progress: bool = True
    # dispatch_agent/dispatch_agent_batch の model 引数。指定時は agents/*.md の
    # frontmatter model より優先する（_run_dispatch_agent_job 参照）。
    model: str | None = None


# dispatch_agent のジョブレジストリ。_script_job._BACKGROUND_JOBS と同じ理由で
# プロセス内メモリのみで永続化はしない（アプリ再起動でジョブは失われるが、
# その内部で走っていた run_subagent 自体も再起動で失われるため実害はない）。
_DISPATCH_AGENT_JOBS: dict[str, _DispatchAgentJob] = {}

# app.py の SUBAGENT_MESSAGE_AUTHOR、frontend/src/utils/messageTree.ts の
# SUBAGENT_MESSAGE_AUTHOR と一致させる（UI側でメインエージェントの回答と
# 区別して表示するための cl.Message author 識別子）。
_SUBAGENT_MESSAGE_AUTHOR = "サブエージェント"


# 動作（current_activity）が変わったときに進捗push を前倒しする際の最短間隔（秒）。
# ツール呼び出しは一瞬で終わることが多く、変わるたびに書き換えると表示が
# ちらつき、ブラウザとの通信も増えるため、前回の push からこれだけは空ける。
_ACTIVITY_PUSH_MIN_INTERVAL_SECONDS = 1.0


async def _wait_next_progress_push(jobs: "list[_DispatchAgentJob]", interval: float, *, early_on_activity: bool) -> None:
    """次の進捗push まで待つ。

    push 間隔（[subagent].background_progress_push_interval_seconds）ごとにしか
    書き換えないと、表示する動作はその瞬間の値になり、時間の大半を占める LLM の
    生成中に当たって「思考中」のまま変わらないように見える（ツールは一瞬で終わる
    ことが多い）。early_on_activity=True なら、いずれかのジョブの動作が変わった
    時点で（ただし最短 _ACTIVITY_PUSH_MIN_INTERVAL_SECONDS 空けて）待つのを
    やめる。経過秒数は従来どおり interval ごとに進む。

    最初のメッセージを送るまでは early_on_activity=False で呼ぶこと（すぐ終わる
    ジョブに進捗メッセージを出さないという従来の振る舞いを変えないため）。
    呼び出し元は、内容を組み立てる前に各ジョブの activity_changed を clear する。
    """
    if not early_on_activity:
        await asyncio.sleep(interval)
        return
    minimum = min(_ACTIVITY_PUSH_MIN_INTERVAL_SECONDS, interval)
    await asyncio.sleep(minimum)
    waiters = [asyncio.ensure_future(job.activity_changed.wait()) for job in jobs]
    try:
        await asyncio.wait(waiters, timeout=max(interval - minimum, 0), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


def _format_activity(job: "_DispatchAgentJob", lang: str = "en") -> str:
    """進捗表示の末尾に付ける、今している動作（UIのStepバッジ名）。未開始なら空文字列。

    job.current_activity は src/subagent.py の (種別, *引数) のタプル。lang は表示言語
    （画面への進捗push はセッションの言語、LLM への応答は英語）。
    """
    if not job.current_activity:
        return ""
    kind, *args = job.current_activity
    if kind == "tools":
        label = t("activity.tools", lang, names=t("list.separator", lang).join(tool_label(n, lang) for n in args))
    else:
        label = t(f"activity.{kind}", lang)
    return t("activity.wrap", lang, activity=label)


def _format_dispatch_agent_progress(job: "_DispatchAgentJob", job_id: str, lang: str = "en") -> str:
    """dispatch_agent ジョブの実行中の状況を表す文字列を組み立てる。

    _push_dispatch_agent_progress（人間向けのUI直接push。lang はセッションの言語）と
    check_dispatch_agent_job の running 分岐（フォールバック経路でのLLM向け
    応答。英語）の両方から呼ぶ、表示フォーマット共通化のためのヘルパー。
    """
    if job.run_started_at is None:
        waited = int(time.monotonic() - job.started_at)
        return t("progress.waiting", lang, waited=waited, job_id=job_id)
    elapsed = int(time.monotonic() - job.run_started_at)
    parts = [
        t(
            "progress.running",
            lang,
            elapsed=elapsed,
            iteration=job.current_iteration,
            max_iterations=job.max_iterations,
            job_id=job_id,
        )
        + _format_activity(job, lang)
    ]
    note_path = _scratch_notes_path_for_run(job.run_id)
    if note_path.is_file():
        note_tail = note_path.read_text(encoding="utf-8", errors="replace")[-_JOB_OUTPUT_TAIL_CHARS:].rstrip()
        if note_tail:
            parts.append(t("progress.note_tail", lang, tail=note_tail))
    return "\n".join(parts)


async def _push_dispatch_agent_progress(job: "_DispatchAgentJob", job_id: str) -> None:
    """dispatch_agent の実行中、人間向けに進捗をチャットへ直接pushする。

    cl.Message送信のみでLLM呼び出しを一切伴わないため、トークンを消費しない。
    「LLM自身がcheck_dispatch_agent_jobを繰り返し呼ぶとその都度LLM推論コストが
    かかる」というフィードバックを受け、dispatch_agent 自体は
    ジョブ完了までLLMに戻らずブロックする設計にしている（_run_dispatch_agent_job
    参照）。その間も人間が「動いているか」を確認できるよう、この関数が
    代わりに進捗を伝える。

    contextvarベースのChainlitセッションコンテキストは asyncio.create_task
    生成時点でコピーされる。このタスクは dispatch_agent の
    ツール呼び出しがまだ生きている（＝元のセッションコンテキストの中にいる）
    間に生成されるため、cl.Message(...).send() が正しいセッションへ届く。

    _run_dispatch_agent_job の finally で確実にキャンセルされる想定
    （ループ条件（job.status=="running"）だけに頼ると、ジョブ完了後も
    次の asyncio.sleep が明けるまでタスクが残留してしまうため）。

    metadata={"ephemeral_progress": True} を付ける（_push_background_job_progress
    と同じ）。ChatThreadDataLayer.create_step（src/thread_store.py）がこの
    フラグを見て永続化をスキップするため、スレッド再開時に「経過N秒・
    反復N/N回・job_id=xxx」という実行時点でしか意味を持たない古い進捗表示が
    復元されない（2026-08-21 ユーザー報告）。ライブ表示（emitter.send_step）
    には影響しない。サブエージェントの実際の発言（write_scratch_note等が
    生む本来のステップ）にはこのフラグを付けないため、それらは通常通り
    履歴に残る。

    2回目以降のpushは新規 cl.Message を送らず、1回目に送ったメッセージの
    content を書き換えて .update() で送る（_push_background_job_progress と
    同じ変更）。以前は毎回 .send() していたため、長時間ジョブでは
    「実行中です」が会話に大量に積み上がり、他のやり取りが流れて見えなく
    なる問題があった（2026-09-07 ユーザー報告）。フロントエンドは
    @chainlit/react-client 標準の useChatSession/useChatMessages をそのまま
    使っており、.update() が送る "update_message" イベント受信時に同一id
    のメッセージを上書きする（新規追加しない）ため、会話上は1件のまま内容
    だけが更新される。
    """
    message: "cl.Message | None" = None
    while job.status == "running":
        await _wait_next_progress_push(
            [job], _state._DISPATCH_AGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS, early_on_activity=message is not None
        )
        if job.status != "running":
            break
        job.activity_changed.clear()
        content = _format_dispatch_agent_progress(job, job_id, current_lang())
        if message is None:
            message = cl.Message(
                content=content,
                author=_SUBAGENT_MESSAGE_AUTHOR,
                metadata={"ephemeral_progress": True},
            )
            await message.send()
        else:
            message.content = content
            await message.update()


async def _run_dispatch_agent_job(
    job: "_DispatchAgentJob",
    job_id: str,
    task: str,
    resolved: "ResolvedAgentType",
    should_skip: "Callable[[], str | None] | None" = None,
) -> None:
    """dispatch_agent のランナータスク本体。

    should_skip はセマフォ確保直後に呼ばれ、文字列（理由）を返した場合は
    run_subagent を起動せずに job.status="error"（error_message=理由）で終える。
    run_started_at は None のまま残るため、呼び出し元は「未実行」と区別できる
    （dispatch_agent_batch が、LLMサーバーに届かない状態で残りのグループを
    起動し続けないために使う）。

    asyncio.create_task() はタスク生成時点のコンテキストをコピーし、以後
    タスク内部の contextvar.set()/reset() は呼び出し元（dispatch_agent）へは
    伝播しない。そのため _IN_SUBAGENT / _SUBAGENT_RUN_ID の設定・セマフォ確保・
    run_subagent() 呼び出し・_append_scratch_note_hint・
    main_agent_tool_guard_call_count のリセットは、このタスクの中で行う必要がある
    （呼び出し元側で行っても、値が呼び出し元のコンテキストにしか残らず
    このタスクの中からは見えない）。

    dispatch_agent は起動後、このタスクの完了を（安全上限まで）同じツール
    呼び出し内で待ち続ける設計だが、それでも run_subagent 自体はこのタスク
    として独立させておく。理由: (1) 安全上限を超えた場合に dispatch_agent
    側だけ制御をLLMへ返しつつ、このタスクは裏側で動き続けさせる必要がある
    （asyncio.shield で保護）。(2) 進捗push タスク（_push_dispatch_agent_progress）
    と並行させる必要がある。

    例外はここで確定的に捕捉し job.status="error" へ変換する。fire-and-forget
    の asyncio.Task 内で未捕捉の例外は「Task exception was never retrieved」
    という警告だけを残して静かに失われ、回収経路が無くなるため
    （_run_background_job と同じ理由）。asyncio.CancelledError
    （stop_dispatch_agent_job による task.cancel()）はここで握りつぶさず
    そのまま伝播させる。stop_dispatch_agent_job が先に status="killed" を
    設定済みのため、下記の各分岐は「killed を上書きしない」ガードを持つ
    （_run_background_job の status!="killed" ガードと同じレース対策）。
    """
    token = _IN_SUBAGENT.set(True)
    run_id_token = _SUBAGENT_RUN_ID.set(job.run_id)
    agent_type_token = _SUBAGENT_AGENT_TYPE.set(job.agent_type)
    # このタスク内の build_model(role="sub")（再構築・圧縮用を含む）が使うモデル。
    # 一致する [llm].sub_url の接続先が無ければルーティング側で無視される。
    model_token = set_preferred_sub_model(job.model or resolved.model)
    progress_task = asyncio.create_task(_push_dispatch_agent_progress(job, job_id)) if job.push_progress else None

    def _on_iteration(iteration: int, max_iterations: int) -> None:
        job.current_iteration = iteration

    def _on_activity(activity: tuple[str, ...]) -> None:
        if activity != job.current_activity:
            job.current_activity = activity
            job.activity_changed.set()

    on_cancelled = _make_rescue_on_cancelled(job)

    skip_reason: str | None = None

    async def _run() -> str | None:
        nonlocal skip_reason
        # セマフォの順番が回ってきた時点で判定する（順番待ちの間に起動を止める理由が生じうるため）。
        skip_reason = should_skip() if should_skip is not None else None
        if skip_reason:
            return None
        job.run_started_at = time.monotonic()
        return await subagent.run_subagent(
            task,
            resolved.tools,
            resolved.system_prompt,
            with_reasoning_effort(_state._LLM_CONFIG, cl.user_session.get(_state.SESSION_REASONING_EFFORT_KEY)),
            job.max_iterations,
            on_iteration=_on_iteration,
            llm_timeout_max_retries=_state._DISPATCH_AGENT_BACKGROUND_LLM_TIMEOUT_MAX_RETRIES,
            on_cancelled=on_cancelled,
            on_activity=_on_activity,
        )

    try:
        sem = _get_session_semaphore(_state._DISPATCH_AGENT_SEMAPHORES, _state._DISPATCH_AGENT_MAX_PARALLEL)
        if sem is not None:
            async with sem:
                result = await _run()
        else:
            result = await _run()
        if result is None:
            # should_skip により起動しなかった（run_started_at は None のまま）。
            if job.status != "killed":
                job.status = "error"
                job.error_message = skip_reason
        else:
            result = _append_scratch_note_hint(result)
            if job.status != "killed":
                job.result = result
                job.status = "completed"
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - fire-and-forgetタスクの例外を消さず確定的に捕捉する
        logger.exception("dispatch_agent failed (run_id=%s)", job.run_id)
        if job.status != "killed":
            job.status = "error"
            # str(e) が空文字列になる例外がある（例: asyncio.TimeoutError()）。
            # 「エラー: サブエージェントの実行に失敗しました: 」のように原因が
            # 一切分からない文言になっていた実例が本番ログで確認された
            # （issue/20260808_022438_dispatch_agent_background_failure.md）。
            # 空の場合は例外の型名で補い、必ず何らかの手がかりを残す。
            job.error_message = f"{str(e) or type(e).__name__}\n{traceback.format_exc()}"
    finally:
        if progress_task is not None:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
        _IN_SUBAGENT.reset(token)
        _SUBAGENT_RUN_ID.reset(run_id_token)
        _SUBAGENT_AGENT_TYPE.reset(agent_type_token)
        reset_preferred_sub_model(model_token)
        # 委譲が完了するたびに main_agent_tool_guard のカウンタをリセットする。
        # 1ターン内で複数回「調査→delegate」を繰り返す正当なケースを妨げないため。
        # ただし、安全上限フォールバックや停止ボタン等で呼び出し元のターンが
        # 既に終わっている場合（turn_still_waiting=False）はリセットしない。
        # ターンが終わっている＝同一セッションで別の新しいターンが既に進行中の
        # 可能性があり、そちらのガードカウンタを横から無効化してしまうため。
        if job.turn_still_waiting:
            cl.user_session.set("main_agent_tool_guard_call_count", None)


def _finalize_dispatch_agent_job_result(job: "_DispatchAgentJob", job_id: str) -> str:
    """終端状態（completed/killed/error）のジョブを最終結果文字列へ整形し、レジストリから取り除く。

    dispatch_agent（安全上限内に完了した場合）と
    check_dispatch_agent_job（終端状態を取得した場合）の両方から呼ぶ、
    ワンショット取得（一度取得したら同じ job_id は再利用できない）の共通処理。
    """
    if job.status == "completed":
        result = job.result or ""
    elif job.status == "killed":
        result = f"Forcibly stopped by stop_dispatch_agent_job.\n{job.result or '(no final answer had been generated at the forced stop)'}"
    else:  # "error"
        result = f"Error: the sub-agent run failed: {job.error_message}"
    _DISPATCH_AGENT_JOBS.pop(job_id, None)
    return result


def _purge_stale_dispatch_agent_jobs() -> None:
    """完了済みのまま check_dispatch_agent_job で回収されなかったジョブを掃除する。

    専用のクリーンアップループは持たず、dispatch_agent の呼び出しの
    度に opportunistic に走らせる（_purge_stale_background_jobs と同じ方針）。
    """
    now = time.monotonic()
    stale = [
        job_id
        for job_id, job in _DISPATCH_AGENT_JOBS.items()
        if job.status != "running" and now - job.started_at > _state._DISPATCH_AGENT_BACKGROUND_JOB_RETENTION_SECONDS
    ]
    for job_id in stale:
        _DISPATCH_AGENT_JOBS.pop(job_id, None)


def _dispatch_agent_job_started_message(job_id: str) -> str:
    """dispatch_agent が安全上限（background_inline_wait_max_seconds）に
    達してもなおジョブが終わっていない場合に、フォールバックとしてLLMへ返す案内文。

    通常はこの文言に到達しない（dispatch_agent 自身がジョブ完了まで
    ツール呼び出し内で待ち続け、最終結果を直接返すため）。到達するのは、
    設定された安全上限を超えるほど長時間のジョブだけである。
    _background_job_started_message と同じ理由づけ（処理時間の長さ自体は
    打ち切る理由にならないことを明記し、モデルの自発的な早期キャンセルを防ぐ）。
    """
    return (
        f"Running in the background (job_id={job_id}). It is taking a long time, "
        "so this turn ends and control is returned for now (the job itself keeps running in the background).\n"
        "Use check_dispatch_agent_job (with job_id) to check completion and get the result. "
        "Taking a long time is not in itself a reason to stop it. "
        "Use stop_dispatch_agent_job (with job_id) only when the user explicitly asks to interrupt or cancel."
    )


async def cancel_dispatch_agent_jobs_for_thread(thread_id: str) -> bool:
    """指定セッションで実行中の dispatch_agent ジョブを全て強制終了する。

    app.py の on_stop（自セッション停止）・_stop_thread_generating
    （閲覧側からのcross-session停止）から呼ぶ。これらはメイングラフの
    タスク（session.current_task）しかキャンセルできず、dispatch_agent が
    起動した job.runner_task は asyncio.shield() で保護されているため
    その影響を受けない。放置すると、停止ボタンを押してバッジが「停止」に
    なった後もサブエージェントのLLM呼び出し・進捗push
    （_push_dispatch_agent_progress）が裏側で動き続ける（2026-08-26
    ユーザー報告。実測ログ: 停止ボタン押下から約107秒後にジョブが
    「11回で完了」と自然終了していた）。

    stop_dispatch_agent_job ツールと同じ job.status="killed" +
    runner_task.cancel() を、対象thread_idの実行中ジョブ全件へ適用する。
    ツール版と異なり、結果を待つ呼び出し元（LLM）がもう存在しない
    （ターン自体が終わっている）ため、進捗メモの案内文などは組み立てず、
    レジストリから取り除くだけでよい。

    全件へ cancel() を先に発行してから asyncio.gather で並行して後始末を
    待つ（1件ずつ cancel()→await を繰り返すと、[subagent].max_parallel で
    複数ジョブが並行実行中の場合、後始末が遅いジョブ1件が残り全件分の
    停止ボタンの反応を直列に遅延させてしまうため）。

    dispatch_agent() 自身の TimeoutError/CancelledError 処理は
    job.turn_still_waiting を即座に False にしてから待つ（_DispatchAgentJob
    のdocstring参照）が、ここでの強制終了はそれとは独立した経路のため
    同様に明示的に False へ倒す。倒さないと、このcancel()がまだ
    _run_dispatch_agent_job のfinally節へ届く前に dispatch_agent() 側の
    経路がスケジュールされないタイミング次第で、turn_still_waitingが
    古いTrueのまま観測され、既に別の新しいターンが始まっている
    main_agent_tool_guard_call_count を誤ってリセットしうる。

    Returns:
        1件以上のジョブを強制終了した場合 True（停止ボタンの呼び出し元が
        「何かを止めた」かどうかの判定に使う）。
    """
    targets = [
        (job_id, job)
        for job_id, job in _DISPATCH_AGENT_JOBS.items()
        if job.thread_id == thread_id and job.status == "running"
    ]
    for job_id, job in targets:
        job.status = "killed"
        job.turn_still_waiting = False
        logger.warning(
            "dispatch_agent: force-stopping because of the stop button (run_id=%s, job_id=%s, iter=%d/%d)",
            job.run_id,
            job_id,
            job.current_iteration,
            job.max_iterations,
        )
        if job.runner_task is not None:
            job.runner_task.cancel()
    runner_tasks = [(job_id, job.runner_task) for job_id, job in targets if job.runner_task is not None]
    if runner_tasks:
        results = await asyncio.gather(*(task for _, task in runner_tasks), return_exceptions=True)
        for (job_id, _), result in zip(runner_tasks, results):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.debug(
                    "Exception while force-stopping dispatch_agent (job_id=%s)",
                    job_id,
                    exc_info=result,
                )
    for job_id, _ in targets:
        _DISPATCH_AGENT_JOBS.pop(job_id, None)
    return bool(targets)


def _resolve_dispatch_agent_job(job_id: str) -> "_DispatchAgentJob | str":
    """job_id を現在のセッション所有のジョブへ解決する（他セッションは拒否）。

    _resolve_job と同じ方針。
    """
    job = _DISPATCH_AGENT_JOBS.get(job_id)
    if job is None:
        return f"Error: job_id '{job_id}' not found (already retrieved, or an invalid ID)."
    thread_id = cl.user_session.get("thread_id") or ""
    if job.thread_id != thread_id:
        return f"Error: job_id '{job_id}' does not belong to the current session."
    return job
