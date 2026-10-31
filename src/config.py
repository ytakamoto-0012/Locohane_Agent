"""設定ローダー。

役割:
- config.ini を読み込み、LLM 接続情報と全保存先パスを 1 つの frozen dataclass に集約する。
- 環境変数が設定されていればそれで上書きする（config.ini の値より優先）。
- data 配下（checkpoints / logs / memory）のディレクトリを起動時に作成する。

このファイル自体は Agent Skills 仕様には対応しない（純粋なアプリ設定）。
「何がどこに溜まるか」をコードから追えるようにするための土台。
"""

from __future__ import annotations

import ast
import configparser
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path

from . import memory

logger = logging.getLogger(__name__)

# プロジェクトルート（このファイルは <root>/src/config.py なので 2 つ上がルート）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.ini"
# 管理ツール（admin/ パッケージ）でUIから変更した設定値の上書き差分の既定パスは
# <instances_dir>/default/config_overrides.json（default_overrides_path() 参照）。
# load_config() の呼び出し元が overrides_path を明示しない場合、環境変数
# CONFIG_OVERRIDES_PATH（管理ツールが子プロセス起動時に設定する）→ この既定値
# の順で使う。app.bat で直接起動した場合もこの既定値が使われるため、
# instances/default/ に対する管理ツールでの変更が app.bat 起動時にも反映される
# （README_DETAIL.md「設定ダッシュボード」参照）。
DEFAULT_INSTANCE_NAME = "default"
# インスタンス別ディレクトリのルート（[admin].instances_dir）の既定値。
DEFAULT_INSTANCES_DIR = "./instances"
# [admin] セクションは、上記のインスタンス別上書きの対象外（管理ツール自身の
# 起動設定を、管理ツールで変更できてしまう循環を避けるため）。[data_backup] も全インスタンス共通の
# 管理ツールの設定（データのバックアップ。admin/data_backup.py）のため対象外。
_OVERRIDE_EXEMPT_SECTIONS = frozenset({"admin", "data_backup"})
# 設定ダッシュボードの「管理者設定」で変えられる全インスタンス共通のキー（<instances_dir>/admin_settings.json に持ち、
# config.ini より優先する。admin_settings_path・_apply_admin_settings・admin/admin_settings.py 参照）。
# インスタンス別の config_overrides.json では上書きできない（_apply_config_overrides が無視する）。ナレッジの取り込みの
# 並列数は、管理ツールが全インスタンス・全コレクションの取り込みで1つの枠として共有する（admin/knowledge.py）。
ADMIN_SETTINGS_FILE = "admin_settings.json"
ADMIN_SETTING_KEYS = frozenset(
    {
        ("vector_db", "ingest_workers"),
        ("vector_db", "ingest_processes"),
        ("vector_db", "ingest_embed_requests"),
        ("vector_db", "qdrant_max_retries"),
        ("vector_db", "qdrant_retry_interval_seconds"),
        ("vector_db", "scan_workers"),
        ("vector_db", "max_files_per_collection"),
        ("vector_db", "office_timeout_seconds"),
        ("vector_db", "ingest_temp_dir"),
        ("vector_db", "max_file_mb"),
        ("admin", "assistant_llm_instance"),
        ("admin", "assistant_embedding_instance"),
    }
)


def is_instance_overridable(section: str, key: str) -> bool:
    """config.ini のキーがインスタンス別の config_overrides.json で上書きできるか。

    _OVERRIDE_EXEMPT_SECTIONS・ADMIN_SETTING_KEYS は _apply_config_overrides が無視するため、設定ダッシュボードの
    インスタンス別の設定画面にも出さない（admin/instances.py の config_key_applies 参照）。
    """
    return section not in _OVERRIDE_EXEMPT_SECTIONS and (section, key) not in ADMIN_SETTING_KEYS


# スキルの登録系のリスト型キー。インスタンス別の config_overrides.json の値は config.ini の値を置き換えず、
# config.ini（既定値）に無い項目だけを足す（merge_additive_override 参照）。置き換えにすると、アップデートで
# 既定値へ足された新しいスキルが、以前に保存した上書きの値で消えてしまうため。既定値の項目は上書きで消したり
# 変えたり（allow_entries の上限回数を含む）できない。
ADDITIVE_OVERRIDE_KEYS = frozenset(
    {
        ("plan", "plan_approval_exempt_scripts"),
        ("subagent", "agent_type_run_script_allowlist"),
        ("main_agent_tool_guard", "allow_entries"),
    }
)


def _additive_item_identity(section: str, key: str, item: object) -> object:
    """ADDITIVE_OVERRIDE_KEYS の重複判定の単位。allow_entries は対象（[対象, 上限回数] の対象）だけで判定する。"""

    def _freeze(x: object) -> object:
        return tuple(_freeze(i) for i in x) if isinstance(x, (list, tuple)) else x

    if (section, key) == ("main_agent_tool_guard", "allow_entries") and isinstance(item, (list, tuple)) and item:
        return _freeze(item[0])
    return _freeze(item)


def merge_additive_override(section: str, key: str, base_text: str, override_text: str) -> tuple[str, list]:
    """config.ini の値（base_text）に、上書きの値（override_text）のうち既定値に無い項目だけを足した値を返す。

    どちらも config.ini に書くのと同じ Python リストリテラル文字列。リストとして読めない場合は
    合成せず override_text をそのまま返す（以降の load_config() の解析で従来どおりエラーにする）。

    Returns:
        (合成した値の文字列, 既定値と同じ対象のため無視した上書きの項目のうち、既定値と内容が違うもの)。
    """
    parsed = _parse_additive_lists(base_text, override_text)
    if parsed is None:
        return override_text, []
    base, extra = parsed
    base_items = {_additive_item_identity(section, key, i): i for i in base}
    merged = list(base)
    seen = set(base_items)
    ignored = []
    for item in extra:
        ident = _additive_item_identity(section, key, item)
        if ident in seen:
            if ident in base_items and _additive_item_identity("", "", item) != _additive_item_identity("", "", base_items[ident]):
                ignored.append(item)
            continue
        seen.add(ident)
        merged.append(item)
    # JSON の書式にする（設定ダッシュボードのリストの編集欄でもそのまま読めるように）。
    return json.dumps(merged, ensure_ascii=False), ignored


def _parse_additive_lists(*texts: str) -> list[list] | None:
    """texts をそれぞれリストリテラルとして読む（空なら []。どれかがリストとして読めなければ None）。"""
    result = []
    for text in texts:
        try:
            value = ast.literal_eval(text.strip()) if text and text.strip() else []
        except (ValueError, SyntaxError):
            return None
        if not isinstance(value, list):
            return None
        result.append(value)
    return result


def additive_override_additions(section: str, key: str, base_text: str, edited_text: str) -> tuple[str | None, list, list]:
    """設定ダッシュボードで編集した全体のリスト（edited_text）から、config.ini の値（base_text）に足す分だけを取り出す。

    ADDITIVE_OVERRIDE_KEYS の上書きは既定値に足す分だけを持つ（merge_additive_override）ため、画面で既定値を含めた
    リストを編集して保存したときに、既定値を上書きへ写さない（写すと、アップデートで既定値から外した項目が残り続ける）。

    Returns:
        (足す分の値の文字列（足す分が無ければ None。リストとして読めなければ edited_text のまま）,
         edited_text から消された既定値の項目, 既定値と同じ対象で内容（allow_entries の上限回数）を変えた項目)。
    """
    parsed = _parse_additive_lists(base_text, edited_text)
    if parsed is None:
        return edited_text, [], []
    base, edited = parsed
    edited_items = {_additive_item_identity(section, key, i): i for i in edited}
    base_items = {_additive_item_identity(section, key, i): i for i in base}
    removed = [i for ident, i in base_items.items() if ident not in edited_items]
    changed = [
        edited_items[ident]
        for ident, i in base_items.items()
        if ident in edited_items and _additive_item_identity("", "", edited_items[ident]) != _additive_item_identity("", "", i)
    ]
    additions = [i for ident, i in edited_items.items() if ident not in base_items]
    return (json.dumps(additions, ensure_ascii=False) if additions else None), removed, changed


# ADMIN_SETTING_KEYS をインスタンス別に上書きしようとした (ファイル, セクション, キー) のうち、警告を出したもの。
_warned_global_only: set[tuple[str, str, str]] = set()

# main_routing_strategy / sub_routing_strategy が取りうる値。
# src/llm/routing.py の _select_endpoint() がこの文字列で分岐する。
LLM_ROUTING_STRATEGIES = frozenset({"round_robin", "random", "priority_failover"})

# LLMEndpoint.provider が取りうる値。"llama_cpp" のみ、round_robin戦略が
# 選択候補にする際 GET /slots で実際に空きスロットがあるか確認する対象になる
# （src/llm/routing.py の _probe_llama_cpp_slots_available() 参照）。
LLM_PROVIDERS = frozenset({"openai_compatible", "llama_cpp", "vllm"})
# [vector_db].backend の選択肢（admin/ini_catalog.py の _KEY_CHOICES と一致させる）。
VECTOR_DB_BACKENDS = ("sqlite_vec", "qdrant")
# [vector_db].distance の選択肢（Qdrant の distance の綴り。admin/ini_catalog.py の _KEY_CHOICES と一致させる）。
# sqlite-vec（vec0）は Dot に対応していない（VECTOR_DB_SQLITE_VEC_DISTANCES）。
VECTOR_DB_DISTANCES = ("Cosine", "Euclid", "Dot", "Manhattan")
VECTOR_DB_SQLITE_VEC_DISTANCES = ("Cosine", "Euclid", "Manhattan")
# [vector_db].qdrant_preset の選択肢（中身は src/vector_db/qdrant_store.py の QDRANT_PRESETS。
# admin/ini_catalog.py の _KEY_CHOICES と一致させる）。none は Qdrant の既定のまま。
VECTOR_DB_QDRANT_PRESETS = ("none", "1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m", "10m")
# [vector_db].embedding_encoding_format の選択肢（/v1/embeddings の encoding_format）。
VECTOR_DB_EMBEDDING_ENCODING_FORMATS = ("base64", "float")

# reasoning_format が取りうる値（llama-server の --reasoning-format と同じ）。
LLM_REASONING_FORMATS = frozenset({"none", "deepseek", "deepseek-legacy"})

# reasoning_effort が取りうる値（llama-server の --reasoning-effort と同じ）。
LLM_REASONING_EFFORTS = frozenset({"none", "default", "minimal", "low", "medium", "high", "xhigh", "max"})

# [thinking_loop_guard].target が取りうる値。
THINKING_LOOP_GUARD_TARGETS = ("all", "content_only", "thinking_only")

# [thinking_control] の思考レベル（低い順）。順序はレベルの比較
# （src/llm/thinking_control.py のルール3）で使う。
THINKING_LEVELS = ("off", "low", "medium", "high", "xhigh")

# [main_agent_tool_guard].visibility_mode が取りうる値。main_agent_tool_guard_mode
# （呼び出し制限そのもののON/OFF/範囲）とは独立した「一覧の見せ方」の軸であり、
# modeの値に関わらずこの値だけで表示が決まる。対象はスキル一覧（{{skills}}）と
# ビルトインツールの呼び出し可否ヒント（blocked_tools_hint）で、MCP動的ツール
# （mcp__<server>__<tool>形式、src/mcp_client.py）はこの一覧・ヒントの対象として
# 想定されていない。ただし blocked_tools_hint は get_all_tools()（ビルトイン+MCP）
# 全体を対象に組み立てるため、main_agent_tool_guard_mode="all" のときは実際には
# MCP動的ツール名もヒントに混ざりうる（mode="tools_skills_only" のときはMCPが
# 常に許可されるため混ざらない）。
#   all   : 全スキル名・ツール名・descriptionを一覧に表示するが、直接実行できない
#           ものは description の末尾に注記を追記する。
#   strict: 呼び出せないスキル・ツールは一覧から完全に除外する。
#   hint  : 全スキル名は一覧に出すが、直接実行できないものは description を
#           固定文言に差し替える。
# src/skills.py の filter_skills_for_main_agent_guard/render_skills_block_with_hint/
# render_skills_block_with_guard_annotation、app.py のシステムプロンプト組み立てが
# この文字列で分岐する。
MAIN_AGENT_TOOL_GUARD_VISIBILITY_MODES = frozenset({"strict", "hint", "all"})

# [main_agent_tool_guard].mode が取りうる値。
#   false             : ガード無効（メインエージェントは全ツールを制限なく呼べる）。
#   tools_skills_only : MCP動的ツール（mcp__<server>__<tool>形式）のみ常に許可
#                       （ガード対象外）。ビルトインツール・skills/配下のスキル
#                       （run_script/run_script_background経由）は従来通り
#                       main_agent_tool_guard_allow_entries の許可リストで制限する。
#   all               : ビルトインツール・スキル・MCP動的ツールの全てをガード対象
#                       にする（allow_entries未登録＝呼び出し不可）。
MAIN_AGENT_TOOL_GUARD_MODES = frozenset({"false", "tools_skills_only", "all"})


@dataclass(frozen=True)
class LLMEndpoint:
    """LLM接続先1件分（[llm].main_url / sub_url の各要素）。

    Attributes:
        base_url: llama.cpp server（OpenAI 互換）のベース URL。
        api_key: LLM API キー。llama.cpp は認証不要のためダミー値でよい。
        model: 使用するモデル名（llama-server の --model / --alias と一致させる）。
        start: この接続先が使用可能になる時刻（時間単位、0〜24の小数、分は
            小数点で表す。例: 9.5 = 9:30）。end とセットで指定し、片方のみの
            指定は不可。両方 None の場合は常時使用可能。start > end の場合は
            日をまたぐ範囲（例: start=22, end=6 なら22:00〜翌6:00）として扱う。
        end: この接続先が使用可能でなくなる時刻（start参照）。
        provider: "openai_compatible"（既定）/ "llama_cpp" / "vllm"。
            送信する拡張パラメータ名と、履歴へ戻す thinking のフィールド名を
            推論サーバーの方言に合わせる（src/llm/dialect.py 参照）。
            "openai_compatible" は llama-server と同じパラメータを送り、
            thinking は reasoning_content / reasoning の両方で送る。
            また "llama_cpp" の場合のみ、round_robin戦略が選択候補にする際
            GET /slots で実際に空きスロットがあるか確認する（空きが無ければ
            スキップして次点へ、全滅なら空きが出るまで待機する。
            src/llm/routing.py の _probe_llama_cpp_slots_available() 参照）。
            llama.cpp server 固有の管理APIのため、それ以外のOpenAI互換
            サーバーでは使えない（provider="openai_compatible"のままにする
            こと。その場合は確認を行わず従来通り即座に選ぶ）。
    """

    base_url: str
    api_key: str
    model: str
    start: float | None = None
    end: float | None = None
    provider: str = "openai_compatible"


@dataclass(frozen=True)
class SandboxDirEntry:
    """[default_workdir].allow_sandbox_dir の1エントリ分。

    書き込みサンドボックスガード（run_script/run_script_background 経由の
    み対象。execute_python_code系は対象外）が、work_dir/default_workdir に
    加えて書き込み・削除を許可する追加ディレクトリ1件と、その書き込みを
    許可する対象（ビルトインツール名、または run_script 配下のスキル
    スクリプト）のペア。src/tools/_subprocess_env.py の
    _allowed_sandbox_dirs_for() が参照する。

    Attributes:
        dir: 許可するディレクトリの絶対パス。
        allow_entries: このディレクトリへの書き込みを許可する対象の集合。
            [main_agent_tool_guard].allow_entries と同じ形式で、要素は
            文字列1件（ビルトインツール名そのもの。現状、書き込み
            サンドボックスガードを持つビルトインツールは run_script/
            run_script_background のみのため実質未使用だが、将来
            ビルトインツール側にガードが増えた場合の拡張用）、または
            (スキル名, スクリプトファイル名) の2要素タプル
            （run_script/run_script_background 経由で呼ばれるスキル
            スクリプトのみを許可）のいずれか。空集合なら対象を問わず
            無制限に書き込み可（誰でも書き込める）。
    """

    dir: Path
    allow_entries: frozenset[str | tuple[str, str]]


