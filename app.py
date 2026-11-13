"""Chainlit UI（LangGraph 実行のストリーミング表示 + ステップ可視化）。

役割:
- 起動時にスキルを走査（第1段階 Discovery）→ システムプロンプトへ注入 → グラフを構築。
- @cl.on_message で LangGraph を astream_events で回し、トークンをストリーミング表示。
- スキル読み込み(read_skill)・ファイル読み込み(read_skill_file)・スクリプト実行(run_script)・
  画像解析(analyze_image)・サブエージェント委譲(dispatch_agent)・ユーザーへの追加質問
  (AskUserQuestion/ask_user_choice)の各ツールコールを cl.Step として可視化
  （「今このスキルを読んでいます」が見える状態）。
  dispatch_agent は内部で独立した ReAct ループを回すが、その会話履歴は親グラフの
  会話状態には乗らない（コンテキスト節約が目的。ただし astream_events 自体は
  contextvar 経由で内部ツール呼び出しのイベントも流すため、ツール実行の
  Step 表示・生成ファイルの自動添付（下記）は内部呼び出しにも実際には効く。
  _resolve_parent_id/_is_subagent_call 参照）。
- アップロードファイルはスレッド専用の作業フォルダ（default_workdir 配下の
  _tmp_<thread_id>/uploads/<送信日時>/）に保存し、パスを `@N` 付きでメッセージへ明示する
  （画像も直接は渡さず、見るかどうかは LLM が analyze_image 等で判断する）。
- ツール結果に生成ファイルの output_path（src/files.py 参照）が含まれる場合、
  画像なら cl.Image でインラインプレビュー、それ以外は cl.File でダウンロード
  可能な添付を自動送信する（pdf-tools/pptx-create 等のファイル生成スキル、
  provide_download・analyze_image の show_in_chat=True 引数に共通で効く）。

会話状態は AsyncSqliteSaver（config.checkpoint_db）で永続化。thread_id はセッション毎。
チェックポインタ（DB接続）・システムプロンプト・ツールはアプリ全体で 1 つを共有するが、
グラフ（LLMモデル・httpx.AsyncClientを含む）はセッション（タブ）ごとに個別に構築し
cl.user_session に保持する。停止ボタンは自セッションのLLMクライアントのみを強制
クローズし、他タブの処理には影響しない（src/llm.py の set_current_session /
aclose_active_llm_clients 参照）。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
import weakref
from dataclasses import replace
from pathlib import Path

# 完全オフライン運用を保証するための防御的な無効化（import より前に設定）。
# 実態: このスタックの外部送信はすべて opt-in。
#   - Chainlit(2.x) は LITERAL_API_KEY を設定した時のみ Literal AI へ送信する。
#   - LangChain は LANGSMITH_API_KEY + トレーシング有効時のみ LangSmith へ送信する。
# いずれのキーも設定しなければ外部通信は発生しないが、事故防止のため明示的に切る。
# （setdefault なので、意図的に有効化したい場合は環境変数で上書きできる。）
os.environ.setdefault("CHAINLIT_TELEMETRY_ENABLED", "false")  # 1.x 互換。2.x では no-op
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("LANGSMITH_TRACING", "false")

# 設定ダッシュボード（管理ツール、admin/ パッケージ）が複数インスタンスを子プロセス
# として起動する場合、インスタンス別の .env（instances/<name>/.env。ログイン
# ユーザー AUTH_USERS・JWT署名鍵 CHAINLIT_AUTH_SECRET 等）をここで読み込む。
# 環境変数 LOCOHANE_INSTANCE_ENV は管理ツールが子プロセス起動時に設定する
# （admin/supervisor.py 参照）。未設定（app.bat で直接起動した場合を含む）なら
# instances/<LOCOHANE_INSTANCE、既定 default>/.env を使うため、app.bat 起動でも管理ツール
# 経由の起動と同じ挙動になる（README_DETAIL.md「.envとユーザー管理」参照）。
# 必ず `import chainlit as cl`（直後、chainlitが自身のモジュール読み込み時に
# プロジェクト直下の .env を override=False で読む）より前に、override=True で
# 読むこと。これにより「インスタンスの.env > OS環境変数 > プロジェクト直下.env」
# の優先度になる。
# instances/ の場所は config.ini の [admin].instances_dir（環境変数 INSTANCES_DIR）で変更できる。
from dotenv import load_dotenv as _load_dotenv

from src.config import resolve_instances_root as _resolve_instances_root

_INSTANCE_NAME = os.getenv("LOCOHANE_INSTANCE") or "default"
_INSTANCE_DIR = _resolve_instances_root() / _INSTANCE_NAME
_instance_env_path = os.getenv("LOCOHANE_INSTANCE_ENV") or str(_INSTANCE_DIR / ".env")
_load_dotenv(_instance_env_path, override=True)

import uuid

import aiosqlite
import chainlit as cl
from chainlit.config import FILES_DIRECTORY as CHAINLIT_FILES_DIRECTORY
from chainlit.context import ChainlitContextException
from chainlit.input_widget import TextInput
from chainlit.session import WebsocketSession
from chainlit.utils import utc_now
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError

from src.agent_types import render_agent_types_block, scan_agent_types
from src.chat_log import (
    append_resume_marker,
    append_turn,
    build_log_path,
    find_existing_log_path,
    resolve_log_username,
)
from src.cleanup import cleanup_old_dirs, cleanup_old_files, cleanup_old_files_in_subdirs
from src.cleanup import run_cleanup_dirs_loop as cleanup_run_cleanup_dirs_loop
from src.cleanup import run_cleanup_files_in_subdirs_loop as cleanup_run_cleanup_files_in_subdirs_loop
from src.cleanup import run_cleanup_loop as cleanup_run_cleanup_loop
from src.config import (
    expand_config_vars,
    load_config,
    render_agent_type_run_script_allowlist_block,
    render_memory_visibility_block,
    render_plan_approval_exempt_scripts_block,
    with_reasoning_effort,
)
from src.context_compaction import (
    force_write_thread_note,
    is_compaction_blocked_by_missing_note,
    maybe_compact,
    register_compaction_usage_listener,
    should_compact,
)
from src.files import extract_generated_files
from src.graph import EMPTY_RESPONSE_NUDGE, build_graph, is_empty_final_message
from src.images import is_image_file, load_image_bytes
from src import instance_lock
from src import runtime_status
from src import skill_drafts
from src.version import __version__ as LOCOHANE_VERSION
from src.workdir_suggest import server_hostname, suggest_work_dirs, work_dir_error_message
from src.llm import (
    LLM_CONNECTION_ERRORS,
    ThinkingLoopDetected,
    ToolCallLoopDetected,
    _register_cancel_scope_watcher,
    aclose_active_llm_clients,
    build_model,
    describe_current_task,
    forget_session,
    get_current_session,
    init_llm_concurrency,
    mark_last_endpoint_failed,
    mark_user_turn,
    pick_loop_nudge_message,
    recent_cancel_scope_breakage,
    set_current_session,
    tool_loop_nudge_text,
)
from src.llm.thinking_control import unknown_tool_names
from src.log_rotation import LineCountRotatingFileHandler
from src.mcp_client import init_mcp_tools, shutdown_mcp_tools
from src.memory import MEMORY_INDEX_MARKER, render_memory_block
from src import ask_reconnect
from src.ask_relay import pending_asks as _pending_asks, resolve_pending_ask
from src.plan_persist import plan_message_id, register_plan_persist
from src.project_instructions import render_project_instructions_block
from src.skills import (
    build_system_prompt_from_block,
    filter_skills_for_agent_type,
    filter_skills_for_main_agent_guard,
    render_skills_block,
    render_skills_block_with_guard_annotation,
    render_skills_block_with_hint,
    scan_skills,
)
from src.subagent import is_truncated_result
from src.thread_store import ChatThreadDataLayer, _sanitize_filename_component
from src.tool_labels import tool_label
from src import i18n
from src import thread_store
from src import user_injection
from src.tools import (
    WorkDirAccessStatus,
    cancel_background_script_jobs_for_thread,
    cancel_dispatch_agent_jobs_for_thread,
    forget_session_tool_semaphores,
    get_all_tools,
    init_tools,
    list_blocked_tool_names_for_hint,
    probe_workdir_access,
    register_raw_unc_paths_in_text,
    reset_call_history_guards_after_compaction,
    toggle_plan_mode_from_ui,
)
from src.tools.dispatch_agent_batch import BATCH_GROUP_RUN_NAME, interrupted_note_topic
from src.tools._plan_render import _render_plan_payload
from src.tools._path_memory_helpers import _register_path_memory
from src.tools._state import SESSION_REASONING_EFFORT_KEY
from src.tools._workdir import _build_workdir_status_info, _resolve_exec_workdir
from src.tools.vector_db_tools import render_document_collections_block

# dispatch_agent（サブエージェント）由来のメッセージに付与する author 名。
# frontend/src/utils/messageTree.ts の SUBAGENT_MESSAGE_AUTHOR、
# src/tools.py の _push_dispatch_agent_progress と一致させる
# （UI側でメインエージェントの回答と区別して表示するための識別子）。
SUBAGENT_MESSAGE_AUTHOR = "サブエージェント"

# 内部でサブエージェント（run_subagent）を起動するツール名。これらの tool run_id
# 配下のLLMイベントはサブエージェント由来として扱う（_is_subagent_call 参照）。
_SUBAGENT_DISPATCH_TOOLS = frozenset({"dispatch_agent", "dispatch_agent_batch"})
# ユーザーへ質問して応答を待つツール。孤立した（応答を受け取る前に中断した）ものは、次のユーザー発言が
# その答えでありうると LLM へ伝える（_build_orphaned_placeholder_messages 参照）。
_ASK_USER_TOOLS = frozenset({"ask_user_choice", "AskUserQuestion", "approve_plan"})


def _tool_step_label(event: dict) -> str:
    """on_tool_start イベントから Step の表示名を決める。

    dispatch_agent は表示名の対応表（src/tool_labels.py）だけだと常に「サブエージェント実行」
    表示になり、狭い幅では「ツール: サブエージェ...」と見切れてどの種別
    （explore/verifier等）に委譲したのか分からない。agent_type 引数が
    取れる場合は "SUB: <agent_type>" 形式にする（frontend/src/components/
    StepItem.tsx 側で、この形式の名前だけは "ツール: " を前置しない特別
    扱いにしている）。
    """
    name = event["name"]
    if name in _SUBAGENT_DISPATCH_TOOLS:
        tool_input = event["data"].get("input")
        agent_type = tool_input.get("agent_type") if isinstance(tool_input, dict) else None
        if agent_type:
            return f"SUB: {agent_type}{i18n.t('step.batch_suffix')}" if name == "dispatch_agent_batch" else f"SUB: {agent_type}"
    return tool_label(name)

# アプリ全体で共有する状態（起動時に一度だけ構築）。
# グラフ（LLMモデル・httpxクライアントを含む）はここには含まない。
# セッション（タブ）ごとに個別構築し cl.user_session["graph"] に保持する
# （理由は本ファイル冒頭のモジュールdocstring参照）。
# chainlit run はモジュール読み込み直後に ensure_jwt_secret() を呼び、
# password_auth_callback が登録済みなら CHAINLIT_AUTH_SECRET の有無を検査する。
# そのため config 読み込みと認証コールバックの登録はモジュールのトップレベル
# （_setup() 内の遅延初期化より前）で行う必要がある。
_config = load_config()
i18n.set_default_language(_config.ui_default_language)
# セッションごとのグラフ構築（on_chat_start）・再構築（_rebuild_graph）に
# 使い回すため保持しておく。
_system_prompt = None
_checkpointer = None
# 会話スレッド一覧（左サイドバー）・再開機能用の軽量ストア（[thread_store].enabled
# のときのみ使う）。接続はアプリ寿命で1つを共有する（_checkpointer と同じ方針）。
_thread_store_conn = None
_thread_data_layer: ChatThreadDataLayer | None = None
# 現在ターン処理中（LLM呼び出し・ツール実行の真っ最中）の thread_id 集合。
# セッション（cl.user_session）を跨いだプロセス全体の一時状態で、DBには
# 永続化しない（プロセス再起動をまたいで意味を持つ情報ではないため）。
# 左サイドバーが「別の会話が裏で処理中」を表示するために、
# on_message の開始/終了で _mark_thread_generating/_unmark_thread_generating が
# 更新し、/locohane/threads がこれを参照する。
_generating_thread_ids: set[str] = set()
# thread_id -> 実際に処理中の asyncio.Task（on_message の外側タスク）。
# 別セッション（このスレッドを開いただけの閲覧側）から停止ボタンを押したときに
# 該当タスクを cancel() するための参照（_stop_thread_generating 参照）。
# _generating_thread_ids とは別集合にしてあるのは、こちらは同期関数の単体テスト
# （test_mark_and_unmark_are_idempotent）からも安全に呼べるようにするため
# （asyncio.current_task() の取得はイベントループ内でしか行えない）。
_generating_thread_tasks: dict[str, "asyncio.Task"] = {}
# thread_id -> chat_logファイルへの追記を直列化する asyncio.Lock。
# _generating_owner_threads は「同じ所有者」が別スレッドで同時に2つ目の
# ターンを開始するのは防ぐが、同一thread_idを複数タブで開いて両方から
# ほぼ同時に送信するケースまでは防がない（意図的な設計。同じスレッドを
# 別タブで開いて生成をリアルタイム閲覧できる _make_relayed_emit の仕組みが
# 前提にある）。append_turn() はログファイルへ複数回 f.write() する非
# アトミックな追記のため、ロック無しだと2つのターンの USER:/AI: 行が
# 交錯して壊れたログになりうる（2026-09-04 レビュー指摘）。
#
# thread_idはUUIDで使い捨て（同じIDが再利用されない）ため、
# _generating_thread_tasks等と同じ「ターン終了時にpopする」方式では、他
# タブの同時ターンがまだロックを保持中に pop されると新規Lockが作られて
# 排他が破れるレースがある。WeakValueDictionaryにして、そのLockを誰も
# 参照しなくなった時点でGC任せに自動で片付ける（2026-09-04 レビュー指摘）。
_chat_log_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()
# owner（thread_store.resolve_owner の結果）-> 現在生成中のthread_id。
# 同じ所有者（匿名モードなら"anonymous"に一元化、認証ありならログイン
# ユーザー単位）が、別タブ/別スレッドから同時に2つ目のターンを開始する
# （新規チャットでの送信・他スレッドでの送信）のを防ぐために使う
# （LangGraphのcheckpointerは1 thread_idにつき1回の会話進行を前提とした
# 設計であり、同じ所有者が複数スレッドで同時にLLM呼び出しを行うと
# サーバー・GPUリソースを圧迫するうえ、そもそも「一度に1つの会話しか
# 進めない」という単純な運用制約を想定している。2026-08-19 ユーザー要望）。
# 値は常に1つのthread_idのみ（同じ所有者が同時に処理できるスレッドは1つに
# 制限する）。on_message側の直前チェックからマーク/解除まで await を挟まない
# ため、単一プロセスのasyncioイベントループ上で競合なく安全に更新できる。
_generating_owner_threads: dict[str, str] = {}
# thread_id -> そのターンを開始した cl.context.session.id（Chainlitのセッション
# ID。sessionIdState としてフロントにも渡っている値と同じ）。
# /locohane/threads/{id}/status がこれを見て「生成中ではあるが、それは
# 質問元セッション自身のターンである」場合に isGenerating=false を返すために使う
# （2026-08-19 ユーザー報告: 自分自身のターンなのに「他のセッションで生成中」の
# バナー・入力欄無効化が誤って出て、完了扱いで window.location.reload() まで
# 発火し、URLに?threadが無い新規チャットとして再接続され「無題の会話」が
# 増殖するバグがあった）。
_generating_thread_session_ids: dict[str, str] = {}
# thread_id -> ターン処理を開始した日時（ISO 8601、UTC）。設定ダッシュボードの
# 「生成中スレッド」の経過時間表示用（_collect_runtime_status 参照）。
_generating_thread_started_at: dict[str, str] = {}
# session.emit を中継してよいイベントの許可リスト（内容の表示に関わるものだけ）。
# 'ask'系プロンプトは session.emit_call 経由で送られるため中継対象に含まれず、
# 閲覧側セッションに二重の承認UIが出る心配は無い。task_start/task_end もあえて
# 対象外にしている（_make_relayed_emit のdocstring参照）。
_RELAYED_EMIT_EVENTS = frozenset(
    {"new_message", "update_message", "delete_message", "stream_start", "stream_token", "element"}
)


def _mark_thread_generating(thread_id: str, owner: str | None = None, session_id: str | None = None) -> None:
    _generating_thread_ids.add(thread_id)
    _generating_thread_started_at[thread_id] = runtime_status.now_iso()
    if owner is not None:
        _generating_owner_threads[owner] = thread_id
    if session_id is not None:
        _generating_thread_session_ids[thread_id] = session_id


def _unmark_thread_generating(thread_id: str, owner: str | None = None) -> None:
    _generating_thread_ids.discard(thread_id)
    _generating_thread_tasks.pop(thread_id, None)
    _generating_thread_session_ids.pop(thread_id, None)
    _generating_thread_started_at.pop(thread_id, None)
    if owner is not None and _generating_owner_threads.get(owner) == thread_id:
        _generating_owner_threads.pop(owner, None)


# thread_id -> 応答待ち（approve_plan/ask_user_question/ask_user_choice の
# Ask*Message）の引き継ぎ用状態。
# Sidebar.goToThread()（frontend/src/components/Sidebar.tsx）はスレッド切り替え時に
# フルページリロードするため、sessionIdState は毎回 uuid4() で新規発行され直す
# （@chainlit/react-client の SessionId atom）。生成中スレッドから離れて
# 応答待ちの状態のまま戻ってきたセッションは、Chainlit的には元のセッションとは
# 完全な別物であり、二度と元のWebSocket/emitterには紐づかない。素の
# cl.AskActionMessage/AskUserMessage/AskElementMessage.send() は元セッションの
# emitterへ送信済みのまま応答不能（相手のWebSocketが既に切断済み）になり、
# ボタン・入力フォームが戻ってきたセッションに一切表示されない
# （2026-08-21 ユーザー報告。当初は approve_plan のみで確認されたが、同じ
# 仕組みの ask_user_question/ask_user_choice にも同一の欠落があったため
# 2026-08-22 に一般化）。
# ここに登録した内容を on_chat_resume が検知し、今まさに繋がっているセッション
# 側で同じAsk*Messageを出し直し、回答をこの Future 経由で元セッション側の
# 待機（各ask系ツール）へ引き継ぐ。
# 登録・解除は src/tools.py 側（各ask系ツール）から行うため、状態そのものは
# src/ask_relay.py（app.py/src/tools.py どちらにも属さない中立モジュール）に
# 置く。以前は app.py 側に置いて src/tools.py から `from app import ...` で
# 遅延importしていたが、chainlit run はapp.pyを "app" という名前で
# sys.modules に登録しないため、その import がapp.py全体を別モジュールとして
# 再実行してしまい、@cl.on_message 等のグローバルなハンドラ登録を壊れた状態の
# ものに上書きしてしまう不具合があった（src/ask_relay.py のdocstring参照）。


async def _relay_pending_ask(thread_id: str) -> None:
    """on_chat_resume から起動する。指定スレッドに未解決の応答待ちがあれば、
    今このセッション（＝実際にブラウザと繋がっている生きたWebSocket。
    Chainlit自体がon_chat_resumeハンドラ実行前にcl.contextをこの新セッション
    へ束縛済み）で同じAsk*Messageを出し直し、回答を _pending_asks の
    Future 経由で元セッション（approve_plan/ask_user_question/ask_user_choice
    を呼んだ側）へ引き継ぐ。

    元セッション側の Ask*Message.send() はフルページリロードで切断済みの
    相手（sid）へ送られたままのため、自然には応答が返らずタイムアウト
    するだけになる（この関数を経由しない限り、戻ってきたセッションに
    ボタン・入力フォームは一切出ない）。

    実体は src/ask_relay.py の resolve_pending_ask（既に接続済みのまま
    留まっているセッションへの中継 dispatch_to_live_viewers とも共有する
    共通ロジック）に委譲するだけの薄いラッパー。
    """
    await resolve_pending_ask(thread_id)


def _make_relayed_emit(session, thread_id: str, original_emit):
    """session.emit を、同じ thread_id を今まさに開いている他セッションにも
    中継するようラップして返す（生成中のスレッドを別セッションで開いたときに、
    そこにも思考ステップ・回答のストリーミングが見えるようにするための処置。
    2026-08-19 ユーザー報告: サイドバーのインジケーターだけでは、実際に
    そのスレッドを開いた画面自体は生成中の内容が全く見えず「止まって見える」
    問題が残っていた）。

    中継は _RELAYED_EMIT_EVENTS の許可リストに限定する。'ask'系（Plan Mode
    承認等の対話プロンプト）は session.emit ではなく session.emit_call
    経由で送られるため、そもそもこの関数の対象外＝自動的に中継されない
    （閲覧側セッションは常に読み取り専用のまま）。task_start/task_end も
    意図的に中継しない — 中継すると閲覧側の useChatData().loading が真になり
    Chainlit純正の「停止」ボタンが表示されてしまうが、それは閲覧側の
    session.current_task（常にNone）しか操作できず実際の生成テスクは
    止められない、見た目だけのボタンになってしまう（停止は
    /locohane/threads/{id}/stop に一本化する。_stop_thread_generating 参照）。

    ChatThreadDataLayer.create_step/update_step は元セッション側で通常通り
    呼ばれ続けるため、DBへの永続化（=あとから開いたときの再現）はこの中継の
    有無に関わらず従来通り機能する。中継はあくまで「今まさに開いている」
    セッションへのライブ配送のみを担う。
    """
    from chainlit.session import ws_sessions_sid

    def relayed_emit(event, data):
        if event in _RELAYED_EMIT_EVENTS:
            for other in list(ws_sessions_sid.values()):
                if other is session or other.thread_id != thread_id:
                    continue
                try:
                    asyncio.create_task(other.emit(event, data))
                except Exception:  # noqa: BLE001 - 中継はベストエフォート
                    logging.getLogger(__name__).debug(
                        "Failed to relay emit to the thread (event=%s)", event, exc_info=True
                    )
        return original_emit(event, data)

    return relayed_emit


def _close_user_injection_on_stop(thread_id: str) -> None:
    """停止ボタンで、生成中に送られたユーザーの追加指示の受付をその場で閉じる。

    タスクのキャンセルが実際に効くまで（後始末の await 中など）受付を開いたままにすると、
    停止後に指示が会話へ入ってしまう。フロントエンドは停止の時点で、まだ会話へ入っていない
    指示を入力欄へ戻すため、二重になる。
    """
    dropped = user_injection.close_thread(thread_id)
    if dropped:
        logging.getLogger(__name__).info(
            "Dropped %d additional user instruction(s) because the turn was stopped thread_id=%s",
            len(dropped),
            thread_id,
        )


async def _stop_thread_generating(thread_id: str) -> bool:
    """他セッション（このスレッドを開いただけの閲覧側）から、生成中タスクを
    止める。@cl.on_stop の thread_id 指定版（app.py:1175 on_stop 参照）。

    session.current_task.cancel() 相当（対象タスクへ asyncio.CancelledError
    を投げ込む）に加え、on_stop() と同様に aclose_active_llm_clients で
    LLMサーバーへのHTTP接続も強制切断する（cancel()だけでは接続が生きたまま
    CPU/GPU使用率が下がらないため。on_stop()のdocstring参照）。

    session.current_task.cancel() は dispatch_agent の job.runner_task・
    run_script_background/execute_python_code_background の job.runner_task
    （いずれも asyncio.shield() で保護され、メイングラフのタスクとは別に
    生き続ける）には届かない。放置すると、停止ボタン押下後もサブエージェント
    の実行・スクリプト実行・進捗pushが裏側で動き続けてしまう（on_stop()の
    docstring・cancel_dispatch_agent_jobs_for_thread参照）ため、ここで併せて
    強制終了する。

    重要: メイングラフのタスク（_generating_thread_tasks[thread_id]）が
    既に None/done() であっても、これらのバックグラウンドジョブは
    まだ動いている可能性がある（dispatch_agent が安全上限超過で
    job_id を返してターンを終えた場合、あるいは1回目の停止ボタンで
    メインタスク自体は既にcancelled=doneになった後、別タブ/再度の停止
    リクエストがこの関数に来た場合）。メインタスクの有無で早期return
    すると、この最も典型的なシナリオでバックグラウンドジョブの強制終了が
    一度も行われなくなってしまう（2026-08-26調査で確認）ため、メインタスク
    の状態に関わらずジョブの強制終了は必ず試みる。

    on_stop()と異なりグラフの再構築（_rebuild_graph）はここでは行わない。
    呼び出し元は「このスレッドを開いただけの閲覧側」であり、
    cl.user_session はそのセッション自身のものしか書き換えられないため、
    ここで _rebuild_graph を呼ぶと新しいグラフが無関係な閲覧側セッションへ
    紐づいてしまい、肝心の生成元セッションには反映されない。生成元セッションは
    次に resume/reconnect した際 on_chat_resume が _rebuild_graph(thread_id)
    を呼ぶため、そこで自然に復旧する。
    """
    task = _generating_thread_tasks.get(thread_id)
    task_was_running = task is not None and not task.done()
    # キャンセルが実際に効くのを待たずに、追加指示の受付を閉じる（src/user_injection.py 冒頭参照）。
    _close_user_injection_on_stop(thread_id)
    if task_was_running:
        task.cancel()
    # dispatch_agent/run_script_background系いずれかのジョブ強制終了で
    # 例外が起きても、もう一方およびLLMクライアントの強制クローズを
    # 妨げないよう、独立させつつ並行して試みる。
    jobs_killed = False
    job_results = await asyncio.gather(
        cancel_dispatch_agent_jobs_for_thread(thread_id),
        cancel_background_script_jobs_for_thread(thread_id),
        return_exceptions=True,
    )
    for result in job_results:
        if isinstance(result, BaseException):
            logging.getLogger(__name__).debug(
                "cross-session stop: failed to kill background jobs [thread_id=%s]",
                thread_id,
                exc_info=result,
            )
        elif result:
            jobs_killed = True
    try:
        await aclose_active_llm_clients(thread_id)
    except Exception:  # noqa: BLE001 - ベストエフォート、失敗してもcancel()自体は有効
        logging.getLogger(__name__).debug(
            "cross-session stop: failed to force-close the LLM client [thread_id=%s]",
            thread_id,
            exc_info=True,
        )
    return task_was_running or jobs_killed


class _CheckpointerTimeout(Exception):
    """checkpointer操作のタイムアウト検知用例外。"""


class _CheckpointerConnectionClosed(_CheckpointerTimeout):
    """checkpointer操作の実行中にDB接続が閉じられていたことを示す例外。

    aiosqliteはバックグラウンドスレッドのキューに操作を積んだ後、実際に
    その操作が処理される前に別タスクが接続をclose()すると
    ValueError("no active connection" / "Connection closed") を送出する
    （アプリシャットダウン処理やcheckpointer再構築との競合。issue.md
    「DB接続切れエラー（no active connection）」参照）。on_message側は
    _CheckpointerTimeout を捕捉してcheckpointer再構築へ倒す設計のため、
    そのサブクラスとして扱い同じ復旧経路（turn_broken_exc +
    checkpointer_needs_rebuild）に合流させる。
    """


class _TimeoutGuardedSqliteSaver(AsyncSqliteSaver):
    """AsyncSqliteSaverをラップし、全操作にタイムアウトを付与する。

    langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver の self.lock は
    asyncio.Lock のインスタンスでタイムアウト機構がない。aiosqliteの
    バックグラウンドスレッドが固着するとロックが永久に解放されず、
    以降の全操作がハングする。本ラッパーは asyncio.timeout() で
    操作全体を包み、超過時に _CheckpointerTimeout を送出する。
    """

    async def _guarded(self, coro):
        """コルーチンを [checkpointer].op_timeout_seconds でタイムアウト包む。

        DB接続がクローズ済みの状態で操作が実行された場合の ValueError
        （_CheckpointerConnectionClosed のdocstring参照）もここで検知し、
        未処理のまま伝播させて on_message をクラッシュさせる代わりに
        _CheckpointerTimeout系の復旧経路へ変換する。
        """
        try:
            async with asyncio.timeout(_config.checkpointer_op_timeout_seconds):
                return await coro
        except ValueError as exc:
            if "no active connection" in str(exc) or "Connection closed" in str(exc):
                raise _CheckpointerConnectionClosed(str(exc)) from exc
            raise

    async def aget_tuple(self, config):
        return await self._guarded(super().aget_tuple(config))

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self._guarded(super().aput(config, checkpoint, metadata, new_versions))

    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await self._guarded(super().aput_writes(config, writes, task_id, task_path))

    async def adelete_thread(self, thread_id):
        return await self._guarded(super().adelete_thread(thread_id))

    async def aget_delta_channel_history(self, *, config, channels):
        return await self._guarded(super().aget_delta_channel_history(config=config, channels=channels))


async def _build_checkpointer():
    """新しいチェックポインタを構築して返す（共通処理）。"""
    conn = await aiosqlite.connect(str(_config.checkpoint_db))
    saver = _TimeoutGuardedSqliteSaver(conn)
    await saver.setup()
    return saver


async def _rebuild_checkpointer(thread_id: str):
    """チェックポインタを新しい接続に差し替える（旧接続はベストエフォートclose）。"""
    global _checkpointer
    new_saver = await _build_checkpointer()
    old_saver = _checkpointer
    _checkpointer = new_saver
    if old_saver is not None:
        try:
            async with asyncio.timeout(_config.checkpointer_close_timeout_seconds):
                await old_saver.conn.close()
        except Exception:  # noqa: BLE001 - リークを許容してログのみ
            logging.getLogger(__name__).debug(
                "Failed to close the old checkpointer connection (leak tolerated)",
                exc_info=True,
            )


if _config.auth_enabled:
    if not os.environ.get("CHAINLIT_AUTH_SECRET"):
        raise RuntimeError(
            "[auth] enabled=true in config.ini, but CHAINLIT_AUTH_SECRET is not set in .env. Add the output of `chainlit create-secret` to .env."
        )
    if not _config.auth_users:
        raise RuntimeError(
            "[auth] enabled=true in config.ini, but AUTH_USERS in .env is not set or empty. Register at least one username/password pair."
        )

    @cl.password_auth_callback
    async def auth_callback(username: str, password: str) -> cl.User | None:
        """[auth] enabled=true のときのみ登録されるログイン検証コールバック。

        require_password=true: ユーザー名・パスワードともに一致必須
            （hmac.compare_digest でタイミング攻撃を軽減した定数時間比較）。
        require_password=false: AUTH_USERS に登録済みのユーザー名であれば
            パスワードの内容を問わず通す。
        """
        stored_password = _config.auth_users.get(username)
        if stored_password is None:
            return None
        if _config.auth_require_password and not hmac.compare_digest(password, stored_password):
            return None
        return cl.User(identifier=username)


if _config.thread_store_enabled:

    @cl.data_layer
    def _get_thread_data_layer():
        """会話スレッド一覧・再開用の BaseDataLayer を Chainlit へ登録する。

        config.code.data_layer には関数（ファクトリ）を渡すだけでよく、実際の
        呼び出し（このファクトリの実行）は Chainlit 側が初回 get_data_layer()
        時に1回だけ遅延実行する。Chainlit本体は `/project/settings` 等、
        WebSocket接続より前に届くHTTPリクエストの中でも get_data_layer() を
        呼ぶため、_thread_data_layer の実体は @cl.on_app_startup（_on_app_startup）
        で、サーバーがリクエストの受付を開始する前に必ず構築しておく
        （この関数定義自体はそれより前で構わない。遅延実行されるため）。
        """
        assert _thread_data_layer is not None, "The data layer was accessed before _on_app_startup() completed"
        return _thread_data_layer


def _patch_chainlit_anonymous_resume() -> None:
    """[auth] enabled=false（匿名モード）でもスレッド再開（on_chat_resume）が
    発火するようにする、chainlit.socket.resume_thread のモンキーパッチ。

    Chainlit本体の resume_thread()（chainlit/socket.py）は
    `if not data_layer or not session.user or not session.thread_id_to_resume: return`
    という無条件ガードを持つ。匿名モードでは connect ハンドラが
    require_login()=False のため session.user を一切設定せず常に None のまま
    のため、パッチ無しではスレッドが存在してもこのガードで毎回弾かれ
    on_chat_resume が永久に呼ばれない。

    resume_thread は chainlit/socket.py 内で無条件名参照（同一モジュール内の
    呼び出し）されるため、chainlit.socket.resume_thread をこの関数で
    差し替えるだけで確実に効く（_patch_chainlit_connection_successful_task_end
    のように @sio.on() でイベントハンドラ自体を再登録する必要は無く、
    登録順を気にしなくてよい）。

    所有者の解決自体（会話ログ・スレッド一覧のバケット分け）はこの関数とは
    無関係に thread_store._current_owner() が cl.context 経由で毎回独立に
    行う（session.user が None のままでも "anonymous" に解決される）。
    ここで補うのは、あくまで resume_thread() 内の truthy性チェックを
    通すためだけの最小限の処置。
    """
    import chainlit.socket as _cl_socket

    _original_resume_thread = _cl_socket.resume_thread

    async def _resume_thread_anonymous(session):
        if session.user is None:
            session.user = cl.User(identifier=thread_store.ANONYMOUS_OWNER)
        return await _original_resume_thread(session)

    _cl_socket.resume_thread = _resume_thread_anonymous
    logging.getLogger(__name__).info("Applied the patch enabling thread resume in anonymous mode.")


def _patch_chainlit_disable_disconnect_thread_touch() -> None:
    """会話履歴を開いて（別スレッドへクリックで移動して）閉じるだけで、その
    スレッドが一覧の先頭にジャンプしてしまう不具合を防ぐ、
    chainlit.socket.persist_user_session の無効化パッチ（2026-08-19
    ユーザー報告）。

    chainlit.socket.disconnect()（WebSocket切断時に必ず呼ばれる）は、
    `if session.thread_id and session.has_first_interaction:` の条件下で
    無条件に `persist_user_session(thread_id, session.to_persistable())`
    を呼ぶ。session.has_first_interaction はスレッド再開（resume_thread）
    でも True になるため、単に会話を開いて（サイドバーの左サイドバーの
    goToThread() によるハードナビゲーションで）別のスレッドへ移ると、
    直前まで見ていたスレッドで必ずこれが発火する。
    persist_user_session() は data_layer.update_thread(thread_id,
    metadata=...) を呼び、ChatThreadDataLayer.update_thread → save_thread
    は呼ばれるたびに無条件で updated_at を更新するため、実際の会話進行が
    一切無くても「見て、離れた」だけでそのスレッドが一覧の先頭に来てしまう
    （list_threads_summary は updated_at DESC でソートするため）。

    session.to_persistable() が持つ情報（chat_settings/chat_profile/
    client_type + cl.user_session の中身）は、このアプリでは on_message が
    ターン完了ごとに thread_store へ明示的にスナップショットする値
    （work_dir/plan/token使用量。on_chat_resume 参照）と完全に重複して
    おり、このアプリ自身はこの汎用persist機構に一切依存していない。その
    ため丸ごと no-op化しても実害が無く、意図した通り「実際に会話が進んだ
    ときだけ updated_at が動く」に戻せる。
    """
    import chainlit.socket as _cl_socket

    async def _persist_user_session_disabled(thread_id: str, metadata: dict) -> None:
        return None

    _cl_socket.persist_user_session = _persist_user_session_disabled
    logging.getLogger(__name__).info(
        "Disabled automatic metadata saving on session disconnect, so merely opening and leaving a conversation does not change the list order."
    )


def _patch_chainlit_websocket_ping_timeout() -> None:
    """WebSocket（Socket.IO）の ping_interval/ping_timeout を [websocket]
    （config.ini）の値で上書きする。

    chainlit/server.py の `sio = socketio.AsyncServer(...)` は
    ping_interval/ping_timeout を指定していないため、python-socketio/
    engineio のデフォルト（ping_interval=25秒, ping_timeout=20秒）がそのまま
    使われる。LLM応答待ちでイベントループがブロッキング気味になる時間帯
    （dispatch_agent実行中など）にこの20秒を超えると、バックエンド処理自体は
    問題なく継続しているにもかかわらずWebSocketが切断され、再接続まで
    Step更新（サイドパネルのタイムライン）がUIに反映されない不具合が起きる
    （2026-08-28 ユーザー報告。1ターン中に切断が5回連発し、workerによる
    修正ステップがタイムラインから丸ごと欠落した実例で確認）。

    socketio.AsyncServer は内部で engineio.AsyncServer を self.eio として
    保持しており、ping_interval/ping_timeout は単純な数値属性のため
    インスタンス生成後でも書き換えられる（sio生成はchainlitのモジュール
    import時点で完了済みなので、on_app_startup時点で確実に上書きできる）。
    """
    from chainlit.socket import sio as _sio

    _sio.eio.ping_interval = _config.websocket_ping_interval_seconds
    _sio.eio.ping_timeout = _config.websocket_ping_timeout_seconds
    logging.getLogger(__name__).info(
        "Applied WebSocket ping_interval=%ss ping_timeout=%ss.",
        _config.websocket_ping_interval_seconds,
        _config.websocket_ping_timeout_seconds,
    )


# チャット送信ではなく独自フロントエンドのUIボタン（ツールバーの各アイコン）から
# 呼ばれる action_callback。これらのクリックは「最初の発言」ではないが、
# Chainlit本体側にその区別が無い（_patch_chainlit_ignore_ui_action_first_interaction
# のdocstring参照）。
_NON_CHAT_ACTION_NAMES = frozenset(
    {"pick_work_dir", "suggest_work_dir", "work_dir_server_info", "toggle_plan_mode", "set_language", "reasoning_effort_info"}
)


def _patch_chainlit_ignore_ui_action_first_interaction() -> None:
    """作業フォルダ選択・Plan Modeバッジ等のUIボタンを、送信前に押しただけで
    会話履歴（左サイドバー）に "pick_work_dir" 等という名前の会話が作られて
    しまう不具合を防ぐ、chainlit.emitter.ChainlitEmitter.flush_thread_queues
    のモンキーパッチ（2026-08-19 ユーザー報告）。

    Chainlit本体の /project/action ハンドラ（chainlit/server.py call_action）は、
    action_callback を呼ぶ前に無条件で
    `if not session.has_first_interaction: session.has_first_interaction = True;
    emitter.init_thread(action.name)` を実行する。これは元々「チャット入力欄に
    最初の発言をした」ことを検知するための仕組みだが、action_callback（ツール
    バーのボタン全般）にも同じ判定が働いてしまうため、区別が無い。
    init_thread() は flush_thread_queues(interaction) を呼び、
    (1) スレッド名を interaction（= 素の action.name 文字列）に設定し、
    (2) @queue_until_user_message() で保留されていた create_step 等
        （on_chat_start のwelcomeメッセージ等）を今すぐ永続化してしまう。
    結果、ユーザーが1文字も発言していなくても "pick_work_dir" という名前の
    会話が左サイドバーに現れる。

    対策として、interaction が _NON_CHAT_ACTION_NAMES（UIボタン由来）のときは
    flush_thread_queues の実処理を丸ごとスキップし、代わりに
    session.has_first_interaction を False に戻す。これにより
    「まだ最初の発言をしていない」状態に復元され、キュー済みのステップは
    保留され続け、実際に最初のチャットメッセージが送られてきた時点で
    （chainlit/emitter.py process_message の同じ判定により）正しく
    フラッシュ・命名される。UIボタンだけクリックして一度もチャットしなかった
    セッションでは、会話は一切永続化されない（元々の望ましい挙動）。
    """
    import chainlit.emitter as _cl_emitter

    _original_flush_thread_queues = _cl_emitter.ChainlitEmitter.flush_thread_queues

    async def _flush_thread_queues_ignoring_ui_actions(self, interaction: str):
        if interaction in _NON_CHAT_ACTION_NAMES:
            self.session.has_first_interaction = False
            return
        await _original_flush_thread_queues(self, interaction)

    _cl_emitter.ChainlitEmitter.flush_thread_queues = _flush_thread_queues_ignoring_ui_actions
    logging.getLogger(__name__).info(
        "Applied the patch preventing UI button (%s) clicks from wrongly triggering conversation naming and early persistence.",
        ", ".join(sorted(_NON_CHAT_ACTION_NAMES)),
    )


if _config.thread_store_enabled:
    from chainlit.auth import get_current_user as _cl_get_current_user
    from chainlit.server import app as _chainlit_asgi_app
    from fastapi import Depends, HTTPException
    from fastapi.responses import FileResponse
    from pydantic import BaseModel

    # chainlit/server.py はモジュール読み込み時（import chainlit の時点で確定）に
    # `@router.get("/{full_path:path}")`（SPA配信用のcatch-all）を含む router を
    # `app.include_router(router)` で丸ごと登録済み。Starlette は app.router.routes
    # を登録順に walk して最初にマッチしたものを使うため、この後 @_chainlit_asgi_app.get(...)
    # で追加するルートは routes リストの末尾に足されるだけでは catch-all より後ろになり、
    # 何を追加しても catch-all（あらゆるパスにマッチする）に先に食われて絶対に呼ばれない
    # （200 OK で index.html が返るだけで例外も出ないため気づきにくい）。
    # このため、登録直後に自分が追加した分だけ catch-all の手前へ並べ替える
    # （_reorder_locohane_routes_before_spa_catchall 参照）。
    _routes_before_registration = len(_chainlit_asgi_app.router.routes)

    class _RenameThreadBody(BaseModel):
        name: str

    async def _assert_owns_thread(thread_id: str, current_user) -> None:
        """指定スレッドの所有者が current_user（匿名モードでは None）と一致するか確認する。

        Chainlit公式の /project/threads* は匿名モードで current_user が常に
        None になり無条件401になるため使えない（chainlit.auth.get_current_user
        は require_login()=False のとき常に None を返す設計）。このアプリ独自の
        ルートは thread_store.resolve_owner(None) が "anonymous" に解決される
        ことを利用し、認証あり/なし両方を同じコードパスで扱う。
        """
        if _thread_store_conn is None:
            # _setup() が一度も完了していない（起動直後にブラウザ側の
            # REST呼び出しがWebSocket接続より先に届いた等）。この時点では
            # どのスレッドも存在しえないため、素直に見つからない扱いにする。
            raise HTTPException(status_code=404, detail="Thread not found")
        owner = await thread_store.get_thread_owner(_thread_store_conn, thread_id)
        if owner is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        if owner != thread_store.resolve_owner(current_user):
            raise HTTPException(status_code=404, detail="Thread not found")

    @_chainlit_asgi_app.get("/locohane/threads")
    async def _locohane_list_threads(
        limit: int = 30,
        before: str | None = None,
        session_id: str | None = None,
        current_user=Depends(_cl_get_current_user),
    ):
        """左サイドバー用の軽量なスレッド一覧（{id,name,updatedAt,isGenerating}[]）。

        Chainlit公式の /project/threads は匿名モードで使えないため独自実装
        （_assert_owns_thread のdocstring参照）。認証ありモードでは
        current_user がログイン中のユーザーに解決され、そのユーザー分だけを返す。

        isGenerating は _generating_thread_ids（on_message の薄いラッパーが
        管理するプロセス全体の一時状態）との突き合わせ。他のブラウザタブ・
        別スレッドを開いた同じタブから見て「このスレッドは今も裏で処理中」を
        表すための項目で、フロントは定期的にこのエンドポイントを
        ポーリングして表示を更新する（ライブpushの仕組みが無いため）。
        session_id（sessionIdState）を渡すと、そのスレッドを生成中なのが
        呼び出し元自身のセッションである場合は isGenerating=false になる
        （/locohane/threads/{id}/status のdocstring参照。自分自身の生成中
        スレッドにもパルスドットが出てしまう見た目の紛らわしさを防ぐ）。

        isWaitingForUser は _pending_asks（src/ask_relay.py。approve_plan/
        ask_user_question/ask_user_choice が応答待ちの間だけ thread_id が
        キーとして存在する）との突き合わせ。「生成中」ではあるがユーザーの
        入力・選択を待って止まっている状態を区別して示すための項目で、
        isGenerating と同じ session_id 除外（自分自身が今まさにその
        Ask*Messageを見ているセッションでは出さない）を適用する
        （2026-08-22 ユーザー要望: 生成中スレッドが他セッションでask系ツールに
        よりユーザーアクション待ちになったことをサイドバーで区別したい）。
        """
        if _thread_store_conn is None:
            # _setup() が一度も完了していない（起動直後にブラウザ側のREST呼び出しが
            # WebSocket接続より先に届いた等）。空一覧を返せば十分（実害なし、
            # フロントは on_chat_start 完了後に再度取得し直す）。
            return {"threads": [], "nextCursor": None}
        owner = thread_store.resolve_owner(current_user)
        items, next_cursor = await thread_store.list_threads_summary(_thread_store_conn, owner, limit, before)
        for item in items:
            is_own_session = session_id is not None and _generating_thread_session_ids.get(item["id"]) == session_id
            is_generating = item["id"] in _generating_thread_ids
            if is_generating and is_own_session:
                is_generating = False
            item["isGenerating"] = is_generating
            is_waiting = item["id"] in _pending_asks
            if is_waiting and is_own_session:
                is_waiting = False
            item["isWaitingForUser"] = is_waiting
        return {"threads": items, "nextCursor": next_cursor}

    @_chainlit_asgi_app.put("/locohane/threads/{thread_id}")
    async def _locohane_rename_thread(
        thread_id: str,
        payload: _RenameThreadBody,
        current_user=Depends(_cl_get_current_user),
    ):
        await _assert_owns_thread(thread_id, current_user)
        await thread_store.rename_thread(_thread_store_conn, thread_id, payload.name)
        return {"success": True}

    @_chainlit_asgi_app.delete("/locohane/threads/{thread_id}")
    async def _locohane_delete_thread(thread_id: str, current_user=Depends(_cl_get_current_user)):
        """生成中のスレッドは削除させない。

        削除すると _generating_thread_tasks/_generating_thread_ids 等の
        紐付け先スレッドが消え、/locohane/threads/{id}/stop
        （_stop_thread_generating参照）で該当タスクを cancel() する手段を
        失ってしまう（削除後もタスク自体は裏で動き続け、CPU/GPUを専有し
        続ける）。session_id による自セッション除外（isGenerating表示用の
        /locohane/threads・.../status とは異なり）はここでは行わず、
        自分自身が今まさに生成中の会話を削除しようとした場合も一律で拒否する。
        """
        await _assert_owns_thread(thread_id, current_user)
        if thread_id in _generating_thread_ids:
            raise HTTPException(status_code=409, detail="This conversation is generating and cannot be deleted. Stop it first, then delete it.")
        await thread_store.delete_thread_row(_thread_store_conn, thread_id, _config.elements_dir)
        return {"success": True}

    @_chainlit_asgi_app.get("/locohane/threads/{thread_id}/status")
    async def _locohane_thread_status(
        thread_id: str,
        session_id: str | None = None,
        current_user=Depends(_cl_get_current_user),
    ):
        """今開いているスレッドが、他セッションで今も生成中かどうかの単発確認用。

        /locohane/threads の一覧は limit 件でページングされ、かつ更新頻度次第では
        開いているスレッドがそこに含まれるとは限らない。フロントエンド
        （App.tsx）は現在表示中のスレッドについてこのエンドポイントを個別に
        ポーリングし、他セッションで処理中なら入力欄を無効化し、処理完了を
        検知した時点で再読み込みして確定内容を取り込む（生成中に別スレッドへ
        移動すると、元のセッションの画面が更新されないまま「会話が終了した
        ように見える」というユーザー報告（2026-08-19）への対応。この画面自体は
        別セッションの生成タスクを引き継げないため、真のライブストリーミング
        ではなく「待って自動再読み込み」に倒す）。

        session_id（sessionIdState。呼び出し元自身の Chainlit セッションID）を
        渡すと、そのスレッドを生成中なのが呼び出し元自身のセッションである場合
        isGenerating=false を返す。これが無いと、自分自身のターンでも
        Plan Mode承認待ち等で一時的に useChatData().loading が false になる
        瞬間（session.emit_call経由の"ask"はtask_end/task_startを挟むため）
        フロントが「他セッションが生成中」と誤検知し、待っても終わらない
        ため window.location.reload() が発火して自分自身のターンを見失い、
        URLに?threadが無い新規チャットとして再接続されてしまう
        （2026-08-19 ユーザー報告: 送信するたびに無題の会話が増殖するバグ）。
        """
        await _assert_owns_thread(thread_id, current_user)
        is_generating = thread_id in _generating_thread_ids
        if is_generating and session_id is not None and _generating_thread_session_ids.get(thread_id) == session_id:
            is_generating = False
        return {"isGenerating": is_generating}

    @_chainlit_asgi_app.get("/locohane/threads/{thread_id}")
    async def _locohane_get_thread(thread_id: str, current_user=Depends(_cl_get_current_user)):
        """離脱→復帰後、生成完了を検知した際にフロントが1回だけ叩く軽量スナップショット取得用。

        以前は完了検知後に window.location.reload() でページ全体を作り直して
        最終状態を取り込んでいたが、既に _make_relayed_emit（on_message参照）で
        生成中のライブ中継自体は機能しているため、完了時にもう一度フルリロード
        する必要は薄い。ここでは Chainlit公式の GET /project/thread/{thread_id}
        （chainlit/server.py）と同じ形（ThreadDict: steps/elements/metadata等）を
        匿名モード対応で返すだけの薄いラッパー。公式ルートは current_user が
        必須で匿名モードでは常に401になるため使えない（_assert_owns_thread の
        docstring参照）。
        """
        await _assert_owns_thread(thread_id, current_user)
        thread = await _thread_data_layer.get_thread(thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        return thread

    @_chainlit_asgi_app.get("/locohane/elements/{element_id}")
    async def _locohane_get_element_file(element_id: str, current_user=Depends(_cl_get_current_user)):
        """永続化済みの添付ファイル・埋め込み画像の実体を配信する。

        Chainlit公式の /project/file/<chainlit_key>?session_id=... はプロセスメモリ上の
        一時マッピング依存で、スレッド再開・プロセス再起動後は参照が切れる
        （thread_store.ChatThreadDataLayer.create_element / persist_element_file 参照）。
        こちらは data/elements/ へ永続化した実ファイルを配信する。element_id から
        thread_id を逆引きしてからでないと所有権確認ができないため、
        「要素なし・ファイルなし・所有者不一致」をすべて404に統一する
        （_assert_owns_thread と同じ、存在漏洩防止の方針）。
        """
        if _thread_store_conn is None:
            raise HTTPException(status_code=404, detail="Element not found")
        row = await thread_store.get_element_row(_thread_store_conn, element_id)
        if row is None or not row.get("file_path"):
            raise HTTPException(status_code=404, detail="Element not found")
        await _assert_owns_thread(row["thread_id"], current_user)
        path = Path(row["file_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Element not found")
        return FileResponse(path, media_type=row.get("mime") or "application/octet-stream")

    @_chainlit_asgi_app.post("/locohane/threads/{thread_id}/stop")
    async def _locohane_stop_thread(thread_id: str, current_user=Depends(_cl_get_current_user)):
        """他セッション（このスレッドを開いただけの閲覧側）から生成を停止する。

        Composerは、自分自身がそのターンを開始したセッションでは Chainlit純正の
        「停止」ボタン（session.current_task.cancel()）を使うが、生成中に別の
        タブ・別スレッドへ移動した後に戻ってきた閲覧側セッションは
        session.current_task を持たない（=純正の停止ボタンは機能しない）ため、
        このエンドポイントが _stop_thread_generating 経由で実際のタスクを
        cancel() する（そのdocstring参照）。
        """
        await _assert_owns_thread(thread_id, current_user)
        stopped = await _stop_thread_generating(thread_id)
        return {"stopped": stopped}

    @_chainlit_asgi_app.get("/locohane/threads/generating")
    async def _locohane_owner_generating_thread(
        exclude: str | None = None,
        current_user=Depends(_cl_get_current_user),
    ):
        """呼び出し元と同じ所有者が、他に生成中のスレッドを持っていないかを返す。

        Composer（App.tsx）はこれをポーリングし、新規チャット・他スレッドからの
        並列送信をフロントエンド側でも事前に防ぐ（バックエンド側の実際の拒否は
        on_message の _generating_owner_threads チェック。ここはそのUI反映用）。
        exclude に現在開いているスレッドIDを渡すと、それ自身が生成中でも
        「他のスレッド」としては数えない（新規チャットから呼ぶ場合は省略可）。
        """
        owner = thread_store.resolve_owner(current_user)
        conflicting_thread_id = _generating_owner_threads.get(owner)
        if conflicting_thread_id is not None and conflicting_thread_id == exclude:
            conflicting_thread_id = None
        return {"threadId": conflicting_thread_id}

    def _reorder_locohane_routes_before_spa_catchall() -> None:
        """直前に登録した /locohane/threads* ルートを、Chainlit本体のSPA
        catch-all（GET /{full_path:path}）より前へ移動する（上のコメント参照）。

        catch-all自体は app.include_router(router) で丸ごと1個の内部ラッパー
        （型名は `_IncludedRouter` 等、FastAPIのバージョン依存の非公開実装）
        として app.router.routes に入っており、パス文字列での検索はできない
        （個々の子ルートへは分解されない）。このためパスでの探索はせず、単純に
        自分が追加した分をリストの先頭へ移す（/locohane/* は他の固定パス
        （/openapi.json 等）と衝突しないため、先頭に置いても副作用は無い）。
        """
        routes = _chainlit_asgi_app.router.routes
        added = routes[_routes_before_registration:]
        del routes[_routes_before_registration:]
        routes[0:0] = added
        logging.getLogger(__name__).info(
            "Moved the thread list routes (%d) to the front (SPA catch-all workaround).", len(added)
        )

    _reorder_locohane_routes_before_spa_catchall()



from pydantic import BaseModel as _PydanticBaseModel  # noqa: E402 - 受付窓口の本文モデル用


# POST /locohane/threads/{id}/inject の本文。app.py は `from __future__ import annotations` のため、
# 関数の中で定義すると FastAPI が型を解決できず、本文として受け取れない（422 になる）。
class _InjectBody(_PydanticBaseModel):
    text: str
    # フロントエンドが決めた ID（応答を受け取れなくても、チャット欄に届いた発言と突き合わせるため）。
    id: str | None = None
    # 添付（アップロード済みのファイル ID）と、それをアップロードした呼び出し元のセッション ID。
    files: list[str] | None = None
    session_id: str | None = None


def _register_user_injection_route() -> None:
    """生成中の追加指示の受付窓口（POST /locohane/threads/{id}/inject）を登録する。

    他の /locohane/threads* ルートは [thread_store].enabled のときだけ登録するが、追加指示は
    スレッドの保存の有無に関わらず使えるよう、常に登録する（所有者の確認も thread_store に
    依存しない。_locohane_inject_into_thread の docstring 参照）。Chainlit の SPA catch-all より
    前へ並べ替えるのは、他のルートと同じ理由（_reorder_locohane_routes_before_spa_catchall 参照）。
    """
    from chainlit.auth import get_current_user as _cl_get_current_user
    from chainlit.server import app as _chainlit_asgi_app
    from fastapi import Depends, HTTPException

    routes_before = len(_chainlit_asgi_app.router.routes)

    @_chainlit_asgi_app.post("/locohane/threads/{thread_id}/inject")
    async def _locohane_inject_into_thread(
        thread_id: str,
        payload: _InjectBody,
        current_user=Depends(_cl_get_current_user),
    ):
        """生成中のターンへユーザーの追加指示を割り込ませる（src/user_injection.py）。

        Composer.tsx は生成中に送信された本文を通常の送信（on_message）ではなくここへ送る。
        積んだ指示は、次にモデルを呼ぶ直前（ツール呼び出しの区切り）に会話へ入る。
        ターンが動いていない・終わりかけで受付を閉じた後は 409 を返し、フロントエンドは
        ターンの終了を待って通常のメッセージとして送り直す。

        添付ファイル（files: アップロード済みのファイル ID、session_id: アップロードした
        呼び出し元の Chainlit セッション）は、受け付けたこの時点でスレッド専用フォルダへコピーし、
        LLM へ渡す本文（保存先と `@N`・貼り付けテキストの展開。通常の送信と同じ
        _build_user_llm_text）もここで組み立てる（_build_injection）。指示が会話へ入るのは後に
        なるが、その時にはアップロードしたセッションが既に無くなり、Chainlit が一時ファイルを
        消していることがある（生成中に別の会話へ移って戻ってきた画面から送った後、また移った場合など）。

        所有者の確認は _assert_owns_thread（thread_store の行）ではなく、「呼び出し元と同じ
        所有者が、今このスレッドを生成中か」（_generating_owner_threads）で行う。thread_store が
        無効な構成でも使えるようにするため（受け付けるのは生成中だけなので、これで足りる）。
        他の所有者のスレッドでも、生成中でないスレッドと同じ 409 を返す（存在を漏らさない）。
        添付の session_id も、同じ所有者・同じスレッドのセッションに限る（他人のアップロードを
        参照させない）。
        """
        owner = thread_store.resolve_owner(current_user)
        text = payload.text.strip()
        file_ids = list(dict.fromkeys(f for f in payload.files or [] if isinstance(f, str) and f))
        if not text and not file_ids:
            raise HTTPException(status_code=400, detail="The instruction is empty.")
        injection_id = (payload.id or "").strip() or None
        if injection_id is not None and (len(injection_id) > 64 or not re.fullmatch(r"[A-Za-z0-9-]+", injection_id)):
            raise HTTPException(status_code=400, detail="Invalid instruction id.")
        if _generating_owner_threads.get(owner) != thread_id or not user_injection.is_open(thread_id):
            raise HTTPException(status_code=409, detail="This conversation is not generating.")
        if injection_id is not None and user_injection.contains(thread_id, injection_id):
            # 再送（応答を受け取れずに送り直した等）。添付を二重にコピーしない。
            return {"queued": True, "id": injection_id}
        sources: list[tuple[str, str | None]] = []
        if file_ids:
            session = WebsocketSession.get_by_id(payload.session_id) if payload.session_id else None
            if (
                session is None
                or session.thread_id != thread_id
                or thread_store.resolve_owner(session.user) != owner
            ):
                raise HTTPException(status_code=400, detail="The attached files are not available. Attach them again.")
            for file_id in file_ids:
                info = session.files.get(file_id)
                if info is None or not Path(info["path"]).is_file():
                    raise HTTPException(status_code=400, detail="The attached files are not available. Attach them again.")
                sources.append((str(info["path"]), info.get("name")))
        limit = _config.ui_max_input_chars
        total = len(text) + sum(
            len(_read_pasted_text(src)) for src, name in sources if _is_pasted_text_file(name or src)
        )
        if limit > 0 and total > limit:
            raise HTTPException(status_code=400, detail=f"The instruction is too long ({total} > {limit} characters).")
        # ここから enqueue まで await を挟まない（is_open の確認と積む処理の間に受付が閉じないように）。
        try:
            llm_text, files = _build_injection(thread_id, text, sources)
        except OSError:
            logging.getLogger(__name__).warning("Failed to save the attachments of an additional instruction", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to save the attached files.") from None
        injection = user_injection.enqueue(thread_id, text, injection_id, llm_text=llm_text, files=files)
        if injection is None:
            raise HTTPException(status_code=409, detail="This conversation is not generating.")
        logging.getLogger(__name__).info(
            "Queued an additional user instruction thread_id=%s id=%s files=%d", thread_id, injection.id, len(files)
        )
        return {"queued": True, "id": injection.id}

    routes = _chainlit_asgi_app.router.routes
    added = routes[routes_before:]
    del routes[routes_before:]
    routes[0:0] = added


_register_user_injection_route()

_RUNTIME_STATUS_INTERVAL_SECONDS = 3.0
_runtime_status_task: "asyncio.Task | None" = None


def _collect_runtime_status() -> dict:
    """設定ダッシュボード向けに、接続中セッションと生成中スレッドを集める（src/runtime_status.py 参照）。

    Chainlit は切断後もタイムアウト（[project].session_timeout）まで
    WebsocketSession を ws_sessions_id に残すため、Socket.IO の接続状態で
    「今つながっているか」を判定する。
    """
    from chainlit.server import sio
    from chainlit.session import ws_sessions_id

    sessions = []
    for session in list(ws_sessions_id.values()):
        if not sio.manager.is_connected(session.socket_id, "/"):
            continue
        sessions.append(
            {
                "session_id": session.id,
                "user": thread_store.resolve_owner(session.user),
                "thread_id": session.thread_id,
                "has_first_interaction": bool(session.has_first_interaction),
            }
        )
    owners_by_thread = {thread_id: owner for owner, thread_id in _generating_owner_threads.items()}
    generating = [
        {
            "thread_id": thread_id,
            "owner": owners_by_thread.get(thread_id),
            "started_at": _generating_thread_started_at.get(thread_id),
            "waiting_for_user": thread_id in _pending_asks,
        }
        for thread_id in sorted(_generating_thread_ids)
    ]
    return {"pid": os.getpid(), "sessions": sessions, "generating": generating}


@cl.on_app_startup
async def _on_app_startup() -> None:
    """プロセス起動時（最初のセッション接続より前）に一度だけ呼ばれる。

    .locohane/settings.json の mcpServers に定義された全MCPサーバーへ自動接続する
    （[mcp] enabled=false なら何もしない）。@cl.on_chat_start（セッションごとに
    複数回発火しうる）とは独立した、プロセス全体で1回のフック。

    会話スレッド一覧（左サイドバー）用の thread_store 初期化もここで行う。
    Chainlit本体は `/project/settings` 等、WebSocket接続（on_chat_start）より
    前に届くHTTPリクエストの中で `get_data_layer()`（= `@cl.data_layer` で登録した
    ファクトリ）を呼ぶため、_setup()（on_chat_start 発火時に初めて走る遅延初期化）
    まで構築を待つと、その最初のHTTPリクエストの時点でまだ `_thread_data_layer`
    が `None` のままファクトリが呼ばれてしまいエラーになる。on_app_startup は
    ASGIのlifespan startupイベントとして、サーバーがHTTPリクエストの受付を
    開始する前に必ず完了するため、ここで先に用意しておく必要がある。
    """
    # 同じ data/ ディレクトリを共有した状態での多重起動を防ぐ（誤って2つ目の
    # プロセスを起動すると checkpoints.sqlite/chat_threads.sqlite が同時書き込みで
    # 破損するため）。このフックはASGI lifespanのstartupイベントであり、
    # 実際にサーバープロセスとして起動した時にのみ一度だけ呼ばれる
    # （app.py を import するだけのテストコードでは発火しない）ため、
    # 以降のDBファイルオープン処理より前の最初の行でチェックする
    # （詳細は src/instance_lock.py 参照）。
    # 注意: chainlit の on_app_startup ハンドラは wrap_user_function() 内の
    # `except Exception` に必ず捕まりログ出力されるだけで再送出されない
    # （chainlit/utils.py 参照）ため、ここで例外を投げっぱなしにしても
    # サーバー起動は止まらず、2つ目のプロセスがそのままリクエストを受け付けて
    # しまう。確実に起動を中止させるため、この場でメッセージを表示した上で
    # プロセスを強制終了する。instance_lock.InstanceAlreadyRunningError
    # だけでなく Exception 全般を捕まえるのは、instance_lock.acquire() 内の
    # mkdir/write_bytes/open() 等（msvcrt.locking() 以外の箇所）が送出しうる
    # OSError（権限不足・ディスク満杯・書き込み不可な既存ロックファイル等）を
    # 個別に捕まえ損ねると、ロックを一切保持しないままサーバー起動が
    # 継続してしまい、多重起動防止機能そのものが無音で無効化されるため
    # （二重起動によるsqlite破損を防ぐ、という本来の目的に反する）。
    try:
        instance_lock.acquire(_config.checkpoint_db.parent / "app.lock")
    except Exception as exc:  # noqa: BLE001 - ロック取得の失敗は理由を問わず起動を中止する
        print(f"\n[FATAL] {exc}\n", file=sys.stderr)
        logging.getLogger(__name__).critical(str(exc))
        os._exit(1)

    _patch_chainlit_websocket_ping_timeout()

    global _runtime_status_task
    _runtime_status_task = asyncio.create_task(
        runtime_status.run_writer_loop(
            _config.common_data_dir / runtime_status.RUNTIME_STATUS_FILENAME,
            _collect_runtime_status,
            _RUNTIME_STATUS_INTERVAL_SECONDS,
        )
    )

    global _thread_store_conn, _thread_data_layer
    if _config.thread_store_enabled:
        _thread_store_conn = await thread_store.init_db(_config.thread_store_db)
        _thread_data_layer = ChatThreadDataLayer(_thread_store_conn, _config.elements_dir)
        # 期限切れスレッドの起動時1回削除＋以降の定期削除（retention_days<=0で無効）。
        # elements_dirも渡し、削除したスレッドの添付・画像ファイルを孤児化させない。
        await thread_store.cleanup_old_threads(
            _thread_store_conn, _config.thread_store_retention_days, _config.elements_dir
        )
        if _config.thread_store_retention_days > 0:
            asyncio.create_task(
                thread_store.run_cleanup_loop(
                    _thread_store_conn,
                    _config.thread_store_retention_days,
                    _config.thread_store_cleanup_interval_hours,
                    _config.elements_dir,
                )
            )
        # 匿名モードではこのパッチが無いとスレッド再開が発火しない（関数docstring参照）。
        if not _config.auth_enabled:
            _patch_chainlit_anonymous_resume()
        # data_layer登録時のみ意味を持つ不具合（flush_thread_queuesはdata_layer
        # 未登録時は何もしないため）。関数docstring参照。
        _patch_chainlit_ignore_ui_action_first_interaction()
        # 同上（persist_user_session内のget_data_layer()がNoneなら何もしない
        # ため、data_layer未登録時は無害だがガードして明示する）。関数docstring参照。
        _patch_chainlit_disable_disconnect_thread_touch()

    if not _config.mcp_enabled:
        logging.getLogger(__name__).info("MCP is disabled ([mcp].enabled=false)")
        return
    await init_mcp_tools(_config)


@cl.on_app_shutdown
async def _on_app_shutdown() -> None:
    """プロセス終了時に一度だけ呼ばれる。接続済みMCPサーバーのstdioサブプロセスを
    正常終了させ（src.mcp_client.shutdown_mcp_tools 参照）、checkpointerの
    DB接続を保留中タスクの後始末を待ってから閉じる
    （_close_checkpointer_gracefully 参照）。
    """
    if _runtime_status_task is not None:
        _runtime_status_task.cancel()
    # 停止後に古い「接続中」情報が管理ツールに残らないよう消しておく。
    runtime_status.remove(_config.common_data_dir / runtime_status.RUNTIME_STATUS_FILENAME)
    await shutdown_mcp_tools()
    await _close_checkpointer_gracefully()
    if _thread_store_conn is not None:
        try:
            await _thread_store_conn.close()
        except Exception:  # noqa: BLE001 - シャットダウン時はベストエフォート、ログのみ
            logging.getLogger(__name__).debug(
                "Failed to close the thread store connection at shutdown (leak tolerated)",
                exc_info=True,
            )
    instance_lock.release()


async def _close_checkpointer_gracefully() -> None:
    """保留中の非同期タスクを完了（またはキャンセル）させてから、checkpointerの
    DB接続を閉じる。

    先に接続を閉じてしまうと、その時点でまだ実行中だったcheckpointer操作
    （_TimeoutGuardedSqliteSaver.aget_tuple/aput_writes等。on_messageの
    ストリーム処理等が発行する）がaiosqliteのバックグラウンドスレッド上で
    ValueError: no active connection を送出してクラッシュする
    （issue.md「DB接続切れエラー（no active connection）」の原因1〜3）。
    このシャットダウンフック自身を除く全タスクを
    [checkpointer].shutdown_drain_timeout_seconds まで待ち、なお残って
    いるものはキャンセルして後始末の完了を待ってから接続を閉じる。
    """
    if _checkpointer is None:
        return
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if pending:
        _done, still_pending = await asyncio.wait(pending, timeout=_config.checkpointer_shutdown_drain_timeout_seconds)
        if still_pending:
            logging.getLogger(__name__).warning(
                "Shutdown: cancelling %d pending tasks that did not finish in time",
                len(still_pending),
            )
            for task in still_pending:
                task.cancel()
            await asyncio.gather(*still_pending, return_exceptions=True)
    try:
        async with asyncio.timeout(_config.checkpointer_close_timeout_seconds):
            await _checkpointer.conn.close()
    except Exception:  # noqa: BLE001 - シャットダウン時はベストエフォート、ログのみ
        logging.getLogger(__name__).debug(
            "Failed to close the checkpointer connection at shutdown (leak tolerated)",
            exc_info=True,
        )


# public/settings/ 配下は Chainlit の /public/{filename:path} ルートで静的配信される
# （custom_build の SPA とは別経路）ため、ここに置いたテキストファイルを編集すれば
# フロントエンド/バックエンドの再ビルド無しで Welcome メッセージ・ヘッダー文言を変更できる。
SETTINGS_DIR = Path(__file__).resolve().parent / "public" / "settings"
# インスタンス専用の表示設定（管理ツールの「表示設定」でインスタンスを選んで
# 保存したもの）。ここにあるファイルが public/settings/ より優先される。
INSTANCE_SETTINGS_DIR = _INSTANCE_DIR / "settings"
# icon.* / favicon.* は拡張子違いを1組として扱う（フロントエンドは拡張子を順に
# 試すため、インスタンス側に1つでもあれば共通側の別拡張子を見せてはいけない）。
_SETTINGS_IMAGE_KINDS = ("icon", "favicon")


def _resolve_settings_file(filename: str) -> Path | None:
    """表示設定ファイルの実体パス（インスタンス専用 > 共通）。無ければ None。"""
    if not filename or Path(filename).name != filename:
        return None
    stem = filename.split(".", 1)[0]
    if stem in _SETTINGS_IMAGE_KINDS:
        base = INSTANCE_SETTINGS_DIR if any(INSTANCE_SETTINGS_DIR.glob(f"{stem}.*")) else SETTINGS_DIR
        path = base / filename
    else:
        path = INSTANCE_SETTINGS_DIR / filename
        if not path.is_file():
            path = SETTINGS_DIR / filename
    return path if path.is_file() else None


def _load_settings_text(filename: str, default: str) -> str:
    """表示設定のテキストファイルを読み込む（インスタンス専用 > public/settings/）。

    ファイルが存在しない、または読み込めない場合は default を返す
    （ユーザーが直接編集する外部ファイルという境界を越える入力のため、
    欠落・破損時にアプリ起動を落とさないための最小限のフォールバック）。
    """
    path = _resolve_settings_file(filename)
    if path is None:
        return default
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return default


def _register_instance_settings_route() -> None:
    """/public/settings/{filename} をインスタンス専用ファイル優先で配信するルートを登録する。

    フロントエンド（Header.tsx/App.tsx/index.html）は /public/settings/ を直接
    fetch するため、Chainlit 標準の /public/{filename:path} より手前（routes の
    先頭）に差し込んで横取りする（catch-all との順序問題は
    _reorder_locohane_routes_before_spa_catchall のコメント参照）。
    """
    from chainlit.server import app as _asgi_app
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    @_asgi_app.get("/locohane/version")
    async def _serve_version():
        # チャット画面のヘッダーのツールチップに出す（ログイン前でも見えてよい情報）。
        return {"version": LOCOHANE_VERSION}

    @_asgi_app.get("/locohane/ui-config")
    async def _serve_ui_config():
        # 表示言語の既定値（[ui].default_language）。利用者がまだ言語を選んでいないとき、
        # ログイン画面から使うため認証なしで返す（frontend/src/i18n.tsx 参照）。
        return {"default_language": _config.ui_default_language}

    @_asgi_app.get("/public/settings/{filename}")
    async def _serve_settings_file(filename: str):
        path = _resolve_settings_file(filename)
        if path is None:
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path)

    routes = _asgi_app.router.routes
    routes.insert(0, routes.pop())
    routes.insert(0, routes.pop())
    routes.insert(0, routes.pop())


_register_instance_settings_route()


# usage_metadata（langchain-openai の UsageMetadata dict）のキー →
# 累計辞書のキーの対応。on_chat_model_end のトークン集計で使う。
_USAGE_KEYS = (("input", "input_tokens"), ("output", "output_tokens"), ("total", "total_tokens"))


def _new_usage_totals() -> dict:
    """トークン使用量累計辞書の初期値（{"input","output","total"}を0で初期化）。"""
    return {"input": 0, "output": 0, "total": 0}


def _accumulate_usage(totals: dict, usage: dict) -> None:
    """usage_metadata の値を totals（{"input","output","total"}）へ加算する。

    Args:
        totals: 加算先の集計辞書（in-placeで更新する）。
        usage: AIMessage.usage_metadata（"input_tokens"/"output_tokens"/
            "total_tokens" を持つ dict）。
    """
    for key, src_key in _USAGE_KEYS:
        totals[key] += usage.get(src_key, 0) or 0


# frontend/src/utils/messageTree.ts の TOKEN_USAGE_PREFIX と一致させる
# （サイドパネルのトークン使用量カード用マーカー。PLAN_PREFIX/WORK_DIR_PREFIX と同じ方式）。
TOKEN_USAGE_PREFIX = "🔢 トークン使用量\n"


def _token_usage_level(total: int) -> str | None:
    """「リクエスト1回あたり」行の合計トークン数から強調表示レベルを判定する。

    config.ini [ui].token_usage_warn_threshold/token_usage_alert_threshold と
    比較する（累計行は会話が進むほど必ず閾値超過するため対象外。この行のみに適用）。
    """
    if _config.ui_token_usage_alert_threshold > 0 and total >= _config.ui_token_usage_alert_threshold:
        return "alert"
    if _config.ui_token_usage_warn_threshold > 0 and total >= _config.ui_token_usage_warn_threshold:
        return "warn"
    return None


def _format_token_usage(call_usage: dict, cumulative: dict) -> str:
    """出力元ごとの直近リクエスト1回分と会話累計（メイン+サブ合算）を、
    サイドパネルの TokenUsageCard（表形式）表示用に JSON 化する。

    call_usage は出力元（None=メインエージェント、それ以外はサブエージェント/batchグループの
    run_id）→ {"label", "input", "output", "total"}。並列実行中のサブエージェントが
    1行を交互に上書きすると、どの値か分からず、上限に近い値も直後に隠れてしまうため、
    出力元ごとに1行ずつ並べ、警告色も行ごとに判定する。メインの行を常に先頭にする。
    group="call" の行はフロント側でスクロール領域に入る（行数が並列数で増えるため）。

    メインエージェント累計（token_usage_cumulative_main）は圧縮判定用に引き続き集計するが、
    表示はしない。
    """
    ordered = sorted(call_usage.items(), key=lambda item: item[0] is not None)
    call_rows = [
        {**row, "level": _token_usage_level(row["total"]), "group": "call"} for _, row in ordered
    ]
    payload = {
        "rows": [
            *call_rows,
            {"label": i18n.t("usage.total"), **cumulative, "group": "total"},
        ]
    }
    return TOKEN_USAGE_PREFIX + json.dumps(payload, ensure_ascii=False)


async def _send_token_usage(call_usage: dict) -> None:
    """call_usage とセッションの会話累計からトークン使用量カードを送り直す。"""
    cumulative = cl.user_session.get("token_usage_cumulative") or _new_usage_totals()
    await cl.Message(content=_format_token_usage(call_usage, cumulative)).send()


# トークン使用量表示（🔢 プレフィックス）と同じ仕組みで、作業ディレクトリの状態を
# 通常のチャット欄ではなく右サイドバー専用領域へ表示するためのマーカー。
# フロントエンド側（frontend/src/utils/messageTree.ts）でこのプレフィックスを見て
# メインスレッドから除外し、サイドパネルへ抽出する。
WORK_DIR_PREFIX = "📁 作業ディレクトリ"

# frontend/src/utils/messageTree.ts の STARTER_PREFIX と一致させる
# （チャット開始時の定型文ボタン表示用マーカー。PLAN_PREFIX/WORK_DIR_PREFIX と同じ方式）。
STARTER_PREFIX = "🚀 定型文\n"

# frontend/src/utils/messageTree.ts の MAX_DISPLAY_MESSAGES_PREFIX と一致させる
# （メインスレッドの表示件数上限をフロントエンドへ伝えるマーカー。あくまで表示専用の
# 制限であり、LLMへ渡す会話コンテキストや会話ログには一切影響しない。
# STARTER_PREFIX/PLAN_PREFIX と同じ方式）。
MAX_DISPLAY_MESSAGES_PREFIX = "📏 表示件数上限\n"

# frontend/src/utils/messageTree.ts の MAX_DISPLAY_SIDE_STEPS_PREFIX と一致させる
# （サイドパネルのStep一覧の表示件数上限をフロントエンドへ伝えるマーカー。
# MAX_DISPLAY_MESSAGES_PREFIX と同様、表示専用の制限でありLLMへ渡す会話コンテキストや
# 会話ログには一切影響しない）。
MAX_DISPLAY_SIDE_STEPS_PREFIX = "🧰 サイドパネル表示件数上限\n"

# frontend/src/utils/messageTree.ts の INPUT_LIMITS_PREFIX と一致させる
# （入力欄の貼り付けテキスト化の閾値・最大入力文字数をフロントエンドへ伝えるマーカー。
# MAX_DISPLAY_MESSAGES_PREFIX と同じ方式。値は JSON オブジェクト）。
INPUT_LIMITS_PREFIX = "📝 入力上限\n"

# frontend/src/utils/messageTree.ts の INTERRUPTED_PREFIX と一致させる
# （ターンが途中で中断されたことをフロントエンドへ伝えるマーカー。これより後に
# ユーザー発言が無い間、入力欄の上に「続ける」ボタンを出す。値は中断理由）。
INTERRUPTED_PREFIX = "⏸ 中断\n"


async def _send_interrupted_marker(reason: str) -> None:
    """ターンの中断を INTERRUPTED_PREFIX 付きメッセージでフロントエンドへ伝える。

    停止ボタン（CancelledError）の後始末の途中からも呼ぶため、送信の失敗
    （切断済み等）は握りつぶして呼び出し元の中断処理を妨げない。
    """
    try:
        await cl.Message(content=INTERRUPTED_PREFIX + reason).send()
    except Exception:  # noqa: BLE001 - 表示用マーカーの送信失敗で中断処理を止めない
        logging.getLogger(__name__).debug("Failed to send the interrupted marker", exc_info=True)


# 割り込み指示（src/user_injection.py）をチャット欄へ出すユーザー発言の metadata キー。
# frontend/src/components/Composer.tsx がこの ID で「送信待ち」の表示を消す。
INJECTION_ID_METADATA_KEY = "injection_id"


class _TurnInjections:
    """1ターンの間に会話へ入った割り込み指示（src/user_injection.py）と、その画面表示の対応。

    「会話へ入った指示は必ず画面に出し、画面に出せないまま終わる指示は会話に残さない」ことを
    保つ。通常はグラフの custom event（INJECTION_APPLIED_EVENT）で表示するが、app.py が
    ストリームを途中で閉じる経路（ターン内のコンテキスト圧縮・計画の却下）では、閉じる間に
    グラフが先へ進んで指示を取り込み、その通知が読まれずに捨てられることがある。その場合は
    チェックポイントを見て表示し直す（sync_with_state）。チェックポイントへ確定する前の書き込み
    （pending write）にだけある指示は、aupdate_state で作る新しいチェックポイントには引き継がれない
    ため、書き換える前に回収する（pending_messages）。停止した場合は、画面に出ていない指示を
    会話から取り除く（drop_undisplayed。フロントエンドはそれらを入力欄へ戻す）。

    Attributes:
        token: このターンの受付のトークン（user_injection.open_thread）。None なら何もしない。
        displayed: 画面に出した指示の ID。
        texts: 画面に出した指示の本文（会話ログ用）。
    """

    def __init__(self, token: str | None) -> None:
        self.token = token
        self.displayed: set[str] = set()
        self.texts: list[str] = []

    async def show(self, items: list[dict]) -> None:
        """まだ出していない指示を、チャット欄にユーザー発言として出す。

        Args:
            items: {"id", "text"} の dict の列（user_injection.event_payload の "injections" 等）。
        """
        user = cl.user_session.get("user")
        author = getattr(user, "identifier", None) or "User"
        for item in items:
            if item["id"] in self.displayed:
                continue
            self.displayed.add(item["id"])
            files = [f for f in item.get("files") or [] if f.get("path")]
            names = i18n.t("list.separator").join(f["name"] for f in files)
            self.texts.append(item["text"] + (f" [attached: {names}]" if files else ""))
            # 表示の失敗（切断済み等）でターン自体を壊さない（指示は既に会話へ入っている）。
            try:
                elements = [
                    cl.Image(name=f["name"], path=f["path"], display="inline")
                    if is_image_file(Path(f["path"]))
                    else cl.File(name=f["name"], path=f["path"], display="inline")
                    for f in files
                    if Path(f["path"]).is_file()
                ]
                await cl.Message(
                    content=item["text"],
                    author=author,
                    type="user_message",
                    metadata={INJECTION_ID_METADATA_KEY: item["id"]},
                    elements=elements,
                ).send()
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).warning("Failed to show an additional user instruction in the chat", exc_info=True)

    def _messages_of_turn(self, messages) -> list:
        if self.token is None:
            return []
        return [m for m in messages or [] if user_injection.injection_items(m, self.token)]

    async def sync_with_state(self, graph, config) -> None:
        """会話（チェックポイント）に入っているのに画面に出ていない、このターンの指示を出す（ベストエフォート）。"""
        if self.token is None:
            return
        try:
            state = await graph.aget_state(config)
        except Exception:  # noqa: BLE001 - 表示の補正のためにターンの後始末を止めない
            logging.getLogger(__name__).debug("Failed to read the state to sync user injections", exc_info=True)
            return
        for m in self._messages_of_turn(state.values.get("messages", []) if state else []):
            await self.show(user_injection.injection_items(m, self.token))

    def pending_messages(self, state) -> list:
        """チェックポイントへ確定していない書き込みにだけある、このターンの指示の HumanMessage。"""
        found: list = []
        for task in getattr(state, "tasks", None) or ():
            if getattr(task, "error", None) is not None:
                continue
            result = getattr(task, "result", None)
            if not isinstance(result, dict):
                continue
            msgs = result.get("messages")
            if msgs is None:
                continue
            found.extend(self._messages_of_turn(msgs if isinstance(msgs, list) else [msgs]))
        return found

    async def drop_undisplayed(self, graph, config) -> None:
        """停止時: 画面に1件も出ていない、このターンの指示を会話から取り除く（ベストエフォート）。

        確定前の書き込みにだけある指示は、次に新しい入力でグラフを始めるときに LangGraph が
        捨てるため対象外（RemoveMessage は確定済みの ID にしか使えない）。
        """
        if self.token is None:
            return
        try:
            state = await graph.aget_state(config)
            pending_ids = {m.id for m in self.pending_messages(state)}
            for m in self._messages_of_turn(state.values.get("messages", []) if state else []):
                items = user_injection.injection_items(m, self.token)
                if m.id in pending_ids or any(i["id"] in self.displayed for i in items):
                    continue
                await graph.aupdate_state(config, {"messages": [RemoveMessage(id=m.id)]}, as_node="tools")
                logging.getLogger(__name__).info(
                    "Removed %d additional user instruction(s) that were not shown before the turn was stopped", len(items)
                )
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug("Failed to drop undisplayed user injections", exc_info=True)


# frontend/src/components/Composer.tsx が長文の貼り付けを添付化する際のファイル名
# （pasted-text-<時刻>[-<連番>].txt）と一致させる。これに一致する添付は保存先パスでは
# なく本文としてLLMへ渡す（_build_human_message 参照）。
PASTED_TEXT_FILENAME_RE = re.compile(r"^pasted-text-\d+(?:-\d+)?\.txt$")


def _format_work_dir_status(resolved: str | None, status: WorkDirAccessStatus | None = None) -> str:
    """作業ディレクトリの現在値・アクセス状況をサイドパネル表示用にまとめる。

    Args:
        resolved: ユーザー指定の作業ディレクトリ（絶対パス文字列）。None なら未設定。
        status: probe_workdir_access() の実測結果。None（未設定時、または
            プローブ未実行）の場合はパスのみを表示する。
    """
    if resolved is None:
        return f"{WORK_DIR_PREFIX}: {i18n.t('workdir.unset', default=_config.default_workdir)}"
    if status is None or (status.exists and status.readable and status.writable):
        return f"{WORK_DIR_PREFIX}: {resolved}"
    if not status.exists:
        label = i18n.t("workdir.missing")
    elif not status.readable:
        label = i18n.t("workdir.unreadable")
    else:
        label = i18n.t("workdir.readonly")
    return (
        f"{WORK_DIR_PREFIX}: {resolved}\n"
        f"{i18n.t('workdir.state', label=label)}\n"
        f"{i18n.t('workdir.redirect_note', default=_config.default_workdir)}"
    )


def _build_work_dir_notice() -> str:
    """作業ディレクトリの実際の絶対パスをLLMへ知らせるテキストブロックを組み立てる。

    system_prompt はサーバー起動時に1回だけ組み立てられセッション単位の作業
    ディレクトリを埋め込めず、サイドパネルの「📁作業ディレクトリ」表示
    （_format_work_dir_status）はUI専用でLLMの会話コンテキストには含まれない。
    そのため、この関数の戻り値を on_message から _build_human_message() 経由で
    HumanMessage へ差し込み、LLMが絶対パスを推測で組み立てずに済むようにする。

    absolute_path 等のパス値は他の説明文と同じ文字列に混ぜず、必ずJSONの
    独立したキーに分離する。低パラメータモデルは「絶対パス: D:\\x（読み書き
    可能）」のような自然文だと括弧書きの説明ごと1つのパス文字列として認識し、
    そのままGlob等のpath引数に渡してしまう事例があったため。

    状態の判定自体は `check_work_dir_status` ツールと共通の
    `_build_workdir_status_info()`（src/tools/_workdir.py）に委譲する。
    以前はこの2箇所が独自に重複したロジックを実装しており、食い違う
    （かつ両方とも誤った）情報をLLMへ伝えていた（2026-08-29 発見・修正）。

    Returns:
        `[作業ディレクトリ]` で始まり、状態を表すJSONオブジェクトを含む
        テキストブロック。
    """
    work_dir = cl.user_session.get("work_dir")
    status: WorkDirAccessStatus | None = cl.user_session.get("work_dir_access") if work_dir else None
    info = _build_workdir_status_info(work_dir, status)
    return (
        "[Working directory]\n"
        f"{json.dumps(info, ensure_ascii=False)}\n"
        "Use absolute_path as is for the target path of Read/Glob/Grep/analyze_image "
        "(treat this value alone as the path; do not join it with other text). "
        "Always write to write_dir. Do not use state to decide where to write "
        "(if write_dir equals absolute_path you can write there directly; if it differs, writes are redirected). "
        "If you only know a subfolder name, omit the path argument of Glob and it automatically "
        "searches under absolute_path (you may put the subfolder name in pattern, e.g. "
        'pattern="**/images/**/*"). Never build paths from memory or guesswork.'
    )


