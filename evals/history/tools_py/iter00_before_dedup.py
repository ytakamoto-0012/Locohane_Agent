"""LangGraph ツール（progressive disclosure 第2・第3段階）。

仕様: https://agentskills.io/specification

LangChain の @tool として定義する。read_skill/read_skill_file/run_script/view_image は
すべて LangGraph のツールコールとして実行され、グラフのトレースに乗る
（Chainlit 側で可視化するため）。dispatch_agent のみ内部で独立した ReAct
ループ（subagent.py）を回す特殊なツールで、その内部の呼び出しはグラフの
トレースに乗らない（意図的、コンテキスト節約のため）。

- read_skill      … 第2段階(Read):    SKILL.md 本文全体を読む
- read_skill_file       … 第3段階(Execute): references/assets 等を必要時に読む
- run_script      … 第3段階(Execute): scripts/ 配下のスクリプトを実行する
- execute_python_code … 第3段階(Execute): LLMが生成したPythonコードをその場で実行する
- get_tool_source … 第3段階(Execute): run_script が失敗した際、原因調査用にスクリプトの絶対パスを返す
- view_image      … 第3段階(Execute): 画像ファイルをVision対応モデルへ見せる
- dispatch_agent  … タスクをサブエージェントへ委譲し、最終回答のみを受け取る
- ask_user_text   … ユーザーへ自由記述で追加質問する（Chainlit AskUserMessage）
- ask_user_choice … ユーザーへ選択肢形式で追加質問する（Chainlit AskActionMessage）
- create_memory / update_memory / delete_memory / read_memory / search_memory /
  list_memories … スレッドをまたぐ永続メモリー（src/memory.py）の読み書き。
  主エージェントのみに公開し、dispatch_agent のサブエージェントには渡さない。

セキュリティ: read_skill_file / run_script / get_tool_source は必ず skills
ディレクトリ配下に限定する（_safe_path でディレクトリトラバーサルを拒否）。保存・実行の
パスがコードから追える事。view_image は skills ディレクトリ配下に加え、run_script と
同じ作業ディレクトリ（_resolve_workdir）配下の画像も許可する（_resolve_view_image_path）。
メモリー系ツールも同様に memory.py 側の _safe_memory_path で memory ルート配下に限定する。

設定（skills ルート・Python 実行ファイル・タイムアウト・サブエージェント設定・
メモリールート）はモジュール globals に init_tools() で一度だけ注入する。動的 import
やメタクラス等の仕掛けは使わない。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import chainlit as cl
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolNode

from . import memory
from .agent_types import AgentType
from .config import Config
from .images import image_followup_message, is_image_file, to_data_url
from .subagent import run_subagent

logger = logging.getLogger(__name__)


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


# init_tools() で注入されるモジュール設定（起動時に一度だけ設定）。
_SKILLS_ROOT: Path | None = None
_SCRIPT_PYTHON: str = "python"
_SCRIPT_TIMEOUT: int = 60
_SCRIPT_REQUIRE_APPROVAL: bool = True
_CODE_EXEC_ENABLED: bool = False
_CODE_EXEC_REQUIRE_APPROVAL: bool = True
_DEFAULT_WORKDIR: Path | None = None
_LLM_CONFIG: Config | None = None
_AGENT_TYPES: dict[str, ResolvedAgentType] = {}
_SUBAGENT_MAX_ITERATIONS: int = 6
_MEMORY_ROOT: Path | None = None
_HELP_PATH: Path | None = None
_PATH_MEMORY_DIR: Path | None = None
_PATH_MEMORY_MAX_ENTRIES: int = 500
_PATH_MEMORY_REGISTRY_MODULE = None  # _load_path_memory_registry() でキャッシュ
_APPROVAL_TIMEOUT_SECONDS: int = 300
_ASK_USER_TEXT_TIMEOUT_SECONDS: int = 60
_ASK_USER_CHOICE_TIMEOUT_SECONDS: int = 90


def init_tools(
    skills_root: Path,
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
    script_require_approval: bool = True,
    code_exec_enabled: bool = False,
    code_exec_require_approval: bool = True,
    approval_timeout_seconds: int = 300,
    ask_user_text_timeout_seconds: int = 60,
    ask_user_choice_timeout_seconds: int = 90,
) -> None:
    """ツールが使う設定を注入する（app 起動時に一度だけ呼ぶ）。

    read_skill / read_skill_file / run_script / dispatch_agent / メモリー系
    ツールはいずれもモジュール globals を参照するため、グラフ構築より前に
    必ずこの関数を呼んでおく必要がある。

    Args:
        skills_root: skills ディレクトリのルートパス。resolve() により
            絶対パスへ正規化した上でモジュール変数に保持する。
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
            [paths].default_workdir 由来）。
        memory_root: 永続メモリーストアのルートディレクトリ（config.ini の
            [paths].memory_dir 由来）。create_memory 等のメモリー系
            ツールが参照する。
        help_path: help ツールが読み込んで返すヘルプ本文ファイルの絶対パス
            （config.ini の [paths].help_path 由来）。
        path_memory_dir: パスメモリー（skills/path-memory）のレジストリ
            ファイル保存先ディレクトリ（config.ini の [path_memory].dir 由来）。
            run_script/execute_python_code のサブプロセスへ環境変数
            AGENT_PATH_MEMORY_DIR として渡すほか、view_image/run_script の
            @N 解決（_load_path_memory_registry 経由）でも使う。
        path_memory_max_entries: パスメモリー1会話あたりの登録上限件数
            （config.ini の [path_memory].max_entries 由来）。
        script_require_approval: run_script の実行前にユーザー承認を
            求めるかどうか。False にすると確認なしで即実行する
            （開発時のスキップ用途）。
        code_exec_enabled: execute_python_code ツール（LLMが生成した
            Pythonコードをその場で実行する）の有効/無効。False の場合、
            ツールは呼び出されてもエラー文字列を返すのみでコードは
            実行されない（config.ini の [scripts].code_execution_enabled 由来）。
        code_exec_require_approval: execute_python_code の実行前に
            ユーザー承認を求めるかどうか。False にすると確認なしで
            即実行する（script_require_approval とは別の専用設定、
            config.ini の [scripts].code_execution_require_approval 由来）。
        approval_timeout_seconds: create_plan/approve_plan の計画承認、
            および run_script/execute_python_code の個別実行確認で
            ユーザーの応答を待つ秒数（config.ini の
            [timeouts].approval_seconds 由来）。
        ask_user_text_timeout_seconds: ask_user_text がユーザーの応答を
            待つ秒数（config.ini の [timeouts].ask_user_text_seconds 由来）。
        ask_user_choice_timeout_seconds: ask_user_choice がユーザーの
            応答を待つ秒数（config.ini の
            [timeouts].ask_user_choice_seconds 由来）。

    Returns:
        None。副作用としてモジュール globals を更新するのみ。
    """
    global _SKILLS_ROOT, _SCRIPT_PYTHON, _SCRIPT_TIMEOUT, _SCRIPT_REQUIRE_APPROVAL
    global _CODE_EXEC_ENABLED, _CODE_EXEC_REQUIRE_APPROVAL
    global _DEFAULT_WORKDIR, _LLM_CONFIG, _AGENT_TYPES, _SUBAGENT_MAX_ITERATIONS
    global _MEMORY_ROOT
    global _HELP_PATH
    global _PATH_MEMORY_DIR, _PATH_MEMORY_MAX_ENTRIES
    global _APPROVAL_TIMEOUT_SECONDS, _ASK_USER_TEXT_TIMEOUT_SECONDS
    global _ASK_USER_CHOICE_TIMEOUT_SECONDS
    _SKILLS_ROOT = Path(skills_root).resolve()
    _SCRIPT_PYTHON = script_python
    _SCRIPT_TIMEOUT = script_timeout
    _SCRIPT_REQUIRE_APPROVAL = script_require_approval
    _CODE_EXEC_ENABLED = code_exec_enabled
    _CODE_EXEC_REQUIRE_APPROVAL = code_exec_require_approval
    _DEFAULT_WORKDIR = Path(default_workdir).resolve()
    _LLM_CONFIG = llm_config
    _AGENT_TYPES = _resolve_agent_types(agent_type_defs)
    _SUBAGENT_MAX_ITERATIONS = subagent_max_iterations
    _MEMORY_ROOT = Path(memory_root).resolve()
    memory.ensure_dirs(_MEMORY_ROOT)
    _HELP_PATH = Path(help_path).resolve()
    _PATH_MEMORY_DIR = Path(path_memory_dir).resolve()
    _PATH_MEMORY_MAX_ENTRIES = path_memory_max_entries
    _APPROVAL_TIMEOUT_SECONDS = approval_timeout_seconds
    _ASK_USER_TEXT_TIMEOUT_SECONDS = ask_user_text_timeout_seconds
    _ASK_USER_CHOICE_TIMEOUT_SECONDS = ask_user_choice_timeout_seconds


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
    tool_lookup = {t.name: t for t in _SUBAGENT_TOOLS}
    resolved: dict[str, ResolvedAgentType] = {}
    for agent_def in agent_type_defs:
        if agent_def.tool_names is None:
            tools = list(_SUBAGENT_TOOLS)
        else:
            tools = []
            for tool_name in agent_def.tool_names:
                tool_obj = tool_lookup.get(tool_name)
                if tool_obj is None:
                    logger.warning(
                        "エージェント種別 '%s': 未知のツール '%s' をスキップ",
                        agent_def.name,
                        tool_name,
                    )
                    continue
                tools.append(tool_obj)
        resolved[agent_def.name] = ResolvedAgentType(
            description=agent_def.description,
            system_prompt=agent_def.system_prompt,
            tools=tools,
        )
    return resolved


_PATH_MEMORY_TOKEN_RE = re.compile(r"^@(\d+)$")


def _load_path_memory_registry():
    """path-memory スキルの scripts/_registry.py を動的ロードしてキャッシュを返す。

    view_image/run_script が `@N` 形式のパスメモリー参照を解決する際に使う。
    skills/path-memory は独立した Agent Skill として自己完結させたいため、
    src/tools.py（アプリ基盤側）はこのモジュールを importlib 経由で薄く
    参照するのみで、file-tools 等スキルの実装詳細は一切知らない。

    Returns:
        _registry モジュール（register/resolve/list_entries 関数を持つ）。
        path-memory スキルが見つからない・ロードに失敗した場合は None
        （呼び出し側はパスメモリー機能を使わずフォールバックする）。
    """
    global _PATH_MEMORY_REGISTRY_MODULE
    if _PATH_MEMORY_REGISTRY_MODULE is not None:
        return _PATH_MEMORY_REGISTRY_MODULE
    if _SKILLS_ROOT is None:
        return None
    registry_path = _SKILLS_ROOT / "path-memory" / "scripts" / "_registry.py"
    if not registry_path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_path_memory_registry", registry_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - 動的ロード失敗時はパスメモリーを無効化するのみ
        logger.exception("path-memory の _registry.py ロードに失敗しました")
        return None
    _PATH_MEMORY_REGISTRY_MODULE = module
    return module


def _resolve_path_memory_token(value: str) -> tuple[str, str | None]:
    """value が `@N` 形式のパスメモリー参照なら実パスへ解決する。

    Args:
        value: view_image の relative_path や run_script の script_args の
            要素として渡された文字列。

    Returns:
        (解決後の値, エラーメッセージ) のタプル。
        - `@N` 形式でなければ (value, None)（従来通りそのまま使う）。
        - `@N` 形式で解決できれば (実パス, None)。
        - `@N` 形式だがパスメモリーが利用できない・未登録の場合は
          (value, "パスメモリー ... は登録されていません。..." というエラー文)。
          呼び出し側はこのエラー文をそのまま「エラー: ...」として返すこと。
    """
    if not _PATH_MEMORY_TOKEN_RE.match(value):
        return value, None
    registry = _load_path_memory_registry()
    if registry is None or _PATH_MEMORY_DIR is None:
        return value, f"パスメモリー機能が利用できません: {value}"
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    resolved = registry.resolve(thread_id, value, _PATH_MEMORY_DIR)
    if resolved is None:
        return value, (
            f"パスメモリー {value} は登録されていません。"
            "path-memory スキルの list_path_memory.py で現在の登録内容を確認してください。"
        )
    return resolved, None


_PATH_MEMORY_TEXT_TOKEN_RE = re.compile(r"(?<![\w@])@(\d+)\b")


def _resolve_path_memory_tokens_in_text(text: str) -> str:
    """自由記述テキスト中に埋め込まれた `@N` パスメモリー参照を実パスへ置換する。

    dispatch_agent の task 引数のように、モデルが自然文の中でパスに触れる場面では
    文字列全体が `@N` のみであることを前提とする _resolve_path_memory_token は使えない。
    このヘルパーは文中に複数含まれうる `@N` を正規表現で検出し、解決できたものだけを
    実パスへ置き換える。未登録・パスメモリー機能が使えない等で解決できないトークンは
    エラーにせず元の `@N` 文字列のまま残す（自由文の一部が理由で呼び出し自体を
    失敗させないため）。

    Args:
        text: `@N` を含みうる自由記述テキスト（dispatch_agent の task 等）。

    Returns:
        解決できた `@N` を実パスへ置換したテキスト。`@N` を含まない、または
        どれも解決できなかった場合は text をそのまま返す。
    """

    def _replace(match: re.Match) -> str:
        token = match.group(0)
        resolved, error = _resolve_path_memory_token(token)
        return token if error else resolved

    return _PATH_MEMORY_TEXT_TOKEN_RE.sub(_replace, text)


def _subprocess_env() -> dict[str, str]:
    """run_script/execute_python_code の子プロセスへ渡す環境変数を組み立てる。

    既存の PYTHONIOENCODING（日本語文字化け対策）に加え、パスメモリー
    （skills/path-memory）用の AGENT_THREAD_ID/AGENT_PATH_MEMORY_DIR/
    AGENT_PATH_MEMORY_MAX_ENTRIES を注入する。file-tools 等のスクリプトは
    これらを `_registry.env_params()` で読み、自分が出力するパスを
    レジストリへ登録する（skills/file-tools/scripts/_common.py 参照）。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env["AGENT_THREAD_ID"] = cl.user_session.get("thread_id") or "_no_session"
    if _PATH_MEMORY_DIR is not None:
        env["AGENT_PATH_MEMORY_DIR"] = str(_PATH_MEMORY_DIR)
    env["AGENT_PATH_MEMORY_MAX_ENTRIES"] = str(_PATH_MEMORY_MAX_ENTRIES)
    return env