@dataclass(frozen=True)
class Config:
    """アプリ全体の設定。すべて絶対パスに解決済みで保持する。

    load_config() によってのみ構築される frozen dataclass。

    Attributes:
        main_endpoints: メインエージェント用のLLM接続先リスト（[llm].main_url）。
            要素数1なら常にそれを使い、複数なら main_routing_strategy に
            従って呼び出しごとに選ぶ（src/llm.py の build_model/_select_endpoint
            参照）。
        main_routing_strategy: main_endpoints が複数ある場合の選び方
            （"round_robin"/"random"/"priority_failover" のいずれか）。
            "round_robin"/"priority_failover" かつ provider="llama_cpp" の
            接続先は、選ぶ前に GET /slots で空きスロットの有無を確認する
            （src/llm/routing.py の _select_endpoint_with_slots_probe() 参照）。
        sub_endpoints: サブエージェント（dispatch_agent）用のLLM接続先リスト
            （[llm].sub_url）。形式は main_endpoints と同じ。sub_url が未指定
            （キー無し、または値が空）の場合は main_url をそのまま使う。
        sub_endpoints_inherit_main: sub_url が未指定だったため sub_endpoints が
            main_endpoints のコピーになっているかどうか。True の場合、
            build_model(role="sub") は sub_routing_strategy による独自選択を
            行わず、同一セッション内でメインエージェントが直近実際に使った
            接続先をそのまま継承する（src/llm.py の _select_endpoint 参照。
            接続先が1件だけ複数のうち別々に選ばれてKVキャッシュが分散する
            事態を避けるため）。
        sub_routing_strategy: sub_endpoints が複数ある場合の選び方
            （sub_endpoints_inherit_main が True の場合は無視される）。
            形式は main_routing_strategy と同じ。
        sub_default_model: サブエージェントにモデル指定（agents/*.md の
            frontmatter model・dispatch_agent の model 引数）が無い場合に
            優先するモデル名（[llm].sub_default_model）。None（空欄）なら
            従来どおりモデル名を見ずに sub_routing_strategy で選ぶ。
            sub_endpoints に一致する model が無ければ無視される
            （src/llm/chat_model.py の build_model 参照）。
            sub_endpoints_inherit_main が True でも有効で、メインが直近
            使った接続先が別モデルなら継承せず、このモデルの接続先から選ぶ。
        temperature: 生成時のtemperature。
        top_p: 累積確率上位のみサンプリングする閾値。None なら未指定
            （llama-server既定に委ねる）。
        top_k: 上位k候補のみサンプリングする（llama.cpp拡張、OpenAI標準API
            には無いため extra_body 経由で渡す）。None なら未指定。
        repeat_penalty: 直近生成トークンの再出現を抑制する係数（llama.cpp
            拡張、extra_body 経由）。None なら未指定。
        frequency_penalty: 出現済みトークン全体への一律ペナルティ（OpenAI
            標準API）。None なら未指定。
        presence_penalty: 一度でも出現したトークンへの一律ペナルティ
            （OpenAI標準API）。None なら未指定。
        max_tokens: 1リクエストあたりの最大生成トークン数。None なら
            無制限（llama-server既定に委ねる）。
        dry_multiplier: DRY (Don't Repeat Yourself) サンプラーの強度
            （llama.cpp拡張、extra_body 経由）。repeat_penalty より長い
            フレーズ単位の反復に効く。None または 0.0 で無効。
        dry_base: DRY サンプラーの反復長に対するペナルティ指数増加率。
            None なら未指定（llama-server既定に委ねる）。
        dry_allowed_length: DRY サンプラーがこの文字数以下の反復を許容する
            閾値。None なら未指定。
        dry_penalty_last_n: DRY サンプラーが反復検出に遡って見るトークン数
            （-1 でコンテキスト全体）。None なら未指定。
        dry_sequence_breakers: DRY サンプラーが反復検出をリセットする区切り
            文字列のリスト。None なら未指定（llama-server既定に委ねる）。
        enable_thinking: Qwen3系モデルの thinking（reasoning、<think>ブロック）
            モードのON/OFF（llama.cpp拡張、extra_body の chat_template_kwargs
            経由）。None なら未指定でモデル・llama-server既定に委ねる。
            False にすると reasoning をオフにする。
        reasoning_format: thinkingタグの扱い方（llama.cpp拡張、extra_body
            経由。llama-server起動時の --reasoning-format に相当）。
            "none"/"deepseek"/"deepseek-legacy" のいずれか。None なら未指定で
            llama-server既定（auto）に委ねる。
        reasoning_budget: 思考に使えるトークン数の上限（llama.cpp拡張、
            extra_body 経由。llama-server起動時の --reasoning-budget に相当）。
            -1=無制限、0=即座に終了、N>0=上限トークン数。None なら未指定で
            llama-server既定（-1）に委ねる。リクエスト上のフィールド名は
            llama-server が reasoning_budget_tokens、vLLM が
            thinking_token_budget（dialect.build_extra_body が変換する）。
            llama-server はリクエストでの 0 を無視する（実測）ため、thinking
            を止めるには reasoning_effort=none か enable_thinking=false を使う。
        reasoning_budget_message: reasoning_budget を使い切った際に思考終了
            タグの直前へ挿入するメッセージ（llama.cpp拡張、extra_body 経由。
            llama-server起動時の --reasoning-budget-message に相当）。None
            なら未指定（挿入しない）。
        reasoning_effort: chat template へ渡す推論努力レベル（llama.cpp拡張、
            extra_body 経由。llama-server起動時の --reasoning-effort に相当）。
            LLM_REASONING_EFFORTS のいずれか。"none" は thinking 無効化。
            None なら未指定（llama-server既定に委ねる）。
        reasoning_preserve: 過去の assistant メッセージの thinking を履歴に
            残して送るか（llama-server起動時の --reasoning-preserve に相当）。
            True/False を extra_body の chat_template_kwargs
            （preserve_reasoning / preserve_thinking）で送る。True の場合は
            さらに ChatLlamaCpp が履歴の AIMessage の reasoning_content を
            リクエストへ載せ直す（langchain_openai は既定で読み捨てるため）。
            None なら未指定（llama-server既定に委ねる）。
        track_token_usage: LLM応答のトークン使用量（入力/出力/合計）を
            取得するかどうか。True の場合 build_model（src/llm.py）が
            ChatOpenAI の stream_usage=True を有効化し、app.py・eval側で
            使用量を集計・表示できるようにする。llama-server が
            stream_options.include_usage 拡張に対応していない場合のみ
            False にする（その場合トークン数は表示されない）。
        request_timeout_seconds: LLMサーバーへのHTTPリクエストのタイムアウト
            秒数（httpx.Timeoutのread/write/poolに適用。connectは別途固定値を
            使う）。build_model（src/llm.py）がhttpx.AsyncClient/httpx.Client
            の生成時に渡す。ストリーミング中はチャンク到達のたびにタイマーが
            リセットされるため、正常な長時間生成は妨げない。ThinkingLoopDetected
            発生後のaclose失敗でクライアントが壊れたまま次のリクエストが応答
            ヘッダー待ちで無期限にハングした本番incidentへの対策
            （詳細はsrc/llm.pyのChatLlamaCpp._astream / build_model参照）。
        stream_chunk_timeout_seconds: ストリーミング中にチャンクが一定時間
            届かない場合のタイムアウト秒数（langchain_openaiのstream_chunk_
            timeout。request_timeout_secondsとは別物で「チャンクとチャンクの
            間隔」の上限）。build_model（src/llm.py）がChatLlamaCppの
            コンストラクタに渡す。大きなコンテキストのプロンプト処理(prefill)
            に時間がかかる環境ほどこの秒数に到達しやすい。
        round_robin_slots_probe_timeout_seconds: round_robin/priority_failover戦略が
            provider="llama_cpp"の接続先を候補にする際に送る GET /slots
            問い合わせ自体のタイムアウト秒数。短く、確認できなければ
            fail-safe側（＝空きとみなして選ぶ）に倒す
            （src/llm/routing.py の _probe_llama_cpp_slots_available() 参照）。
        round_robin_busy_poll_interval_seconds: round_robin/priority_failover戦略で、時間帯
            制約の対象となっている全接続先のスロットが埋まっていた場合に、
            再度空き状況を確認するまで待機する秒数
            （src/llm/routing.py の _select_endpoint_with_slots_probe() 参照）。
        skills_dir: スキル群を格納するディレクトリの絶対パス。
        agents_dir: エージェント種別定義（dispatch_agent の agent_type、
            ClaudeCode の .claude/agents/*.md 相当）を格納するディレクトリの
            絶対パス。*.md 1ファイル = 1種別、frontmatterで name/
            description/tools（省略時は既定ツール一式を継承）を指定する。
        project_locohane_dirs: プロジェクト固有の拡張ディレクトリ（ClaudeCode の
            .claude/ 相当）の絶対パスのリスト。既定 [.locohane]。複数パスは
            nudge_messages と同じJSON/Python風リスト形式（角カッコ＋改行複数行
            OK）で指定できる。この配下の skills/・agents/・LOCOHANE.md から
            locohane_skills_dirs/locohane_agents_dirs/project_instructions_paths
            を導出する（load_config() 参照）。
        locohane_skills_dirs: skills_dir に追加でマージ走査するディレクトリの
            絶対パスのリスト（project_locohane_dirs の各要素 / "skills"）。
            同名スキルが両方に存在する場合は後方（.locohane側）が優先される
            （scan_skills() 参照）。
        locohane_agents_dirs: agents_dir に追加でマージ走査するディレクトリの
            絶対パスのリスト（project_locohane_dirs の各要素 / "agents"）。
            同名定義が両方に存在する場合は後方（.locohane側）が優先される
            （scan_agent_types() 参照）。
        locohane_skills_pythons: locohane_skills_dirs のうち、専用の Python
            実行ファイルが指定されたもの（project_locohane_dir の要素を
            {"dir": ..., "python": ...} 形式で書いた場合）の対応表
            （skills ディレクトリの絶対パス → Python実行ファイル）。この配下の
            .py スクリプトは run_script/run_script_background で script_python
            の代わりにこの Python で起動される（src/tools/_script_job.py の
            _script_python_for() 参照）。
        instance_locohane_dir: そのインスタンスだけが走査する拡張ディレクトリ
            （[skill_lab].instance_locohane_dir、既定 instances/<名前>）の絶対パス。
            project_locohane_dirs の後ろ（最優先）として locohane_skills_dirs/
            locohane_agents_dirs/project_instructions_paths の末尾に必ず含まれる。
            スキル研究室で昇格したスキル・サブエージェントの置き場。
        instance_dir: インスタンスの設定フォルダ（[admin].instances_dir/<名前>）の
            絶対パス。${instance_dir} プレースホルダーの展開先。
        bin_path:run_script/execute_python_code のサブプロセスへ渡す PATH の
            先頭に追加するディレクトリの絶対パスのリスト（既定は空）。
            コマンド名を素の状態で叩く前提の外部バイナリをOS側のPATH登録なしで
            呼び出せるようにする（src/tools.py の _subprocess_env() 参照）。
            存在しないディレクトリは無視される。
        system_prompt_path: システムプロンプトのテンプレートファイル
            （{{skills}} にスキル一覧を差し込む）の絶対パス。
        project_instructions_paths: プロジェクト固有の追加指示ファイル
            （ClaudeCode の CLAUDE.md 相当）の絶対パスのリスト
            （project_locohane_dirs の各要素 / "LOCOHANE.md"）。1つも
            存在しなくてもエラーにはならず、システムプロンプトの
            {{project_instructions}} には「（プロジェクト固有の指示は
            ありません）」が差し込まれる（render_project_instructions_block()
            参照）。
        checkpoint_db: LangGraph の会話状態を永続化する SQLite ファイルの絶対パス。
        log_dir: アプリケーションログの出力先絶対パス。
        log_level: ログの詳細度（"info"/"debug"/"none"のいずれか、大文字小文字は
            区別しない）。"info" は現行仕様（各ツール呼び出しの概要のみ）、
            "debug" はツール呼び出しの全引数・全結果・LLM応答本文・thinking
            （reasoning_content）まで記録、"none" はログを一切生成しない。
            app.py の _setup() がこの値に応じてロギングを設定する。
        log_clear_on_startup: 起動のたびに新しい app_*.log ファイルを作成する
            か、直近の既存ファイルに追記を試みるか。False（既定）なら直近の
            app_*.log への追記を試みる（既に log_max_lines を超えていれば
            その場で新しいファイルにローテーションする）。True なら起動の
            たびに必ず新しい日時つきファイルを作成する。log_level="none" の
            ときは意味を持たない。
        log_max_lines: 1つのログファイル（app_YYYYMMDD_HHMMSS[_N].log）が保持する
            最大行数。この行数を超えたら新しいファイルへローテーションする
            （src/log_rotation.py の LineCountRotatingFileHandler が使う）。
            0以下でローテーション無効化。log_level="none" のときは意味を
            持たない。
        log_retention_days: ローテーションで増え続ける古い app_*.log の
            保持日数。この日数を過ぎた（更新日時が古い）ファイルは自動削除
            する。0以下で無効化。同じ log_dir に evals/run_case.py が書く
            evals.log は対象外（cleanup 呼び出し側で pattern="app_*.log"
            を指定するため）。
        log_cleanup_interval_hours: 上記の自動削除チェックの実行間隔（時間）。
        chat_log_enabled: 会話ログ（ユーザー発言とAIの最終応答）をテキスト
            ファイルへ記録する機能の有効/無効（[chat_log].enabled）。
        chat_log_dir: 会話ログの保存先ルートディレクトリの絶対パス。
            実際には <chat_log_dir>/<ユーザー名>/<日付>_<thread_id>.log の
            構成で書き出す（src/chat_log.py 参照）。
        thread_store_enabled: 会話スレッド一覧（左サイドバー）・再開機能の
            有効/無効（[thread_store].enabled）。Chainlit の BaseDataLayer
            実装（src/thread_store.py）を登録するかどうかを決める。
        thread_store_db: スレッド一覧・Step 履歴を保存する SQLite ファイルの
            絶対パス（data/checkpoints.sqlite とは別ファイル。LangGraph の
            会話状態そのものではなく、一覧表示・再開に必要なメタデータのみ
            を持つ）。
        thread_store_retention_days: スレッド一覧の保持日数。この日数
            更新が無いスレッドを自動削除する。0以下で無効化（既定、
            会話履歴はアップロード等より価値が高いため無期限保持が既定）。
            LangGraph 側の checkpoints.sqlite は削除しない（孤立データが
            残るのは許容するトレードオフ）。
        thread_store_cleanup_interval_hours: 上記の自動削除チェックの
            実行間隔（時間）。
        chat_starter_prompts: チャット開始時に表示する定型文ボタンの候補
            リスト（[chat_starters].prompts）。クリックするとそのまま
            メッセージとして送信される。空リストならボタンを表示しない。
        default_workdir: エージェントの既定の作業ディレクトリ（run_script の
            cwd、スクリプトが生成するファイルの出力先の基準）。Chainlit の
            ChatSettings でセッション単位の作業ディレクトリが指定されな
            かった場合に使われる。run_script 専用ではなくエージェント全体の
            作業拠点という位置づけのため、他の run_script 実行設定とは
            分けて保存先パス群に含める。
        allow_sandbox_dirs: 書き込みサンドボックスガード（run_script/
            run_script_background 経由のみ対象。execute_python_code系は
            対象外。src/tools/_subprocess_env.py の
            _allowed_sandbox_dirs_for()/_run_script_guard_env() 参照）が
            work_dir/default_workdir に加えて書き込み・削除を許可する
            追加ディレクトリのエントリ集合（config.ini の
            [default_workdir].allow_sandbox_dir 由来）。各エントリは
            SandboxDirEntry（ディレクトリ本体 + そこへの書き込みを許可
            する対象の集合）で、実行しようとしているスキルスクリプトが
            そのエントリの allow_entries に含まれる場合にのみ、対応する
            ディレクトリが allowed_roots に追加される。既定は空タプルで、
            その場合は従来通りの書き込みサンドボックス原則がそのまま適用
            される。work_dir/default_workdirと異なりセッション・スレッド
            ごとの分離は効かず、許可されたディレクトリは全セッションから
            常時書き込み可能になる点に注意（運用者が用意した固定の共有
            出力先などを想定した設定）。存在しないディレクトリは実際の
            ガード適用時に無視される。
        memory_dir: 永続メモリー（User/Feedback/Project/Reference）の
            保存先ルートディレクトリの絶対パス。配下に4種のtype
            サブディレクトリと索引ファイル MEMORY.md を持つ
            （src/memory.py 参照）。
        help_path: help ツールが読み込んで返すヘルプ本文（ユーザー向け、
            Markdown）ファイルの絶対パス。
        chainlit_files_retention_days: Chainlit自身のセッションファイル
            ディレクトリ（`.files/<セッションID>/`。analyze_image の
            show_in_chat=True・回答本文への画像埋め込みのライブ表示中のみが
            使う）の保持日数。ディレクトリ
            単位で削除する（src/cleanup.py の cleanup_old_dirs 参照）。
            0以下で無効化。
        chainlit_files_cleanup_interval_hours: 上記の自動削除チェック間隔
            （時間）。起動時にも1回チェックする。
        elements_dir: 添付ファイル・回答本文への画像埋め込みの永続化先
            （スレッド再開・プロセス再起動後も表示するため。
            src/thread_store.py の create_element/get_element、
            app.py の _embed_local_images_as_session_urls 参照）。
        elements_retention_days: 上記の保持日数。0以下で無効化。
        elements_cleanup_interval_hours: 上記の自動削除チェック間隔
            （時間）。起動時にも1回チェックする。
        image_max_long_side_pixels: 画像をLLMへ渡す前に縮小する際の、長辺の
            ピクセル数の上限。0以下、または画像の長辺が既にこの値以下の
            場合は縮小しない（src/images.py の to_data_url 参照）。
        image_jpeg_quality: 上記の縮小時に再エンコードするJPEG品質（1-95）。
        image_inline_preview_max_long_side_pixels: LLMの回答本文（Markdown
            テーブルのセル等）へ直接埋め込む画像プレビューの、長辺ピクセル数の
            上限。image_max_long_side_pixels（Vision向け）とは別の、意図的に
            小さい値（表示用サムネイルの帯域・容量を抑える目的。
            app.py の _embed_local_images_as_session_urls 参照）。
        image_inline_preview_jpeg_quality: 上記プレビューの再エンコード品質
            （1-95）。
        image_inline_preview_min_long_side_pixels: 上記プレビューの、長辺
            ピクセル数の下限。0以下、または画像の長辺が既にこの値以上の
            場合は拡大しない。アイコンサイズ程度の小さい画像がそのまま
            埋め込まれ視認できなくなる問題への対策（src/images.py の
            _downscale_to_jpeg 参照）。
        default_workdir_retention_days: default_workdir 直下に溜まり続ける
            ファイルの保持日数。この日数を過ぎた（更新日時が古い）ファイルは
            自動削除する。0以下で無効化。ユーザーが ChatSettings で指定した
            セッション単位の work_dir は対象外。
        default_workdir_cleanup_interval_hours: default_workdir 自動削除の
            チェック間隔（時間）。起動時にも1回チェックする。
        path_memory_dir: パスメモリー（src/path_memory.py）が
            会話ごとのレジストリファイル（<thread_id>.json）を保存する
            ルートディレクトリの絶対パス。
        path_memory_retention_days: パスメモリーの保持日数。この日数を
            過ぎたレジストリファイルは自動削除する。0以下で無効化。
        path_memory_cleanup_interval_hours: パスメモリー自動削除のチェック
            間隔（時間）。起動時にも1回チェックする。
        path_memory_max_entries: パスメモリー1会話あたりの登録上限件数。
        path_memory_search_min_score: パスメモリー類似検索（search_path_memory・
            「見つかりません」エラー時の候補提示）で結果に出す類似度の下限。
        path_memory_search_filename_weight: パスメモリー類似検索で、検索語が
            フルパスの場合にファイル名部分を重視する割合（残りはフォルダ部分
            同士の類似度）。
        vector_db_enabled: ベクターDB（src/vector_db/、社内文書の意味検索）の
            有効/無効。False の場合 search_documents / list_document_collections
            は「無効です」を返すのみ。
        vector_db_backend: ベクターの保存先（VECTOR_DB_BACKENDS のいずれか）。
        vector_db_dir: catalog.sqlite（コレクション定義・取り込み済みファイル）と
            backend=sqlite_vec 時の vectors.sqlite を置くディレクトリ。
        vector_db_qdrant_url: backend=qdrant 時の Qdrant サーバー URL。
        vector_db_qdrant_api_key: Qdrant の API キー（.env の QDRANT_API_KEY のみ）。
        vector_db_qdrant_collection_prefix: Qdrant のコレクション名の接頭辞
            （${instance} 展開済み）。
        vector_db_qdrant_timeout_seconds: Qdrant への1リクエストのタイムアウト秒数（検索は最大
            qdrant_store.SEARCH_TIMEOUT_SECONDS 秒、接続は最大 qdrant_store.CONNECT_TIMEOUT_SECONDS 秒）。
        vector_db_qdrant_max_retries: Qdrant へ接続できない・接続を切られた・タイムアウトした・5xx・408・429 が返ったときに、
            vector_db_qdrant_retry_interval_seconds 秒待って同じリクエストを送り直す回数。-1 なら成功するまで無制限
            （取り込みは「停止」で止められる）。0 なら送り直さない。管理者設定（全インスタンス共通）。
            検索・コレクションの削除は止める手段が無いため、この値によらず最大1回だけ送り直す（qdrant_store.SEARCH_MAX_RETRIES）。
        vector_db_qdrant_retry_interval_seconds: 上記の送り直しの間隔（秒）。管理者設定（全インスタンス共通）。
        vector_db_qdrant_preset: Qdrant のコレクションの規模別の設定（VECTOR_DB_QDRANT_PRESETS
            のいずれか。none は Qdrant の既定のまま）。
        vector_db_distance: コレクションを作るときの距離関数（VECTOR_DB_DISTANCES の
            いずれか。backend=sqlite_vec では VECTOR_DB_SQLITE_VEC_DISTANCES のいずれか）。
        vector_db_embedding_endpoints: 埋め込みを計算する接続先（/v1/embeddings）。
            enabled=False のときは空でもよい。
        vector_db_embedding_batch_size: 埋め込み1リクエストあたりのチャンク数。
        vector_db_embedding_timeout_seconds: 埋め込み1リクエストのタイムアウト秒数。
        vector_db_embedding_routing_strategy: vector_db_embedding_endpoints が複数ある場合の
            選び方（LLM_ROUTING_STRATEGIES のいずれか。src/vector_db/embeddings.py の
            _endpoint_order()・_choose_order() 参照。round_robin/priority_failover は llama_cpp・vllm の
            接続先の空きを確かめ、round_robin_slots_probe_timeout_seconds・round_robin_busy_poll_interval_seconds
            （最大 0.5 秒）も使う）。どの戦略でも、エラーが返った接続先の次の接続先で試し直す。
        vector_db_embedding_max_retries: すべての接続先でエラーになったとき、
            vector_db_embedding_retry_interval_seconds 秒待ってから全接続先を試し直す回数。
            0 なら成功するまで無制限に繰り返す。検索語の埋め込み（search_documents）には使わない。
        vector_db_search_embedding_max_retries: 検索語の埋め込みで同じく試し直す回数。
            検索が止まらなくならないよう、0 は「試し直さない」（無制限は指定できない）。
        vector_db_embedding_retry_interval_seconds: 上記の試し直しの間隔（秒。取り込み・検索で共通）。
        vector_db_embedding_encoding_format: 埋め込みの受け取り方（VECTOR_DB_EMBEDDING_ENCODING_FORMATS の
            いずれか）。base64 は応答が小さく解析が速い。対応しない接続先は自動で float に切り替える。
        vector_db_query_prefix / vector_db_document_prefix: 検索語・文書の先頭に
            付ける文字列（e5 系モデルの "query: " / "passage: " 等）。設定値が auto なら
            埋め込みモデル名から決めた値（auto_embedding_prefixes()）が入る。
        vector_db_chunk_size / vector_db_chunk_overlap: チャンク分割の目安文字数と
            隣り合うチャンクの重複文字数。
        vector_db_include_extensions: 取り込む拡張子（小文字・"." 付き）。画像ファイルは
            vector_db_image_input が False ならファイルパスだけを登録する。
        vector_db_text_extensions: 対応形式（.pdf 等）のほかに、.txt と同じくテキストとして読む拡張子（小文字・"." 付き。
            AutoForm の .mat 等のアプリ専用のテキスト形式用）。include_extensions にも書いたものだけを取り込む。
            中身がバイナリ（NUL を含む）ならパスだけを登録する。
        vector_db_max_file_mb: 取り込む1ファイルの上限サイズ（MB）。管理者設定（全インスタンス共通）。
        vector_db_image_input: True なら image_input_extensions の文書をページごとの画像にし、それ以外の Office ファイルは
            中の画像を取り出して（embedded_images）、画像ファイル（.png/.jpg/.heic 等）もそのまま画像として埋め込む
            （マルチモーダルの埋め込みモデルが必要）。
        vector_db_image_input_extensions: image_input = True のときにページ画像も埋め込む文書の形式
            （PAGE_IMAGE_EXTENSIONS のうちの一部。含めない形式は本文だけで埋め込む）。画像ファイルは常に画像で埋め込む。
        vector_db_embedded_images: image_input = True で、ページ画像にしない Office ファイル（.docx/.pptx/.xlsx/.xlsm）の
            中に入っている画像（写真・図）を取り出し、1枚ずつ画像として埋め込むか。
        vector_db_embedded_image_min_px: 取り出す画像の幅・高さの下限（px）。どちらかが下回る画像（アイコン・罫線等）は埋め込まない。
            0 なら大きさで除かない。
        vector_db_image_dpi: 画像として取り込むときのページの解像度（DPI）。
        vector_db_office_timeout_seconds: image_input で Office ファイルを PDF に書き出す1回の上限秒数。
            超えたら（見えない確認画面で止まった等）その Office のプロセスを終了し、失敗一覧に載せる。管理者設定（全インスタンス共通）。
        vector_db_ingest_temp_dir: 取り込みの間だけ使う一時ファイル（ページ画像の PNG・Office から書き出した PDF）を置くフォルダ
            （絶対パスの文字列）。空ならシステムの一時フォルダ。この下に取り込み1回ごとのフォルダを作り、終わったら消す。
            管理者設定（全インスタンス共通）。
        vector_db_ingest_workers: 取り込みで並列に準備するファイル数（ingest_processes が 1 以上ならその数）。
        vector_db_ingest_processes: 取り込みで本文の抽出・ページの画像化を行う子プロセスの数。
            0 なら子プロセスを使わず、ワーカースレッドで行う。
        vector_db_ingest_embed_requests: 取り込みで同時に送る埋め込みのリクエスト数。0 なら ingest_workers と同じ。
        vector_db_scan_workers: 取り込み元フォルダを走査するときに、同時に読むフォルダの数（スレッド数）。管理者設定（全インスタンス共通）。
        vector_db_max_files_per_collection: 1コレクションで取り込む最大ファイル数。超えた分は取り込まず、
            ナレッジ画面に警告を出す（取り込み済みのファイルを優先し、残りはパス順の先頭から選ぶ）。管理者設定（全インスタンス共通）。
        vector_db_top_k: search_documents の既定の返却件数。
        vector_db_max_result_chars: search_documents の結果全体の最大文字数。
        vector_db_min_score: search_documents の結果に出す類似度の下限（本文のチャンク）。
        vector_db_image_min_score: 同じくページ画像（image_input）の類似度の下限。
            検索語（テキスト）と画像の類似度は本文より低めに出るため別に持つ。
        vector_db_path_min_score: search_document_paths の結果に出すファイルパスの類似度の下限。
        vector_db_rrf_weight_text / vector_db_rrf_weight_keyword / vector_db_rrf_weight_image:
            本文の埋め込み・本文のキーワード（BM25）・ページ画像の検索結果を順位で統合する
            （Reciprocal Rank Fusion）ときの重み。0 ならその検索を使わない。
        vector_db_rrf_weight_path_keyword: search_document_paths で、ファイルパスのキーワード（BM25）の結果を
            ファイルパスの埋め込みの結果（重み 1.0）と順位で統合するときの重み。0 ならキーワードを使わない。
        script_timeout: run_script の実行タイムアウト秒数。
        script_python: run_script が .py スクリプトを起動する際に使う
            Python 実行ファイル。
        code_exec_enabled: execute_python_code ツール（LLMが生成した
            Pythonコードをその場で実行する）の有効/無効。False の場合、
            ツールは呼び出されてもエラー文字列を返すのみでコードは
            実行されない。
        script_background_max_runtime_seconds: run_script_background で
            起動したプロセスを強制終了するまでの上限秒数（script_timeout とは
            別軸。同期版 run_script より長時間の実行を想定した上限）。
        script_background_job_retention_seconds: run_script_background の
            ジョブが完了・失敗・タイムアウト等で終了した後、check_script_job で
            一度も取得されないまま registry に残ってよい秒数。超過分は次回の
            run_script_background 呼び出し時に破棄される。
        script_background_min_poll_interval_seconds: check_script_job が
            「実行中」ステータスを返した直後、同じジョブへの次の
            check_script_job 呼び出しを許可するまでの最短間隔秒数
            （src/tools.py 参照）。0以下で無効化（強制なし）。
        script_background_min_poll_message: 上記の最短間隔未満で
            check_script_job が呼ばれた際にLLMへ返すメッセージのテンプレート。
            .format() で {wait_remaining}/{job_id}/{min_interval} を
            埋め込む。空欄なら DEFAULT_SCRIPT_BACKGROUND_MIN_POLL_MESSAGE
            を使う。
        script_background_inline_wait_max_seconds: run_script_background/
            execute_python_code_background のツール呼び出しが、ジョブ完了を
            この秒数までLLMを介さずコード側で待つ上限秒数。この秒数を
            超えてもまだ実行中の場合のみ job_id を返してLLMへ制御を戻す。
            ジョブ自体はこの上限に達してもキャンセルされず動き続ける。
            0以下を指定すると無期限に待つ（フォールバック経路が事実上
            無効になる。subagent_background_inline_wait_max_seconds と
            同じ設計）。
        script_background_progress_push_interval_seconds: 上記の待機中、
            人間向けに経過秒数・標準出力/標準エラー末尾をチャットへ直接
            送る間隔（秒）。cl.Message送信のみでLLM呼び出しを伴わず
            トークンを消費しない。
        script_background_job_output_tail_chars: 上記の進捗表示、および
            check_script_job/stop_script_job/read_thread_note が「実行中」
            「強制終了時点」等で末尾のみ表示する際の、標準出力/標準エラー/
            進捗メモの最大文字数。全量を表示するとコンテキストを圧迫する
            ため切り詰める。dispatch_agent の進捗表示（_dispatch_agent_job.py）
            も同じ値を共有する。
        plan_approval_exempt_scripts: run_script/run_script_background
            の計画承認（Plan Mode）を免除する、副作用のない読み取り専用
            スクリプトのホワイトリスト（{(スキル名, スクリプトファイル名), ...}）。
        subagent_agent_type_run_script_allowlist: dispatch_agent サブエージェントの
            agent_type ごとに run_script で呼べるスキル/
            スクリプトを制限するホワイトリスト（{(agent_type, 対象), ...}）。
            対象は文字列1件（例: "web-search"）ならそのスキル配下の全スクリプトを
            許可、2要素タプル（例: ("pdf-tools", "render_pdf_pages.py")）なら
            そのスクリプトのみ許可を表す。未登録の agent_type は制限なし
            （init_tools() が agent_type ごとにグループ化して
            _AGENT_TYPE_RUN_SCRIPT_ALLOWLIST へ注入する。src/tools/_state.py参照）。
        file_tools_duplicate_guard_enabled: 読み取り専用の Read/Glob/Grep/
            json_query ツールを同一引数で繰り返し呼び出すのを防ぐガード機能の
            有効/無効。
        file_tools_duplicate_guard_max_calls: 同一シグネチャ（ツール名+引数）
            の呼び出しを何回まで許可するか。この回数に達した以降の呼び出しは
            エラーで拒否する。
        file_tools_duplicate_guard_carry_over_to_main: サブエージェント
            （dispatch_agent）内での呼び出し履歴を、メインエージェントの重複
            判定へ持ち越すかどうか。True なら両者で呼び出し集合を共有し、
            False なら別々に管理する（src/tools.py の _IN_SUBAGENT 参照）。
        main_agent_tool_guard_mode: メインエージェント自身が呼び出せるツールを
            main_agent_tool_guard_allow_entries の許可リストへ限定するガード
            機能のモード（"false"/"tools_skills_only"/"all"、
            MAIN_AGENT_TOOL_GUARD_MODES参照）。plan_approval_exempt_scripts は
            計画承認を免除するだけで直接呼び出し自体は妨げないため、
            render_pdf_pages.py→analyze_image のような「1件の重い調査」を
            メインエージェントが委譲せず自分で最後まで実行し続けてトークン
            上限に達する事例（src/tools.py の _guard_main_agent_tool_limit
            参照）を防ぐための汎用ガード。ビルトインツール名（Glob・
            analyze_image 等）も run_script 配下のスキルスクリプトも同じ
            リストへ登録できる（旧・Glob専用ガード main_agent_glob_guard は
            本ガードへ統合済み）。"all" にする場合、メインエージェントに
            必要な全ツールを main_agent_tool_guard_allow_entries へ登録すること
            （未登録＝呼び出し不可のため）。"tools_skills_only" はMCP動的
            ツール（mcp__<server>__<tool>形式）のみ常に許可し、それ以外は
            "all" と同じ許可リスト判定を行う。
        main_agent_tool_guard_allow_entries: 本ガードの許可リストの集合
            （frozenset[tuple[str | tuple[str, str], int]]）。各要素は
            (対象, max_calls) のペア。対象が文字列1件（例: "Glob",
            "analyze_image"）ならビルトインツール名そのもの、2要素タプル
            （例: ("pdf-tools", "render_pdf_pages.py")）なら
            run_script/run_script_background 経由で呼ばれる
            (スキル名, スクリプトファイル名) の組を表す。max_calls は
            エントリごとに個別指定でき、メインエージェントが1ターンあたり
            そのエントリを何回まで直接呼び出せるかを表す（0=完全ブロック、
            -1=登録した上で無制限に許可、1以上=その回数まで許可。他の
            呼び出し回数ガード（_record_and_check_duplicate）は0以下を
            「無制限」として扱うが、本ガードは0と-1の意味が逆になる点に
            注意）。plan_approval_exempt_scripts とは独立したリストで、
            **ここに登録されていないツール・スクリプトは main_agent_tool_guard_
            mode="all"（または"tools_skills_only"時のMCP以外）の間、
            メインエージェントから一切呼び出せない**（許可リスト方式）。
            空集合なら main_agent_tool_guard_mode!="false" の間メインエージェント
            はMCP動的ツール（tools_skills_only時）以外いかなるツールも呼び出せ
            なくなる。
        graph_impl: ReAct ループの実装切替。"handwritten"（手書き
            StateGraph）または "prebuilt"（LangGraph の
            create_react_agent）。build_graph() が参照する。
        graph_recursion_limit: メインの ReAct ループ（agent→tools 遷移）の
            最大反復回数。LangGraph の recursion_limit にそのまま渡す
            （単位はノード遷移数で、subagent_max_iterations とは数え方が
            異なる）。
        graph_connection_error_max_retries: メインエージェントのターン中に
            LLMサーバーとの通信エラー（LLM_CONNECTION_ERRORS、src/llm.py参照）
            を検知した場合、直近使用した接続先を一時的にクールダウンした上で
            （mark_last_endpoint_failed。main_routing_strategy=priority_failover
            の場合のみ次点の接続先へ切り替わり、round_robin/random は呼び出し
            ごとに元から切り替わる）グラフを再構築し、同じ反復をこの回数まで
            自動リトライする（app.py の on_message 参照）。0を指定すると
            従来通りリトライせず、ユーザーへ通信エラーを通知してターンを
            中断する。サブエージェント（dispatch_agent）側の同種の設定は
            [subagent].background_llm_timeout_max_retries。
        graph_tool_max_parallel: メインエージェントのツール呼び出し
            （ImageAwareToolNode）を、1セッションあたり同時に何件まで
            並列実行してよいか。ToolNode は同一AIMessage内の複数tool_calls
            を asyncio.gather() で完全並列実行するため、共有リソース
            （llama-server・DB等）への同時アクセスや呼び出し順の乱れが
            起きうる（subagent_max_parallel と同じ理由づけのメイン
            エージェント版）。セッション（thread_id）ごとに独立した
            asyncio.Semaphore を持つ（src/tools.py の _TOOL_CALL_SEMAPHORES
            参照）ため、この値は「1セッションが同時に使える枠」の上限であり、
            複数セッション間の並列自体は妨げない。1以上はその値までに
            ガードし（既定1＝完全直列化）、0以下はガードを無効化して
            並列呼び出しをそのまま許可する。
        graph_token_guard_enabled: メインエージェントの1リクエストあたりの
            トークン量を監視し、上限が近づいたら引継ぎプロンプトの生成を
            促す機能の有効/無効（src.main_token_guard 参照）。
        graph_token_guard_soft_threshold: 直近1回のLLM応答の total_tokens が
            この値以上になったら、次のモデル呼び出しの入力へ
            graph_handoff_prompt_path の文言を差し込む。
        graph_handoff_prompt_path: 上記で差し込む文言（新しいチャットへの
            引継ぎ手順）のMarkdownファイルの絶対パス。
        subagent_max_iterations: dispatch_agent が内部で回す ReAct
            ループの最大反復回数（agent→tools 遷移の回数）。
        subagent_max_parallel: dispatch_agent ツールの実LLM呼び出しを、
            1セッションあたり同時に何件まで許可するか。単一インスタンスの
            llama-serverへdispatch_agentの並列リクエストが飛ぶとチェック
            ポイント破損（ToolMessage欠落によるValueError）が本番で発生した
            ための保険措置。セッション（thread_id）ごとに独立した
            asyncio.Semaphore を持つ（src/tools.py の
            _DISPATCH_AGENT_SEMAPHORES 参照）ため、この値は「1セッションが
            同時に使える枠」の上限であり、複数セッション間の並列自体は
            妨げない。1以上はその値までにガードし（既定1＝完全直列化）、
            0以下はガードを無効化して並列呼び出しをそのまま許可する（検証用）。
        subagent_token_guard_enabled: dispatch_agent 内のLLM応答の
            usage_metadata.total_tokens を監視し、閾値超過時に注意喚起
            （ソフト）→打ち切り（ハード）を行う機能の有効/無効。
            track_token_usage=False の場合は usage_metadata が取得できず
            実質発火しない（run_subagent 側で無条件に無効化される）。
        subagent_token_guard_soft_threshold: 直近1回のLLM呼び出しの
            total_tokens がこの値以上になったら、そのiterationのtool_calls
            は通常通り実行した上で、次のモデル呼び出し前に
            subagent_token_guard_soft_warning_text の注意メッセージを
            1回だけ注入する。
        subagent_token_guard_soft_warning_text: 上記ソフト閾値到達時に
            注入する注意メッセージの文言。
        subagent_token_guard_hard_threshold: ソフト警告後もなお
            total_tokens がこの値以上の応答が続いた場合、そのtool_calls
            は実行せず、それ以上model.ainvoke()を呼ばずに打ち切る
            （subagent_max_iterations到達時と同じ要約フォーマットで返す）。
            subagent_token_guard_soft_threshold以上の値を設定すること。
        subagent_empty_response_max_retries: dispatch_agent内のLLM応答が
            tool_callsも本文も空（LLMサーバー側の異常応答。本番ログ
            2026-07-23で確認）だった場合に再試行する最大回数。この回数を
            使い切ってもなお空の応答が続いた場合は、正常終了として空文字列を
            返さず、それまでに集めたツール実行結果を要約して打ち切る
            （subagent_max_iterations到達時と同じ要約フォーマット）。
        approval_timeout_seconds: create_plan/approve_plan の計画承認で
            ユーザーの応答を待つ秒数。未応答は安全側に倒してタイムアウト
            扱いにする。0以下は無期限待ち（タイムアウトしない）。
        ask_user_question_timeout_seconds: AskUserQuestion（自由記述の
            質問。labels省略時は単発質問、labels指定時は複数項目フォーム）
            でユーザーの応答を待つ秒数。0以下は無期限待ち。
        ask_user_choice_timeout_seconds: ask_user_choice（選択肢形式の
            質問）でユーザーの応答を待つ秒数。0以下は無期限待ち。
        plan_badge_allow_unlock: 送信ボタン付近の Plan Mode / Edit Automatically
            バッジをクリックした際、Plan Mode → Edit Automatically 方向への
            切り替え（ロック解除）も許可するか。False にすると Edit
            Automatically → Plan Mode 方向（ロック）のクリックのみ有効になり、
            ロック解除は approve_plan（ユーザー承認フロー）経由に限定される
            （config.ini の [plan].allow_badge_unlock 由来）。
        plan_reset_approval_on_recreate: 既に Edit Automatically（計画承認済み）の
            状態で create_plan が再度呼ばれた際、plan_approved を強制的に
            False へ戻す（Plan Mode へ戻す）か。True（既定）なら常に戻し
            approve_plan による再承認を必須にする。False なら承認済み状態を
            維持したまま steps だけ差し替える（未承認状態からの呼び出しは
            この設定に関わらず常に Plan Mode のまま）。
            （config.ini の [plan].reset_approval_on_recreate 由来）。
        plan_reset_approval_on_new_message: 新しいユーザーメッセージを受け取る
            たびに plan_approved を無条件で False へ戻す（Plan Mode へ戻す）か。
            True（既定）なら常に戻し、承認済みの計画が途中でも新しいメッセージ
            ごとに approve_plan による再承認を必須にする（計画承認を
            「このユーザーメッセージへの応答」に限定するための従来挙動）。
            False なら承認済み状態をメッセージをまたいで維持する。
            thinking_loop_guard がリトライ上限に達して打ち切られ、ユーザーが
            続行を促す新しいメッセージを送った場合も同一タスクの継続として
            扱われ、承認済みの計画をやり直させられずに済む。
            （config.ini の [plan].reset_approval_on_new_message 由来）。
        plan_auto_approve: True の場合、approve_plan 呼び出し時にユーザーへの
            確認（承認/却下ボタンの表示・応答待ち）を一切行わず、その場で
            自動的に承認済み扱いにする。書き込み系ツール（run_script/
            execute_python_code等）が人の確認を経ずに実行されるようになるため、
            信頼できる自動化用途以外では False（既定）を推奨する。
            （config.ini の [plan].auto_approve 由来）。
        thinking_loop_guard_enabled: LLM応答（thinking/本文）のストリーミング中に
            反復ループを検知したら生成を打ち切って再試行する機能の有効/無効。
        thinking_loop_guard_target: ループ検知の監視対象（THINKING_LOOP_GUARD_TARGETS）。
            "all" は思考と本文の両方、"content_only" は本文だけを監視し、思考の
            ループは思考予算（[llm].reasoning_budget）での打ち切りに任せる。
            "thinking_only" は思考だけを監視する。
        thinking_loop_guard_window_chars: ループ検知の判定対象に使う
            直近テキストのウィンドウ文字数。
        thinking_loop_guard_check_interval_chars: このバイト数増えるごとに
            再チェックする。
        thinking_loop_guard_confirm_count: 反復判定条件が連続で何回成立したら
            確定でループと判定するか（誤検知防止）。
        thinking_loop_guard_max_history_chars: 直近ウィンドウとの最長一致
            部分文字列を探す際に比較対象とする、過去履歴の上限文字数
            （MAX_K）。大きいほど長い周期の反復も検知できるが計算コストが
            増える。
        thinking_loop_guard_match_ratio_threshold: 直近ウィンドウと過去履歴の
            最長一致長をwindow_charsで割った値（match_ratio）がこの値を
            上回った場合にループ確定とする（真の反復ループと正当なJSON生成等を
            区別するため）。
        thinking_loop_guard_max_retries: ループ検知後、注意メッセージを注入して
            再試行する最大回数。
        thinking_loop_guard_nudge_messages: ループ検知後に注入する注意メッセージの
            候補リスト。リトライ回数に応じて順番に使い、使い切ったらランダムに
            選ぶ（src.llm.pick_loop_nudge_message 参照）。空リストなら組み込みの
            既定文言を使う。
        thinking_loop_guard_empty_response_max_retries: 無言終了（tool_calls も
            本文も空のAIMessage）を検知した場合、最終回答を促して再試行する
            最大回数。thinking_loop_guard_max_retries と合算予算
            （total_retries）を共有する。
        tool_loop_guard_enabled: 全く同じ応答（本文とツール呼び出しの名前・引数）を
            連続で繰り返すループを検知し、
            [thinking_loop_guard] と同じ仕組み（注意メッセージを入れて再試行、
            max_retries で停止を通知）で回復させる機能の有効/無効
            （src.tool_loop_guard 参照）。
        tool_loop_guard_max_repeats: 全く同じ応答が何回連続したらループとみなすか。
            ツールの結果は比べない。1以下なら無効。
        tool_loop_guard_exclude_tools: このツールだけを呼ぶ応答は数えない
            （状態確認のポーリング等、同じ呼び出しを正当に繰り返すツール）。
        tool_loop_guard_nudge_messages: ループ検知後に注入する注意メッセージの
            候補リスト。思考のループとは別に数えた回数に応じて順番に使い、
            使い切ったらランダムに選ぶ。末尾には何を繰り返したかの説明が
            添えられる。空リストなら組み込みの既定文言を使う。
        thinking_control_enabled: ReActループの各ステップで、直前の履歴の形から
            思考レベル（THINKING_LEVELS）を選んでリクエストごとに切り替える機能
            全体の有効/無効（src.llm.thinking_control 参照）。False なら従来と
            全く同じリクエストになる。[llm] 側で思考が無効
            （enable_thinking=false / reasoning_effort=none）なら有効でも働かない。
        thinking_control_apply_to_main / thinking_control_apply_to_sub:
            メインエージェント / サブエージェントのループに適用するか。
            要約・圧縮のためのLLM呼び出しには常に適用しない。
        thinking_control_rule_user_turn: 末尾がユーザー発言（ナッジ含む）の
            ときのレベル（ルール1）。None（config.ini では false）ならルール無効。
        thinking_control_rule_tool_error: 直前のツール結果がエラーのときの
            レベル（ルール2）。None ならルール無効。
        thinking_control_rule_consecutive_cap: 直前 max_consecutive_reduced 回が
            全てこのレベル未満なら、このレベルにする（ルール3）。None なら無効。
        thinking_control_rule_after_tools: 直前に呼んだツールが全て after_tools
            にあるときのレベル（ルール4）。None ならルール無効。
        thinking_control_default_level: どのルールにも当たらなかったときの
            レベル（ルール5）。
        thinking_control_budget_low / _medium / _high / _xhigh: 各レベルの
            思考予算（-1 = 無制限）。None なら [llm].reasoning_budget を使う。
        thinking_control_after_tools: ルール4の対象ツール名。
        thinking_control_error_prefixes: ツールの結果がこの文字列で始まれば
            エラーとみなす（ルール2。ToolMessage.status="error" も対象）。
        thinking_control_max_consecutive_reduced: ルール3が働くまでの連続回数。
            0以下なら働かない。
        context_trim_enabled: 会話履歴中の古い ToolMessage を切り詰めて
            LLMへの入力を抑える機能の有効/無効（src.context_trim 参照）。
            メインエージェント（src.graph）向けの設定。サブエージェント
            （src.subagent の run_subagent、dispatch_agent のローカル履歴）は
            対応する context_trim_subagent_* を使う（config.ini の
            [context_trim.subagent] に項目があればその値、無ければこちらの
            値を継承する）。
        context_trim_keep_recent_tool_iterations: 全文保持する直近の反復数
            （src.context_trim.find_iteration_cut_index）。「1反復」は
            ReActループ1周＝LLM呼び出し1回（AIMessage 1件とそれに対応する
            ToolMessage群）で、メインの agent→tools 遷移1回・サブエージェント
            の1 iteration がそのまま単位になる。それより古い ToolMessage の
            み切り詰め対象にする。ユーザーの発話回数では数えない（画像
            フォローアップや各種nudgeがHumanMessageとして積まれるためずれ、
            注入が増えるほど保持範囲が広がってトリムが効かなくなる）。1回の
            AIMessageが並列発行した複数tool_callsに対応するToolMessage群は
            必ず反復単位で丸ごと同じ側（保持/切り詰め）に入り、一部だけ
            分断されることはない。
        context_trim_truncated_max_chars: 切り詰め対象 ToolMessage /
            AIMessage の content を、先頭何文字まで残すか
            （超過分はマーカー文言に置換）。context_trim_duplicate_guard_tool_max_chars
            の対象ツール（Read/Glob/Grep/json_query/analyze_image）には適用されない。
        context_trim_duplicate_guard_tool_max_chars: Read/Glob/Grep/
            json_query/analyze_image（src.tools の _check_file_tools_duplicate
            等、同一引数での再呼び出しに上限回数があるツール）の ToolMessage
            にだけ適用する切り詰め文字数。これらのツールは上限到達時
            「会話履歴にある前回の実行結果を参照してください」と案内するが、
            前回結果が context_trim_truncated_max_chars（既定は小さめの値）で
            切り詰められていると、実際にはモデルへ渡っていない分を参照させる
            ことになり案内が機能しない。そのため通常より大きめの値を
            別枠で持たせる。
        context_trim_ai_messages: ToolMessage だけでなく AIMessage
            （モデル自身の思考本文と tool_calls の引数）も切り詰めるか。
        context_trim_keep_recent_ai_iterations: 全文保持する直近の反復数
            （context_trim_keep_recent_tool_iterations と同じ判定方式・独立した値）。
            それより古い AIMessage のみ切り詰め対象にする。
        context_trim_trigger_total_tokens: トリムを発動させる閾値
            （Claude API の context editing、clear_tool_uses_20250919 の
            trigger.value 相当）。会話履歴内のLLM呼び出しの total_tokens が
            この値未満のうちはトリムを発動しない。一度でも達したら以後その
            会話ではトリムを継続し、コンテキスト圧縮で解除される
            （src.context_trim.is_trigger_reached）。0以下を指定すると常に発動する（この閾値機能が
            無かった旧来の挙動と同じ）。track_token_usage=false 等で
            total_tokens を取得できない場合は、閾値未到達とみなし発動しない。
        context_compaction_enabled: メインエージェントの累積トークン数、または
            直近1回のLLM呼び出しのトークン数が閾値を超えたら会話履歴を要約して
            圧縮する機能の有効/無効（src.context_compaction 参照）。context_trim
            と異なり永続履歴（checkpointer上のメッセージ）自体を書き換える。
            context_trim と同様、サブエージェント（src.subagent の
            run_subagent）は対応する context_compaction_subagent_* を使う
            （config.ini の [context_compaction.subagent] に項目があればその値、
            無ければこちらの値を継承する）。ただし「累積トークン数」の起点は
            メインとサブで別物である点に注意（メインはスレッド全体を通じた
            累積、サブは個々の dispatch_agent 呼び出し1回の中だけの累積。
            圧縮発火のたびに0へリセットされるのは共通）。
        context_compaction_token_threshold: 圧縮を発火させる、メインエージェントの
            累積 total_tokens（token_usage_cumulative_main、圧縮発火のたびに
            リセットされる）の閾値。直近1回のLLM呼び出し分だけで判定すると、
            context_trim による送信ペイロード削減の影響で閾値未満に収まり
            続け、圧縮が発火しないまま永続履歴だけが肥大化しうるため、
            累積値でも判定する（context_compaction_single_request_token_threshold
            とのOR判定）。track_token_usage=False の場合は判定材料が無いため
            この条件は実質発火しない。
        context_compaction_single_request_token_threshold: 圧縮を発火させる、
            直近1回のLLM呼び出しの total_tokens の閾値。会話全体の累積は
            低くても、1ターンで巨大なツール結果やファイル内容を一気に積む
            などして単発のリクエストがモデルのcontext window上限に迫る
            ケースを検知するためのもの。低パラメータモデルでは1リクエスト
            あたりcontext window未満に収める必要があるため、ツール結果1往復分
            の余裕を見てそれより低い値にすること。track_token_usage=False の
            場合は判定材料が無いためこの条件は実質発火しない。
        context_compaction_keep_recent_iterations: 圧縮時に丸ごと保持する直近の
            反復数（context_trim_keep_recent_tool_iterations と同じ単位＝
            ReActループ1周＝LLM呼び出し1回）。tool_calls とそれに対応する
            ToolMessage の対応関係を壊さないよう、安全な切断点
            （src.context_trim.find_iteration_cut_index）でのみ古い側を
            切り離す。圧縮は永続履歴を要約で置き換える恒久的な操作のため、
            この反復数とは別に「末尾のHumanMessage以降は必ず原文のまま残す」
            上限も内部で被せる（src.context_compaction._find_compaction_cut_index。
            ただし適用すると要約対象が空になる場合は、圧縮の機会自体が
            失われるため適用しない）。
        context_compaction_min_messages_to_compact: 会話全体のメッセージ数が
            この件数未満なら、閾値を超えていても圧縮しない安全弁。
        context_compaction_prompt_path: 要約を指示するプロンプト本文
            （Markdown）の絶対パス。
        context_compaction_summary_source_max_chars: 要約対象の古い
            ToolMessage を要約LLMへ渡す前に切り詰める文字数。context_trim
            （プリフィル短縮が目的で、直近以外の全ツール結果に一律適用
            される小さめの値）とは別枠。要約は永続履歴を置き換える
            恒久的な操作のため、context_trim と同じ値を使うと要約対象
            ツール結果の情報がまとめて失われ、要約が内容の薄いものに
            なりうる（大量ファイル処理タスクでファイル名の列挙しか
            残らない等）。
        context_compaction_skill_reattach_max_chars_per_skill: 圧縮で要約対象に
            入った read_skill 結果（スキル本文）を要約の後ろへ再添付する際の、
            1スキルあたりの最大文字数（src/context_compaction.py の
            _render_reattached_skills）。サブエージェントもこの値を共有する。
        context_compaction_skill_reattach_total_max_chars: 上記の再添付全体の
            最大文字数。収まらない古いスキルは名前だけを列挙する。
        context_compaction_pre_note_threshold: 圧縮が発火する前に
            write_thread_noteへの書き出しを促す注意メッセージを注入する、
            直近1回のLLM呼び出しの total_tokens の閾値
            （src.context_compaction.maybe_append_precompact_note_nudge）。
            context_compaction_single_request_token_threshold より十分
            小さい値にすること（注意メッセージ注入後、実際に圧縮が発火
            するまでの「次の1応答」でwrite_thread_noteを呼ぶ余地を残す
            ため）。0以下を指定するとこの機能を無効化する。
            src.main_token_guard の引継ぎ促し
            （graph_token_guard_soft_threshold、会話全体を打ち切って
            新しいチャットへの引継ぎを促す想定）とは別物で、こちらは
            会話を止めずに注意喚起するだけの軽いフック。
        context_compaction_pre_note_warning_text: 上記閾値到達時に注入する
            注意メッセージの文言。
        context_compaction_require_note_max_skips: 圧縮の発火条件に「直近
            context_compaction_keep_recent_iterations以内にwrite_thread_noteが
            呼ばれていること」を追加で課す
            （src.context_compaction.is_compaction_blocked_by_missing_note）。
            未呼び出しのまま閾値超過（token_threshold/
            single_request_token_threshold）が続いて圧縮を見送った回数が
            この値に達すると、記録が無くても圧縮を強制する安全弁。0を
            指定すると無期限に待つ（write_thread_noteが呼ばれるまで圧縮
            しない。token_guardのsoft/hard閾値による打ち切りに委ねる
            ことになる）。
        context_trim_subagent_*: 上記 context_trim_* の各項目のサブエージェント
            専用版。config.ini の [context_trim.subagent] に対応するキーが
            あればその値、無ければ同名の context_trim_* の値を継承する
            （[context_trim.subagent] セクション自体が無ければ全項目継承）。
        context_compaction_subagent_*: 上記 context_compaction_* の各項目の
            サブエージェント専用版。継承ルールは context_trim_subagent_* と
            同じ（[context_compaction.subagent] 参照）。
        auth_enabled: ログイン認証機能のON/OFF（[auth].enabled）。True の場合、
            app.py がモジュール読み込み時に @cl.password_auth_callback を
            登録し、未ログインユーザーはチャット画面にアクセスできなくなる。
        auth_require_password: auth_enabled=True のとき、ユーザー名だけで
            なくパスワードの一致まで要求するかどうか（[auth].require_password）。
            False の場合、auth_users に登録済みのユーザー名であればパスワードの
            内容は問わない。
        auth_users: ログイン可能なユーザー名→パスワードの対応表。config.ini
            には対応するキーが存在せず、環境変数 AUTH_USERS（.env 推奨）
            のみから読む機密情報専用フィールド（他フィールドと異なり
            config.ini 側フォールバックを持たない）。
    """

    # --- LLM (llama.cpp server / OpenAI 互換) ---
    main_endpoints: tuple[LLMEndpoint, ...]
    main_routing_strategy: str
    sub_endpoints: tuple[LLMEndpoint, ...]
    sub_endpoints_inherit_main: bool
    sub_routing_strategy: str
    sub_default_model: str | None
    temperature: float
    top_p: float | None
    top_k: int | None
    repeat_penalty: float | None
    frequency_penalty: float | None
    presence_penalty: float | None
    max_tokens: int | None
    dry_multiplier: float | None
    dry_base: float | None
    dry_allowed_length: int | None
    dry_penalty_last_n: int | None
    dry_sequence_breakers: list[str] | None
    enable_thinking: bool | None
    reasoning_format: str | None
    reasoning_budget: int | None
    reasoning_budget_message: str | None
    reasoning_effort: str | None
    reasoning_preserve: bool | None
    track_token_usage: bool
    request_timeout_seconds: float
    stream_chunk_timeout_seconds: float
    llm_max_concurrent_requests: int
    round_robin_slots_probe_timeout_seconds: float
    round_robin_busy_poll_interval_seconds: float

    # --- 保存先パス（すべて絶対パス） ---
    skills_dir: Path
    agents_dir: Path
    project_locohane_dirs: list[Path]
    locohane_skills_dirs: list[Path]
    locohane_agents_dirs: list[Path]
    locohane_skills_pythons: dict[Path, str]
    # [skill_lab].instance_locohane_dir の解決結果（そのインスタンスだけが走査する拡張ディレクトリ）。
    # locohane_skills_dirs/locohane_agents_dirs/project_instructions_paths の末尾（最優先）に
    # 必ず含まれる。スキル研究室の昇格先（admin/skill_lab/promotion.py）。
    instance_locohane_dir: Path
    # インスタンスの設定フォルダ（[admin].instances_dir/<インスタンス名>）。${instance_dir} の展開先。
    instance_dir: Path
    bin_path: list[Path]
    system_prompt_path: Path
    project_instructions_paths: list[Path]
    # [paths].common_data_dir の解決結果（${instance} 展開済み）。管理ツールの
    # インスタンス削除時に、データ削除対象の候補として使う（admin/instances.py）。
    common_data_dir: Path
    checkpoint_db: Path
    log_dir: Path
    log_level: str
    log_clear_on_startup: bool
    default_workdir: Path
    allow_sandbox_dirs: tuple[SandboxDirEntry, ...]
    memory_dir: Path
    plans_dir: Path
    help_path: Path

    # --- ユーザー別ドラフトスキル（[skill_creator]、src/skill_drafts.py） ---
    # skill-creator が作るドラフトスキルの置き場。起動時のスキル走査対象
    # （skills_dir/project_locohane_dirs）とは重ならないことを load_config() が保証する。
    skill_draft_dir: Path
    # スキル安定化トライアウト（同じケースを何回連続で通せば合格とするか）の既定回数。
    skill_tryout_repeats: int
    # 他ユーザーのドラフトの扱い（hidden/listed/readable/full、SKILL_DRAFT_VISIBILITY_MODES）。
    skill_other_users_drafts: str
    # 昇格したドラフトの退避先（スキル走査対象・skill_draft_dir と重ならないことを load_config() が保証する）。
    skill_archive_dir: Path

    # --- スキル調整ワーカー（[skill_lab_worker]、admin/skill_lab/worker.py） ---
    skill_lab_max_iterations: int
    skill_lab_max_hours: float
    skill_lab_poll_interval_seconds: float
    skill_lab_fixer_max_file_chars: int
    skill_lab_transcript_excerpt_chars: int

    # --- Chainlitセッションファイルディレクトリ（.files/）の自動削除 ---
    chainlit_files_retention_days: int
    chainlit_files_cleanup_interval_hours: float

    # --- 添付ファイル・埋め込み画像の永続化先（data/elements/）の自動削除 ---
    elements_dir: Path
    elements_retention_days: int
    elements_cleanup_interval_hours: float

    # --- 画像をLLMへ渡す前の縮小 ---
    image_max_long_side_pixels: int
    image_jpeg_quality: int

    # --- 回答本文へ埋め込む画像プレビューの縮小 ---
    image_inline_preview_max_long_side_pixels: int
    image_inline_preview_jpeg_quality: int
    image_inline_preview_min_long_side_pixels: int

    # --- default_workdir 直下のファイルの自動削除 ---
    default_workdir_retention_days: int
    default_workdir_cleanup_interval_hours: float

    # --- パスメモリー（src/path_memory.py）の保存・自動削除 ---
    path_memory_dir: Path
    path_memory_retention_days: int
    path_memory_cleanup_interval_hours: float
    path_memory_max_entries: int
    path_memory_search_min_score: float
    path_memory_search_filename_weight: float

    # --- ベクターDB（[vector_db]、src/vector_db/） ---
    vector_db_enabled: bool
    vector_db_backend: str
    vector_db_dir: Path
    vector_db_qdrant_url: str
    vector_db_qdrant_api_key: str
    vector_db_qdrant_collection_prefix: str
    vector_db_qdrant_timeout_seconds: float
    vector_db_qdrant_max_retries: int
    vector_db_qdrant_retry_interval_seconds: float
    vector_db_qdrant_preset: str
    vector_db_distance: str
    vector_db_embedding_endpoints: tuple[LLMEndpoint, ...]
    vector_db_embedding_batch_size: int
    vector_db_embedding_timeout_seconds: float
    vector_db_embedding_routing_strategy: str
    vector_db_embedding_max_retries: int
    vector_db_search_embedding_max_retries: int
    vector_db_embedding_retry_interval_seconds: float
    vector_db_embedding_encoding_format: str
    vector_db_query_prefix: str
    vector_db_document_prefix: str
    vector_db_chunk_size: int
    vector_db_chunk_overlap: int
    vector_db_include_extensions: tuple[str, ...]
    vector_db_text_extensions: tuple[str, ...]
    vector_db_max_file_mb: float
    vector_db_image_input: bool
    vector_db_image_input_extensions: tuple[str, ...]
    vector_db_embedded_images: bool
    vector_db_embedded_image_min_px: int
    vector_db_image_dpi: int
    vector_db_office_timeout_seconds: float
    vector_db_ingest_temp_dir: str
    vector_db_ingest_workers: int
    vector_db_ingest_processes: int
    vector_db_ingest_embed_requests: int
    vector_db_scan_workers: int
    vector_db_max_files_per_collection: int
    vector_db_top_k: int
    vector_db_max_result_chars: int
    vector_db_min_score: float
    vector_db_image_min_score: float
    vector_db_path_min_score: float
    vector_db_rrf_weight_text: float
    vector_db_rrf_weight_keyword: float
    vector_db_rrf_weight_image: float
    vector_db_rrf_weight_path_keyword: float

    # --- ログファイル（app_*.log）の行数ベースローテーション・自動削除 ---
    log_max_lines: int
    log_retention_days: int
    log_cleanup_interval_hours: float

    # --- 会話ログ（[chat_log]） ---
    chat_log_enabled: bool
    chat_log_dir: Path

    # --- 会話スレッド一覧・再開（[thread_store]） ---
    thread_store_enabled: bool
    thread_store_db: Path
    thread_store_retention_days: int
    thread_store_cleanup_interval_hours: float

    # --- チャット開始時の定型文ボタン（[chat_starters]） ---
    chat_starter_prompts: list[str]
    # 英語表示のセッションで使う定型文（[chat_starters].prompts_en）。空なら
    # chat_starter_prompts をそのまま使う（app.py の on_chat_start 参照）。
    chat_starter_prompts_en: list[str]

    # --- run_script / execute_python_code 共通実行設定 ---
    script_timeout: int
    script_python: str
    code_exec_enabled: bool

    # --- run_script_background 用設定 ---
    script_background_max_runtime_seconds: int
    script_background_job_retention_seconds: int
    script_background_min_poll_interval_seconds: int
    script_background_min_poll_message: str
    script_background_inline_wait_max_seconds: int
    script_background_progress_push_interval_seconds: int
    script_background_job_output_tail_chars: int

    # --- run_script/run_script_background の計画承認免除ホワイトリスト ---
    plan_approval_exempt_scripts: frozenset[tuple[str, str]]

    # --- dispatch_agent のagent_typeごとに run_script で呼べるスキル/スクリプトを制限するホワイトリスト ---
    subagent_agent_type_run_script_allowlist: frozenset[tuple[str, str | tuple[str, str]]]

    # --- Read/Glob/Grep/json_query 重複呼び出しガード（src/tools.py の _check_file_tools_duplicate） ---
    file_tools_duplicate_guard_enabled: bool
    file_tools_duplicate_guard_max_calls: int
    file_tools_duplicate_guard_carry_over_to_main: bool

    # --- メインエージェント自身の任意ツール直接呼び出し回数ガード（ビルトインツール名・
    #     run_script配下のスキルスクリプトの両方を、エントリごとの max_calls 付きで
    #     登録できる。Glob専用だった旧ガードもここへ統合済み。src/tools.py の
    #     _guard_main_agent_tool_limit） ---
    main_agent_tool_guard_mode: str
    main_agent_tool_guard_visibility_mode: str
    main_agent_tool_guard_allow_entries: frozenset[tuple[str | tuple[str, str], int]]

    # --- グラフ実装切替 ---
    graph_impl: str
    graph_recursion_limit: int
    graph_connection_error_max_retries: int
    graph_tool_max_parallel: int
    graph_token_guard_enabled: bool
    graph_token_guard_soft_threshold: int
    graph_handoff_prompt_path: Path

    # --- サブエージェント（dispatch_agent）設定 ---
    subagent_max_iterations: int
    subagent_max_parallel: int
    subagent_token_guard_enabled: bool
    subagent_token_guard_soft_threshold: int
    subagent_token_guard_soft_warning_text: str
    subagent_token_guard_hard_threshold: int
    subagent_empty_response_max_retries: int
    subagent_background_job_retention_seconds: int
    subagent_background_min_poll_interval_seconds: int
    subagent_background_min_poll_message: str
    subagent_background_inline_wait_max_seconds: int
    subagent_background_progress_push_interval_seconds: int
    subagent_background_llm_timeout_max_retries: int
    subagent_batch_max_groups: int
    subagent_batch_stop_after_unreachable_groups: int

    # --- ユーザー応答待ちタイムアウト（Chainlit の Ask*Message） ---
    approval_timeout_seconds: int
    ask_user_question_timeout_seconds: int
    ask_user_choice_timeout_seconds: int

    # --- Plan Mode / Edit Automatically バッジ（送信ボタン付近のUI） ---
    plan_badge_allow_unlock: bool
    plan_reset_approval_on_recreate: bool
    plan_reset_approval_on_new_message: bool
    plan_auto_approve: bool

    # --- LLM応答の反復ループ検知（src/llm.py の ChatLlamaCpp） ---
    thinking_loop_guard_enabled: bool
    thinking_loop_guard_target: str
    thinking_loop_guard_window_chars: int
    thinking_loop_guard_check_interval_chars: int
    thinking_loop_guard_confirm_count: int
    thinking_loop_guard_max_history_chars: int
    thinking_loop_guard_match_ratio_threshold: float
    thinking_loop_guard_max_retries: int
    thinking_loop_guard_nudge_messages: list[str]
    thinking_loop_guard_empty_response_max_retries: int

    # --- 全く同じ応答（ツール呼び出し）を連続で繰り返すループの検知（src/tool_loop_guard.py） ---
    tool_loop_guard_enabled: bool
    tool_loop_guard_max_repeats: int
    tool_loop_guard_exclude_tools: list[str]
    tool_loop_guard_nudge_messages: list[str]

    # --- ステップごとの思考レベル切り替え（src/llm/thinking_control.py） ---
    thinking_control_enabled: bool
    thinking_control_apply_to_main: bool
    thinking_control_apply_to_sub: bool
    thinking_control_rule_user_turn: str | None
    thinking_control_rule_tool_error: str | None
    thinking_control_rule_consecutive_cap: str | None
    thinking_control_rule_after_tools: str | None
    thinking_control_default_level: str
    thinking_control_budget_low: int | None
    thinking_control_budget_medium: int | None
    thinking_control_budget_high: int | None
    thinking_control_budget_xhigh: int | None
    thinking_control_after_tools: list[str]
    thinking_control_error_prefixes: list[str]
    thinking_control_max_consecutive_reduced: int

    # --- 会話履歴トリミング（src/context_trim.py） ---
    context_trim_enabled: bool
    context_trim_keep_recent_tool_iterations: int
    context_trim_truncated_max_chars: int
    context_trim_duplicate_guard_tool_max_chars: int
    context_trim_ai_messages: bool
    context_trim_keep_recent_ai_iterations: int
    context_trim_trigger_total_tokens: int

    # --- 会話履歴トリミング・サブエージェント専用上書き（[context_trim.subagent]） ---
    context_trim_subagent_enabled: bool
    context_trim_subagent_keep_recent_tool_iterations: int
    context_trim_subagent_truncated_max_chars: int
    context_trim_subagent_duplicate_guard_tool_max_chars: int
    context_trim_subagent_ai_messages: bool
    context_trim_subagent_keep_recent_ai_iterations: int
    context_trim_subagent_trigger_total_tokens: int

    # --- 会話履歴の自動要約・圧縮（src/context_compaction.py） ---
    context_compaction_enabled: bool
    context_compaction_token_threshold: int
    context_compaction_single_request_token_threshold: int
    context_compaction_keep_recent_iterations: int
    context_compaction_min_messages_to_compact: int
    context_compaction_prompt_path: Path
    context_compaction_summary_source_max_chars: int
    context_compaction_skill_reattach_max_chars_per_skill: int
    context_compaction_skill_reattach_total_max_chars: int
    context_compaction_pre_note_threshold: int
    context_compaction_pre_note_warning_text: str
    context_compaction_require_note_max_skips: int

    # --- 会話履歴の自動要約・圧縮・サブエージェント専用上書き（[context_compaction.subagent]） ---
    context_compaction_subagent_enabled: bool
    context_compaction_subagent_token_threshold: int
    context_compaction_subagent_single_request_token_threshold: int
    context_compaction_subagent_keep_recent_iterations: int
    context_compaction_subagent_min_messages_to_compact: int
    context_compaction_subagent_prompt_path: Path
    context_compaction_subagent_summary_source_max_chars: int
    context_compaction_subagent_pre_note_threshold: int
    context_compaction_subagent_pre_note_warning_text: str
    context_compaction_subagent_require_note_max_skips: int

    # --- ログイン認証（[auth]、機密情報は .env 側） ---
    auth_enabled: bool
    auth_require_password: bool
    auth_users: dict[str, str]

    # --- MCP（Model Context Protocol）サーバー接続（[mcp]、src/mcp_client.py） ---
    # .locohane/settings.json（Claude Code/Qwen Code の mcpServers 形式を参考にした
    # 設定ファイル、git管理対象）の "mcp" ブロックがあれば、ここに列挙する4値は
    # load_config() の末尾でその内容により上書きされる（config.ini/環境変数はデフォルト値）。
    mcp_enabled: bool
    mcp_settings_path: Path
    mcp_connect_timeout_seconds: float
    mcp_call_timeout_seconds: float

    # --- checkpointer（LangGraphの会話状態を永続化するSQLite接続）タイムアウト ---
    checkpointer_op_timeout_seconds: float
    checkpointer_close_timeout_seconds: float
    checkpointer_shutdown_drain_timeout_seconds: float

    # --- UI（フロントエンド表示、[ui]） ---
    # チャット画面メインスレッドの描画件数上限（0 = 無制限）。あくまで表示専用の
    # 間引きであり、LLMへ渡す会話コンテキストや会話ログには影響しない
    # （frontend/src/App.tsx / messageTree.ts 参照）。
    ui_max_display_messages: int
    # サイドパネル（右側、ツール呼び出し等のStep一覧）の描画件数上限（0 = 無制限）。
    # 上記と同様、表示専用の間引きでありLLMへ渡す会話コンテキストや会話ログには
    # 影響しない（frontend/src/App.tsx / messageTree.ts 参照）。
    ui_max_display_side_steps: int
    # 入力欄へこの文字数以上のテキストを貼り付けたら、入力欄へ展開せず
    # 「貼り付けテキスト」添付（pasted-text-*.txt）として扱う（0以下で無効。
    # frontend/src/components/Composer.tsx、app.py の _build_human_message 参照）。
    ui_paste_as_attachment_threshold_chars: int
    # 1回の送信で受け付ける最大文字数（本文＋貼り付けテキストの合計。0以下で
    # 無制限）。フロントエンドで送信を止め、app.py の on_message でも拒否する。
    ui_max_input_chars: int
    # トークン使用量カード（TokenUsageCard）の「リクエスト1回あたり」行で、
    # 直近1回のLLM呼び出しの合計トークン数（total）がこの値以上になったら、
    # 該当行をオレンジ太字で強調表示する（0以下で無効。frontend/src/components/
    # TokenUsageCard.tsx 参照）。
    ui_token_usage_warn_threshold: int
    # 上記と同じ判定対象で、この値以上になったら赤太字で強調表示する
    # （warn_threshold より優先。0以下で無効）。
    ui_token_usage_alert_threshold: int
    # 作業フォルダの入力欄（WorkDirButton.tsx）に出すパス候補の最大件数
    # （0以下で候補を出さない。src/workdir_suggest.py 参照）。
    ui_work_dir_max_suggestions: int
    # 表示言語の既定値（UI_DEFAULT_LANGUAGES のいずれか）。利用者が画面で選んだ
    # 言語（ブラウザの localStorage）が無いときの初期値で、"auto" はブラウザの
    # 言語に従う（src/i18n.py・frontend/src/i18n.tsx 参照）。
    ui_default_language: str
    # WebSocket（Socket.IO）のping間隔・タイムアウト秒数。chainlit本体は
    # これらを指定せずAsyncServerを生成しているため、未設定時は
    # python-socketio/engineioのデフォルト（25秒/20秒）が使われる
    # （app.py の _patch_chainlit_websocket_ping_timeout 参照）。
    websocket_ping_interval_seconds: int
    websocket_ping_timeout_seconds: int

    # --- 設定ダッシュボード（管理ツール、[admin]、admin/ パッケージ） ---
    # 管理ツール自身の設定。インスタンス別の config_overrides.json による上書きの
    # 対象外（admin/overrides.py が [admin] セクションへの上書きを拒否する）。
    admin_host: str
    admin_port: int
    admin_session_timeout_minutes: int
    admin_backup_keep: int
    admin_stop_apps_on_exit: bool
    # インスタンス別ディレクトリのルート（resolve_instances_root() 参照）。
    admin_instances_dir: Path
    # 管理アシスタント（ダッシュボードの「AIに相談」。admin/assistant.py）。
    admin_assistant_enabled: bool
    # 回答に使う [llm] 設定の取得元インスタンス名。空文字列なら相談対象のインスタンス自身。
    admin_assistant_llm_instance: str
    admin_assistant_max_keys: int
    admin_assistant_max_docs: int
    admin_assistant_use_embedding: bool
    # 埋め込みの接続先の取得元インスタンス名。空文字列なら自動で探す（admin/server.py）。
    admin_assistant_embedding_instance: str
    admin_assistant_max_history_messages: int


