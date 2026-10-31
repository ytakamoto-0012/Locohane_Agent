"""AI 修正（評価で失敗した回を研究室の LLM に見せ、テーマのファイルを直させる）。

直せるのはテーマ内の資産（assets/ 配下のスキル・サブエージェント）・ケース（cases/*.yaml）・
設定パッチ（config_patch.json）・仕様以外のファイル。仕様（spec.md）は人が決めた要件なので直させない。
ケースも直してよいが、変更はレビュー画面で強調表示し、人が確かめる（ケースが変われば
トライアウトは0回からやり直しになる）。

修正は全部適用できるか、全部やめるかのどちらか。パスの制限（themes.resolve_path）と形式の検証
（SKILL.md・エージェント定義の frontmatter と tools 名・ケース・設定パッチ・Python の構文）に
1つでも通らなければ元に戻し、理由を次の反復の入力にする。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from src.config import Config

from . import evaluation, themes
from .llm import LabLLMError, ask_json

_SYSTEM = """You fix the skills and sub-agents of a business AI assistant (an agent running on a local LLM).
A skill consists of SKILL.md (frontmatter with name and description at the top, the procedure in the body), scripts/ (Python scripts) and references/.
A sub-agent consists of agents/<name>.md (frontmatter with name, description and tools at the top; the body is the system prompt).
Read the records of failures in the evaluation, identify the cause, and fix the files so that they behave as the spec says.