def _safe_path(relative: str) -> Path:
    """skills ルート配下に限定した絶対パスを返す。境界外なら ValueError。

    ディレクトリトラバーサル対策の中核。relative に ".." やシンボリック
    リンク経由の脱出が含まれていても、resolve() で正規化した上で
    is_relative_to() により境界を検証するため、skills ルート外への
    アクセスは常に拒否される。

    Args:
        relative: skills ルートからの相対パス（例: "word-counter/SKILL.md"）。

    Returns:
        skills ルート配下に解決された絶対パス（Path）。

    Raises:
        RuntimeError: init_tools() が未実行で _SKILLS_ROOT が None の場合。
        ValueError: 解決後のパスが skills ルート配下に収まらない場合
            （ディレクトリトラバーサルの試行とみなす）。
    """
    if _SKILLS_ROOT is None:
        raise RuntimeError("init_tools() が未実行です")
    # resolve() でシンボリックリンクや .. を正規化した上で境界を検証する。
    candidate = (_SKILLS_ROOT / relative).resolve()
    if not candidate.is_relative_to(_SKILLS_ROOT):
        raise ValueError(f"skills ディレクトリ外へのアクセスは許可されません: {relative}")
    return candidate


@tool
def read_skill(skill_name: str) -> str:
    """スキルの SKILL.md 本文全体を読み込んで返す。

    ユーザーの要求に合致するスキルを選んだら、まずこのツールで本文（手順）を読むこと。
    Agent Skills 標準の progressive disclosure における第2段階（Read）に相当する。

    Args:
        skill_name: 読み込むスキルのフォルダ名（= SKILL.md の name）。

    Returns:
        SKILL.md の本文全体（UTF-8 テキスト）。skill_name が skills ルート外を
        指す場合や SKILL.md が存在しない場合は、例外を送出せず
        「エラー: ...」形式の文字列を返す（LLM がそのまま読める形にするため）。
    """
    try:
        skill_md = _safe_path(f"{skill_name}/SKILL.md")
    except ValueError as e:
        return f"エラー: {e}"
    if not skill_md.is_file():
        return f"エラー: スキル '{skill_name}' の SKILL.md が見つかりません。"
    logger.info("read_skill: %s", skill_name)
    return skill_md.read_text(encoding="utf-8")