def _resolve(base: Path, value: str) -> Path:
    """config.ini 内の相対パスをプロジェクトルート基準の絶対パスへ解決する。

    Args:
        base: 相対パスの基準ディレクトリ（通常は PROJECT_ROOT）。
        value: config.ini または環境変数から得た生のパス文字列。
            絶対パスであればそのまま使う。

    Returns:
        value が絶対パスならそれをそのまま Path 化したもの、相対パスなら
        base 基準で resolve() した絶対パス。
    """
    p = Path(value)
    return p if p.is_absolute() else (base / p).resolve()


def _as_optional_dir(value: str) -> str:
    """空でもよいフォルダの設定値を、空ならそのまま、そうでなければ PROJECT_ROOT 基準の絶対パスの文字列にする。"""
    value = value.strip()
    return str(_resolve(PROJECT_ROOT, value)) if value else ""


def _as_ingest_temp_dir(value: str) -> str:
    """[vector_db].ingest_temp_dir（全インスタンス共通）を _as_optional_dir で絶対パスにする。

    ${common_data_dir}・${instance} 等の ${…} はインスタンスごとに変わるため置換しない。書かれていたら、その文字列
    どおりの名前のフォルダをプロジェクトルートの下に作ってしまうため、警告してシステムの一時フォルダを使う（空にする）。
    既存のファイルを指す値も同じく使わない（設定ダッシュボードの「管理者設定」はどちらも保存させない）。
    """
    value = value.strip()
    if "${" in value:
        logger.warning(
            "[vector_db].ingest_temp_dir = %r: ${...} cannot be used in this setting (it is shared by all instances), "
            "so the system temporary folder is used instead.", value,
        )
        return ""
    resolved = _as_optional_dir(value)
    if resolved and Path(resolved).is_file():
        logger.warning("[vector_db].ingest_temp_dir = %r is an existing file, so the system temporary folder is used instead.", value)
        return ""
    return resolved


