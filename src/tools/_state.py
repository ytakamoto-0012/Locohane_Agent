"""ツール群が共有するモジュールレベル状態（config値・セマフォ・contextvar等）と
init_tools() による注入処理。

他のファイルはここの値を必ず `_state.NAME`（属性アクセス）で参照すること。
`from ._state import NAME` は禁止（init_tools() の再代入や monkeypatch が反映されなくなるため）。"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from langchain_core.tools import BaseTool
from pathlib import Path
import asyncio
import contextvars
import logging

from .. import memory
from ..agent_types import AgentType
from ..config import Config
from ..config import DEFAULT_DISPATCH_AGENT_BACKGROUND_MIN_POLL_MESSAGE
from ..config import DEFAULT_SCRIPT_BACKGROUND_MIN_POLL_MESSAGE
from ..config import SandboxDirEntry
from ..config import expand_config_vars
from ..llm import get_current_session
from . import registry

logger = logging.getLogger(__name__)


# 現在の実行がサブエージェント（dispatch_agent 経由）の内部かどうかを示す
# コンテキスト変数。dispatch_agent がサブエージェント起動前に True を設定する。
# contextvars は asyncio.gather() が生成する子タスクにも起動時点の値が
# コピーされるため、run_subagent 内から並列実行される各ツール呼び出しからも
# 参照できる（_check_file_tools_duplicate の重複ガードが、
# [file_tools_duplicate_guard].carry_over_to_main=false のときに
# メインエージェント/サブエージェントの呼び出し履歴を分けるために使う）。
_IN_SUBAGENT: contextvars.ContextVar[bool] = contextvars.ContextVar("_in_subagent", default=False)

# 現在の dispatch_agent 呼び出しを一意に識別するID（サブエージェント外では None）。
# 重複ガードの集合をサブエージェント実行ごとに分けるために使う。サブエージェントの
# 会話履歴は委譲元にも他のサブエージェントにも共有されず、返るのは最終回答テキスト
# だけなので、あるサブエージェントが読んだ画像・ファイルを別のサブエージェントや
# メインが読み直すのは重複ではなく正当な再取得である（同一集合で数えると、
# 1件目のサブエージェントが読んで返しきれなかった画像を誰も読み直せなくなる）。
_SUBAGENT_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("_subagent_run_id", default=None)

# 現在実行中のサブエージェントの agent_type 名（サブエージェント外では None）。
# _AGENT_TYPE_RUN_SCRIPT_ALLOWLIST によるスキル/スクリプト制限のチェックに使う。
_SUBAGENT_AGENT_TYPE: contextvars.ContextVar[str | None] = contextvars.ContextVar("_subagent_agent_type", default=None)

# チャット画面（入力欄ツールバー）で選んだ reasoning_effort を入れる cl.user_session のキー。
# app.py の on_message が送信時の値（空文字なら config.ini の値を使う）を入れ、メインの
# グラフと dispatch_agent のサブエージェントが config.with_reasoning_effort() で反映する。
SESSION_REASONING_EFFORT_KEY = "reasoning_effort"

# agent_type ごとに run_script で呼んでよいスキル/スクリプトを制限する（未登録の
# agent_type は制限なし）。要素は「スキル名の文字列」（そのスキル配下の全スクリプト
# を許可）または「(スキル名, スクリプトファイル名)」のタプル（そのスクリプトのみ
# 許可）のいずれかを混在できる。pdf-tools のように同一スキル配下に読み込み専用
# スクリプト（read_pdf.py/render_pdf_pages.py）と書き込みスクリプト（create_pdf.py）
# が混在するスキルは、スキル名単位ではなくタプルで個別に許可すること。
#
# agents/*.md のプロンプト文面だけでこの制約を課しているagent_typeは、低パラメータ
# モデルでは指示を無視して他スキルの読み込み専用スクリプトまで呼んでしまうことが
# ある（本番同等のevalケースで実際に発生: explore-websearch が本来explore専用の
# read_excel.pyを呼んでxlsxを調査し、explore向けの誤診断防止ルールが適用され
# ないまま処理が進んでしまった）。プロンプトの記述と実際の許可を一致させるため、
# コード側でも強制する。explore/verifier は「書き込み系スクリプトは絶対に
# 呼び出さない」とプロンプト上で強く約束しているため、同じ理由でここに含める。
#
# ここでの初期値は init_tools() 未実行時（テスト等）のフォールバック。通常起動時は
# config.ini の [subagent].agent_type_run_script_allowlist から
# init_tools() が上書きする（他の値と異なり _script_job.py 側は
# `_state._AGENT_TYPE_RUN_SCRIPT_ALLOWLIST` と属性アクセスで参照すること。
# `from ._state import _AGENT_TYPE_RUN_SCRIPT_ALLOWLIST` は再代入が反映されないため禁止）。
_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST: dict[str, frozenset[str | tuple[str, str]]] = {
    "explore": frozenset(
        {
            "docx-render",
            "docx-read",
            "excel-render",
            "excel-read",
            "excel-vba-read",
            "pptx-render",
            "pptx-read",
            "pptx-inspect",
            ("pdf-tools", "render_pdf_pages.py"),
            ("pdf-tools", "read_pdf.py"),
        }
    ),
    "verifier": frozenset(
        {
            "excel-render",
            "excel-read",
            "docx-render",
            "docx-read",
            "pptx-render",
            "pptx-read",
            "pptx-inspect",
            ("pdf-tools", "render_pdf_pages.py"),
            ("pdf-tools", "read_pdf.py"),
        }
    ),
}

def _duplicate_guard_session_key(base_key: str) -> str:
    """重複ガードの記録先キーを、実行コンテキスト（メイン／各サブエージェント）ごとに分ける。

    [file_tools_duplicate_guard].carry_over_to_main が false のとき、
    サブエージェント内の呼び出しはメインエージェント側の判定に影響させない。
    さらにサブエージェント同士も互いに影響しないよう、dispatch_agent の実行ID
    ごとに別キーにする。

    Args:
        base_key: メインエージェント用の記録先キー
            （例: "file_tools_call_signatures"）。

    Returns:
        実際に cl.user_session へ保存するキー。
    """
    cfg = _LLM_CONFIG
    guard_carry_over = cfg.file_tools_duplicate_guard_carry_over_to_main if cfg else True
    if guard_carry_over:
        return base_key
    run_id = _SUBAGENT_RUN_ID.get()
    if run_id is None:
        return base_key
    return f"{base_key}_subagent_{run_id}"


# dispatch_agent の実際のLLM呼び出しの同時実行数をガードするセマフォ。
# ToolNode は同一AIMessage内の複数tool_callsを並列実行するため、モデルが
# dispatch_agent を1ターンで複数回呼ぶと、単一インスタンスのllama-server
# へ複数リクエストが同時に飛ぶ。system_prompt.md で「並列発行しない」と
# 指示するだけでは低パラメータモデルの遵守が不確実な上、llama-serverの
# --parallel スロット数を超えて並列実行するとチェックポイントの破損
# （AIMessageのtool_callsに対応するToolMessageが欠落し、次のモデル呼び出し
# でLangGraphがValueErrorを送出する）が本番で確認されたため、モデルの
# 挙動に関係なく確実に効くコード側のガードとして同時実行数を制限する
# （並列呼び出し自体は許可し、超過分は待ち順に処理される）。
# init_tools() が config.ini の [subagent].max_parallel に応じて
# _DISPATCH_AGENT_MAX_PARALLEL を再設定する。0以下はガード無効（無制限）を表す。
# セッション（llm.get_current_session() が返す thread_id）ごとに独立した
# Semaphore を遅延生成する辞書で保持する（グローバル単一 Semaphore だと、
# 無関係な複数セッションのdispatch_agent呼び出しが互いに待ち合ってしまうため。
# 全体のHTTPリクエスト総数は [llm].max_concurrent_requests が別途一元管理する）。
# 既定値1は init_tools() 未実行時（テスト等）の安全側フォールバック。
_DISPATCH_AGENT_MAX_PARALLEL: int = 1
_DISPATCH_AGENT_SEMAPHORES: "dict[str | None, asyncio.Semaphore]" = {}

# メインエージェントの全ツール呼び出し（ImageAwareToolNode 経由）の同時実行数を
# ガードするセマフォ。init_tools() が config.ini の [graph].max_parallel に
# 応じて _TOOL_CALL_MAX_PARALLEL を再設定する。0以下はガード無効を表す。
# 上記 _DISPATCH_AGENT_SEMAPHORES と同じ理由・同じ辞書方式でセッション毎に
# 独立させる。既定値1は init_tools() 未実行時（テスト等）の安全側フォールバック。
_TOOL_CALL_MAX_PARALLEL: int = 1
_TOOL_CALL_SEMAPHORES: "dict[str | None, asyncio.Semaphore]" = {}

# `_tmp_<name>`（execute_python_code / run_script の中間生成物置き場）の
# `<name>` 部分（作成時刻プレフィックス付きthread_id）を、スレッド（thread_id）
# ごとにキャッシュする辞書。_workdir.py の _exec_tmp_name() が使う。
# キャッシュしないと、同一スレッド内で名前を計算する複数の呼び出し箇所
# （環境変数へ渡す計算とディレクトリ実体を作る計算）がごく僅かなタイミング差で
# 異なる名前を生成してしまい、書き込みガードと実ディレクトリ名が食い違う
# 事故があった（2026-08-26 発見・修正）。
_EXEC_TMP_NAME_CACHE: "dict[str, str]" = {}


def _get_session_semaphore(registry: "dict[str | None, asyncio.Semaphore]", max_parallel: int) -> "asyncio.Semaphore | None":
    """現在のセッション（llm.get_current_session()）専用の Semaphore を
    registry から取得する。無ければ max_parallel で新規生成して登録する
    （評価ハーネス等、Chainlitセッションを持たない呼び出し元は session_id が
    None になり、その呼び出し元同士で1つの Semaphore を共有する）。
    max_parallel が0以下ならガード無効として None を返す。
    """
    if max_parallel <= 0:
        return None
    session_id = get_current_session()
    sem = registry.get(session_id)
    if sem is None:
        sem = asyncio.Semaphore(max_parallel)
        registry[session_id] = sem
    return sem


def forget_session_tool_semaphores(session_id: str) -> None:
    """セッション終了時（@cl.on_chat_end）に、そのセッション専用の Semaphore
    エントリ・`_exec_tmp_name()` キャッシュエントリを辞書から削除する。
    Semaphore 自体は参照が無くなり次第GCされるが、辞書キー（session_id文字列）
    がプロセス寿命中ずっと残り続けるのを防ぐのが目的（src/llm.py の
    forget_session() と同じ理由）。
    """
    _TOOL_CALL_SEMAPHORES.pop(session_id, None)
    _DISPATCH_AGENT_SEMAPHORES.pop(session_id, None)
    _EXEC_TMP_NAME_CACHE.pop(session_id, None)


async def _tool_call_semaphore_wrap(request, execute):
    """ToolNode(awrap_tool_call=...) 用インターセプタ。

    全ツール呼び出し（同期/非同期問わず _execute_tool_async 経由で正しく
    振り分けられた実行）を、現在のセッション専用の Semaphore（_TOOL_CALL_SEMAPHORES
    参照）で待ち合わせる。dispatch_agent は専用の _DISPATCH_AGENT_SEMAPHORES でも
    重ねてガードされる形になるが、単に入れ子になるだけで問題ない。
    """
    sem = _get_session_semaphore(_TOOL_CALL_SEMAPHORES, _TOOL_CALL_MAX_PARALLEL)
    if sem is None:
        return await execute(request)
    if sem.locked():
        tool_name = request.tool_call.get("name")
        logger.info("tool_call: no free slot, waiting tool=%r", tool_name)
    async with sem:
        return await execute(request)


@dataclass(frozen=True)
class ResolvedAgentType:
    """AgentType のツール名を実際の BaseTool へ解決した実行時表現。

    init_tools() が agent_type_defs（frontmatterの生値）から組み立て、
    _AGENT_TYPES に保持する。dispatch_agent はこれを引いて run_subagent()
    に渡す。
    """

    description: str
    system_prompt: str
    tools: list[BaseTool]
    # frontmatter の model（AgentType.model 参照）。None なら通常のルーティング。
    model: str | None = None


# init_tools() で注入されるモジュール設定（起動時に一度だけ設定）。
# 複数ディレクトリ対応（scan_skills()/scan_agent_types() と同じマージ設計。
# 例: [*reversed(locohane_skills_dirs), skills_dir]。前方のディレクトリが優先される）。
_SKILLS_ROOTS: list[Path] | None = None
_SCRIPT_PYTHON: str = "python"
_SCRIPT_TIMEOUT: int = 60
_CODE_EXEC_ENABLED: bool = False
_SCRIPT_BACKGROUND_MAX_RUNTIME_SECONDS: int = 3600
_SCRIPT_BACKGROUND_JOB_RETENTION_SECONDS: int = 1800
_SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS: int = 20
_SCRIPT_BACKGROUND_MIN_POLL_MESSAGE: str = DEFAULT_SCRIPT_BACKGROUND_MIN_POLL_MESSAGE
_SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS: int = 0
_SCRIPT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS: int = 20
_SCRIPT_BACKGROUND_JOB_OUTPUT_TAIL_CHARS: int = 4000
_DEFAULT_WORKDIR: Path | None = None
# config.ini の [default_workdir].allow_sandbox_dir 由来。work_dir/
# default_workdir に加えて書き込みサンドボックスガードが許可する追加
# ディレクトリのエントリ集合（run_script/run_script_background 経由の
# み対象。execute_python_code系は対象外）。各エントリの allow_entries に
# 一致したスキルスクリプトからのみ書き込みが許可される
# （_subprocess_env.py の _allowed_sandbox_dirs_for()/_run_script_guard_env() 参照）。
_ALLOW_SANDBOX_DIRS: tuple[SandboxDirEntry, ...] = ()
_LLM_CONFIG: Config | None = None
_AGENT_TYPES: dict[str, ResolvedAgentType] = {}
_SUBAGENT_MAX_ITERATIONS: int = 6
_DISPATCH_AGENT_BACKGROUND_JOB_RETENTION_SECONDS: int = 1800
_DISPATCH_AGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS: int = 20
_DISPATCH_AGENT_BACKGROUND_MIN_POLL_MESSAGE: str = DEFAULT_DISPATCH_AGENT_BACKGROUND_MIN_POLL_MESSAGE
_DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS: int = 1800
_DISPATCH_AGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS: int = 20
_DISPATCH_AGENT_BACKGROUND_LLM_TIMEOUT_MAX_RETRIES: int = 3
_DISPATCH_AGENT_BATCH_MAX_GROUPS: int = 50
_DISPATCH_AGENT_BATCH_STOP_AFTER_UNREACHABLE_GROUPS: int = 1
_MEMORY_ROOT: Path | None = None
_PLANS_DIR: Path | None = None
_HELP_PATH: Path | None = None
_PATH_MEMORY_DIR: Path | None = None
_PATH_MEMORY_MAX_ENTRIES: int = 500
_PATH_MEMORY_SEARCH_MIN_SCORE: float = 0.3
_PATH_MEMORY_SEARCH_FILENAME_WEIGHT: float = 0.7
_SRC_DIR: Path = Path(__file__).parent.parent  # src/ディレクトリ（path_memory.py 等がある。このファイルはsrc/tools/にあるため一段上）
_APPROVAL_TIMEOUT_SECONDS: int = 300
_ASK_USER_QUESTION_TIMEOUT_SECONDS: int = 60
_ASK_USER_CHOICE_TIMEOUT_SECONDS: int = 90
_PLAN_BADGE_ALLOW_UNLOCK: bool = True
_PLAN_RESET_APPROVAL_ON_RECREATE: bool = True
_PLAN_AUTO_APPROVE: bool = False

# run_script は本来「書き込み系ツール」として一律に計画承認を要求するが、
# 副作用のない純粋な読み取り専用スクリプトはここに (skill_name, script_filename)
# を明示的に登録することで承認チェックを免除できる。init_tools() が config.ini の
# [plan].plan_approval_exempt_scripts から注入するため、ここでは空集合で初期化する。
_PLAN_APPROVAL_EXEMPT_SCRIPTS: set[tuple[str, str]] = set()


# 「無期限待ち」の代替として使う実質無期限の秒数（2^31-1 ≈ 68年）。
# chainlit.types.AskSpec は pydantic dataclass で timeout: int（Optional化されて
# いない）を要求するため、timeout=None を渡すと ValidationError
# 「timeout: Input should be a valid integer」がツール実行時の例外として
# LLMに見えてしまう（本番ログ 2026-07-25 で確認）。None は使わず、この定数を渡す。
_UNLIMITED_TIMEOUT_SECONDS = 2**31 - 1

def _resolve_ask_timeout(seconds: int) -> int:
    """config.ini のタイムアウト秒数を cl.Ask*Message(timeout=...) 用の値へ変換する。

    0以下（無期限待ち指定）は _UNLIMITED_TIMEOUT_SECONDS（実質無期限）に変換する。
    """
    return seconds if seconds > 0 else _UNLIMITED_TIMEOUT_SECONDS

def init_tools(
    skills_root: Path | str | Sequence[Path | str],
    script_python: str,
    script_timeout: int,
    llm_config: Config,
    agent_type_defs: list[AgentType],
    subagent_max_iterations: int,
    default_workdir: Path,
    memory_root: Path,
    help_path: Path,
    path_memory_dir: Path,
    path_memory_max_entries: int,
    code_exec_enabled: bool = False,
    approval_timeout_seconds: int = 300,
    ask_user_question_timeout_seconds: int = 60,
    ask_user_choice_timeout_seconds: int = 90,
    plan_badge_allow_unlock: bool = True,
    dispatch_agent_max_parallel: int = 1,
    graph_tool_max_parallel: int = 1,
    script_background_max_runtime_seconds: int = 3600,
    script_background_job_retention_seconds: int = 1800,
    script_background_min_poll_interval_seconds: int = 20,
    script_background_min_poll_message: str = DEFAULT_SCRIPT_BACKGROUND_MIN_POLL_MESSAGE,
    script_background_inline_wait_max_seconds: int = 0,
    script_background_progress_push_interval_seconds: int = 20,
    script_background_job_output_tail_chars: int = 4000,
    dispatch_agent_background_job_retention_seconds: int = 1800,
    dispatch_agent_background_min_poll_interval_seconds: int = 20,
    dispatch_agent_background_min_poll_message: str = DEFAULT_DISPATCH_AGENT_BACKGROUND_MIN_POLL_MESSAGE,
    dispatch_agent_background_inline_wait_max_seconds: int = 1800,
    dispatch_agent_background_progress_push_interval_seconds: int = 20,
    dispatch_agent_background_llm_timeout_max_retries: int = 3,
    dispatch_agent_batch_max_groups: int = 50,
    dispatch_agent_batch_stop_after_unreachable_groups: int = 1,
    plan_approval_exempt_scripts: Iterable[tuple[str, str]] = (),
    agent_type_run_script_allowlist: Iterable[tuple[str, str | tuple[str, str]]] | None = None,
    plans_dir: Path | None = None,
    plan_reset_approval_on_recreate: bool = True,
    plan_auto_approve: bool = False,
    allow_sandbox_dirs: Iterable[SandboxDirEntry] = (),
    path_memory_search_min_score: float = 0.3,
    path_memory_search_filename_weight: float = 0.7,
    skill_names: Sequence[str] = (),
) -> None:
    """ツールが使う設定を注入する（app 起動時に一度だけ呼ぶ）。

    read_skill / read_skill_file / run_script / dispatch_agent / メモリー系
    ツールはいずれもモジュール globals を参照するため、グラフ構築より前に
    必ずこの関数を呼んでおく必要がある。

    Args:
        skills_root: skills ディレクトリのルートパス、またはその並び。
            複数渡した場合は渡した順に探索され、前方のディレクトリが優先
            される（例: [*reversed(locohane_skills_dirs), skills_dir]）。各要素は
            resolve() により絶対パスへ正規化した上でモジュール変数に保持する。
        script_python: run_script が .py スクリプトを起動する際に使う
            Python 実行ファイル（例: "python", "C:\\path\\to\\python.exe"）。
        script_timeout: run_script のタイムアウト秒数。
        llm_config: dispatch_agent がサブエージェント用モデルを構築する
            際に使うアプリ設定（build_model に渡す）。
        agent_type_defs: scan_agent_types() が返したエージェント種別定義
            （system_prompt は {{skills}} 差し込み済みであること）。
            dispatch_agent の agent_type 引数で選べる種別一覧として、
            ツール名を実際の BaseTool に解決した上で _AGENT_TYPES に
            保持する。
        subagent_max_iterations: dispatch_agent の ReAct ループの
            最大反復回数。
        default_workdir: run_script の既定の作業ディレクトリ（cwd）。
            Chainlit の ChatSettings でセッション単位の作業ディレクトリが
            指定されなかった場合に使われる（config.ini の
            [default_workdir].dir 由来）。
        memory_root: 永続メモリーストアのルートディレクトリ（config.ini の
            [paths].memory_dir 由来）。create_memory 等のメモリー系
            ツールが参照する。
        help_path: help ツールが読み込んで返すヘルプ本文ファイルの絶対パス
            （config.ini の [paths].help_path 由来）。
        path_memory_dir: パスメモリー（src/path_memory.py）のレジストリ
            ファイル保存先ディレクトリ（config.ini の [path_memory].dir 由来）。
            run_script/execute_python_code のサブプロセスへ環境変数
            AGENT_PATH_MEMORY_DIR として渡すほか、Read/Glob/Grep/analyze_image/
            run_script の @N 解決でも使う。
        path_memory_max_entries: パスメモリー1会話あたりの登録上限件数
            （config.ini の [path_memory].max_entries 由来）。
        path_memory_search_min_score: パスメモリー類似検索で結果に出す類似度の下限
            （config.ini の [path_memory].search_min_score 由来）。
        path_memory_search_filename_weight: パスメモリー類似検索で、検索語がフルパスの
            場合にファイル名部分を重視する割合（config.ini の
            [path_memory].search_filename_weight 由来）。
        code_exec_enabled: execute_python_code ツール（LLMが生成した
            Pythonコードをその場で実行する）の有効/無効。False の場合、
            ツールは呼び出されてもエラー文字列を返すのみでコードは
            実行されない（config.ini の [scripts].code_execution_enabled 由来）。
        approval_timeout_seconds: create_plan/approve_plan の計画承認で
            ユーザーの応答を待つ秒数（config.ini の
            [user_response_timeouts].approval_seconds 由来）。0以下は無期限待ちを意味する。
        ask_user_question_timeout_seconds: AskUserQuestion（自由記述の
            質問。labels省略時は単発質問、labels指定時は複数項目フォーム）
            がユーザーの応答を待つ秒数（config.ini の
            [user_response_timeouts].ask_user_question_seconds 由来）。0以下は無期限待ちを意味する。
        ask_user_choice_timeout_seconds: ask_user_choice がユーザーの
            応答を待つ秒数（config.ini の
            [user_response_timeouts].ask_user_choice_seconds 由来）。0以下は無期限待ちを意味する。
        plan_badge_allow_unlock: 送信ボタン付近の Plan Mode / Edit Automatically
            バッジをクリックした際、Plan Mode → Edit Automatically 方向
            （ロック解除）も許可するか。False の場合はロック方向のクリックのみ
            有効になる（config.ini の [plan].allow_badge_unlock 由来）。
        dispatch_agent_max_parallel: dispatch_agent ツールの実LLM呼び出しを、
            1セッションあたり _DISPATCH_AGENT_SEMAPHORES で同時に何件まで
            許可するか。1以上はその値までにガードし（既定1＝完全直列化）、
            0以下はガードを無効化して並列呼び出しをそのまま許可する
            （config.ini の [subagent].max_parallel 由来）。
        graph_tool_max_parallel: メインエージェントの全ツール呼び出し
            （ImageAwareToolNode）を、1セッションあたり _TOOL_CALL_SEMAPHORES
            で同時に何件まで許可するか。1以上はその値までにガードし
            （既定1＝完全直列化）、0以下はガードを無効化して並列呼び出しを
            そのまま許可する（config.ini の [graph].max_parallel 由来）。
        script_background_max_runtime_seconds: run_script_background で
            起動したプロセスを強制終了するまでの上限秒数（config.ini の
            [scripts].background_max_runtime_seconds 由来）。
        script_background_job_retention_seconds: run_script_background の
            ジョブが終了後、check_script_job で一度も取得されないまま
            registry に残ってよい秒数（config.ini の
            [scripts].background_job_retention_seconds 由来）。
        script_background_min_poll_interval_seconds: check_script_job が
            「実行中」ステータスを返した直後、同じジョブへの次の
            check_script_job 呼び出しを許可するまでの最短間隔秒数。
            SKILL.md やツールのdocstringでLLMに「数秒おきに呼び直さない」
            よう指示しても、指示に従わないローカルLLMでは無視され得るため、
            サーバー側で強制する（config.ini の
            [scripts].background_min_poll_interval_seconds 由来）。
            0以下を指定すると強制を無効化する。
        script_background_min_poll_message: 上記の最短間隔未満で
            check_script_job が呼ばれた際にLLMへ返すメッセージのテンプレート
            （config.ini の [scripts].background_min_poll_message 由来）。
            .format() で {wait_remaining}（あと何秒待つべきか）/
            {job_id}（対象ジョブID、repr形式）/{min_interval}（設定値
            そのもの）を埋め込める。
        script_background_inline_wait_max_seconds: run_script_background/
            execute_python_code_background がジョブ完了をLLMを介さず
            コード側で待つ上限秒数（config.ini の
            [scripts].background_inline_wait_max_seconds 由来）。この秒数を
            超えてもまだ実行中の場合のみ job_id を返してLLMへ制御を戻す。
            ジョブ自体はこの上限に達してもキャンセルされず動き続ける。
            0以下を指定すると無期限に待つ（フォールバック経路が事実上無効に
            なる）。
        script_background_progress_push_interval_seconds: 上記の待機中、
            人間向けに経過秒数・標準出力/標準エラー末尾をチャットへ直接送る
            間隔（秒）。cl.Message送信のみでLLM呼び出しを伴わずトークンを
            消費しない（config.ini の
            [scripts].background_progress_push_interval_seconds 由来）。
        script_background_job_output_tail_chars: 上記の進捗表示、および
            check_script_job/stop_script_job/read_thread_note が末尾のみ
            表示する際の、標準出力/標準エラー/進捗メモの最大文字数
            （config.ini の [scripts].background_job_output_tail_chars 由来）。
            dispatch_agent の進捗表示も同じ値を共有する。
        dispatch_agent_background_job_retention_seconds: dispatch_agent の
            ジョブが終了後、check_dispatch_agent_job で一度も取得されないまま
            registry に残ってよい秒数（config.ini の
            [subagent].background_job_retention_seconds 由来）。
        dispatch_agent_background_min_poll_interval_seconds: check_dispatch_agent_job が
            「実行中」ステータスを返した直後、同じジョブへの次の
            check_dispatch_agent_job 呼び出しを許可するまでの最短間隔秒数
            （config.ini の [subagent].background_min_poll_interval_seconds 由来。
            script_background_min_poll_interval_seconds と同じ理由でサーバー側強制）。
            0以下を指定すると強制を無効化する。
        dispatch_agent_background_min_poll_message: 上記の最短間隔未満で
            check_dispatch_agent_job が呼ばれた際にLLMへ返すメッセージの
            テンプレート（config.ini の [subagent].background_min_poll_message 由来）。
            プレースホルダーは script_background_min_poll_message と同じ。
        dispatch_agent_background_inline_wait_max_seconds: dispatch_agent
            がジョブ完了をLLMを介さずコード側で待つ上限秒数（config.ini の
            [subagent].background_inline_wait_max_seconds 由来）。この秒数を
            超えてもまだ実行中の場合のみ job_id を返してLLMへ制御を戻す。
            ジョブ自体はこの上限に達してもキャンセルされず動き続ける。
            0以下を指定すると無期限に待つ（フォールバック経路が事実上無効になる）。
        dispatch_agent_background_progress_push_interval_seconds: 上記の待機中、
            人間向けに経過秒数・反復回数・進捗メモ末尾をチャットへ直接送る間隔
            （秒）。cl.Message送信のみでLLM呼び出しを伴わずトークンを消費しない
            （config.ini の [subagent].background_progress_push_interval_seconds 由来）。
        dispatch_agent_background_llm_timeout_max_retries: dispatch_agent
            実行中のLLM呼び出しがタイムアウトした場合、モデルを再構築してから
            同じ反復を再試行する最大回数（config.ini の
            [subagent].background_llm_timeout_max_retries 由来）。dispatch_agent は
            常にこの設定を使う（旧・同期版が使っていた即時打ち切りは廃止済み）。
        dispatch_agent_batch_max_groups: dispatch_agent_batch が1回の呼び出しで
            起動してよいグループ数の上限。0以下なら無制限（config.ini の
            [subagent].batch_max_groups 由来）。
        dispatch_agent_batch_stop_after_unreachable_groups: dispatch_agent_batch で
            LLMのタイムアウト・通信エラーにより打ち切られたグループがこの数に
            達したら、未起動のグループを起動しない。0以下なら無効（config.ini の
            [subagent].batch_stop_after_unreachable_groups 由来）。
        plan_approval_exempt_scripts: run_script/run_script_background の
            計画承認（Plan Mode）を免除する、副作用のない読み取り専用
            スクリプトのホワイトリスト。(skill_name, script_filename) の
            並び（config.ini の [plan].plan_approval_exempt_scripts 由来）。
        agent_type_run_script_allowlist: dispatch_agent サブエージェントの
            agent_type ごとに run_script で呼んでよい
            スキル/スクリプトを制限するホワイトリスト。(agent_type, 対象) の
            並び（config.ini の [subagent].agent_type_run_script_allowlist 由来）。
            対象はスキル名の文字列（そのスキル配下の全スクリプトを許可）、
            または (skill_name, script_filename) のタプル（そのスクリプト
            のみ許可）。None を渡すとモジュール既定値
            （_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST の初期値）を維持する。
        plans_dir: create_plan が detail_markdown 引数を渡した際に詳細計画
            Markdownを書き出す保存先ディレクトリ（config.ini の
            [paths].plans_dir 由来）。None の場合は detail_markdown を
            渡してもファイル保存をスキップする。
        plan_reset_approval_on_recreate: 既に Edit Automatically（計画承認済み）
            の状態で create_plan が再度呼ばれた際、plan_approved を強制的に
            False へ戻す（Plan Mode へ戻す）か。True（既定）なら常に戻し
            approve_plan による再承認を必須にする。False なら承認済み状態を
            維持したまま steps だけ差し替える（未承認状態からの呼び出しは
            この設定に関わらず常に Plan Mode のまま）。
            （config.ini の [plan].reset_approval_on_recreate 由来）。
        plan_auto_approve: True の場合、approve_plan 呼び出し時にユーザーへの
            確認（承認/却下ボタンの表示・応答待ち）を一切行わず、その場で
            自動的に承認済み扱いにする。False（既定）なら従来通りユーザーの
            明示的な承認を必須にする（config.ini の [plan].auto_approve 由来）。
        allow_sandbox_dirs: 書き込みサンドボックスガード（run_script/
            run_script_background 経由のみ対象。execute_python_code系は
            対象外）が work_dir/default_workdir に加えて書き込み・削除を
            許可する追加ディレクトリのエントリ集合（config.ini の
            [default_workdir].allow_sandbox_dir 由来）。各エントリの
            allow_entries に一致したスキルスクリプトからのみ実際に書き込み
            が許可される（allow_entries が空なら対象を問わず無制限に許可）。
            既定は空で、その場合従来通りの書き込みサンドボックス原則が
            そのまま適用される。
        skill_names: scan_skills() が返した有効なスキル名の並び。read_skill の
            skill_name 引数をこの選択肢（enum）に制約する（read_skill.py の
            apply_skill_name_enum）。空の場合は制約しない。

    Returns:
        None。副作用としてモジュール globals を更新するのみ。
    """
    global _SKILLS_ROOTS, _SCRIPT_PYTHON, _SCRIPT_TIMEOUT
    global _CODE_EXEC_ENABLED
    global _SCRIPT_BACKGROUND_MAX_RUNTIME_SECONDS, _SCRIPT_BACKGROUND_JOB_RETENTION_SECONDS
    global _SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS, _SCRIPT_BACKGROUND_MIN_POLL_MESSAGE
    global _SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS, _SCRIPT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS
    global _DISPATCH_AGENT_BACKGROUND_JOB_RETENTION_SECONDS, _DISPATCH_AGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS
    global _DISPATCH_AGENT_BACKGROUND_MIN_POLL_MESSAGE
    global _DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS, _DISPATCH_AGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS
    global _DISPATCH_AGENT_BACKGROUND_LLM_TIMEOUT_MAX_RETRIES, _DISPATCH_AGENT_BATCH_MAX_GROUPS
    global _DISPATCH_AGENT_BATCH_STOP_AFTER_UNREACHABLE_GROUPS
    global _DEFAULT_WORKDIR, _LLM_CONFIG, _AGENT_TYPES, _SUBAGENT_MAX_ITERATIONS
    global _ALLOW_SANDBOX_DIRS
    global _MEMORY_ROOT
    global _PLANS_DIR
    global _HELP_PATH
    global _PATH_MEMORY_DIR, _PATH_MEMORY_MAX_ENTRIES
    global _PATH_MEMORY_SEARCH_MIN_SCORE, _PATH_MEMORY_SEARCH_FILENAME_WEIGHT
    global _APPROVAL_TIMEOUT_SECONDS, _ASK_USER_QUESTION_TIMEOUT_SECONDS
    global _ASK_USER_CHOICE_TIMEOUT_SECONDS
    global _PLAN_BADGE_ALLOW_UNLOCK
    global _PLAN_RESET_APPROVAL_ON_RECREATE
    global _PLAN_AUTO_APPROVE
    global _DISPATCH_AGENT_MAX_PARALLEL
    global _TOOL_CALL_MAX_PARALLEL
    global _PLAN_APPROVAL_EXEMPT_SCRIPTS
    global _AGENT_TYPE_RUN_SCRIPT_ALLOWLIST
    _skills_root_list = [skills_root] if isinstance(skills_root, (str, Path)) else list(skills_root)
    _SKILLS_ROOTS = [Path(p).resolve() for p in _skills_root_list]
    # ユーザー別ドラフト（[skill_creator].draft_dir）は最後尾＝最低優先のルートにする。
    # ドラフトは `<ユーザー名>/<スキル名>` で参照されるため正式スキル名と衝突せず、
    # 他ユーザー分へのアクセス可否は _safe_path() 側で判定する（src/skill_drafts.py）。
    _draft_dir = getattr(llm_config, "skill_draft_dir", None)
    if _draft_dir is not None and Path(_draft_dir).resolve() not in _SKILLS_ROOTS:
        _SKILLS_ROOTS.append(Path(_draft_dir).resolve())
    _SCRIPT_PYTHON = script_python
    _SCRIPT_TIMEOUT = script_timeout
    _CODE_EXEC_ENABLED = code_exec_enabled
    _SCRIPT_BACKGROUND_MAX_RUNTIME_SECONDS = script_background_max_runtime_seconds
    _SCRIPT_BACKGROUND_JOB_RETENTION_SECONDS = script_background_job_retention_seconds
    _SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS = script_background_min_poll_interval_seconds
    _SCRIPT_BACKGROUND_MIN_POLL_MESSAGE = script_background_min_poll_message
    _SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS = script_background_inline_wait_max_seconds
    _SCRIPT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS = script_background_progress_push_interval_seconds
    _DISPATCH_AGENT_BACKGROUND_JOB_RETENTION_SECONDS = dispatch_agent_background_job_retention_seconds
    _DISPATCH_AGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS = dispatch_agent_background_min_poll_interval_seconds
    _DISPATCH_AGENT_BACKGROUND_MIN_POLL_MESSAGE = dispatch_agent_background_min_poll_message
    _DISPATCH_AGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS = dispatch_agent_background_inline_wait_max_seconds
    _DISPATCH_AGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS = dispatch_agent_background_progress_push_interval_seconds
    _DISPATCH_AGENT_BACKGROUND_LLM_TIMEOUT_MAX_RETRIES = dispatch_agent_background_llm_timeout_max_retries
    _DISPATCH_AGENT_BATCH_MAX_GROUPS = dispatch_agent_batch_max_groups
    _DISPATCH_AGENT_BATCH_STOP_AFTER_UNREACHABLE_GROUPS = dispatch_agent_batch_stop_after_unreachable_groups
    _PLAN_APPROVAL_EXEMPT_SCRIPTS = set(plan_approval_exempt_scripts)
    if agent_type_run_script_allowlist is not None:
        grouped: dict[str, set[str | tuple[str, str]]] = {}
        for agent_type, target in agent_type_run_script_allowlist:
            grouped.setdefault(agent_type, set()).add(target)
        _AGENT_TYPE_RUN_SCRIPT_ALLOWLIST = {agent_type: frozenset(targets) for agent_type, targets in grouped.items()}
    _DEFAULT_WORKDIR = Path(default_workdir).resolve()
    _ALLOW_SANDBOX_DIRS = tuple(allow_sandbox_dirs)
    _LLM_CONFIG = llm_config
    _AGENT_TYPES = _resolve_agent_types(agent_type_defs)
    _SUBAGENT_MAX_ITERATIONS = subagent_max_iterations
    _MEMORY_ROOT = Path(memory_root).resolve()
    memory.ensure_dirs(_MEMORY_ROOT)
    _PLANS_DIR = Path(plans_dir).resolve() if plans_dir else None
    if _PLANS_DIR is not None:
        _PLANS_DIR.mkdir(parents=True, exist_ok=True)
    _HELP_PATH = Path(help_path).resolve()
    _PATH_MEMORY_DIR = Path(path_memory_dir).resolve()
    _PATH_MEMORY_MAX_ENTRIES = path_memory_max_entries
    _PATH_MEMORY_SEARCH_MIN_SCORE = path_memory_search_min_score
    _PATH_MEMORY_SEARCH_FILENAME_WEIGHT = path_memory_search_filename_weight
    _APPROVAL_TIMEOUT_SECONDS = approval_timeout_seconds
    _ASK_USER_QUESTION_TIMEOUT_SECONDS = ask_user_question_timeout_seconds
    _ASK_USER_CHOICE_TIMEOUT_SECONDS = ask_user_choice_timeout_seconds
    _PLAN_BADGE_ALLOW_UNLOCK = plan_badge_allow_unlock
    _PLAN_RESET_APPROVAL_ON_RECREATE = plan_reset_approval_on_recreate
    _PLAN_AUTO_APPROVE = plan_auto_approve
    _DISPATCH_AGENT_MAX_PARALLEL = dispatch_agent_max_parallel
    _TOOL_CALL_MAX_PARALLEL = graph_tool_max_parallel
    # 設定値の変更（再初期化）に追従できるよう、以前の値で生成済みの
    # セッション毎 Semaphore を破棄する。既に待機/取得中のタスクがあっても、
    # 参照を持つ限りその Semaphore オブジェクト自体は生きたまま使われ続ける
    # ため、ここでの clear() が実行中の待ち合わせを壊すことはない。
    _DISPATCH_AGENT_SEMAPHORES.clear()
    _TOOL_CALL_SEMAPHORES.clear()
    # 各ツールの description（LLMに見えるツールスキーマ説明）内の ${変数名} を
    # config.ini の実値へ展開する。@tool デコレータが docstring から description を
    # 設定するのは import 時の一度きりなので、ここで書き換えないと LLM には
    # プレースホルダーがそのまま見えてしまう。BaseTool（pydanticモデル）は
    # hashable とは限らないため、set化ではなく id() で重複を除いて回す。
    # MCPサーバー由来のツール（registry._MCP_TOOLS）はここでは対象外にする。description が
    # 外部サーバー由来のため、無関係な ${...} パターンを誤って展開しようとして
    # ValueError を送出するリスクがあるため（register_mcp_tools/get_all_tools 参照）。
    seen_tool_ids: set[int] = set()
    for tool_obj in [*registry._BASE_TOOLS, *registry._SUBAGENT_TOOLS]:
        if id(tool_obj) in seen_tool_ids:
            continue
        seen_tool_ids.add(id(tool_obj))
        tool_obj.description = expand_config_vars(tool_obj.description, llm_config)
    # read_skill.py は _duplicate_guard 経由でこのモジュールを import するため、
    # モジュール先頭で import すると循環 import になる（registry は import 済みの
    # read_skill を参照するだけなので問題ない）。
    from .read_skill import apply_skill_name_enum

    apply_skill_name_enum(skill_names)

def _resolve_agent_types(agent_type_defs: list[AgentType]) -> dict[str, ResolvedAgentType]:
    """AgentType のツール名一覧を実際の BaseTool へ解決する。

    tool_names が None（frontmatterで tools: 省略）の場合は、サブエージェント
    に割当可能な既定ツール一式（_SUBAGENT_TOOLS）をそのまま継承させる
    （Anthropic公式のサブエージェント仕様における「tools省略時は全ツール
    継承」を、本プロジェクトのセキュリティ境界内で読み替えたもの）。
    未知のツール名は例外を投げず警告してスキップする（scan_agent_types()
    と同じフェイルオープンしない方針）。

    Args:
        agent_type_defs: scan_agent_types() が返した定義のリスト
            （system_prompt は {{skills}} 差し込み済みであること）。

    Returns:
        エージェント種別名 -> ResolvedAgentType の辞書。
    """
    tool_lookup = {t.name: t for t in registry._SUBAGENT_TOOLS}
    resolved: dict[str, ResolvedAgentType] = {}
    for agent_def in agent_type_defs:
        if agent_def.tool_names is None:
            tools = list(registry._SUBAGENT_TOOLS)
        else:
            tools = []
            for tool_name in agent_def.tool_names:
                tool_obj = tool_lookup.get(tool_name)
                if tool_obj is None:
                    logger.warning(
                        "Agent type '%s': skipping unknown tool '%s'",
                        agent_def.name,
                        tool_name,
                    )
                    continue
                tools.append(tool_obj)
        resolved[agent_def.name] = ResolvedAgentType(
            description=agent_def.description,
            system_prompt=agent_def.system_prompt,
            tools=tools,
            model=agent_def.model,
        )
    return resolved
