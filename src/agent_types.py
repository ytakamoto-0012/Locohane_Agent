"""dispatch_agent 用のエージェント種別定義（skills.py と同じ設計パターン）。

ClaudeCode の `.claude/agents/*.md` 相当。agents_dir 配下の各 `*.md` を
1ファイル=1種別として走査し、YAML frontmatter（name, description, 任意で
tools・model）をパース・検証する。仕様に準拠しないファイルは例外を投げず
ログ警告してスキップし、全体は落とさない（scan_skills() と同方針）。

tools.py はここから意図的に import しない（BaseTool への解決は tools.py
側の責務。循環import回避、subagent.py と同じ設計方針）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# skills.py の Skill.name と同じ検証ルールを踏襲する。
_NAME_RE = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*$")
_NAME_MAX = 64
_DESC_MAX = 1024


@dataclass(frozen=True)
class AgentType:
    """発見された1つのエージェント種別（frontmatter＋本文）。

    Attributes:
        name: frontmatter の name（ファイル名の stem と一致必須）。
        description: frontmatter の description。
        tool_names: frontmatter の tools（カンマ区切り文字列 or YAMLリストを
            正規化した list[str]）。省略時は None（呼び出し側が既定の
            ツール一式を継承させる）。
        system_prompt: frontmatter を除いた本文（`{{skills}}` は未置換のまま）。
        model: frontmatter の model（Anthropic公式のサブエージェント仕様の
            model 相当）。[llm].sub_url の接続先の model 名を書くと、その
            モデルの接続先だけから sub_routing_strategy に従って選ぶ。
            一致する接続先が無い場合は無視される（src/llm/routing.py の
            _select_endpoint 参照）。省略時は None。
    """

    name: str
    description: str
    tool_names: list[str] | None
    system_prompt: str
    model: str | None = None


def _parse_frontmatter(text: str) -> dict | None:
    """ファイル先頭の `---` で囲まれた YAML frontmatter を dict で返す。

    skills.py の _parse_frontmatter と同一ロジック（意図的な複製、
    このプロジェクトでは frontmatter パーサーをモジュールごとに複製する
    のが既存パターン）。

    Args:
        text: エージェント定義ファイルの全文（UTF-8 でデコード済み）。

    Returns:
        frontmatter を YAML パースした dict。frontmatter が無い/壊れて
        いる/dict でない場合は None。
    """
    if not text.lstrip().startswith("---"):
        return None
    stripped = text.lstrip()
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        logger.warning("Failed to parse frontmatter YAML: %s", e)
        return None
    return (data, parts[2]) if isinstance(data, dict) else None


def _parse_tools_field(raw: object) -> list[str] | None:
    """frontmatter の tools フィールドを list[str] に正規化する。

    Anthropic公式のサブエージェント仕様に合わせ、カンマ区切り文字列
    （例: "read_skill, analyze_image"）を主形式として想定するが、YAMLの
    リスト形式（例: ["read_skill", "analyze_image"]）も許容する。

    Args:
        raw: frontmatter から取得した tools の生値。省略時は None。

    Returns:
        正規化されたツール名のリスト。raw が None ならそのまま None
        （既定ツール一式を継承する合図）。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return None


def _validate(name: object, description: object, file_stem: str) -> str | None:
    """name / description を検証する（skills.py の _validate と同じルール）。

    Args:
        name: frontmatter から取得した name の生値。
        description: frontmatter から取得した description の生値。
        file_stem: このエージェント定義ファイルの拡張子抜きファイル名。
            name と一致するか検証する。

    Returns:
        検証に失敗した場合はその理由。すべて通過した場合は None。
    """
    if not isinstance(name, str) or not name:
        return "name is missing or not a string"
    if len(name) > _NAME_MAX:
        return f"name exceeds {_NAME_MAX} characters"
    if "--" in name or "__" in name or "-_" in name or "_-" in name:
        return "name contains consecutive separators (- or _)"
    if not _NAME_RE.match(name):
        return "name must contain only lowercase letters, digits, hyphens and underscores, and must not start or end with a separator"
    if name != file_stem:
        return f"name '{name}' does not match the file name '{file_stem}'"
    if not isinstance(description, str) or not description.strip():
        return "description is missing or empty"
    if len(description) > _DESC_MAX:
        return f"description exceeds {_DESC_MAX} characters"
    return None