@tool
def read_skill_file(relative_path: str) -> str:
    """skills ディレクトリ配下のファイルを読み込んで返す。

    SKILL.md 本文が references/assets を参照している場合など、必要時のみ使う。
    Agent Skills 標準の progressive disclosure における第3段階（Execute）の一部。

    Args:
        relative_path: skills ルートからの相対パス（例: word-counter/references/notes.md）。

    Returns:
        ファイル内容（UTF-8、デコード不能なバイト列は errors="replace" で置換）。
        skills ルート外を指す場合やファイルが存在しない場合は、例外を送出せず
        「エラー: ...」形式の文字列を返す。
    """
    try:
        path = _safe_path(relative_path)
    except ValueError as e:
        return f"エラー: {e}"
    if not path.is_file():
        return (
            f"エラー: ファイルが見つかりません: {relative_path}"
            "（read_skill_file は skills ディレクトリ配下限定です。作業ディレクトリ配下の"
            "ファイルを読みたい場合は file-tools の read_file.py を run_script で使ってください）"
        )
    logger.info("read_skill_file: %s", relative_path)
    return path.read_text(encoding="utf-8", errors="replace")


@tool
def get_tool_source(skill_name: str, script: str) -> str:
    """run_script で実行したスクリプトの絶対パスを返す（中身は返さない）。

    run_script がエラー（非0終了コード・スタックトレース）を返した場合の原因調査に使う。
    このツールでソースファイルの絶対パスを取得し、必要なら read_skill_file で中身を
    確認するか、execute_python_code 内で `sys.path.insert(0, "<このパスの親ディレクトリ>")`
    のようにして _common.py 等の同スキル内ヘルパーモジュールを直接 import して調査・
    代替コードの実行に使う。

    Args:
        skill_name: スクリプトを持つスキルのフォルダ名。
        script: スキルフォルダからの相対パス（必ず scripts/ 配下、例: scripts/read_file.py）。
            scripts/ で始まらないパスは拒否される。

    Returns:
        スクリプトの絶対パス文字列。パスが skills ルート外を指す場合、scripts/ 配下で
        ない場合、スクリプトが存在しない場合は、例外を送出せず「エラー: ...」形式の
        文字列を返す。
    """
    if not (script.startswith("scripts/") or script.startswith("scripts\\")):
        return "エラー: 対象にできるのは scripts/ 配下のスクリプトのみです。"
    try:
        script_path = _safe_path(f"{skill_name}/{script}")
    except ValueError as e:
        return f"エラー: {e}"
    if not script_path.is_file():
        return f"エラー: スクリプトが見つかりません: {skill_name}/{script}"
    logger.info("get_tool_source: %s/%s", skill_name, script)
    return str(script_path)


def _resolve_workdir() -> Path:
    """run_script が subprocess.run に渡す cwd を決定する。

    Chainlit の ChatSettings（歯車アイコン）でユーザーがセッションに
    作業ディレクトリを設定していればそれを使い（app.py の
    on_settings_update が cl.user_session["work_dir"] に絶対パス文字列を
    保存する）、未設定なら config.ini の [paths].default_workdir
    （init_tools() で注入された _DEFAULT_WORKDIR）にフォールバックする。

    read_skill / read_skill_file / スクリプト本体の場所解決には影響しない
    （それらは常に _safe_path 経由で skills ルート配下に固定される）。
    セキュリティ境界は意図的にかけない（ユーザー入力の絶対パスを信頼する）。

    Returns:
        subprocess.run(cwd=...) に渡す絶対パス。

    Raises:
        RuntimeError: init_tools() が未実行で _DEFAULT_WORKDIR が None の場合。
    """
    if _DEFAULT_WORKDIR is None:
        raise RuntimeError("init_tools() が未実行です")
    work_dir = cl.user_session.get("work_dir")
    return Path(work_dir) if work_dir else _DEFAULT_WORKDIR


def _resolve_view_image_path(raw: str) -> Path:
    """view_image 専用のパス解決。skills ルート配下または作業ディレクトリ配下を許可する。

    相対パスは従来通り skills ルート基準で解決する（SKILL.md の
    references/assets からの参照や既存の呼び出し規約との後方互換のため）。
    絶対パスは、_resolve_workdir()（run_script と同じ、ChatSettings の
    セッション作業ディレクトリ→未設定なら _DEFAULT_WORKDIR）配下か、
    skills ルート配下のどちらかであれば許可する。作業ディレクトリ配下の
    画像を指す場合は曖昧さを避けるため絶対パス指定を必須とする。

    Args:
        raw: view_image に渡された relative_path（相対パスまたは絶対パス）。

    Returns:
        解決済みの絶対パス（Path）。

    Raises:
        RuntimeError: init_tools() が未実行の場合。
        ValueError: 解決後のパスが skills ルート配下・作業ディレクトリ配下の
            どちらにも収まらない場合。
    """
    if _SKILLS_ROOT is None or _DEFAULT_WORKDIR is None:
        raise RuntimeError("init_tools() が未実行です")
    p = Path(raw)
    if p.is_absolute():
        candidate = p.resolve()
    else:
        candidate = (_SKILLS_ROOT / p).resolve()
    workdir = _resolve_workdir()
    if candidate.is_relative_to(_SKILLS_ROOT) or candidate.is_relative_to(workdir):
        return candidate
    raise ValueError(
        f"画像ファイルは skills ディレクトリ配下、または作業ディレクトリ"
        f"（{workdir}）配下のみアクセスできます: {raw}"
    )


