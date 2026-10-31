"""スキル発見ミドルウェア（自作部分の中核）。

Agent Skills 標準の progressive disclosure「第1段階 Discovery」を実装する。
仕様: https://agentskills.io/specification

やること（それだけ）:
1. 起動時に skills_dir 配下の各サブフォルダを走査する。
2. 各 SKILL.md の YAML frontmatter（name, description）だけをパース・検証する。
3. 仕様に準拠しない SKILL.md はスキップし警告ログを出す（全体を落とさない）。
4. name + description の一覧をシステムプロンプトへ注入する文字列を組み立てる。

本文（第2段階）や scripts/references/assets（第3段階）はここでは読まない。
それらは tools.py のツール（read_skill / read_skill_file / run_script）が担当する。

賢い仕掛け（動的import・ホットリロード）は入れない。走査して読む、それだけ。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)

# 仕様の name 検証ルール:
#   1〜64文字 / 小文字英数字・ハイフン・アンダースコアのみ / 先頭末尾の区切り文字不可 / 区切り文字の連続不可
# （下記正規表現で「先頭末尾不可」を担保。区切り文字の連続は別途チェック。）
_NAME_RE = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*$")
_NAME_MAX = 64
_DESC_MAX = 1024

# SKILL.md を意図的に持たない共用コードディレクトリ（スキルではない）。
# skills/OFFICE_SKILLS_README.md の「B2. skills/office_shared/ への共用モジュール
# 配置」参照。ここに載っていないディレクトリでSKILL.mdが無いのは設定ミスの
# 可能性が高いためWARNINGのままにする。
_KNOWN_NON_SKILL_DIRS = frozenset({"office_shared"})


@dataclass(frozen=True)
class Skill:
    """発見された 1 つのスキル（frontmatter のメタデータのみ保持）。

    scan_skills() の走査結果として生成される。本文（SKILL.md の
    Markdown 部分）はここでは保持せず、必要時に read_skill ツールが
    skill_md_path から読み直す。

    Attributes:
        name: SKILL.md frontmatter の name（親ディレクトリ名と一致必須）。
        description: SKILL.md frontmatter の description。
        dir_path: スキルのディレクトリパス（skills/<name>/）。
        skill_md_path: SKILL.md 自体のパス（skills/<name>/SKILL.md）。
    """

    name: str
    description: str
    dir_path: Path       # skills/<name>/
    skill_md_path: Path  # skills/<name>/SKILL.md
    has_scripts: bool = False  # scripts/配下に*.pyが1つ以上あるか（run_script実行対象の有無）


def _parse_frontmatter(text: str) -> dict | None:
    """SKILL.md 先頭の `---` で囲まれた YAML frontmatter を dict で返す。

    frontmatter が無い / 壊れている場合は None を返す。

    Args:
        text: SKILL.md ファイルの全文（UTF-8 でデコード済み）。

    Returns:
        frontmatter を YAML パースした dict。以下の場合は None を返す:
        - 先頭が "---" で始まっていない（frontmatter が無い）
        - "---" が2回以上出現しない（frontmatter が閉じられていない）
        - YAML の構文が不正
        - パース結果が dict でない（例: リストやスカラー値）
    """
    # 仕様上 SKILL.md は「YAML frontmatter + Markdown 本文」。先頭が --- で始まる。
    if not text.lstrip().startswith("---"):
        return None
    # 先頭の --- 以降、次の --- までを切り出す。
    stripped = text.lstrip()
    parts = stripped.split("---", 2)
    # parts = ["", "<yaml>", "<body>"] を期待する。
    if len(parts) < 3:
        return None
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        logger.warning("Failed to parse frontmatter YAML: %s", e)
        return None
    return data if isinstance(data, dict) else None


# read_skill の戻り値を囲むタグ。context_trim（切り詰め保護）・context_compaction
# （圧縮後の再添付）が「どのスキルの本文か」をこの開始タグから判定するため、
# 書式は wrap_skill_content()/skill_content_name() の組でのみ扱う。
_SKILL_CONTENT_OPEN_RE = re.compile(r'^<skill_content name="([^"]+)">')


def wrap_skill_content(name: str, body: str) -> str:
    """read_skill が返す本文を `<skill_content name="...">` タグで囲む。"""
    return f'<skill_content name="{name}">\n{body}\n</skill_content>'


def skill_content_name(text: object) -> str | None:
    """wrap_skill_content() で囲まれた文字列ならスキル名を、そうでなければ None を返す。"""
    if not isinstance(text, str):
        return None
    m = _SKILL_CONTENT_OPEN_RE.match(text)
    return m.group(1) if m else None


def strip_frontmatter(text: str) -> str:
    """SKILL.md 全文から YAML frontmatter を除いた本文を返す。

    read_skill が LLM へ返す本文から、Discovery 段階で既に提示済みの
    name/description を含む frontmatter を除いてトークンを節約するために使う。
    区切り方は _parse_frontmatter() と同じ（先頭の `---` から次の `---` まで）。

    Args:
        text: SKILL.md ファイルの全文。

    Returns:
        frontmatter を除き前後の空白を落とした本文。frontmatter が無い・
        閉じられていない場合は text をそのまま返す。
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].strip()


