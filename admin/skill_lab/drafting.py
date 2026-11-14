"""研究室の LLM による下書き（仕様・ケース・新しいスキル／サブエージェント）。

下書きは人が画面で確かめて直す前提。書き込みはテーマのファイル操作（themes.py）を通し、
形式の検証に通らないものは保存しない。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from src.config import PROJECT_ROOT, Config

from . import themes
from .fixer import theme_context
from .llm import LabLLMError, ask_json, ask_text

_SPEC_SYSTEM = """You write the specs of skills and sub-agents for a business AI assistant.
The spec is the grading standard that decides what the research targets (the skills and sub-agents this theme fixes or creates) should do.
Read the list and contents of the research targets (SKILL.md, scripts/, references/ and sub-agent definitions under assets/), work out
what they are for, in which work and situations they are used, what they output and what they must not do, and write that down.
- If a research target is a copy of an official one in use now, base the spec on its current behavior, and follow the requester's note for the points to fix.
- Treat a research target the AI is still creating as the thing its order asks for.
- If the requester left a note, it takes precedence over what you read from the research targets.
- If the current spec has content written by a person (other than "TODO:" lines), keep it and fill in only the empty parts.
- If the current spec uses the old Japanese headings (目的, 使う場面, 期待する出力, やってはいけないこと), move their content under the matching English headings below.
Output only a Markdown spec with the following headings (no preamble). Keep the heading text exactly as shown (the screen parses these headings):
# <theme name>
## Purpose
## When to use
## Expected output
## Must not do
Under "When to use", write in which work and situations it is used; do not write concrete instructions a user would type (those go in the test cases).
Do not add features by guesswork; write only what can be read from the material. Write unclear points as lines starting with "TODO:".
Keep the headings above in English exactly as shown, and write the theme name line and the body text under each heading in the output language given below
(translate content you take over from the research targets or an existing spec written in another language)."""

_CASES_SYSTEM = """You create test cases that check the behavior of a business AI assistant.
The assistant runs on a fairly small local LLM, so write the user instructions (turns) short and to the point, as real users would type them, in the output language given below.
The assistant answers in that language too, so write response_contains / response_not_contains words, judge and notes in it as well.
A case is a test question that checks whether the research targets (the skills and sub-agents under assets/) behave as the spec (spec.md) says.
- From the spec's "Purpose" and "When to use", create instructions users would actually type.
- The assistant chooses a skill or sub-agent only from its name and description, so each instruction must give it a reason to use
  the research target: ask for the work the target's description and "When to use" cover. If a skill can only be chosen by name
  (for example a test skill that does the same whatever is asked), name it in the instruction with the exact spelling.
  An instruction such as only "hello" or "what is 1+1?" that is unrelated to the target never makes the assistant use it.
- Make the checks match what the instruction asks; do not check for something the instruction does not ask for.
- When you create 2 or more cases, include at least one case where the research target must NOT be used: an instruction for other
  work that looks similar, checked with tool_call_args_not_contains (e.g. {"read_skill": {"skill_name": "<target>"}} or
  {"dispatch_agent": {"agent_type": "<target>"}}). This keeps fixes from making a skill claim every message.
- Turn the spec's "Expected output" into pass conditions (response_contains, judge), and "Must not do" into fail conditions
  (response_not_contains, tool_not_called, judge).
- Use only skill names, sub-agent names and tool names that actually exist in the research targets.
- Do not check features not in the spec or points marked "TODO:". Do not create cases that check the same thing as existing cases.
A single case may check a flow that uses several skills and sub-agents.
Output only a JSON array in the following format:
[{"id": "001_basic", "turns": ["user instruction"],
  "expect": {"tool_called_any": [...], "tool_not_called": [...], "tool_call_args_contains": {"tool name": {"argument": "value"}},
             "tool_call_args_not_contains": {"tool name": {"argument": "value"}}, "response_contains": [...], "response_not_contains": [...]},
  "judge": "the viewpoint for judging the result (optional; only for points that cannot be decided objectively)",
  "work_dir": "fixtures/<folder of input files> (only when input files are needed)",
  "notes": "what this case checks"}]