# GetDriveTypeW の DRIVE_REMOTE（ネットワークドライブ）。
_DRIVE_REMOTE = 4


def is_network_path(path: Path | str) -> bool:
    """path がネットワーク上の場所（UNC パス、または Windows のネットワークドライブ）か。

    UNC は \\\\server\\share・//server/share・\\\\?\\UNC\\... の形。ドライブ文字（Z: 等）は Windows の
    GetDriveTypeW で割り当て先がネットワークかを調べる（Windows 以外・調べられないときは False）。
    """
    text = str(path)
    if text.startswith(("\\\\", "//")) and not text.startswith(("\\\\?\\", "\\\\.\\")):
        return True
    if text.upper().startswith("\\\\?\\UNC\\"):
        return True
    drive = text.removeprefix("\\\\?\\")[:2]
    if sys.platform != "win32" or len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") == _DRIVE_REMOTE
    except (AttributeError, OSError):
        return False


def _reject_network_path(path: Path, label: str) -> None:
    """SQLite を置くパスがネットワーク上なら ValueError（_validate_local_data_paths 参照）。"""
    if is_network_path(path):
        raise ValueError(
            f"{label} must be on a local disk, not a network share or network drive: {path}. "
            "SQLite databases on a network share lock slowly and unreliably (e.g. 'database is locked'), so keep the data on a "
            "local disk and back it up to the share with the settings dashboard's data backup ([data_backup])."
        )


def _validate_local_data_paths(cfg: "Config") -> None:
    """SQLite を置くパス（common_data_dir・checkpoint_db・[thread_store] db・[vector_db] dir）をローカルディスクに限る。

    ネットワーク上の SQLite はロックの取り合いで「database is locked」になりやすく、WAL も使えない（SQLite の制約）。
    環境変数で指定した値も含め、解決後のパスで判定する。
    """
    for path, label in (
        (cfg.common_data_dir, "[paths].common_data_dir"),
        (cfg.checkpoint_db, "[paths].checkpoint_db"),
        (cfg.thread_store_db, "[thread_store].db"),
        (cfg.vector_db_dir, "[vector_db].dir"),
    ):
        _reject_network_path(path, label)


def resolve_instances_root(config_path: Path | None = None) -> Path:
    """インスタンス別ディレクトリのルート（[admin].instances_dir）を絶対パスで返す。

    環境変数 INSTANCES_DIR > config.ini の [admin].instances_dir >
    DEFAULT_INSTANCES_DIR の順で決める。config_overrides.json 自体がこの
    ディレクトリ配下にあるため、load_config() を経由せず（上書き差分を適用せず）
    config.ini だけを読む。app.py が chainlit の import 前にインスタンス別 .env を
    探すのにも使うため、軽量に呼べるようにしてある。

    Args:
        config_path: 読み込む config.ini のパス。省略時は DEFAULT_CONFIG_PATH。
            ファイルが無ければ既定値を使う。
    """
    raw = os.getenv("INSTANCES_DIR")
    if not raw:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(config_path or DEFAULT_CONFIG_PATH, encoding="utf-8")
        raw = parser.get("admin", "instances_dir", fallback=DEFAULT_INSTANCES_DIR)
    root = _resolve(PROJECT_ROOT, raw)
    # インスタンスの設定・記録（config_overrides.json・SQLite を持つ data の既定の置き場の隣）もローカルディスクに限る。
    _reject_network_path(root, "[admin].instances_dir")
    return root


def resolve_ssl_files(config_path: Path | None = None) -> tuple[Path, Path] | None:
    """HTTPS 用の証明書・秘密鍵（[admin].ssl_certfile / ssl_keyfile）を絶対パスで返す。

    環境変数 SSL_CERTFILE / SSL_KEYFILE > config.ini の [admin] の順で決める。
    両方空なら None（HTTP で待ち受ける）。管理ツール（admin/server.py）と、
    そこから起動する本体インスタンス（admin/supervisor.py）、app.bat が使う。
    設定の誤りで本体以外（evals・MCPサーバー等）まで起動できなくなるのを避けるため、
    load_config() には含めず、待ち受ける側だけがこれを呼ぶ。

    Args:
        config_path: 読み込む config.ini のパス。省略時は DEFAULT_CONFIG_PATH。

    Raises:
        ValueError: 片方だけ指定されている、または指定されたファイルが無い場合。
    """
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path or DEFAULT_CONFIG_PATH, encoding="utf-8")
    cert = os.getenv("SSL_CERTFILE", parser.get("admin", "ssl_certfile", fallback="")).strip()
    key = os.getenv("SSL_KEYFILE", parser.get("admin", "ssl_keyfile", fallback="")).strip()
    if not cert and not key:
        return None
    if not cert or not key:
        raise ValueError("Specify both [admin].ssl_certfile and ssl_keyfile (leave both empty to listen on HTTP).")
    cert_path, key_path = _resolve(PROJECT_ROOT, cert), _resolve(PROJECT_ROOT, key)
    for label, p in (("ssl_certfile", cert_path), ("ssl_keyfile", key_path)):
        if not p.is_file():
            raise ValueError(f"[admin].{label} file not found: {p}")
    return cert_path, key_path


def admin_settings_path(config_path: Path | None = None) -> Path:
    """全インスタンス共通の設定（設定ダッシュボードの「管理者設定」）の JSON のパス。

    環境変数 ADMIN_SETTINGS_PATH > <[admin].instances_dir>/admin_settings.json。
    """
    raw = os.getenv("ADMIN_SETTINGS_PATH")
    if raw:
        return _resolve(PROJECT_ROOT, raw)
    return resolve_instances_root(config_path) / ADMIN_SETTINGS_FILE


def _apply_admin_settings(parser: configparser.ConfigParser, path: Path) -> None:
    """管理者設定（{"<section>": {"<key>": "<値>"}}）の ADMIN_SETTING_KEYS だけを parser へ重ねる。

    config.ini より優先し、インスタンス別の config_overrides.json・環境変数より弱い
    （ADMIN_SETTING_KEYS はどれもインスタンス別には上書きできないキー）。
    """
    if not path.is_file():
        return
    # 読めない・形の違うファイルは警告して無視する（config.ini の値で動かす）。全インスタンスと管理ツールが読むため、
    # 例外にすると手で編集して壊しただけで全部が起動できなくなる。画面で保存し直せば正しい内容で上書きされる。
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("%s: ignoring the admin settings because the file cannot be read: %s", path, exc)
        return
    if not isinstance(data, dict):
        logger.warning("%s: ignoring the admin settings because the top level is not an object.", path)
        return
    for section, values in data.items():
        if not isinstance(values, dict):
            logger.warning("%s: ignoring section %r of the admin settings because its value is not an object.", path, section)
            continue
        for key, value in values.items():
            if (section, key) not in ADMIN_SETTING_KEYS or not parser.has_section(section):
                logger.warning("%s: ignoring [%s].%s because it is not a setting of the admin settings screen.", path, section, key)
                continue
            parser.set(section, key, str(value))


def default_overrides_path(config_path: Path | None = None) -> Path:
    """default インスタンスの config_overrides.json のパスを返す。"""
    return resolve_instances_root(config_path) / DEFAULT_INSTANCE_NAME / "config_overrides.json"


def _sub_instance(value: str, instance_name: str) -> str:
    """値中の ${instance} プレースホルダーをインスタンス名へ置換する。"""
    return value.replace("${instance}", instance_name)


def _sub_instance_dir(value: str, instance_dir: Path) -> str:
    """値中の ${instance_dir} プレースホルダーをインスタンスの設定フォルダへ置換する。"""
    return value.replace("${instance_dir}", str(instance_dir))


def _sub_common_data_dir(value: str, common_data_dir: Path, instance_name: str) -> str:
    """値中の ${common_data_dir} / ${instance} プレースホルダーを置換する。

    [paths].common_data_dir を使った、configparser標準の補間機能に頼らない
    独自のセクション横断プレースホルダー置換（BasicInterpolationは同一
    セクション内でしか使えないため）。置換後は絶対パス文字列になるため、
    後段の _resolve() はそのまま素通りする。

    Args:
        value: config.ini または環境変数から得た生の文字列。
        common_data_dir: 解決済み（絶対パス化済み）の共通データディレクトリ。
        instance_name: ${instance} に入るインスタンス名（load_config() 参照）。

    Returns:
        プレースホルダーを置換した文字列（含んでいなければ value のまま）。
    """
    value = _sub_instance(value, instance_name)
    if "${common_data_dir}" not in value:
        return value
    return value.replace("${common_data_dir}", str(common_data_dir))


def _as_bool(value: bool | str) -> bool:
    """config.ini のbool値、または環境変数由来の文字列をboolへ変換する。

    Args:
        value: config.ini から得た bool、または環境変数から得た文字列。

    Returns:
        value が bool ならそのまま。文字列なら "0"/"false"/"no"（大文字小文字
        を問わない）を False、それ以外を True として扱う。
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no")


def _subagent_override(section, key: str, main_value, converter=lambda v: v):
    """[xxx.subagent] セクションの継承フォールバックを1箇所に共通化する。

    section にキーの記載があれば converter を通した値を、無ければ
    main_value（環境変数解決済みの [xxx] 側の実効値）をそのまま返す。
    config.ini の生値へ直接フォールバックすると環境変数オーバーライドが
    サブエージェント側にだけ反映されなくなるため、必ず呼び出し元で
    環境変数解決済みの値を main_value として渡すこと
    （context_trim_subagent_*/context_compaction_subagent_* 参照）。

    Args:
        section: config.ini の [xxx.subagent] セクション（SectionProxy/dict）。
        key: セクション内のキー名。
        main_value: 対応する [xxx] 側の実効値（環境変数解決済み）。
        converter: section から得た生の文字列値を変換する関数（既定は恒等）。

    Returns:
        section にキーがあれば converter(生値)、無ければ main_value。
    """
    raw = section.get(key)
    return converter(raw) if raw is not None else main_value


def _as_optional_float(value: float | str | None) -> float | None:
    """config.ini の空欄、または環境変数の空文字列を None（未指定）として扱う。

    Args:
        value: config.ini から得た値、または環境変数から得た文字列。

    Returns:
        空欄・None なら None、それ以外は float に変換した値。
    """
    if value is None:
        return None
    if isinstance(value, float):
        return value
    text = str(value).strip()
    return float(text) if text else None


def _as_optional_int(value: int | str | None) -> int | None:
    """config.ini の空欄、または環境変数の空文字列を None（未指定）として扱う。

    Args:
        value: config.ini から得た値、または環境変数から得た文字列。

    Returns:
        空欄・None なら None、それ以外は int に変換した値。
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text else None


def _as_optional_bool(value: bool | str | None) -> bool | None:
    """config.ini の空欄、または環境変数の空文字列を None（未指定）として扱う。

    Args:
        value: config.ini から得た値、または環境変数から得た文字列。

    Returns:
        空欄・None なら None、bool ならそのまま、それ以外は _as_bool と同じ
        規則（"0"/"false"/"no" を False、それ以外を True）で変換した値。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    return _as_bool(text) if text else None


def _as_optional_str_list(value: str | None) -> list[str] | None:
    """config.ini のリスト形式（["a", "b"]）の値を list[str] に変換する。空は None。

    管理ツールのリストエディタで編集できるよう、カンマ区切りではなく
    _as_message_list と同じ JSON/Python風のリスト形式で書く。

    Args:
        value: config.ini から得たリスト形式の文字列、または環境変数由来の文字列。

    Returns:
        空欄・空リスト・None なら None、それ以外は文字列のリスト。

    Raises:
        ValueError: 値がリスト（配列）として解釈できない場合。
    """
    items = _as_message_list(value)
    return items or None


def _as_optional_str(value: str | None) -> str | None:
    """config.ini の空欄、または環境変数の空文字列を None（未指定）として扱う。

    Args:
        value: config.ini から得た値、または環境変数から得た文字列。

    Returns:
        前後の空白を除いた文字列。空欄・None なら None。
    """
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _as_optional_reasoning_format(value: str | None) -> str | None:
    """[llm].reasoning_format の値を検証する。

    Args:
        value: config.ini から得た値、または環境変数から得た文字列。

    Returns:
        前後の空白を除いた文字列（LLM_REASONING_FORMATS のいずれか）。
        空欄・None なら None（未指定、llama-server既定の auto に委ねる）。

    Raises:
        ValueError: LLM_REASONING_FORMATS に無い値が指定された場合。
    """
    text = _as_optional_str(value)
    if text is None:
        return None
    if text not in LLM_REASONING_FORMATS:
        choices = "/".join(sorted(LLM_REASONING_FORMATS))
        raise ValueError(f"[llm].reasoning_format must be one of {choices} (current value: {text!r})")
    return text


UI_DEFAULT_LANGUAGES = frozenset({"auto", "ja", "en"})


def _as_ui_language(value: str | None) -> str:
    """[ui].default_language の値を検証して小文字で返す。

    Args:
        value: config.ini / 環境変数の生の値。空欄・None は "auto" とみなす。

    Returns:
        UI_DEFAULT_LANGUAGES のいずれか。

    Raises:
        ValueError: UI_DEFAULT_LANGUAGES に無い値が指定された場合。
    """
    text = (_as_optional_str(value) or "auto").lower()
    if text not in UI_DEFAULT_LANGUAGES:
        choices = "/".join(sorted(UI_DEFAULT_LANGUAGES))
        raise ValueError(f"[ui].default_language must be one of {choices} (current value: {text!r})")
    return text


def _as_optional_reasoning_effort(value: str | None) -> str | None:
    """[llm].reasoning_effort の値を検証する。

    Args:
        value: config.ini から得た値、または環境変数から得た文字列。

    Returns:
        前後の空白を除いた文字列（LLM_REASONING_EFFORTS のいずれか）。
        空欄・None なら None（未指定、llama-server既定に委ねる）。

    Raises:
        ValueError: LLM_REASONING_EFFORTS に無い値が指定された場合。
    """
    text = _as_optional_str(value)
    if text is None:
        return None
    if text not in LLM_REASONING_EFFORTS:
        choices = "/".join(sorted(LLM_REASONING_EFFORTS))
        raise ValueError(f"[llm].reasoning_effort must be one of {choices} (current value: {text!r})")
    return text


def with_reasoning_effort(config: Config, value: object) -> Config:
    """チャット画面で選んだ reasoning_effort を [llm].reasoning_effort へ上書きした Config を返す。

    Args:
        config: アプリ設定（config.ini の値）。
        value: 画面で選んだ値。LLM_REASONING_EFFORTS に無い値（空文字・None を含む）は
            「config.ini の値を使う」とみなす。

    Returns:
        上書きした Config。上書き不要なら config をそのまま返す。
    """
    if not isinstance(value, str) or value not in LLM_REASONING_EFFORTS or value == config.reasoning_effort:
        return config
    return replace(config, reasoning_effort=value)


def _as_thinking_loop_guard_target(value: str | None) -> str:
    """[thinking_loop_guard].target（THINKING_LOOP_GUARD_TARGETS のいずれか）を検証する。

    Args:
        value: config.ini から得た値、または環境変数から得た文字列。

    Returns:
        前後の空白を除いた文字列。

    Raises:
        ValueError: THINKING_LOOP_GUARD_TARGETS に無い値が指定された場合。
    """
    text = str(value or "").strip()
    if text not in THINKING_LOOP_GUARD_TARGETS:
        choices = "/".join(THINKING_LOOP_GUARD_TARGETS)
        raise ValueError(f"[thinking_loop_guard].target must be one of {choices}: {value!r}")
    return text


def _as_thinking_level(value: str | None, key_name: str) -> str:
    """[thinking_control] のレベル指定（THINKING_LEVELS のいずれか）を検証する。

    Args:
        value: config.ini から得た値、または環境変数から得た文字列。
        key_name: エラーメッセージに使う設定キー名。

    Returns:
        前後の空白を除いた文字列。

    Raises:
        ValueError: THINKING_LEVELS に無い値が指定された場合。
    """
    text = str(value or "").strip()
    if text not in THINKING_LEVELS:
        choices = "/".join(THINKING_LEVELS)
        raise ValueError(f"[thinking_control].{key_name} must be one of {choices}: {value!r}")
    return text


def _as_optional_thinking_level(value: str | None, key_name: str) -> str | None:
    """[thinking_control].rule_* のレベル指定を検証する。false はルール無効（None）。

    Args:
        value: config.ini から得た値、または環境変数から得た文字列。
        key_name: エラーメッセージに使う設定キー名。

    Returns:
        THINKING_LEVELS のいずれか。false なら None。

    Raises:
        ValueError: false でも THINKING_LEVELS でもない値（空欄を含む）が
            指定された場合。
    """
    if str(value or "").strip().lower() == "false":
        return None
    text = str(value or "").strip()
    if text not in THINKING_LEVELS:
        choices = "/".join(("false", *THINKING_LEVELS))
        raise ValueError(f"[thinking_control].{key_name} must be one of {choices}: {value!r}")
    return text


# [thinking_control].after_tools が config.ini に無い場合の既定値。
_DEFAULT_THINKING_CONTROL_AFTER_TOOLS = (
    '["Read", "Glob", "Grep", "json_query", "search_path_memory", "check_work_dir_status",'
    ' "update_task_progress", "get_plan_status"]'
)


def _as_message_list(value: str | None) -> list[str]:
    """config.ini のJSON/Python風リスト値を list[str] に変換する。

    例: '["a", "b"]' や、末尾カンマを含む複数行の配列リテラル。
    json.loads ではなく ast.literal_eval を使うのは、末尾カンマ等の
    Python的な緩い記法（コピペしやすい）も許容するため。

    Args:
        value: config.ini から得たリスト形式の文字列、または環境変数由来の文字列。
            空欄・None なら空リストとして扱う。

    Returns:
        パースした文字列のリスト（空要素は除外）。

    Raises:
        ValueError: 値がリスト（配列）として解釈できない場合
            （構文エラー、またはリスト以外の型だった場合）。
    """
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"The config.ini value must be a JSON/Python list (array): {text!r}") from e
    if not isinstance(parsed, list):
        raise ValueError(f"The config.ini value must be a list (array): {text!r}")
    return [str(item) for item in parsed if str(item).strip()]


# [llm].main_url / sub_url が config.ini に無い場合の既定値（1件のみ）。
_DEFAULT_LLM_URL = '[{"base_url": "http://localhost:8080/v1", "api_key": "dummy-not-used", "model": "local-model"}]'

# [scripts].background_min_poll_message が空の場合に使う既定メッセージ。
# check_script_job() が最短確認間隔未満での再呼び出しを検知した際にLLMへ
# 返す文字列のテンプレート。.format() で {wait_remaining}/{job_id}/
# {min_interval} を埋め込む（src/tools.py 参照）。
DEFAULT_SCRIPT_BACKGROUND_MIN_POLL_MESSAGE = (
    "Checked too soon. Wait about {wait_remaining} more seconds, "
    "then call check_script_job(job_id={job_id!r}) again "
    "(minimum check interval: {min_interval} seconds)."
)

# [subagent].background_min_poll_message が空の場合に使う既定メッセージ。
# check_dispatch_agent_job() が最短確認間隔未満での再呼び出しを検知した際に
# LLMへ返す文字列のテンプレート。プレースホルダーは
# DEFAULT_SCRIPT_BACKGROUND_MIN_POLL_MESSAGE と同じ
# （.format() で {wait_remaining}/{job_id}/{min_interval} を埋め込む）。
DEFAULT_DISPATCH_AGENT_BACKGROUND_MIN_POLL_MESSAGE = (
    "Checked too soon. Wait about {wait_remaining} more seconds, "
    "then call check_dispatch_agent_job(job_id={job_id!r}) again "
    "(minimum check interval: {min_interval} seconds)."
)

# [context_compaction].pre_note_warning_text / [context_compaction.subagent]の
# 同項目が空の場合に使う既定メッセージ。
_DEFAULT_CONTEXT_COMPACTION_PRE_NOTE_WARNING_TEXT = (
    "[System notice: context compaction (summarizing the old conversation history) "
    "will happen soon. Facts not written down may be lost after the summary, so "
    "if there are important facts (values, counts, locations, etc.) not yet recorded in write_thread_note, "
    "write them down before calling any tool in your next response]"
)


def _validate_poll_message_template(text: str) -> str:
    """[scripts].background_min_poll_message のテンプレート文字列を検証する。

    {wait_remaining}/{job_id}/{min_interval} 以外のプレースホルダーや
    書式指定の誤りを起動時に検出するため、ダミー値で実際に .format() を
    試してみる。

    Args:
        text: config.ini または環境変数から得たテンプレート文字列。

    Returns:
        検証済みの文字列（そのまま）。

    Raises:
        ValueError: .format(wait_remaining=.., job_id=.., min_interval=..)
            が失敗する場合（未知のプレースホルダー等）。
    """
    try:
        text.format(wait_remaining=1, job_id="dummy", min_interval=1)
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(
            f"[scripts].background_min_poll_message has an invalid placeholder: {text!r} ({e})"
        ) from e
    return text


def _as_llm_endpoint_time(value: Any, key_name: str, field_name: str, section: str = "llm") -> float | None:
    """[llm].main_url / sub_url の各要素の start/end キーを検証してfloatへ変換する。

    Args:
        value: item.get("start") / item.get("end") の生値。未指定または空文字は
            None（常時使用可能）として扱う。
        key_name: エラーメッセージに使う設定キー名（例: "main_url"）。
        section: エラーメッセージに使うセクション名（既定 "llm"。[vector_db].embedding_url では "vector_db"）。
        field_name: エラーメッセージに使うフィールド名（"start" または "end"）。

    Returns:
        0.0〜24.0 の範囲に収まる時刻（時間単位）、または未指定時は None。

    Raises:
        ValueError: 数値に変換できない、または0〜24の範囲外の場合。
    """
    if value is None or value == "":
        return None
    try:
        hour = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field_name} of [{section}].{key_name} must be a number (hours; use decimals for minutes): {value!r}") from e
    if not (0.0 <= hour <= 24.0):
        raise ValueError(f"{field_name} of [{section}].{key_name} must be between 0 and 24: {value!r}")
    return hour


def _as_llm_endpoints(value: str | None, key_name: str, section: str = "llm") -> tuple[LLMEndpoint, ...]:
    """[llm].main_url / sub_url のJSON/Python風リスト値を LLMEndpoint のタプルに変換する。

    例: '[{"base_url": "http://localhost:8080/v1", "api_key": "dummy-not-used",
    "model": "local-model"}]'。_as_message_list() と同じく ast.literal_eval を
    使い、末尾カンマ等の緩い記法を許容する。

    各要素には任意で start/end（使用可能時間帯、時間単位・0〜24・分は小数）を
    指定できる。両方セットで指定するか、両方省略（常時使用可能）のいずれか。
    リスト全体で最低1件は start/end を両方省略した「常時使用可能」な接続先が
    必要（すべて時間帯限定だと、どの時間帯にも当てはまらない瞬間に接続先が
    無くなってしまうため）。

    各要素には任意で provider（"openai_compatible" 既定 / "llama_cpp"）も
    指定できる（LLMEndpoint.provider docstring参照）。未指定時は
    "openai_compatible"（既存config.iniとの後方互換のため）。

    Args:
        value: config.ini から得たリスト形式の文字列、または環境変数由来の文字列。
        key_name: エラーメッセージに使う設定キー名（例: "main_url"）。
        section: エラーメッセージに使うセクション名（既定 "llm"。[vector_db].embedding_url では "vector_db"）。

    Returns:
        パースした LLMEndpoint のタプル（1件以上、うち最低1件は start/end 未指定）。

    Raises:
        ValueError: リスト（配列）として解釈できない、空リスト、要素が
            dict でない、必須キー（base_url/model）が欠けている、start/end が
            片方のみ指定されている、start/end が範囲外、provider が
            LLM_PROVIDERS に無い値、または start/end を両方省略した接続先が
            1件も無い場合。
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"[{section}].{key_name} must specify at least one endpoint: {value!r}")
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"[{section}].{key_name} must be a JSON/Python list (array): {text!r}") from e
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"[{section}].{key_name} must be a list (array) with at least one element: {text!r}")
    endpoints: list[LLMEndpoint] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError(f"Each element of [{section}].{key_name} must be a " '{"base_url":..., "api_key":..., "model":...} ' f"dict: {item!r}")
        if not item.get("base_url") or not item.get("model"):
            raise ValueError(f"Each element of [{section}].{key_name} requires base_url and model: {item!r}")
        start = _as_llm_endpoint_time(item.get("start"), key_name, "start", section)
        end = _as_llm_endpoint_time(item.get("end"), key_name, "end", section)
        if (start is None) != (end is None):
            raise ValueError(f"Each element of [{section}].{key_name} must specify both start and end, or neither: {item!r}")
        if start is not None and end is not None and start == end:
            raise ValueError(f"start and end of [{section}].{key_name} are the same (there would be no available time window): {item!r}")
        provider = str(item.get("provider") or "openai_compatible").strip()
        if provider not in LLM_PROVIDERS:
            choices = "/".join(sorted(LLM_PROVIDERS))
            raise ValueError(f"provider of [{section}].{key_name} must be one of {choices}: {item!r}")
        endpoints.append(
            LLMEndpoint(
                base_url=str(item["base_url"]),
                api_key=str(item.get("api_key") or "dummy-not-used"),
                model=str(item["model"]),
                start=start,
                end=end,
                provider=provider,
            )
        )
    if not any(e.start is None and e.end is None for e in endpoints):
        raise ValueError(f"[{section}].{key_name} needs at least one endpoint with neither start nor end (always available): {text!r}")
    return tuple(endpoints)