async def _confirm_run_script(skill_name: str, script: str, args: list[str], workdir: Path) -> bool:
    """run_script の実行前にユーザーへ承認を求める。

    ask_user_choice と同じ cl.AskActionMessage パターンを使い、実行しようと
    しているスキル名・スクリプト・引数・作業ディレクトリを提示した上で
    承認/拒否を選ばせる。タイムアウト（未応答）は安全側に倒して拒否扱いにする。

    Args:
        skill_name: 実行対象スキルのフォルダ名。
        script: スキルフォルダからの相対パス。
        args: スクリプトへ渡す追加引数のリスト。
        workdir: 実際に subprocess.run の cwd として使われる絶対パス。

    Returns:
        ユーザーが実行を許可した場合 True、拒否またはタイムアウトの場合 False。
    """
    args_str = " ".join(args) if args else "(なし)"
    content = (
        "次のスクリプトを実行しようとしています。実行してよいですか？\n\n"
        f"- スキル: `{skill_name}`\n"
        f"- スクリプト: `{script}`\n"
        f"- 引数: `{args_str}`\n"
        f"- 作業ディレクトリ: `{workdir}`"
    )
    actions = [
        cl.Action(name="approve", payload={"value": "approve"}, label="✅ 実行を許可"),
        cl.Action(name="deny", payload={"value": "deny"}, label="🚫 拒否"),
    ]
    res = await cl.AskActionMessage(
        content=content, actions=actions, timeout=_APPROVAL_TIMEOUT_SECONDS
    ).send()
    if res is None:
        logger.info("run_script: 承認待ちタイムアウト skill=%s script=%s", skill_name, script)
        return False
    return res["payload"].get("value") == "approve"


_FAILURE_STREAK_THRESHOLD = 4

def _track_failure_streak(session_key: str, failed: bool, tool_label: str) -> str:
    """同一ツールの連続失敗回数を cl.user_session に記録し、閾値超過時は警告文を返す。

    小型ローカルモデルは、同じコード・引数の微修正を繰り返すだけで根本的な
    アプローチを変えないまま失敗を重ねることがある（tune-prompt調査で
    execute_python_code が13回連続で構文エラーを繰り返した実例あり）。
    system_prompt.md の「連続失敗3回で切り替える」という自己申告ルールだけ
    では守られないことがあるため、ツール自体が連続失敗回数を数え、閾値を
    超えたら結果メッセージに強制的な警告を追記する（会話（thread）単位で
    独立させるため cl.user_session を使う）。

    Args:
        session_key: cl.user_session に保存するカウンタのキー
            （ツールごとに別カウンタにするため、ツール名を含める）。
        failed: 今回の実行が失敗（非0終了コード）だったか。
        tool_label: 警告文に出すツール名（例: "execute_python_code"）。

    Returns:
        閾値に達していれば先頭に改行を含む警告文、そうでなければ空文字列
        （空文字列は呼び出し側で無視してよい）。
    """
    if not failed:
        cl.user_session.set(session_key, 0)
        return ""
    streak = cl.user_session.get(session_key, 0) + 1
    cl.user_session.set(session_key, streak)
    if streak < _FAILURE_STREAK_THRESHOLD:
        return ""
    return (
        f"\n\n【システム警告】{tool_label} が直近{streak}回連続で失敗しています。"
        "同じコード・引数を少しずつ書き直す対症療法をやめ、根本的に別の書き方・"
        "別の手段に切り替えるか、この手段にこだわらず代替アプローチを検討してください。"
    )


@tool
async def run_script(skill_name: str, script: str, script_args: list[str] | None = None) -> str:
    """スキルの scripts/ 配下のスクリプトを実行し、標準出力/標準エラーを返す。

    作業ディレクトリは、Chainlit の ChatSettings（歯車アイコン）でユーザーが
    セッションに設定していればそのディレクトリ、未設定なら config.ini の
    [paths].default_workdir を使う（_resolve_workdir 参照）。
    タイムアウトは設定値（既定 60 秒）。
    .py スクリプトは設定された Python 実行ファイルで起動する。
    Agent Skills 標準の progressive disclosure における第3段階（Execute）に相当する。
    設定（script_require_approval）が有効な場合、実行前にユーザーへ承認を求める。
    ただし create_plan/approve_plan で計画が承認済み（cl.user_session["plan_approved"]
    が True）の間は、この個別確認をスキップする。

    Args:
        skill_name: スクリプトを持つスキルのフォルダ名。
        script: スキルフォルダからの相対パス（必ず scripts/ 配下、例: scripts/count.py）。
            scripts/ で始まらないパスは実行前に拒否される。
        script_args: スクリプトへ渡す追加引数のリスト（省略可）。

    Returns:
        「[終了コード] N」に続けて、標準出力・標準エラーがあれば
        それぞれ「[標準出力]」「[標準エラー]」の見出し付きで連結した文字列。
        パスが skills ルート外を指す場合、scripts/ 配下でない場合、
        スクリプトが存在しない場合、タイムアウトした場合、起動自体に
        失敗した場合、ユーザーが実行を拒否した場合はいずれも例外を送出せず
        「エラー: ...」形式で返す。
    """
    return await _run_script_impl(skill_name, script, script_args)


@tool
async def run_readonly_script(
    skill_name: str, script: str, script_args: list[str] | None = None
) -> str:
    """run_script の制限版。file-tools スキルのスクリプトのみ実行できる。

    read-only 前提のサブエージェント種別（explore 等、run_script/execute_python_code
    を持たない）が、作業ディレクトリ配下の任意の絶対パスにあるテキストファイルの
    読み込み（read_file.py）・ファイル名検索（glob_file.py）・内容検索（grep_file.py）・
    JSON クエリ（json_query.py）を行うための唯一の手段。file-tools の4スクリプトは
    いずれも状態を変更しない読み取り専用の操作のため、このツール経由での実行を
    read-only エージェントにも許可している。

    引数の意味・使い方は run_script と完全に同一（file-tools の SKILL.md を参照）。
    skill_name に "file-tools" 以外を指定した場合は実行せずエラーを返す
    （状態を変更しうる他スキルのスクリプトを read-only エージェントが実行できない
    ようにする境界）。

    Args:
        skill_name: 必ず "file-tools" を指定すること（他のスキルは拒否される）。
        script: スキルフォルダからの相対パス（必ず scripts/ 配下）。
        script_args: スクリプトへ渡す追加引数のリスト（省略可）。

    Returns:
        run_script と同じ形式の実行結果文字列。skill_name が "file-tools" 以外の
        場合は例外を送出せず「エラー: ...」形式の文字列を返す（実行しない）。
    """
    if skill_name != "file-tools":
        return (
            "エラー: run_readonly_script は file-tools スキルのスクリプトのみ"
            f"実行できます（指定された skill_name='{skill_name}' は拒否されました）。"
        )
    return await _run_script_impl(skill_name, script, script_args)


async def _run_script_impl(
    skill_name: str, script: str, script_args: list[str] | None = None
) -> str:
    """run_script / run_readonly_script が共有する実行本体。"""
    args = script_args or []
    # script_args 内の各要素で `@N`（パスメモリー参照）を実パスへ解決する。
    # 対象外の文字列（トークン形式でない）はそのまま通す。
    resolved_args = []
    for a in args:
        resolved, error = _resolve_path_memory_token(a)
        if error:
            return f"エラー: {error}"
        resolved_args.append(resolved)
    args = resolved_args
    # scripts/ 配下に限定する（第3段階の実行対象は scripts/ のみ）。
    if not (script.startswith("scripts/") or script.startswith("scripts\\")):
        return "エラー: 実行できるのは scripts/ 配下のスクリプトのみです。"
    try:
        script_path = _safe_path(f"{skill_name}/{script}")
    except ValueError as e:
        return f"エラー: {e}"
    if not script_path.is_file():
        return f"エラー: スクリプトが見つかりません: {skill_name}/{script}"
    workdir = _resolve_workdir()

    if _SCRIPT_REQUIRE_APPROVAL and not cl.user_session.get("plan_approved"):
        approved = await _confirm_run_script(skill_name, script, args, workdir)
        if not approved:
            logger.info("run_script: ユーザーが拒否 skill=%s script=%s", skill_name, script)
            return (
                f"エラー: ユーザーが実行を拒否しました（skill={skill_name}, script={script}）。"
                "このスクリプトは実行されていません。"
            )

    # .py は設定の Python で、それ以外はそのまま実行を試みる。
    if script_path.suffix == ".py":
        cmd = [_SCRIPT_PYTHON, str(script_path), *args]
    else:
        cmd = [str(script_path), *args]

    logger.info("run_script: %s %s cwd=%s", skill_name, script, workdir)
    try:
        # 承認待ちの await 済みで別スレッドの必要はないが、subprocess.run 自体は
        # ブロッキング呼び出しのため、to_thread でイベントループのブロックを避ける。
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=_SCRIPT_TIMEOUT,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return f"エラー: スクリプトが {_SCRIPT_TIMEOUT} 秒でタイムアウトしました。"
    except OSError as e:
        return f"エラー: スクリプトを実行できませんでした: {e}"

    # stdout / stderr / 終了コードをまとめて返す（LLM が結果を解釈できるように）。
    parts = [f"[終了コード] {proc.returncode}"]
    if proc.stdout:
        parts.append(f"[標準出力]\n{proc.stdout.rstrip()}")
    if proc.stderr:
        parts.append(f"[標準エラー]\n{proc.stderr.rstrip()}")
    warning = _track_failure_streak("run_script_failure_streak", proc.returncode != 0, "run_script")
    if warning:
        parts.append(warning)
    return "\n".join(parts)