def _validate(name: object, description: object, dir_name: str) -> str | None:
    """name / description を仕様に照らして検証する。

    Agent Skills 仕様の name 検証ルール（1〜64文字、小文字英数字と
    ハイフン・アンダースコアのみ、先頭末尾の区切り文字不可、区切り文字の
    連続不可、親ディレクトリ名と一致必須）と、description の必須・
    文字数上限（1024文字）を検証する。

    Args:
        name: frontmatter から取得した name の生値（型不正を検出するため object）。
        description: frontmatter から取得した description の生値。
        dir_name: このスキルの親ディレクトリ名。name と一致するか検証する。

    Returns:
        検証に失敗した場合はその理由を説明する文字列。
        すべての検証を通過した場合は None。
    """
    if not isinstance(name, str) or not name:
        return "name is missing or not a string"
    if len(name) > _NAME_MAX:
        return f"name exceeds {_NAME_MAX} characters"
    if "--" in name or "__" in name or "-_" in name or "_-" in name:
        return "name contains consecutive separators (- or _)"
    if not _NAME_RE.match(name):
        return "name must contain only lowercase letters, digits, hyphens and underscores, and must not start or end with a separator"
    if name != dir_name:
        return f"name '{name}' does not match the parent directory name '{dir_name}'"
    if not isinstance(description, str) or not description.strip():
        return "description is missing or empty"
    if len(description) > _DESC_MAX:
        return f"description exceeds {_DESC_MAX} characters"
    return None


def _skill_has_scripts(dir_path: Path) -> bool:
    """スキルが run_script/run_script_background の実行対象となる *.py を持つか。

    filter_skills_for_main_agent_guard() / is_skill_directly_runnable() が、
    SKILL.md + references のみ（scriptsを持たない）のスキルを、main_agent_tool_guard
    の allow_entries 登録有無に関わらず常に一覧へ残すために使う判定。
    """
    scripts_dir = dir_path / "scripts"
    if not scripts_dir.is_dir():
        return False
    return any(scripts_dir.glob("*.py"))


def _scan_one(root: Path) -> list[Skill]:
    """1つのディレクトリ直下を走査し、有効な Skill の一覧を返す（内部ヘルパー）。"""
    skills: list[Skill] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            if entry.name in _KNOWN_NON_SKILL_DIRS:
                logger.debug("Skipping because there is no SKILL.md (shared code directory, as expected): %s", entry.name)
            else:
                logger.warning("Skipping because there is no SKILL.md: %s", entry.name)
            continue

        text = skill_md.read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)
        if fm is None:
            logger.warning("Skipping because the frontmatter cannot be read: %s", entry.name)
            continue

        name = fm.get("name")
        description = fm.get("description")
        error = _validate(name, description, entry.name)
        if error:
            logger.warning("Skipping because it violates the spec (%s): %s", entry.name, error)
            continue

        skills.append(
            Skill(
                name=name,
                description=description.strip(),
                dir_path=entry,
                skill_md_path=skill_md,
                has_scripts=_skill_has_scripts(entry),
            )
        )
        logger.info("Discovered skill: %s", name)

    return skills