def _as_embedding_endpoints(value: str | None) -> tuple[LLMEndpoint, ...]:
    """[vector_db].embedding_url を LLMEndpoint のタプルに変換する（空欄・[] なら空タプル）。

    形式は [llm].main_url と同じ（_as_llm_endpoints() 参照）。埋め込みは接続先ごとに
    同じベクターを返す必要があるため、全要素の model が同じであることと、
    時間帯で切り替える start/end を使わないことを追加で要求する。

    Raises:
        ValueError: _as_llm_endpoints() の検証エラー、model が混在している、
            または start/end が指定されている場合。
    """
    if str(value or "").strip() in ("", "[]"):
        return ()
    endpoints = _as_llm_endpoints(value, "embedding_url", section="vector_db")
    if any(e.start is not None for e in endpoints):
        raise ValueError(f"[vector_db].embedding_url cannot have start/end (search breaks if the embedding model changes by time of day): {value!r}")
    if len({e.model.lower() for e in endpoints}) > 1:
        raise ValueError(f"All models in [vector_db].embedding_url must be the same (search breaks if vectors differ per endpoint): {value!r}")
    return endpoints


def _as_vector_db_backend(value: str | None) -> str:
    """[vector_db].backend の値を検証する（VECTOR_DB_BACKENDS のいずれか）。"""
    text = str(value or "").strip().lower()
    if text not in VECTOR_DB_BACKENDS:
        raise ValueError(f"[vector_db].backend must be one of {'/'.join(VECTOR_DB_BACKENDS)}: {value!r}")
    return text


def _as_vector_db_qdrant_preset(value: str | None) -> str:
    """[vector_db].qdrant_preset の値を検証する（VECTOR_DB_QDRANT_PRESETS のいずれか、大文字小文字は問わない）。"""
    text = str(value or "").strip().lower()
    if text not in VECTOR_DB_QDRANT_PRESETS:
        raise ValueError(f"[vector_db].qdrant_preset must be one of {'/'.join(VECTOR_DB_QDRANT_PRESETS)}: {value!r}")
    return text


def _as_vector_db_encoding_format(value: str | None) -> str:
    """[vector_db].embedding_encoding_format の値を検証する（VECTOR_DB_EMBEDDING_ENCODING_FORMATS のいずれか）。"""
    text = str(value or "").strip().lower()
    if text not in VECTOR_DB_EMBEDDING_ENCODING_FORMATS:
        raise ValueError(
            f"[vector_db].embedding_encoding_format must be one of {'/'.join(VECTOR_DB_EMBEDDING_ENCODING_FORMATS)}: {value!r}"
        )
    return text


def _as_vector_db_distance(value: str | None) -> str:
    """[vector_db].distance の値を検証し、VECTOR_DB_DISTANCES の綴りにする（大文字小文字は問わない）。"""
    text = str(value or "").strip().lower()
    for distance in VECTOR_DB_DISTANCES:
        if text == distance.lower():
            return distance
    raise ValueError(f"[vector_db].distance must be one of {'/'.join(VECTOR_DB_DISTANCES)}: {value!r}")


def _as_extension_list(value: str | None) -> tuple[str, ...]:
    """カンマ区切りの拡張子リストを、小文字・"." 付きに正規化したタプルにする。"""
    result: list[str] = []
    for item in str(value or "").split(","):
        ext = item.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        if ext not in result:
            result.append(ext)
    return tuple(result)


# ページごとに画像にできる文書の形式（[vector_db].image_input_extensions に書ける値。src/vector_db/render.py もこれを使う）。
PAGE_IMAGE_EXTENSIONS = (".pdf", ".docx", ".pptx", ".xlsx", ".xlsm")


def _as_page_image_extensions(value: str | None) -> tuple[str, ...]:
    """[vector_db].image_input_extensions を読む（空なら、どの文書もページ画像にしない）。"""
    extensions = _as_extension_list(value)
    unknown = [e for e in extensions if e not in PAGE_IMAGE_EXTENSIONS]
    if unknown:
        raise ValueError(
            f"[vector_db].image_input_extensions can only contain {', '.join(PAGE_IMAGE_EXTENSIONS)} (current value: {', '.join(unknown)})"
        )
    return extensions


def _unquote(value: str | None) -> str:
    """値全体がダブルクォートで囲まれていれば外す。

    configparser は値の末尾の空白を落とすため、"query: " のように空白を残したい値は
    クォートで囲んで書けるようにする。
    """
    stripped = str(value or "").strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        return stripped[1:-1]
    return stripped


# [vector_db] query_prefix / document_prefix = auto のときの、埋め込みモデル名から決める接頭辞。
# (モデル名の正規表現, 検索語の接頭辞, 文書の接頭辞) を上から順に試し、最初に当たったものを使う。
# どれにも当たらないモデル（bge-m3 等）は接頭辞なし。各モデルカードの推奨どおり。
_EMBEDDING_PREFIX_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # multilingual-e5-large-instruct 等: 検索語だけに指示文を付け、文書には付けない
    (
        re.compile(r"(^|[^a-z0-9])e5([^a-z0-9]|$).*instruct"),
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: ",
        "",
    ),
    (re.compile(r"(^|[^a-z0-9])e5([^a-z0-9]|$)"), "query: ", "passage: "),
    (re.compile(r"ruri-v3"), "検索クエリ: ", "検索文書: "),
    (re.compile(r"ruri"), "クエリ: ", "文章: "),
    (re.compile(r"nomic-embed"), "search_query: ", "search_document: "),
    # NVIDIA の Nemotron 系の埋め込みモデル（Llama-Nemotron-Embed・Nemotron-*-Embed 等）
    (re.compile(r"nemotron.*embed|embed.*nemotron"), "query: ", "passage: "),
)


def auto_embedding_prefixes(model: str) -> tuple[str, str]:
    """埋め込みモデル名から (検索語の接頭辞, 文書の接頭辞) を決める（_EMBEDDING_PREFIX_RULES 参照）。"""
    name = (model or "").lower()
    for pattern, query_prefix, document_prefix in _EMBEDDING_PREFIX_RULES:
        if pattern.search(name):
            return query_prefix, document_prefix
    return "", ""


def _as_embedding_prefix(value: str | None, endpoints: tuple[LLMEndpoint, ...], kind: str) -> str:
    """[vector_db].query_prefix / document_prefix の値を、実際に付ける文字列にする。

    auto（大文字小文字は問わない）なら embedding_url の model から決める
    （auto_embedding_prefixes()）。それ以外は _unquote() した値をそのまま使う（空なら付けない）。
    """
    text = _unquote(value)
    if text.lower() != "auto":
        return text
    if not endpoints:
        return ""
    query_prefix, document_prefix = auto_embedding_prefixes(endpoints[0].model)
    return query_prefix if kind == "query" else document_prefix


def _validate_vector_db(cfg: "Config") -> None:
    """[vector_db] のキー同士の組み合わせを検証する。

    Raises:
        ValueError: chunk_overlap が 0〜chunk_size-1 の範囲外、min_score が 0〜1 の範囲外、
            backend=sqlite_vec なのに distance が sqlite-vec の対応しない値（Dot）、
            または enabled=true なのに embedding_url が空・backend=qdrant なのに
            qdrant_url が空の場合。
    """
    if not (0 <= cfg.vector_db_chunk_overlap < cfg.vector_db_chunk_size):
        raise ValueError(
            f"[vector_db].chunk_overlap must be at least 0 and less than chunk_size ({cfg.vector_db_chunk_size}): {cfg.vector_db_chunk_overlap}"
        )
    if not (0.0 <= cfg.vector_db_min_score <= 1.0):
        raise ValueError(f"[vector_db].min_score must be between 0.0 and 1.0: {cfg.vector_db_min_score}")
    if not (0.0 <= cfg.vector_db_image_min_score <= 1.0):
        raise ValueError(f"[vector_db].image_min_score must be between 0.0 and 1.0: {cfg.vector_db_image_min_score}")
    if not (0.0 <= cfg.vector_db_path_min_score <= 1.0):
        raise ValueError(f"[vector_db].path_min_score must be between 0.0 and 1.0: {cfg.vector_db_path_min_score}")
    for key in ("text", "keyword", "image", "path_keyword"):
        if getattr(cfg, f"vector_db_rrf_weight_{key}") < 0:
            raise ValueError(f"[vector_db].rrf_weight_{key} must be 0 or more: {getattr(cfg, f'vector_db_rrf_weight_{key}')}")
    if cfg.vector_db_backend == "sqlite_vec" and cfg.vector_db_distance not in VECTOR_DB_SQLITE_VEC_DISTANCES:
        raise ValueError(
            f"With [vector_db].backend = sqlite_vec, distance must be one of {'/'.join(VECTOR_DB_SQLITE_VEC_DISTANCES)} "
            f"(sqlite-vec does not support {cfg.vector_db_distance})."
        )
    if not cfg.vector_db_enabled:
        return
    if not cfg.vector_db_embedding_endpoints:
        raise ValueError("Specify embedding_url when [vector_db].enabled = true.")
    if cfg.vector_db_backend == "qdrant" and not cfg.vector_db_qdrant_url:
        raise ValueError("Specify qdrant_url when [vector_db].backend = qdrant.")


def _as_routing_strategy(value: str | None, key_name: str, section: str = "llm") -> str:
    """[llm].main_routing_strategy / sub_routing_strategy（[vector_db].embedding_routing_strategy）の値を検証する。

    Args:
        value: config.ini から得た文字列、または環境変数由来の文字列。
        key_name: エラーメッセージに使う設定キー名。
        section: エラーメッセージに使うセクション名。

    Returns:
        前後の空白を除いた文字列（LLM_ROUTING_STRATEGIES のいずれか）。

    Raises:
        ValueError: LLM_ROUTING_STRATEGIES に無い値が指定された場合。
    """
    text = str(value or "").strip()
    if text not in LLM_ROUTING_STRATEGIES:
        choices = "/".join(sorted(LLM_ROUTING_STRATEGIES))
        raise ValueError(f"[{section}].{key_name} must be one of {choices}: {value!r}")
    return text


def _as_main_agent_tool_guard_visibility_mode(value: str | None) -> str:
    """[main_agent_tool_guard].visibility_mode の値を検証する。

    strict/hint/all の3値。main_agent_tool_guard_mode（呼び出し制限の
    ON/OFF/範囲）とは独立した設定で、modeの値に関わらずこの値だけで一覧の
    見せ方が決まる。

    Args:
        value: config.ini から得た文字列、または環境変数由来の文字列。
            空欄・None なら既定値 "strict" を返す。

    Returns:
        前後の空白を除いた文字列（MAIN_AGENT_TOOL_GUARD_VISIBILITY_MODES のいずれか）。

    Raises:
        ValueError: MAIN_AGENT_TOOL_GUARD_VISIBILITY_MODES に無い値が指定された場合。
    """
    text = str(value or "").strip() or "strict"
    if text not in MAIN_AGENT_TOOL_GUARD_VISIBILITY_MODES:
        choices = "/".join(sorted(MAIN_AGENT_TOOL_GUARD_VISIBILITY_MODES))
        raise ValueError(f"[main_agent_tool_guard].visibility_mode must be one of {choices}: {value!r}")
    return text


def _as_main_agent_tool_guard_mode(value: str | None) -> str:
    """[main_agent_tool_guard].mode の値を検証する。

    false/tools_skills_only/all の3値。大文字小文字は区別しない（evals
    ケースyamlの `env:` セクションで `MAIN_AGENT_TOOL_GUARD_MODE: false` の
    ように書くと、YAMLがPythonのbool Falseとしてパースし、
    evals/case_schema.py の str(v) で "False"（先頭大文字）になるため、
    _as_bool() 同様に大文字小文字を吸収する）。

    Args:
        value: config.ini から得た文字列、または環境変数由来の文字列。
            空欄・None なら既定値 "all" を返す。

    Returns:
        前後の空白を除き小文字化した文字列（MAIN_AGENT_TOOL_GUARD_MODES のいずれか）。

    Raises:
        ValueError: MAIN_AGENT_TOOL_GUARD_MODES に無い値が指定された場合。
    """
    text = str(value or "").strip().lower() or "all"
    if text not in MAIN_AGENT_TOOL_GUARD_MODES:
        choices = "/".join(sorted(MAIN_AGENT_TOOL_GUARD_MODES))
        raise ValueError(f"[main_agent_tool_guard].mode must be one of {choices}: {value!r}")
    return text


def _as_path_list(value: str | None, base: Path) -> list[Path]:
    """config.ini のパス指定を list[Path] に変換する。

    従来通りの単一パス（角カッコなしのプレーンな文字列）と、
    nudge_messages と同じJSON/Python風リスト形式（角カッコ＋改行複数行OK、
    例: '[\n    "./a.md",\n    "./b.md",\n    ]'）の両方を許容する
    （後方互換のため、既存の config.ini を書き換えなくても動く）。

    Args:
        value: config.ini または環境変数から得た生の文字列。
        base: 相対パスの解決基準ディレクトリ（通常は PROJECT_ROOT）。

    Returns:
        _resolve() で絶対パス化した Path のリスト（空欄なら空リスト）。
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return []
    items = _as_message_list(text) if text.startswith("[") else [text]
    return [_resolve(base, item) for item in items]


def _parse_project_locohane_dirs(value: str | None, base: Path) -> list[tuple[Path, str | None]]:
    """config.ini の [paths].project_locohane_dir をパースする。

    _as_path_list と同じ単一パス／リスト形式に加え、リストの各要素に
    {"dir": ディレクトリ, "python": Python実行ファイル} の辞書も書ける
    （allow_sandbox_dir と同じ ast.literal_eval による緩い記法）。
    "python" を指定したディレクトリ配下の skills/ にある .py スクリプトは、
    [scripts].python ではなくその Python で起動される
    （src/tools/_script_job.py の _script_python_for() 参照）。
    例: ["./.locohane", {"dir": "./.locohane_team", "python": "./.locohane_team/.venv/Scripts/python.exe"}]

    Args:
        value: config.ini または環境変数から得た生の文字列。
        base: 相対パスの解決基準ディレクトリ（通常は PROJECT_ROOT）。

    Returns:
        (ディレクトリの絶対パス, Python実行ファイル or None) のリスト。
        Python実行ファイルはパス区切りを含む場合のみ base 基準で絶対パス化し、
        含まない場合（"python" 等のコマンド名）はそのまま返す。

    Raises:
        ValueError: リストとして解釈できない場合、要素が文字列でも辞書でも
            ない場合、辞書の "dir" が空、または "python" が文字列でない場合。
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return []
    if not text.startswith("["):
        return [(_resolve(base, text), None)]
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"project_locohane_dir must be a Python list: {text!r}") from e
    if not isinstance(parsed, list):
        raise ValueError(f"project_locohane_dir must be a list (array): {text!r}")
    result: list[tuple[Path, str | None]] = []
    for item in parsed:
        if isinstance(item, str):
            if item.strip():
                result.append((_resolve(base, item.strip()), None))
            continue
        if not isinstance(item, dict):
            raise ValueError(f'Each element of project_locohane_dir must be a string or a dict {{"dir": ..., "python": ...}}: {item!r}')
        dir_value = item.get("dir")
        if not isinstance(dir_value, str) or not dir_value.strip():
            raise ValueError(f"dir of project_locohane_dir must be a non-empty string: {item!r}")
        python_value = item.get("python")
        if python_value is not None and not isinstance(python_value, str):
            raise ValueError(f"python of project_locohane_dir must be a string: {item!r}")
        python = python_value.strip() if python_value else ""
        if python and ("/" in python or "\\" in python):
            python = str(_resolve(base, python))
        result.append((_resolve(base, dir_value.strip()), python or None))
    return result


# [skill_creator].other_users_drafts の取りうる値（他ユーザーのドラフトの扱い）。
#   hidden  : 一覧に出さない・読めない・実行できない・書き込み/評価できない
#   listed  : 一覧には出す（使用不可の注記付き）が、読み取り以降はできない
#   readable: 一覧に出し、読み取りもできるが、実行・書き込み・評価はできない
#   full    : 一覧・読み取り・実行・書き込み・評価のすべてができる
SKILL_DRAFT_VISIBILITY_MODES = ("hidden", "listed", "readable", "full")


def _parse_skill_draft_visibility(value: object) -> str:
    """[skill_creator].other_users_drafts を検証して返す。

    Raises:
        ValueError: SKILL_DRAFT_VISIBILITY_MODES 以外の値の場合。
    """
    mode = str(value).strip().lower()
    if mode not in SKILL_DRAFT_VISIBILITY_MODES:
        raise ValueError(
            f"[skill_creator].other_users_drafts must be one of {'/'.join(SKILL_DRAFT_VISIBILITY_MODES)}: {value!r}"
        )
    return mode


def _positive_float(value: object, key: str) -> float:
    """0より大きい数値として解釈して返す。

    Raises:
        ValueError: 数値として解釈できない、または0以下の場合。
    """
    try:
        number = float(str(value).strip())
    except ValueError as e:
        raise ValueError(f"{key} must be a number greater than 0: {value!r}") from e
    if number <= 0:
        raise ValueError(f"{key} must be a number greater than 0: {value!r}")
    return number


def _non_negative_int(value: object, key: str) -> int:
    """0以上の整数として解釈して返す。

    Raises:
        ValueError: 整数として解釈できない、または0未満の場合。
    """
    try:
        number = int(str(value).strip())
    except ValueError as e:
        raise ValueError(f"{key} must be an integer of 0 or more: {value!r}") from e
    if number < 0:
        raise ValueError(f"{key} must be an integer of 0 or more: {value!r}")
    return number


def _retry_count(value: object, key: str) -> int:
    """再試行の回数（-1 は無制限、0 は再試行しない）として解釈して返す。

    Raises:
        ValueError: 整数として解釈できない、または -1 未満の場合。
    """
    try:
        number = int(str(value).strip())
    except ValueError as e:
        raise ValueError(f"{key} must be an integer of -1 (unlimited) or more: {value!r}") from e
    if number < -1:
        raise ValueError(f"{key} must be an integer of -1 (unlimited) or more: {value!r}")
    return number


def _positive_int(value: object, key: str) -> int:
    """1以上の整数として解釈して返す。

    Raises:
        ValueError: 整数として解釈できない、または1未満の場合。
    """
    try:
        number = int(str(value).strip())
    except ValueError as e:
        raise ValueError(f"{key} must be an integer of 1 or more: {value!r}") from e
    if number < 1:
        raise ValueError(f"{key} must be an integer of 1 or more: {value!r}")
    return number


def _validate_skill_draft_dir(cfg: "Config") -> None:
    """ドラフト置き場が起動時のスキル走査対象と重ならないことを確かめる。

    ドラフトは作成者の会話にだけ見せる未検証のスキルであり、skills_dir や
    project_locohane_dir 配下（全ユーザー共通で走査される）に置かれると、
    再起動のたびに全ユーザーへ公開され、同名なら正式スキルを上書きしてしまう。
    設定者の注意に頼らず、同一・包含関係のどちらも起動時に拒否する。

    Raises:
        ValueError: skill_draft_dir・skill_archive_dir が skills_dir・agents_dir・
            project_locohane_dirs・instance_locohane_dir のいずれかと同一、または
            包含関係にある場合、または skill_draft_dir と skill_archive_dir が重なる場合。
    """
    scanned = (
        ("[paths].skills_dir", cfg.skills_dir),
        ("[paths].agents_dir", cfg.agents_dir),
        *(("[paths].project_locohane_dir", d) for d in cfg.project_locohane_dirs),
        ("[skill_lab].instance_locohane_dir", cfg.instance_locohane_dir),
    )
    for key, target in (("draft_dir", cfg.skill_draft_dir), ("archive_dir", cfg.skill_archive_dir)):
        draft = target.resolve()
        for label, other in scanned:
            resolved = other.resolve()
            if draft == resolved or draft.is_relative_to(resolved) or resolved.is_relative_to(draft):
                raise ValueError(
                    f"[skill_creator].{key} ({draft}) must not overlap with {label} ({resolved}) "
                    "(to keep drafts out of every user's skill list)"
                )
    draft, archive = cfg.skill_draft_dir.resolve(), cfg.skill_archive_dir.resolve()
    if draft == archive or draft.is_relative_to(archive) or archive.is_relative_to(draft):
        raise ValueError(
            f"[skill_creator].archive_dir ({archive}) must not overlap with draft_dir ({draft}) "
            "(otherwise archived drafts would come back into the author's list)"
        )


def _parse_auth_users(value: str | None) -> dict[str, str]:
    """AUTH_USERS環境変数（Python風の [["user","pass"], ...] リテラル）を
    ユーザー名→パスワードの辞書へ変換する。

    _as_message_list と同様 ast.literal_eval を使う（末尾カンマ等の緩い
    記法も許容し、.env へのコピペを容易にするため）。

    Args:
        value: 環境変数 AUTH_USERS の生文字列。空欄・None なら空辞書。

    Returns:
        {ユーザー名: パスワード} の辞書（重複ユーザー名は後勝ち）。

    Raises:
        ValueError: リストとして解釈できない場合、または各要素が
            [ユーザー名, パスワード] の2要素配列でない場合。
    """
    if not value or not value.strip():
        return {}
    text = value.strip()
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"AUTH_USERS must be a Python list: {text!r}") from e
    if not isinstance(parsed, list):
        raise ValueError(f"AUTH_USERS must be a list (array): {text!r}")
    users: dict[str, str] = {}
    for item in parsed:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise ValueError(f"Each element of AUTH_USERS must be a two-element [username, password]: {item!r}")
        users[str(item[0])] = str(item[1])
    return users


def _parse_plan_approval_exempt_scripts(value: str | None) -> frozenset[tuple[str, str]]:
    """config.ini の [plan].plan_approval_exempt_scripts をパースする。

    run_script/run_script_background の計画承認（Plan Mode）を免除する、
    副作用のない読み取り専用スクリプトのホワイトリスト。Python風の
    [["スキル名","スクリプトファイル名"], ...] リテラルを _parse_auth_users
    と同様 ast.literal_eval で読む（末尾カンマ等の緩い記法も許容するため）。

    Args:
        value: config.ini から得たリスト形式の文字列、または環境変数由来の文字列。
            空欄・None なら空集合を返す。

    Returns:
        {(スキル名, スクリプトファイル名), ...} の frozenset。

    Raises:
        ValueError: リストとして解釈できない場合、または各要素が
            [スキル名, スクリプトファイル名] の2要素配列でない場合。
    """
    if not value or not value.strip():
        return frozenset()
    text = value.strip()
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"plan_approval_exempt_scripts must be a Python list: {text!r}") from e
    if not isinstance(parsed, list):
        raise ValueError(f"plan_approval_exempt_scripts must be a list (array): {text!r}")
    entries: set[tuple[str, str]] = set()
    for item in parsed:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise ValueError(f"Each element of plan_approval_exempt_scripts must be a two-element [skill name, script file name]: {item!r}")
        entries.add((str(item[0]), str(item[1])))
    return frozenset(entries)


def _parse_agent_type_run_script_allowlist(value: str | None) -> frozenset[tuple[str, str | tuple[str, str]]]:
    """config.ini の [subagent].agent_type_run_script_allowlist をパースする。

    dispatch_agent サブエージェントの agent_type ごとに、run_script
    で呼んでよいスキル/スクリプトを制限するホワイトリスト
    （未登録の agent_type は制限なし）。各要素は [agent_type, 対象] の2要素で、
    対象はさらに次の2種類のいずれかを許容する:
      - 文字列1件（例: "web-search"）: そのスキル配下の全スクリプトを許可。
      - [スキル名, スクリプトファイル名] の2要素配列（例:
        ["pdf-tools", "render_pdf_pages.py"]）: そのスクリプトのみ許可。
        pdf-tools のように同一スキル配下に読み込み専用スクリプトと書き込み
        スクリプトが混在するスキルは、スキル名単位ではなくこちらで個別に
        許可すること。
    例: [["explore", "docx-read"],
         ["explore", ["pdf-tools", "render_pdf_pages.py"]]]
    _parse_main_agent_tool_guard_allow_entries と同様 ast.literal_eval で読む
    （末尾カンマ等の緩い記法も許容するため）。

    Args:
        value: config.ini から得たリスト形式の文字列、または環境変数由来の文字列。
            空欄・None なら空集合を返す。

    Returns:
        {(agent_type, 対象), ...} の frozenset。対象はスキル名の文字列、または
        (スキル名, スクリプトファイル名) のタプル。

    Raises:
        ValueError: リストとして解釈できない場合、各要素が [agent_type, 対象] の
            2要素でない場合、agent_type が文字列でない場合、または対象が
            文字列でも2要素配列でもない場合。
    """
    if not value or not value.strip():
        return frozenset()
    text = value.strip()
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"agent_type_run_script_allowlist must be a Python list: {text!r}") from e
    if not isinstance(parsed, list):
        raise ValueError(f"agent_type_run_script_allowlist must be a list (array): {text!r}")
    entries: set[tuple[str, str | tuple[str, str]]] = set()
    for item in parsed:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise ValueError(
                f"Each element of agent_type_run_script_allowlist must be a two-element [agent_type, target]: {item!r}"
            )
        agent_type, target = item
        if not isinstance(agent_type, str):
            raise ValueError(f"agent_type of agent_type_run_script_allowlist must be a string: {agent_type!r}")
        key: str | tuple[str, str]
        if isinstance(target, str):
            key = target
        elif isinstance(target, (list, tuple)) and len(target) == 2:
            key = (str(target[0]), str(target[1]))
        else:
            raise ValueError(
                "The target of agent_type_run_script_allowlist must be a skill name string or "
                f"a two-element [skill name, script file name]: {target!r}"
            )
        entries.add((agent_type, key))
    return frozenset(entries)