async def _confirm_execute_python_code(code: str, workdir: Path) -> bool:
    """execute_python_code の実行前にユーザーへ承認を求める。

    _confirm_run_script と同じ cl.AskActionMessage パターンを使い、実行しようと
    しているコード全文と作業ディレクトリを提示した上で承認/拒否を選ばせる。
    タイムアウト（未応答）は安全側に倒して拒否扱いにする。

    Args:
        code: 実行しようとしている Python コード全文。
        workdir: 実際に subprocess.run の cwd として使われる絶対パス。

    Returns:
        ユーザーが実行を許可した場合 True、拒否またはタイムアウトの場合 False。
    """
    content = (
        "次のPythonコード（LLMが生成したもの）を実行しようとしています。実行してよいですか？\n\n"
        f"```python\n{code}\n```\n\n"
        f"- 作業ディレクトリ: `{workdir}`"
    )
    actions = [
        cl.Action(name="approve", payload={"value": "approve"}, label="✅ 実行を許可"),
        cl.Action(name="deny", payload={"value": "deny"}, label="🚫 拒否"),
    ]
    res = await cl.AskActionMessage(
        content=content, actions=actions, timeout=_APPROVAL_TIMEOUT_SECONDS
    ).send()
    if res is None:
        logger.info("execute_python_code: 承認待ちタイムアウト")
        return False
    return res["payload"].get("value") == "approve"


@tool
async def execute_python_code(code: str) -> str:
    """LLMが生成したPythonコードをその場で実行し、標準出力/標準エラーを返す。

    run_script が skills/*/scripts/ 配下の既存ファイルしか実行できないのに対し、
    このツールはコード文字列を一時ファイルへ書き出してその場で実行する。任意コード
    実行はリスクが高いため、config.ini の [scripts].code_execution_enabled が
    false の場合は実行せずエラーを返す。設定（code_execution_require_approval）が
    有効な場合、実行前にユーザーへ承認を求める（run_script と同じ確認ダイアログ
    パターン、コード全文を提示）。ただし create_plan/approve_plan で計画が
    承認済み（cl.user_session["plan_approved"] が True）の間は、この個別確認を
    スキップする（run_script と同じ挙動）。

    作業ディレクトリは run_script と同じ _resolve_workdir() で決定する
    （Chainlit の ChatSettings で設定されていればそれ、無ければ
    config.ini の [paths].default_workdir）。タイムアウトや Python 実行ファイルも
    run_script と共通の設定（[scripts].timeout / [scripts].python）を流用する。

    Args:
        code: 実行する Python コード全文。

    Returns:
        「[終了コード] N」に続けて、標準出力・標準エラーがあれば
        それぞれ「[標準出力]」「[標準エラー]」の見出し付きで連結した文字列。
        code が空の場合、config.ini で無効化されている場合、タイムアウトした場合、
        起動自体に失敗した場合、ユーザーが実行を拒否した場合はいずれも例外を送出せず
        「エラー: ...」形式で返す。
    """
    if not code.strip():
        return "エラー: code が空です。"
    if not _CODE_EXEC_ENABLED:
        return (
            "エラー: LLMが生成したPythonコードの実行はconfig.iniで無効化されています"
            "（[scripts] code_execution_enabled=false）。"
        )
    workdir = _resolve_workdir()

    if _CODE_EXEC_REQUIRE_APPROVAL and not cl.user_session.get("plan_approved"):
        approved = await _confirm_execute_python_code(code, workdir)
        if not approved:
            logger.info("execute_python_code: ユーザーが拒否")
            return "エラー: ユーザーが実行を拒否しました。このコードは実行されていません。"

    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", dir=str(workdir), delete=False, encoding="utf-8"
        )
        tmp.write(code)
        tmp.close()
        tmp_path = Path(tmp.name)
    except OSError as e:
        return f"エラー: 一時ファイルを作成できませんでした: {e}"

    logger.info("execute_python_code: cwd=%s", workdir)
    try:
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [_SCRIPT_PYTHON, str(tmp_path)],
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=_SCRIPT_TIMEOUT,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            return f"エラー: コードが {_SCRIPT_TIMEOUT} 秒でタイムアウトしました。"
        except OSError as e:
            return f"エラー: コードを実行できませんでした: {e}"
    finally:
        tmp_path.unlink(missing_ok=True)

    parts = [f"[終了コード] {proc.returncode}"]
    if proc.stdout:
        parts.append(f"[標準出力]\n{proc.stdout.rstrip()}")
    if proc.stderr:
        parts.append(f"[標準エラー]\n{proc.stderr.rstrip()}")
    warning = _track_failure_streak(
        "execute_python_code_failure_streak", proc.returncode != 0, "execute_python_code"
    )
    if warning:
        parts.append(warning)
    return "\n".join(parts)


