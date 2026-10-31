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
    print_json(
        {
            "draft": ref.full_name,
            "valid": error is None,
            "error": error,
            "name": fm.get("name"),
            "description": fm.get("description"),
            "description_length": len(fm.get("description", "")),
            "script_files": sorted(p.name for p in scripts_dir.glob("*") if p.is_file()) if scripts_dir.is_dir() else [],
            "eval_cases": sorted(p.stem for p in (ref.dir / "evals").glob("*.yaml")),
        }
    )
    return 0 if error is None else 1


if __name__ == "__main__":
    run_main(main)