def _parse_main_agent_tool_guard_allow_entries(value: str | None) -> frozenset[tuple[str | tuple[str, str], int]]:
    """config.ini の [main_agent_tool_guard].allow_entries をパースする。

    本ガードは許可リスト（ホワイトリスト）方式であり、ここに登録されて
    いないツール名・run_scriptスキルスクリプトはメインエージェントから
    一切呼び出せない（src/tools/tool_node.py の _guard_main_agent_tool_limit
    参照）。そのため main_agent_tool_guard_mode="all" で運用する場合、
    メインエージェントに必要な全ツールをここへ登録する必要がある
    （"tools_skills_only" ならMCP動的ツールは登録不要で常に許可される）。

    各要素は [対象, max_calls] の2要素配列。max_calls をエントリごとに
    個別指定できるようにするため、plan_approval_exempt_scripts のような
    「対象だけの集合」ではなく「対象→上限回数」のペアの集合として持つ。
    対象（1つ目の要素）はさらに次の2種類のいずれかを許容する
    （メインエージェント自身の直接呼び出しを制限したい対象が、ビルトイン
    ツール名単体の場合と run_script 配下のスキルスクリプトの場合の
    両方があるため）:
      - 文字列1件（例: "Glob", "analyze_image"）: ビルトインツール名そのもの。
      - [スキル名, スクリプトファイル名] の2要素配列（例:
        ["pdf-tools","render_pdf_pages.py"]）: run_script/
        run_script_background 経由で呼ばれるスキルスクリプト。
    max_calls は次の3通りの意味を持つ:
      - 0     : 登録はするが完全ブロック（1回も呼び出せない）。
      - -1    : 登録した上で回数無制限に許可する
                （dispatch_agent/create_plan のような、1ターン内で
                複数回呼ぶのが正常な基本運用ツール向け）。
      - 1以上 : その回数まで許可し、超過分を拒否する。
      -2以下は指定ミス防止のためエラーにする。
    例: entries = [["Glob", 1], ["dispatch_agent", -1], [["pdf-tools","render_pdf_pages.py"], 0]]
    Python風のリストリテラルを ast.literal_eval で読む（末尾カンマ等の
    緩い記法も許容するため）。

    Args:
        value: config.ini から得たリスト形式の文字列、または環境変数由来の文字列。
            空欄・None なら空集合を返す（許可リスト方式のため、この場合
            main_agent_tool_guard_mode="all" だとメインエージェントは
            いかなるツールも呼び出せなくなる点に注意。"tools_skills_only"
            ならMCP動的ツールのみ引き続き呼び出せる）。

    Returns:
        {(対象, max_calls), ...} の frozenset。対象はツール名の文字列、または
        (スキル名, スクリプトファイル名) のタプル。

    Raises:
        ValueError: リストとして解釈できない場合、各要素が [対象, max_calls] の
            2要素でない場合、対象が文字列でも2要素配列でもない場合、
            max_calls が整数でない場合、または max_calls が -2 以下の場合。
    """
    if not value or not value.strip():
        return frozenset()
    text = value.strip()
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"main_agent_tool_guard.allow_entries must be a Python list: {text!r}") from e
    if not isinstance(parsed, list):
        raise ValueError(f"main_agent_tool_guard.allow_entries must be a list (array): {text!r}")
    entries: set[tuple[str | tuple[str, str], int]] = set()
    seen_keys: set[str | tuple[str, str]] = set()
    for item in parsed:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise ValueError(f"Each element of main_agent_tool_guard.allow_entries must be a two-element [target, max_calls]: {item!r}")
        target, max_calls = item
        key: str | tuple[str, str]
        if isinstance(target, str):
            key = target
        elif isinstance(target, (list, tuple)) and len(target) == 2:
            key = (str(target[0]), str(target[1]))
        else:
            raise ValueError(
                "The target of main_agent_tool_guard.allow_entries must be a tool name string or "
                f"a two-element [skill name, script file name]: {target!r}"
            )
        if not isinstance(max_calls, int) or isinstance(max_calls, bool):
            raise ValueError(f"max_calls of main_agent_tool_guard.allow_entries must be an integer: {max_calls!r}")
        if max_calls < -1:
            raise ValueError(
                "max_calls of main_agent_tool_guard.allow_entries must be an integer of -1 or more "
                f"(0=fully blocked, -1=unlimited, 1 or more=allowed up to that many times): {max_calls!r}"
            )
        if key in seen_keys:
            # 同じ対象を異なる max_calls で複数回登録すると、_guard_main_agent_tool_limit
            # 側で frozenset を dict() 化する際にどちらが勝つかが Python の
            # ハッシュシード（PYTHONHASHSEED）依存になり、プロセス起動ごとに
            # 有効な max_calls が非決定的に変わってしまう（同一設定でも実行の
            # たびに結果が変わる実害を実測確認済み）。設定ミスとして早期に拒否する。
            raise ValueError(f"The same target is registered twice in main_agent_tool_guard.allow_entries: {key!r}")
        seen_keys.add(key)
        entries.add((key, max_calls))
    return frozenset(entries)


def _parse_allow_sandbox_dir(value: str | None, base: Path) -> tuple[SandboxDirEntry, ...]:
    """config.ini の [default_workdir].allow_sandbox_dir をパースする。

    書き込みサンドボックスガード（run_script/run_script_background 経由の
    み対象。execute_python_code系は対象外）が work_dir/default_workdir に
    加えて書き込みを許可する追加ディレクトリを、ディレクトリごとの許可
    リスト（allow_entries）付きで登録する。各要素は
    {"dir": ディレクトリ, "allow_entries": [...]} の辞書形式。
    allow_entries の各要素は [main_agent_tool_guard].allow_entries の対象
    指定と同じ形式（文字列1件でビルトインツール名そのもの、または
    [スキル名, スクリプトファイル名] の2要素配列でrun_script経由のスキル
    スクリプトのみを許可）。
    例: [{"dir": "E:/shared_output", "allow_entries": [["excel-edit", "write_excel.py"]]}]
    Python風の辞書・リストリテラルを ast.literal_eval で読む（末尾カンマ等の
    緩い記法も許容するため）。

    Args:
        value: config.ini から得たリスト形式の文字列、または環境変数由来の文字列。
            空欄・None なら空タプルを返す（=書き込みサンドボックス原則が
            そのまま適用され、追加許可ディレクトリは無い）。
        base: 相対パスの解決基準ディレクトリ（通常は PROJECT_ROOT）。

    Returns:
        SandboxDirEntry のタプル。

    Raises:
        ValueError: リストとして解釈できない場合、各要素が辞書でない場合、
            "dir" キーが無い・文字列でない場合、"allow_entries" が
            リストでない場合、またはその要素が文字列でも2要素配列でも
            ない場合。
    """
    if not value or not value.strip():
        return ()
    text = value.strip()
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"allow_sandbox_dir must be a Python list: {text!r}") from e
    if not isinstance(parsed, list):
        raise ValueError(f"allow_sandbox_dir must be a list (array): {text!r}")
    result: list[SandboxDirEntry] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError(f'Each element of allow_sandbox_dir must be a dict {{"dir": ..., "allow_entries": [...]}}: {item!r}')
        dir_value = item.get("dir")
        if not isinstance(dir_value, str) or not dir_value.strip():
            raise ValueError(f"dir of allow_sandbox_dir must be a non-empty string: {item!r}")
        allow_entries_raw = item.get("allow_entries", [])
        if not isinstance(allow_entries_raw, list):
            raise ValueError(f"allow_entries of allow_sandbox_dir must be a list (array): {item!r}")
        entries: set[str | tuple[str, str]] = set()
        for target in allow_entries_raw:
            if isinstance(target, str):
                entries.add(target)
            elif isinstance(target, (list, tuple)) and len(target) == 2:
                entries.add((str(target[0]), str(target[1])))
            else:
                raise ValueError(
                    "Each element of allow_entries of allow_sandbox_dir must be a tool name string or "
                    f"a two-element [skill name, script file name]: {target!r}"
                )
        result.append(SandboxDirEntry(dir=_resolve(base, dir_value), allow_entries=frozenset(entries)))
    return tuple(result)


def render_plan_approval_exempt_scripts_block(entries: frozenset[tuple[str, str]]) -> str:
    """system_prompt.md の {{plan_approval_exempt_scripts}} へ差し込むテキストを組み立てる。

    config.ini の [plan].plan_approval_exempt_scripts（frozenset）は集合の
    ため反復順序が不定。プロンプトへ差し込む表示を安定させるため、
    スキル名→スクリプトファイル名の順にソートしてから箇条書きへ整形する。

    Args:
        entries: {(スキル名, スクリプトファイル名), ...}（config.plan_approval_exempt_scripts）。

    Returns:
        差し込み用テキスト。空集合の場合は「（登録なし）」を返す。
    """
    if not entries:
        return "(none registered)"
    return "\n".join(f"- `{skill}` / `{script}`" for skill, script in sorted(entries))


def render_agent_type_run_script_allowlist_block(agent_type: str, entries: frozenset[tuple[str, str | tuple[str, str]]]) -> str:
    """agents/*.md の `{{run_script_allowlist}}` へ差し込むテキストを組み立てる。

    agent_type ごとに異なる内容を差し込む（`{{skills}}` も同じエントリで
    src/skills.py の filter_skills_for_agent_type が絞り込む）。呼び出し元
    （app.py/evals/run_case.py）が AgentType 1件ずつに対して agent_type を
    渡して呼ぶ）。config.ini の [subagent].agent_type_run_script_allowlist
    （subagent_agent_type_run_script_allowlist）は全 agent_type 分の
    (agent_type, 対象) をまとめて持つ frozenset のため、ここで対象の
    agent_type 分だけへ絞り込む。表示を安定させるため対象文字列でソートする。

    Args:
        agent_type: 差し込み先の agents/*.md の frontmatter name。
        entries: config.subagent_agent_type_run_script_allowlist（全 agent_type 分）。

    Returns:
        差し込み用テキスト。対象の agent_type のエントリが1件も無い場合は
        「（登録なし＝制限なし。run_scriptで全スキルのスクリプトを呼べる）」を返す
        （_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST は未登録の agent_type を無制限として
        扱うため、それと矛盾しない文言にする）。
    """
    targets = [target for a, target in entries if a == agent_type]
    if not targets:
        return "(none registered = no restriction; run_script can call the scripts of every skill)"

    def _format(target: str | tuple[str, str]) -> str:
        if isinstance(target, tuple):
            skill, script = target
            return f"- `{skill}` / `{script}`"
        return f"- `{target}` (all scripts in it)"

    def _sort_key(target: str | tuple[str, str]) -> tuple[str, str]:
        return target if isinstance(target, tuple) else (target, "")

    return "\n".join(_format(t) for t in sorted(targets, key=_sort_key))