async def _resync_live_steps() -> None:
    """再接続時、進行中ターンのStepツリーを現在の状態でフロントへ再送する。

    chainlit/socket.py の emit_fn は `sio.emit(event, data, to=sid)` という
    特定sid宛のfire-and-forgetで、確認応答も再送も無い。そのため、進行中
    ターンの最中に切断が起きると、その切断ウィンドウ中に発行されたStep完了
    更新（on_tool_end のstep.update()等）は再接続後も届かず、StepItem.tsx側は
    「実行中」のまま固まる（issue参照。実例1: 06:42:34切断→dispatch_agent
    完了→06:42:55再接続。実例2: thinking Step生成10:11:55→切断→切断中の
    10:15:58に確定→10:16:03再接続、という順序で「確定」自体が切断ウィンドウ
    中に発生し再接続後も届かなかったケースも確認済み）。

    on_message側で cl.user_session["live_steps"]/["live_thinking"] に
    このターンで触れたStepをミラーしてあるが、closeイベントの送信成否を
    Python側では検知できない（fire-and-forgetのため）ので、closeした
    Stepもそのままターン終了まで参照を残す（_close_thinking/on_tool_end
    参照）。そのため、ここでは「まだ開いているか」に関わらず、追跡中の
    Step全件を無条件でupdate()し直す。既に正しく届いているStepへの
    再送は冪等（同じ状態で上書きするだけ）なので害はない。
    cl.Step.send()はpersisted済みだとno-opになりうるため、必ずupdate()を使う
    （update()にはそのガードが無く、常に再emitされる）。
    """
    thinking: cl.Step | None = cl.user_session.get("live_thinking")
    if thinking is not None:
        await thinking.update()
    steps: dict[str, cl.Step] = cl.user_session.get("live_steps") or {}
    for step in list(steps.values()):
        await step.update()


