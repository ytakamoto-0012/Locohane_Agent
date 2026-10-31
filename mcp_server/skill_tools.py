"""skills/ を実際に呼び出す3つのMCPツール（Tools方式）。

Locohane本体の3段階progressive disclosure
（`src/skills.py` の frontmatterパース、`src/tools/read_skill.py`、
`src/tools/run_script.py`）と同じ考え方を、Chainlitに依存しない
standalone プロセスとして実装し直したもの:

- list_skills       … Discovery相当（全スキルの name: description 一覧）
- read_skill        … Read相当（SKILL.md 本文全体）
- run_skill_script   … Execute相当（scripts/ 配下のスクリプトを実際に実行）

対象ディレクトリは `config.SKILLS_SRC`（元の `skills/`。複数ルート指定可、
`LOCOHANE_MCP_SKILLS_SRC`参照）を直接見る。
`publish.build_publish_dir()` が作る配布用コピー（`.env` 等除外済み）は
Resources方式（外部への配布）専用であり、ここでの実行はMCPサーバーと
同一マシン上の信頼された呼び出し元がその場で行うだけなので、
`web-search` の `scripts/.env`（TAVILY_API_KEY）等も問題なく使える必要がある
（脅威モデルが異なるため、意図的に別ディレクトリを見ている）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from .config import SCRIPT_PYTHON, SCRIPT_TIMEOUT_SECONDS, SKILLS_SRC, WORKDIR


def _parse_frontmatter(text: str) -> dict | None:
    """SKILL.md 先頭の `---` で囲まれた YAML frontmatter を dict で返す。

    `src/skills.py` の `_parse_frontmatter()` と同じロジック（依存を持たせず
    ここに直接複製。standalone プロセスのため Locohane 本体の `src/` は
    importしない）。
    """
    if not text.lstrip().startswith("---"):
        return None
    parts = text.lstrip().split("---", 2)
    if len(parts) < 3:
        return None
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _safe_skill_path(skill_name: str, relative: str) -> Path | None:
    """`<root>/skill_name/relative` を解決し、境界外なら None を返す。

    `src/tools/_safe_path.py` の `_safe_path()` と同じ考え方
    （`resolve()` 後に `is_relative_to()` でいずれかの `SKILLS_SRC` ルート
    配下に限定するディレクトリトラバーサル対策、かつ複数ルートを前方から
    順に解決し実在する最初の候補を返す）を依存なしで再実装したもの。
    どのルートにも実在しない場合は先頭ルート基準の候補を返す（「見つかりません」
    エラーへ自然に流すため）。全ルートで境界外と判定された場合のみ None。
    """
    candidates: list[Path] = []
    for root in SKILLS_SRC:
        candidate = (root / skill_name / relative).resolve()
        if not candidate.is_relative_to(root.resolve()):
            continue
        candidates.append(candidate)
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else None


def _resolve_script_path(skill_name: str, script_filename: str) -> Path:
    """スキルの scripts/ 配下からファイル名でスクリプトを探し、絶対パスを返す。

    `src/tools/_safe_path.py` の `_resolve_script_filename()` と同じ考え方。
    呼び出し側は `scripts/` プレフィックスを書く必要はなく、ファイル名のみ
    渡せば `scripts/` 配下を再帰探索して解決する（同名ファイルが複数階層に
    ある場合は最も浅い階層を採用）。

    Raises:
        ValueError: skill_name が SKILLS_SRC 外を指す場合、scripts/ が
            無い場合、該当ファイルが見つからない場合。
    """
    basename = os.path.basename(script_filename)
    if not basename:
        raise ValueError(f"specify the file name of the script: {script_filename!r}")
    scripts_root = _safe_skill_path(skill_name, "scripts")
    if scripts_root is None:
        raise ValueError(f"access outside the skills directory is not allowed: {skill_name}")
    if not scripts_root.is_dir():
        raise ValueError(f"skill '{skill_name}' has no scripts/ directory")
    matches = [p for p in scripts_root.rglob(basename) if p.is_file()]
    if not matches:
        raise ValueError(f"script not found: {basename} (skill={skill_name})")
    matches.sort(key=lambda p: (len(p.relative_to(scripts_root).parts), str(p)))
    return matches[0]


def list_skills() -> str:
    """List Locohane's skills, one "name: description" line per skill.

    Call this first to see which skills are available. When you find a skill whose
    name looks relevant, read its instructions with read_skill(skill_name).

    Returns:
        Text whose lines are "name: description" (skill folders without valid Agent Skills
        frontmatter are left out). A skill that exists in several skill roots is listed once.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for root in SKILLS_SRC:
        if not root.is_dir():
            continue
        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name in seen:
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            if fm is None:
                continue
            name = fm.get("name")
            description = fm.get("description")
            if isinstance(name, str) and isinstance(description, str):
                seen.add(skill_dir.name)
                lines.append(f"{name}: {description}")
    return "\n".join(lines)


def read_skill(skill_name: str) -> str:
    """Read the whole SKILL.md of a skill.

    Use it to check the steps of a skill found with list_skills and how to call it
    (which file under scripts/ to run with which arguments).

    Args:
        skill_name: Folder name of the skill to read (= name in SKILL.md).

    Returns:
        The whole SKILL.md text. If skill_name is invalid or SKILL.md does not exist,
        a string starting with "Error: ".
    """
    skill_md = _safe_skill_path(skill_name, "SKILL.md")
    if skill_md is None:
        return f"Error: access outside the skills directory is not allowed: {skill_name}"
    if not skill_md.is_file():
        return f"Error: SKILL.md of skill '{skill_name}' was not found."
    return skill_md.read_text(encoding="utf-8")


def run_skill_script(skill_name: str, script_filename: str, script_args: list[str] | None = None) -> str:
    """Run a script under scripts/ of a skill and return the result.

    Pass script_filename and script_args following the call written in SKILL.md
    (read with read_skill; written as `python <script>.py <args...>`).
    Give output locations (output_path, etc.) in script_args as absolute paths, not
    relative ones. The working directory (cwd) is the directory this MCP server was
    started in (LOCOHANE_MCP_SKILLS_WORKDIR takes precedence if set). The script runs
    with this MCP server's own Python (LOCOHANE_MCP_SKILLS_PYTHON takes precedence if set).

    Args:
        skill_name: Folder name of the skill that has the script.
        script_filename: File name of the script to run (e.g. "count.py").
            No scripts/ prefix is needed.
        script_args: Additional arguments passed to the script (optional).

    Returns:
        "[exit code] N", followed by the standard output and standard error under the
        headings "[stdout]" and "[stderr]" when present. When the skill or script is not
        found, or the script times out or cannot be started, a string starting with "Error: ".
    """
    try:
        script_path = _resolve_script_path(skill_name, script_filename)
    except ValueError as e:
        return f"Error: {e}"

    if WORKDIR is not None:
        WORKDIR.mkdir(parents=True, exist_ok=True)
    cmd = [SCRIPT_PYTHON, str(script_path), *(script_args or [])]
    try:
        result = subprocess.run(
            cmd,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SCRIPT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"Error: the script timed out after {SCRIPT_TIMEOUT_SECONDS} seconds."
    except OSError as e:
        return f"Error: could not start the script: {e}"

    parts = [f"[exit code] {result.returncode}"]
    if result.stdout:
        parts.append(f"[stdout]\n{result.stdout.rstrip()}")
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr.rstrip()}")
    return "\n".join(parts)