Rules:
- Do not change the spec (spec.md). Fix the assets to match the spec.
- Do not add ad hoc instructions that only work for the failure at hand (such as "always answer XX"). Generalize the cause and fix that.
- The frontmatter name of SKILL.md and agent definitions must be the same as the folder name / file name.
- On success, scripts print one line of JSON to stdout with exit code 0; on failure, they print the reason to stderr with exit code 1.
- You may fix a case (cases/*.yaml) only if the case itself contradicts the spec. If you do, write the reason in analysis.
  You cannot delete or add cases, or change the cases' input files (cases/fixtures/).
- Paths you can fix: assets/skills/<name>/... and assets/agents/<name>.md of existing research targets, existing cases/<ID>.yaml, config_patch.json
  (you cannot create new skills or sub-agents, and you cannot delete SKILL.md or agent definitions themselves)
- Keep the language of the files you edit (do not translate existing text).

Output only JSON in the following format:
{"analysis": "the cause of the failure and how to fix it",
 "edits": [
   {"path": "assets/skills/<name>/SKILL.md", "old": "the original string to replace (must match exactly one place in the file)", "new": "the new string"},
   {"path": "assets/skills/<name>/scripts/run.py", "content": "the new content of the whole file"},
   {"path": "assets/skills/<name>/references/old.md", "delete": true}
 ]}"""


def _read_limited(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "(binary file)"
    if len(text) > limit:
        return text[:limit] + f"\n… ({len(text) - limit} characters omitted)"
    return text


def theme_context(directory: Path, max_file_chars: int, *, include_cases: bool = True) -> str:
    """仕様・資産・設定パッチ・ケースの全文（AI に渡す材料）。"""
    parts = [f"## Spec (spec.md)\n{_read_limited(directory / themes.SPEC_FILENAME, max_file_chars)}"]
    for item in themes.list_files(directory):
        rel = item["path"]
        if rel == themes.SPEC_FILENAME or rel.startswith(f"{themes.CASES_DIRNAME}/{themes.FIXTURES_DIRNAME}/"):
            continue
        if not include_cases and rel.startswith(f"{themes.CASES_DIRNAME}/"):
            continue
        body = _read_limited(directory / rel, max_file_chars) if item["text"] else "(binary file)"
        parts.append(f"## {rel}\n```\n{body}\n```")
    fixtures = themes.list_fixtures(directory)
    if fixtures:
        listing = []
        for f in fixtures:
            base = directory / themes.CASES_DIRNAME / f
            listing += [p.relative_to(directory / themes.CASES_DIRNAME).as_posix() for p in sorted(base.rglob("*")) if p.is_file()]
        parts.append("## Input files of the cases (contents omitted)\n" + "\n".join(f"- {x}" for x in listing))
    return "\n\n".join(parts)


def failures_text(results: list[dict], verdicts: list[dict], excerpt_chars: int, per_case: int = 2) -> str:
    """失敗した回の説明（ケースごとに最大 per_case 回分）。"""
    by_key = {(v["case_id"], v["repeat_index"]): v for v in verdicts}
    shown: dict[str, int] = {}
    blocks = []
    for r in results:
        v = by_key.get((r.get("case_id"), r.get("repeat_index", 1)))
        if not v or v["outcome"] == "pass":
            continue
        cid = r.get("case_id", "?")
        if shown.get(cid, 0) >= per_case:
            continue
        shown[cid] = shown.get(cid, 0) + 1
        failed_rules = {k: d for k, d in (r.get("rule_results") or {}).items() if not d.get("pass")}
        lines = [f"### Case {cid} (run {r.get('repeat_index', 1)}): {v['outcome']}"]
        if failed_rules:
            lines.append("Unmet checks: " + json.dumps(failed_rules, ensure_ascii=False)[:1500])
        if v.get("ai_judge"):
            lines.append(f"AI judge's reason for failing: {v['ai_judge'].get('reason', '')}")
        lines.append(evaluation.transcript_excerpt(r, excerpt_chars))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "(no failure records)"


def history_text(iterations: list[dict], limit: int = 5) -> str:
    rows = []
    for it in iterations[-limit:]:
        fix = it.get("fix") or {}
        rows.append(
            f"- Iteration {it.get('n')} ({it.get('phase')}): verdict {it.get('verdict')}"
            + (f", fixed files {fix.get('applied')}, analysis: {str(fix.get('analysis', ''))[:300]}" if fix else "")
            + (f", why the fix could not be applied: {fix.get('errors')}" if fix.get("errors") else "")
        )
    return "\n".join(rows) or "(none yet)"


def propose(config: Config, directory: Path, *, failures: str, history: str, max_file_chars: int) -> dict:
    user = (
        f"{theme_context(directory, max_file_chars)}\n\n"
        f"# Iterations so far\n{history}\n\n"
        f"# Failure records of this evaluation\n{failures}\n\n"
        "Identify the cause and output the fix in the specified JSON format."
    )
    data = ask_json(config, _SYSTEM, user)
    if not isinstance(data, dict) or not isinstance(data.get("edits"), list):
        raise LabLLMError(f"Invalid format of the fix response: {str(data)[:300]}")
    return {"analysis": str(data.get("analysis", "")), "edits": data["edits"]}


def _forbidden(directory: Path, rel: str, path: Path, *, delete: bool) -> str | None:
    """AI 修正でしてはいけない変更なら理由を返す。

    試験に通るだけの直し方（失敗したケースを消す・入力ファイルを都合よく書き換える・
    研究対象そのものを消す・別の資産を作って差し替える）を防ぐ。ケースの中身を直すのは、
    仕様と矛盾している場合に限って認め、レビューで強調表示する。
    """
    parts = rel.split("/")
    if parts[0] == themes.CASES_DIRNAME and len(parts) >= 3:
        return "The AI cannot change the input files of cases"
    if parts[0] == themes.CASES_DIRNAME:
        if delete:
            return "The AI cannot delete cases"
        if not path.is_file():
            return "The AI cannot add cases (it can only fix existing ones)"
    if parts[0] == themes.ASSETS_DIRNAME:
        asset = path if parts[1] == "agents" else directory / themes.ASSETS_DIRNAME / "skills" / parts[2]
        exists = asset.is_file() if parts[1] == "agents" else (asset / "SKILL.md").is_file()
        if not exists:
            return "The AI cannot create skills or sub-agents that are not research targets"
        if delete and (parts[1] == "agents" or parts[3:] == ["SKILL.md"]):
            return "The AI cannot delete the research targets themselves (SKILL.md, agent definitions)"
    return None


def apply_edits(directory: Path, edits: list, *, known_tools: list[str] | None) -> tuple[list[str], list[str]]:
    """edits をすべて適用する（1つでも失敗したら全部元に戻す）。(適用したパス, エラー) を返す。"""
    errors: list[str] = []
    planned: list[tuple[str, Path, str | None]] = []  # (rel, path, 新しい内容 or None=削除)
    pending: dict[Path, str | None] = {}
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict) or not isinstance(edit.get("path"), str):
            errors.append(f"item {i + 1}: no path")
            continue
        rel = edit["path"].replace("\\", "/").strip().strip("/")
        if rel == themes.SPEC_FILENAME:
            errors.append(f"{rel}: the AI cannot change the spec")
            continue
        try:
            path = themes.resolve_path(directory, rel, for_write=True)
        except themes.ThemeError as e:
            errors.append(str(e))
            continue
        forbidden = _forbidden(directory, rel, path, delete=bool(edit.get("delete")))
        if forbidden:
            errors.append(f"{rel}: {forbidden}")
            continue
        try:
            current = pending[path] if path in pending else (path.read_text(encoding="utf-8") if path.is_file() else None)
        except UnicodeDecodeError:
            errors.append(f"{rel}: not a text file, so the AI cannot fix it")
            continue
        if edit.get("delete"):
            new_content = None
        elif isinstance(edit.get("content"), str):
            new_content = edit["content"]
        elif isinstance(edit.get("old"), str) and isinstance(edit.get("new"), str):
            if current is None:
                errors.append(f"{rel}: the file does not exist, so it cannot be fixed with old/new (write the whole file with content)")
                continue
            count = current.count(edit["old"])
            if count != 1:
                errors.append(f"{rel}: old matched {count} places in the file (make it match exactly one place)")
                continue
            new_content = current.replace(edit["old"], edit["new"])
        else:
            errors.append(f"{rel}: none of content, old/new or delete is specified")
            continue
        if new_content is not None:
            try:
                themes.validate_content(rel, new_content)
                if rel.startswith(f"{themes.ASSETS_DIRNAME}/agents/") and known_tools is not None:
                    error = themes.validate_agent_md(new_content, Path(rel).stem, known_tools)
                    if error:
                        raise themes.ThemeError(error)
            except themes.ThemeError as e:
                errors.append(str(e))
                continue
        pending[path] = new_content
        planned.append((rel, path, new_content))
    if errors:
        return [], errors
    if not planned:
        return [], ["There are no fixes"]

    with tempfile.TemporaryDirectory(prefix="skill_lab_fix_") as tmp:
        backups: dict[Path, Path | None] = {}
        try:
            for rel, path, content in planned:
                if path not in backups:
                    if path.is_file():
                        backup = Path(tmp) / f"{len(backups)}"
                        shutil.copy2(path, backup)
                        backups[path] = backup
                    else:
                        backups[path] = None
                if content is None:
                    if path.is_file():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
        except OSError as e:
            for path, backup in backups.items():
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    shutil.copy2(backup, path)
            return [], [f"Failed to write files: {e}"]
    applied = list(dict.fromkeys(rel for rel, _, _ in planned))
    themes.mark_changed(directory, "AI", "AI fix: " + ", ".join(applied))
    return applied, []