@tool
async def dispatch_agent(task: str, agent_type: str) -> str:
    """タスクを独立したサブエージェントへ委譲し、最終回答のみを受け取る。

    調査や複数ステップの下調べなど、詳細な思考過程やツール呼び出しの
    経緯までは自分の会話履歴に残す必要が無い作業に使う。サブエージェントは
    agent_type で選んだ種別のツールセットを使って自律的に作業するが、
    その内部の思考過程・ツール呼び出しはあなたの会話履歴には一切残らず、最終回答の
    テキストのみが返る（ログファイルには内部の記録が残る）。run_script が
    呼ばれた場合、その承認確認はサブエージェントの実行中にそのまま
    ユーザーへ表示される。数十〜数百件規模のファイル（画像等）を扱う調査は、
    年・サブフォルダ等の単位でこのツールへ分割委任すると効率的。
    サブエージェントはさらに別のサブエージェントへ委譲することはできない。

    Args:
        task: サブエージェントに依頼したいタスクの説明。必要な背景情報・
            期待する出力形式を過不足なく書くこと（サブエージェントは
            この会話の文脈を一切知らない）。対象パスは記憶から書き起こさず、
            直前の glob_file.py 等で得た `@N`（パスメモリー参照）をそのまま
            文中に埋め込んでよい（解決できるものは実パスへ自動置換される）。
        agent_type: 使用するサブエージェントの種別名（必須、暗黙の既定値は
            無い）。利用可能な種別とそれぞれの用途はシステムプロンプトの
            一覧を参照し、タスクの内容に合った種別を毎回明示的に選ぶこと。

    Returns:
        サブエージェントの最終回答テキスト。init_tools() が未実行の場合、
        agent_type が不明な場合、サブエージェントの実行に失敗した場合は、
        例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    if _LLM_CONFIG is None:
        return "エラー: init_tools() が未実行です"
    resolved = _AGENT_TYPES.get(agent_type)
    if resolved is None:
        available = ", ".join(sorted(_AGENT_TYPES)) or "（登録なし）"
        return f"エラー: 不明な agent_type '{agent_type}' です。利用可能: {available}"
    task = _resolve_path_memory_tokens_in_text(task)
    logger.info("dispatch_agent: task=%r agent_type=%r", task, agent_type)
    try:
        return await run_subagent(
            task,
            resolved.tools,
            resolved.system_prompt,
            _LLM_CONFIG,
            _SUBAGENT_MAX_ITERATIONS,
        )
    except Exception as e:  # noqa: BLE001 - 致命的エラーもエラー文字列化して返す
        logger.exception("dispatch_agent 失敗")
        return f"エラー: サブエージェントの実行に失敗しました: {e}"


_VALID_TASK_STATUSES = ("pending", "in_progress", "completed")

_STATUS_MARKERS = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "completed": "[x]",
}


def _render_plan(plan: list[dict], *, finished: bool = False) -> str:
    """計画（ステップ一覧）をチェックリスト形式の Markdown へ整形する。

    create_plan / update_task_progress の両方から呼ばれる表示専用の純関数。

    Args:
        plan: {"content": str, "activeForm": str,
            "status": "pending"|"in_progress"|"completed"} の辞書のリスト。
        finished: 全ステップ完了時に完了メッセージを付記するかどうか。

    Returns:
        「### 実行計画」見出しに続けて各ステップをチェックリストで並べた Markdown 文字列。
        "in_progress" のステップは content の代わりに activeForm を表示する。
    """
    lines = ["### 実行計画"]
    for step in plan:
        marker = _STATUS_MARKERS[step["status"]]
        text = step["activeForm"] if step["status"] == "in_progress" else step["content"]
        lines.append(f"- {marker} {text}")
    if finished:
        lines.append("\n✅ 計画完了")
    return "\n".join(lines)


@tool
async def create_plan(steps: list[dict[str, str]]) -> str:
    """複数ステップの実行計画を作成し、ユーザーへチェックリストとして表示する。

    複数回の run_script 実行を伴うような多段階のタスクに着手する前に、まずこの
    ツールでステップ一覧を提示する。作成しただけでは run_script の個別確認は
    スキップされない。承認を得るには続けて approve_plan を呼ぶこと。

    Args:
        steps: 実行計画の各ステップを表す辞書のリスト（1件以上、実行順）。
            各辞書は次の2キーを持つこと。
            - content: ステップの内容（例: "設定ファイルを読み込む"）。
            - activeForm: 実行中（in_progress）の間だけチェックリストに
              表示する現在進行形の説明（例: "設定ファイルを読み込み中"）。

    Returns:
        計画を作成した旨とステップ件数を伝えるテキスト。steps が空、または
        いずれかの要素に content / activeForm が欠けている場合は、例外を
        送出せず「エラー: ...」形式の文字列を返す。
    """
    if not steps:
        return "エラー: steps が空です。1件以上のステップを指定してください。"
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or not s.get("content") or not s.get("activeForm"):
            return (
                f"エラー: steps[{i}] には content と activeForm の両方を"
                f"文字列で指定してください: {s!r}"
            )
    plan = [
        {"content": s["content"], "activeForm": s["activeForm"], "status": "pending"}
        for s in steps
    ]
    cl.user_session.set("plan", plan)
    cl.user_session.set("plan_approved", False)
    message = cl.Message(content=_render_plan(plan))
    await message.send()
    cl.user_session.set("plan_message", message)
    logger.info("create_plan: %d steps", len(steps))
    return f"計画を作成しました（全{len(steps)}件）。approve_plan でユーザーの承認を得てください。"


@tool
async def approve_plan() -> str:
    """作成済みの実行計画についてユーザーの承認を得る。

    _confirm_run_script と同じ cl.AskActionMessage パターンで計画内容を提示し、
    承認/拒否を選ばせる。承認されると、以後の run_script は個別確認なしで実行
    できるようになる（cl.user_session["plan_approved"] を参照）。タイムアウト
    （未応答）は安全側に倒して未承認扱いにするが、ユーザーが明示的に却下した
    場合とは返り値のテキストで区別する（無応答は単に手が離せないだけの
    可能性が高く、計画自体を作り直す必要はないため）。

    Returns:
        承認・明示的却下・タイムアウトのいずれかを伝えるテキスト。計画が未作成の
        場合は例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    plan = cl.user_session.get("plan")
    if not plan:
        return "エラー: 計画がありません。先に create_plan を呼んでください。"
    content = (
        _render_plan(plan)
        + "\n\nこの計画を承認しますか？承認後は各ステップの run_script が"
        "個別確認なしで実行されます。"
    )
    actions = [
        cl.Action(name="approve", payload={"value": "approve"}, label="✅ 計画を承認"),
        cl.Action(name="deny", payload={"value": "deny"}, label="🚫 却下"),
    ]
    res = await cl.AskActionMessage(
        content=content, actions=actions, timeout=_APPROVAL_TIMEOUT_SECONDS
    ).send()
    approved = res is not None and res["payload"].get("value") == "approve"
    cl.user_session.set("plan_approved", approved)
    if approved:
        logger.info("approve_plan: 承認されました")
        return "ユーザーが計画を承認しました。承認後は run_script の個別確認なしで実行できます。"
    if res is None:
        logger.info("approve_plan: 応答なし（タイムアウト）")
        return (
            f"ユーザーからの応答が{_APPROVAL_TIMEOUT_SECONDS}秒間ありませんでした"
            "（離席中の可能性があります）。計画自体はそのまま保持されているので、"
            "作り直す必要はありません。少し時間を置いてから改めて approve_plan を"
            "呼び直してください。"
        )
    logger.info("approve_plan: 明示的に却下されました")
    return "ユーザーが計画を却下しました。計画を修正して create_plan を呼び直してください。"