def _parse_model_field(value: object) -> str | None:
    """frontmatter の model を正規化する（未指定・空文字・"inherit" は None）。

    "inherit" は Anthropic公式仕様の「メインと同じモデル」の指定。ClaudeCode の
    定義ファイルをそのまま持ち込んだ場合に、存在しないモデル名として毎回
    警告ログを出さないよう、指定なし（通常のルーティング）として扱う。
    """
    if value is None:
        return None
    model = str(value).strip()
    if not model or model.casefold() == "inherit":
        return None
    return model


def _scan_one(root: Path) -> list[AgentType]:
    """1つのディレクトリ直下の `*.md` を走査し、有効な AgentType の一覧を返す（内部ヘルパー）。"""
    agent_types: list[AgentType] = []
    for entry in sorted(root.glob("*.md")):
        text = entry.read_text(encoding="utf-8")
        parsed = _parse_frontmatter(text)
        if parsed is None:
            logger.warning("Skipping because the frontmatter cannot be read: %s", entry.name)
            continue
        fm, body = parsed

        name = fm.get("name")
        description = fm.get("description")
        error = _validate(name, description, entry.stem)
        if error:
            logger.warning("Skipping because it violates the spec (%s): %s", entry.name, error)
            continue

        agent_types.append(
            AgentType(
                name=name,
                description=description.strip(),
                tool_names=_parse_tools_field(fm.get("tools")),
                system_prompt=body.strip() + "\n",
                model=_parse_model_field(fm.get("model")),
            )
        )
        logger.info("Discovered agent type: %s", name)

    return agent_types


def scan_agent_types(agents_dirs: Path | str | Sequence[Path | str]) -> list[AgentType]:
    """agents_dirs 配下の `*.md` を走査し、有効な AgentType の一覧を返す。

    個々の定義が仕様に準拠しない場合でも例外は送出せず、そのファイルのみ
    スキップして走査を継続する（1つの不正な定義が全体の起動を妨げない
    ようにするため、scan_skills() と同方針）。

    複数ディレクトリを渡した場合は渡した順に走査し、name をキーにマージする。
    同名の定義が複数ディレクトリに存在する場合は、後から走査したディレクトリの
    定義で上書きする（例: [agents_dir, *locohane_agents_dirs] の順で渡すと
    .locohane 側が優先される）。

    Args:
        agents_dirs: エージェント種別定義（*.md）を格納するディレクトリ、
            またはその並び。

    Returns:
        検証を通過した AgentType のリスト（name の昇順）。
        いずれのディレクトリも存在しない場合や、有効な定義が1つも無い場合は
        空リストを返す。
    """
    if isinstance(agents_dirs, (str, Path)):
        agents_dirs = [agents_dirs]

    merged: dict[str, AgentType] = {}
    for agents_dir in agents_dirs:
        root = Path(agents_dir)
        if not root.is_dir():
            logger.debug("agents directory does not exist (optional): %s", root)
            continue
        for agent_type in _scan_one(root):
            if agent_type.name in merged:
                logger.info("Agent type '%s' overridden by the definition in %s", agent_type.name, root)
            merged[agent_type.name] = agent_type

    return sorted(merged.values(), key=lambda a: a.name)


def render_agent_types_block(agent_types: list[AgentType]) -> str:
    """エージェント種別一覧を `{{agent_types}}` プレースホルダー用に整形する。

    Args:
        agent_types: scan_agent_types() が返した AgentType のリスト。

    Returns:
        "- name: description" の下に使用可能ツール一覧を添えた箇条書き。
        メインエージェントが委譲先のツール構成を知らずに試し呼びしてトークンを
        浪費しないよう、frontmatter の tools も併記する（省略時は既定ツール一式を
        継承する旨を記す、_resolve_agent_types() 参照）。空リストの場合は
        「利用可能なエージェント種別はありません」という旨の文言を返す。
    """
    if agent_types:
        lines = []
        for a in agent_types:
            tools = ", ".join(a.tool_names) if a.tool_names is not None else "(the default tool set for sub-agents)"
            lines.append(f"- {a.name}: {a.description}\n  Available tools: {tools}")
        return "\n".join(lines)
    return "(no agent types available)"