Write only the expect keys you use. Whether a skill was read can be checked with {"tool_call_args_contains": {"read_skill": {"skill_name": "<skill name>"}}},
and delegation to a sub-agent with {"tool_call_args_contains": {"dispatch_agent": {"agent_type": "<name>"}}}.
Always write at least one of expect and judge. The id may contain only letters, digits, hyphens and underscores.
Do not write verify_script (a checking script in the input file folders that a person prepares)."""

_SKILL_SYSTEM = """You write skills (SKILL.md) for a business AI assistant.
A SKILL.md starts with frontmatter (name, description) and the body describes the procedure.
- The description is the only trigger clue. Write concretely "what it does" and "for what kind of instructions it is used" (1024 characters or fewer).
- The body is 500 lines or fewer. If it uses scripts, write call examples in the form `python <script>.py <args...>` and also explain what the output means.
- Do not write tool names (run_script, etc.) in the body.
- The body is read by a fairly small local LLM: short sentences, one rule per line with what to do instead, conditions as bullet
  points, no long background or many examples. Write example names and values in the users' language.
- Write the description and the body in the output language given below (keep name as given).
Output only the full text of SKILL.md (no preamble, no code fences)."""

_AGENT_SYSTEM = """You write sub-agents (agents/<name>.md) for a business AI assistant.
A sub-agent is a separate agent that runs when the main assistant delegates work to it with dispatch_agent.
The file starts with frontmatter (name, description, tools), and the body becomes the system prompt of the sub-agent.
- The description is the clue the main assistant uses to choose whom to delegate to. Write concretely what this agent is entrusted with.
- For tools, choose only the minimum needed, comma-separated, from the given list of tool names.
- In the body, write the role, procedure, form of the final answer and prohibitions; write {{skills}} where the skill list is inserted,
  and {{run_script_allowlist}} where the list of skills runnable with run_script is inserted.