def load_config(
    config_path: Path | None = None,
    overrides_path: Path | None = None,
    instance_name: str | None = None,
) -> Config:
    """config.ini を読み、config_overrides.json・環境変数で上書きした Config を返す。

    優先度（高い順）: 環境変数 > overrides_path（管理ツールでの変更） >
    config.ini（既定値）。overrides_path省略時は、環境変数
    CONFIG_OVERRIDES_PATH → default_overrides_path()（<[admin].instances_dir>/
    default/config_overrides.json）の順で決める（_apply_config_overrides() 参照）。

    環境変数（設定されていれば config.ini より優先）:
      LLM_MAIN_URL / LLM_MAIN_ROUTING_STRATEGY / LLM_SUB_URL / LLM_SUB_ROUTING_STRATEGY
      LLM_TEMPERATURE
      LLM_TOP_P / LLM_TOP_K / LLM_REPEAT_PENALTY / LLM_FREQUENCY_PENALTY / LLM_PRESENCE_PENALTY / LLM_MAX_TOKENS
      LLM_DRY_MULTIPLIER / LLM_DRY_BASE / LLM_DRY_ALLOWED_LENGTH / LLM_DRY_PENALTY_LAST_N / LLM_DRY_SEQUENCE_BREAKERS
      LLM_ENABLE_THINKING / LLM_TRACK_TOKEN_USAGE
      COMMON_DATA_DIR / SKILLS_DIR / AGENTS_DIR / PROJECT_LOCOHANE_DIR / SYSTEM_PROMPT_PATH / CHECKPOINT_DB / LOG_DIR / LOG_LEVEL / LOG_CLEAR_ON_STARTUP / DEFAULT_WORKDIR / MEMORY_DIR / PLANS_DIR / HELP_PATH
      PATH_MEMORY_DIR / PATH_MEMORY_RETENTION_DAYS / PATH_MEMORY_CLEANUP_INTERVAL_HOURS / PATH_MEMORY_MAX_ENTRIES
      PATH_MEMORY_SEARCH_MIN_SCORE / PATH_MEMORY_SEARCH_FILENAME_WEIGHT
      SCRIPT_TIMEOUT / SCRIPT_PYTHON / SCRIPT_REQUIRE_APPROVAL
      CODE_EXECUTION_ENABLED / CODE_EXECUTION_REQUIRE_APPROVAL
      FILE_TOOLS_DUPLICATE_GUARD_ENABLED / FILE_TOOLS_DUPLICATE_GUARD_MAX_CALLS /
      FILE_TOOLS_DUPLICATE_GUARD_CARRY_OVER_TO_MAIN
      GRAPH_IMPL / GRAPH_RECURSION_LIMIT
      SUBAGENT_MAX_ITERATIONS / SUBAGENT_SYSTEM_PROMPT_PATH / SUBAGENT_MAX_PARALLEL
      SUBAGENT_TOKEN_GUARD_ENABLED / SUBAGENT_TOKEN_GUARD_SOFT_THRESHOLD /
      SUBAGENT_TOKEN_GUARD_HARD_THRESHOLD
      APPROVAL_TIMEOUT_SECONDS / ASK_USER_TEXT_TIMEOUT_SECONDS / ASK_USER_CHOICE_TIMEOUT_SECONDS
      PLAN_BADGE_ALLOW_UNLOCK
      THINKING_LOOP_GUARD_ENABLED / THINKING_LOOP_GUARD_WINDOW_CHARS /
      THINKING_LOOP_GUARD_CHECK_INTERVAL_CHARS / THINKING_LOOP_GUARD_COMPRESSION_RATIO_THRESHOLD /
      THINKING_LOOP_GUARD_CONFIRM_COUNT / THINKING_LOOP_GUARD_MAX_RETRIES /
      THINKING_LOOP_GUARD_NUDGE_MESSAGES
      CONTEXT_TRIM_ENABLED / CONTEXT_TRIM_KEEP_RECENT_TOOL_ITERATIONS / CONTEXT_TRIM_TRUNCATED_MAX_CHARS
      CONTEXT_COMPACTION_ENABLED / CONTEXT_COMPACTION_TOKEN_THRESHOLD /
      CONTEXT_COMPACTION_KEEP_RECENT_ITERATIONS / CONTEXT_COMPACTION_MIN_MESSAGES_TO_COMPACT /
      CONTEXT_COMPACTION_PROMPT_PATH
      AUTH_ENABLED / AUTH_REQUIRE_PASSWORD / AUTH_USERS（AUTH_USERS は .env 専用、
      config.ini 側フォールバックを持たない）
      CHAT_LOG_ENABLED / CHAT_LOG_DIR
      THREAD_STORE_ENABLED / THREAD_STORE_DB / THREAD_STORE_RETENTION_DAYS /
      THREAD_STORE_CLEANUP_INTERVAL_HOURS
      CHAT_STARTER_PROMPTS
      MCP_ENABLED / MCP_SETTINGS_PATH / MCP_CONNECT_TIMEOUT_SECONDS / MCP_CALL_TIMEOUT_SECONDS
      （これら4値は .locohane/settings.json の "mcp" ブロックがあればさらに
      上書きされる。config.ini/環境変数はその既定値という位置づけ）

    パス系の値はすべて _resolve() でプロジェクトルート基準の絶対パスへ
    解決し、checkpoint_db の親ディレクトリ・log_dir は
    存在しなければここで作成する（アプリ起動時に呼ぶことを想定）。

    checkpoint_db/log_dir/default_workdir/memory_dir/plans_dir/
    path_memory_dir/chat_log_dir の値に "${common_data_dir}" という文字列が
    含まれる場合は、[paths].common_data_dir（環境変数 COMMON_DATA_DIR で
    上書き可能。既定 "./data/${instance}"）の解決済み絶対パスへ置換してから
    _resolve() する（_sub_common_data_dir() 参照）。データ保存先ルートを一括で
    変更したい場合に、各キーを個別に書き換えなくて済むようにするための仕組み。
    common_data_dir 自身を含むこれらの値中の "${instance}" は、インスタンス名
    （引数 instance_name、省略時は環境変数 LOCOHANE_INSTANCE、それも無ければ
    "default"）に置換される。

    Args:
        config_path: 読み込む config.ini のパス。省略時は
            <プロジェクトルート>/config.ini（DEFAULT_CONFIG_PATH）を使う。
        overrides_path: config_overrides.json のパス。省略時は環境変数
            CONFIG_OVERRIDES_PATH、それも無ければ default_overrides_path()。
        instance_name: ${instance} に入るインスタンス名。省略時は環境変数
            LOCOHANE_INSTANCE、それも無ければ DEFAULT_INSTANCE_NAME。

    Returns:
        LLM 接続情報・各種保存先パス・run_script 実行設定を集約した
        frozen な Config インスタンス。

    Raises:
        FileNotFoundError: config_path（または既定の config.ini）が
            存在しない場合。
        configparser.Error: config.ini の構文が不正な場合。
    """
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(path)
    # interpolation=None: configparser 標準の %(...)s 補間は使っていない
    # （${common_data_dir} 等は独自置換）。既定の BasicInterpolation のままだと
    # 値に "%" を1つ含むだけで ValueError になり、管理ツールから「残り10%」の
    # ような値を保存できなかった（2026-09-29 レビューで発見）。
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")

    # 全インスタンス共通の設定（設定ダッシュボードの「管理者設定」。admin_settings.json）→ インスタンス別の上書きの順に重ねる。
    _apply_admin_settings(parser, admin_settings_path(path))
    resolved_overrides_path = overrides_path or _resolve(
        PROJECT_ROOT, os.getenv("CONFIG_OVERRIDES_PATH") or str(default_overrides_path(path))
    )
    _apply_config_overrides(parser, resolved_overrides_path)

    llm = parser["llm"] if parser.has_section("llm") else {}
    paths = parser["paths"] if parser.has_section("paths") else {}
    chainlit_files = parser["chainlit_files"] if parser.has_section("chainlit_files") else {}
    elements_section = parser["elements"] if parser.has_section("elements") else {}
    images_section = parser["images"] if parser.has_section("images") else {}
    default_workdir_section = parser["default_workdir"] if parser.has_section("default_workdir") else {}
    skill_creator_section = parser["skill_creator"] if parser.has_section("skill_creator") else {}
    skill_lab_section = parser["skill_lab"] if parser.has_section("skill_lab") else {}
    skill_lab_worker_section = parser["skill_lab_worker"] if parser.has_section("skill_lab_worker") else {}
    path_memory = parser["path_memory"] if parser.has_section("path_memory") else {}
    vector_db = parser["vector_db"] if parser.has_section("vector_db") else {}
    log_section = parser["log"] if parser.has_section("log") else {}
    chat_log = parser["chat_log"] if parser.has_section("chat_log") else {}
    thread_store = parser["thread_store"] if parser.has_section("thread_store") else {}
    chat_starters = parser["chat_starters"] if parser.has_section("chat_starters") else {}
    scripts = parser["scripts"] if parser.has_section("scripts") else {}
    file_tools_duplicate_guard = parser["file_tools_duplicate_guard"] if parser.has_section("file_tools_duplicate_guard") else {}
    main_agent_tool_guard = parser["main_agent_tool_guard"] if parser.has_section("main_agent_tool_guard") else {}
    graph = parser["graph"] if parser.has_section("graph") else {}
    subagent = parser["subagent"] if parser.has_section("subagent") else {}
    timeouts = parser["user_response_timeouts"] if parser.has_section("user_response_timeouts") else {}
    plan_section = parser["plan"] if parser.has_section("plan") else {}
    thinking_loop_guard = parser["thinking_loop_guard"] if parser.has_section("thinking_loop_guard") else {}
    thinking_control = parser["thinking_control"] if parser.has_section("thinking_control") else {}
    tool_loop_guard = parser["tool_loop_guard"] if parser.has_section("tool_loop_guard") else {}
    context_trim = parser["context_trim"] if parser.has_section("context_trim") else {}
    context_trim_subagent = parser["context_trim.subagent"] if parser.has_section("context_trim.subagent") else {}
    context_compaction = parser["context_compaction"] if parser.has_section("context_compaction") else {}
    context_compaction_subagent = (
        parser["context_compaction.subagent"] if parser.has_section("context_compaction.subagent") else {}
    )

    # [xxx.subagent] 側の継承フォールバック（_subagent_override() 参照）に
    # 環境変数オーバーライドを反映させるため、[xxx] 側の実効値を先に計算する。
    # ここで config.ini の生値（context_trim.get(...) 等）へ直接フォールバック
    # すると、CONTEXT_TRIM_*/CONTEXT_COMPACTION_* 環境変数がサブエージェント側
    # にだけ反映されなくなる。
    _context_trim_enabled = _as_bool(os.getenv("CONTEXT_TRIM_ENABLED", context_trim.get("enabled", True)))
    _context_trim_keep_recent_tool_iterations = int(
        os.getenv("CONTEXT_TRIM_KEEP_RECENT_TOOL_ITERATIONS", context_trim.get("keep_recent_tool_iterations", 3))
    )
    _context_trim_truncated_max_chars = int(
        os.getenv("CONTEXT_TRIM_TRUNCATED_MAX_CHARS", context_trim.get("truncated_max_chars", 2000))
    )
    _context_trim_duplicate_guard_tool_max_chars = int(
        os.getenv(
            "CONTEXT_TRIM_DUPLICATE_GUARD_TOOL_MAX_CHARS",
            context_trim.get("duplicate_guard_tool_max_chars", 2000),
        )
    )
    _context_trim_ai_messages = _as_bool(os.getenv("CONTEXT_TRIM_AI_MESSAGES", context_trim.get("trim_ai_messages", True)))
    _context_trim_keep_recent_ai_iterations = int(
        os.getenv("CONTEXT_TRIM_KEEP_RECENT_AI_ITERATIONS", context_trim.get("keep_recent_ai_iterations", 3))
    )
    _context_trim_trigger_total_tokens = int(
        os.getenv("CONTEXT_TRIM_TRIGGER_TOTAL_TOKENS", context_trim.get("trigger_total_tokens", 100_000))
    )
    _context_compaction_enabled = _as_bool(
        os.getenv("CONTEXT_COMPACTION_ENABLED", context_compaction.get("enabled", True))
    )
    _context_compaction_token_threshold = int(
        os.getenv("CONTEXT_COMPACTION_TOKEN_THRESHOLD", context_compaction.get("token_threshold", 60000))
    )
    _context_compaction_single_request_token_threshold = int(
        os.getenv(
            "CONTEXT_COMPACTION_SINGLE_REQUEST_TOKEN_THRESHOLD",
            context_compaction.get("single_request_token_threshold", 60000),
        )
    )
    _context_compaction_keep_recent_iterations = int(
        os.getenv("CONTEXT_COMPACTION_KEEP_RECENT_ITERATIONS", context_compaction.get("keep_recent_iterations", 2))
    )
    _context_compaction_min_messages_to_compact = int(
        os.getenv(
            "CONTEXT_COMPACTION_MIN_MESSAGES_TO_COMPACT",
            context_compaction.get("min_messages_to_compact", 10),
        )
    )
    _context_compaction_prompt_path = _resolve(
        PROJECT_ROOT,
        os.getenv(
            "CONTEXT_COMPACTION_PROMPT_PATH",
            context_compaction.get("compaction_prompt_path", "./system_prompt/compaction_prompt.md"),
        ),
    )
    _context_compaction_summary_source_max_chars = int(
        os.getenv(
            "CONTEXT_COMPACTION_SUMMARY_SOURCE_MAX_CHARS",
            context_compaction.get("summary_source_max_chars", 2000),
        )
    )
    _context_compaction_skill_reattach_max_chars_per_skill = int(
        os.getenv(
            "CONTEXT_COMPACTION_SKILL_REATTACH_MAX_CHARS_PER_SKILL",
            context_compaction.get("skill_reattach_max_chars_per_skill", 12000),
        )
    )
    _context_compaction_skill_reattach_total_max_chars = int(
        os.getenv(
            "CONTEXT_COMPACTION_SKILL_REATTACH_TOTAL_MAX_CHARS",
            context_compaction.get("skill_reattach_total_max_chars", 36000),
        )
    )
    _context_compaction_pre_note_threshold = int(
        os.getenv("CONTEXT_COMPACTION_PRE_NOTE_THRESHOLD", context_compaction.get("pre_note_threshold", 40000))
    )
    _context_compaction_pre_note_warning_text = os.getenv(
        "CONTEXT_COMPACTION_PRE_NOTE_WARNING_TEXT",
        context_compaction.get("pre_note_warning_text", _DEFAULT_CONTEXT_COMPACTION_PRE_NOTE_WARNING_TEXT),
    )
    _context_compaction_require_note_max_skips = int(
        os.getenv(
            "CONTEXT_COMPACTION_REQUIRE_NOTE_MAX_SKIPS",
            context_compaction.get("require_note_max_skips", 2),
        )
    )

    auth = parser["auth"] if parser.has_section("auth") else {}
    mcp = parser["mcp"] if parser.has_section("mcp") else {}
    checkpointer = parser["checkpointer"] if parser.has_section("checkpointer") else {}
    ui = parser["ui"] if parser.has_section("ui") else {}
    websocket = parser["websocket"] if parser.has_section("websocket") else {}
    admin = parser["admin"] if parser.has_section("admin") else {}

    resolved_instance_name = instance_name or os.getenv("LOCOHANE_INSTANCE") or DEFAULT_INSTANCE_NAME
    common_data_dir = _resolve(
        PROJECT_ROOT,
        _sub_instance(
            os.getenv("COMMON_DATA_DIR", paths.get("common_data_dir", "./data/${instance}")), resolved_instance_name
        ),
    )

    project_locohane_entries = _parse_project_locohane_dirs(
        os.getenv("PROJECT_LOCOHANE_DIR", paths.get("project_locohane_dir", "./.locohane")),
        PROJECT_ROOT,
    )
    project_locohane_dirs = [d for d, _ in project_locohane_entries]
    # インスタンス専用の拡張ディレクトリ。project_locohane_dirs の後ろ（最優先）に常に足す
    # （config_overrides.json は project_locohane_dir のリストを丸ごと置き換えるため、
    # リストに入れると上書きしたインスタンスで抜け落ちる）。
    instance_dir = resolve_instances_root(path) / resolved_instance_name
    instance_locohane_dir = _resolve(
        PROJECT_ROOT,
        _sub_common_data_dir(
            _sub_instance_dir(
                os.getenv(
                    "INSTANCE_LOCOHANE_DIR", skill_lab_section.get("instance_locohane_dir", "${instance_dir}")
                ),
                instance_dir,
            ),
            common_data_dir,
            resolved_instance_name,
        ),
    )
    extension_dirs = [d for d in project_locohane_dirs if d != instance_locohane_dir] + [instance_locohane_dir]
    bin_path = _as_path_list(
        os.getenv("BIN_PATH", paths.get("bin_path", "")),
        PROJECT_ROOT,
    )
    allow_sandbox_dirs = _parse_allow_sandbox_dir(
        os.getenv("ALLOW_SANDBOX_DIR", default_workdir_section.get("allow_sandbox_dir", "")),
        PROJECT_ROOT,
    )

    main_url_raw = os.getenv("LLM_MAIN_URL", llm.get("main_url", _DEFAULT_LLM_URL))
    # sub_url が未指定（キー無し、または値が空）の場合、静的な接続先リストを
    # main_url からコピーするだけでなく、実行時に「委譲元メインエージェントの
    # このセッションが現在使っている接続先」をそのまま継承する
    # （sub_endpoints_inherit_main、src/llm.py の _select_endpoint 参照）。
    sub_url_configured_raw = os.getenv("LLM_SUB_URL", llm.get("sub_url", ""))
    sub_endpoints_inherit_main = not str(sub_url_configured_raw or "").strip()
    sub_url_raw = main_url_raw if sub_endpoints_inherit_main else sub_url_configured_raw

    vector_db_embedding_endpoints = _as_embedding_endpoints(os.getenv("VECTOR_DB_EMBEDDING_URL", vector_db.get("embedding_url", "")))

    cfg = Config(
        main_endpoints=_as_llm_endpoints(
            main_url_raw,
            "main_url",
        ),
        main_routing_strategy=_as_routing_strategy(
            os.getenv("LLM_MAIN_ROUTING_STRATEGY", llm.get("main_routing_strategy", "round_robin")),
            "main_routing_strategy",
        ),
        sub_endpoints=_as_llm_endpoints(
            sub_url_raw,
            "sub_url",
        ),
        sub_endpoints_inherit_main=sub_endpoints_inherit_main,
        sub_routing_strategy=_as_routing_strategy(
            os.getenv("LLM_SUB_ROUTING_STRATEGY", llm.get("sub_routing_strategy", "round_robin")),
            "sub_routing_strategy",
        ),
        sub_default_model=str(os.getenv("LLM_SUB_DEFAULT_MODEL", llm.get("sub_default_model", "")) or "").strip() or None,
        temperature=float(os.getenv("LLM_TEMPERATURE", llm.get("temperature", 0.3))),
        top_p=_as_optional_float(os.getenv("LLM_TOP_P", llm.get("top_p", ""))),
        top_k=_as_optional_int(os.getenv("LLM_TOP_K", llm.get("top_k", ""))),
        repeat_penalty=_as_optional_float(os.getenv("LLM_REPEAT_PENALTY", llm.get("repeat_penalty", ""))),
        frequency_penalty=_as_optional_float(os.getenv("LLM_FREQUENCY_PENALTY", llm.get("frequency_penalty", ""))),
        presence_penalty=_as_optional_float(os.getenv("LLM_PRESENCE_PENALTY", llm.get("presence_penalty", ""))),
        max_tokens=_as_optional_int(os.getenv("LLM_MAX_TOKENS", llm.get("max_tokens", ""))),
        dry_multiplier=_as_optional_float(os.getenv("LLM_DRY_MULTIPLIER", llm.get("dry_multiplier", ""))),
        dry_base=_as_optional_float(os.getenv("LLM_DRY_BASE", llm.get("dry_base", ""))),
        dry_allowed_length=_as_optional_int(os.getenv("LLM_DRY_ALLOWED_LENGTH", llm.get("dry_allowed_length", ""))),
        dry_penalty_last_n=_as_optional_int(os.getenv("LLM_DRY_PENALTY_LAST_N", llm.get("dry_penalty_last_n", ""))),
        dry_sequence_breakers=_as_optional_str_list(os.getenv("LLM_DRY_SEQUENCE_BREAKERS", llm.get("dry_sequence_breakers", ""))),
        enable_thinking=_as_optional_bool(os.getenv("LLM_ENABLE_THINKING", llm.get("enable_thinking", ""))),
        reasoning_format=_as_optional_reasoning_format(os.getenv("LLM_REASONING_FORMAT", llm.get("reasoning_format", ""))),
        reasoning_budget=_as_optional_int(os.getenv("LLM_REASONING_BUDGET", llm.get("reasoning_budget", ""))),
        reasoning_budget_message=_as_optional_str(
            os.getenv("LLM_REASONING_BUDGET_MESSAGE", llm.get("reasoning_budget_message", ""))
        ),
        reasoning_effort=_as_optional_reasoning_effort(os.getenv("LLM_REASONING_EFFORT", llm.get("reasoning_effort", ""))),
        reasoning_preserve=_as_optional_bool(os.getenv("LLM_REASONING_PRESERVE", llm.get("reasoning_preserve", ""))),
        track_token_usage=_as_bool(os.getenv("LLM_TRACK_TOKEN_USAGE", llm.get("track_token_usage", True))),
        request_timeout_seconds=float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", llm.get("request_timeout_seconds", 300))),
        stream_chunk_timeout_seconds=float(
            os.getenv(
                "LLM_STREAM_CHUNK_TIMEOUT_SECONDS",
                llm.get("stream_chunk_timeout_seconds", 120),
            )
        ),
        llm_max_concurrent_requests=int(
            os.getenv(
                "LLM_MAX_CONCURRENT_REQUESTS",
                llm.get("max_concurrent_requests", 1),
            )
        ),
        round_robin_slots_probe_timeout_seconds=float(
            os.getenv(
                "LLM_ROUND_ROBIN_SLOTS_PROBE_TIMEOUT_SECONDS",
                llm.get("round_robin_slots_probe_timeout_seconds", 3),
            )
        ),
        round_robin_busy_poll_interval_seconds=float(
            os.getenv(
                "LLM_ROUND_ROBIN_BUSY_POLL_INTERVAL_SECONDS",
                llm.get("round_robin_busy_poll_interval_seconds", 2),
            )
        ),
        skills_dir=_resolve(PROJECT_ROOT, os.getenv("SKILLS_DIR", paths.get("skills_dir", "./skills"))),
        agents_dir=_resolve(PROJECT_ROOT, os.getenv("AGENTS_DIR", paths.get("agents_dir", "./agents"))),
        project_locohane_dirs=project_locohane_dirs,
        locohane_skills_dirs=[d / "skills" for d in extension_dirs],
        locohane_agents_dirs=[d / "agents" for d in extension_dirs],
        locohane_skills_pythons={d / "skills": py for d, py in project_locohane_entries if py},
        instance_locohane_dir=instance_locohane_dir,
        instance_dir=instance_dir,
        bin_path=bin_path,
        system_prompt_path=_resolve(
            PROJECT_ROOT, os.getenv("SYSTEM_PROMPT_PATH", paths.get("system_prompt_path", "./system_prompt/system_prompt.md"))
        ),
        project_instructions_paths=[d / "LOCOHANE.md" for d in extension_dirs],
        common_data_dir=common_data_dir,
        checkpoint_db=_resolve(
            PROJECT_ROOT,
            _sub_common_data_dir(os.getenv("CHECKPOINT_DB", paths.get("checkpoint_db", "${common_data_dir}/checkpoints.sqlite")), common_data_dir, resolved_instance_name),
        ),
        log_dir=_resolve(
            PROJECT_ROOT, _sub_common_data_dir(os.getenv("LOG_DIR", log_section.get("dir", "${common_data_dir}/logs")), common_data_dir, resolved_instance_name)
        ),
        log_level=os.getenv("LOG_LEVEL", log_section.get("level", "info")).strip().lower(),
        log_clear_on_startup=_as_bool(os.getenv("LOG_CLEAR_ON_STARTUP", log_section.get("clear_on_startup", False))),
        default_workdir=_resolve(
            PROJECT_ROOT,
            _sub_common_data_dir(os.getenv("DEFAULT_WORKDIR", default_workdir_section.get("dir", "${common_data_dir}/temp")), common_data_dir, resolved_instance_name),
        ),
        allow_sandbox_dirs=allow_sandbox_dirs,
        memory_dir=_resolve(
            PROJECT_ROOT, _sub_common_data_dir(os.getenv("MEMORY_DIR", paths.get("memory_dir", "${common_data_dir}/memory")), common_data_dir, resolved_instance_name)
        ),
        plans_dir=_resolve(
            PROJECT_ROOT, _sub_common_data_dir(os.getenv("PLANS_DIR", paths.get("plans_dir", "${common_data_dir}/plans")), common_data_dir, resolved_instance_name)
        ),
        help_path=_resolve(PROJECT_ROOT, os.getenv("HELP_PATH", paths.get("help_path", "./system_prompt/help.md"))),
        skill_draft_dir=_resolve(
            PROJECT_ROOT,
            _sub_common_data_dir(
                os.getenv("SKILL_DRAFT_DIR", skill_creator_section.get("draft_dir", "${common_data_dir}/skill_drafts")),
                common_data_dir,
                resolved_instance_name,
            ),
        ),
        skill_tryout_repeats=_positive_int(
            os.getenv("SKILL_TRYOUT_REPEATS", skill_creator_section.get("tryout_repeats", 10)), "[skill_creator].tryout_repeats"
        ),
        skill_other_users_drafts=_parse_skill_draft_visibility(
            os.getenv("SKILL_OTHER_USERS_DRAFTS", skill_creator_section.get("other_users_drafts", "hidden"))
        ),
        skill_archive_dir=_resolve(
            PROJECT_ROOT,
            _sub_common_data_dir(
                os.getenv("SKILL_ARCHIVE_DIR", skill_creator_section.get("archive_dir", "${common_data_dir}/skill_drafts_archive")),
                common_data_dir,
                resolved_instance_name,
            ),
        ),
        skill_lab_max_iterations=_positive_int(
            os.getenv("SKILL_LAB_MAX_ITERATIONS", skill_lab_worker_section.get("max_iterations", 8)), "[skill_lab_worker].max_iterations"
        ),
        skill_lab_max_hours=_positive_float(os.getenv("SKILL_LAB_MAX_HOURS", skill_lab_worker_section.get("max_hours", 6)), "[skill_lab_worker].max_hours"),
        skill_lab_poll_interval_seconds=_positive_float(
            os.getenv("SKILL_LAB_POLL_INTERVAL_SECONDS", skill_lab_worker_section.get("poll_interval_seconds", 10)),
            "[skill_lab_worker].poll_interval_seconds",
        ),
        skill_lab_fixer_max_file_chars=_positive_int(
            os.getenv("SKILL_LAB_FIXER_MAX_FILE_CHARS", skill_lab_worker_section.get("fixer_max_file_chars", 20000)),
            "[skill_lab_worker].fixer_max_file_chars",
        ),
        skill_lab_transcript_excerpt_chars=_positive_int(
            os.getenv("SKILL_LAB_TRANSCRIPT_EXCERPT_CHARS", skill_lab_worker_section.get("transcript_excerpt_chars", 6000)),
            "[skill_lab_worker].transcript_excerpt_chars",
        ),
        chainlit_files_retention_days=int(os.getenv("CHAINLIT_FILES_RETENTION_DAYS", chainlit_files.get("retention_days", 7))),
        chainlit_files_cleanup_interval_hours=float(
            os.getenv("CHAINLIT_FILES_CLEANUP_INTERVAL_HOURS", chainlit_files.get("cleanup_interval_hours", 1))
        ),
        elements_dir=_resolve(
            PROJECT_ROOT,
            _sub_common_data_dir(os.getenv("ELEMENTS_DIR", elements_section.get("dir", "${common_data_dir}/elements")), common_data_dir, resolved_instance_name),
        ),
        elements_retention_days=int(os.getenv("ELEMENTS_RETENTION_DAYS", elements_section.get("retention_days", 0))),
        elements_cleanup_interval_hours=float(
            os.getenv("ELEMENTS_CLEANUP_INTERVAL_HOURS", elements_section.get("cleanup_interval_hours", 1))
        ),
        image_max_long_side_pixels=int(os.getenv("IMAGE_MAX_LONG_SIDE_PIXELS", images_section.get("max_long_side_pixels", 0))),
        image_jpeg_quality=int(os.getenv("IMAGE_JPEG_QUALITY", images_section.get("jpeg_quality", 85))),
        image_inline_preview_max_long_side_pixels=int(
            os.getenv("IMAGE_INLINE_PREVIEW_MAX_LONG_SIDE_PIXELS", images_section.get("inline_preview_max_long_side_pixels", 320))
        ),
        image_inline_preview_jpeg_quality=int(
            os.getenv("IMAGE_INLINE_PREVIEW_JPEG_QUALITY", images_section.get("inline_preview_jpeg_quality", 70))
        ),
        image_inline_preview_min_long_side_pixels=int(
            os.getenv(
                "IMAGE_INLINE_PREVIEW_MIN_LONG_SIDE_PIXELS", images_section.get("inline_preview_min_long_side_pixels", 0)
            )
        ),
        default_workdir_retention_days=int(os.getenv("DEFAULT_WORKDIR_RETENTION_DAYS", default_workdir_section.get("retention_days", 7))),
        default_workdir_cleanup_interval_hours=float(
            os.getenv("DEFAULT_WORKDIR_CLEANUP_INTERVAL_HOURS", default_workdir_section.get("cleanup_interval_hours", 1))
        ),
        path_memory_dir=_resolve(
            PROJECT_ROOT,
            _sub_common_data_dir(os.getenv("PATH_MEMORY_DIR", path_memory.get("dir", "${common_data_dir}/path_memory")), common_data_dir, resolved_instance_name),
        ),
        path_memory_retention_days=int(os.getenv("PATH_MEMORY_RETENTION_DAYS", path_memory.get("retention_days", 1))),
        path_memory_cleanup_interval_hours=float(os.getenv("PATH_MEMORY_CLEANUP_INTERVAL_HOURS", path_memory.get("cleanup_interval_hours", 1))),
        path_memory_max_entries=int(os.getenv("PATH_MEMORY_MAX_ENTRIES", path_memory.get("max_entries", 500))),
        path_memory_search_min_score=float(os.getenv("PATH_MEMORY_SEARCH_MIN_SCORE", path_memory.get("search_min_score", 0.3))),
        path_memory_search_filename_weight=float(
            os.getenv("PATH_MEMORY_SEARCH_FILENAME_WEIGHT", path_memory.get("search_filename_weight", 0.7))
        ),
        vector_db_enabled=_as_bool(os.getenv("VECTOR_DB_ENABLED", vector_db.get("enabled", False))),
        vector_db_backend=_as_vector_db_backend(os.getenv("VECTOR_DB_BACKEND", vector_db.get("backend", "sqlite_vec"))),
        vector_db_dir=_resolve(
            PROJECT_ROOT,
            _sub_common_data_dir(
                os.getenv("VECTOR_DB_DIR", vector_db.get("dir", "${common_data_dir}/vector_db")), common_data_dir, resolved_instance_name
            ),
        ),
        vector_db_qdrant_url=str(os.getenv("VECTOR_DB_QDRANT_URL", vector_db.get("qdrant_url", ""))).strip(),
        vector_db_qdrant_api_key=os.getenv("QDRANT_API_KEY", ""),
        vector_db_qdrant_collection_prefix=_sub_instance(
            str(os.getenv("VECTOR_DB_QDRANT_COLLECTION_PREFIX", vector_db.get("qdrant_collection_prefix", "locohane_${instance}_"))).strip(),
            resolved_instance_name,
        ),
        vector_db_qdrant_timeout_seconds=_positive_float(
            os.getenv("VECTOR_DB_QDRANT_TIMEOUT_SECONDS", vector_db.get("qdrant_timeout_seconds", 120)), "[vector_db].qdrant_timeout_seconds"
        ),
        vector_db_qdrant_max_retries=_retry_count(
            os.getenv("VECTOR_DB_QDRANT_MAX_RETRIES", vector_db.get("qdrant_max_retries", 5)), "[vector_db].qdrant_max_retries"
        ),
        vector_db_qdrant_retry_interval_seconds=_positive_float(
            os.getenv("VECTOR_DB_QDRANT_RETRY_INTERVAL_SECONDS", vector_db.get("qdrant_retry_interval_seconds", 10)),
            "[vector_db].qdrant_retry_interval_seconds",
        ),
        vector_db_qdrant_preset=_as_vector_db_qdrant_preset(os.getenv("VECTOR_DB_QDRANT_PRESET", vector_db.get("qdrant_preset", "none"))),
        vector_db_distance=_as_vector_db_distance(os.getenv("VECTOR_DB_DISTANCE", vector_db.get("distance", "Cosine"))),
        vector_db_embedding_endpoints=vector_db_embedding_endpoints,
        vector_db_embedding_batch_size=_positive_int(
            os.getenv("VECTOR_DB_EMBEDDING_BATCH_SIZE", vector_db.get("embedding_batch_size", 16)), "[vector_db].embedding_batch_size"
        ),
        vector_db_embedding_timeout_seconds=_positive_float(
            os.getenv("VECTOR_DB_EMBEDDING_TIMEOUT_SECONDS", vector_db.get("embedding_timeout_seconds", 120)),
            "[vector_db].embedding_timeout_seconds",
        ),
        vector_db_embedding_routing_strategy=_as_routing_strategy(
            os.getenv("VECTOR_DB_EMBEDDING_ROUTING_STRATEGY", vector_db.get("embedding_routing_strategy", "round_robin")),
            "embedding_routing_strategy",
            section="vector_db",
        ),
        vector_db_embedding_max_retries=_non_negative_int(
            os.getenv("VECTOR_DB_EMBEDDING_MAX_RETRIES", vector_db.get("embedding_max_retries", 3)), "[vector_db].embedding_max_retries"
        ),
        vector_db_search_embedding_max_retries=_non_negative_int(
            os.getenv("VECTOR_DB_SEARCH_EMBEDDING_MAX_RETRIES", vector_db.get("search_embedding_max_retries", 2)),
            "[vector_db].search_embedding_max_retries",
        ),
        vector_db_embedding_retry_interval_seconds=_positive_float(
            os.getenv("VECTOR_DB_EMBEDDING_RETRY_INTERVAL_SECONDS", vector_db.get("embedding_retry_interval_seconds", 10)),
            "[vector_db].embedding_retry_interval_seconds",
        ),
        vector_db_embedding_encoding_format=_as_vector_db_encoding_format(
            os.getenv("VECTOR_DB_EMBEDDING_ENCODING_FORMAT", vector_db.get("embedding_encoding_format", "base64"))
        ),
        vector_db_query_prefix=_as_embedding_prefix(
            os.getenv("VECTOR_DB_QUERY_PREFIX", vector_db.get("query_prefix", "auto")), vector_db_embedding_endpoints, "query"
        ),
        vector_db_document_prefix=_as_embedding_prefix(
            os.getenv("VECTOR_DB_DOCUMENT_PREFIX", vector_db.get("document_prefix", "auto")), vector_db_embedding_endpoints, "document"
        ),
        vector_db_chunk_size=_positive_int(os.getenv("VECTOR_DB_CHUNK_SIZE", vector_db.get("chunk_size", 800)), "[vector_db].chunk_size"),
        vector_db_chunk_overlap=int(os.getenv("VECTOR_DB_CHUNK_OVERLAP", vector_db.get("chunk_overlap", 100))),
        vector_db_include_extensions=_as_extension_list(
            os.getenv("VECTOR_DB_INCLUDE_EXTENSIONS", vector_db.get("include_extensions", ".pdf, .docx, .xlsx, .xlsm, .pptx, .md, .txt, .csv, .png, .jpg, .jpeg, .gif, .bmp, .webp, .tif, .tiff, .heic, .heif"))
        ),
        vector_db_text_extensions=_as_extension_list(os.getenv("VECTOR_DB_TEXT_EXTENSIONS", vector_db.get("text_extensions", ""))),
        vector_db_max_file_mb=_positive_float(os.getenv("VECTOR_DB_MAX_FILE_MB", vector_db.get("max_file_mb", 50)), "[vector_db].max_file_mb"),
        vector_db_image_input=_as_bool(os.getenv("VECTOR_DB_IMAGE_INPUT", vector_db.get("image_input", False))),
        vector_db_image_input_extensions=_as_page_image_extensions(
            os.getenv("VECTOR_DB_IMAGE_INPUT_EXTENSIONS", vector_db.get("image_input_extensions", ".pdf"))
        ),
        vector_db_embedded_images=_as_bool(os.getenv("VECTOR_DB_EMBEDDED_IMAGES", vector_db.get("embedded_images", True))),
        vector_db_embedded_image_min_px=_non_negative_int(
            os.getenv("VECTOR_DB_EMBEDDED_IMAGE_MIN_PX", vector_db.get("embedded_image_min_px", 128)), "[vector_db].embedded_image_min_px"
        ),
        vector_db_image_dpi=_positive_int(os.getenv("VECTOR_DB_IMAGE_DPI", vector_db.get("image_dpi", 100)), "[vector_db].image_dpi"),
        vector_db_office_timeout_seconds=_positive_float(
            os.getenv("VECTOR_DB_OFFICE_TIMEOUT_SECONDS", vector_db.get("office_timeout_seconds", 180)),
            "[vector_db].office_timeout_seconds",
        ),
        # 全インスタンス共通の設定のため、インスタンスごとに変わる ${common_data_dir}・${instance} は置換しない。
        vector_db_ingest_temp_dir=_as_ingest_temp_dir(str(os.getenv("VECTOR_DB_INGEST_TEMP_DIR", vector_db.get("ingest_temp_dir", "")))),
        vector_db_ingest_workers=_positive_int(
            os.getenv("VECTOR_DB_INGEST_WORKERS", vector_db.get("ingest_workers", 1)), "[vector_db].ingest_workers"
        ),
        vector_db_ingest_processes=_non_negative_int(
            os.getenv("VECTOR_DB_INGEST_PROCESSES", vector_db.get("ingest_processes", 0)), "[vector_db].ingest_processes"
        ),
        vector_db_ingest_embed_requests=_non_negative_int(
            os.getenv("VECTOR_DB_INGEST_EMBED_REQUESTS", vector_db.get("ingest_embed_requests", 0)),
            "[vector_db].ingest_embed_requests",
        ),
        vector_db_scan_workers=_positive_int(
            os.getenv("VECTOR_DB_SCAN_WORKERS", vector_db.get("scan_workers", 8)), "[vector_db].scan_workers"
        ),
        vector_db_max_files_per_collection=_positive_int(
            os.getenv("VECTOR_DB_MAX_FILES_PER_COLLECTION", vector_db.get("max_files_per_collection", 1000000)),
            "[vector_db].max_files_per_collection",
        ),
        vector_db_top_k=_positive_int(os.getenv("VECTOR_DB_TOP_K", vector_db.get("top_k", 5)), "[vector_db].top_k"),
        vector_db_max_result_chars=_positive_int(
            os.getenv("VECTOR_DB_MAX_RESULT_CHARS", vector_db.get("max_result_chars", 6000)), "[vector_db].max_result_chars"
        ),
        vector_db_min_score=float(os.getenv("VECTOR_DB_MIN_SCORE", vector_db.get("min_score", 0.3))),
        vector_db_image_min_score=float(os.getenv("VECTOR_DB_IMAGE_MIN_SCORE", vector_db.get("image_min_score", 0.1))),
        vector_db_path_min_score=float(os.getenv("VECTOR_DB_PATH_MIN_SCORE", vector_db.get("path_min_score", 0.3))),
        vector_db_rrf_weight_text=float(os.getenv("VECTOR_DB_RRF_WEIGHT_TEXT", vector_db.get("rrf_weight_text", 1.0))),
        vector_db_rrf_weight_keyword=float(os.getenv("VECTOR_DB_RRF_WEIGHT_KEYWORD", vector_db.get("rrf_weight_keyword", 1.0))),
        vector_db_rrf_weight_image=float(os.getenv("VECTOR_DB_RRF_WEIGHT_IMAGE", vector_db.get("rrf_weight_image", 1.0))),
        vector_db_rrf_weight_path_keyword=float(
            os.getenv("VECTOR_DB_RRF_WEIGHT_PATH_KEYWORD", vector_db.get("rrf_weight_path_keyword", 1.0))
        ),
        log_max_lines=int(os.getenv("LOG_MAX_LINES", log_section.get("max_lines", 5000))),
        log_retention_days=int(os.getenv("LOG_RETENTION_DAYS", log_section.get("retention_days", 7))),
        log_cleanup_interval_hours=float(os.getenv("LOG_CLEANUP_INTERVAL_HOURS", log_section.get("cleanup_interval_hours", 1))),
        chat_log_enabled=_as_bool(os.getenv("CHAT_LOG_ENABLED", chat_log.get("enabled", False))),
        chat_log_dir=_resolve(
            PROJECT_ROOT, _sub_common_data_dir(os.getenv("CHAT_LOG_DIR", chat_log.get("dir", "${common_data_dir}/logs_chat")), common_data_dir, resolved_instance_name)
        ),
        thread_store_enabled=_as_bool(os.getenv("THREAD_STORE_ENABLED", thread_store.get("enabled", True))),
        thread_store_db=_resolve(
            PROJECT_ROOT, _sub_common_data_dir(os.getenv("THREAD_STORE_DB", thread_store.get("db", "${common_data_dir}/chat_threads.sqlite")), common_data_dir, resolved_instance_name)
        ),
        thread_store_retention_days=int(os.getenv("THREAD_STORE_RETENTION_DAYS", thread_store.get("retention_days", 0))),
        thread_store_cleanup_interval_hours=float(
            os.getenv("THREAD_STORE_CLEANUP_INTERVAL_HOURS", thread_store.get("cleanup_interval_hours", 1))
        ),
        chat_starter_prompts=_as_message_list(os.getenv("CHAT_STARTER_PROMPTS", chat_starters.get("prompts", ""))),
        chat_starter_prompts_en=_as_message_list(
            os.getenv("CHAT_STARTER_PROMPTS_EN", chat_starters.get("prompts_en", ""))
        ),
        script_timeout=int(os.getenv("SCRIPT_TIMEOUT", scripts.get("timeout", 60))),
        # 空欄なら本体を起動している Python（python_env.bat で指定した環境）を使う
        script_python=os.getenv("SCRIPT_PYTHON", scripts.get("python", "")) or sys.executable,
        code_exec_enabled=_as_bool(os.getenv("CODE_EXECUTION_ENABLED", scripts.get("code_execution_enabled", True))),
        script_background_max_runtime_seconds=int(
            os.getenv(
                "SCRIPT_BACKGROUND_MAX_RUNTIME_SECONDS",
                scripts.get("background_max_runtime_seconds", 3600),
            )
        ),
        script_background_job_retention_seconds=int(
            os.getenv(
                "SCRIPT_BACKGROUND_JOB_RETENTION_SECONDS",
                scripts.get("background_job_retention_seconds", 1800),
            )
        ),
        script_background_min_poll_interval_seconds=int(
            os.getenv(
                "SCRIPT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS",
                scripts.get("background_min_poll_interval_seconds", 20),
            )
        ),
        script_background_min_poll_message=_validate_poll_message_template(
            os.getenv(
                "SCRIPT_BACKGROUND_MIN_POLL_MESSAGE",
                scripts.get("background_min_poll_message", "") or DEFAULT_SCRIPT_BACKGROUND_MIN_POLL_MESSAGE,
            )
        ),
        script_background_inline_wait_max_seconds=int(
            os.getenv(
                "SCRIPT_BACKGROUND_INLINE_WAIT_MAX_SECONDS",
                scripts.get("background_inline_wait_max_seconds", 0),
            )
        ),
        script_background_progress_push_interval_seconds=int(
            os.getenv(
                "SCRIPT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS",
                scripts.get("background_progress_push_interval_seconds", 20),
            )
        ),
        script_background_job_output_tail_chars=int(
            os.getenv(
                "SCRIPT_BACKGROUND_JOB_OUTPUT_TAIL_CHARS",
                scripts.get("background_job_output_tail_chars", 4000),
            )
        ),
        plan_approval_exempt_scripts=_parse_plan_approval_exempt_scripts(
            os.getenv(
                "PLAN_APPROVAL_EXEMPT_SCRIPTS",
                plan_section.get(
                    "plan_approval_exempt_scripts",
                    '[["excel-vba-read","read_vba.py"],["excel-read","read_excel.py"],'
                    '["excel-render","render_excel.py"],'
                    '["docx-read","read_docx.py"],["docx-render","render_docx.py"],'
                    '["pdf-tools","read_pdf.py"],["pdf-tools","render_pdf_pages.py"],'
                    '["pptx-read","read_pptx.py"],["pptx-inspect","inspect_pptx.py"],'
                    '["pptx-render","render_pptx.py"],'
                    '["web-search","search_web.py"],'
                    '["archive-tools","list_archive.py"]]',
                ),
            )
        ),
        subagent_agent_type_run_script_allowlist=_parse_agent_type_run_script_allowlist(
            os.getenv(
                "SUBAGENT_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST",
                subagent.get(
                    "agent_type_run_script_allowlist",
                    '[["explore","docx-render"],["explore","docx-read"],'
                    '["explore","excel-render"],["explore","excel-read"],'
                    '["explore","excel-vba-read"],'
                    '["explore","pptx-render"],["explore","pptx-read"],'
                    '["explore","pptx-inspect"],'
                    '["explore",["pdf-tools","render_pdf_pages.py"]],'
                    '["explore",["pdf-tools","read_pdf.py"]],'
                    '["explore",["archive-tools","list_archive.py"]],'
                    '["verifier","excel-render"],["verifier","excel-read"],'
                    '["verifier","docx-render"],["verifier","docx-read"],'
                    '["verifier","pptx-render"],["verifier","pptx-read"],'
                    '["verifier","pptx-inspect"],'
                    '["verifier",["pdf-tools","render_pdf_pages.py"]],'
                    '["verifier",["pdf-tools","read_pdf.py"]],'
                    '["verifier",["archive-tools","list_archive.py"]]]',
                ),
            )
        ),
        file_tools_duplicate_guard_enabled=_as_bool(
            os.getenv(
                "FILE_TOOLS_DUPLICATE_GUARD_ENABLED",
                file_tools_duplicate_guard.get("enabled", True),
            )
        ),
        file_tools_duplicate_guard_max_calls=int(
            os.getenv(
                "FILE_TOOLS_DUPLICATE_GUARD_MAX_CALLS",
                file_tools_duplicate_guard.get("max_calls", 1),
            )
        ),
        file_tools_duplicate_guard_carry_over_to_main=_as_bool(
            os.getenv(
                "FILE_TOOLS_DUPLICATE_GUARD_CARRY_OVER_TO_MAIN",
                file_tools_duplicate_guard.get("carry_over_to_main", True),
            )
        ),
        main_agent_tool_guard_mode=_as_main_agent_tool_guard_mode(
            os.getenv(
                "MAIN_AGENT_TOOL_GUARD_MODE",
                main_agent_tool_guard.get("mode", "all"),
            )
        ),
        main_agent_tool_guard_visibility_mode=_as_main_agent_tool_guard_visibility_mode(
            os.getenv(
                "MAIN_AGENT_TOOL_GUARD_VISIBILITY_MODE",
                main_agent_tool_guard.get("visibility_mode", "strict"),
            )
        ),
        main_agent_tool_guard_allow_entries=_parse_main_agent_tool_guard_allow_entries(
            os.getenv(
                "MAIN_AGENT_TOOL_GUARD_ALLOW_ENTRIES",
                main_agent_tool_guard.get("allow_entries", '[["Glob", 1]]'),
            )
        ),
        graph_impl=os.getenv("GRAPH_IMPL", graph.get("implementation", "handwritten")),
        graph_recursion_limit=int(os.getenv("GRAPH_RECURSION_LIMIT", graph.get("recursion_limit", 50))),
        graph_connection_error_max_retries=int(
            os.getenv(
                "GRAPH_CONNECTION_ERROR_MAX_RETRIES",
                graph.get("connection_error_max_retries", 3),
            )
        ),
        graph_tool_max_parallel=int(os.getenv("GRAPH_TOOL_MAX_PARALLEL", graph.get("max_parallel", 1))),
        graph_token_guard_enabled=_as_bool(os.getenv("GRAPH_TOKEN_GUARD_ENABLED", graph.get("token_guard_enabled", True))),
        graph_token_guard_soft_threshold=int(
            os.getenv(
                "GRAPH_TOKEN_GUARD_SOFT_THRESHOLD",
                graph.get("token_guard_soft_threshold", 49152),
            )
        ),
        graph_handoff_prompt_path=_resolve(
            PROJECT_ROOT,
            os.getenv(
                "GRAPH_HANDOFF_PROMPT_PATH",
                graph.get("handoff_prompt_path", "./system_prompt/handoff_prompt.md"),
            ),
        ),
        subagent_max_iterations=int(os.getenv("SUBAGENT_MAX_ITERATIONS", subagent.get("max_iterations", 6))),
        subagent_max_parallel=int(os.getenv("SUBAGENT_MAX_PARALLEL", subagent.get("max_parallel", 1))),
        subagent_token_guard_enabled=_as_bool(os.getenv("SUBAGENT_TOKEN_GUARD_ENABLED", subagent.get("token_guard_enabled", True))),
        subagent_token_guard_soft_threshold=int(
            os.getenv(
                "SUBAGENT_TOKEN_GUARD_SOFT_THRESHOLD",
                subagent.get("token_guard_soft_threshold", 40000),
            )
        ),
        subagent_token_guard_soft_warning_text=os.getenv(
            "SUBAGENT_TOKEN_GUARD_SOFT_WARNING_TEXT",
            subagent.get(
                "token_guard_soft_warning_text",
                "[System notice: the token usage of this task is approaching the limit. "
                "Do not add more tool calls; in your next response, summarize what you have found so far "
                "and what remains unprocessed (if anything)]",
            ),
        ),
        subagent_token_guard_hard_threshold=int(
            os.getenv(
                "SUBAGENT_TOKEN_GUARD_HARD_THRESHOLD",
                subagent.get("token_guard_hard_threshold", 55000),
            )
        ),
        subagent_empty_response_max_retries=int(
            os.getenv(
                "SUBAGENT_EMPTY_RESPONSE_MAX_RETRIES",
                subagent.get("empty_response_max_retries", 2),
            )
        ),
        subagent_background_job_retention_seconds=int(
            os.getenv(
                "SUBAGENT_BACKGROUND_JOB_RETENTION_SECONDS",
                subagent.get("background_job_retention_seconds", 1800),
            )
        ),
        subagent_background_min_poll_interval_seconds=int(
            os.getenv(
                "SUBAGENT_BACKGROUND_MIN_POLL_INTERVAL_SECONDS",
                subagent.get("background_min_poll_interval_seconds", 60),
            )
        ),
        subagent_background_min_poll_message=_validate_poll_message_template(
            os.getenv(
                "SUBAGENT_BACKGROUND_MIN_POLL_MESSAGE",
                subagent.get("background_min_poll_message", "") or DEFAULT_DISPATCH_AGENT_BACKGROUND_MIN_POLL_MESSAGE,
            )
        ),
        subagent_background_inline_wait_max_seconds=int(
            os.getenv(
                "SUBAGENT_BACKGROUND_INLINE_WAIT_MAX_SECONDS",
                subagent.get("background_inline_wait_max_seconds", 1800),
            )
        ),
        subagent_background_progress_push_interval_seconds=int(
            os.getenv(
                "SUBAGENT_BACKGROUND_PROGRESS_PUSH_INTERVAL_SECONDS",
                subagent.get("background_progress_push_interval_seconds", 20),
            )
        ),
        subagent_background_llm_timeout_max_retries=int(
            os.getenv(
                "SUBAGENT_BACKGROUND_LLM_TIMEOUT_MAX_RETRIES",
                subagent.get("background_llm_timeout_max_retries", 3),
            )
        ),
        subagent_batch_max_groups=int(
            os.getenv(
                "SUBAGENT_BATCH_MAX_GROUPS",
                subagent.get("batch_max_groups", 50),
            )
        ),
        subagent_batch_stop_after_unreachable_groups=int(
            os.getenv(
                "SUBAGENT_BATCH_STOP_AFTER_UNREACHABLE_GROUPS",
                subagent.get("batch_stop_after_unreachable_groups", 1),
            )
        ),
        approval_timeout_seconds=int(os.getenv("APPROVAL_TIMEOUT_SECONDS", timeouts.get("approval_seconds", 300))),
        ask_user_question_timeout_seconds=int(os.getenv("ASK_USER_QUESTION_TIMEOUT_SECONDS", timeouts.get("ask_user_question_seconds", 60))),
        ask_user_choice_timeout_seconds=int(os.getenv("ASK_USER_CHOICE_TIMEOUT_SECONDS", timeouts.get("ask_user_choice_seconds", 90))),
        plan_badge_allow_unlock=_as_bool(os.getenv("PLAN_BADGE_ALLOW_UNLOCK", plan_section.get("allow_badge_unlock", True))),
        plan_reset_approval_on_recreate=_as_bool(
            os.getenv("PLAN_RESET_APPROVAL_ON_RECREATE", plan_section.get("reset_approval_on_recreate", True))
        ),
        plan_reset_approval_on_new_message=_as_bool(
            os.getenv("PLAN_RESET_APPROVAL_ON_NEW_MESSAGE", plan_section.get("reset_approval_on_new_message", True))
        ),
        plan_auto_approve=_as_bool(os.getenv("PLAN_AUTO_APPROVE", plan_section.get("auto_approve", False))),
        thinking_loop_guard_enabled=_as_bool(os.getenv("THINKING_LOOP_GUARD_ENABLED", thinking_loop_guard.get("enabled", True))),
        thinking_loop_guard_target=_as_thinking_loop_guard_target(
            os.getenv("THINKING_LOOP_GUARD_TARGET", thinking_loop_guard.get("target", "all"))
        ),
        thinking_loop_guard_window_chars=int(os.getenv("THINKING_LOOP_GUARD_WINDOW_CHARS", thinking_loop_guard.get("window_chars", 600))),
        thinking_loop_guard_check_interval_chars=int(
            os.getenv(
                "THINKING_LOOP_GUARD_CHECK_INTERVAL_CHARS",
                thinking_loop_guard.get("check_interval_chars", 150),
            )
        ),
        thinking_loop_guard_confirm_count=int(os.getenv("THINKING_LOOP_GUARD_CONFIRM_COUNT", thinking_loop_guard.get("confirm_count", 2))),
        thinking_loop_guard_max_history_chars=int(
            os.getenv(
                "THINKING_LOOP_GUARD_MAX_HISTORY_CHARS",
                thinking_loop_guard.get("max_history_chars", 4000),
            )
        ),
        thinking_loop_guard_match_ratio_threshold=float(
            os.getenv(
                "THINKING_LOOP_GUARD_MATCH_RATIO_THRESHOLD",
                thinking_loop_guard.get("match_ratio_threshold", 0.2),
            )
        ),
        thinking_loop_guard_max_retries=int(os.getenv("THINKING_LOOP_GUARD_MAX_RETRIES", thinking_loop_guard.get("max_retries", 2))),
        thinking_loop_guard_nudge_messages=_as_message_list(
            os.getenv("THINKING_LOOP_GUARD_NUDGE_MESSAGES", thinking_loop_guard.get("nudge_messages", ""))
        ),
        thinking_loop_guard_empty_response_max_retries=int(
            os.getenv(
                "THINKING_LOOP_GUARD_EMPTY_RESPONSE_MAX_RETRIES",
                thinking_loop_guard.get("empty_response_max_retries", 2),
            )
        ),
        tool_loop_guard_enabled=_as_bool(os.getenv("TOOL_LOOP_GUARD_ENABLED", tool_loop_guard.get("enabled", True))),
        tool_loop_guard_max_repeats=int(os.getenv("TOOL_LOOP_GUARD_MAX_REPEATS", tool_loop_guard.get("max_repeats", 3))),
        tool_loop_guard_exclude_tools=_as_optional_str_list(
            os.getenv(
                "TOOL_LOOP_GUARD_EXCLUDE_TOOLS",
                tool_loop_guard.get("exclude_tools", '["check_script_job", "check_dispatch_agent_job"]'),
            )
        )
        or [],
        tool_loop_guard_nudge_messages=_as_message_list(
            os.getenv("TOOL_LOOP_GUARD_NUDGE_MESSAGES", tool_loop_guard.get("nudge_messages", ""))
        ),
        thinking_control_enabled=_as_bool(os.getenv("THINKING_CONTROL_ENABLED", thinking_control.get("enabled", False))),
        thinking_control_apply_to_main=_as_bool(
            os.getenv("THINKING_CONTROL_APPLY_TO_MAIN", thinking_control.get("apply_to_main", True))
        ),
        thinking_control_apply_to_sub=_as_bool(
            os.getenv("THINKING_CONTROL_APPLY_TO_SUB", thinking_control.get("apply_to_sub", True))
        ),
        thinking_control_rule_user_turn=_as_optional_thinking_level(
            os.getenv("THINKING_CONTROL_RULE_USER_TURN", thinking_control.get("rule_user_turn", "high")),
            "rule_user_turn",
        ),
        thinking_control_rule_tool_error=_as_optional_thinking_level(
            os.getenv("THINKING_CONTROL_RULE_TOOL_ERROR", thinking_control.get("rule_tool_error", "high")),
            "rule_tool_error",
        ),
        thinking_control_rule_consecutive_cap=_as_optional_thinking_level(
            os.getenv("THINKING_CONTROL_RULE_CONSECUTIVE_CAP", thinking_control.get("rule_consecutive_cap", "high")),
            "rule_consecutive_cap",
        ),
        thinking_control_rule_after_tools=_as_optional_thinking_level(
            os.getenv("THINKING_CONTROL_RULE_AFTER_TOOLS", thinking_control.get("rule_after_tools", "low")),
            "rule_after_tools",
        ),
        thinking_control_default_level=_as_thinking_level(
            os.getenv("THINKING_CONTROL_DEFAULT_LEVEL", thinking_control.get("default_level", "high")),
            "default_level",
        ),
        thinking_control_budget_low=_as_optional_int(
            os.getenv("THINKING_CONTROL_BUDGET_LOW", thinking_control.get("budget_low", "1024"))
        ),
        thinking_control_budget_medium=_as_optional_int(
            os.getenv("THINKING_CONTROL_BUDGET_MEDIUM", thinking_control.get("budget_medium", "2048"))
        ),
        thinking_control_budget_high=_as_optional_int(
            os.getenv("THINKING_CONTROL_BUDGET_HIGH", thinking_control.get("budget_high", ""))
        ),
        thinking_control_budget_xhigh=_as_optional_int(
            os.getenv("THINKING_CONTROL_BUDGET_XHIGH", thinking_control.get("budget_xhigh", "-1"))
        ),
        thinking_control_after_tools=_as_optional_str_list(
            os.getenv("THINKING_CONTROL_AFTER_TOOLS", thinking_control.get("after_tools", _DEFAULT_THINKING_CONTROL_AFTER_TOOLS))
        )
        or [],
        thinking_control_error_prefixes=_as_optional_str_list(
            os.getenv("THINKING_CONTROL_ERROR_PREFIXES", thinking_control.get("error_prefixes", '["Error"]'))
        )
        or [],
        thinking_control_max_consecutive_reduced=int(
            os.getenv("THINKING_CONTROL_MAX_CONSECUTIVE_REDUCED", thinking_control.get("max_consecutive_reduced", 3))
        ),
        context_trim_enabled=_context_trim_enabled,
        context_trim_keep_recent_tool_iterations=_context_trim_keep_recent_tool_iterations,
        context_trim_truncated_max_chars=_context_trim_truncated_max_chars,
        context_trim_duplicate_guard_tool_max_chars=_context_trim_duplicate_guard_tool_max_chars,
        context_trim_ai_messages=_context_trim_ai_messages,
        context_trim_keep_recent_ai_iterations=_context_trim_keep_recent_ai_iterations,
        context_trim_trigger_total_tokens=_context_trim_trigger_total_tokens,
        context_trim_subagent_enabled=_subagent_override(
            context_trim_subagent, "enabled", _context_trim_enabled, _as_bool
        ),
        context_trim_subagent_keep_recent_tool_iterations=_subagent_override(
            context_trim_subagent, "keep_recent_tool_iterations", _context_trim_keep_recent_tool_iterations, int
        ),
        context_trim_subagent_truncated_max_chars=_subagent_override(
            context_trim_subagent, "truncated_max_chars", _context_trim_truncated_max_chars, int
        ),
        context_trim_subagent_duplicate_guard_tool_max_chars=_subagent_override(
            context_trim_subagent,
            "duplicate_guard_tool_max_chars",
            _context_trim_duplicate_guard_tool_max_chars,
            int,
        ),
        context_trim_subagent_ai_messages=_subagent_override(
            context_trim_subagent, "trim_ai_messages", _context_trim_ai_messages, _as_bool
        ),
        context_trim_subagent_keep_recent_ai_iterations=_subagent_override(
            context_trim_subagent, "keep_recent_ai_iterations", _context_trim_keep_recent_ai_iterations, int
        ),
        context_trim_subagent_trigger_total_tokens=_subagent_override(
            context_trim_subagent, "trigger_total_tokens", _context_trim_trigger_total_tokens, int
        ),
        context_compaction_enabled=_context_compaction_enabled,
        context_compaction_token_threshold=_context_compaction_token_threshold,
        context_compaction_single_request_token_threshold=_context_compaction_single_request_token_threshold,
        context_compaction_keep_recent_iterations=_context_compaction_keep_recent_iterations,
        context_compaction_min_messages_to_compact=_context_compaction_min_messages_to_compact,
        context_compaction_prompt_path=_context_compaction_prompt_path,
        context_compaction_summary_source_max_chars=_context_compaction_summary_source_max_chars,
        context_compaction_skill_reattach_max_chars_per_skill=_context_compaction_skill_reattach_max_chars_per_skill,
        context_compaction_skill_reattach_total_max_chars=_context_compaction_skill_reattach_total_max_chars,
        context_compaction_pre_note_threshold=_context_compaction_pre_note_threshold,
        context_compaction_pre_note_warning_text=_context_compaction_pre_note_warning_text,
        context_compaction_require_note_max_skips=_context_compaction_require_note_max_skips,
        context_compaction_subagent_enabled=_subagent_override(
            context_compaction_subagent, "enabled", _context_compaction_enabled, _as_bool
        ),
        context_compaction_subagent_token_threshold=_subagent_override(
            context_compaction_subagent, "token_threshold", _context_compaction_token_threshold, int
        ),
        context_compaction_subagent_single_request_token_threshold=_subagent_override(
            context_compaction_subagent,
            "single_request_token_threshold",
            _context_compaction_single_request_token_threshold,
            int,
        ),
        context_compaction_subagent_keep_recent_iterations=_subagent_override(
            context_compaction_subagent, "keep_recent_iterations", _context_compaction_keep_recent_iterations, int
        ),
        context_compaction_subagent_min_messages_to_compact=_subagent_override(
            context_compaction_subagent,
            "min_messages_to_compact",
            _context_compaction_min_messages_to_compact,
            int,
        ),
        context_compaction_subagent_prompt_path=_subagent_override(
            context_compaction_subagent,
            "compaction_prompt_path",
            _context_compaction_prompt_path,
            lambda v: _resolve(PROJECT_ROOT, v),
        ),
        context_compaction_subagent_summary_source_max_chars=_subagent_override(
            context_compaction_subagent,
            "summary_source_max_chars",
            _context_compaction_summary_source_max_chars,
            int,
        ),
        context_compaction_subagent_pre_note_threshold=_subagent_override(
            context_compaction_subagent, "pre_note_threshold", _context_compaction_pre_note_threshold, int
        ),
        context_compaction_subagent_pre_note_warning_text=_subagent_override(
            context_compaction_subagent, "pre_note_warning_text", _context_compaction_pre_note_warning_text
        ),
        context_compaction_subagent_require_note_max_skips=_subagent_override(
            context_compaction_subagent,
            "require_note_max_skips",
            _context_compaction_require_note_max_skips,
            int,
        ),
        auth_enabled=_as_bool(os.getenv("AUTH_ENABLED", auth.get("enabled", False))),
        auth_require_password=_as_bool(os.getenv("AUTH_REQUIRE_PASSWORD", auth.get("require_password", True))),
        auth_users=_parse_auth_users(os.getenv("AUTH_USERS", "")),
        mcp_enabled=_as_bool(os.getenv("MCP_ENABLED", mcp.get("enabled", True))),
        mcp_settings_path=_resolve(
            PROJECT_ROOT,
            os.getenv("MCP_SETTINGS_PATH", mcp.get("settings_path", "./.locohane/settings.json")),
        ),
        mcp_connect_timeout_seconds=float(os.getenv("MCP_CONNECT_TIMEOUT_SECONDS", mcp.get("connect_timeout_seconds", 15))),
        mcp_call_timeout_seconds=float(os.getenv("MCP_CALL_TIMEOUT_SECONDS", mcp.get("call_timeout_seconds", 60))),
        checkpointer_op_timeout_seconds=float(
            os.getenv(
                "CHECKPOINTER_OP_TIMEOUT_SECONDS",
                checkpointer.get("op_timeout_seconds", 15),
            )
        ),
        checkpointer_close_timeout_seconds=float(
            os.getenv(
                "CHECKPOINTER_CLOSE_TIMEOUT_SECONDS",
                checkpointer.get("close_timeout_seconds", 3),
            )
        ),
        checkpointer_shutdown_drain_timeout_seconds=float(
            os.getenv(
                "CHECKPOINTER_SHUTDOWN_DRAIN_TIMEOUT_SECONDS",
                checkpointer.get("shutdown_drain_timeout_seconds", 5),
            )
        ),
        ui_max_display_messages=int(os.getenv("UI_MAX_DISPLAY_MESSAGES", ui.get("max_display_messages", 50))),
        ui_max_display_side_steps=int(
            os.getenv("UI_MAX_DISPLAY_SIDE_STEPS", ui.get("max_display_side_steps", 50))
        ),
        ui_paste_as_attachment_threshold_chars=int(
            os.getenv(
                "UI_PASTE_AS_ATTACHMENT_THRESHOLD_CHARS", ui.get("paste_as_attachment_threshold_chars", 2000)
            )
        ),
        ui_max_input_chars=int(os.getenv("UI_MAX_INPUT_CHARS", ui.get("max_input_chars", 30000))),
        ui_token_usage_warn_threshold=int(
            os.getenv("UI_TOKEN_USAGE_WARN_THRESHOLD", ui.get("token_usage_warn_threshold", 48000))
        ),
        ui_token_usage_alert_threshold=int(
            os.getenv("UI_TOKEN_USAGE_ALERT_THRESHOLD", ui.get("token_usage_alert_threshold", 64000))
        ),
        ui_work_dir_max_suggestions=int(
            os.getenv("UI_WORK_DIR_MAX_SUGGESTIONS", ui.get("work_dir_max_suggestions", 100))
        ),
        ui_default_language=_as_ui_language(os.getenv("UI_DEFAULT_LANGUAGE", ui.get("default_language", "auto"))),
        websocket_ping_interval_seconds=int(
            os.getenv("WEBSOCKET_PING_INTERVAL_SECONDS", websocket.get("ping_interval_seconds", 25))
        ),
        websocket_ping_timeout_seconds=int(
            os.getenv("WEBSOCKET_PING_TIMEOUT_SECONDS", websocket.get("ping_timeout_seconds", 20))
        ),
        admin_host=os.getenv("ADMIN_HOST", admin.get("host", "127.0.0.1")),
        admin_port=int(os.getenv("ADMIN_PORT", admin.get("port", 8001))),
        admin_session_timeout_minutes=int(
            os.getenv("ADMIN_SESSION_TIMEOUT_MINUTES", admin.get("session_timeout_minutes", 60))
        ),
        admin_backup_keep=int(os.getenv("ADMIN_BACKUP_KEEP", admin.get("backup_keep", 20))),
        admin_stop_apps_on_exit=_as_bool(
            os.getenv("ADMIN_STOP_APPS_ON_EXIT", admin.get("stop_apps_on_exit", False))
        ),
        admin_instances_dir=resolve_instances_root(path),
        admin_assistant_enabled=_as_bool(
            os.getenv("ADMIN_ASSISTANT_ENABLED", admin.get("assistant_enabled", True))
        ),
        admin_assistant_llm_instance=os.getenv(
            "ADMIN_ASSISTANT_LLM_INSTANCE", admin.get("assistant_llm_instance", "")
        ).strip(),
        admin_assistant_max_keys=_positive_int(
            os.getenv("ADMIN_ASSISTANT_MAX_KEYS", admin.get("assistant_max_keys", 12)), "[admin].assistant_max_keys"
        ),
        admin_assistant_max_docs=_non_negative_int(
            os.getenv("ADMIN_ASSISTANT_MAX_DOCS", admin.get("assistant_max_docs", 4)), "[admin].assistant_max_docs"
        ),
        admin_assistant_use_embedding=_as_bool(
            os.getenv("ADMIN_ASSISTANT_USE_EMBEDDING", admin.get("assistant_use_embedding", True))
        ),
        admin_assistant_embedding_instance=os.getenv(
            "ADMIN_ASSISTANT_EMBEDDING_INSTANCE", admin.get("assistant_embedding_instance", "")
        ).strip(),
        admin_assistant_max_history_messages=_positive_int(
            os.getenv("ADMIN_ASSISTANT_MAX_HISTORY_MESSAGES", admin.get("assistant_max_history_messages", 20)),
            "[admin].assistant_max_history_messages",
        ),
    )

    # .locohane/settings.json の "mcp" ブロックがあれば、config.ini/環境変数由来の
    # 既定値をさらに上書きする（settings.json が最優先）。settings.json の
    # "mcpServers" 本体の読み込みは src/mcp_client.py 側で独立して行うため、
    # ここでは config.py が mcp_client.py に依存する循環importを避けるため、
    # このブロックだけを自前で読む（構文エラーは他のconfig読み込みと同様に伝播させる）。
    mcp_overrides = _load_mcp_global_overrides(cfg.mcp_settings_path)
    if mcp_overrides:
        cfg = replace(
            cfg,
            mcp_enabled=_as_bool(mcp_overrides.get("enabled", cfg.mcp_enabled)),
            mcp_connect_timeout_seconds=float(mcp_overrides.get("connectTimeoutSeconds", cfg.mcp_connect_timeout_seconds)),
            mcp_call_timeout_seconds=float(mcp_overrides.get("callTimeoutSeconds", cfg.mcp_call_timeout_seconds)),
        )

    _validate_skill_draft_dir(cfg)
    _validate_vector_db(cfg)
    _validate_local_data_paths(cfg)

    # data 配下のディレクトリを確実に用意する（checkpoint_db は親ディレクトリを作る）。
    cfg.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    cfg.thread_store_db.parent.mkdir(parents=True, exist_ok=True)
    cfg.elements_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    cfg.path_memory_dir.mkdir(parents=True, exist_ok=True)
    if cfg.vector_db_enabled:
        cfg.vector_db_dir.mkdir(parents=True, exist_ok=True)
    cfg.default_workdir.mkdir(parents=True, exist_ok=True)
    cfg.plans_dir.mkdir(parents=True, exist_ok=True)
    cfg.skill_draft_dir.mkdir(parents=True, exist_ok=True)
    memory.ensure_dirs(cfg.memory_dir)

    return cfg