def _patch_chainlit_connection_successful_task_end() -> None:
    """Chainlit本体の connection_successful ハンドラが誤って task_end を
    発行してしまう不具合を、site-packages 無改変のまま上書き修正する。

    chainlit/socket.py の connection_successful は session.current_task の
    状態を一切見ずに無条件で task_end() を送る。フロント（@chainlit/react-client）
    は WebSocket が再接続するたびに connection_successful を送るため、
    dispatch_agent の無期限待ち（[subagent].background_inline_wait_max_seconds=0）
    のような分単位の長時間ターンの最中にネットワーク瞬断・スリープ復帰等で
    再接続が起きると、バックエンドはターンを継続しているのにフロントの
    送信ボタンだけが復活してしまう（ユーザーが実行中のターンへ新規メッセージを
    送れてしまう不具合の原因）。あわせて _resync_live_steps() を呼び、
    切断中に失われたStep完了更新をフロントへ再送する。

    python-socketio の AsyncServer.on() はイベントハンドラを
    self.handlers[namespace][event] = handler として単純に上書きするだけ
    （socketio/base_server.py 参照）なので、chainlit.socket インポート後に
    ここで再登録すれば安全に差し替えられる。元のハンドラ（resume_thread・
    on_chat_start 起動等）はそのまま呼び出し、その直後に
    session.current_task がまだ終わっていなければ task_start() を送り直して
    フロントの表示（停止ボタン）を実態に合わせて復元する。

    重要: _resync_live_steps() は task_start() の if ブロックの外で、
    task の完了有無に関わらず必ず呼ぶ。切断中にターンが完了してから
    再接続した場合（実例確認済み: 06:42:34切断→dispatch_agent完了→
    06:42:55再接続）、再接続時点で task.done() が True になっており、
    if ブロックの中に置くと _resync_live_steps() 自体が呼ばれず、切断
    ウィンドウ中に発行されたStep完了更新が永久にフロントへ届かない
    （StepItem.tsx側が「実行中」のまま固まる）。_resync_live_steps() は
    冪等（既に届いている場合は同じ状態で上書きするだけ）なので、
    task完了後に呼んでも害はない。

    _resync_live_steps() では救えないケースがもう一つある: 切断ウィンドウ
    中に「新規作成」されたStep（例: create_plan/approve_planのツール呼び出し）
    そのもの。_resync_live_steps() は cl.Step.update() を呼ぶだけで、これは
    フロント（@chainlit/react-client）の update_message ハンドラ側で「既存
    messages配列内でid一致するものを探して書き換える」実装になっており、
    まだ一度も受け取っていない未知のIDに対しては何もしない（新規追加＝upsert
    はしない）。つまり作成イベント自体が切断済みソケットへ送られて消えた場合、
    その後どれだけ update() を再送してもフロントには永久に復元されない
    （2026-08-30 ユーザー報告。verifier完了直後の320秒経過表示で画面が固まり、
    その後のcreate_plan/approve_planのStepが一度も表示されなかった実例で確認。
    ログ上はバックエンドが正常に進行し続けていたにもかかわらず、フロントは
    停止ボタン押下時の最終メッセージ以外一切更新されなかった）。

    これを解消するため、chainlit標準の resume_thread イベント（フロントの
    メッセージ配列を丸ごとDBの最新状態で置き換える。thread.get_thread_detail
    の docstring 参照）を、進行中セッションへの再接続時にも明示的に送る。
    chainlit標準の connection_successful は context.session.thread_id_to_resume
    が立っている場合（＝/thread/{id}ページを新規に開いた場合）にしかこれを
    送らないため、単なる裏側での切断→再接続ではこの経路を通らない。
    """
    from chainlit.data import get_data_layer
    from chainlit.socket import connection_successful as _original_connection_successful
    from chainlit.socket import init_ws_context as _init_ws_context
    from chainlit.socket import sio as _sio

    @_sio.on("connection_successful")  # pyright: ignore [reportOptionalCall]
    async def _connection_successful_fixed(sid: str) -> None:
        await _original_connection_successful(sid)
        context = _init_ws_context(sid)
        task = getattr(context.session, "current_task", None)
        if task is not None and not task.done():
            await context.emitter.task_start()

        # context.session.restored は WebsocketSession.restore()（既存セッションへの
        # 再接続時のみ）で立つフラグ。thread_id_to_resume が立っていれば
        # _original_connection_successful 側で既に resume_thread 送信済みなので、
        # ここでは進行中セッションへの「裏側の」再接続（＝標準経路が発火しない
        # ケース）に限定してフル再同期を行う。
        if (
            context.session.restored
            and not context.session.thread_id_to_resume
            and context.session.thread_id
        ):
            data_layer = get_data_layer()
            if data_layer:
                thread = await data_layer.get_thread(thread_id=context.session.thread_id)
                if thread:
                    logging.getLogger(__name__).info(
                        "WebSocket reconnect: sent a full resync (resume_thread) for the in-progress session thread_id=%s",
                        context.session.thread_id,
                    )
                    await context.emitter.resume_thread(thread)

        await _resync_live_steps()

        # 標準の clear_ask で消えた回答待ちの質問を、新しい接続へ送り直させる
        # （src/ask_reconnect.py 参照）。clear_ask・resume_thread より後に呼ぶこと。
        ask_reconnect.notify_reconnected(context.session)

    ask_reconnect.install()
    logging.getLogger(__name__).info(
        "Applied the patch preventing a spurious task_end on Chainlit connection_successful."
    )


def _register_socket_lifecycle_logging() -> None:
    """WebSocketの接続・切断・再接続をINFOログに記録する（診断目的、動作変更なし）。

    StepItem.tsx（フロント）の「完了」バッジは、そのStepに対する update_step
    イベント（step.end 付き）を受信して初めて表示が切り替わる。ところが
    chainlit/socket.py の emit_fn は `sio.emit(event, data, to=sid)` という
    特定sid宛のfire-and-forgetで、確認応答も再送も無い。そのため、進行中
    ターンの最中に切断が起きると、その切断中に発行されたStep完了更新
    （on_tool_end等）は再接続まで届かない。この再送は現在
    connection_successful（_patch_chainlit_connection_successful_task_end参照）
    側で _resync_live_steps() により対応済み（taskの完了有無に関わらず
    再接続のたびに呼ぶ）。ここではその相関確認・診断のため、接続イベント
    自体をログするに留める（動作への影響は無い）。
    """
    from chainlit.session import WebsocketSession as _WebsocketSession
    from chainlit.socket import connect as _original_connect
    from chainlit.socket import disconnect as _original_disconnect
    from chainlit.socket import sio as _sio

    logger = logging.getLogger(__name__)

    @_sio.on("connect")  # pyright: ignore [reportOptionalCall]
    async def _connect_logged(sid, environ, auth):
        session_id = auth.get("sessionId") if isinstance(auth, dict) else None
        existing = _WebsocketSession.get_by_id(session_id) if session_id else None
        if existing is not None:
            task = existing.current_task
            task_active = task is not None and not task.done()
            logger.info(
                "WebSocket reconnect: sid=%s thread_id=%s turn_in_progress=%s (reconnecting mid-turn may leave steps stuck as running if updates completed while disconnected never reached the frontend)",
                sid,
                existing.thread_id,
                task_active,
            )
        else:
            logger.info("WebSocket connected: sid=%s session_id=%s", sid, session_id)
        return await _original_connect(sid, environ, auth)

    @_sio.on("disconnect")  # pyright: ignore [reportOptionalCall]
    async def _disconnect_logged(sid):
        session = _WebsocketSession.get(sid)
        if session is not None:
            task = session.current_task
            task_active = task is not None and not task.done()
            logger.info(
                "WebSocket disconnected: sid=%s thread_id=%s turn_in_progress=%s (disconnecting mid-turn may lose step completion updates until reconnect)",
                sid,
                session.thread_id,
                task_active,
            )
        else:
            logger.info("WebSocket disconnected: sid=%s (unknown session)", sid)
        return await _original_disconnect(sid)

    logger.info("Enabled WebSocket connect/disconnect lifecycle logging.")