@tool
async def update_task_progress(step_index: int, status: str) -> str:
    """実行計画中のステップの進捗状態を更新し、表示中のチェックリストへ反映する。

    ステップの実行前に "in_progress"、完了後に "completed" を設定してユーザーに
    進捗を見せること。"in_progress" の間はチェックリスト上に content の代わりに
    create_plan で渡した activeForm が表示される。同時に "in_progress" にする
    ステップは1つまでにすること。全ステップが completed になると計画は完了した
    ものとみなし、plan_approved を False に戻す（承認は作成済み計画の実行に
    限定したスコープのため、完了後の無関係な run_script は再び個別確認が必要に
    なる）。

    Args:
        step_index: create_plan で渡した steps のインデックス（0始まり）。
        status: "pending" | "in_progress" | "completed" のいずれか。

    Returns:
        更新内容を説明する短いテキスト。計画が未作成、step_index が範囲外、
        status が不正な値の場合は、例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    plan = cl.user_session.get("plan")
    if not plan:
        return "エラー: 計画がありません。先に create_plan を呼んでください。"
    if status not in _VALID_TASK_STATUSES:
        return f"エラー: status は {_VALID_TASK_STATUSES} のいずれかを指定してください: {status}"
    if not (0 <= step_index < len(plan)):
        return f"エラー: step_index が範囲外です（0〜{len(plan) - 1}）: {step_index}"

    plan[step_index]["status"] = status
    cl.user_session.set("plan", plan)
    finished = all(s["status"] == "completed" for s in plan)
    if finished:
        cl.user_session.set("plan_approved", False)

    message: cl.Message | None = cl.user_session.get("plan_message")
    if message is not None:
        message.content = _render_plan(plan, finished=finished)
        await message.update()

    logger.info("update_task_progress: step=%d status=%s finished=%s", step_index, status, finished)
    label = plan[step_index]["content"]
    suffix = "\n計画は全ステップ完了しました。" if finished else ""
    return f"ステップ{step_index}「{label}」を {status} に更新しました。{suffix}"


@tool
async def ask_user_text(question: str) -> str:
    """会話を続けるために必要な追加情報を、ユーザーに自由記述で質問する。

    要求が曖昧・情報が不足している等、自由記述の回答（固有名詞・ファイルパス・
    詳細な要望など）が必要な場合に使う。選択肢から選んでほしい場合は
    ask_user_choice を使うこと。

    Args:
        question: ユーザーに表示する質問文。

    Returns:
        ユーザーが入力した回答テキスト。設定値（config.ini の
        [timeouts].ask_user_text_seconds）の秒数以内に応答が無い場合は、
        例外を送出せず「エラー: ユーザーからの応答がありませんでした
        （タイムアウト）。」を返す。
    """
    logger.info("ask_user_text: %s", question)
    res = await cl.AskUserMessage(content=question, timeout=_ASK_USER_TEXT_TIMEOUT_SECONDS).send()
    if res is None:
        return "エラー: ユーザーからの応答がありませんでした（タイムアウト）。"
    return res.get("output", "")


@tool
async def ask_user_choice(question: str, choices: list[str]) -> str:
    """会話を続けるために必要な選択を、ユーザーに選択肢形式で質問する。

    複数の進め方・方針からユーザーに1つ選んでもらいたい場合に使う。
    自由記述の回答が必要な場合は ask_user_text を使うこと。

    Args:
        question: ユーザーに表示する質問文。
        choices: 選択肢の文字列リスト（1件以上）。

    Returns:
        ユーザーが選んだ選択肢の文字列。choices が空の場合や、設定値
        （config.ini の [timeouts].ask_user_choice_seconds）の秒数以内に
        応答が無い場合は、例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    if not choices:
        return "エラー: choices が空です。1件以上の選択肢を指定してください。"
    logger.info("ask_user_choice: %s choices=%s", question, choices)
    actions = [
        cl.Action(name=f"choice_{i}", payload={"value": c}, label=c)
        for i, c in enumerate(choices)
    ]
    res = await cl.AskActionMessage(
        content=question, actions=actions, timeout=_ASK_USER_CHOICE_TIMEOUT_SECONDS
    ).send()
    if res is None:
        return "エラー: ユーザーからの応答がありませんでした（タイムアウト）。"
    return res["payload"].get("value") or res.get("label", "")


@tool(response_format="content_and_artifact")
def view_image(relative_path: str) -> tuple[str, dict | None]:
    """skills ディレクトリ配下、または作業ディレクトリ配下の画像ファイルをLLMへ視覚情報として見せる。

    SKILL.md 本文が references/assets 配下の画像を参照している場合、
    run_script が生成した画像ファイルの内容を確認させたい場合、または
    ユーザーが指定した作業ディレクトリ配下にある画像（写真・スキャン画像等）
    を確認したい場合に使う。
    OpenAI互換APIの制約上、ツール呼び出し結果（ToolMessage）自体には
    画像を積めないため、この関数はテキストの確認メッセージのみを返し、
    実データは artifact 経由でグラフ側（ImageAwareToolNode）に渡す。
    ImageAwareToolNode がそれを直後の HumanMessage として会話履歴へ追加し、
    次のモデル呼び出しで実際にLLMへ見えるようになる。

    Args:
        relative_path: 相対パスを渡すと skills ルートからの相対パスとして
            解決する（例: word-counter/references/example.png）。作業
            ディレクトリ配下の画像を見る場合は絶対パス（例:
            C:\\Users\\foo\\data\\2019\\img1.png）で指定すること。
            file-tools の glob_file.py/grep_file.py/read_file.py の結果に
            付与されたパスメモリー参照（`@N` 形式）をそのまま渡すこともできる
            （path-memory スキル参照）。

    Returns:
        (確認テキスト, artifact) のタプル。artifact は画像を読み込めた場合のみ
        {"image_url": "data:<mime>;base64,<...>"} を持つ dict、それ以外は None。
        skills ルート・作業ディレクトリのどちらの配下でもない場合・
        ファイルが存在しない場合・対応拡張子（png/jpg/jpeg/gif/webp/bmp）
        でない場合、`@N` が未登録の場合は、例外を送出せず「エラー: ...」
        形式のテキストと None を返す。
    """
    resolved_path, error = _resolve_path_memory_token(relative_path)
    if error:
        return f"エラー: {error}", None
    try:
        path = _resolve_view_image_path(resolved_path)
    except ValueError as e:
        return f"エラー: {e}", None
    if not path.is_file():
        return f"エラー: ファイルが見つかりません: {relative_path}", None
    if not is_image_file(path):
        return (
            f"エラー: 対応していない画像形式です（png/jpg/jpeg/gif/webp/bmpのみ）: {relative_path}",
            None,
        )
    logger.info("view_image: %s", relative_path)
    return f"画像を読み込みました: {relative_path}", {"image_url": to_data_url(path)}


# init_tools() の _resolve_agent_types() がこのリストを実行時に参照するだけなので、
# 定義順は init_tools()/dispatch_agent より後でもよい（view_image を含めるため
# view_image の定義後に置く）。
_SUBAGENT_TOOLS: list = [
    read_skill,
    read_skill_file,
    run_script,
    run_readonly_script,
    execute_python_code,
    get_tool_source,
    view_image,
]


def _with_image_followups(result: dict) -> dict:
    """ToolMessage.artifact に画像があれば、直後に画像付き HumanMessage を追加する。

    view_image が {"image_url": ...} という artifact 付きの ToolMessage を
    返した場合にのみ発火する。それ以外のツール結果には触れない。

    Args:
        result: ToolNode.invoke/ainvoke の戻り値（{"messages": [ToolMessage, ...]}）。

    Returns:
        画像artifactがあれば末尾に HumanMessage を追加した新しい dict、
        無ければ result をそのまま返す。
    """
    messages = result.get("messages", [])
    extra = [
        followup
        for msg in messages
        if (followup := image_followup_message(getattr(msg, "artifact", None))) is not None
    ]
    if not extra:
        return result
    return {**result, "messages": [*messages, *extra]}