def scan_skills(skills_dirs: Path | str | Sequence[Path | str]) -> list[Skill]:
    """skills_dirs 配下を走査し、有効な Skill の一覧を返す。

    Agent Skills 標準の progressive disclosure「第1段階 Discovery」の実装。
    各ディレクトリ直下の各サブディレクトリについて SKILL.md の存在確認・
    frontmatter パース・仕様検証を行う。個々のスキルが仕様に準拠しない
    場合でも例外は送出せず、そのスキルのみスキップして走査を継続する
    （1つの不正なスキルが全体の起動を妨げないようにするため）。

    複数ディレクトリを渡した場合は渡した順に走査し、name をキーにマージする。
    同名スキルが複数ディレクトリに存在する場合は、後から走査したディレクトリの
    定義で上書きする（例: [skills_dir, *locohane_skills_dirs] の順で渡すと
    .locohane 側が優先される）。

    Args:
        skills_dirs: スキル群を格納するディレクトリのパス、またはその並び。

    Returns:
        検証を通過した Skill のリスト（ディレクトリ名の昇順）。
        いずれのディレクトリも存在しない場合や、有効なスキルが1つも
        見つからない場合は空リストを返す。
    """
    if isinstance(skills_dirs, (str, Path)):
        skills_dirs = [skills_dirs]

    merged: dict[str, Skill] = {}
    for skills_dir in skills_dirs:
        root = Path(skills_dir)
        if not root.is_dir():
            logger.debug("skills directory does not exist (optional): %s", root)
            continue
        for skill in _scan_one(root):
            if skill.name in merged:
                logger.info("Skill '%s' overridden by the definition in %s", skill.name, root)
            merged[skill.name] = skill

    return sorted(merged.values(), key=lambda s: s.name)


def render_skills_block(skills: list[Skill]) -> str:
    """スキル一覧を `{{skills}}` プレースホルダー用の箇条書きテキストに整形する。

    build_system_prompt() のほか、agent_types.py（サブエージェント種別の
    システムプロンプトにも同じ `{{skills}}` プレースホルダーがある）からも
    共有で使う。

    Args:
        skills: scan_skills() が返した有効な Skill のリスト。

    Returns:
        "- name: description" 形式の箇条書き。skills が空リストの場合は
        「利用可能なスキルはありません」という旨の文言を返す。
    """
    if skills:
        return "\n".join(f"- {s.name}: {s.description}" for s in skills)
    return "(no skills available)"


def is_skill_directly_runnable(skill: Skill, config: "Config") -> bool:
    """メインエージェントがそのスキルを{{skills}}一覧上「直接扱える」とみなすか。

    [main_agent_tool_guard].allow_entries に [skill_name, script_filename] の
    ペアが1件も max_calls≠0 で登録されていなければ False（未登録は常に除外する
    ホワイトリスト方式）。

    scripts/にrun_script実行対象の*.pyを持つスキル（skill.has_scripts=True）は、
    script_filename に実在のファイル名を登録する（run_script/run_script_background
    を直接呼んでも1件も許可されていなければ src/tools/tool_node.py の
    _guard_main_agent_tool_limit に常に拒否される）。

    scripts/を持たないスキル（skill.has_scripts=False、SKILL.md + references
    のみ等）は run_script の対象自体が無いため、script_filename に空文字列
    "" を指定した [skill_name, ""] のダミーエントリで登録する。空文字列は
    run_script/run_script_background の実際の args.script_filename とは
    絶対に一致しないため、このエントリは {{skills}} 一覧への表示可否のみに
    効き、実行許可には影響しない（filter_main_agent_tools() の
    run_script_allowed 判定も同様に空文字列エントリを無視する）。

    filter_skills_for_main_agent_guard() / render_skills_block_with_hint() の
    双方が本関数の判定結果を使う。

    Args:
        skill: 判定対象の Skill。
        config: main_agent_tool_guard_allow_entries を持つ Config。

    Returns:
        {{skills}}一覧上「直接扱える」とみなすなら True。
    """
    # skills_dir 配下の skill-creator 本体は allow_entries の登録に関係なく常に直接扱える
    # （src/skill_drafts.py の is_guard_exempt_script。ドラフトは別枠の一覧で示す）。
    skills_dir = getattr(config, "skills_dir", None)
    if skill.name == "skill-creator" and skills_dir is not None and skill.dir_path.resolve() == (skills_dir / skill.name).resolve():
        return True
    allowed = {
        key[0]
        for key, max_calls in config.main_agent_tool_guard_allow_entries
        if isinstance(key, tuple) and max_calls != 0
    }
    return skill.name in allowed