class _ThreadIdLogFilter(logging.Filter):
    """全ログレコードへ会話の thread_id を付与する logging.Filter。

    set_current_session()（@cl.on_message/@cl.on_chat_start 冒頭等で呼ばれる）が
    contextvars 経由で設定した値を読むだけなので、src/ 配下の各所にある
    既存の logging 呼び出しを一切変更せずに、全ログ行へ thread_id を
    反映できる。dispatch_agent のサブエージェントも同じ contextvars を
    引き継ぐ子タスクとして動くため、そのログにも同じ thread_id が付く。
    未設定（起動時ログ・evals/ハーネス等 Chainlit セッションを持たない
    呼び出し元）の場合は "-" にする。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.thread_id = get_current_session() or "-"
        return True


async def _setup() -> None:
    """スキル走査・ツール初期化など、全セッション共有資源の構築を一度だけ行う（冪等）。

    モジュール globals の _system_prompt が None でなければ即座に return する
    ため、複数回の @cl.on_chat_start 発火（複数セッション）でも
    重い初期化（DB接続・スキル走査）は1回しか実行されない。
    グラフ（LLMモデル・httpxクライアント）はここでは構築しない。
    セッションごとに @cl.on_chat_start が個別に build_graph() を呼ぶ
    （本ファイル冒頭のモジュールdocstring参照）。

    _config はモジュール読み込み時（トップレベル）で既に読み込み済み
    （password_auth_callback の登録判定に必要なため。詳細はモジュール
    globals 定義箇所のコメント参照）。ここでは流用するのみで再読み込みしない。

    実行内容:
    1. ログ出力先を設定する（_config はモジュール読み込み時に読み込み済み）。
    2. scan_skills() でスキルを走査し、build_system_prompt_from_block() で
       システムプロンプトを組み立てる（第1段階 Discovery）。
    3. init_tools() でツール（read_skill/read_skill_file/run_script/
       execute_python_code/dispatch_agent/メモリー系ツール）に skills ルート・
       サブエージェント設定・メモリールート等を注入する。
    4. AsyncSqliteSaver を構築し、checkpoint_db への接続をアプリ
       寿命で保持する。
    5. default_workdir（アップロードファイルを置くスレッド専用フォルダ
       `_tmp_*` を含む）等の期限切れファイルを1回削除し、以降は
       cleanup_interval_hours 間隔で削除を続けるバックグラウンドタスクを起動する
       （各 retention_days が0以下なら無効化）。

    Args:
        なし。

    Returns:
        None。副作用としてモジュール globals（_system_prompt, _checkpointer）
        を設定する。
    """
    global _system_prompt, _checkpointer
    if _system_prompt is not None:
        return

    # ログをファイルへ（何がどこに溜まるかを追えるように log_dir に出す）。
    # 注意: chainlit run はモジュール読み込み前に自前で logging.basicConfig() を
    # 呼び root logger にハンドラを付けてしまうため、basicConfig() は仕様上
    # no-op になり FileHandler が root に付かない。そのため root logger へ
    # 明示的に addHandler する。
    #
    # config.log_level（config.ini の [log].level）で詳細度を切り替える:
    #   "none"  - logging.disable() でプロセス全体のログ出力を無効化する
    #             （ハンドラ自体を作らない。data/logs/app_*.log は生成されない）。
    #   "info"  - 従来通り root logger を INFO でハンドラへ出す。
    #   "debug" - root logger を DEBUG にする。DEBUG レベルのログ（ツール呼び出しの
    #             全引数・全結果、LLM応答本文・thinkingを含む、tools.py/subagent.py/
    #             llm.py 側で発行）も同じハンドラに記録されるようになる。
    # ログファイルは data/logs/app_YYYYMMDD_HHMMSS.log のように日時つきの名前で
    # 出力され、config.log_max_lines（[log].max_lines）行を超えると新しい
    # 日時つきファイルへ自動的にローテーションする（LineCountRotatingFileHandler、
    # src/log_rotation.py）。config.log_clear_on_startup（[log].clear_on_startup）
    # が True なら、起動のたびに必ず新しいファイルを作成する。False（既定）なら
    # 直近の既存ファイルへの追記を試みる（行数超過ならその場でローテーション）。
    # ローテーションで増え続ける古い app_*.log は config.log_retention_days
    # （[log].retention_days）で自動削除する。
    log_level = _config.log_level
    if log_level not in ("info", "debug", "none"):
        raise ValueError(
            f"unknown log_level: {_config.log_level!r} (set [log].level in config.ini to one of info/debug/none)"
        )
    if log_level == "none":
        logging.disable(logging.CRITICAL)
    else:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG if log_level == "debug" else logging.INFO)
        # thread_id を asctime の直後（できるだけ行の前側）に置き、
        # どの会話スレッドのログかを grep 等で追いやすくする（_ThreadIdLogFilter参照）。
        formatter = logging.Formatter("%(asctime)s [thread=%(thread_id)s] %(levelname)s %(name)s: %(message)s")
        file_handler = LineCountRotatingFileHandler(
            _config.log_dir,
            _config.log_max_lines,
            clear_on_startup=_config.log_clear_on_startup,
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_ThreadIdLogFilter())
        # chainlit run が basicConfig() で仕込んだコンソール用ハンドラを全て除去。
        # root logger が DEBUG レベルのとき、openai/httpx/httpcore/aiosqlite などの
        # サードパーティ製ロガーが DEBUG ログ（HTTP リクエスト/レスポンス本文を含む）
        # をコンソールへ直接出力すると、Windows コンソール（QuickEdit Mode 有効時に
        # WriteConsole がブロックされる）のブロックがイベントループ全体を止め、
        # アプリがフリーズしたように見える。これを防ぐため、レベル制限ではなく
        # ハンドラ自体を完全に削除する（config.ini での切り替えは設けず常時無効化）。
        for h in list(root_logger.handlers):
            root_logger.removeHandler(h)
        # サードパーティ製ロガーの DEBUG ログを抑制し、ログファイルの肥大化も解消。
        # root logger 自体は log_level=debug のとき DEBUG のまま維持し、
        # src.llm / src.tools / src.subagent などの自アプリロガーは
        # file_handler を通じて DEBUG で記録され続ける。
        for lib in ("openai", "httpx", "httpcore", "aiosqlite", "asyncio"):
            logging.getLogger(lib).setLevel(logging.WARNING)
        root_logger.addHandler(file_handler)

    # 診断用: このプロセスが実際にメモリ上に持っている接続先設定を、起動の
    # たびに必ずログの先頭付近に残す（config.ini はプロセス起動時に一度だけ
    # 読み込まれホットリロードされないため、「config.iniを編集したのに挙動が
    # 変わらない」＝実は編集後に再起動していない、という原因を特定できる
    # ようにするため。詳細な選択過程は _select_endpoint() のログを参照）。
    logging.getLogger(__name__).info("Locohane v%s started.", LOCOHANE_VERSION)
    logging.getLogger(__name__).info(
        "LLM endpoint settings (loaded at startup): main_routing_strategy=%s main_endpoints=%s",
        _config.main_routing_strategy,
        [
            f"[{i}] base_url={e.base_url} model={e.model} start={e.start} end={e.end} provider={e.provider}"
            for i, e in enumerate(_config.main_endpoints)
        ],
    )
    logging.getLogger(__name__).info(
        "LLM endpoint settings (loaded at startup): sub_endpoints_inherit_main=%s sub_routing_strategy=%s sub_default_model=%r sub_endpoints=%s",
        _config.sub_endpoints_inherit_main,
        _config.sub_routing_strategy,
        _config.sub_default_model,
        [
            f"[{i}] base_url={e.base_url} model={e.model} start={e.start} end={e.end} provider={e.provider}"
            for i, e in enumerate(_config.sub_endpoints)
        ],
    )
    # 既定モデル名の誤りは委譲のたびではなく起動時に1回だけ知らせる
    # （_select_endpoint は委譲ごとには INFO に留める）。
    if _config.sub_default_model and not any(
        (e.model or "").strip().casefold() == _config.sub_default_model.casefold() for e in _config.sub_endpoints
    ):
        logging.getLogger(__name__).warning(
            "No endpoint with a model matching [llm].sub_default_model=%r exists in %s, so the default model setting is ignored",
            _config.sub_default_model,
            "main_url (inherited because sub_url is not set)" if _config.sub_endpoints_inherit_main else "sub_url",
        )

    # 第1段階 Discovery: スキルを走査して name+description をシステムプロンプトへ。
    # skills_dir と locohane_skills_dirs をマージ（同名は locohane 側優先）。
    skills = scan_skills([_config.skills_dir, *_config.locohane_skills_dirs])
    # メインエージェントの system_prompt に差し込む {{skills}}/{{main_agent_blocked_tools_hint}}
    # は [main_agent_tool_guard].visibility_mode の値だけで組み立て方が変わる
    # （dispatch_agent配下のサブエージェント用 {{skills}} は下記の通り本ガードと無関係に
    # skills から別途組み立てるため、いずれのモードでもスキル自体は失われない）。
    # main_agent_tool_guard_mode（呼び出し制限そのもののON/OFF/範囲）とは独立した軸で、
    # mode=false の場合は filter_skills_for_main_agent_guard 等が内部で
    # 絞り込み・ブロックを行わなくなるため、strict/hint を選んでも自然に
    # 「全部見せる」として振る舞う。visibility_modeの対象はスキル一覧＋ビルトイン
    # ツールのヒント表示だが、blocked_tools_hint は get_all_tools()（ビルトイン+MCP）
    # 全体が対象のため、mode=all の場合はMCP動的ツール名もヒントに混ざりうる
    # （mode=tools_skills_only ならMCPは常に許可されるため混ざらない）。
    #   visibility_mode=all   : 全スキル名・descriptionを一覧に表示するが、直接実行不可な
    #                          分は description の末尾に注記を追記し、呼べないビルトイン
    #                          ツール名一覧もテキストで案内する（bindはしない）。
    #   visibility_mode=hint  : 全スキル名を列挙しつつ直接実行不可な分は description を
    #                          固定文言に差し替え、呼べないビルトインツール名一覧も
    #                          テキストで案内する（bindはしない。無駄な往復を避けるため）。
    #   visibility_mode=strict: 直接実行できないスキルは一覧から除外する（従来の挙動）。
    if _config.main_agent_tool_guard_visibility_mode in ("all", "hint"):
        if _config.main_agent_tool_guard_visibility_mode == "all":
            main_skills_block = render_skills_block_with_guard_annotation(skills, _config)
        else:
            main_skills_block = render_skills_block_with_hint(skills, _config)
        blocked_names = list_blocked_tool_names_for_hint(get_all_tools(), _config)
        blocked_tools_hint = (
            "The following built-in tools cannot be called directly (delegate to dispatch_agent if you need details or execution): " + ", ".join(blocked_names)
            if blocked_names
            else ""
        )
    else:
        main_skills_block = render_skills_block(filter_skills_for_main_agent_guard(skills, _config))
        blocked_tools_hint = ""
    # ループ検知が本文だけを監視する設定で思考予算も無制限だと、思考のループを
    # 止めるものが無くなる。
    if (
        _config.thinking_loop_guard_enabled
        and _config.thinking_loop_guard_target == "content_only"
        and _config.enable_thinking is not False
        and (_config.reasoning_budget is None or _config.reasoning_budget < 0)
    ):
        logging.getLogger(__name__).warning(
            "[thinking_loop_guard].target = content_only but [llm].reasoning_budget is unlimited, so nothing stops a thinking loop"
        )
    # [thinking_control].after_tools に書き間違いがあると、そのツールの後は
    # 黙って default_level になるだけで気づきにくいため、起動時に警告しておく。
    if _config.thinking_control_enabled:
        unknown = unknown_tool_names(_config, {t.name for t in get_all_tools()})
        if unknown:
            logging.getLogger(__name__).warning(
                "[thinking_control].after_tools contains unknown tool names (MCP tools may be false positives depending on connection state): %s",
                ", ".join(unknown),
            )
    # ユーザー別ドラフトスキル（src/skill_drafts.py）はセッションごとに見える範囲が
    # 違うため、ここでは目印だけを一覧の末尾に残し、_rebuild_graph() で差し替える。
    main_skills_block = f"{main_skills_block}{skill_drafts.DRAFT_SKILLS_MARKER}"
    system_prompt = build_system_prompt_from_block(main_skills_block, _config.system_prompt_path).replace(
        "{{main_agent_blocked_tools_hint}}", blocked_tools_hint
    )
    # エージェント種別（agents/*.md、ClaudeCode の .claude/agents/*.md 相当）を走査し、
    # 各種別のシステムプロンプトにも {{skills}}/{{agent_types}} を差し込む
    # （本文で {{agent_types}} を使う種別が dispatch_agent の委譲先一覧を参照できるようにするため）。
    # agents_dir と locohane_agents_dirs をマージ（同名は locohane 側優先）。
    # {{skills}} は [subagent].agent_type_run_script_allowlist に登録がある種別だけ
    # 登録スキルへ絞る（run_script で呼べないスキルを載せても拒否されるだけのため）。
    # 絞り込みたくない種別は {{all_skills}} を使うと全スキル一覧が載る。
    agent_type_defs = scan_agent_types([_config.agents_dir, *_config.locohane_agents_dirs])
    agent_types_block = render_agent_types_block(agent_type_defs)
    all_skills_block = render_skills_block(skills)
    agent_type_defs = [
        replace(
            a,
            system_prompt=a.system_prompt.replace(
                "{{skills}}",
                render_skills_block(
                    filter_skills_for_agent_type(skills, a.name, _config.subagent_agent_type_run_script_allowlist)
                ),
            )
            .replace("{{all_skills}}", all_skills_block)
            .replace("{{agent_types}}", agent_types_block)
            .replace(
                "{{run_script_allowlist}}",
                render_agent_type_run_script_allowlist_block(a.name, _config.subagent_agent_type_run_script_allowlist),
            ),
        )
        for a in agent_type_defs
    ]
    # 永続メモリーの索引はユーザー別（自分 → 共通 → 他のユーザーの共有のもの）のため、ここでは目印だけを残し、
    # セッションごとに _apply_session_draft_skills() が差し替える（起動後に増えたメモリーも次の会話から載る）。
    # subagent 側には差し込まない。
    system_prompt = system_prompt.replace("{{memory}}", MEMORY_INDEX_MARKER)
    # dispatch_agent が選べるエージェント種別一覧を {{agent_types}} へ差し込む。
    system_prompt = system_prompt.replace("{{agent_types}}", agent_types_block)
    # 計画承認（Plan Mode）を免除される読み取り専用スクリプトのホワイトリストを
    # {{plan_approval_exempt_scripts}} へ差し込む（config.ini の値が唯一の正）。
    system_prompt = system_prompt.replace(
        "{{plan_approval_exempt_scripts}}",
        render_plan_approval_exempt_scripts_block(_config.plan_approval_exempt_scripts),
    )
    # config.ini の値を ${変数名} として埋め込めるよう展開する（{{...}}置換完了後に行う）。
    system_prompt = expand_config_vars(system_prompt, _config)
    # .locohane/LOCOHANE.md はユーザー自由記述のため ${...} を偶然含んでいても
    # expand_config_vars() の未定義変数エラーで起動が落ちないよう、config変数展開が
    # 完了した後の最後の差し込みとして扱う。
    system_prompt = system_prompt.replace(
        "{{project_instructions}}",
        render_project_instructions_block(_config.project_instructions_paths),
    )
    # メモリーの公開範囲の決め方（[memory]）。振り分け基準は管理者の自由記述のため、project_instructions と同じく
    # config 変数の展開の後に差し込む。
    system_prompt = system_prompt.replace("{{memory_visibility}}", render_memory_visibility_block(_config))

    # サブエージェント共通の注意事項（反復回数の定義・フォールバック方針）を、
    # agents/*.md ファイル自体には書き込まず、各 agent_type の system_prompt
    # （メモリ上の文字列）の末尾に自動連結する。
    subagent_common = expand_config_vars(
        (_config.system_prompt_path.parent / "subagent_common.md").read_text(encoding="utf-8"),
        _config,
    )
    agent_type_defs = [replace(a, system_prompt=f"{a.system_prompt}\n\n{subagent_common}") for a in agent_type_defs]

    # ツールに skills ルート等を注入（dispatch_agent 用にエージェント種別定義も渡す）。
    # locohane_skills_dirs を逆順で先に置くことで、read_skill/run_script/analyze_image 等の
    # 実体解決も scan_skills() と同じ「後方（.locohane 側・インスタンス専用側）優先」のマージ挙動になる
    # （scan_skills は後方優先、_SKILLS_ROOTS は前方優先のため逆順にする）。
    init_tools(
        [*reversed(_config.locohane_skills_dirs), _config.skills_dir],
        _config.script_python,
        _config.script_timeout,
        _config,
        agent_type_defs,
        _config.subagent_max_iterations,
        _config.default_workdir,
        _config.memory_dir,
        _config.help_path,
        _config.path_memory_dir,
        _config.path_memory_max_entries,
        _config.code_exec_enabled,
        _config.approval_timeout_seconds,
        _config.ask_user_question_timeout_seconds,
        _config.ask_user_choice_timeout_seconds,
        _config.plan_badge_allow_unlock,
        _config.subagent_max_parallel,
        _config.graph_tool_max_parallel,
        script_background_max_runtime_seconds=_config.script_background_max_runtime_seconds,
        script_background_job_retention_seconds=_config.script_background_job_retention_seconds,
        script_background_min_poll_interval_seconds=_config.script_background_min_poll_interval_seconds,
        script_background_min_poll_message=_config.script_background_min_poll_message,
        script_background_inline_wait_max_seconds=_config.script_background_inline_wait_max_seconds,
        script_background_progress_push_interval_seconds=_config.script_background_progress_push_interval_seconds,
        dispatch_agent_background_job_retention_seconds=_config.subagent_background_job_retention_seconds,
        dispatch_agent_background_min_poll_interval_seconds=_config.subagent_background_min_poll_interval_seconds,
        dispatch_agent_background_min_poll_message=_config.subagent_background_min_poll_message,
        dispatch_agent_background_inline_wait_max_seconds=_config.subagent_background_inline_wait_max_seconds,
        dispatch_agent_background_progress_push_interval_seconds=_config.subagent_background_progress_push_interval_seconds,
        dispatch_agent_background_llm_timeout_max_retries=_config.subagent_background_llm_timeout_max_retries,
        dispatch_agent_batch_max_groups=_config.subagent_batch_max_groups,
        dispatch_agent_batch_stop_after_unreachable_groups=_config.subagent_batch_stop_after_unreachable_groups,
        plan_approval_exempt_scripts=_config.plan_approval_exempt_scripts,
        agent_type_run_script_allowlist=_config.subagent_agent_type_run_script_allowlist,
        plans_dir=_config.plans_dir,
        allow_sandbox_dirs=_config.allow_sandbox_dirs,
        plan_reset_approval_on_recreate=_config.plan_reset_approval_on_recreate,
        plan_auto_approve=_config.plan_auto_approve,
        path_memory_search_min_score=_config.path_memory_search_min_score,
        path_memory_search_filename_weight=_config.path_memory_search_filename_weight,
        skill_names=[s.name for s in skills],
        skill_dirs={s.name: s.dir_path for s in skills},
    )

    # チェックポインタ（会話状態の永続化）。接続はアプリ寿命で保持する。
    _checkpointer = await _build_checkpointer()

    # on_stop でのグラフ再構築（_rebuild_graph）に使い回すため保持する。
    _system_prompt = system_prompt

    logging.getLogger(__name__).info("Shared resources initialized. skills=%d", len(skills))

    # analyze_image(show_in_chat=True)・回答本文への画像埋め込み（_embed_local_images_as_session_urls）が
    # 使う Chainlit 自身のセッションファイルディレクトリ（.files/<セッションID>/）も
    # 同様に日数ベースで自動削除する。Chainlit自身にもブラウザ切断時の削除処理は
    # あるが、プロセス再起動等では確実に働くとは限らないための保険（ファイルでは
    # なくセッションID単位のディレクトリを削除するため cleanup_old_dirs を使う）。
    cleanup_old_dirs(CHAINLIT_FILES_DIRECTORY, _config.chainlit_files_retention_days)
    if _config.chainlit_files_retention_days > 0:
        asyncio.create_task(
            cleanup_run_cleanup_dirs_loop(
                CHAINLIT_FILES_DIRECTORY,
                _config.chainlit_files_retention_days,
                _config.chainlit_files_cleanup_interval_hours,
            )
        )

    # 添付ファイル・回答本文への画像埋め込みの永続化先（data/elements/<thread_id>/）も
    # 同様に日数ベースで自動削除する（src/thread_store.py の persist_element_file 参照）。
    # スレッドIDごとのサブフォルダ構成のため cleanup_old_files_in_subdirs を使う。
    cleanup_old_files_in_subdirs(_config.elements_dir, _config.elements_retention_days)
    if _config.elements_retention_days > 0:
        asyncio.create_task(
            cleanup_run_cleanup_files_in_subdirs_loop(
                _config.elements_dir,
                _config.elements_retention_days,
                _config.elements_cleanup_interval_hours,
            )
        )

    # default_workdir（ユーザーが ChatSettings で work_dir を指定しなかった場合の
    # 既定作業ディレクトリ）直下に溜まり続けるファイルも同様に自動削除する。
    # ユーザー指定の work_dir はここでは触らない。
    cleanup_old_files(_config.default_workdir, _config.default_workdir_retention_days)
    if _config.default_workdir_retention_days > 0:
        asyncio.create_task(
            cleanup_run_cleanup_loop(
                _config.default_workdir,
                _config.default_workdir_retention_days,
                _config.default_workdir_cleanup_interval_hours,
            )
        )

    # execute_python_code が default_workdir 配下に作る `_tmp_<thread_id>`
    # （src/tools.py の _resolve_exec_workdir() 参照）は on_chat_end での
    # 即時削除を廃止したため（生成中に別スレッドへ切り替えると誤って
    # 消えてしまう問題があった）、他の default_workdir 配下のファイルと
    # 同じ日数ベースの自動削除（起動時1回 + 定期ループ）に一本化する。
    cleanup_old_dirs(_config.default_workdir, _config.default_workdir_retention_days, pattern="_tmp_*")
    if _config.default_workdir_retention_days > 0:
        asyncio.create_task(
            cleanup_run_cleanup_dirs_loop(
                _config.default_workdir,
                _config.default_workdir_retention_days,
                _config.default_workdir_cleanup_interval_hours,
                pattern="_tmp_*",
            )
        )

    # パスメモリー（src/path_memory.py）のレジストリファイルも同様に日数ベースで
    # 自動削除する（「会話終了」を検知する手段が無いための代替措置）。
    cleanup_old_files(_config.path_memory_dir, _config.path_memory_retention_days)
    if _config.path_memory_retention_days > 0:
        asyncio.create_task(
            cleanup_run_cleanup_loop(
                _config.path_memory_dir,
                _config.path_memory_retention_days,
                _config.path_memory_cleanup_interval_hours,
            )
        )

    # ローテーションで増え続ける古い app_*.log も同様に日数ベースで自動削除する。
    # 同じ log_dir に evals/run_case.py が書く evals.log は対象外にする
    # （pattern="app_*.log" で絞り込む）。
    cleanup_old_files(_config.log_dir, _config.log_retention_days, pattern="app_*.log")
    if _config.log_retention_days > 0:
        asyncio.create_task(
            cleanup_run_cleanup_loop(
                _config.log_dir,
                _config.log_retention_days,
                _config.log_cleanup_interval_hours,
                pattern="app_*.log",
            )
        )

    # P0-3: httpcore の cancel scope breakage 検知フィルタを登録する。
    # _setup() は一度だけ呼ばれるため、ここで登録すれば十分（冪等処理あり）。
    _register_cancel_scope_watcher()

    # 長時間ターン中の再接続で送信ボタンが誤って復活する不具合を防ぐ。
    _patch_chainlit_connection_successful_task_end()

    # Stepの完了表示漏れ調査用: WebSocket接続/切断/再接続をログに残す。
    _register_socket_lifecycle_logging()

    # LLM 同時実行数ガードを初期化する。
    init_llm_concurrency(_config.llm_max_concurrent_requests)


async def _rebuild_graph(thread_id: str, *, wait_when_busy: bool = True):
    """既存の system_prompt・checkpointer を使い回し、指定セッションのグラフだけを
    （再）構築して cl.user_session へ保存する。

    on_chat_start（初回構築）・on_stop（LLM クライアント強制クローズ後）・
    on_message の ThinkingLoopDetected リトライ経路（クライアント再構築後）の
    いずれからも呼ぶ。build_model() は呼ばれるたびに新しい httpx.AsyncClient を
    生成するため、build_graph() の直前に set_current_session(thread_id) を
    呼び、新しいクライアントが正しくこのセッションへ紐づけて登録されるように
    する。checkpointer の接続（_checkpointer）はそのまま使い回すため、
    thread_id ごとの会話履歴は失われない。

    グラフはセッション（タブ）ごとに個別のインスタンスであり、他タブの
    グラフ・httpxクライアントには一切影響しない（モジュール globals では
    なく cl.user_session に保存する理由）。

    Args:
        thread_id: このセッションの thread_id（cl.user_session の
            "thread_id"）。
        wait_when_busy: build_graph()/build_model() の同名引数にそのまま渡す。
            既定True（round_robin戦略・provider="llama_cpp"で全接続先が
            ビジーなら空きが出るまで待つ）。on_chat_start/on_chat_resume/
            on_stop は「グラフを構築するだけで直後に生成するとは限らない、
            かつUI操作としては即座に完了してほしい」操作のためFalseを渡す。
            これを渡さないと、他タブが生成中で接続先が唯一かつビジーな間、
            無関係なはずのスレッド切替・新規セッション開始・停止ボタンが
            その生成が終わるまでブロックされてしまう（2026-08-26 ユーザー
            報告）。ThinkingLoopDetected/接続エラー時の再構築だけは、直後に
            同じ生成を即リトライするためTrueのままにする（空いている接続先を
            選びたい）。

    Returns:
        新しく構築されたコンパイル済みグラフ。呼び出し元はこれをローカル
        変数として使い続けること。
    """
    # tab_id=cl.context.session.id: 同じ thread_id を複数タブで同時に開いた
    # 場合でも、round_robin/priority_failoverの「直近選択」（_LAST_SELECTED_INDEX）
    # がタブをまたいで混線しないようにする（set_current_session docstring参照。
    # 2026-08-26 監査で発見した既存の軽微なエッジケースの修正）。
    set_current_session(thread_id, tab_id=cl.context.session.id)
    # ナレッジのコレクション一覧は取り込みで変わるため、起動時ではなく組み立てのたびに差し込む。
    system_prompt = (
        _apply_session_draft_skills().replace("{{document_collections}}", render_document_collections_block(_config))
        + i18n.response_language_directive()
    )
    # 組み立てに使った言語。画面で言語を切り替えたら on_message が比べて組み直す。
    cl.user_session.set("graph_lang", i18n.current_lang())
    # 入力欄ツールバーで選んだ reasoning_effort（on_message が送信時の値を入れる）。
    # 変わったら on_message が比べて組み直す。
    config = with_reasoning_effort(_config, cl.user_session.get(SESSION_REASONING_EFFORT_KEY))
    cl.user_session.set("graph_reasoning_effort", config.reasoning_effort)
    graph = await build_graph(config, system_prompt, _checkpointer, wait_when_busy=wait_when_busy)
    cl.user_session.set("graph", graph)
    return graph


def _apply_session_draft_skills() -> str:
    """このセッションのユーザーに見えるドラフトスキルを反映したシステムプロンプトを返す。

    ドラフト（src/skill_drafts.py）は作成者本人の会話にだけ見せるため、起動時に
    1回だけ組み立てる _system_prompt には目印（DRAFT_SKILLS_MARKER）だけを残し、
    グラフを組み立てるたびにここで一覧へ差し替える。あわせて、read_skill の
    選択肢へ足すドラフト名（読み取りできるものだけ）を cl.user_session の
    "visible_draft_names" に入れる（src/tools/session_tools.py 参照）。
    組み立てに使ったドラフトの要約（skill_drafts.drafts_signature）も
    "drafts_signature" に残し、on_message が変化を検知して組み直せるようにする。
    永続メモリーの索引（MEMORY_INDEX_MARKER）も、このユーザー向けのもの（src/memory.py の
    render_memory_block）へ差し替える。
    """
    user = cl.user_session.get("user")
    draft_user = skill_drafts.draft_owner_name(user.identifier if user else None)
    cl.user_session.set(skill_drafts.SESSION_USER_KEY, draft_user)
    drafts = skill_drafts.scan_visible_drafts(_config, draft_user)
    cl.user_session.set("visible_draft_names", [d.skill.name for d in drafts if d.readable])
    cl.user_session.set("drafts_signature", skill_drafts.drafts_signature(drafts))
    return _system_prompt.replace(
        skill_drafts.DRAFT_SKILLS_MARKER, skill_drafts.render_draft_skills_block(drafts)
    ).replace(MEMORY_INDEX_MARKER, render_memory_block(_config.memory_dir, viewer=draft_user))


@cl.on_stop
async def on_stop() -> None:
    """停止ボタン押下時、【このセッション自身の】LLMサーバーへの根底のHTTP接続を
    強制的に切断する。

    Chainlit本体（session.current_task.cancel()）は実行中タスクへ
    asyncio.CancelledError を投げ込むのみで、LLMサーバーへのHTTP接続を
    切断する処理は持たない。ChatLlamaCpp._astream の finally 節にある
    agen.aclose() はキャンセル済みコンテキストでは正しく完了しない
    可能性が高く、接続が生きたままだと停止ボタンを押してもCPU/GPU使用率が
    下がらない事象につながる（tune-prompt iter27でユーザー報告・調査）。

    ここはキャンセルされていない別の task コンテキストで呼ばれるため、
    aclose_active_llm_clients(thread_id) で確実に接続を強制クローズできる。
    その直後に自セッションのグラフを再構築し、新しいLLMクライアントに
    差し替える。

    Chainlit本体が cancel() するのはメイングラフのタスク
    （session.current_task）のみで、dispatch_agent・run_script_background/
    execute_python_code_background それぞれの job.runner_task
    （いずれも asyncio.shield() で保護されている）には届かない。放置すると、
    停止ボタンを押してバッジが「停止」になった後もサブエージェントの
    LLM呼び出し・スクリプト実行・進捗pushが裏側で動き続けてしまう
    （2026-08-26 ユーザー報告）ため、ここで併せて強制終了する。

    以前はプロセス全体で共有される _active_async_clients を一括クローズ
    しており、別タブで実行中の処理まで巻き添えで
    "Cannot send a request, as the client has been closed" 等のエラーで
    止まってしまう不具合があった。グラフ（＝LLMモデル・httpxクライアント）を
    セッションごとに分離したことで、thread_id を指定して自セッション分
    だけを閉じられるようになっている。
    """
    thread_id = cl.user_session.get("thread_id")
    if thread_id is None:
        # _setup()/on_chat_start 完了前に停止が押された等の異常系。
        # 紐づくセッションのグラフがまだ無いため何もしない。
        logging.getLogger(__name__).warning("on_stop: thread_id is not set, doing nothing")
        return
    # 最初の await より前に、追加指示の受付を閉じる（_close_user_injection_on_stop 参照）。
    _close_user_injection_on_stop(thread_id)
    # aclose_active_llm_clients・cancel_dispatch_agent_jobs_for_thread内で
    # CancelledErrorが発生しても、後始末（グラフ再構築・ログ出力）は
    # 最後まで実行してから呼び出し元へ伝播する。2つを同じtryブロックに
    # まとめると、前者のCancelledErrorで後者（本来の主目的である接続の
    # 強制クローズ）がスキップされてしまうため、独立したtryブロックに分け、
    # どちらも必ず試みる。
    cancelled: asyncio.CancelledError | None = None
    try:
        await cancel_dispatch_agent_jobs_for_thread(thread_id)
    except asyncio.CancelledError as exc:
        cancelled = exc
    try:
        await cancel_background_script_jobs_for_thread(thread_id)
    except asyncio.CancelledError as exc:
        cancelled = exc
    try:
        await aclose_active_llm_clients(thread_id)
    except asyncio.CancelledError as exc:
        cancelled = exc
    # _rebuild_graph が例外を送出しても検知できないまま処理が終わるのを防ぐ。
    # 最大2回まで試行する。
    # wait_when_busy=False: 停止ボタンは「今すぐUIの制御を取り戻す」操作であり、
    # 直後に生成するとは限らない（on_chat_start/on_chat_resumeと同様）。他タブが
    # 唯一の接続先を使って生成中だと、Trueのままではそのタブの生成が終わるまで
    # 停止ボタン自体が固まってしまう（_rebuild_graph docstring参照）。
    rebuild_ok = False
    for _retry in range(2):
        try:
            await _rebuild_graph(thread_id, wait_when_busy=False)
            rebuild_ok = True
            break
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).error(
                "on_stop: failed to rebuild the graph [%s]",
                describe_current_task(),
                exc_info=True,
            )
            await cl.Message(
                content=i18n.t("notice.session_reinit_failed"),
                type="system_message",
            ).send()
    logging.getLogger(__name__).info(
        "on_stop: force-closed the LLM client of session thread_id=%s and rebuilt the graph%s%s",
        thread_id,
        " (the task was cancelled during the close, but the cleanup completed)" if cancelled else "",
        " (graph rebuild: succeeded)" if rebuild_ok else " (graph rebuild: failed)",
    )
    if cancelled is not None:
        raise cancelled


def _init_session_lang() -> None:
    """接続時にフロントエンドが userEnv で送った表示言語をセッションへ保存する。

    frontend/src/App.tsx が connect({ userEnv: { LOCOHANE_LANG } }) で送る。送られて
    こない（古いフロントエンド・evals 等）場合は既定言語（[ui].default_language）にする。
    グラフを組み立てる（_rebuild_graph）前に呼ぶこと（応答言語の指示に使うため）。
    """
    env = cl.user_session.get("env")
    lang = i18n.normalize(env.get(i18n.USER_ENV_KEY)) if isinstance(env, dict) else None
    cl.user_session.set(i18n.SESSION_KEY, lang or i18n.current_lang())


async def _send_starter_prompts() -> None:
    """表示言語に合わせた定型文ボタンを送る（定型文が無ければ何もしない）。

    英語のセッションでは [chat_starters].prompts_en を使い、空なら prompts にする。
    フロントエンドは最新の定型文メッセージだけを表示する（selectLatestStarters）。
    """
    prompts = _config.chat_starter_prompts
    if i18n.current_lang() == "en" and _config.chat_starter_prompts_en:
        prompts = _config.chat_starter_prompts_en
    if prompts:
        await cl.Message(content=STARTER_PREFIX + json.dumps(prompts, ensure_ascii=False)).send()


@cl.on_chat_start
async def on_chat_start() -> None:
    """Chainlit のチャットセッション開始時に呼ばれるフック。

    アプリ全体で共有する資源の初期化（_setup、未実施なら）を行った上で、
    このセッション専用の thread_id を発行して LangGraph の
    チェックポイント分離キーとして user_session に保存する。
    続けてこのセッション専用のグラフ（LLMモデル・httpxクライアントを含む）を
    _rebuild_graph() で構築し、cl.user_session["graph"] に保存する
    （他タブとはLLMクライアントを共有しない。本ファイル冒頭の
    モジュールdocstring参照）。最後に利用可能なスキル一覧を挨拶メッセージ
    として送信する。

    Args:
        なし（Chainlit のフック規約により引数なし）。

    Returns:
        None。副作用として cl.user_session への書き込みと
        ウェルカムメッセージの送信を行う。
    """
    await _setup()
    # LangGraphチェックポイントの分離キー（かつLLMクライアントのセッション
    # スコープ分離キーにも流用する）は、Chainlit自身のスレッドID
    # （cl.context.session.thread_id）をそのまま使う。新規接続時はこれも
    # 結局 uuid4() で新規発行される（chainlit/session.py 参照）ため従来と
    # 挙動は変わらないが、再開時（on_chat_resume）は要求されたIDがそのまま
    # 入るため、LangGraph側のチェックポイントとChainlit側のスレッドが
    # 自動的に一致し、同じ会話状態を引き継げる（[thread_store] 参照）。
    thread_id = cl.context.session.thread_id
    cl.user_session.set("thread_id", thread_id)
    _init_session_lang()
    # 会話ログ（[chat_log].enabled=true の場合のみ）の書き出し先を
    # セッション開始時に1回だけ確定する（日をまたいでも同じファイルに
    # 追記し続ける。src/chat_log.py 参照）。
    if _config.chat_log_enabled:
        user = cl.user_session.get("user")
        username = resolve_log_username(user.identifier if user else None)
        cl.user_session.set("chat_log_path", build_log_path(_config.chat_log_dir, username, thread_id))
    # このセッション専用のグラフを構築する（他タブとはLLMクライアントを共有しない）。
    # wait_when_busy=False: 新規セッション開始は直後に生成するとは限らないため、
    # 他タブが唯一の接続先を使用中でも待たない（_rebuild_graph docstring参照）。
    await _rebuild_graph(thread_id, wait_when_busy=False)
    cl.user_session.set("work_dir", None)
    cl.user_session.set("work_dir_access", None)
    # 次の on_message でLLMへ実際の作業ディレクトリ絶対パスを知らせるためのフラグ
    # （system_prompt はサーバー起動時に1回だけ組み立てられるため、セッション単位で
    # 変わる作業ディレクトリはそちらへ埋め込めない。HumanMessage側で伝える）。
    cl.user_session.set("work_dir_notice_pending", True)
    # サイドパネル上部の「作業ディレクトリ」表示を初期状態（未設定）から出す。
    await cl.Message(content=_format_work_dir_status(None)).send()
    # Task Management ツール（create_plan/approve_plan/update_task_progress）の
    # セッション状態。新しいセッションでは未作成・未承認から開始する。
    cl.user_session.set("plan", None)
    cl.user_session.set("plan_message", None)
    cl.user_session.set("plan_approved", False)
    # トークン使用量の会話全体累計（on_message の on_chat_model_end で加算）。
    # メインエージェント自身のLLM呼び出しとサブエージェント（dispatch_agent）
    # 内部のLLM呼び出しの両方を合算した値。
    cl.user_session.set("token_usage_cumulative", _new_usage_totals())
    # 上記のうちメインエージェント自身の呼び出し分のみの累計
    # （サブエージェントへ委譲した分は含まない。委譲がどれだけ会話コンテキストの
    # 節約に寄与しているかをユーザーが確認できるように分けて集計する）。
    cl.user_session.set("token_usage_cumulative_main", _new_usage_totals())
    # write_thread_note未呼び出しのまま圧縮を見送った回数（is_compaction_blocked_
    # by_missing_note の安全弁）。token_usage_cumulative_main と同じく、新しい
    # セッションでは0から開始する。
    cl.user_session.set("context_compaction_note_skip_count", 0)

    # run_script の作業ディレクトリを歯車アイコンから指定できるようにする。
    # 未入力（初期値）なら config.ini の [default_workdir].dir が使われる
    # （tools.py の _resolve_workdir）。
    await _send_work_dir_settings("")

    skills = scan_skills([_config.skills_dir, *_config.locohane_skills_dirs])
    names = i18n.t("list.separator").join(s.name for s in skills) or i18n.t("welcome.no_skills")
    welcome_template = _load_settings_text("welcome.md", i18n.t("welcome.default"))
    await cl.Message(content=welcome_template.replace("{skills}", names)).send()

    await _send_starter_prompts()

    # メインスレッドの表示件数上限をフロントエンドへ伝える（0=無制限も含め常に送信）。
    # 表示専用の設定であり、これ自体がLLMへの会話コンテキストに影響することはない
    # （フロントエンド側で selectMainThread() のフィルタにより本メッセージ自体も除外される）。
    await cl.Message(content=MAX_DISPLAY_MESSAGES_PREFIX + json.dumps(_config.ui_max_display_messages)).send()

    # サイドパネル（Step一覧）の表示件数上限をフロントエンドへ伝える（0=無制限も含め常に送信）。
    # 表示専用の設定であり、これ自体がLLMへの会話コンテキストに影響することはない。
    await cl.Message(
        content=MAX_DISPLAY_SIDE_STEPS_PREFIX + json.dumps(_config.ui_max_display_side_steps)
    ).send()

    # 入力欄の貼り付けテキスト化の閾値・最大入力文字数をフロントエンドへ伝える
    # （0以下=無効/無制限も含め常に送信）。最大入力文字数は on_message 側でも
    # _check_input_length() が最終防衛線として検査する。
    await cl.Message(
        content=INPUT_LIMITS_PREFIX
        + json.dumps(
            {
                "pasteAsAttachmentThresholdChars": _config.ui_paste_as_attachment_threshold_chars,
                "maxInputChars": _config.ui_max_input_chars,
            }
        )
    ).send()


if _config.thread_store_enabled:

    @cl.on_chat_resume
    async def on_chat_resume(thread: dict) -> None:
        """左サイドバーで過去のスレッドを選んだ際に、on_chat_start の代わりに呼ばれる。

        ウェルカムメッセージ・定型文ボタン・作業ディレクトリカード・表示件数上限
        マーカーは再送しない（resume_thread イベントで過去の cl.Message が
        そのまま frontend の messagesState へ再生されるため、再送すると
        画面上で二重表示になる）。cl.ChatSettings(...) だけは再送が必要
        （ステップとして永続化されないライブ push のみの情報のため、
        再開直後はギアアイコンのパネルが空になってしまう）。

        work_dir/plan/plan_approved/トークン累計は on_message 側で毎ターン
        thread_store へスナップショットした値（thread["metadata"]）から
        復元する（無ければ on_chat_start と同じ既定値）。これが無いと、
        Plan Mode中のタスク一覧・承認状態は cl.user_session 限定の状態
        （LangGraph側には保存されない）のため、再開のたびに静かに失われる。
        """
        await _setup()
        thread_id = cl.context.session.thread_id
        cl.user_session.set("thread_id", thread_id)
        _init_session_lang()

        if _config.chat_log_enabled:
            user = cl.user_session.get("user")
            username = resolve_log_username(user.identifier if user else None)
            # 同じ thread_id の既存ログファイルがあれば追記継続する（無ければ
            # 新規作成）。ファイルが分割されないよう、再開したことは「いつ再開
            # したか」が分かる区切り行として同じファイル内に残す。
            existing_log_path = find_existing_log_path(_config.chat_log_dir, username, thread_id)
            log_path = existing_log_path or build_log_path(_config.chat_log_dir, username, thread_id)
            cl.user_session.set("chat_log_path", log_path)
            if existing_log_path is not None:
                append_resume_marker(log_path)

        # wait_when_busy=False: スレッド再開は直後に生成するとは限らないため、
        # 他タブが唯一の接続先を使用中でも待たない（_rebuild_graph docstring参照。
        # 2026-08-26 ユーザー報告: 生成中の別タブがある間スレッド切替が固まる不具合）。
        await _rebuild_graph(thread_id, wait_when_busy=False)

        meta = thread.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        cl.user_session.set("work_dir", meta.get("work_dir"))
        cl.user_session.set("work_dir_access", None)
        cl.user_session.set("work_dir_notice_pending", True)
        plan = meta.get("plan")
        plan_approved = bool(meta.get("plan_approved"))
        cl.user_session.set("plan", plan)
        cl.user_session.set("plan_approved", plan_approved)
        if plan:
            # plan_message（サイドパネルのPlanCard用メッセージ）は cl.Message
            # インスタンスのためスレッドのmetadataへ永続化できず、旧実装は
            # ここで常にNoneにリセットしていた。VSCode拡張のWebViewが非表示→
            # 再表示で破棄・再構築されると、ユーザーが明示的にリロードした
            # 覚えがなくても on_chat_resume が発火する（2026-09-04 ユーザー
            # 報告・実機ログで確認）。plan_messageがNoneのまま以降
            # update_task_progress が呼ばれ続けると、ステップの状態自体は
            # 更新されるのに message.update() がガードでスキップされ続け、
            # サーバーログには成功と出るのにサイドパネルの表示だけ古いまま
            # 固着する。
            #
            # ここでは send()/update() で新規にDBへ書き込まない（2026-09-04
            # レビュー指摘で判明: chainlit/socket.py の connection_successful は
            # on_chat_resume 完了後に thread（on_chat_resume 呼び出し*前*に
            # 確定したDBスナップショット）で emitter.resume_thread() を呼び、
            # フロント側の resume_thread ハンドラは新しい配列をそのスナップ
            # ショットだけから作って messagesState を丸ごと置き換える
            # （@chainlit/react-client dist/index.js 参照）。そのため
            # on_chat_resume内で新規送信しても直後の resume_thread イベントで
            # 上書きされ消える上、送信のたびに新規step（＝新規DB行）が
            # steps テーブルに積み上がる副作用だけが残る）。
            #
            # create_plan/update_task_progress が既に plan_message_id()
            # （thread_idから決定的に導出したid、src/plan_persist.py参照）を
            # 使って書き込み済みのstep行を、resume_thread イベントがそのまま
            # 正しい最新内容で再生してくれるため、ここでは同じidを持つ
            # cl.Message インスタンスをローカル参照として作るだけでよい
            # （send()しない）。これで以降の update_task_progress の
            # message.update() が正しく効くようになる。
            #
            resolved_message_id = plan_message_id(thread_id)
            # created_at はDBの既存step（あれば）から引き継ぐ。指定しないと
            # cl.Message の created_at は None のままとなり、後続の
            # message.update() が upsert_step_row の created_at をNoneの
            # フォールバック（_now()）で現在時刻に書き換えてしまい、
            # get_thread_detail の ORDER BY created_at によるsteps再生順序が
            # 狂う（2026-09-04 レビュー指摘）。
            existing_step = next(
                (s for s in thread.get("steps", []) if s.get("id") == resolved_message_id),
                None,
            )
            finished = all(s["status"] == "completed" for s in plan)
            plan_message = cl.Message(
                id=resolved_message_id,
                created_at=(existing_step or {}).get("createdAt"),
                content=_render_plan_payload(plan, finished=finished, approved=plan_approved),
            )
            cl.user_session.set("plan_message", plan_message)
        else:
            cl.user_session.set("plan_message", None)
        cl.user_session.set("token_usage_cumulative", meta.get("token_usage_cumulative") or _new_usage_totals())
        cl.user_session.set("token_usage_cumulative_main", meta.get("token_usage_cumulative_main") or _new_usage_totals())
        cl.user_session.set("context_compaction_note_skip_count", meta.get("context_compaction_note_skip_count") or 0)

        # 復元した実体値をそのまま初期表示する（以前は常に空文字固定
        # だったため、歯車パネルを開いても実際の値が見えず、表示との
        # 不一致に気づく手段が無かった）。
        await _send_work_dir_settings(meta.get("work_dir") or "")

        if thread_id in _pending_asks:
            # 応答待ちのまま離れて戻ってきたスレッド。今このセッションで
            # 同じAsk*Messageを出し直す（_relay_pending_ask のdocstring参照）。
            # on_chat_resume 自体をブロックしないよう非同期に起動する。
            asyncio.create_task(_relay_pending_ask(thread_id))

        logging.getLogger(__name__).info("Resumed thread thread_id=%s", thread_id)


@cl.on_chat_end
async def on_chat_end() -> None:
    """タブを閉じる・切断された際に呼ばれるフック。

    src/llm.py の _active_async_clients（セッションID→WeakSetの辞書）は、
    以前は「セッション終了」を検知する手段が無かったため、キー自体は
    プロセス寿命中ずっと残り続けていた（値のWeakSetはクライアントの
    参照が無くなり次第GCで自然に空になるが、辞書エントリそのものは
    消えない）。実害はごく軽微（文字列+空WeakSetオブジェクト程度）だが、
    forget_session() で明示的に片付ける。src/tools.py のセッション毎
    ツール呼び出しガード（_TOOL_CALL_SEMAPHORES/_DISPATCH_AGENT_SEMAPHORES）
    も同じ理由で、forget_session_tool_semaphores() により辞書エントリを
    片付ける。

    注意: ここでは強制クローズ（aclose_active_llm_clients）は呼ばない。
    このフックは session.current_task をキャンセルしないため、停止
    ボタンを押さずにタブを閉じた場合はバックグラウンドで処理が続いて
    いる可能性があり、その最中にクライアントを強制closeすると新たな
    エラーを誘発しかねない。辞書キーの掃除のみに留める。

    以前はここで execute_python_code が作る `_tmp_<thread_id>`
    （src/tools.py の _resolve_exec_workdir() 参照）も即座に rmtree して
    いたが、廃止した。サイドバーで別スレッドへ切り替えるとソケットが
    切断されこのフックが発火する（chainlit/socket.py の disconnect
    ハンドラ）が、バックグラウンド処理自体は current_task がキャンセル
    されないため続行する。そのため生成中に別スレッドへ切り替えると、
    execute_python_code が使用中の `_tmp_<thread_id>` が処理の途中で
    消え、裏で継続中のPython実行がファイル欠落で失敗しうる不具合が
    あった。`_tmp_<thread_id>` は常に default_workdir 配下に作られる
    （_resolve_exec_workdir() 参照）ため、後片付けは
    default_workdir_retention_days による日数ベースの自動削除
    （起動時cleanup_old_dirs + cleanup_run_cleanup_dirs_loop）に一本化する。
    """
    thread_id = cl.user_session.get("thread_id")
    if thread_id is not None:
        forget_session(thread_id)
        forget_session_tool_semaphores(thread_id)


async def _send_work_dir_settings(initial: str) -> None:
    """歯車アイコンの ChatSettings（作業ディレクトリ入力欄）を initial の値で送る。

    cl.ChatSettings.send() は画面の入力欄と cl.context.session.chat_settings の
    両方を initial で上書きする。on_chat_start（空）・on_chat_resume（復元値）に
    加え、on_settings_update で入力値を拒否したときにも現在の値で送り直し、
    拒否したパスが入力欄・chat_settings に残って実際の作業ディレクトリと
    食い違わないようにする。

    Args:
        initial: 入力欄に表示する値（未設定なら空文字）。

    Returns:
        None。
    """
    await cl.ChatSettings(
        [
            TextInput(
                id="work_dir",
                label=i18n.t("workdir.setting_label"),
                initial=initial,
                placeholder=i18n.t("workdir.setting_placeholder", default=_config.default_workdir),
            )
        ]
    ).send()


async def _apply_work_dir(raw: str) -> str | None:
    """作業ディレクトリの入力値を実際にプローブして cl.user_session に反映する。

    入力値が空文字なら work_dir を None に戻す（既定動作＝config.ini の
    default_workdir に復帰）。値がある場合は probe_workdir_access() で
    サーバーPCから見た実際の読み書き可否を確認し、存在しない・読み取れない
    フォルダは反映せずエラーメッセージを返す（呼び出し元が UI に表示する）。
    以前は拒否せず保存して既定フォルダへ黙ってフォールバックしていたが、
    ブラウザだけ別PCの構成では自分のPCのパスを入力したことに気づけなかった
    ため、設定時点で知らせるようにした（2026-10-23 ユーザー要望）。
    読み取りのみ可能なフォルダは受け付け、結果を
    cl.user_session["work_dir_access"] にキャッシュする（src/tools.py の
    _resolve_workdir() がこれを見て書き込みを既定フォルダへフォールバックする）。

    ChatSettings（歯車アイコン）と、独自フロントエンドの「作業フォルダ」
    アイコン（pick_work_dir action_callback）の両方から共通で呼ばれる。

    以前は cl.user_session を更新するのみで、thread_store への永続化は
    ターン完了時のベストエフォート・スナップショット（_on_message_impl末尾、
    3248行目付近）任せだった。そのため、設定直後のターンが停止ボタン・
    通信エラー・思考ループ上限・recursion_limit等で異常終了しスナップ
    ショットに到達しないと、work_dir の変更がDBへ一度も保存されないまま
    残った。この状態でスレッドを再開すると、サイドパネルの表示（過去の
    確認メッセージをsteps再生で表示しているだけなので常に最新に見える）
    と、cl.user_session（on_chat_resume がDBのmetadataから復元する実体）
    が食い違い、表示上はカスタムディレクトリなのに実際のツール実行は
    既定値へ静かにフォールバックする不具合につながっていた（2026-08-21
    ユーザー報告）。ここで即座に保存することで、ターンの成否に関わらず
    表示と実体を一致させる。

    Args:
        raw: ユーザーが指定したパス文字列（未加工）。

    Returns:
        反映できなかった場合はエラーメッセージ、反映した場合は None。反映時は
        副作用として cl.user_session の更新・thread_store への即時永続化・
        状態メッセージの送信を行う。
    """
    raw = raw.strip().strip('"')
    if not raw:
        cl.user_session.set("work_dir", None)
        cl.user_session.set("work_dir_access", None)
        cl.user_session.set("work_dir_notice_pending", True)
        await _persist_work_dir(None)
        await cl.Message(content=_format_work_dir_status(None)).send()
        return None

    try:
        resolved, status = await asyncio.wait_for(asyncio.to_thread(_probe_work_dir_input, raw), _WORK_DIR_PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return i18n.t("workdir.probe_timeout", path=raw)
    except (OSError, ValueError, RuntimeError) as e:
        return i18n.t("workdir.probe_invalid", path=raw, error=e)
    error = work_dir_error_message(resolved, status)
    if error is not None:
        return error
    cl.user_session.set("work_dir", resolved)
    cl.user_session.set("work_dir_access", status)
    cl.user_session.set("work_dir_notice_pending", True)
    await _persist_work_dir(resolved)
    await cl.Message(content=_format_work_dir_status(resolved, status)).send()
    return None


# 作業フォルダの存在確認・候補列挙の待ち時間上限（秒）。応答しないネットワーク
# 共有（UNC パス等）を指定されても入力欄が固まり続けないようにする。
_WORK_DIR_PROBE_TIMEOUT_SECONDS = 5.0


def _probe_work_dir_input(raw: str) -> tuple[str, WorkDirAccessStatus]:
    """入力パスを絶対パスへ解決し、サーバーPCから見たアクセス可否を調べる（スレッドで実行）。"""
    resolved = str(Path(raw).expanduser().resolve())
    return resolved, probe_workdir_access(Path(resolved))


async def _persist_work_dir(resolved: str | None) -> None:
    """work_dir の変更を、ターン完了を待たずに即座に thread_store へ保存する。

    thread_store.save_thread の metadata は dict.update() で既存値へ統合
    される（丸ごと置き換えではない）ため、plan/token_usage_cumulative等の
    他フィールドを巻き込まずに work_dir だけを更新できる（src/thread_store.py
    save_thread docstring参照）。_apply_work_dir docstring も参照。

    session.has_first_interaction が False（＝まだ一度もチャット発言していない
    新規セッションで、歯車アイコン/作業フォルダアイコンだけ操作した状態）の
    間は保存しない。save_thread は無条件で threads 行をINSERT OR IGNOREする
    ため、ここでガード無しに呼ぶと「1文字も発言していないのに左サイドバーへ
    空の会話が現れる」不具合（_patch_chainlit_ignore_ui_action_first_interaction
    docstring参照、2026-08-19ユーザー報告）をthread_store経路で再発させて
    しまう。最初のチャット発言後（has_first_interaction=True）に変更した
    場合のみ、その場で保存する。
    """
    if not (_config.thread_store_enabled and _thread_store_conn is not None):
        return
    if not cl.context.session.has_first_interaction:
        return
    thread_id = cl.user_session.get("thread_id")
    if thread_id is None:
        return
    await thread_store.save_thread(
        _thread_store_conn,
        thread_id,
        metadata={"work_dir": resolved},
    )


async def _persist_plan_state() -> None:
    """plan/plan_approved の変更を、ターン完了を待たずに即座に thread_store へ
    保存する。src/plan_persist.py 経由で src/tools.py の各ツール
    （create_plan/approve_plan/update_task_progress/lock_plan_mode/
    toggle_plan_mode_from_ui）から呼ばれる（同モジュールのdocstring参照）。

    _persist_work_dir と同じ理由・同じパターン: 承認直後のターンが停止
    ボタン・通信エラー・思考ループ上限・recursion_limit・計画却下等で異常
    終了すると、ターン完了時のまとめてスナップショット（_on_message_impl
    末尾）に到達せず、承認済み状態が一度もDBへ保存されないままスレッドが
    再開時に古い（未承認の）metadataへ巻き戻ってしまう
    （2026-08-24 ユーザー報告）。ここで即座に保存することで、ターンの
    成否に関わらず状態を一致させる。
    """
    if not (_config.thread_store_enabled and _thread_store_conn is not None):
        return
    thread_id = cl.user_session.get("thread_id")
    if thread_id is None:
        return
    await thread_store.save_thread(
        _thread_store_conn,
        thread_id,
        metadata={
            "plan": cl.user_session.get("plan"),
            "plan_approved": cl.user_session.get("plan_approved"),
        },
    )


register_plan_persist(_persist_plan_state)


async def _persist_token_usage() -> None:
    """トークン使用量の累計を、ターン完了を待たずに即座に thread_store へ保存する。

    _persist_plan_state と同じ理由・同じパターン: 累計はターン完了時のまとめて
    スナップショット（_on_message_impl 末尾）でしか保存していなかったため、
    生成中にブラウザが切断される・停止ボタン・通信エラー等でターンが完了
    しないと一度も保存されず、スレッド再開（on_chat_resume）で 0 から数え直しに
    なっていた（2026-10-08、設定ダッシュボードのトークン推移グラフで発覚。
    会話一覧の「トークン累計」やチャット画面のトークン使用量カードも少なく出ていた）。

    on_chat_model_end（LLM呼び出し1回）ごとに呼ばれる。保存の失敗で進行中の
    ターンを止める価値は無いため、例外は警告ログに留める。
    """
    if not (_config.thread_store_enabled and _thread_store_conn is not None):
        return
    thread_id = cl.user_session.get("thread_id")
    if thread_id is None:
        return
    try:
        await thread_store.save_thread(
            _thread_store_conn,
            thread_id,
            metadata={
                "token_usage_cumulative": cl.user_session.get("token_usage_cumulative"),
                "token_usage_cumulative_main": cl.user_session.get("token_usage_cumulative_main"),
            },
        )
    except Exception:  # noqa: BLE001 - 付帯状態の保存失敗で会話を止めない
        logging.getLogger(__name__).warning("Failed to save cumulative token usage immediately thread_id=%s", thread_id, exc_info=True)


async def _record_monthly_usage(usage: dict) -> None:
    """LLM 呼び出し1回分の usage_metadata を、月別・ユーザー別のトークン台帳へ加算する。

    設定ダッシュボードの「利用量・料金」画面用（src/thread_store.py の
    token_usage_monthly）。スレッドの累計とは別に持つのは、月の区切りが要るのと、
    スレッドを削除しても使った量は消さないため。失敗しても会話は止めない。
    """
    if not (_config.thread_store_enabled and _thread_store_conn is not None):
        return
    totals = _new_usage_totals()
    _accumulate_usage(totals, usage)
    try:
        owner = thread_store.resolve_owner(cl.user_session.get("user"))
        await thread_store.add_monthly_usage(_thread_store_conn, owner, totals)
    except Exception:  # noqa: BLE001 - 集計用の付帯情報のため
        logging.getLogger(__name__).warning("Failed to record monthly token usage", exc_info=True)


async def _on_compaction_usage(kind: str, role: str, usage: dict) -> None:
    """圧縮処理自身のLLM呼び出し（要約・write_thread_note 強制実行）のトークンを会話累計へ加算する。

    これらはグラフ外の ainvoke のため on_chat_model_end では捕捉されず、従来は
    会話累計（トークン使用量カード・スレッドの token_usage_cumulative）に一切
    含まれていなかった（src/context_compaction.py _record_compaction_usage 参照）。
    token_usage_cumulative_main（圧縮の発火判定用）には加算しない。圧縮の直後に
    0 へ戻す値であり、圧縮コストを含めると判定の意味が変わるため。
    """
    try:
        cumulative = cl.user_session.get("token_usage_cumulative") or _new_usage_totals()
    except ChainlitContextException:
        # Chainlit のセッション文脈外（テスト・evals 等）では集計しない（ログには残っている）。
        return
    _accumulate_usage(cumulative, usage)
    cl.user_session.set("token_usage_cumulative", cumulative)
    await _persist_token_usage()
    await _record_monthly_usage(usage)
    call_totals = _new_usage_totals()
    _accumulate_usage(call_totals, usage)
    await _send_token_usage({"compaction": {"label": i18n.t("usage.compaction"), **call_totals}})


register_compaction_usage_listener(_on_compaction_usage)


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    """ChatSettings（歯車アイコン）で作業ディレクトリが変更されたときに呼ばれるフック。

    Args:
        settings: ChatSettings の全入力値（{"work_dir": "..."}）。

    Returns:
        None。
    """
    error = await _apply_work_dir(settings.get("work_dir") or "")
    if error is not None:
        # 拒否したパスが入力欄・chat_settings に残らないよう、現在の値で送り直す。
        await _send_work_dir_settings(cl.user_session.get("work_dir") or "")
        await cl.Message(content=i18n.t("workdir.change_failed", error=error)).send()


@cl.action_callback("toggle_plan_mode")
async def on_toggle_plan_mode(action: cl.Action) -> None:
    """送信ボタン付近の Plan Mode / Edit Automatically バッジをクリックした際の
    action_callback。実体は src/tools.py の toggle_plan_mode_from_ui（config.ini の
    [plan].allow_badge_unlock によるロック解除方向のガードもそちら側で行う）。

    Args:
        action: フロントエンドから callAction で渡された cl.Action（未使用）。

    Returns:
        None。
    """
    del action
    await toggle_plan_mode_from_ui()


@cl.action_callback("pick_work_dir")
async def on_pick_work_dir(action: cl.Action) -> dict:
    """独自フロントエンドの「作業フォルダ」アイコンから呼ばれる action_callback。

    以前は tkinter の OS ネイティブなフォルダ選択ダイアログをここで表示して
    いたが、それはバックエンドとブラウザが同一マシン上で動く前提の機能で、
    サーバー駆動でブラウザだけ別PCという構成だとダイアログがサーバー側の
    誰も見ていない画面に無言で表示され、クライアントは応答をひたすら待つ
    だけになり「フリーズした」ように見える不具合があった（2026-08-19
    ユーザー報告）。フロントエンド（WorkDirButton.tsx）側にパス入力欄の
    ポップオーバーを設け、入力済みのパス文字列を payload で受け取る方式に
    変更した。

    サーバーPCから存在しない・読み取れないフォルダは反映せず、エラーメッセージを
    返す（ポップオーバーを閉じずに入力欄の下へ表示する）。

    Args:
        action: フロントエンドから callAction で渡された cl.Action。
            payload["path"] にユーザーが入力したパス文字列が入る。

    Returns:
        {"ok": bool, "error": str | None}（callAction のレスポンスの response に入る）。
    """
    raw = str(action.payload.get("path", ""))
    error = await _apply_work_dir(raw)
    return {"ok": error is None, "error": error}


@cl.action_callback("set_language")
async def on_set_language(action: cl.Action) -> dict:
    """画面の表示設定で言語を切り替えたときに独自フロントエンドから呼ばれる action_callback。

    セッションの言語だけを更新し、グラフ（応答言語の指示）は次の on_message で
    組み直す（生成中に切り替えても今の生成を壊さないため）。サイドパネルの
    作業ディレクトリ表示・歯車の入力欄・定型文ボタンは、新しい言語で送り直す
    （フロントエンドはいずれも最新のメッセージを表示する）。

    Args:
        action: payload["lang"] に "ja" / "en" が入る。

    Returns:
        {"ok": bool, "lang": str}（callAction のレスポンスの response に入る）。
    """
    lang = i18n.normalize(action.payload.get("lang"))
    if lang is None:
        return {"ok": False, "lang": i18n.current_lang()}
    if lang == i18n.current_lang():
        return {"ok": True, "lang": lang}
    cl.user_session.set(i18n.SESSION_KEY, lang)
    work_dir = cl.user_session.get("work_dir")
    await cl.Message(
        content=_format_work_dir_status(work_dir, cl.user_session.get("work_dir_access") if work_dir else None)
    ).send()
    await _send_work_dir_settings(work_dir or "")
    await _send_starter_prompts()
    return {"ok": True, "lang": lang}


@cl.action_callback("work_dir_server_info")
async def on_work_dir_server_info(action: cl.Action) -> dict:
    """作業フォルダのポップオーバーに出すサーバーPCのホスト名を返す action_callback。

    ブラウザから見えるのは接続先のアドレス（0.0.0.0 で待ち受けて LAN の IP で
    開いた場合はその IP）だけなので、利用者が「どのPCのパスか」を分かるよう
    サーバー側のホスト名を返す。取得できなければ空文字（フロントエンドは IP を出す）。

    Args:
        action: 未使用。

    Returns:
        {"hostname": str}。
    """
    del action
    return {"hostname": server_hostname()}


@cl.action_callback("reasoning_effort_info")
async def on_reasoning_effort_info(action: cl.Action) -> dict:
    """入力欄ツールバーの reasoning_effort 選択（ReasoningEffortButton.tsx）が、
    「config.ini の値を使う」選択肢に添える [llm].reasoning_effort の値を返す action_callback。

    選んだ値そのものは送信時にメッセージの metadata で受け取る（on_message 参照）。

    Args:
        action: 未使用。

    Returns:
        {"default": str}（config.ini で空欄なら空文字）。
    """
    del action
    return {"default": _config.reasoning_effort or ""}


@cl.action_callback("suggest_work_dir")
async def on_suggest_work_dir(action: cl.Action) -> dict:
    """作業フォルダ入力欄に数文字入力したときのパス候補を返す action_callback。

    候補はサーバーPC上に実在するフォルダ（src/workdir_suggest.py 参照）。
    応答しないネットワーク共有等で固まらないよう、時間内に終わらなければ空で返す。

    Args:
        action: payload["text"] に入力途中の文字列が入る。

    Returns:
        {"suggestions": [絶対パス, ...]}（最大 config.ini [ui].work_dir_max_suggestions 件）。
    """
    text = str(action.payload.get("text", ""))
    limit = _config.ui_work_dir_max_suggestions
    try:
        suggestions = await asyncio.wait_for(
            asyncio.to_thread(suggest_work_dirs, text, limit), _WORK_DIR_PROBE_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, OSError, ValueError):
        suggestions = []
    return {"suggestions": suggestions}


def _save_uploads(message: cl.Message) -> list[str]:
    """アップロードファイルをこのスレッド専用の作業フォルダへ保存し、保存先パスの一覧を返す。

    message.elements に含まれる各添付要素について、element.path
    （Chainlit が保持する一時保存先）から、default_workdir 配下のスレッド
    専用フォルダ（_resolve_exec_workdir()、`_tmp_<thread_id>`）の
    `uploads/<送信日時>/` へコピーする。

    以前は [uploads].dir/<ユーザー名>/ の1フォルダへ上書きで保存していたが、
    画像を data URL で会話へ埋め込まずパスだけを LLM へ渡すようにしてからは、
    LLM が後で開くまでに同名のファイル（別スレッド・後のターンでの再アップ
    ロード）で上書きされると別の画像を見てしまう（analyze_image の重複ガードは
    パスで判定するため、新しい画像を「確認済み」として拒否もする）。また
    dispatch_agent_batch でフォルダごと委譲すると過去のアップロードまで対象に
    入っていた。送信ごとのフォルダにすることで、その送信のファイルだけを
    指すフォルダが得られ、他スレッドからは _foreign_tmp_dir_error で読めず、
    削除も default_workdir の保持日数（_tmp_* の丸ごと削除）に一本化される。
    作業フォルダをツールバーで指定していても保存先は変えない（利用者の
    フォルダへアップロードの複製を撒かないため）。

    path を持たない要素（テキストのみの要素など）はスキップする。同じ送信内で
    ファイル名が重複した場合は「名前 (2).拡張子」のように番号を付けて残す。
    ファイル名は src/thread_store.py の添付の永続化と同じ規則で無害化する。

    Args:
        message: on_message で受け取った Chainlit のメッセージオブジェクト。
            elements にアップロードファイルの情報が含まれる。

    Returns:
        保存先の絶対パス文字列のリスト（保存した順）。
        アップロードが無ければ空リスト。
    """
    elements = [e for e in message.elements or [] if getattr(e, "path", None)]
    if not elements:
        return []
    return _copy_uploads([(str(e.path), e.name) for e in elements], _resolve_exec_workdir())


def _build_injection(thread_id: str, text: str, sources: list[tuple[str, str | None]]) -> tuple[str, list[dict]]:
    """生成中の追加指示の、LLM へ渡す本文と添付ファイルを、受け付けた時点で用意する。

    添付は通常の送信（_save_uploads）と同じくスレッド専用フォルダの `uploads/<送信日時>/` へコピーし、
    本文は _build_user_llm_text で同じ形にする（貼り付けテキストの展開・保存先と `@N`・UNC パスの `@N` 化）。
    Chainlit のセッション文脈の無い HTTP の処理から呼ぶため、thread_id を明示して使う。

    Args:
        thread_id: 対象スレッド。
        text: ユーザーが入力した本文。
        sources: (Chainlit が保持する一時保存先, 元のファイル名) の列。

    Returns:
        (LLM へ渡す本文, 画面表示用の添付 {"name", "path"} の列)。

    Raises:
        OSError: 添付のコピーに失敗した場合。
    """
    saved = _copy_uploads(sources, _resolve_exec_workdir(thread_id)) if sources else []
    llm_text = _build_user_llm_text(register_raw_unc_paths_in_text(text, thread_id), saved, thread_id=thread_id)
    return llm_text, [{"name": Path(p).name, "path": p} for p in saved]


def _copy_uploads(sources: list[tuple[str, str | None]], exec_dir: Path) -> list[str]:
    """アップロードされたファイルを exec_dir の `uploads/<送信日時>/` へコピーし、保存先パスの一覧を返す。

    _save_uploads と、生成中の追加指示（/locohane/threads/{id}/inject）で共通。

    Args:
        sources: (Chainlit が保持する一時保存先, 元のファイル名) の列。
        exec_dir: スレッド専用の作業フォルダ（_resolve_exec_workdir）。

    Returns:
        保存先の絶対パス文字列のリスト（sources の順）。
    """
    if not sources:
        return []
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{uuid.uuid4().hex[:6]}"
    dest_dir = exec_dir / "uploads" / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for src, original_name in sources:
        # ブラウザ以外から送られた名前のパス区切り・`..` で送信フォルダの外へ出さず、
        # Windows で使えない文字・予約デバイス名で copyfile が失敗しないようにする。
        name = _sanitize_filename_component(original_name or Path(src).name)
        dest = dest_dir / name
        n = 2
        while dest.exists():
            dest = dest_dir / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        shutil.copyfile(src, dest)
        saved.append(str(dest))
        logging.getLogger(__name__).info("Saved upload: %s", dest)
    # _tmp_* の自動削除はフォルダ自身の更新日時で判定する（cleanup_old_dirs）が、
    # 入れ子の uploads/<送信日時>/ を作っても _tmp_* 自身の更新日時は変わらない
    # ことがあるため、アップロードしたばかりのスレッドが消されないよう更新する。
    try:
        os.utime(exec_dir)
    except OSError:
        logging.getLogger(__name__).warning("Failed to update the modification time of the thread work folder: %s", exec_dir, exc_info=True)
    return saved


def _is_pasted_text_file(path: str) -> bool:
    """フロントエンドが長文の貼り付けから生成した添付（PASTED_TEXT_FILENAME_RE）か判定する。"""
    return PASTED_TEXT_FILENAME_RE.match(Path(path).name) is not None


def _read_pasted_text(path: str) -> str:
    """貼り付けテキスト添付の本文を読む（フロントエンドは File([text]) で UTF-8 として送る）。"""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _check_input_length(message: cl.Message) -> str | None:
    """送信内容が [ui].max_input_chars を超えていれば、ユーザーへ返す拒否メッセージを返す。

    本文（message.content）と貼り付けテキスト添付（_is_pasted_text_file）の
    合計文字数で判定する。フロントエンド（Composer.tsx）も同じ上限で送信を
    止めるが、古いフロントエンドや直接のSocket.IO送信を想定した最終防衛線。

    Args:
        message: on_message で受け取った Chainlit のメッセージ。

    Returns:
        上限以内（または max_input_chars が0以下=無制限）なら None。
        超過していれば拒否理由の文字列。
    """
    limit = _config.ui_max_input_chars
    if limit <= 0:
        return None
    total = len(message.content or "")
    for element in message.elements or []:
        src = getattr(element, "path", None)
        if src and _is_pasted_text_file(element.name or src):
            total += len(_read_pasted_text(src))
    if total <= limit:
        return None
    return i18n.t("notice.input_too_long", total=total, limit=limit)


def _build_human_message(user_text: str, saved_paths: list[str], work_dir_notice: str | None = None) -> HumanMessage:
    """アップロードファイルを踏まえて HumanMessage を組み立てる。

    貼り付けテキスト添付（_is_pasted_text_file。入力欄へ長文を貼り付けた際に
    フロントエンドが添付化したもの）は、ユーザーが本文として入力したものと
    同じ扱いにするため内容をテキストへ展開する（低パラメータモデルにファイル
    読み込みのツール呼び出しを強いないため）。それ以外のファイルは画像も含めて
    パスをテキストへ追記するのみ（内容を見るかどうかは LLM に任せ、画像なら
    analyze_image、それ以外は run_script 等のツールにパスとして渡させる）。
    画像を data URL として直接積まないのは、大量の画像をアップロードすると
    メインエージェントのコンテキストを画像だけで大きく圧迫するため。

    追記するパスは path_memory へ登録して `@N` を添える（システムプロンプトの
    「ツールのパス引数には `@N` を渡し、フルパスを書き起こさない」に従えるように
    するため。長い日本語のパスやUNCパスの書き写し誤りを避ける）。2件以上なら
    送信フォルダ（_save_uploads() の `uploads/<送信日時>/`）も載せ、
    dispatch_agent_batch の path にそのまま渡せるようにする。

    Args:
        user_text: ユーザーが入力したメッセージ本文。
        saved_paths: _save_uploads() が返した保存先パスの一覧。
        work_dir_notice: 作業ディレクトリの実際の絶対パスをLLMへ知らせる
            テキストブロック（呼び出し元が「知らせる必要がある」と判断した
            ターンのみ渡す。None なら何も追記しない）。ユーザーの画面上の
            チャット吹き出し（message.content）ではなくLLM向けの
            HumanMessage.content のみに追記されるため、UIには表示されない。

    Returns:
        文字列 content の HumanMessage。
    """
    # ユーザー自身の発話の目印（[llm].reasoning_preserve 有効時、thinking を
    # 載せ直す範囲の境界になる。src/llm/dialect.py の mark_user_turn 参照）。
    return mark_user_turn(HumanMessage(content=_build_user_llm_text(user_text, saved_paths, work_dir_notice)))


def _build_user_llm_text(
    user_text: str,
    saved_paths: list[str],
    work_dir_notice: str | None = None,
    *,
    thread_id: str | None = None,
) -> str:
    """_build_human_message の本文部分（貼り付けテキストの展開・アップロードの保存先と `@N`）。

    生成中の追加指示（/locohane/threads/{id}/inject）でも同じ形で LLM へ渡すため切り出している。

    Args:
        user_text / saved_paths / work_dir_notice: _build_human_message と同じ。
        thread_id: パスメモリーへ登録するスレッド。省略時は今の Chainlit セッションの thread_id
            （セッション文脈の無い HTTP の処理から呼ぶ場合は明示する）。
    """
    pasted_paths = [p for p in saved_paths if _is_pasted_text_file(p)]
    other_paths = [p for p in saved_paths if not _is_pasted_text_file(p)]

    text = user_text
    for i, p in enumerate(pasted_paths, start=1):
        # 保存先パスも添えておく（長文をスクリプトで処理させたい場合に渡せるように）。
        text += f"\n\n[Pasted text {i} (saved to: {p})]\n{_read_pasted_text(p)}\n[End of pasted text {i}]"
    if work_dir_notice:
        text += f"\n\n{work_dir_notice}"
    if other_paths:
        tokens = {
            path: token
            for token, path in _register_path_memory(other_paths, description="upload", thread_id=thread_id).items()
        }
        lines = [f"- {tokens[p]}: {p}" if p in tokens else f"- {p}" for p in other_paths]
        folders = list(dict.fromkeys(str(Path(p).parent) for p in other_paths))
        if len(other_paths) >= 2 and len(folders) == 1:
            folder_token = next(
                iter(_register_path_memory(folders, description="upload (sent folder)", thread_id=thread_id)), None
            )
            lines.append(f"(folder: {folder_token}: {folders[0]})" if folder_token else f"(folder: {folders[0]})")
        paths = "\n".join(lines)
        text += f"\n\n[Saved locations of uploaded files (pass @N to tools)]\n{paths}"
    return text


_TABLE_LINE_RE = re.compile(r"^[ \t]{0,3}\|")


def _ensure_blank_line_before_tables(text: str) -> str:
    """Markdownテーブルらしき行の直前に空行が無ければ挿入する。

    LLMが「番号付きリストで一覧を書いた直後、空行を挟まずに表を書く」
    ような出力をすると、Markdownパーサーは表を独立したブロックと認識できず
    直前のリスト項目の続きの段落として飲み込んでしまう（実機検証で確認済み:
    表が `<table>` ではなく直前の `<li>` に取り込まれ、セル内の
    `![alt](パス)` も `<img src="">` — src が空文字列 — になり画像ごと
    消える）。空行の挿入はMarkdownの意味を変えない安全な正規化なので、
    LLMの出力形式に関わらず常に適用する。
    """
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        if _TABLE_LINE_RE.match(line) and out and out[-1].strip() != "" and not _TABLE_LINE_RE.match(out[-1]):
            out.append("")
        out.append(line)
    return "\n".join(out)


_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^()]+)\)")


async def _embed_local_images_as_session_urls(text: str) -> str:
    """回答本文中の `![alt](絶対パス)` を、ブラウザから取得できるURLへ差し替える。

    `data/elements/` への永続化配信（`/locohane/elements/{id}`、
    `src/thread_store.py` の `persist_element_file` 参照）に統一している
    （以前は Chainlit のセッションファイル配信 `/project/file/<id>?session_id=...`
    を使っていたが、プロセスメモリ上の一時マッピング依存でプロセス再起動後に
    リンク切れになっていた）。`[thread_store] enabled = false` で
    `_thread_store_conn` が無い場合のみ、従来の `session.persist_file()` 経路へ
    フォールバックする（スレッド一覧機能自体を無効化しているユーザーの
    既存挙動を壊さないため。この場合は元々プロセス再起動をまたぐ再開機能も
    無いので後退にはならない）。

    data URL（base64）をテキストへ直接埋め込む方式は、メッセージサイズや
    Markdownパーサーとの相性で表示が壊れることが実機検証で分かった
    （表の直前に空行が無いと表ごと `<li>` に飲み込まれ `<img src="">` に
    なる等）ため採用していない。

    `http(s)://`・`data:`・`/`（Chainlitの `/public` 配下等）で始まる href、
    相対パス、実在しないパス、画像以外の拡張子はいずれもそのまま残す
    （＝該当箇所だけ画像が表示されない自然な失敗になる）。
    """
    matches = list(_MARKDOWN_IMAGE_RE.finditer(text))
    if not matches:
        return text

    session = cl.context.session
    thread_id = session.thread_id
    use_thread_store = _thread_store_conn is not None and thread_id
    if use_thread_store:
        # thread_id/owner はこの関数の呼び出し中不変なため、ループの外で1回だけ
        # 呼ぶ（画像N枚を含む回答でN回の冗長なUPSERTを避ける）。
        owner = thread_store.resolve_owner(session.user)
        await thread_store.upsert_thread_stub(_thread_store_conn, thread_id, owner)
    replacements: dict[str, str] = {}
    for m in matches:
        raw = m.group(0)
        if raw in replacements:
            continue
        alt, href = m.group(1), m.group(2).strip()
        if not href or href.startswith(("http://", "https://", "data:", "/")):
            continue
        path = Path(href)
        if not path.is_absolute() or not path.is_file() or not is_image_file(path):
            continue
        try:
            data, mime = load_image_bytes(
                path,
                max_long_side=_config.image_inline_preview_max_long_side_pixels,
                jpeg_quality=_config.image_inline_preview_jpeg_quality,
                min_long_side=_config.image_inline_preview_min_long_side_pixels,
            )
            if use_thread_store:
                element_id = str(uuid.uuid4())
                await thread_store.persist_element_file(
                    _thread_store_conn,
                    _config.elements_dir,
                    thread_id=thread_id,
                    element_id=element_id,
                    name=path.name,
                    content=data,
                    mime=mime,
                    for_id=None,
                    kind="inline",
                    extra_data={"id": element_id, "name": path.name, "mime": mime},
                )
                url = f"/locohane/elements/{element_id}"
            else:
                file_ref = await session.persist_file(name=path.name, mime=mime, content=data)
                url = f"/project/file/{file_ref['id']}?session_id={session.id}"
        except Exception:
            logging.getLogger(__name__).exception("Failed to persist the image: %s", path)
            continue
        replacements[raw] = f"![{alt}]({url})"

    if not replacements:
        return text
    return _MARKDOWN_IMAGE_RE.sub(lambda m: replacements.get(m.group(0), m.group(0)), text)


async def _send_answer(answer: cl.Message) -> None:
    """本回答メッセージを確定送信する（送信直前にローカル画像埋め込みを解決）。

    LLMが回答テキスト中に `![alt](絶対パス)` の形でローカル画像を直接
    参照している場合、ストリーミング中は生パスのまま流れる（ブラウザは
    読めず一瞬壊れた画像アイコンになり得る）が、この確定送信の瞬間に
    表示可能なURLへ変換してからUIへ渡すことで最終表示を正しくする
    （_embed_local_images_as_session_urls 参照）。あわせて、表の直前に
    空行が無いために表自体が描画されない問題（_ensure_blank_line_before_tables
    参照）も正規化する。
    """
    answer.content = _ensure_blank_line_before_tables(answer.content)
    answer.content = await _embed_local_images_as_session_urls(answer.content)
    await answer.send()


def _resolve_parent_id(event: dict, steps: dict[str, cl.Step]) -> str | None:
    """astream_events の event から、UI Step の親として使う parent_id を決める。

    dispatch_agent は内部で独立したサブエージェント（run_subagent）を回すが、
    LangChain の astream_events は contextvar 経由でその内部呼び出しの
    イベントも同じストリームへ伝播させる（当初の設計意図「内部は表示しない」
    とは異なる実装挙動）。何も対策しないと、サブエージェント内部のツール・
    思考Stepが親の dispatch_agent Step と見分けの付かない兄弟として並び、
    「サブエージェントが実行中のまま止まって見えるのに下に完了済みStepが
    並ぶ」というUI上の混乱を招く。

    event["parent_ids"]（LangChain が管理する実際の呼び出し階層。dispatch_agent
    のツール実行がRunnable Bを呼べば、B のイベントの parent_ids に
    dispatch_agent 側の run_id が含まれる）を見て、現在 steps 辞書に
    残っている（＝まだ完了していない）祖先Stepがあれば、その Step の下へ
    ネストさせる。該当が無ければ従来通り cl.context.current_step を使う。
    """
    # parent_ids は「ルート→直近の親」の順に並ぶため、逆順に見て一番近い祖先を選ぶ
    # （dispatch_agent_batch のグループStepのように、開いているStepが入れ子になる
    # 場合に外側のStepへ吸い寄せられないようにするため）。
    for parent_run_id in reversed(event.get("parent_ids", [])):
        parent_step = steps.get(parent_run_id)
        if parent_step is not None:
            return parent_step.id
    parent = cl.context.current_step
    return parent.id if parent else None


def _stream_owner(event: dict, owner_run_ids: set[str]) -> str | None:
    """このイベントの出力元（思考Step・回答Messageを分けて持つ単位）を返す。

    サブエージェント（dispatch_agent の run_id、または dispatch_agent_batch の
    グループの run_id）由来なら一番近いその run_id、メインエージェント由来なら None。
    [subagent].max_parallel が2以上だと複数のサブエージェントが同時にトークンを
    流すため、思考Step・回答Messageを1つずつしか持たないと別々のサブエージェントの
    思考が同じStepへ交互に書き込まれ、片方のツール開始でもう片方の思考Stepまで
    閉じられてしまう。
    """
    for parent_run_id in reversed(event.get("parent_ids", [])):
        if parent_run_id in owner_run_ids:
            return parent_run_id
    return None


async def _get_or_open_thinking(
    event: dict,
    owner: str | None,
    thinkings: dict[str | None, cl.Step],
    steps: dict[str, cl.Step],
    resync_steps: dict[str, cl.Step],
) -> cl.Step:
    """指定した出力元の「思考中」Stepを返す（無ければ生成して送信する）。

    サブエージェント（owner が None 以外）の思考Stepは、思考を input、回答本文を
    output に流す（回答をチャット欄の Message ではなくこのStepの出力欄に出すため）。
    フロントが2欄を「思考」「出力」と表示できるよう metadata.subagent_output を立てる。
    """
    thinking = thinkings.get(owner)
    if thinking is not None:
        return thinking
    thinking = cl.Step(
        name="Thinking",
        type="llm",
        parent_id=_resolve_parent_id(event, steps),
        metadata={"subagent_output": True} if owner is not None else None,
    )
    thinking.start = utc_now()
    await thinking.send()
    thinkings[owner] = thinking
    if owner is None:
        cl.user_session.set("live_thinking", thinking)
    else:
        # live_thinking はメインの1件だけを追跡するため、サブエージェントの
        # 思考Stepは再接続時の再送対象（resync_steps）へ入れる。
        resync_steps[f"thinking:{thinking.id}"] = thinking
    logging.getLogger(__name__).info(
        "thinking step created: step_id=%s parent_id=%s [%s]",
        thinking.id,
        thinking.parent_id,
        describe_current_task(),
    )
    return thinking


async def _close_owner_output(
    owner: str | None,
    thinkings: dict[str | None, cl.Step],
    answers: dict[str | None, cl.Message],
    *,
    stopped_reason: str | None = None,
) -> None:
    """指定した出力元の思考Stepを確定させ、回答Messageを確定送信する（他の出力元には触れない）。"""
    await _close_thinking(thinkings.pop(owner, None), stopped_reason=stopped_reason)
    answer = answers.pop(owner, None)
    if answer is not None:
        await _send_answer(answer)


async def _close_all_outputs(
    thinkings: dict[str | None, cl.Step],
    answers: dict[str | None, cl.Message],
    *,
    stopped_reason: str | None = None,
) -> None:
    """全出力元の思考Step・回答Messageを確定させる（ターンの中断・終了時用）。"""
    for owner in list({*thinkings, *answers}):
        await _close_owner_output(owner, thinkings, answers, stopped_reason=stopped_reason)


# dispatch_agent_batch が各グループのジョブを包む Runnable の run_name。
# この名前の on_chain_start/on_chain_end で、グループごとの中間Stepを作る。
# 表示名は metadata の "batch_group_label" を使う。
_BATCH_GROUP_RUN_NAME = BATCH_GROUP_RUN_NAME


def _is_subagent_call(event: dict, dispatch_agent_run_ids: set[str]) -> bool:
    """この astream_events イベントが dispatch_agent 内部（サブエージェント）由来かを判定する。

    event["parent_ids"]（LangChain が管理する祖先run_idの全チェーン）に、
    これまで観測した dispatch_agent の run_id が1つでも含まれていれば、
    その実行中に発生したイベント＝サブエージェント内部由来と判定できる
    （LLM呼び出しを内部で行うツールは dispatch_agent のみ）。

    以前は steps 辞書（_resolve_parent_id と同じ「現在開いているツール
    Step」）で判定していたが、それだと [subagent].background_inline_wait_max_seconds
    超過時に取りこぼす実害があった: dispatch_agent 自身のツール呼び出しは
    安全上限超過で早期リターンし on_tool_end が発火して steps から
    自身の run_id が消えるが、asyncio.shield で温存されたバックグラウンド
    ジョブ（_dispatch_agent_job.py の job.runner_task）は asyncio.create_task
    生成時点でコピーされた contextvar 経由でその後も astream_events へ
    内部LLM呼び出しのイベントを流し続ける（src/subagent.py 冒頭のコメント
    参照）。steps だけで判定すると、この早期リターン後にターン内で届く
    内部イベントが「メインエージェント由来」に誤分類され、on_chat_model_end
    ハンドラの last_usage/cumulative_main がサブエージェント内部の巨大な
    トークン数で汚染され、本来不要なメインエージェントの会話圧縮を誤って
    引き起こす（2026-08-26調査）。dispatch_agent_run_ids は steps と異なり
    on_tool_end で pop せずターン終了まで保持することで、早期リターン後の
    内部イベントも引き続きサブエージェント由来と判定できるようにする。
    """
    return any(pid in dispatch_agent_run_ids for pid in event.get("parent_ids", []))


def _is_dispatch_agent_error(tool_name: str, content: object) -> bool:
    """dispatch_agent が失敗時に返す「エラー: ...」文字列かどうかを判定する。

    src/tools.py の dispatch_agent() は失敗時も例外を投げず、この形式の
    文字列を正常な戻り値として返す。on_tool_end はそのままだと常に正常
    終了として扱ってしまうため、ここで検知して呼び出し元が is_error を
    立てられるようにする。
    """
    return tool_name in _SUBAGENT_DISPATCH_TOOLS and isinstance(content, str) and content.startswith("Error:")


def _is_dispatch_agent_truncated(tool_name: str, content: object) -> bool:
    """dispatch_agent がサブエージェント内部の打ち切り（max_iterations到達・
    空応答連続・トークン閾値超過・LLMタイムアウト）で返した文字列かどうかを判定する。

    src/subagent.py の run_subagent() はこれらの打ち切りを例外にせず、
    is_truncated_result() で判定できるプレフィックス付き文字列を正常な
    戻り値として返す。_is_dispatch_agent_error と異なりエラーではない
    （収集済みの結果を伴う「未完了終了」）ため、is_error ではなく
    stopped_reason バッジで区別する。
    """
    return tool_name == "dispatch_agent" and is_truncated_result(content)


async def _close_thinking(thinking: cl.Step | None, *, stopped_reason: str | None = None) -> None:
    """thinking Step（<think>ブロックの可視化用）を確定させる。

    「思考中」Stepが「実行中」のまま固まる不具合の調査用に、閉じた事実を
    Step idとともにログする（issue参照）。次回同じ現象が起きた際、該当
    Step idがここに出ていなければPython側の閉じ忘れ、出ていればWebSocket
    配信側の問題と切り分けられる。呼び出し元は戻り値を thinking へ代入する
    （`thinking = await _close_thinking(thinking)`）。

    cl.user_session["live_thinking"] はここでNoneへ戻さない（あえて）。
    このupdate()呼び出し自体がWebSocket切断中に発生し、確定通知がフロントへ
    届かないまま消えるケースが実測で確認されている（生成→切断→切断中に
    確定→再接続、という順序で完了通知だけが失われる）。Noneへ戻すと
    _resync_live_steps() が次の再接続時にこのStepを再送できなくなるため、
    次に新しい thinking Step が生成されて上書きされるまではそのまま
    参照を残しておく（再送は冪等なので、既に届いている場合も無害）。
    """
    if thinking is None:
        return None
    if stopped_reason is not None:
        thinking.metadata = {**(thinking.metadata or {}), "stopped_reason": stopped_reason}
    thinking.end = utc_now()
    await thinking.update()
    logging.getLogger(__name__).info(
        "thinking step finalized: step_id=%s stopped_reason=%s [%s]",
        thinking.id,
        stopped_reason,
        describe_current_task(),
    )
    return None


async def _finalize_orphaned_steps(steps: dict[str, cl.Step], reason: str) -> None:
    """ThinkingLoopDetected/GraphRecursionError で打ち切られた際、on_tool_end が
    届かないまま steps に残っている進行中Step（dispatch_agent等）を確定させる。

    リトライ時は steps がループ先頭で新しい空dictに差し替わるため、ここで
    finalize しないと該当Stepがフロントエンド上で「実行中」のまま孤立し続ける
    （UI側は step.end が来るまで恒久的に「実行中」表示のため）。thinking の
    ループ検知時と同じ metadata.stopped_reason を使い、「停止」バッジで
    正常完了と区別できるようにする。
    """
    for step in steps.values():
        step.metadata = {"stopped_reason": reason}
        step.end = utc_now()
        await step.update()
    steps.clear()


# 安全上限で dispatch_agent_batch が先に返った後も裏で動き続けるグループStepの出力欄の文言。
_BATCH_GROUP_BACKGROUND_OUTPUT = (
    "This group keeps running in the background because the safety limit was reached (its result is collected with check_dispatch_agent_job)."
)


async def _mark_background_group_steps(group_run_ids, steps: dict[str, cl.Step]) -> None:
    """dispatch_agent_batch の終了時点でまだ終わっていないグループStepを「バックグラウンド継続」で確定させる。

    安全上限（[subagent].background_inline_wait_max_seconds）超過でツールが先に返ると、
    裏で続くグループの on_chain_end はこのターンのイベントストリームへ届かない。
    steps に残したままだとターン終了時の _finalize_orphaned_steps で「停止」と
    表示され、実際には動いているのに止まったように見えるため、ここで
    metadata.background を付けて閉じる（フロントは「バックグラウンド」バッジを出す）。
    """
    for run_id in group_run_ids:
        step = steps.pop(run_id, None)
        if step is None:
            continue
        step.metadata = {"background": True}
        step.output = i18n.t("step.batch_background")
        step.end = utc_now()
        await step.update()


async def _aclose_event_stream(event_stream) -> bool:
    """astream_events() の非同期ジェネレータを、5秒のタイムアウト付きで閉じる。

    async for が例外で中断された場合、明示的に aclose() しないと LangGraph
    内部のバックグラウンドタスクや checkpointer のロックが残留しうる
    （GC任せだと即座に aclose() される保証がない）。既にクローズ済みの
    event_stream に対して呼んでも安全（PEP 525の仕様通り無害な no-op）。

    ChatLlamaCpp._astream（src/llm.py）と同じ設計方針で asyncio.timeout() を使う:
    asyncio.wait_for() は渡されたコルーチンを ensure_future で別タスクに
    ラップするため、asyncio.CancelScope の「開いたのと同じタスクで閉じる」
    制約を破りうる（2026-07-28 incident の直接原因）。asyncio.timeout() は
    現在のタスクに直接タイムアウトを注入し新規タスクを生成しないため、
    同一タスク制約を破らずにタイムアウトを維持できる。

    Returns:
        クローズに失敗した（タイムアウトまたは例外）場合 True。
    """
    try:
        async with asyncio.timeout(5.0):
            await event_stream.aclose()
        return False
    except TimeoutError:
        logging.getLogger(__name__).warning(
            "Closing astream_events (aclose) timed out after 5 seconds",
            exc_info=True,
        )
        return True
    except Exception:
        logging.getLogger(__name__).debug("Exception while closing astream_events", exc_info=True)
        return True


class _PlanDeniedInterrupt(Exception):
    """approve_plan が明示的に却下された直後、このターンを打ち切るための内部例外。

    ToolNode が生成した ToolMessage はこの時点ではまだチェックポイントへ
    コミットされていない。async for ループの外側（except節、finallyの
    aclose()より前）まで運んでから graph.aupdate_state() で明示コミットする
    ことで、次回のグラフ実行時に「tool_calls に対応する ToolMessage が無い」
    という不整合（_validate_chat_history エラー）を防ぐ。
    """

    def __init__(self, tool_message: ToolMessage) -> None:
        self.tool_message = tool_message


class _CompactionCheckpoint(Exception):
    """ループ内の安全な区切りでコンテキスト圧縮の条件を満たしたことを、
    async for ループの外側まで伝えるための内部例外。

    「安全な区切り」とは、直近のメインエージェントのAIMessageが発行した
    tool_calls に対応する ToolMessage が on_tool_end で全て返却済みになった
    時点を指す（サブエージェント内部の呼び出しは対象外。_is_subagent_call
    参照）。_PlanDeniedInterrupt と同じ理由（ToolNode が生成した
    ToolMessage群はこの時点ではまだチェックポイントへコミットされていない）
    で、まず aupdate_state(as_node="tools") で明示コミットしてから圧縮を行う。
    """

    def __init__(self, tool_messages: list[ToolMessage]) -> None:
        self.tool_messages = tool_messages


def _find_orphaned_tool_calls(messages: list) -> list[dict]:
    """全AIMessageのtool_callsのうち、対応するToolMessageがまだ無いものを返す。

    停止ボタン等によるCancelledErrorでターンが中断された場合、チェックポイントに
    コミット済みのAIMessage(tool_calls)に対し、ToolNodeが生成するはずだった
    ToolMessageが記録されないまま終わることがある（_PlanDeniedInterruptの
    docstring参照）。次回グラフ実行前にこの孤立を検出するために使う。

    以前は末尾のAIMessageだけを見ていたが、孤立tool_callの発生後に
    loop_nudge等の後続メッセージが追記される・コンテキスト圧縮で圧縮後の
    保持ウィンドウの途中に残る、といった経路で孤立tool_callが末尾ではなく
    なるケースがあり検出漏れになっていた（issue/20260804_234928_
    orphaned_tool_call_dual_session_freeze.md の再発）。langgraphの
    _validate_chat_history と同じく、履歴全体を対象に判定する。
    """
    answered_ids = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    orphaned: list[dict] = []
    seen_ids: set[str] = set()
    for m in messages:
        tool_calls = getattr(m, "tool_calls", None)
        if not tool_calls:
            continue
        for tc in tool_calls:
            tc_id = tc["id"]
            if tc_id in answered_ids or tc_id in seen_ids:
                continue
            seen_ids.add(tc_id)
            orphaned.append(tc)
    return orphaned


def _pending_completed_tool_messages(state) -> list[ToolMessage]:
    """中断した step で完了済みだったツールの ToolMessage（まだチェックポイントにコミットされていないもの）。

    ToolNode は tool_call ごとに別タスクとして動き、完了したタスクの結果は step 全体が
    終わるまで「pending write」としてだけ保存される。aget_state() はこれを values へ
    適用して返すため、_find_orphaned_tool_calls からは回答済みに見えるが、aupdate_state()
    で作る新しいチェックポイントには引き継がれない。停止ボタンで step が中断されると、
    完了済みだったサブエージェントの結果が LLM の履歴から消え（画面には表示済み）、
    次のターンで孤立 tool_call として「ユーザーの停止で中断」の仮結果に置き換わっていた
    （本番ログ 2026-11-03: 同じ応答で並べた explore 2本のうち、完了済みだった1本の結果が
    失われ、モデルが画面と食い違う説明をした）。修復時にこれらを書き戻す。
    """
    found: list[ToolMessage] = []
    for task in getattr(state, "tasks", None) or ():
        if getattr(task, "error", None) is not None:
            continue
        result = getattr(task, "result", None)
        if not isinstance(result, dict):
            continue
        msgs = result.get("messages")
        if msgs is None:
            continue
        for m in msgs if isinstance(msgs, list) else [msgs]:
            if isinstance(m, ToolMessage):
                found.append(m)
    return found


def _orphan_repair_messages(state, base_reason: str) -> tuple[list, int, int]:
    """孤立 tool_call を修復するために aupdate_state へ渡すメッセージ列と、仮結果で埋めた件数・書き戻した件数。

    完了済みだった（pending write にだけある）ToolMessage は本物をそのまま書き戻し、
    本当に結果の無い tool_call だけをプレースホルダで埋める（_pending_completed_tool_messages 参照）。
    """
    messages = state.values.get("messages", []) if state else []
    recovered = _pending_completed_tool_messages(state)
    orphaned = _find_orphaned_tool_calls(messages)
    repair = [*recovered, *(msg for tc in orphaned for msg in _build_orphaned_placeholder_messages(tc, base_reason))]
    return repair, len(orphaned), len(recovered)


def _dispatch_agent_rescue_note_paths(tc: dict) -> list[Path]:
    """孤立した dispatch_agent / dispatch_agent_batch の tool_call に対応する、実在する緊急退避ファイルの一覧。

    パス解決に失敗した場合（_dispatch_agent_rescue_note_hint の docstring 参照）は空リスト。
    """
    try:
        from src.tools.write_scratch_note import _scratch_notes_path_for_run, sanitize_run_id

        run_id = sanitize_run_id(tc["id"])
        if tc.get("name") != "dispatch_agent_batch":
            candidate = _scratch_notes_path_for_run(run_id)
            return [candidate] if candidate.is_file() else []
        # sanitize_run_id 後の run_id は英数字・"_"・"-" だけなので glob の特殊文字を含まない。
        notes_dir = _scratch_notes_path_for_run(run_id).parent
        prefix = f"_scratch_notes_{run_id}_g"
        found = [p for p in notes_dir.glob(f"{prefix}*.md") if p.is_file() and p.stem[len(prefix) :].isdigit()]
        return sorted(found, key=lambda p: int(p.stem[len(prefix) :]))
    except Exception:  # noqa: BLE001 - パス解決に失敗しても案内文の生成自体は諦めない
        return []


def _dispatch_agent_rescue_note_hint(tc: dict) -> str:
    """強制停止されたdispatch_agentの孤立tool_callに添える、緊急退避内容の案内文を組み立てる。

    dispatch_agent は自身の tool_call_id を run_id として使う
    （sanitize_run_id 経由、src/tools/dispatch_agent.py 参照）。孤立した
    tc["id"] に同じ変換を適用すれば、退避先ファイル
    （_scratch_notes_path_for_run）の絶対パスをここで直接特定できる。
    以前は「作業ディレクトリ配下」としか案内できず、ユーザーがツールバーで
    カスタム作業ディレクトリを設定しているとexploreが実際の退避先
    （_tmp_<thread_id>配下）を見つけられない問題があった。

    _scratch_notes_path_for_run() は内部で cl.user_session から thread_id を
    読むため、Chainlitセッション文脈が解決できない状況（cross-session停止で
    タスク生成元のタブが既に閉じられている場合等）では例外を送出しうる。
    その場合、またはファイルが実際に存在しない場合は「退避を確認できな
    かった」ことを明示し、通常運用へのフォールバックを促す。

    dispatch_agent_batch はグループごとに `<tool_call_id>_g<N>` を run_id とする
    （src/tools/dispatch_agent_batch.py 参照）ため、実行中だったグループの数だけ
    退避ファイルがありうる。_dispatch_agent_rescue_note_paths で全件を拾う。
    退避ファイルは実行中だったグループの分しか無いため、完了済みグループの結果と
    未完了グループの担当ファイルを保存した thread note があれば、その topic も先頭に載せる。
    """
    hint = _dispatch_agent_rescue_files_hint(_dispatch_agent_rescue_note_paths(tc))
    note_line = _batch_interrupted_note_line(tc)
    return f"{note_line}\n{hint}" if note_line else hint


def _batch_interrupted_note_line(tc: dict) -> str:
    """dispatch_agent_batch の中断時に保存した thread note があれば、その案内文を返す（無ければ空文字列）。"""
    if tc.get("name") != "dispatch_agent_batch":
        return ""
    try:
        from src.tools.thread_notes import _parse_thread_notes, _thread_notes_path

        topic = interrupted_note_topic(tc["id"])
        path = _thread_notes_path()
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(b.topic == topic for b in _parse_thread_notes(text)):
            return ""
    except Exception:  # noqa: BLE001 - 確認できなければ案内しないだけ
        return ""
    return (
        f'The state of each group at the interruption (results of completed groups and the files assigned to unfinished groups) has been saved to thread note "{topic}". '
        "When resuming, first read it with read_thread_note, do not redo completed groups, "
        "and re-delegate only the unfinished groups."
    )


def _dispatch_agent_rescue_files_hint(paths: list[Path]) -> str:
    """緊急退避ファイルの一覧から案内文を組み立てる（_dispatch_agent_rescue_note_hint 参照）。"""
    if len(paths) == 1:
        return (
            "The conversation of this sub-agent delegation up to the interruption has been "
            f"saved for recovery to the following file: {paths[0]}\n"
            "When you resume this task, first delegate to the explore sub-agent to "
            "understand this file, record the key points with write_thread_note, and then "
            "continue the work."
        )
    if paths:
        listing = "\n".join(f"- {p}" for p in paths)
        return (
            "For this batch delegation, the conversations of the sub-agents of each group running at the interruption "
            f"have been saved for recovery to the following files:\n{listing}\n"
            "When you resume this task, first delegate to the explore sub-agent to "
            "understand these files, record the key points with write_thread_note, and then "
            "continue the work."
        )
    return (
        "An attempt was made to save the conversation of this sub-agent delegation up to the interruption "
        "for recovery, but the saved file could not be confirmed (it may have stopped "
        "before saving). In that case, follow the user's instructions as usual."
    )


def _dispatch_agent_rescue_nudge_text(hint: str, tool_name: str = "dispatch_agent") -> str:
    """孤立したdispatch_agent tool_callの直後に追加する、念押しのHumanMessage文言。

    ToolMessage単体（「エラー: ...」で始まる失敗通知の体裁）では、低
    パラメータモデルが「このアプローチは失敗した、別の方法で最初から
    やり直そう」と解釈し、後半の具体的な指示（scratch_note確認・
    write_thread_note記録）を実行に移さない事象が実運用で確認された
    （2026-09-09: dispatch_agent(explore)によるファイル読取り委譲が
    停止ボタンで中断→ユーザーの「続けてください」の直後、案内に一切
    触れず無関係な大規模調査を一から組み立て直した）。ToolMessageと
    同じ情報をHumanMessage（ユーザー発話に近い形式）としても積むことで、
    直近の文脈として拾われやすくする。

    ただし、この直後に続くユーザー自身の実際の指示が明確に別方向・
    中断を求めるものであれば、そちらを優先すべきなのでその旨も明記する
    （retryではなく1回きりの案内であり、ユーザーの意思を上書きしては
    ならないため）。
    """
    return (
        f"[System notice] The previous delegation to {tool_name} was interrupted. {hint} "
        "However, if the next instruction clearly asks to change to different work or to stop, "
        "follow that instead."
    )


def _build_orphaned_placeholder_messages(tc: dict, base_reason: str) -> list:
    """孤立tool_callを埋めるプレースホルダのメッセージ列を組み立てる。

    _repair_orphaned_tool_calls（セッション復旧時）・on_message の
    except asyncio.CancelledError（停止ボタン等による中断時）の両方から
    使う共通処理。tool_callがdispatch_agent・dispatch_agent_batchの場合のみ、強制停止時に
    run_subagent が緊急退避した会話履歴（_dispatch_agent_job.py の
    _make_rescue_on_cancelled 参照）をどう扱うべきかの案内を、通常の
    ToolMessageに加えてHumanMessage（念押し、_dispatch_agent_rescue_nudge_text
    参照）としても積む。langgraphのadd_messages reducerは渡した順序の
    ままチェックポイントへ積むため、ToolMessage→HumanMessageの順で返す。

    Args:
        tc: 孤立したtool_call（"id"/"name"を含む辞書）。
        base_reason: 中断理由の文言（例: "ユーザーの停止操作等により、"）。
            文末に「このツール呼び出しの実行が中断されました。」が続く
            前提で渡すこと。

    Returns:
        補完用のメッセージ列（dispatch_agent・dispatch_agent_batch以外はToolMessage1件のみ）。
    """
    content = f"Error: {base_reason}the execution of this tool call was interrupted."
    if tc.get("name") in _ASK_USER_TOOLS:
        # 質問への応答を受け取る前に止まった。ユーザーは画面に残った質問を見て、次の発言で答えることが多い
        # （本番ログ 2026-11-11: 再起動後に選択肢の文言をそのまま送った）。
        content = (
            f"{content} The user's answer to this question was not received. "
            "If the user's next message answers this question (for example, it repeats one of the choices), "
            "take it as the answer and continue; ask again only if the answer is still needed and not given."
        )
    if tc.get("name") not in _SUBAGENT_DISPATCH_TOOLS:
        return [ToolMessage(content=content, tool_call_id=tc["id"], name=tc.get("name", ""))]
    hint = _dispatch_agent_rescue_note_hint(tc)
    content = f"{content}\n\n[{hint}]"
    return [
        ToolMessage(content=content, tool_call_id=tc["id"], name=tc.get("name", "")),
        HumanMessage(content=_dispatch_agent_rescue_nudge_text(hint, tc["name"])),
    ]


async def _repair_orphaned_tool_calls(graph, config: dict) -> int:
    """チェックポイント末尾に孤立tool_callがあれば、プレースホルダのToolMessageを
    補完コミットして修復する。修復した件数を返す（0なら修復不要だった）。

    _rebuild_graph() 自体はグラフ・接続オブジェクトの再構築のみで永続化済みの
    チェックポイント内容は変更しないため、孤立tool_callが一度コミットされると
    再構築だけでは直らず次回ターンも同じValueErrorを再現し続ける
    （issue.md ISSUE-003 / issue/20260804_234928_orphaned_tool_call_dual_session_freeze.md
    参照）。復旧経路の最後に必ずこれを呼び、次回ターンが同じ場所で失敗し続けない
    ようにする。
    """
    try:
        state = await graph.aget_state(config)
    except _CheckpointerTimeout:
        logging.getLogger(__name__).debug(
            "Checkpointer operation timed out while repairing orphaned tool_calls",
            exc_info=True,
        )
        return 0
    repair, orphaned_count, _ = _orphan_repair_messages(state, "Because the previous session ended abnormally, ")
    if not repair:
        return 0
    await graph.aupdate_state(config, {"messages": repair}, as_node="tools")
    return orphaned_count


async def _remove_message_ids_if_present(graph, config: dict, ids: list[str]) -> None:
    """指定した message id のうち、現在のグラフ状態に実在するものだけを RemoveMessage で取り除く。

    loop_nudge_ids / empty_nudge_ids は ThinkingLoopDetected・無言終了リトライの
    たびに機械的な HumanMessage の id を蓄積するが、蓄積後に
    _run_context_compaction が発火すると、圧縮対象は要約に置き換わり、保持された
    直近メッセージも新しい uuid.uuid4() を振った複製へ置き換わる
    （context_compaction.py 参照）。この場合、蓄積済みのidはどちらも現在のstate
    にもう存在しない「幽霊id」になる。存在しないidを RemoveMessage(id=...) で
    渡すと langgraph の add_messages リデューサが ValueError を送出し、
    on_message のこの経路はどの except 節にも保護されていないため、そのまま
    Chainlitの外側まで伝播して未処理エラーになっていた
    （本番ログ: app.py:2103 のトレースバック）。ここで現在のstateに実在する
    idだけへ絞り込み、安全に無視できるようにする。
    """
    if not ids:
        return
    try:
        state = await graph.aget_state(config)
    except _CheckpointerTimeout:
        logging.getLogger(__name__).debug(
            "Checkpointer operation timed out while removing nudge messages",
            exc_info=True,
        )
        return
    live_ids = {m.id for m in (state.values.get("messages", []) if state else [])}
    present = [i for i in ids if i in live_ids]
    if present:
        await graph.aupdate_state(config, {"messages": [RemoveMessage(id=i) for i in present]})


def _messages_summary(messages: list) -> str:
    """メッセージリストの概要を文字列化する（role + content冒頭120文字）。"""
    parts = []
    for i, m in enumerate(messages):
        role = type(m).__name__
        content = getattr(m, "content", "")
        if isinstance(content, str):
            preview = content[:120].replace("\n", " ").replace("\r", "")
        else:
            preview = str(content)[:120]
        parts.append(f"  [{i}] {role}: {preview}")
    return "\n".join(parts)


async def _run_context_compaction(
    graph,
    config: dict,
    thread_id: str,
    last_usage: dict | None,
    *,
    dry_run: bool = False,
) -> bool:
    """コンテキスト圧縮（src/context_compaction.py）の判定・実行を行う。

    「安全な区切り」（進行中のグラフ実行と aupdate_state が競合しない
    タイミング）でのみ呼ばれる想定。on_message から、ターン完了後と
    ループ内の安全点（_CompactionCheckpoint）の両方から呼ぶ共通ヘルパー。
    会話ログ追記はターン完了時専用の処理のため、ここには含めない。
    dry_run引数の使い方は _run_context_compaction_visible 参照。

    Args:
        dry_run: True の場合、判定（awaiting_approve_plan_call/
            orphaned_tool_calls/should_compact/is_compaction_blocked_by_
            missing_note）だけを行い、実際の要約LLM呼び出し・aupdate_state・
            各種カウンタのリセットは行わない。_run_context_compaction_visible
            が「圧縮が実際に発火するか」を正確に予測するために使う
            （4つのゲートを個別に手で複製すると、ゲートが増減した際に
            片方だけ更新し忘れて予測がずれる恐れがあるため、判定ロジック
            自体をここへ一本化する）。context_compaction_note_skip_count は
            読むだけで更新しない（実際に圧縮を試みる非dry_run呼び出し側でのみ
            更新し、二重カウントを避ける）。

    Returns:
        圧縮を実行した場合（dry_run=True では「実行するはず」と判定した
        場合）は True。
    """
    if cl.user_session.get("awaiting_approve_plan_call"):
        # create_plan直後、approve_plan/lock_plan_modeが呼ばれるまでの承認待ち中。
        # ここで圧縮（要約用LLM呼び出し）が割り込むと、失敗・長時間化した際に
        # thinking/answerが既に確定送信済みでユーザーには何も表示されず、
        # 承認ボタンも出ない見かけ上のハングになるため、承認確定まで圧縮しない。
        return False
    try:
        state = await graph.aget_state(config)
    except Exception:
        # checkpointer操作の失敗（DB接続切れ・ロック固着等）。呼び出し元の
        # 2箇所（_CompactionCheckpointハンドラの中／ターン完了後の最終防衛
        # ライン）はどちらも turn_broken_exc 経由の自己修復ルート
        # （_rebuild_checkpointer等）を通らない構造のため、ここで例外を
        # 伝播させると自己修復が一切走らないまま壊れたcheckpointerが
        # 居座り続ける（プロセス再起動でしか直らない）。圧縮を今回だけ
        # スキップし、次の通常ターンが同じ操作で同じ例外に遭遇した際に
        # on_message側の既存の except _CheckpointerTimeout /
        # except LLM_CONNECTION_ERRORS 経路で自己修復させる。
        logging.getLogger(__name__).exception("Context compaction: getting state (aget_state) failed, skipping this time")
        return False
    messages = state.values.get("messages", []) if state else []
    if _find_orphaned_tool_calls(messages):
        # 未解決のtool_callが残っている間は圧縮しない
        # （安全のための防御線。通常は_CompactionCheckpointのpending_main_tool_idsで
        # 既にガードされるが、ターン完了直後の最終呼び出し[app.py]はこのチェックが
        # 無かった）。
        return False
    cumulative_main = cl.user_session.get("token_usage_cumulative_main")
    if not should_compact(cumulative_main, last_usage, len(messages), _config):
        return False
    skip_count = cl.user_session.get("context_compaction_note_skip_count") or 0
    summary_model = None
    if is_compaction_blocked_by_missing_note(messages, _config, skip_count):
        if dry_run:
            # 見送るのではなく、これから強制的にwrite_thread_noteを書かせてから
            # 圧縮する予定のため、「圧縮するはず」自体は変わらない。
            return True
        summary_model = await build_model(_config, role="main")
        forced = await force_write_thread_note(messages, summary_model, _config)
        if forced is None:
            # 強制実行自体が失敗した場合のみ、従来通り見送ってターンを継続する
            # （安全弁。無限に諦めないと require_note_max_skips の意味が無くなる）。
            cl.user_session.set("context_compaction_note_skip_count", skip_count + 1)
            logging.getLogger(__name__).debug(
                "Context compaction: forced write_thread_note failed, postponing (skip_count=%d)",
                skip_count + 1,
            )
            return False
        ai_message, tool_messages = forced
        forced_messages = [ai_message, *tool_messages]
        try:
            await graph.aupdate_state(config, {"messages": forced_messages}, as_node="tools")
        except Exception:
            # 上のaget_state/aupdate_state同様の理由で握りつぶす。強制実行済みの
            # write_thread_noteの結果は次回以降の判定（_write_thread_note_called_recently）
            # で再度見えるとは限らないため、見送り扱いにしてskip_countを進める。
            logging.getLogger(__name__).exception(
                "Context compaction: updating state (aupdate_state) with the forced write_thread_note result failed, postponing this time"
            )
            cl.user_session.set("context_compaction_note_skip_count", skip_count + 1)
            return False
        # ここでのリセットは「圧縮完了を確認してからリセットする」という下の
        # 方針と矛盾しない。skip_count は write_thread_note が書かれるまでの
        # 猶予回数であり、強制実行が成功した＝事実の退避自体は済んでいる。
        # 以降で要約LLM呼び出しが失敗しても、失われる恐れのある事実は既に
        # thread note へ書き出されているため、猶予を積み直す意味が無い。
        cl.user_session.set("context_compaction_note_skip_count", 0)
        messages = messages + forced_messages
    if dry_run:
        return True
    if summary_model is None:
        summary_model = await build_model(_config, role="main")
    new_messages = await maybe_compact(messages, summary_model, _config, role="main")
    if new_messages is None:
        return False
    logging.getLogger(__name__).debug(
        "Context compaction: before messages=%d\n%s",
        len(messages),
        _messages_summary(messages),
    )
    logging.getLogger(__name__).debug(
        "Context compaction: after messages=%d\n%s",
        len(new_messages),
        _messages_summary(new_messages),
    )
    try:
        await graph.aupdate_state(
            config,
            {"messages": [RemoveMessage(id=m.id) for m in messages] + new_messages},
            as_node="tools",
        )
    except Exception:
        # 上のaget_state同様の理由で握りつぶす。要約結果は破棄して構わない
        # （次回のshould_compact判定でまた同じ範囲が圧縮候補になるだけ）。
        logging.getLogger(__name__).exception("Context compaction: updating state (aupdate_state) failed, skipping this time")
        return False
    # 圧縮により古い履歴が要約へ置き換わったため、次にまた同じ閾値で
    # 即座に発火し続けないよう、メインエージェントの累積トークン数をリセットする。
    cl.user_session.set("token_usage_cumulative_main", _new_usage_totals())
    # 保存しないと、直後に切断→再開した場合に圧縮前の値が復元され、
    # 再開直後にまた圧縮が発火してしまう。
    await _persist_token_usage()
    # write_thread_note見送りカウンタも、実際に圧縮が完了した時点でのみリセットする
    # （is_compaction_blocked_by_missing_noteがFalseを返した直後ではなく、maybe_compact/
    # aupdate_stateの成功を確認してから。ここより前でリセットすると、要約LLM呼び出しの
    # 失敗やcheckpointer更新の失敗時にも猶予が失われ、安全弁が骨抜きになる）。
    cl.user_session.set("context_compaction_note_skip_count", 0)
    # 要約後のモデルは要約に含まれなかった個々のツール呼び出しを覚えていない
    # ため、Read/Glob等の重複呼び出しガードの履歴も合わせてリセットする
    # （記憶が無いのにガードだけが残り、拒否され続けてループする問題を防ぐ）。
    reset_call_history_guards_after_compaction()
    logging.getLogger(__name__).warning(
        "context compacted thread_id=%s messages=%d->%d",
        thread_id,
        len(messages),
        len(new_messages),
    )
    return True


async def _run_context_compaction_visible(
    graph,
    config: dict,
    thread_id: str,
    last_usage: dict | None,
) -> bool:
    """_run_context_compaction をユーザーに見える形（system_messageのMessage表示）で実行するラッパー。

    要約LLM呼び出しは会話が長いほど時間がかかり（実測: 62件の要約で初回チャンク
    まで約97秒）、その間ターンが完了しないため、何も表示しないとユーザーには
    「タスクは終わったはずなのに思考中のまま止まっている」ように見える
    （2026-08-12、issue/20260812_124500_post_turn_compaction_97s_ux_delay.md
    参照）。当初はcl.Stepで表示していたが、ターン完了後（最終防衛ライン）に
    送るとチャット表示部に何も現れないとユーザーから報告があったため、
    ループ検知停止・通信エラー等の他の通知と同じ type="system_message" の
    cl.Message（チャット本文に確実に残る）へ切り替えた。should_compact() の
    判定自体は一瞬で終わり、実際に圧縮が発火するときだけ時間がかかるため、
    メッセージは「発火が決まった後」にのみ表示する（圧縮不要な大多数の
    ターンでは何も表示されない）。

    表示要否の判定は _run_context_compaction(dry_run=True) にそのまま委譲する
    （awaiting_approve_plan_call/orphaned_tool_calls/should_compact/
    is_compaction_blocked_by_missing_note の4ゲート全てを1箇所のロジックで
    判定させることで、以前ここに手で複製していた一部のゲートだけを見て
    「圧縮します」と表示した直後に本体側の別ゲートで却下される、という
    表示と実処理の食い違いを構造的に防ぐ）。
    """
    try:
        will_compact = await _run_context_compaction(graph, config, thread_id, last_usage, dry_run=True)
    except Exception:
        # dry_run=True でも aget_state 失敗等は _run_context_compaction 内部で
        # 捕捉されFalseが返る設計のため、ここに来るのは想定外の場合のみ。
        # 表示の要否だけ判定できなくても、圧縮の実処理自体は
        # _run_context_compaction 内で同じ失敗に対する自己修復（今回は
        # スキップして次ターンに委ねる）を既に持っているため、ここでは
        # 例外を伝播させずそちらに委譲する。
        logging.getLogger(__name__).exception("Context compaction: failed to decide whether to show the notice, falling back to the normal path")
        return await _run_context_compaction(graph, config, thread_id, last_usage)
    if not will_compact:
        return await _run_context_compaction(graph, config, thread_id, last_usage)

    await cl.Message(
        content=i18n.t("notice.compaction_start"),
        type="system_message",
    ).send()
    compacted = await _run_context_compaction(graph, config, thread_id, last_usage)
    await cl.Message(
        content=i18n.t("notice.compaction_done") if compacted else i18n.t("notice.compaction_skipped"),
        type="system_message",
    ).send()
    return compacted


def _should_retry_after_loop(
    loop_exc: ThinkingLoopDetected | None, loop_attempt: int, loop_max_retries: int
) -> bool:
    """ThinkingLoopDetected検知後にリトライすべきか判定する。

    呼び出し側はTrueを見てリトライ処理（グラフ再構築・nudge注入）をした直後、
    必ず`loop_exc = None`へリセットすること。リセットを怠ると、on_message内の
    while Trueループが次の周回で新たな検知が無いのにloop_exc is not Noneの
    ままとなり、正常完了したはずのターンまで誤ってリトライされ続ける
    （2026-08-14 状態リークバグの回帰防止。turn_broken_exc側はハンドラが
    必ずreturnするため同種のリークが起きず、リセット不要）。
    """
    return loop_exc is not None and loop_attempt < loop_max_retries


async def _on_message_impl(message: cl.Message) -> None:
    """Chainlit がユーザーからのメッセージを受け取るたびに呼ばれるフック。

    セッションの thread_id を使って LangGraph のグラフを astream_events
    で実行し、以下をリアルタイムに UI へ反映する:
    - on_chat_model_stream: モデルのトークンを最終回答へストリーム表示。
    - on_tool_start / on_tool_end: ツール呼び出し（read_skill/read_skill_file/
      run_script/view_image）を cl.Step として可視化し、入出力を表示する。

    アップロードファイルがあれば _save_uploads() で保存し、_build_human_message()
    で HumanMessage を組み立ててグラフへ渡す。画像も含めて保存先パスを
    本文に追記する（LLM が analyze_image・run_script 等にパスを渡せるように
    するため）。本文中に生のUNCパスが
    含まれる場合は register_raw_unc_paths_in_text() で path_memory へ
    事前登録し `@N` へ置換してからグラフへ渡す（ISSUE-002対策）。

    Args:
        message: ユーザーが送信した Chainlit のメッセージ
            （本文 content と添付ファイル elements を含む）。

    Returns:
        None。副作用として cl.Message / cl.Step の送信・更新を行う。
    """
    thread_id = cl.user_session.get("thread_id")
    # on_message が開いた追加指示の受付のトークン（await より前に取ること。on_message の同名コメント参照）。
    injection_token = user_injection.current_token(thread_id)
    too_long_reason = _check_input_length(message)
    if too_long_reason is not None:
        await cl.Message(content=too_long_reason, type="system_message").send()
        return
    # 計画承認は既定では「このユーザーメッセージへの応答で作られた計画の実行」に
    # 限定されたスコープであるべきなので、新しいメッセージを受け取るたびに
    # 前回（放置されて完了しなかった計画など）の承認状態を持ち越さない。
    # ただし [plan].reset_approval_on_new_message=false の場合は、
    # thinking_loop_guard のリトライ上限到達などで打ち切られたタスクを
    # ユーザーが新しいメッセージで継続するケースを想定し、承認済み状態を
    # メッセージをまたいで維持する（無関係な新規依頼でも維持される点は
    # 設定側のトレードオフとしてドキュメント済み）。
    if _config.plan_reset_approval_on_new_message:
        cl.user_session.set("plan_approved", False)
    # main_agent_tool_guard（src/tools.py の _guard_main_agent_tool_limit）の
    # カウンタも同様に、新しいターンでは前回の消費分を持ち越さない。
    cl.user_session.set("main_agent_tool_guard_call_count", None)
    # dispatch_agent 経由でこのタスクから派生するサブエージェントの
    # build_model() 呼び出しも、このセッションへ紐づけて登録されるようにする
    # （src/llm.py の set_current_session / _CURRENT_SESSION_ID 参照）。
    # tab_id=cl.context.session.id: 同じ thread_id を複数タブで同時に開いた
    # 場合の _LAST_SELECTED_INDEX 混線防止（_rebuild_graph 内の同種コメント参照）。
    set_current_session(thread_id, tab_id=cl.context.session.id)
    # ドラフトが作成・削除・description 変更された後は、スキル一覧と read_skill /
    # run_script の選択肢を更新するためグラフを組み直す（本文の変更は読み込み時に
    # 反映されるため不要）。skill-creator の実行時にフラグを立てる方式だと、
    # run_script_background の完了前に組み直してフラグを下ろしてしまい、新しい
    # ドラフトが出ないままになるため、毎回実際のドラフトの状態と比べる。
    current_drafts = skill_drafts.scan_visible_drafts(_config, cl.user_session.get(skill_drafts.SESSION_USER_KEY))
    # 画面で表示言語を切り替えた後も、応答言語の指示を差し替えるため組み直す
    # （set_language アクションは生成中にも届くため、ここで反映する）。
    drafts_changed = skill_drafts.drafts_signature(current_drafts) != cl.user_session.get("drafts_signature")
    # 入力欄ツールバーで選んだ reasoning_effort（Composer.tsx が送信時に metadata へ入れる。
    # 空・不正値は config.ini の値）。画面は生成中に変更させず、ここで送信時の値に確定させる
    # ため、ターンの途中（サブエージェントを含む）で切り替わることはない。
    requested_effort = (message.metadata or {}).get("reasoning_effort")
    cl.user_session.set(SESSION_REASONING_EFFORT_KEY, requested_effort if isinstance(requested_effort, str) else "")
    effort_changed = (
        with_reasoning_effort(_config, cl.user_session.get(SESSION_REASONING_EFFORT_KEY)).reasoning_effort
        != cl.user_session.get("graph_reasoning_effort")
    )
    if drafts_changed or effort_changed or i18n.current_lang() != cl.user_session.get("graph_lang"):
        await _rebuild_graph(thread_id, wait_when_busy=False)
    graph = cl.user_session.get("graph")
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": _config.graph_recursion_limit,
    }

    # アップロードがあれば保存し、保存先パスを本文に明示する（LLM が
    # analyze_image・run_script 等に渡せるように）。
    upload_user_obj = cl.user_session.get("user")
    upload_username = resolve_log_username(upload_user_obj.identifier if upload_user_obj else None)
    saved = _save_uploads(message)
    # log_level="info"（既定）は「概要のみ」の方針（config.py Config.log_level
    # docstring参照: ツール呼び出しの全引数・全結果・LLM応答本文はdebug限定）。
    # ユーザーの生入力にはパスワード・APIキー等が含まれうるため、本文を伴う
    # このログはdebugに留める（2026-09-04 レビュー指摘）。
    logging.getLogger(__name__).debug(
        "User message: user=%s content=%r",
        upload_username,
        message.content,
    )
    processed_text = register_raw_unc_paths_in_text(message.content)
    # スレッド開始時・作業ディレクトリ変更時（on_chat_start/_apply_work_dir が
    # 立てるフラグ）のみ、実際の絶対パスをLLMへ知らせる（詳細は
    # _build_work_dir_notice() docstring参照）。以降のターンは会話履歴に
    # 残ったこのブロックを参照させるため、毎ターン注入はしない。
    work_dir_notice: str | None = None
    if cl.user_session.get("work_dir_notice_pending"):
        cl.user_session.set("work_dir_notice_pending", False)
        work_dir_notice = _build_work_dir_notice()
    human_message = _build_human_message(processed_text, saved, work_dir_notice)

    # 前回のプロセスがツールの実行中（質問の応答待ち等）に止まると、チェックポイント末尾に結果の無い
    # tool_call が残る。そのまま新しい発言を足して流すと、langgraph が履歴の検証（INVALID_CHAT_HISTORY）で
    # 失敗し、この発言はエラーで終わっていた（修復はその失敗の後でしか走らず、2回目の送信でやっと動いた。
    # 本番ログ 2026-11-11: ask_user_choice の応答待ちでプロセスが止まり、再起動後の選択肢の回答がエラーに）。
    # 流す前に修復しておく。
    try:
        repaired = await _repair_orphaned_tool_calls(graph, config)
    except Exception:  # noqa: BLE001 - 修復できなくても発言の処理は続ける（失敗時の経路で再度修復する）
        logging.getLogger(__name__).warning("Pre-turn repair of orphaned tool_calls failed", exc_info=True)
    else:
        if repaired:
            logging.getLogger(__name__).warning("Repaired %d orphaned tool_calls before the turn", repaired)

    inputs = {"messages": [human_message]}
    turn_totals = {"input": 0, "output": 0, "total": 0}  # このターンのトークン使用量
    # 無言終了（tool_calls も回答テキストも無いまま終わる）を検知した場合、
    # 最終回答を促して自動的にリトライする（下のループ末尾を参照。
    # [thinking_loop_guard].empty_response_max_retries 由来）。
    max_empty_retries = _config.thinking_loop_guard_empty_response_max_retries
    # LLM応答の反復ループ（src/llm.py の ChatLlamaCpp が検知して
    # ThinkingLoopDetected を送出する）を検知した場合の再試行回数と、
    # 注入した注意メッセージ（機械的なもの）のid（成功後に履歴から取り除くため）。
    loop_max_retries = _config.thinking_loop_guard_max_retries
    loop_attempt = 0
    # ツール呼び出しのループ（ToolCallLoopDetected）で注意した回数。再試行の予算は
    # loop_attempt と合算だが、[tool_loop_guard].nudge_messages の段階はこちらで数える。
    tool_loop_attempt = 0
    loop_nudge_ids: list[str] = []
    # 無言終了リマインダーのid（成功後に履歴から取り除くため。thinking_loopの
    # nudgeと同様、機械的な注入を会話履歴に残さないようにする）。
    empty_nudge_ids: list[str] = []
    # 2つのリトライ要因（無言終了・反復ループ）で試行回数の予算を共有する。
    total_retries = max_empty_retries + loop_max_retries
    # 直近1回分のLLM呼び出しの usage_metadata。ターン終了後、コンテキスト
    # 圧縮（src/context_compaction.py）の単発リクエスト閾値判定に使う。
    last_usage: dict | None = None
    # このターンの途中で会話へ入ったユーザーの追加指示と、その画面表示（_TurnInjections 参照）。
    turn_injections = _TurnInjections(injection_token)

    loop_exc: ThinkingLoopDetected | None = None
    turn_broken_exc: Exception | None = None
    checkpointer_needs_rebuild: bool = False
    # LLMサーバーとの通信エラー（LLM_CONNECTION_ERRORS）を検知した場合の
    # 自動リトライ回数と上限（[graph].connection_error_max_retries）。
    # loop_exc/empty応答用の total_retries とは別予算で管理する（下の
    # except LLM_CONNECTION_ERRORS / if turn_broken_exc is not None 参照）。
    connection_retry_attempt = 0
    connection_error_max_retries = _config.graph_connection_error_max_retries
    # total_retries（無言終了・反復ループ用のエラーリトライ予算）とは別に、
    # ターン内コンテキスト圧縮（_CompactionCheckpoint）による継続を扱うため
    # for range(...) ではなく手動カウンタの while True にしている。圧縮継続は
    # attempt を増やさない（1ターンで圧縮が何度発火してもエラーリトライの
    # 残り予算に影響を与えないため）。既存の for range(total_retries + 1) と
    # 同じ意味を保つよう、リトライする経路では continue の直前に必ず
    # attempt += 1 する。
    attempt = 0
    while True:
        # 出力元（None=メインエージェント、それ以外=サブエージェントの run_id）ごとの
        # 表示中の回答Message・<think>ブロック（reasoning_content）を表示するStep
        # （_stream_owner 参照）。回答はツール呼び出しごとに区切って新規発行する。
        answers: dict[str | None, cl.Message] = {}
        thinkings: dict[str | None, cl.Step] = {}
        # 出力元として扱う run_id（dispatch_agent 系ツールと、dispatch_agent_batch の
        # グループ）。on_tool_end/on_chain_end でも取り除かない（ターン内で後から
        # 届く内部イベントの出力元判定に使うため。dispatch_agent_run_ids と同じ理由）。
        owner_run_ids: set[str] = set()
        # 出力元 run_id → その配下のグループ run_id（dispatch_agent_batch 終了時に
        # グループ側の出力もまとめて確定させるため）。
        owner_children: dict[str, set[str]] = {}
        # 出力元 run_id → 表示名（トークン使用量カードの行ラベル）と、
        # 出力元 → 直近のリクエスト1回分（_format_token_usage 参照）。
        owner_labels: dict[str, str] = {}
        call_usage: dict[str | None, dict] = {}
        steps: dict[str, cl.Step] = {}  # run_id -> Step（ツール開始/終了を対応付け。_resolve_parent_idが「まだ完了していない祖先」の判定に使うため、on_tool_endで必ずpopする）
        # dispatch_agent（サブエージェント委譲）のtool run_id集合。_is_subagent_call
        # が使う。steps と異なり on_tool_end で pop しない（_is_subagent_call
        # docstring参照。[subagent].background_inline_wait_max_seconds 超過で
        # dispatch_agent が早期リターンした後もバックグラウンドジョブが
        # 投げ続ける内部LLMイベントを、ターンが終わるまでサブエージェント
        # 由来と判定し続ける必要があるため）。
        dispatch_agent_run_ids: set[str] = set()
        # 再接続時のStep再同期（_resync_live_steps参照）用に、このターンで
        # 触れた全Step（完了済みも含む）を steps とは別に保持する。steps は
        # on_tool_end で pop される（_resolve_parent_id 等の「現在開いている
        # 祖先」判定に使うため）が、closeイベント自体が切断中に失われる
        # ケース（実例確認済み: 生成10:11:55→切断→確定10:15:58→再接続10:16:03、
        # 確定がちょうど切断ウィンドウ中に発生し再接続後もフロントに届か
        # なかった）に対応するには、closeの成否に関わらずターン終了まで
        # 参照を残し、再接続のたびに現在の状態で再送し続ける必要がある
        # （再送は冪等: 既に届いている場合はフロント側で同じ状態を上書き
        # するだけで害はない）。
        resync_steps: dict[str, cl.Step] = {}
        cl.user_session.set("live_steps", resync_steps)
        cl.user_session.set("live_thinking", None)
        # コンテキスト圧縮の安全点検知用: メインエージェントのtool_call idの
        # 集合と、対応する返却済みToolMessage。サブエージェント（dispatch_agent
        # 内部）呼び出しは対象外（_is_subagent_call参照）。
        #
        # 重要: ツール呼び出しは複数回の LLM 応答にまたがって返却される場合が
        # ある（例: dispatch_agent の tool_call は別の AIMessage を挟んでから
        # ToolMessage が返ってくる）。そのため、この集合は on_chat_model_end
        # でリセットせず、ターン通じて累積する。
        pending_main_tool_ids: set[str] = set()
        pending_main_tool_msgs: list[ToolMessage] = []
        compaction_continued = False  # ループ内圧縮による継続かどうか

        event_stream = graph.astream_events(inputs, config=config, version="v2")
        # リトライ経路（attempt>0）での初回チャンク受信までの待ち時間を計測。
        # llama-server 側で旧リクエストの生成が続きスロットが埋まっている場合、
        # この値が異常に大きくなる（クライアント側後始末では対処不能）。
        retry_first_chunk_start: float | None = None
        if attempt > 0:
            retry_first_chunk_start = time.time()
        # P1: リトライ経路（attempt>0）でのみ、どのタスクで新リクエストが始まったか、
        # cancel scope breakage の結果をログする。
        if attempt > 0:
            csb = recent_cancel_scope_breakage()
            logging.getLogger(__name__).warning(
                "on_message: starting retry %d [%s, cancel_scope_breakage_last_60s=%d]",
                attempt + 1,
                describe_current_task(),
                csb,
            )
        else:
            logging.getLogger(__name__).debug(
                "on_message: starting first request [%s]",
                describe_current_task(),
            )
        try:
            async for event in event_stream:
                kind = event["event"]

                if kind == "on_chat_model_stream":
                    # モデルのトークンをストリーム表示。stream_token() は初回呼び出し時に
                    # UI上の表示位置を確定させるため、ツール呼び出しをまたいで使い回すと
                    # 後続のStepより上に固定表示されてしまう。ツール呼び出し区切りで
                    # 新規 Message を発行し、時系列順に表示されるようにする。
                    chunk = event["data"]["chunk"]

                    # llama-server が reasoning_content（<think>ブロック）を返す場合、
                    # ChatLlamaCpp（src/llm.py）が additional_kwargs へ拾い上げている。
                    # 本回答とは別の折りたたみStepとして「思考中」を可視化する。
                    owner = _stream_owner(event, owner_run_ids)
                    # サブエージェントの回答本文はチャット欄へ流さず、思考Stepの出力欄
                    # （output）へ流す。その場合、思考は入力欄（input）側へ流して分ける
                    # （フロントは metadata.subagent_output を見て「思考」「出力」と表示）。
                    to_thinking_step = owner is not None
                    reasoning = chunk.additional_kwargs.get("reasoning_content")
                    if reasoning:
                        thinking = await _get_or_open_thinking(event, owner, thinkings, steps, resync_steps)
                        await thinking.stream_token(reasoning, is_input=to_thinking_step)

                    if chunk.content:
                        # リトライ後の初回チャンク受信までの待ち時間を記録。
                        # 通常の初回リクエストでは retry_first_chunk_start は
                        # None なのでここは通らない。閾値は暫定10秒（実測で
                        # 調整済み）。
                        if retry_first_chunk_start is not None:
                            elapsed_s = time.time() - retry_first_chunk_start
                            # reasoning_content が先に来る場合、content は後。
                            # 両方合わせて「LLM応答の初回チャンク」とみなす。
                            if elapsed_s > 10.0:
                                logging.getLogger(__name__).warning(
                                    "%.0f seconds until the first chunk after retry (abnormal delay) [%s] - llama-server slots may be stuck",
                                    elapsed_s,
                                    describe_current_task(),
                                )
                            retry_first_chunk_start = None  # 2回目以降は計測しない
                        if to_thinking_step:
                            # 思考Stepはツール開始・出力元の終了時に閉じる（_close_owner_output）。
                            thinking = await _get_or_open_thinking(event, owner, thinkings, steps, resync_steps)
                            await thinking.stream_token(chunk.content)
                        else:
                            # 思考が終わり本回答が始まったのでStepを確定させる（同じ出力元の分だけ）。
                            await _close_thinking(thinkings.pop(owner, None))
                            answer = answers.get(owner)
                            if answer is None:
                                # dispatch_agent 内部（サブエージェント）由来のチャンクは、
                                # astream_events がcontextvar経由で内部呼び出しの
                                # イベントも同じストリームへ伝播させるため、対策しないと
                                # メインエージェントの回答と見分けが付かないまま届く
                                # （_resolve_parent_id/_is_subagent_call 参照）。
                                # 出力元判定（_stream_owner）をすり抜けた場合の保険として
                                # authorを変えてUI側で区別できるようにする。
                                is_subagent_answer = _is_subagent_call(event, dispatch_agent_run_ids)
                                answer = cl.Message(
                                    content="",
                                    author=SUBAGENT_MESSAGE_AUTHOR if is_subagent_answer else None,
                                )
                                answers[owner] = answer
                            await answer.stream_token(chunk.content)

                elif kind == "on_tool_start":
                    # ここまでの思考/回答があれば確定送信し、次のテキストは新しい Message に分ける
                    # （このツールを呼んだ出力元の分だけ。並列実行中の別サブエージェントの分には触れない）。
                    await _close_owner_output(_stream_owner(event, owner_run_ids), thinkings, answers)
                    # ツール実行を Step として可視化（どのスキル/ツールかが見える）。
                    # cl.Step はコンストラクタで local_steps（@cl.on_message が積む
                    # 実行ラン）を自動継承しない（cl.Message は継承する）ため、
                    # _resolve_parent_id() で明示的に親を決める。
                    label = _tool_step_label(event)
                    step = cl.Step(name=label, type="tool", parent_id=_resolve_parent_id(event, steps))
                    step.start = utc_now()
                    step.input = event["data"].get("input")
                    await step.send()
                    steps[event["run_id"]] = step
                    resync_steps[event["run_id"]] = step
                    if event["name"] in _SUBAGENT_DISPATCH_TOOLS:
                        # _is_subagent_call docstring参照。steps と違いターン
                        # 終了までここから取り除かない。
                        dispatch_agent_run_ids.add(event["run_id"])
                        owner_run_ids.add(event["run_id"])
                        owner_labels[event["run_id"]] = label

                elif kind == "on_chain_start" and event["name"] == _BATCH_GROUP_RUN_NAME:
                    # dispatch_agent_batch のグループごとの中間Step。配下のサブエージェントの
                    # 思考・ツールStepはこの下へ入る（_resolve_parent_id が一番近い祖先を選ぶ）。
                    label = (event.get("metadata") or {}).get("batch_group_label") or f"SUB: {i18n.t('step.group')}"
                    step = cl.Step(name=label, type="tool", parent_id=_resolve_parent_id(event, steps))
                    step.start = utc_now()
                    await step.send()
                    steps[event["run_id"]] = step
                    resync_steps[event["run_id"]] = step
                    owner_run_ids.add(event["run_id"])
                    owner_labels[event["run_id"]] = label
                    batch_owner = _stream_owner(event, owner_run_ids - {event["run_id"]})
                    if batch_owner is not None:
                        owner_children.setdefault(batch_owner, set()).add(event["run_id"])

                elif kind == "on_chain_end" and event["name"] == _BATCH_GROUP_RUN_NAME:
                    await _close_owner_output(event["run_id"], thinkings, answers)
                    # 終わったグループの行はカードから消す（行数を並列数までに抑える）。
                    if call_usage.pop(event["run_id"], None) is not None:
                        await _send_token_usage(call_usage)
                    step = steps.pop(event["run_id"], None)
                    if step is not None:
                        output = event["data"].get("output")
                        step.output = output if isinstance(output, str) else ""
                        step.end = utc_now()
                        if isinstance(output, str) and output.startswith("Error:"):
                            step.is_error = True
                        await step.update()

                elif kind == "on_tool_error":
                    # ツール内で例外が起きると on_tool_end ではなくこちらが届く。
                    # 閉じないと、呼び出し側（サブエージェントの _run_one_tool_call 等）が
                    # 例外を拾って処理を続けても、Step が「実行中」のまま残る。
                    step = steps.pop(event["run_id"], None)
                    if step is not None:
                        await _close_owner_output(_stream_owner(event, owner_run_ids), thinkings, answers)
                        if event["name"] in _SUBAGENT_DISPATCH_TOOLS:
                            for child_owner in [event["run_id"], *owner_children.get(event["run_id"], ())]:
                                await _close_owner_output(child_owner, thinkings, answers)
                        step.output = f"Error: {event['data'].get('error')}"
                        step.end = utc_now()
                        step.is_error = True
                        await step.update()

                elif kind == "on_tool_end":
                    step = steps.pop(event["run_id"], None)
                    if step is not None:
                        # サブエージェントの回答は現在は思考Stepの出力欄へ流すが
                        # （上のon_chat_model_stream参照）、出力元判定をすり抜けた分は
                        # author=SUBAGENT_MESSAGE_AUTHOR の answer として
                        # ストリーム表示される。このツール終了時点で確定させておかないと、続く
                        # メインエージェントの最終回答が（間に別のon_tool_startを
                        # 挟まない限り）answer is None判定を通らず、このSUB名義の
                        # Messageへそのまま追記されてしまう
                        # （チャット画面でメインの回答がサブエージェント名義に
                        # 見える不具合の原因）。on_tool_startの冒頭と同じ
                        # flush/resetをここでも行い、ツール境界を必ず区切る。
                        # 将来この確定処理をツール名で除外する改修をする場合、
                        # dispatch_agent は対象に含めないこと（上記のサブ
                        # エージェント名義混同バグが再発する）。
                        # 出力元ごとに分けた後は、このツールを呼んだ出力元に加え、
                        # サブエージェント起動ツールならその配下（自身・グループ）の分も確定させる。
                        await _close_owner_output(_stream_owner(event, owner_run_ids), thinkings, answers)
                        if event["name"] in _SUBAGENT_DISPATCH_TOOLS:
                            removed_usage = False
                            for child_owner in [event["run_id"], *owner_children.get(event["run_id"], ())]:
                                await _close_owner_output(child_owner, thinkings, answers)
                                removed_usage |= call_usage.pop(child_owner, None) is not None
                            if removed_usage:
                                await _send_token_usage(call_usage)
                            await _mark_background_group_steps(owner_children.get(event["run_id"], ()), steps)
                        output = event["data"].get("output")
                        content = getattr(output, "content", output)
                        step.output = content
                        step.end = utc_now()
                        if _is_dispatch_agent_error(event["name"], content):
                            step.is_error = True
                        elif _is_dispatch_agent_truncated(event["name"], content):
                            # ISSUE-001: max_iterations到達等によるサブエージェント
                            # 内部の打ち切りは例外にならず正常なToolMessageとして
                            # 返るため、放置すると「正常完了」に見えてしまう。
                            # ループ検知等と同じstopped_reasonバッジで区別した上で、
                            # 親LLMの言及に頼らず打ち切りをユーザーへ明示する。
                            step.metadata = {"stopped_reason": "subagent_truncated"}
                        await step.update()
                        if _is_dispatch_agent_truncated(event["name"], content):
                            await cl.Message(
                                content=i18n.t("notice.subagent_truncated"),
                                type="system_message",
                            ).send()
                        if isinstance(content, str):
                            generated = extract_generated_files(content)
                            if generated:
                                elements = [
                                    cl.Image(name=path.name, path=str(path), display="inline")
                                    if is_image_file(path)
                                    else cl.File(name=path.name, path=str(path))
                                    for path in generated
                                ]
                                names = i18n.t("list.separator").join(path.name for path in generated)
                                await cl.Message(
                                    content=i18n.t("notice.generated_files", names=names),
                                    elements=elements,
                                    type="system_message",
                                ).send()
                    # 実行計画がユーザーに却下された直後。ツール結果の文言・
                    # system_prompt.mdの指示だけに頼ると、LLMが計画を微修正して
                    # 勝手に続行してしまう恐れがあるため、ここでコード側から
                    # 確実にこのターンを打ち切る（approve_planがフラグを立てる。
                    # src/tools.py参照）。ここで即returnせず _PlanDeniedInterrupt を
                    # raise するのは、ToolNode が生成したこの ToolMessage がまだ
                    # チェックポイントへコミットされていないため（下の except節参照）。
                    # コンテキスト圧縮の安全点チェック（下）より必ず先に評価する:
                    # 却下と圧縮条件が同時に満たされた場合、却下によるターン
                    # 打ち切りを圧縮継続で握りつぶしてはならないため。
                    if event["name"] == "approve_plan" and cl.user_session.get("plan_denied_just_now"):
                        cl.user_session.set("plan_denied_just_now", False)
                        # ここからストリームを閉じるまでの間に、先へ進んだグラフが追加指示を
                        # 取り出さないよう保留する（_TurnInjections 参照。このターンはここで終わる）。
                        user_injection.set_held(thread_id, True)
                        await _close_all_outputs(thinkings, answers)
                        await cl.Message(
                            content=i18n.t("notice.plan_denied"),
                            type="system_message",
                        ).send()
                        raise _PlanDeniedInterrupt(tool_message=event["data"].get("output"))

                    # コンテキスト圧縮: 直近のメインAIMessageが発行した
                    # tool_calls（pending_main_tool_ids）に、今回返却された
                    # ToolMessageが全て揃った時点が、孤立tool_callの無い
                    # 安全な区切り。ここで should_compact() の条件を満たして
                    # いれば _CompactionCheckpoint を送出し、async for ループの
                    # 外側（except節）で安全に aupdate_state による圧縮を行う。
                    if step is not None and not _is_subagent_call(event, dispatch_agent_run_ids):
                        tool_msg = event["data"].get("output")
                        if isinstance(tool_msg, ToolMessage) and tool_msg.tool_call_id in pending_main_tool_ids:
                            pending_main_tool_msgs.append(tool_msg)
                            pending_main_tool_ids.discard(tool_msg.tool_call_id)
                            if not pending_main_tool_ids and pending_main_tool_msgs:
                                cumulative_main = cl.user_session.get("token_usage_cumulative_main")
                                cstate = await graph.aget_state(config)
                                committed_messages = cstate.values.get("messages", []) if cstate else []
                                approx_count = len(committed_messages) + len(pending_main_tool_msgs)
                                compact_due = should_compact(cumulative_main, last_usage, approx_count, _config)
                                awaiting_approval = bool(cl.user_session.get("awaiting_approve_plan_call"))
                                # ISSUE調査用: 安全点（pending_main_tool_idsが空）に到達する
                                # たびに、実際に圧縮まで踏み切ったかどうかを判定材料つきで
                                # 記録する。cumulative_mainが閾値超なのに発火しない事例が
                                # 観測されたため、原因切り分けに使う（2026-08-12調査）。
                                logging.getLogger(__name__).debug(
                                    "Context compaction: reached a safe point compact_due=%s awaiting_approval=%s cumulative_main_total=%s approx_count=%d [%s]",
                                    compact_due,
                                    awaiting_approval,
                                    (cumulative_main or {}).get("total"),
                                    approx_count,
                                    describe_current_task(),
                                )
                                if compact_due and not awaiting_approval:
                                    # 圧縮が終わって再開するまで、追加指示の取り出しを保留する
                                    # （_TurnInjections 参照。except _CompactionCheckpoint で解除）。
                                    user_injection.set_held(thread_id, True)
                                    raise _CompactionCheckpoint(list(pending_main_tool_msgs))

                elif kind == "on_chat_model_end":
                    # config.ini [llm].track_token_usage=true の場合のみ usage_metadata が乗る
                    # （src/llm.py の build_model が stream_usage を有効化している場合）。
                    output = event["data"].get("output")
                    usage = getattr(output, "usage_metadata", None)
                    if usage:
                        _accumulate_usage(turn_totals, usage)
                        # 長時間の承認待ち等でセッションデータが失われている場合に
                        # 備え、Noneなら0から再初期化する（totals[key]でのクラッシュ防止）。
                        cumulative = cl.user_session.get("token_usage_cumulative") or _new_usage_totals()
                        _accumulate_usage(cumulative, usage)
                        cl.user_session.set("token_usage_cumulative", cumulative)
                        cumulative_main = cl.user_session.get("token_usage_cumulative_main") or _new_usage_totals()
                        is_subagent_usage = _is_subagent_call(event, dispatch_agent_run_ids)
                        if not is_subagent_usage:
                            # last_usage はコンテキスト圧縮の単発閾値判定
                            # （should_compact の single_request_token_threshold）
                            # にのみ使うため、メインエージェント由来のusageでのみ
                            # 更新する。サブエージェント呼び出しで無条件に
                            # 上書きしてしまうと、dispatch_agent委譲直後の
                            # on_tool_end安全点でまさに「直前のon_chat_model_end
                            # がサブエージェント内部の最後の呼び出しだった」
                            # ケースが高頻度で起き、判定が歪む。
                            last_usage = usage
                            _accumulate_usage(cumulative_main, usage)
                            cl.user_session.set("token_usage_cumulative_main", cumulative_main)
                            # コンテキスト圧縮: ループ内の安全な区切り検知用に、
                            # メインエージェントが発行したtool_callsを追跡する。
                            #
                            # 重要: ツール呼び出しは複数回の LLM 応答にまたがっ
                            # て返却される場合がある（例: dispatch_agent の
                            # tool_call はサブエージェント内部の最終 LLM 応答
                            # を挟んでから ToolMessage が返ってくる）。
                            # そのため、この集合は on_chat_model_end でリセッ
                            # トせず、ターン通じて累積して tool_call id を追
                            # 跡する。対応する ToolMessage が on_tool_end で
                            # 全て返却された時点で、孤立 tool_call の無い安全
                            # な区切りになる。
                            tool_calls = getattr(output, "tool_calls", None) or []
                            pending_main_tool_ids.update(tc["id"] for tc in tool_calls)
                        # UI表示（cl.Message）はセッション終了とともに追えなくなる
                        # ため、事後にapp.logだけでトークン使用量の推移（LLM呼び出し
                        # 単位の値・このターンの累積・会話全体の累積）を追跡できる
                        # よう、on_chat_model_endのたびにINFOで記録しておく。
                        logging.getLogger(__name__).info(
                            "token usage thread_id=%s call(in=%d,out=%d,total=%d) turn(in=%d,out=%d,total=%d) cumulative(in=%d,out=%d,total=%d) cumulative_main(in=%d,out=%d,total=%d)",
                            thread_id,
                            usage.get("input_tokens", 0) or 0,
                            usage.get("output_tokens", 0) or 0,
                            usage.get("total_tokens", 0) or 0,
                            turn_totals["input"],
                            turn_totals["output"],
                            turn_totals["total"],
                            cumulative["input"],
                            cumulative["output"],
                            cumulative["total"],
                            cumulative_main["input"],
                            cumulative_main["output"],
                            cumulative_main["total"],
                        )
                        # ターンが完了しないまま切断・停止されても再開時に累計が
                        # 0 に戻らないよう、加算のたびに保存する（関数docstring参照）。
                        await _persist_token_usage()
                        await _record_monthly_usage(usage)
                        # ツール呼び出しを挟んで長く動くターンでも、LLM呼び出しの
                        # たびにサイドパネルの表示を更新する（ターン完了まで待たないと
                        # 見えない、という問題を避けるため）。表示は「このターン」の
                        # 累積ではなく、直近のリクエスト（LLM呼び出し）1回分の値を出力元ごとに使う。
                        call_totals = {"input": 0, "output": 0, "total": 0}
                        _accumulate_usage(call_totals, usage)
                        usage_owner = _stream_owner(event, owner_run_ids)
                        if usage_owner is None and is_subagent_usage:
                            usage_owner, usage_label = "sub", "SUB"
                        elif usage_owner is None:
                            usage_label = i18n.t("usage.main")
                        else:
                            usage_label = owner_labels.get(usage_owner, "SUB")
                        call_usage[usage_owner] = {"label": usage_label, **call_totals}
                        await _send_token_usage(call_usage)

                elif kind == "on_custom_event" and event["name"] == "subagent_loop_retry":
                    # src/subagent.py の _invoke_with_loop_retry がサブエージェント
                    # 内部のループ検知リトライを例外を外へ出さずに処理するため、
                    # このターン全体を打ち切る except ThinkingLoopDetected（下）は
                    # 発火しない。何もしないと、打ち切られた直前の試行分の
                    # 「思考中」Stepが「停止」バッジ無しで残ったまま、thinkingが
                    # 非Noneのため次のon_chat_model_streamで新規Stepが作られず、
                    # リトライ後のトークンが同じStepへ継ぎ足されてしまう
                    # （2026-08-21ユーザー報告）。ターン自体は継続するため、
                    # answer/steps/_finalize_orphaned_stepsには触れず、thinkingの
                    # クローズ&リセットのみ行う。
                    await _close_thinking(thinkings.pop(_stream_owner(event, owner_run_ids), None), stopped_reason="loop_detected")

                elif kind == "on_custom_event" and event["name"] == user_injection.INJECTION_APPLIED_EVENT:
                    # 生成中に届いたユーザーの追加指示が会話へ入った（src/graph.py の
                    # _take_user_injections）。ここまでのメインの回答を区切ってから、ユーザー発言として出す
                    # （バックグラウンドで動き続けるサブエージェントの出力には触れない）。
                    await _close_owner_output(None, thinkings, answers)
                    await turn_injections.show((event.get("data") or {}).get("injections") or [])
        except ThinkingLoopDetected as exc:
            # LLM応答（thinking/本文）が反復ループに陥り打ち切られた場合。
            # ここまでの思考/回答があれば確定送信してから、注意メッセージを
            # 注入して再試行する（id付きHumanMessageとして注入し、成功後に
            # 履歴から取り除けるようidを記録しておく）。
            #
            # P2: グラフ再構築は finally 後の共通処理へ延期する。
            # これにより「aclose→rebuild→新リクエスト」の順序を保証できる
            # （従来は except で rebuild → continue した後 finally が実行
            # される順序だった）。
            loop_exc = exc
            logging.getLogger(__name__).warning(
                "Detected an LLM response loop (retry %d): recent text=%r [%s]",
                loop_attempt + 1,
                exc.snippet,
                describe_current_task(),
            )
            # ループ検知による打ち切りである旨をフロントへ伝える。StepItem.tsx
            # 側はこの metadata を見て「完了」ではなく「停止」バッジを表示する
            # （デフォルトでは end のみ設定された Step は他の正常完了Stepと
            # 見分けが付かず「完了」と誤表示されていたため）。
            await _close_all_outputs(thinkings, answers, stopped_reason="loop_detected")
            await _finalize_orphaned_steps(steps, "loop_detected")
        except LLM_CONNECTION_ERRORS as exc:
            # LLMサーバーとの通信エラー（接続失敗・5xx・httpx系）。
            # thinking/answerを確定送信 → チェックポイントの orphan steps
            # を片付ける → turn_broken_exc をセットして finally 後のリビルド
            # 分岐へ合流する。GraphRecursionError ブロックと同型のパターン。
            # [graph].connection_error_max_retries 回までは、接続先を
            # 切り替えつつ自動リトライする（下の if turn_broken_exc is not
            # None: 参照）。リトライを使い切った場合のみユーザーへメッセージを
            # 送って中断する。
            turn_broken_exc = exc
            logging.getLogger(__name__).warning(
                "Detected a communication error with the LLM server: %s [%s]",
                exc,
                describe_current_task(),
            )
            # [llm].main_routing_strategy=priority_failover の場合、直近使用した
            # 接続先を一時的にクールダウンし、次回 build_model() で次点の
            # 接続先へ切り替わるようにする（他戦略では実質無視される）。
            mark_last_endpoint_failed("main")
            await _close_all_outputs(thinkings, answers)
            await _finalize_orphaned_steps(steps, "connection_error")
            checkpointer_needs_rebuild = True
        except _CheckpointerTimeout as exc:
            # checkpointer操作がタイムアウトした（ロック取得の固着）、または
            # 操作の実行中にDB接続が閉じられていた（_CheckpointerConnectionClosed。
            # issue.md「DB接続切れエラー」参照）。いずれも共有のSQLite接続が
            # 不健全な状態にある可能性が高い。turn_broken_exc +
            # checkpointer_needs_rebuild をセットし、対応1の後処理へ合流させる。
            turn_broken_exc = exc
            checkpointer_needs_rebuild = True
            logging.getLogger(__name__).warning(
                "Checkpointer operation failed (%s): %s [%s]",
                type(exc).__name__,
                exc,
                describe_current_task(),
            )
            await _close_all_outputs(thinkings, answers)
            await _finalize_orphaned_steps(steps, "checkpointer_timeout")
        except GraphRecursionError:
            # メインの ReAct ループが recursion_limit（config.ini [graph]）に達した場合。
            # ここまでの思考/回答があれば確定送信してから、打ち切りを明示する。
            await _close_all_outputs(thinkings, answers)
            await _finalize_orphaned_steps(steps, "recursion_limit")
            await cl.Message(
                content=i18n.t("notice.recursion_limit", limit=_config.graph_recursion_limit),
                type="system_message",
            ).send()
            await _send_interrupted_marker("recursion_limit")
            await _remove_message_ids_if_present(graph, config, loop_nudge_ids)
            await turn_injections.sync_with_state(graph, config)
            return
        except _PlanDeniedInterrupt as e:
            # 却下メッセージの送信・thinking/answerの確定は on_tool_end 内で
            # 既に行っている（raise直前）。ここでは、その時点ではまだ
            # チェックポイントへコミットされていない ToolMessage を、async for
            # ループを抜けた今この場（finallyのaclose()より前）で明示的に
            # コミットする。これにより次回のグラフ実行時に「tool_calls に
            # 対応する ToolMessage が無い」という不整合を防ぐ。
            await _finalize_orphaned_steps(steps, "interrupted")
            # 先にストリームを閉じてグラフを止めてから状態を書き換える（_CompactionCheckpoint の
            # 経路と同じ理由。閉じる処理は冪等で、finally の呼び出しは何もしなくなる）。閉じる間に
            # グラフが追加指示を取り込んでいて、それが確定前の書き込みにだけある場合は、ここで
            # 一緒に確定させる（aupdate_state で作るチェックポイントには引き継がれないため）。
            await _aclose_event_stream(event_stream)
            pending_injections = turn_injections.pending_messages(await graph.aget_state(config))
            await graph.aupdate_state(config, {"messages": [e.tool_message, *pending_injections]}, as_node="tools")
            await turn_injections.sync_with_state(graph, config)
            return
        except _CompactionCheckpoint as exc:
            # ループ内の安全な区切り（直近のメインAIMessageのtool_callsに
            # 対応するToolMessageが全て返却済み）でコンテキスト圧縮の条件を
            # 満たした。thinking/answerを確定送信してから、まず孤立していない
            # ことが確認済みのToolMessage群を明示コミットし（_PlanDeniedInterrupt
            # と同じ理由）、圧縮を実行してから inputs=None で同じグラフ実行の
            # 続き（チェックポイントのpending task）を再開する。新しいユーザー
            # 発言を追加するわけではないため、ThinkingLoopDetectedのnudge注入
            # 経路（STARTからの再実行）とは異なる継続方法になる。
            logging.getLogger(__name__).warning(
                "on_message: the context compaction condition was met at a safe point in the loop, pausing graph execution within the turn to compact [%s]",
                describe_current_task(),
            )
            await _close_all_outputs(thinkings, answers)
            await _finalize_orphaned_steps(steps, "context_compaction")
            # 重要: aupdate_state/_run_context_compaction（要約のLLM呼び出しを
            # 含み、数十秒〜数分かかりうる）を呼ぶ前に、必ずこの event_stream を
            # 閉じる。_CompactionCheckpoint は「直近のメインAIMessageのtool_calls
            # に対応するToolMessageが全て返却済み」＝この一つ前のtool_callが
            # 解決した瞬間に発火するため、まだ event_stream 自体は生きており、
            # 閉じないまま放置すると、LangGraph側は agent→tools ノードの実行を
            # バックグラウンドで継続してしまう（例: 次のtool_callとして
            # dispatch_agentが新たに呼ばれ、その完了を待たずに以下の
            # aupdate_state が同じチェックポイントを書き換える）。この競合により
            # バックグラウンドで進行中だったツール呼び出しがLangGraph側で強制
            # キャンセルされ、対応するToolMessageの無い孤立tool_callが生じて
            # 次回モデル呼び出しでValueErrorになる不具合が本番で確認された
            # （2026-08-07、dispatch_agent(verifier)で発生。ThinkingLoopDetected/
            # GraphRecursionError等では既にfinallyでaclose()してから次の
            # astream_events()を呼んでいたが、_CompactionCheckpointの経路だけ
            # aclose()より前にaupdate_stateを呼んでいたのが原因）。
            await _aclose_event_stream(event_stream)
            # 閉じる間にグラフが追加指示を取り込んでいて、それが確定前の書き込みにだけある場合は、
            # ToolMessage と一緒に確定させる（aupdate_state で作るチェックポイントには引き継がれず、
            # 失われるため。_TurnInjections 参照）。
            pending_injections = turn_injections.pending_messages(await graph.aget_state(config))
            to_commit = [*exc.tool_messages, *pending_injections]
            if to_commit:
                await graph.aupdate_state(config, {"messages": to_commit}, as_node="tools")
            try:
                await _run_context_compaction_visible(graph, config, thread_id, last_usage)
            finally:
                # 再開（inputs=None で inject ノードから）の前に保留を解く。
                user_injection.set_held(thread_id, False)
            # 圧縮後に新しいツール呼び出しが続く場合に備え、安全点検知用
            # 変数をリセットする。
            pending_main_tool_ids.clear()
            pending_main_tool_msgs.clear()
            compaction_continued = True
        except asyncio.CancelledError as exc:
            # 停止ボタン・切断・リロード等でこのターンがキャンセルされた場合、
            # 実行中だったtool_call（approve_planのAskActionMessage待機中など）
            # に対応するToolMessageが生成されないままチェックポイントが
            # コミット済みのAIMessage(tool_calls)だけを残し、次回グラフ実行時に
            # 「tool_callsに対応するToolMessageが無い」不整合を起こす
            # （langchain-ai/langgraph#6726と同種の問題）。_PlanDeniedInterrupt
            # と同じ手法で、プレースホルダのToolMessageを補完コミットしてから
            # キャンセルを再送出する。
            #
            # P1: 無条件で1行WARNINGを出す（孤立tool_callの有無に関わらず）。
            # 例外チェーン（__cause__/__context__）を記録し、なぜキャンセル
            # されたかの診断に使う。
            logging.getLogger(__name__).warning(
                "on_message: detected CancelledError [%s, cause=%r, context=%r]",
                describe_current_task(),
                repr(exc.__cause__),
                repr(exc.__context__),
            )
            try:
                state = await graph.aget_state(config)
                repair, orphaned_count, recovered_count = _orphan_repair_messages(state, "Because the user stopped it (or similar), ")
                # 画面に出した追加指示は、確定前の書き込みにだけあっても会話に残す（画面と揃える）。
                # 出していない指示は残さない（フロントエンドが入力欄へ戻すため。下の drop_undisplayed）。
                repair += [
                    m
                    for m in turn_injections.pending_messages(state)
                    if any(i["id"] in turn_injections.displayed for i in user_injection.injection_items(m))
                ]
                if repair:
                    await graph.aupdate_state(config, {"messages": repair}, as_node="tools")
                    logging.getLogger(__name__).warning(
                        "on_message: detected CancelledError and repaired the checkpoint by adding placeholder ToolMessages for %d orphaned tool_calls"
                        " (and recommitting %d results of tool calls that had completed)",
                        orphaned_count,
                        recovered_count,
                    )
                await turn_injections.drop_undisplayed(graph, config)
            except _CheckpointerTimeout:
                # checkpointerのロックが固着している可能性が高いが、
                # この例外を伝播させるとCancelledErrorが消える。
                # ログのみに留めて元のCancelledErrorの伝播を妨げない
                # （次回メッセージ送信時のaget_tupleで同じタイムアウトが
                # 再検知され、その時のturn_broken_exc処理で自己修復する）。
                logging.getLogger(__name__).debug(
                    "Checkpointer operation timed out while handling CancelledError",
                    exc_info=True,
                )
            await _send_interrupted_marker("stopped")
            raise
        except Exception as exc:  # noqa: BLE001
            # 上のどの except 節にも一致しない未分類の例外に対する保険。
            # 個別に列挙した例外型（LLM_CONNECTION_ERRORS等）から1つ漏れて
            # いた場合、従来はここで捕捉されずChainlit最上位ハンドラまで
            # 伝播し、turn_broken_exc が一切セットされないままターンが
            # 終わってしまっていた（_rebuild_checkpointer/_rebuild_graphが
            # 一度も呼ばれず、壊れたLLMクライアント/checkpointerを抱えた
            # セッションが永久に復旧しない。本番incident・2026-07-31:
            # httpx.ReadErrorがLLM_CONNECTION_ERRORSに含まれておらず発生。
            # 根本対応はsrc.llm.LLM_CONNECTION_ERRORSをhttpx.TransportError
            # 基底クラスへ広げたことだが、今後また未知の例外型が漏れても
            # セッションを自己修復可能な状態に保つため、ここでも
            # except LLM_CONNECTION_ERRORS と同じ回復処理に倒す）。
            turn_broken_exc = exc
            checkpointer_needs_rebuild = True
            logging.getLogger(__name__).error(
                "on_message: detected an unclassified exception (%s): %s [%s]",
                type(exc).__name__,
                exc,
                describe_current_task(),
                exc_info=True,
            )
            await _close_all_outputs(thinkings, answers)
            await _finalize_orphaned_steps(steps, "unclassified_error")
        finally:
            # astream_events() の非同期ジェネレータは、async for が
            # ThinkingLoopDetected/GraphRecursionError等で中断された場合、
            # 明示的に aclose() しないとLangGraph内部のバックグラウンド
            # タスクやcheckpointerのロックが残留しうる（GC任せだと即座に
            # aclose()される保証がない）。次のイテレーションで同じ
            # thread_id に対し astream_events() を呼び直す前に必ず閉じる
            # （_CompactionCheckpoint経路は except 節内で既に閉じているため、
            # ここでの呼び出しは _aclose_event_stream() 内の通り no-op になる）。
            await _aclose_event_stream(event_stream)
            # ThinkingLoopDetected/GraphRecursionError以外（停止ボタンによる
            # CancelledError等、上のexcept節が捕捉しない中断）でも steps が
            # 未finalizeのまま残りうる。安全網として finally で必ず片付ける
            # （空dictなら _finalize_orphaned_steps は何もしない）。
            await _finalize_orphaned_steps(steps, "interrupted")
            # thinking も同じ理由で未クローズのまま残りうる（本番実測:
            # ThinkingLoopDetectedリトライ中に停止ボタンでCancelledErrorが
            # 発生し、except asyncio.CancelledError節はraiseするのみで
            # thinkingを閉じないため、フロント側で「実行中」のまま固着した）。
            # steps と同じ安全網パターンで必ず閉じる。
            for owner in list(thinkings):
                await _close_thinking(thinkings.pop(owner), stopped_reason="interrupted")

        # ストリームが途中で閉じた（圧縮・ループ検知・通信エラー等）場合に、会話へ入ったのに
        # 通知が読まれず画面に出ていない追加指示があれば出す（_TurnInjections 参照）。
        # checkpointer を作り直す必要がある（固着・接続切れの疑い）ときは、読みに行くと
        # タイムアウトまで待たされるため飛ばす。
        if not checkpointer_needs_rebuild:
            await turn_injections.sync_with_state(graph, config)

        # P2: ThinkingLoopDetected のグラフ再構築・nudge注入を finally 後へ延期。
        # これにより「aclose→rebuild→新リクエスト」の順序が保証される。
        # 却下する案: finally の後始末を別タスクに切り離す案は、今回の障害の
        # 直接原因（後始末が別タスクに漏れたこと）を悪化させるだけなので採用しない。
        if compaction_continued:
            # ターン内コンテキスト圧縮による継続は、total_retries（エラー
            # リトライ予算）を消費しない。inputs=None はチェックポイントの
            # pending task（agentノードの続き）から再開する意味であり、
            # 新しいユーザー発言を追加するわけではない。
            inputs = None
            continue

        if loop_exc is not None:
            if _should_retry_after_loop(loop_exc, loop_attempt, loop_max_retries):
                # ThinkingLoopDetected 発生時は常に旧接続を強制クローズする。
                # llama-server側で旧ストリームが残ったままになると、
                # 新しいクライアントを使っても次のリトライが応答ヘッダー
                # 待ちでハングしうる（本番incident・2026-07-20:
                # 7分11秒間ハング。2026-07-31: 同型の事象が再発し、
                # ユーザーが手動キャンセルするまで復帰しなかった）。
                await aclose_active_llm_clients(thread_id)
                graph = await _rebuild_graph(thread_id)
                logging.getLogger(__name__).warning(
                    "ThinkingLoopDetected: rebuilt the LLM graph before retrying [%s]",
                    describe_current_task(),
                )
                if isinstance(loop_exc, ToolCallLoopDetected) and loop_exc.remove_ids:
                    # 繰り返した応答を残すと、注意しても同じ tool_call を書き写し続ける
                    # （src/tool_loop_guard.py 冒頭参照）。
                    await _remove_message_ids_if_present(graph, config, list(loop_exc.remove_ids))
                    logging.getLogger(__name__).warning(
                        "Removed the tool call loop section (%d messages) from the history [%s]",
                        len(loop_exc.remove_ids),
                        describe_current_task(),
                    )
                repaired = await _repair_orphaned_tool_calls(graph, config)
                if repaired:
                    logging.getLogger(__name__).warning(
                        "Repaired %d orphaned tool_calls [%s]",
                        repaired,
                        describe_current_task(),
                    )
                nudge_id = str(uuid.uuid4())
                loop_nudge_ids.append(nudge_id)
                if isinstance(loop_exc, ToolCallLoopDetected):
                    text = tool_loop_nudge_text(_config.tool_loop_guard_nudge_messages, tool_loop_attempt, loop_exc)
                    tool_loop_attempt += 1
                else:
                    text = pick_loop_nudge_message(_config.thinking_loop_guard_nudge_messages, loop_attempt)
                loop_attempt += 1
                inputs = {"messages": [HumanMessage(content=text, id=nudge_id)]}
                attempt += 1  # for range(total_retries + 1) の暗黙インクリメント相当
                loop_exc = None  # このターンの検知を消費したので次周回へ持ち越さない（状態リーク防止）
                continue
            if isinstance(loop_exc, ToolCallLoopDetected) and loop_exc.remove_ids:
                # 上限で停止する場合も取り除く。残すと次のターンでまた同じ tool_call を
                # 書き写し、通知に添える loop_exc.detail の「取り除きました」とも食い違う。
                await _remove_message_ids_if_present(graph, config, list(loop_exc.remove_ids))
            await cl.Message(
                content=(
                    i18n.t("notice.tool_call_loop", retries=loop_max_retries, detail=loop_exc.detail)
                    if isinstance(loop_exc, ToolCallLoopDetected)
                    else i18n.t("notice.generation_loop", retries=loop_max_retries)
                ),
                type="system_message",
            ).send()
            await _remove_message_ids_if_present(graph, config, loop_nudge_ids)
            return

        if turn_broken_exc is not None:
            # 接続エラー（LLM_CONNECTION_ERRORS）で、かつ
            # graph_connection_error_max_retries の予算が残っている場合のみ、
            # 下のリビルド後に中断せず自動リトライする。_CheckpointerTimeout・
            # 未分類例外（安全網）はこれまで通り常に中断する
            # （通信先の切り替えでは解決しない種類の失敗のため）。
            can_retry_connection_error = (
                isinstance(turn_broken_exc, LLM_CONNECTION_ERRORS)
                and connection_retry_attempt < connection_error_max_retries
            )
            if checkpointer_needs_rebuild:
                await _rebuild_checkpointer(thread_id)
                logging.getLogger(__name__).warning(
                    "Rebuilt the checkpointer [%s]",
                    describe_current_task(),
                )
            # ThinkingLoopDetected 経路（1772行目）と同じ理由で、旧接続を
            # 強制クローズしてから再構築する。ここを素通りすると
            # llama-server側で旧ストリームがactiveなまま残り、次のターンが
            # 応答ヘッダー待ちでハングして復旧不能になる。
            await aclose_active_llm_clients(thread_id)
            graph = await _rebuild_graph(thread_id)
            logging.getLogger(__name__).warning(
                "Rebuilt the graph because of an error: %s [%s]",
                turn_broken_exc,
                describe_current_task(),
            )
            repaired = await _repair_orphaned_tool_calls(graph, config)
            if repaired:
                logging.getLogger(__name__).warning(
                    "Repaired %d orphaned tool_calls [%s]",
                    repaired,
                    describe_current_task(),
                )
            if can_retry_connection_error:
                connection_retry_attempt += 1
                logging.getLogger(__name__).warning(
                    "Switching endpoints and retrying automatically because of a communication error (%d/%d) [%s]",
                    connection_retry_attempt,
                    connection_error_max_retries,
                    describe_current_task(),
                )
                turn_broken_exc = None
                checkpointer_needs_rebuild = False
                # inputs=None はチェックポイントの pending task（agentノードの
                # 続き＝失敗したLLM呼び出しそのもの）から再開する意味であり、
                # 新しいユーザー発言を追加するわけではない
                # （_CompactionCheckpoint 経路と同じ考え方）。
                inputs = None
                continue
            await cl.Message(
                content=i18n.t("notice.connection_error"),
                type="system_message",
            ).send()
            await _send_interrupted_marker("connection_error")
            return

        await _close_all_outputs(thinkings, answers)

        # 無言終了（tool_calls も回答テキストも無いまま終わる）を検知した場合、
        # 最終回答を促す短いメッセージを注入して自動的に1回だけリトライする
        # （thinkingの長引き・コンテキスト逼迫等でこの状態に陥ることがある
        # 小型ローカルモデル対策。evals/run_case.py の
        # ainvoke_ensuring_final_text と同じ考え方）。
        if attempt < total_retries:
            state = await graph.aget_state(config)
            messages = state.values.get("messages", []) if state else []
            if is_empty_final_message(messages):
                nudge_id = str(uuid.uuid4())
                empty_nudge_ids.append(nudge_id)
                inputs = {"messages": [HumanMessage(content=EMPTY_RESPONSE_NUDGE, id=nudge_id)]}
                attempt += 1  # for range(total_retries + 1) の暗黙インクリメント相当
                continue

        # 最終回答の生成中に届いたユーザーの追加指示（次のモデル呼び出しが無いため
        # グラフでは取り込まれない）は、そのまま次のユーザー発言としてグラフを続けて回す。
        # 残りが無ければ受付を閉じる。drain と close_thread の間に await を挟まないため、
        # 受け付けた指示がここで取りこぼされることは無い（閉じた後の送信は 409 になり、
        # フロントエンドがターンの終了後に通常のメッセージとして送り直す）。
        # 停止で受付が閉じられた・別のターンの受付に替わった後は取り出さない（トークンで判定）。
        leftover = (
            user_injection.drain(thread_id)
            if injection_token is not None and user_injection.current_token(thread_id) == injection_token
            else []
        )
        if leftover:
            await turn_injections.show(user_injection.event_payload(leftover)["injections"])
            # 最終回答の後なので、ターン途中の割り込みと違い新しいユーザー発話として扱う
            # （mark_user_turn: [llm].reasoning_preserve で前のターンの thinking を送り続けないため）。
            inputs = {"messages": [mark_user_turn(user_injection.to_human_message(leftover, injection_token))]}
            continue
        if injection_token is not None:
            user_injection.close_thread(thread_id, injection_token)
        break

    # ループ検知・無言終了いずれの注意メッセージ（機械的な注入）も、成功して
    # 会話が完了した後は履歴に残さず取り除く（残すと長い会話ほど全量再送で
    # コンテキストを圧迫し、過去の失敗の痕跡がモデル自身の目に触れ続けることに
    # なる）。
    await _remove_message_ids_if_present(graph, config, loop_nudge_ids + empty_nudge_ids)

    # 会話ログ（[chat_log].enabled=true の場合のみ、on_chat_start で
    # cl.user_session["chat_log_path"] が設定済み）。ユーザー発言と
    # このターンのAIの最終応答（thinking・ツール呼び出し詳細は含めない）
    # のみをテキストファイルへ追記する。
    chat_log_path = cl.user_session.get("chat_log_path")
    if chat_log_path is not None:
        state = await graph.aget_state(config)
        messages = state.values.get("messages", []) if state else []
        final_ai_message = messages[-1] if messages else None
        ai_text = final_ai_message.content if isinstance(final_ai_message, AIMessage) else ""
        # 同一thread_idを複数タブから使うと2ターンがほぼ同時に完了しうる
        # （_chat_log_locks 宣言部のコメント参照）。ロックで直列化して
        # ログファイルの行が交錯しないようにする。
        lock = _chat_log_locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            append_turn(
                chat_log_path,
                message.content + "".join(f"\n\n[additional instruction] {text}" for text in turn_injections.texts),
                ai_text,
                token_usage_cumulative=cl.user_session.get("token_usage_cumulative"),
            )

    # スレッド再開（on_chat_resume）用に、cl.user_session限定でLangGraph側には
    # 保存されない付帯状態（work_dir/plan/トークン累計）をターン完了ごとに
    # thread_store へスナップショットする（ベストエフォート。無くても会話の
    # 続き自体はLangGraphのチェックポイントから正しく再開できる）。
    if _config.thread_store_enabled and _thread_store_conn is not None:
        await thread_store.save_thread(
            _thread_store_conn,
            thread_id,
            metadata={
                "work_dir": cl.user_session.get("work_dir"),
                "plan": cl.user_session.get("plan"),
                "plan_approved": cl.user_session.get("plan_approved"),
                "token_usage_cumulative": cl.user_session.get("token_usage_cumulative"),
                "token_usage_cumulative_main": cl.user_session.get("token_usage_cumulative_main"),
                "context_compaction_note_skip_count": cl.user_session.get("context_compaction_note_skip_count"),
            },
        )

    # コンテキスト圧縮（ClaudeCodeのcompact相当）。ターン内の安全な区切り
    # （_CompactionCheckpoint）で既に発火している可能性があるが、ここでも
    # ターン完了直後の状態で改めて判定する（ループ内で発火しなかった場合の
    # 最終防衛ライン）。_run_context_compaction は進行中のグラフ実行が
    # 完全に終わった後のこのタイミングでも安全に呼べる。
    # ユーザーへの返信は既に送信済みだが、要約LLM呼び出しに数十秒〜数分
    # かかりうる（実測97秒）ため、_run_context_compaction_visible で
    # Stepを表示し「思考中のまま何も起きていないように見える」状態を防ぐ。
    await _run_context_compaction_visible(graph, config, thread_id, last_usage)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """_on_message_impl の薄いラッパー。

    会話履歴（左サイドバー）で「他のスレッドが裏で処理中」を示すインジケーター
    表示のため、ターン処理の開始/終了を _generating_thread_ids へ記録する。
    ユーザーがこのタブから離れて別のスレッドをクリックしても（フロントエンドは
    完全に別セッションとして新規接続し直すため）、LangGraph側の処理自体は
    documented通りバックグラウンドで継続する（on_chat_end 参照）。しかし従来は
    「今も裏で動いている」ことを他のセッション（他のブラウザタブ・別スレッドを
    開いた同じタブ）が知る手段が無く、あたかも会話が終了したかのように見えて
    いた（2026-08-19 ユーザー報告）。

    合わせて以下2点も他セッションから見えるようにする（同日の追加報告）:
    - session.emit を _make_relayed_emit でラップし、同じスレッドを今まさに
      開いている他セッションへ思考ステップ・回答のストリーミングを中継する。
    - asyncio.current_task() を _generating_thread_tasks へ登録し、閲覧側の
      「停止」ボタン（/locohane/threads/{id}/stop）からこのタスクを
      cancel() できるようにする。

    さらに、同じ所有者（thread_store.resolve_owner。匿名モードでは
    "anonymous"に一元化）が別スレッドで既に生成中の場合、このメッセージは
    実行せず拒否する（新規チャット・他の会話履歴からの並列送信を防ぐ。
    2026-08-19 ユーザー要望）。_generating_owner_threads の確認〜マークの
    間に await を挟まないため、asyncioの単一イベントループ上で競合なく
    安全に判定できる。

    try/finally で必ず記録・中継設定を元に戻すため、正常終了・例外・
    停止ボタンによる CancelledError のいずれの経路でも取りこぼさない。
    """
    thread_id = cl.user_session.get("thread_id")
    session = cl.context.session if thread_id is not None else None
    owner = thread_store.resolve_owner(session.user) if session is not None else None

    if thread_id is not None and owner is not None:
        conflicting_thread = _generating_owner_threads.get(owner)
        if conflicting_thread is not None and conflicting_thread != thread_id:
            logging.getLogger(__name__).info(
                "on_message: rejected this message (thread_id=%s) without running it because another thread (thread_id=%s) of owner %s is generating",
                owner,
                conflicting_thread,
                thread_id,
            )
            await cl.Message(
                content=i18n.t("notice.other_thread_busy"),
                type="system_message",
            ).send()
            return

    if thread_id is not None and thread_id in _generating_thread_ids:
        # 同じスレッドが既に生成中（停止ボタンの後始末がまだ終わっていない場合や、生成中の会話へ
        # 戻ってきた直後で、フロントエンドが「他セッションで処理中」をまだ知らない間に送った場合）。
        # 並行して2本目のターンを走らせると、同じチェックポイントを奪い合い、追加指示の受付
        # （src/user_injection.py）も取り違える。生成中の追加指示は /locohane/threads/{id}/inject へ送る。
        logging.getLogger(__name__).info(
            "on_message: rejected this message without running it because the same thread (thread_id=%s) is still generating",
            thread_id,
        )
        await cl.Message(content=i18n.t("notice.this_thread_busy"), type="system_message").send()
        return

    original_emit = session.emit if session is not None else None
    if thread_id is not None and session is not None:
        _mark_thread_generating(thread_id, owner, session.id)
        _generating_thread_tasks[thread_id] = asyncio.current_task()
        session.emit = _make_relayed_emit(session, thread_id, original_emit)
        # 生成中に送られたユーザーの追加指示（/locohane/threads/{id}/inject）の受付を始める。
        # 正常に終わる場合は _on_message_impl がターンの最後に閉じる（user_injection.current_token
        # でこのトークンを受け取る。open_thread からその呼び出しまで await を挟まないこと）。
        injection_token = user_injection.open_thread(thread_id)
    try:
        await _on_message_impl(message)
    finally:
        if thread_id is not None and session is not None:
            # 停止・エラーで中断した場合に残っていた指示は捨てる（フロントエンドは反映されなかった
            # 指示を入力欄へ戻す）。トークン付きなので、停止後に同じスレッドで始まった次のターンの
            # 受付は閉じない。
            dropped = user_injection.close_thread(thread_id, injection_token)
            if dropped:
                logging.getLogger(__name__).info(
                    "Dropped %d additional user instruction(s) because the turn ended before applying them thread_id=%s",
                    len(dropped),
                    thread_id,
                )
            session.emit = original_emit
            _unmark_thread_generating(thread_id, owner)
