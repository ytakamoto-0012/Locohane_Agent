"""ドラフトスキルの SKILL.md frontmatter を src/skills.py の _validate() と同一ルールで検証する。

skill-creator スキルの実行スクリプト（progressive disclosure 第3段階）。

    python validate_skill.py --name my-skill

frontmatter のパースは PyYAML 等の外部依存を避けた簡易パーサーで行う
（name/description/license の単純なスカラー値のみを見る。最終的な合否は
Locohane 本体の走査が唯一の正）。

自己完結（標準ライブラリのみ）。依存なし。
"""

from __future__ import annotations

import argparse

from _common import draft_context, find_official_skill, parse_frontmatter, print_json, read_meta, resolve_draft, run_main, validate_skill_md

# 新規ドラフトの最初の版の本文の目安（空行を除く行数）。超えたら warnings で短くするよう促す（合否には使わない）。
FIRST_VERSION_MAX_LINES = 40


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="draft name (for your own drafts, just the skill name)")
    args = parser.parse_args()

    ctx = draft_context()
    ref = resolve_draft(ctx, args.name, "read")
    error = validate_skill_md(ref.dir)
    fm = parse_frontmatter((ref.dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")) or {}
    if error is None and read_meta(ref).get("kind") != "improve" and find_official_skill(ref.name) is not None:
        error = f"It has the same name as the official skill '{ref.name}' (give a new skill another name)"
    scripts_dir = ref.dir / "scripts"
    script_files = sorted(p.name for p in scripts_dir.glob("*") if p.is_file()) if scripts_dir.is_dir() else []
    body_lines = _body_line_count((ref.dir / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
    warnings = []
    # 新規ドラフトは短い SKILL.md だけで始める（育てるのはスキル研究室の評価で）。改善版は元の正式スキルの
    # 長さを引き継ぐので対象にしない。
    if read_meta(ref).get("kind") != "improve":
        if body_lines > FIRST_VERSION_MAX_LINES:
            warnings.append(
                f"The body has {body_lines} lines. Keep a first version to about {FIRST_VERSION_MAX_LINES} lines: remove background, examples and rules nobody asked for."
            )
        if script_files:
            warnings.append("It has scripts. Remove them unless the work cannot be done with the assistant's usual tools or the user asked for one.")
        if (ref.dir / "references").is_dir() and any((ref.dir / "references").iterdir()):
            warnings.append("It has references/. A first version needs only SKILL.md.")
    print_json(
        {
            "draft": ref.full_name,
            "valid": error is None,
            "error": error,
            "name": fm.get("name"),
            "description": fm.get("description"),
            "description_length": len(fm.get("description", "")),
            "body_lines": body_lines,
            "script_files": script_files,
            "eval_cases": sorted(p.stem for p in (ref.dir / "evals").glob("*.yaml")),
            "warnings": warnings,
        }
    )
    return 0 if error is None else 1


def _body_line_count(text: str) -> int:
    """frontmatter を除いた本文の、空行でない行数。"""
    body = text
    if text.startswith("---"):
        parts = text.split("\n---", 1)
        body = parts[1].split("\n", 1)[1] if len(parts) == 2 and "\n" in parts[1] else ""
    return sum(1 for line in body.splitlines() if line.strip())


if __name__ == "__main__":
    run_main(main)