def filter_skills_for_main_agent_guard(skills: list[Skill], config: "Config") -> list[Skill]:
    """[main_agent_tool_guard] mode!="false" かつ visibility_mode=strict 時、
    メインエージェントの `{{skills}}` へ載せるスキルを、直接実行が許可された
    ものだけに絞り込む。

    scriptsを持つのに [skill_name, script_filename] ペアが max_calls≠0 で
    登録されていないスキルは、メインエージェントが run_script/
    run_script_background を直接呼んでも常に拒否される
    （src/tools/tool_node.py の _guard_main_agent_tool_limit 参照）。それでも
    `{{skills}}` へ全件載せたままだと「実行できる」と誤認して試み、拒否→
    dispatch_agentへの再委譲を促されるだけの無駄な往復が発生する
    （src/tools/tool_node.py の filter_main_agent_tools と同じ理由づけ）。
    scriptsを持たないスキルも、allow_entries に [skill_name, ""] のダミー
    エントリを登録していなければ同様に除外する（is_skill_directly_runnable参照）。

    dispatch_agent配下のサブエージェントには本ガードと無関係な `{{skills}}`
    が渡る（app.py の agent_type_defs 差し込み参照。agent_type_run_script_allowlist
    登録があればそのスキルのみ、filter_skills_for_agent_type参照）ため、ここで
    絞ってもスキル自体へアクセスする手段が失われるわけではない。

    Args:
        skills: scan_skills() が返した有効な Skill のリスト。
        config: main_agent_tool_guard_mode / main_agent_tool_guard_allow_entries
            を持つ Config。

    Returns:
        mode="false" 時は skills をそのまま返す。それ以外は
        is_skill_directly_runnable() が True のスキルのみに絞ったリスト
        （tools_skills_only はMCP動的ツールのみに関わる設定でスキル自体の
        許可判定には影響しないため、"all" と同じ扱いになる）。
    """
    if config.main_agent_tool_guard_mode == "false":
        return skills
    return [s for s in skills if is_skill_directly_runnable(s, config)]


def filter_skills_for_agent_type(
    skills: list[Skill],
    agent_type: str,
    entries: Iterable[tuple[str, str | tuple[str, str]]],
) -> list[Skill]:
    """サブエージェント（agents/*.md）の `{{skills}}` へ載せるスキルを、
    [subagent].agent_type_run_script_allowlist に登録されたものだけに絞り込む。

    その agent_type のエントリが1件でもあれば、run_script で呼べない
    スキルを一覧に載せても拒否されるだけなので、対象スキル（文字列指定は
    そのスキル、[スキル名, スクリプト名] 指定はそのスキル名）だけを残す。
    エントリが1件も無い agent_type は制限なしのため skills をそのまま返す
    （render_agent_type_run_script_allowlist_block と同じ扱い）。

    Args:
        skills: scan_skills() が返した有効な Skill のリスト。
        agent_type: 差し込み先の agents/*.md の frontmatter name。
        entries: config.subagent_agent_type_run_script_allowlist（全 agent_type 分）。

    Returns:
        絞り込み後のスキルリスト（元の並び順を保つ）。
    """
    allowed = {target[0] if isinstance(target, tuple) else target for a, target in entries if a == agent_type}
    if not allowed:
        return skills
    return [s for s in skills if s.name in allowed]