def _load_mcp_global_overrides(path: Path) -> dict:
    """.locohane/settings.json の "mcp" ブロック（全体挙動の上書き設定）を読む。

    src/mcp_client.py には依存しない自己完結の実装（循環import回避）。
    "mcpServers"（個々のサーバー定義）はここでは読まない
    （src/mcp_client.py が起動時に独立して読み込む）。

    Args:
        path: cfg.mcp_settings_path（既定 <root>/.locohane/settings.json）。

    Returns:
        "mcp" ブロックの内容（dict）。ファイル不在、または "mcp" キー
        自体が無ければ空の dict。

    Raises:
        json.JSONDecodeError: settings.json の構文が不正な場合
            （config.ini 同様、設定ミスを起動時に検出するためfail fastする）。
    """
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("mcp", {}) if isinstance(data, dict) else {}


def _apply_config_overrides(parser: configparser.ConfigParser, path: Path) -> None:
    """管理ツール（admin/ パッケージ）が書き出した config_overrides.json を
    configparser の値へ適用する（config.ini のパース直後、他の全読み込みより
    前に呼ぶこと）。

    JSON の形は ``{"<section>": {"<key>": "<config.ini に書くのと同じ文字列>"}}``
    （admin/overrides.py 参照）。値は必ず文字列として扱う（bool/数値が
    渡ってきた場合は str() で変換する。JSON側の型が揺れても configparser
    自体はどのみち全部文字列として扱うため実害はない）。

    この関数はここで parser の値を書き換えるだけで、以降の型変換・
    バリデーション・環境変数による上書き（load_config() 本体の
    `os.getenv(ENV, section.get(key, default))` 呼び出し群）はすべて
    既存のコードパスがそのまま担う。そのため162キーそれぞれに個別対応する
    必要がない（新しいconfig.iniキーを追加した場合も自動的に上書き対象になる）。

    [admin] セクションは上書き対象から除外する（管理ツール自身の起動設定を
    管理ツールから変更できてしまう循環を避けるため）。ADMIN_SETTING_KEYS（全インスタンス共通の
    キー）も除外する。ADDITIVE_OVERRIDE_KEYS（スキルの登録系のリスト）は置き換えず、
    config.ini の値に無い項目だけを足す（merge_additive_override 参照）。

    Args:
        parser: config.ini を読み込み済みの ConfigParser（このオブジェクトを
            in-place で書き換える）。
        path: config_overrides.json のパス。存在しなければ何もしない
            （管理ツールで一度も保存していない状態が既定であり、これは
            エラーではない）。

    Raises:
        json.JSONDecodeError: JSON構文が不正な場合（config.ini同様、
            設定ミスを起動時に検出するためfail fastする）。
        ValueError: トップレベル、またはセクションの値がdictでない場合。
    """
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the top level must be an object (a dict keyed by section name).")
    for section, section_values in data.items():
        if not isinstance(section_values, dict):
            raise ValueError(f"{path}: the value of section {section!r} must be an object (a dict keyed by key name).")
        if section in _OVERRIDE_EXEMPT_SECTIONS:
            logger.warning(
                "%s: ignoring section [%s] because it cannot be overridden per instance (it holds the admin tool's own settings).",
                path,
                section,
            )
            continue
        if not parser.has_section(section):
            logger.warning(
                "%s: ignoring section [%s] because it does not exist in config.ini (possibly a typo or a removed key).",
                path,
                section,
            )
            continue
        for key, value in section_values.items():
            if not parser.has_option(section, key):
                logger.warning(
                    "%s: ignoring key [%s].%s because it does not exist in config.ini.",
                    path,
                    section,
                    key,
                )
                continue
            if (section, key) in ADMIN_SETTING_KEYS:
                # 管理ツールは設定を何度も読み直すため、同じファイル・キーの警告は1回だけ出す。
                if (str(path), section, key) not in _warned_global_only:
                    _warned_global_only.add((str(path), section, key))
                    logger.warning(
                        "%s: ignoring key [%s].%s because it is shared by all instances (change it on the admin settings screen of the settings dashboard).",
                        path,
                        section,
                        key,
                    )
                continue
            if (section, key) in ADDITIVE_OVERRIDE_KEYS:
                merged, ignored = merge_additive_override(section, key, parser.get(section, key), str(value))
                if ignored and (str(path), section, key) not in _warned_global_only:
                    _warned_global_only.add((str(path), section, key))
                    logger.warning(
                        "%s: ignoring %s in [%s].%s because the config.ini defaults cannot be overridden (only new entries are added).",
                        path,
                        ignored,
                        section,
                        key,
                    )
                parser.set(section, key, merged)
                continue
            parser.set(section, key, str(value))


_CONFIG_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


def expand_config_vars(template: str, config: Config) -> str:
    """テンプレート文字列内の ``${フィールド名}`` を Config の実際の値へ展開する。

    システムプロンプトやツールのdocstringに config.ini の値（例: 反復回数の
    上限）をハードコードせず参照させるための汎用機構。変数名は Config
    dataclass の属性名をそのまま使う（例: ``${subagent_max_iterations}``）。
    属性名は「セクション名_キー名」で一意になるよう命名されているため、
    セクション修飾なしのフラットな名前で衝突しない。

    Args:
        template: ``${変数名}`` を含む文字列。
        config: 値の取得元となる Config インスタンス。

    Returns:
        ``${変数名}`` を実際の値の文字列表現に置き換えた文字列。

    Raises:
        ValueError: テンプレート中に Config に存在しないフィールド名が
            参照されている場合。設定ミス（typo等）を起動時に検出できるよう
            fail-fast する。
    """
    values = {f.name: getattr(config, f.name) for f in fields(config)}

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"Undefined config variable referenced: ${{{name}}}")
        return str(values[name])

    return _CONFIG_VAR_PATTERN.sub(_replace, template)