- Write the description and the body in the output language given below (keep name and tool names as they are).
Output only JSON in the following format:
{"content": "full text of the file", "allowlist": [["<this name>", "<skill name allowed to be called with run_script>"], ...]}
Write allowlist only if there are skills it should call with run_script (otherwise an empty array)."""


def targets_brief(directory: Path) -> str:
    """研究対象の一覧（入手元と、新規に作ったものの注文。AI が作成中のものも含む）。"""
    data = themes.read(directory)
    sources = data.get("sources") or {}
    labels = {"skills": "skill", "agents": "sub-agent"}
    lines = []
    for asset_type, names in themes.list_assets(directory).items():
        for name in names:
            src = (sources.get(asset_type) or {}).get(name) or {}
            kind = src.get("kind")
            if kind == "draft":
                origin = f"finishing a draft prototyped by user {src.get('owner') or ''}"
            elif kind == "official":
                origin = "a copy of an official one in use now, made to improve it"
            else:
                origin = "newly created in this theme"
            if src.get("description"):
                origin += f" (order: {src['description']})"
            lines.append(f"- {labels[asset_type]} {name}: {origin}")
    for task in data.get("tasks") or []:
        params = task.get("params") or {}
        if task.get("type") == "draft_asset" and task.get("status") in ("queued", "running"):
            lines.append(f"- {labels.get(params.get('asset_type'), '')} {params.get('name')}: being created by the AI (order: {params.get('request', '')})")
    return "\n".join(lines) or "(none yet)"


def draft_spec(config: Config, directory: Path, *, max_file_chars: int, request: str = "") -> None:
    data = themes.read(directory)
    user = (
        f"Theme name: {data.get('title')}\n\n## Research targets\n{targets_brief(directory)}\n\n"
        f"{theme_context(directory, max_file_chars, include_cases=False)}"
    )
    if request:
        user += f"\n\n## Note from the requester\n{request}"
    text = ask_text(config, _SPEC_SYSTEM, user)
    themes.write_text(directory, themes.SPEC_FILENAME, text.strip() + "\n", actor="AI (draft)")


def draft_cases(config: Config, directory: Path, *, max_file_chars: int, count: int, request: str = "") -> list[str]:
    existing = {c["id"] for c in themes.list_cases(directory)}
    # Windows ではファイル名の大文字小文字を区別しないため、ID の重なりも区別せずに確かめる
    # （区別すると 001_Basic が既存の 001_basic.yaml を上書きした）。
    taken = {i.casefold() for i in existing}
    user = (
        f"## Research targets\n{targets_brief(directory)}\n\n"
        f"{theme_context(directory, max_file_chars)}\n\n"
        f"Existing case IDs: {sorted(existing) or 'none'}\n"
        f"Available input file folders (values allowed in work_dir): {themes.list_fixtures(directory) or 'none'}\n"
        f"Create {count} case(s)."
    )
    if request:
        user += f"\n\n## Note from the requester\n{request}"
    items = ask_json(config, _CASES_SYSTEM, user)
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise LabLLMError(f"Invalid format of the cases response: {str(items)[:300]}")
    created, errors = [], []
    for item in items[: max(1, count)]:
        if not isinstance(item, dict):
            continue
        case_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(item.get("id") or "")).strip("_-")[:48]
        n = 1
        base_id = case_id or "case"
        while not case_id or case_id.casefold() in taken:
            case_id = f"{base_id}_{n}"
            n += 1
        turns = item.get("turns") or []
        if isinstance(turns, str):
            turns = [turns]
        case = {"id": case_id, "target": "skill_lab", "turns": turns}
        for key in ("expect", "judge", "work_dir", "notes"):
            if item.get(key):
                case[key] = item[key]
        content = yaml.safe_dump(case, allow_unicode=True, sort_keys=False)
        try:
            themes.write_text(directory, f"{themes.CASES_DIRNAME}/{case_id}.yaml", content, actor="AI (draft)")
        except themes.ThemeError as e:
            errors.append(str(e))
            continue
        taken.add(case_id.casefold())
        created.append(case_id)
    if not created:
        raise LabLLMError("No cases could be saved: " + " / ".join(errors))
    return created


def draft_asset(
    config: Config,
    directory: Path,
    *,
    asset_type: str,
    name: str,
    request: str,
    known_tools: list[str],
    skills: list[dict],
    max_file_chars: int,
) -> None:
    """新しいスキル・サブエージェントを下書きして資産に加える。"""
    themes.asset_path(directory, asset_type, name)  # 名前の検証
    spec = (directory / themes.SPEC_FILENAME).read_text(encoding="utf-8")[:max_file_chars]
    if asset_type == "skills":
        user = f"Skill name (name and folder name): {name}\n\n## What to create\n{request}\n\n## Theme spec\n{spec}"
        content = _strip_fence(ask_text(config, _SKILL_SYSTEM, user))
        themes.add_asset_new(directory, "skills", name, content, actor="AI (draft)", description=request)
        return
    example = _agent_example()
    skill_lines = "\n".join(f"- {s['name']}: {s['description'][:200]}" for s in skills)
    user = (
        f"Agent name (name and file name): {name}\n\n## What to create\n{request}\n\n## Theme spec\n{spec}\n\n"
        f"## Tool names allowed in tools\n{', '.join(known_tools)}\n\n## Available skills\n{skill_lines}\n\n"
        f"## Example (an existing sub-agent; use it as a reference for the structure)\n{example}"
    )
    data = ask_json(config, _AGENT_SYSTEM, user)
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        raise LabLLMError(f"Invalid format of the agent definition response: {str(data)[:300]}")
    content = _strip_fence(data["content"])
    error = themes.validate_agent_md(content, name, known_tools)
    if error:
        raise LabLLMError(error)
    themes.add_asset_new(directory, "agents", name, content, actor="AI (draft)", description=request)
    allowlist = [e for e in data.get("allowlist") or [] if isinstance(e, list) and len(e) == 2 and e[0] == name]
    if allowlist:
        try:
            merge_config_patch(directory, ("subagent", "agent_type_run_script_allowlist"), allowlist, actor="AI (draft)")
        except (themes.ThemeError, ValueError):
            # 形の崩れた許可はあきらめる（エージェントは作れているので、許可は「フォームで編集」で足せる）。
            pass


def merge_config_patch(directory: Path, key: tuple[str, str], items: list, *, actor: str) -> None:
    """config_patch.json のリストへ項目を足す（重複は足さない）。"""
    path = directory / themes.CONFIG_PATCH_FILENAME
    data = json.loads(path.read_text(encoding="utf-8") or "{}") if path.is_file() else {}
    current = data.setdefault(key[0], {}).setdefault(key[1], [])
    for item in items:
        if item not in current:
            current.append(item)
    themes.write_text(directory, themes.CONFIG_PATCH_FILENAME, json.dumps(data, ensure_ascii=False, indent=2) + "\n", actor=actor)


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip() + "\n"


def _agent_example() -> str:
    path = PROJECT_ROOT / "agents" / "explore.md"
    if not path.is_file():
        return "(no example)"
    return path.read_text(encoding="utf-8")[:3000]
