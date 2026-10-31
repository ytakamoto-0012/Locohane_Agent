"""config.ini の読み取り専用パーサー。

configparser は使わない。configparser で読み込んで書き戻すと、コメント・
空行・書式がすべて失われる（configparser はコメントを保持しない）ため、
「キー一覧・既定値・直前のコメントを説明文として取り出す」目的に特化した
行ベースの簡易パーサーをここに実装する。

このモジュールは config.ini を**書き換えない**（管理ツールは config.ini を
既定値として扱い、変更は instances/<name>/config_overrides.json に記録する。
admin/overrides.py 参照）。そのため、ここでは「読んで一覧化する」ことだけに
責務を絞る。

configparser の継続行（値の1行目の次の行以降、行頭が空白/タブで始まる行は
前のキーの値の続き）と同じ規則で複数行値を検出する。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# セクションヘッダ: "[section]"（前後の空白は許容、行末は改行のみ）。
_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")
# キー行: 行頭が空白でなく、識別子っぽい名前 = 値、の形。
# config.ini のキー名は英数字・アンダースコア・ドットのみ（例: context_trim.subagent
# はセクション名であり、キー名自体にドットを含む例は無いが、将来の拡張に備え許容する）。
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*)$")
# グループ見出しコメント: "# --- 見出しテキスト ---" 形式。
_HEADING_RE = re.compile(r"^#\s*-{2,}\s*(.+?)\s*-{2,}\s*$")
# 通常のコメント行（行頭が # で始まる行。config.ini は全コメントを行頭=カラム0に
# 置く方針で統一されているため、行頭空白は許容しない）。
_COMMENT_RE = re.compile(r"^#(.*)$")


@dataclass(frozen=True)
class KeyInfo:
    """config.ini 内の1キーの情報。"""

    section: str
    key: str
    # 値（"=" の右側）。複数行の場合は "\n" 区切りで、2行目以降は元のインデントを
    # そのまま含む（例: "[\n    {...},\n    ]"）。
    default_value: str
    # 直前に連続していたコメント行を "\n" 区切りで連結した説明文
    # （各行頭の "# " は取り除き済み）。無ければ空文字列。
    description: str
    # このキーが属するグループ見出し（直近の "# --- xxx ---" 行の xxx 部分）。
    # 見出しが一度も出ていない場合は None。
    group_heading: str | None
    # フォームでの表現形の推定: "bool" | "choice" | "list" | "multiline" | "text"。
    ui_kind: str
    # ui_kind="choice" のときの選択肢（表示順）。空文字列は「未指定（空欄）」を表す。
    choices: tuple[str, ...] = ()
    # ui_kind="list" のとき、値の構造（下記 _KEY_SCHEMAS 参照）。None なら
    # 要素の型を自由に編集する汎用エディタで表示する。
    # （dict はハッシュ不可のため hash の対象から外す）
    schema: dict[str, Any] | None = field(default=None, hash=False)


# 固有キーワードのいずれかしか受け付けないキーの選択肢（表示順）。
# 値の集合は src/config.py の各定数（LLM_ROUTING_STRATEGIES 等）と一致させること
# （tests/test_admin_ini_catalog.py で突き合わせている）。frozenset は順序を
# 持たないため、UIでの表示順はここで決める。先頭の "" は「空欄＝未指定」を
# 許容するキー（src/config.py 側で None 扱いになるもの）にだけ置く。
_ROUTING_STRATEGY_CHOICES = ("round_robin", "random", "priority_failover")
# src/config.py の THINKING_LEVELS と同じ値・順序。
_THINKING_LEVEL_CHOICES = ("off", "low", "medium", "high", "xhigh")
_KEY_CHOICES: dict[tuple[str, str], tuple[str, ...]] = {
    ("llm", "main_routing_strategy"): _ROUTING_STRATEGY_CHOICES,
    ("llm", "sub_routing_strategy"): _ROUTING_STRATEGY_CHOICES,
    ("vector_db", "embedding_routing_strategy"): _ROUTING_STRATEGY_CHOICES,
    ("llm", "reasoning_format"): ("", "none", "deepseek", "deepseek-legacy"),
    ("llm", "reasoning_effort"): ("", "none", "default", "minimal", "low", "medium", "high", "xhigh", "max"),
    ("main_agent_tool_guard", "mode"): ("false", "tools_skills_only", "all"),
    ("main_agent_tool_guard", "visibility_mode"): ("strict", "hint", "all"),
    ("skill_creator", "other_users_drafts"): ("hidden", "listed", "readable", "full"),
    ("log", "level"): ("info", "debug", "none"),
    # src/config.py の UI_DEFAULT_LANGUAGES と同じ値。
    ("ui", "default_language"): ("auto", "ja", "en"),
    # src/config.py の VECTOR_DB_BACKENDS と同じ値・順序。
    ("vector_db", "backend"): ("sqlite_vec", "qdrant"),
    # src/config.py の VECTOR_DB_DISTANCES と同じ値・順序。
    ("vector_db", "distance"): ("Cosine", "Euclid", "Dot", "Manhattan"),
    # src/config.py の VECTOR_DB_QDRANT_PRESETS と同じ値・順序。
    ("vector_db", "qdrant_preset"): ("none", "1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m", "10m"),
    ("vector_db", "embedding_encoding_format"): ("base64", "float"),
    ("thinking_loop_guard", "target"): ("all", "content_only", "thinking_only"),
    ("thinking_control", "default_level"): _THINKING_LEVEL_CHOICES,
    # rule_* は false＝ルール無効。
    ("thinking_control", "rule_user_turn"): ("false", *_THINKING_LEVEL_CHOICES),
    ("thinking_control", "rule_tool_error"): ("false", *_THINKING_LEVEL_CHOICES),
    ("thinking_control", "rule_consecutive_cap"): ("false", *_THINKING_LEVEL_CHOICES),
    ("thinking_control", "rule_after_tools"): ("false", *_THINKING_LEVEL_CHOICES),
}


# リスト値の構造定義。管理画面で「文字/数値」等の型を選ばせず、意味のある
# 名前付きの入力欄で編集させるために使う（admin/static/admin.js の
# buildSchemaEditor が解釈する）。形式は src/config.py の各パーサー
# （_as_llm_endpoints / _parse_plan_approval_exempt_scripts 等）に合わせること。
#   {"type": "string", "placeholder": ..., "default": 追加時の初期値（省略時は ""）}
#   {"type": "number", "placeholder": ..., "default": 追加時の初期値（省略時は 0）}
#   {"type": "choice", "choices": [...]}
#   {"type": "list", "item": <schema>}              要素を追加/削除できるリスト
#   {"type": "tuple", "items": [{"label", "schema"}]} 位置で意味が決まる固定長リスト
#   {"type": "dict", "fields": [{"name", "schema", "optional"}]}
#       optional=True のフィールドは空欄なら辞書から取り除く
#   {"type": "oneof", "variants": [{"label", "schema"}]} いずれかの形を選ぶ
#   {"type": "grouped", "group_label", "group_placeholder", "group_source", "item": <schema>}
#       [[グループ名, item], ...] のリスト。画面ではグループ名ごとのパネルに
#       まとめて表示し、保存時は同じ形のリストへ戻す（グループ順に並び替わる
#       ため、要素の順序に意味が無い集合的なキーにだけ使う）
# string の "source"（grouped は "group_source"）を付けた欄は、そのインスタンスが認識している
# 候補（admin/server.py の /config/candidates）から選ばせる:
#   "skill"      スキル名
#   "script"     スクリプトファイル名（同じ tuple 内の "skill" 欄で選んだスキルの scripts/*.py）
#   "agent_type" サブエージェント（agents/*.md）の名前
#   "tool"       メインエージェントのビルトインツール名（MCP の動的ツールは手入力）
def _str(placeholder: str = "", source: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "placeholder": placeholder}
    if source:
        schema["source"] = source
    return schema


_SKILL_SCRIPT = {
    "type": "tuple",
    "items": [
        {"label": "スキル名", "schema": _str("例: excel-read", "skill")},
        {"label": "スクリプトファイル名", "schema": _str("例: read_excel.py", "script")},
    ],
}
# ビルトインツール名、または [スキル名, スクリプトファイル名]
# （[main_agent_tool_guard].allow_entries / [default_workdir].allow_sandbox_dir の対象）。
_TOOL_OR_SKILL_SCRIPT = {
    "type": "oneof",
    "variants": [
        {"label": "ツール名", "schema": _str("例: dispatch_agent", "tool")},
        {"label": "スキルのスクリプト", "schema": _SKILL_SCRIPT},
    ],
}
_LLM_URL = {
    "type": "list",
    "item": {
        "type": "dict",
        "fields": [
            {"name": "base_url", "schema": _str("例: http://localhost:8080/v1")},
            {"name": "api_key", "schema": {**_str("dummy-not-used"), "default": "dummy-not-used"}},
            {"name": "model", "schema": _str("llama-server の --alias と同じ名前")},
            {"name": "provider", "schema": {"type": "choice", "choices": ["openai_compatible", "llama_cpp", "vllm"]}, "optional": True},
            {"name": "start", "schema": {"type": "number", "placeholder": "使用開始時刻 0〜24（常時使用なら空欄）"}, "optional": True},
            {"name": "end", "schema": {"type": "number", "placeholder": "使用終了時刻 0〜24（常時使用なら空欄）"}, "optional": True},
        ],
    },
}
# [vector_db].embedding_url。接続先ごとにベクターが変わらないよう start/end は持たない
# （src/config.py の _as_embedding_endpoints 参照）。provider は空きの確かめ方に使う
# （src/vector_db/embeddings.py の _probe_load 参照）。
_EMBEDDING_URL = {
    "type": "list",
    "item": {
        "type": "dict",
        "fields": [
            {"name": "base_url", "schema": _str("例: http://localhost:12440/v1")},
            {"name": "api_key", "schema": {**_str("dummy-not-used"), "default": "dummy-not-used"}},
            {"name": "model", "schema": _str("埋め込みモデル名（全接続先で同じにする）")},
            {"name": "provider", "schema": {"type": "choice", "choices": ["openai_compatible", "llama_cpp", "vllm"]}, "optional": True},
        ],
    },
}
_KEY_SCHEMAS: dict[tuple[str, str], dict[str, Any]] = {
    ("llm", "main_url"): _LLM_URL,
    ("llm", "sub_url"): _LLM_URL,
    ("vector_db", "embedding_url"): _EMBEDDING_URL,
    ("plan", "plan_approval_exempt_scripts"): {"type": "list", "item": _SKILL_SCRIPT},
    ("subagent", "agent_type_run_script_allowlist"): {
        "type": "grouped",
        "group_label": "agent_type",
        "group_placeholder": "例: explore",
        "group_source": "agent_type",
        "item": {
            "type": "oneof",
            "variants": [
                {"label": "スキル全体", "schema": _str("スキル名", "skill")},
                {"label": "スクリプト指定", "schema": _SKILL_SCRIPT},
            ],
        },
    },
    ("main_agent_tool_guard", "allow_entries"): {
        "type": "list",
        "item": {
            "type": "tuple",
            "items": [
                {"label": "対象", "schema": _TOOL_OR_SKILL_SCRIPT},
                {"label": "max_calls（-1=無制限）", "schema": {"type": "number", "placeholder": "-1", "default": -1}},
            ],
        },
    },
    # 文字列（ディレクトリのみ）、または {"dir", "python"} の辞書
    # （src/config.py の _parse_project_locohane_dirs 参照）。
    ("paths", "project_locohane_dir"): {
        "type": "list",
        "item": {
            "type": "oneof",
            "variants": [
                {"label": "ディレクトリのみ", "schema": _str("例: ./.locohane")},
                {
                    "label": "専用Python環境あり",
                    "schema": {
                        "type": "dict",
                        "fields": [
                            {"name": "dir", "schema": _str("例: ./.locohane_team")},
                            {"name": "python", "schema": _str("例: ./.locohane_team/.venv/Scripts/python.exe"), "optional": True},
                        ],
                    },
                },
            ],
        },
    },
    ("default_workdir", "allow_sandbox_dir"): {
        "type": "list",
        "item": {
            "type": "dict",
            "fields": [
                {"name": "dir", "schema": _str("例: E:/shared_output")},
                {"name": "allow_entries", "schema": {"type": "list", "item": _TOOL_OR_SKILL_SCRIPT}},
            ],
        },
    },
}


@dataclass(frozen=True)
class IniCatalog:
    """config.ini 全体のパース結果。"""

    keys: tuple[KeyInfo, ...]
    # ファイルの最終更新時刻（管理ツールが config.ini 自体の変更を検知する用途。
    # config.ini 自体は書き換えないが、ユーザーが手動編集した場合に備える）。
    mtime: float


def _infer_ui_kind(section: str, key: str, value: str) -> str:
    """値の見た目から、フォームでの入力欄の種類を推定する。

    最終的な妥当性判定は src.config.load_config() 側のバリデーションに委ねる
    （ここでの推定はあくまでUI表示のヒント）。

    "list" は src/config.py と同じく ast.literal_eval でリストとして解釈できる
    値に限る（"[**システム通知: ...**]" のように "[" で始まるだけの文字列は
    "multiline" のまま）。
    """
    if (section, key) in _KEY_CHOICES:
        return "choice"
    stripped = value.strip()
    if stripped.startswith("["):
        try:
            if isinstance(ast.literal_eval(stripped), list):
                return "list"
        except (ValueError, SyntaxError):
            pass
    if "\n" in value or stripped.startswith("[") or stripped.startswith("{"):
        return "multiline"
    if stripped.lower() in ("true", "false"):
        return "bool"
    return "text"


def parse(text: str) -> IniCatalog:
    """config.ini のテキストを解析し、キー一覧を返す。

    Args:
        text: config.ini の全文（改行コードは "\n" 前提。呼び出し元で
            universal newlines 読み込み（open(..., newline=None) 相当）を
            済ませておくこと）。

    Returns:
        IniCatalog。mtime は呼び出し元が別途セットする想定のため常に 0.0。
    """
    lines = text.split("\n")
    n = len(lines)
    keys: list[KeyInfo] = []
    section: str | None = None
    current_heading: str | None = None
    pending_comment: list[str] = []

    i = 0
    while i < n:
        line = lines[i]

        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1)
            current_heading = None
            pending_comment = []
            i += 1
            continue

        comment_match = _COMMENT_RE.match(line)
        if comment_match:
            heading_match = _HEADING_RE.match(line)
            if heading_match:
                current_heading = heading_match.group(1)
                pending_comment = []
            else:
                pending_comment.append(comment_match.group(1).strip())
            i += 1
            continue

        if not line.strip():
            # 空行はコメントブロックとキーの結び付きを断つ（次のキーには
            # この空行より前のコメントを説明文として付けない）。
            pending_comment = []
            i += 1
            continue

        key_match = _KEY_RE.match(line) if section is not None else None
        if key_match:
            key = key_match.group(1)
            value_lines = [key_match.group(2)]
            j = i + 1
            while j < n and lines[j].strip() and lines[j][:1] in (" ", "\t"):
                value_lines.append(lines[j])
                j += 1
            value = "\n".join(value_lines)
            keys.append(
                KeyInfo(
                    section=section,
                    key=key,
                    default_value=value,
                    description="\n".join(pending_comment).strip(),
                    group_heading=current_heading,
                    ui_kind=_infer_ui_kind(section, key, value),
                    choices=_KEY_CHOICES.get((section, key), ()),
                    schema=_KEY_SCHEMAS.get((section, key)),
                )
            )
            pending_comment = []
            i = j
            continue

        # 未対応の行（キーでもコメントでもセクションでもない。通常は
        # 発生しないが、壊れた行があってもパース全体を止めない）。
        pending_comment = []
        i += 1

    return IniCatalog(keys=tuple(keys), mtime=0.0)


def parse_file(path: Path) -> IniCatalog:
    """config.ini ファイルを読んで parse() する。mtime も埋める。"""
    text = path.read_text(encoding="utf-8")
    catalog = parse(text)
    return IniCatalog(keys=catalog.keys, mtime=path.stat().st_mtime)


def find_key(catalog: IniCatalog, section: str, key: str) -> KeyInfo | None:
    """指定した [section].key の KeyInfo を返す（無ければ None）。"""
    for info in catalog.keys:
        if info.section == section and info.key == key:
            return info
    return None