def render_skills_block_with_guard_annotation(skills: list[Skill], config: "Config") -> str:
    """[main_agent_tool_guard] visibility_mode=all 用のスキル一覧を組み立てる。

    render_skills_block_with_hint() と異なり description は書き換えず、
    is_skill_directly_runnable() が False のスキルのみ description の末尾に
    「（直接実行不可。実行はdispatch_agent へ委譲。）」を追記する。

    Args:
        skills: scan_skills() が返した有効な Skill のリスト（フィルタ前）。
        config: is_skill_directly_runnable() が必要とする Config。

    Returns:
        "- name: description" 形式の箇条書き（実行不可分は末尾に注記を追加）。
        skills が空リストの場合は「利用可能なスキルはありません」という
        旨の文言を返す。
    """
    if not skills:
        return "(no skills available)"
    lines = []
    for s in skills:
        if is_skill_directly_runnable(s, config):
            lines.append(f"- {s.name}: {s.description}")
        else:
            lines.append(f"- {s.name}: {s.description} (cannot be run directly; delegate running it to dispatch_agent.)")
    return "\n".join(lines)


def render_skills_block_with_hint(skills: list[Skill], config: "Config") -> str:
    """[main_agent_tool_guard] visibility_mode=hint 用のスキル一覧を組み立てる。

    render_skills_block() と異なり絞り込みは行わず全スキル名を列挙するが、
    is_skill_directly_runnable() が False のスキルは description を
    固定文言に差し替え、直接実行できないこと・dispatch_agentへの委譲が
    必要なことを示す。

    Args:
        skills: scan_skills() が返した有効な Skill のリスト（フィルタ前）。
        config: is_skill_directly_runnable() が必要とする Config。

    Returns:
        "- name: description" 形式の箇条書き（実行不可分は description を
        固定文言に差し替え）。skills が空リストの場合は「利用可能なスキルは
        ありません」という旨の文言を返す。
    """
    if not skills:
        return "(no skills available)"
    lines = []
    for s in skills:
        if is_skill_directly_runnable(s, config):
            lines.append(f"- {s.name}: {s.description}")
        else:
            lines.append(f"- {s.name}: cannot be run directly. Delegate checking details of and running this skill to dispatch_agent")
    return "\n".join(lines)


def build_system_prompt_from_block(skills_block: str, template_path: Path | str) -> str:
    """テンプレートファイルの `{{skills}}` を、組み立て済みのスキル一覧文字列で置換する。

    render_skills_block() / render_skills_block_with_hint() のどちらで
    組み立てたブロックでも渡せるよう、build_system_prompt() から
    ブロック組み立て部分を分離したもの。

    Args:
        skills_block: render_skills_block() 等で組み立て済みのスキル一覧文字列。
        template_path: `{{skills}}` プレースホルダーを含むシステムプロンプトの
            テンプレートファイルのパス。

    Returns:
        LLM に渡すシステムプロンプト全文（複数行の文字列）。

    Raises:
        FileNotFoundError: template_path が存在しない場合。
    """
    template = Path(template_path).read_text(encoding="utf-8")
    return template.replace("{{skills}}", skills_block)


def build_system_prompt(skills: list[Skill], template_path: Path | str) -> str:
    """テンプレートファイルにスキル一覧を差し込んでシステムプロンプトを組み立てる。

    固定文言（役割説明・progressive disclosure の使い方指示）は
    template_path のファイルに保守しやすい形で分離されている。
    このファイル内の `{{skills}}` プレースホルダーを、発見したスキルの
    name+description 一覧（第1段階 Discovery）で置換する。

    Args:
        skills: scan_skills() が返した有効な Skill のリスト。
            空リストの場合は「利用可能なスキルはありません」という
            旨の文言が差し込まれる。
        template_path: `{{skills}}` プレースホルダーを含むシステムプロンプトの
            テンプレートファイルのパス。

    Returns:
        LLM に渡すシステムプロンプト全文（複数行の文字列）。

    Raises:
        FileNotFoundError: template_path が存在しない場合。
    """
    return build_system_prompt_from_block(render_skills_block(skills), template_path)