def _log_tool_calls_debug(input) -> None:  # noqa: A002
    """メイングラフのツール呼び出し（呼び出し前）を DEBUG レベルで記録する。

    config.ini の [paths].log_level が "debug" のときのみ実際にログへ出る
    （logger.isEnabledFor で早期リターンし、通常時はほぼゼロオーバーヘッド）。
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    messages = input.get("messages") if isinstance(input, dict) else input
    if not messages:
        return
    for call in getattr(messages[-1], "tool_calls", None) or []:
        logger.debug(
            "tool_call: name=%s args=%r id=%s", call.get("name"), call.get("args"), call.get("id")
        )


def _log_tool_results_debug(result: dict) -> None:
    """メイングラフのツール呼び出し結果を DEBUG レベルで記録する（全文、未省略）。"""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    for msg in result.get("messages", []):
        logger.debug("tool_result: name=%s content=%r", getattr(msg, "name", None), msg.content)


class ImageAwareToolNode(ToolNode):
    """view_image の実行結果（画像）を、後続の HumanMessage として自動追加する ToolNode。

    OpenAI互換API の tool role メッセージは文字列content しか持てないため、
    画像を持つ ToolMessage.artifact をそのまま次のモデル呼び出しに含めることは
    できない。そこで ToolNode 実行後に _with_image_followups() で後処理し、
    画像を content に持つ HumanMessage を会話履歴へ追加する。
    handwritten/prebuilt いずれのグラフ実装でも、素の ToolNode の代わりに
    このクラスを使うだけで画像受け渡しに対応できる。
    """

    def invoke(self, input, config=None, **kwargs):  # noqa: A002
        _log_tool_calls_debug(input)
        result = super().invoke(input, config, **kwargs)
        _log_tool_results_debug(result)
        return _with_image_followups(result)

    async def ainvoke(self, input, config=None, **kwargs):  # noqa: A002
        _log_tool_calls_debug(input)
        result = await super().ainvoke(input, config, **kwargs)
        _log_tool_results_debug(result)
        return _with_image_followups(result)


def _require_memory_root() -> Path:
    """_MEMORY_ROOT を返す。init_tools() が未実行なら RuntimeError。"""
    if _MEMORY_ROOT is None:
        raise RuntimeError("init_tools() が未実行です")
    return _MEMORY_ROOT


@tool
def create_memory(name: str, description: str, memory_type: str, content: str) -> str:
    """新しい永続メモリーを保存する。

    スレッドをまたいで将来の会話へ引き継ぎたい価値ある事実を学んだときに使う。
    memory_type ごとの保存タイミング・保存してはいけないものはシステムプロンプトの
    Memory System セクションを参照すること。同名のメモリーが既にある場合はエラーに
    なるので、既存メモリーの更新には update_memory を使うこと（迷ったら先に
    search_memory / list_memories で重複が無いか確認する）。

    Args:
        name: 一意な名前（英数字・ハイフン・アンダースコアのみ、64文字以内）。
        description: 一行の説明文（索引 MEMORY.md にそのまま載る）。
        memory_type: "user" | "feedback" | "project" | "reference" のいずれか。
        content: メモリー本文。feedback/project タイプはルール/事実の後に
            「**Why:**」「**How to apply:**」の行を含めることが望ましい。

    Returns:
        保存したファイルパスを伝えるテキスト。name/memory_type が不正、
        description/content が空、同名のメモリーが既に存在する場合は、
        例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    try:
        path = memory.create_memory(_require_memory_root(), name, description, memory_type, content)
    except (RuntimeError, ValueError) as e:
        return f"エラー: {e}"
    logger.info("create_memory: %s", name)
    return f"メモリーを保存しました: {path}"


@tool
def update_memory(name: str, content: str) -> str:
    """既存の永続メモリーの本文を更新する（name/description/memory_typeは変わらない）。

    Args:
        name: 更新対象メモリーの名前。
        content: 新しい本文（既存の本文を丸ごと置き換える）。

    Returns:
        更新したファイルパスを伝えるテキスト。content が空、または name の
        メモリーが存在しない場合は、例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    try:
        path = memory.update_memory(_require_memory_root(), name, content)
    except (RuntimeError, ValueError) as e:
        return f"エラー: {e}"
    logger.info("update_memory: %s", name)
    return f"メモリーを更新しました: {path}"


@tool
def delete_memory(name: str) -> str:
    """永続メモリーを削除する。

    Args:
        name: 削除対象メモリーの名前。

    Returns:
        削除したファイルパスを伝えるテキスト。name のメモリーが存在しない場合は、
        例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    try:
        path = memory.delete_memory(_require_memory_root(), name)
    except (RuntimeError, ValueError) as e:
        return f"エラー: {e}"
    logger.info("delete_memory: %s", name)
    return f"メモリーを削除しました: {path}"


@tool
def read_memory(name: str) -> str:
    """永続メモリー1件を本文込みで全文読み込む。

    search_memory / list_memories は一覧（name+description）しか返さないため、
    メモリーの内容そのものを確認・引用する前には必ずこのツールで全文を読むこと。

    Args:
        name: 読み込むメモリーの名前。

    Returns:
        「[type] name\\ndescription\\n\\ncontent」形式の全文。name のメモリーが
        存在しない場合は、例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    try:
        mem = memory.read_memory(_require_memory_root(), name)
    except (RuntimeError, ValueError) as e:
        return f"エラー: {e}"
    logger.info("read_memory: %s", name)
    return f"[{mem.memory_type}] {mem.name}\n{mem.description}\n\n{mem.content}"


@tool
def search_memory(query: str, memory_type: str | None = None) -> str:
    """永続メモリーを name/description/本文に対するキーワード部分一致で検索する。

    本文は返さず一覧（name+description）のみを返す（コンテキスト節約のため）。
    内容そのものが必要な場合は、ヒットした name を read_memory へ渡すこと。

    Args:
        query: 検索キーワード（大文字小文字は区別しない）。
        memory_type: 指定すれば "user"|"feedback"|"project"|"reference" の
            いずれかに絞り込む（省略時は全type対象）。

    Returns:
        ヒットしたメモリーの「- [type] name: description」一覧と件数。
        0件の場合は「一致するメモリーはありません」。query が空、または
        memory_type が不正な値の場合は、例外を送出せず「エラー: ...」形式の
        文字列を返す。
    """
    try:
        hits = memory.search_memories(_require_memory_root(), query, memory_type)
    except (RuntimeError, ValueError) as e:
        return f"エラー: {e}"
    logger.info("search_memory: query=%r type=%s hits=%d", query, memory_type, len(hits))
    if not hits:
        return "一致するメモリーはありません。"
    lines = [f"- [{m.memory_type}] {m.name}: {m.description}" for m in hits]
    return f"{len(hits)}件ヒットしました。\n" + "\n".join(lines)


@tool
def list_memories(memory_type: str | None = None) -> str:
    """保存されている永続メモリーを一覧表示する。

    Args:
        memory_type: 指定すれば "user"|"feedback"|"project"|"reference" の
            いずれかに絞り込む（省略時は全type対象）。

    Returns:
        「- [type] name: description」一覧。0件の場合は
        「保存されているメモリーはありません」。memory_type が不正な値の
        場合は、例外を送出せず「エラー: ...」形式の文字列を返す。
    """
    try:
        mems = memory.list_memories(_require_memory_root(), memory_type)
    except (RuntimeError, ValueError) as e:
        return f"エラー: {e}"
    logger.info("list_memories: type=%s count=%d", memory_type, len(mems))
    if not mems:
        return "保存されているメモリーはありません。"
    return "\n".join(f"- [{m.memory_type}] {m.name}: {m.description}" for m in mems)


@tool("help")
def show_help() -> str:
    """このシステムの使い方に関するヘルプ本文を返す。

    ユーザーがヘルプや使い方、フィードバックの窓口について尋ねてきた場合に呼ぶ。
    本文は config.ini の [paths].help_path が指すMarkdownファイル
    （既定: system_prompt/help.md）に記述されており、このツールはその内容を
    そのまま読み込んで返すだけの薄いラッパー（憶測でヘルプ内容を生成しない）。

    Returns:
        ヘルプ本文（UTF-8 テキスト、Markdown形式）。init_tools() が未実行、
        または help_path のファイルが存在しない場合は、例外を送出せず
        「エラー: ...」形式の文字列を返す。
    """
    if _HELP_PATH is None:
        return "エラー: init_tools() が未実行です"
    if not _HELP_PATH.is_file():
        return f"エラー: ヘルプファイルが見つかりません: {_HELP_PATH}"
    logger.info("show_help")
    return _HELP_PATH.read_text(encoding="utf-8")


# グラフに渡すツール一覧（3 段階の progressive disclosure + サブエージェント委譲 +
# ユーザー質問 + 永続メモリー + ヘルプ）。
ALL_TOOLS = [
    read_skill,
    read_skill_file,
    run_script,
    execute_python_code,
    get_tool_source,
    view_image,
    dispatch_agent,
    ask_user_text,
    ask_user_choice,
    create_plan,
    approve_plan,
    update_task_progress,
    create_memory,
    update_memory,
    delete_memory,
    read_memory,
    search_memory,
    list_memories,
    show_help,
]
