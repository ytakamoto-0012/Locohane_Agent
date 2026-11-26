"""メインエージェントとサブエージェント（agents/*.md）のシステムプロンプトを組み立てる。

本番（app.py の _setup / _rebuild_graph）と評価（evals/run_case.py）が同じ関数を使い、評価のプロンプトが
本番とずれないようにする。以前は2か所で別々に組み立てており、評価では {{project_instructions}}
（LOCOHANE.md の会社・インスタンス専用の指示）・{{plan_approval_exempt_scripts}}・{{main_agent_blocked_tools_hint}}
が差し込まれずに残り、スキル一覧も [main_agent_tool_guard] visibility_mode を無視して全スキルの説明を
そのまま見せていた（2026-11-13 ユーザー指摘「評価ループ中の使用可能なスキルは本番と同じか」）。

組み立ては2段階:
1. build_prompts(): 起動時に1回（評価ではケースごとに1回）。ユーザーごとに変わる部分（ドラフトスキルの一覧・
   システムプロンプトに表示するメモリー）は目印（DRAFT_SKILLS_MARKER・MEMORY_INDEX_MARKER）のまま残す。
2. fill_session_parts(): 会話（グラフ）を組み立てるたび。目印をそのユーザー向けの内容に差し替え、
   ナレッジのコレクション一覧を差し込む。応答言語の指示（src/i18n.py の response_language_directive）は呼び出し側が末尾に足す。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

# メインエージェントが直接呼べないビルトインツールの案内（visibility_mode が all / hint のとき）
_BLOCKED_TOOLS_HINT = "The following built-in tools cannot be called directly (delegate to dispatch_agent if you need details or execution): "

# visibility_mode=strict で一覧から外したスキルがあるとき、メインの {{skills}} の末尾に足す指示。
# メインは外したスキルの名前を知らないため、選定は目録（{{skill_catalog}}）を持つ explore に任せる。
# 目録をメインの文脈に入れないことで、スキルが多くてもメインのトークンを増やさない（2026-11-20 ユーザー要望）。
_STRICT_HIDDEN_SKILLS_NOTE = (
    "{count} more skills exist but are not listed here (they cannot be run by you directly). "
    "Do not guess their names. When the task needs a skill (creating, converting or reading a particular kind of file, "
    "using an external service, etc.) and none listed above fits, first delegate to `explore`: "
    '"From the skill catalog, find the skill for <what the task needs>, read its SKILL.md with `read_skill`, and report '
    'the skill name, the script to run, its arguments and the inputs it needs (or that no skill fits)." '
    "Then write that report into the task and delegate the execution to `worker`. "
    "If `explore` reports that no skill fits, do the task without a skill."
)

# explore の {{skill_catalog}} に入れる目録の見出しと説明（strict で外したスキルがあるときだけ）
_SKILL_CATALOG_HEADER = (
    "### Skill catalog (for choosing a skill for the main agent)\n\n"
    "The main agent cannot see the skills below. When the task asks you to find a skill for some work, choose from "
    "this catalog (and the skills above), read the chosen SKILL.md with `read_skill`, and report the skill name, the "
    "script to run, its arguments and the inputs it needs; if none fits, say so. Do not run them yourself unless they "
    "are in the run_script list above: the main agent hands the execution to `worker`.\n\n"
)


def strict_hidden_skills(skills: list, config: "Config") -> list:
    """visibility_mode=strict でメインの {{skills}} から外れるスキル（strict 以外・mode=false では常に空）。"""
    from .skills import is_skill_directly_runnable

    if config.main_agent_tool_guard_visibility_mode != "strict" or config.main_agent_tool_guard_mode == "false":
        return []
    return [s for s in skills if not is_skill_directly_runnable(s, config)]


def render_skill_catalog_block(skills: list, config: "Config") -> str:
    """explore の {{skill_catalog}} に入れる文字列。strict で外したスキルが無ければ空文字。"""
    from .skills import render_skills_block

    hidden = strict_hidden_skills(skills, config)
    return f"{_SKILL_CATALOG_HEADER}{render_skills_block(hidden)}" if hidden else ""


def main_skills_block(skills: list, config: "Config") -> tuple[str, str]:
    """メインエージェントの {{skills}} と {{main_agent_blocked_tools_hint}} に入れる文字列。

    [main_agent_tool_guard] visibility_mode で見せ方が変わる（mode=false なら絞り込み・差し替えは起きない）。
      all   : 全スキルを出し、直接扱えないものは説明の末尾に注記。呼べないツール名も案内する。
      hint  : 全スキル名を出し、直接扱えないものは説明を「委譲する」旨の固定文言に差し替える。呼べないツール名も案内する。
      strict: 直接扱えないスキルは一覧から外し、外したものがあれば「explore に目録から選ばせる」指示を末尾に足す
              （目録は explore の {{skill_catalog}}、render_skill_catalog_block 参照）。
    サブエージェントの {{skills}} はこの設定と関係なく build_prompts() が別に組み立てる。
    """
    from .skills import (
        filter_skills_for_main_agent_guard,
        render_skills_block,
        render_skills_block_with_guard_annotation,
        render_skills_block_with_hint,
    )
    from .tools import get_all_tools
    from .tools.tool_node import list_blocked_tool_names_for_hint

    if config.main_agent_tool_guard_visibility_mode in ("all", "hint"):
        if config.main_agent_tool_guard_visibility_mode == "all":
            block = render_skills_block_with_guard_annotation(skills, config)
        else:
            block = render_skills_block_with_hint(skills, config)
        blocked = list_blocked_tool_names_for_hint(get_all_tools(), config)
        return block, (_BLOCKED_TOOLS_HINT + ", ".join(blocked)) if blocked else ""
    hidden = strict_hidden_skills(skills, config)
    if not hidden:
        return render_skills_block(filter_skills_for_main_agent_guard(skills, config)), ""
    visible = filter_skills_for_main_agent_guard(skills, config)
    listed = render_skills_block(visible) if visible else "(no skills you can run directly)"
    return f"{listed}\n\n{_STRICT_HIDDEN_SKILLS_NOTE.format(count=len(hidden))}", ""


def build_prompts(config: "Config", skills: list, agent_type_defs: list) -> tuple[str, list]:
    """(メインエージェントのシステムプロンプト, サブエージェントの定義（プロンプト差し込み済み）) を返す。

    メインのプロンプトには DRAFT_SKILLS_MARKER・MEMORY_INDEX_MARKER・{{document_collections}} が残る
    （fill_session_parts() で埋める）。
    """
    from .agent_types import render_agent_types_block
    from .config import (
        expand_config_vars,
        render_agent_type_run_script_allowlist_block,
        render_memory_visibility_block,
        render_persona_block,
        render_plan_approval_exempt_scripts_block,
    )
    from .memory import MEMORY_INDEX_MARKER
    from .project_instructions import render_project_instructions_block
    from .skill_drafts import DRAFT_SKILLS_MARKER
    from .skills import build_system_prompt_from_block, filter_skills_for_agent_type, render_skills_block

    skills_block, blocked_tools_hint = main_skills_block(skills, config)
    # ユーザー別ドラフトスキルはセッションごとに見える範囲が違うため、目印だけを一覧の末尾に残す。
    system_prompt = build_system_prompt_from_block(f"{skills_block}{DRAFT_SKILLS_MARKER}", config.system_prompt_path).replace(
        "{{main_agent_blocked_tools_hint}}", blocked_tools_hint
    )
    # サブエージェント（agents/*.md）にも {{skills}}/{{agent_types}} 等を差し込む。{{skills}} は
    # [subagent].agent_type_run_script_allowlist に登録がある種別だけ登録スキルへ絞る（{{all_skills}} は全スキル）。
    agent_types_block = render_agent_types_block(agent_type_defs)
    all_skills_block = render_skills_block(skills)
    skill_catalog_block = render_skill_catalog_block(skills, config)
    agents = [
        replace(
            a,
            system_prompt=a.system_prompt.replace(
                "{{skills}}",
                render_skills_block(filter_skills_for_agent_type(skills, a.name, config.subagent_agent_type_run_script_allowlist)),
            )
            .replace("{{all_skills}}", all_skills_block)
            .replace("{{skill_catalog}}", skill_catalog_block)
            .replace("{{agent_types}}", agent_types_block)
            .replace(
                "{{run_script_allowlist}}",
                render_agent_type_run_script_allowlist_block(a.name, config.subagent_agent_type_run_script_allowlist),
            ),
        )
        for a in agent_type_defs
    ]
    # 永続メモリーの索引はユーザー別のため目印だけを残す（サブエージェントには差し込まない）。
    system_prompt = system_prompt.replace("{{memory}}", MEMORY_INDEX_MARKER)
    system_prompt = system_prompt.replace("{{agent_types}}", agent_types_block)
    # 計画承認（Plan Mode）を免除される読み取り専用スクリプトの一覧（config.ini の値が唯一の正）。
    system_prompt = system_prompt.replace(
        "{{plan_approval_exempt_scripts}}", render_plan_approval_exempt_scripts_block(config.plan_approval_exempt_scripts)
    )
    # config.ini の値を ${変数名} として埋め込めるよう展開する（{{...}} の置換が終わった後に行う）。
    system_prompt = expand_config_vars(system_prompt, config)
    # LOCOHANE.md と公開範囲の振り分け基準は自由記述のため、偶然 ${...} を含んでいても展開でエラーにならないよう、展開の後に差し込む。
    system_prompt = system_prompt.replace("{{project_instructions}}", render_project_instructions_block(config.project_instructions_paths))
    system_prompt = system_prompt.replace("{{memory_visibility}}", render_memory_visibility_block(config))
    # AI の名前・性別・性格（[persona]）も自由記述のため展開の後に差し込む。未設定なら目印の行ごと消す。
    persona_block = render_persona_block(config)
    system_prompt = (
        system_prompt.replace("{{persona}}", persona_block)
        if persona_block
        else system_prompt.replace("{{persona}}\n\n", "").replace("{{persona}}", "")
    )

    # サブエージェント共通の注意事項を各種別の末尾に連結する（agents/*.md 自体には書かない）。
    subagent_common = expand_config_vars((config.system_prompt_path.parent / "subagent_common.md").read_text(encoding="utf-8"), config)
    agents = [replace(a, system_prompt=f"{a.system_prompt}\n\n{subagent_common}") for a in agents]
    return system_prompt, agents


def fill_session_parts(system_prompt: str, config: "Config", *, drafts: list, memory_viewer: str) -> str:
    """build_prompts() の目印を、この会話のユーザー向けの内容に差し替える（応答言語の指示は呼び出し側が足す）。

    drafts: このユーザーに見えるドラフトスキル（src/skill_drafts.py の scan_visible_drafts）。評価では常に空
        （評価対象のスキルは研究対象として正式スキルと同じく重ねる。利用者のドラフトは本番でもその人にしか見えないため除く）。
    """
    from .memory import DEFAULT_SYSTEM_PROMPT_MAX_CHARS, MEMORY_INDEX_MARKER, render_system_prompt_memories
    from .skill_drafts import DRAFT_SKILLS_MARKER, render_draft_skills_block
    from .tools.vector_db_tools import render_document_collections_block

    # メモリーの索引は載せない（件数が増えるとプロンプトが膨らむため）。設定ダッシュボードで選んだものだけを載せ、
    # ほかはターンの始めの自動検索（src/memory_recall.py）で見せる。
    pinned = render_system_prompt_memories(
        config.memory_dir,
        viewer=memory_viewer,
        max_chars=getattr(config, "memory_system_prompt_max_chars", DEFAULT_SYSTEM_PROMPT_MAX_CHARS),
    )
    filled = system_prompt.replace(DRAFT_SKILLS_MARKER, render_draft_skills_block(drafts)).replace(MEMORY_INDEX_MARKER, pinned)
    # ナレッジのコレクション一覧は取り込みで変わるため、組み立てのたびに差し込む。
    return filled.replace("{{document_collections}}", render_document_collections_block(config))
