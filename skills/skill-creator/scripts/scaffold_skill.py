"""新しいドラフトスキルの雛形（SKILL.md + references/ + evals/ +（任意で）scripts/）を作る。

skill-creator スキルの実行スクリプト（progressive disclosure 第3段階）。

    python scaffold_skill.py --name my-new-skill --description "..." [--with-script]

作成先は会話のユーザー自身のドラフト置き場 `<draft_dir>/<ユーザー名>/<name>/`。
正式スキル（skills/ や project_locohane_dir 配下）には書き込まない。正式スキルと
同じ名前は拒否する（既存スキルを直したいときは fork_skill.py で改善案を作る）。

自己完結（標準ライブラリのみ）。依存なし。
"""

from __future__ import annotations

import argparse
import os

from _common import (
    DRAFT_EVALS_DIRNAME,
    SkillCreatorError,
    draft_context,
    find_official_skill,
    now_iso,
    print_json,
    resolve_draft,
    run_main,
    validate_name_description,
    write_meta,
)

SKILL_MD_TEMPLATE = """---
name: {name}
description: {description}
---

# {name}

(Write in 1-2 sentences what this skill does)

## Procedure

1. (What to do first)
2. Run the following command:
   ```
   python run.py --input <input file>
   ```
3. Write what the keys of the output JSON mean and how to report to the user.

## Notes

- (How to handle invalid input, what not to do)
"""

SAMPLE_SCRIPT_TEMPLATE = '''"""Script that runs the {name} skill.

Keep it self-contained with the standard library only (if dependencies are needed, state them in SKILL.md).
On success, print one line of JSON to stdout with exit code 0; on failure, print the reason to stderr with exit code 1.
"""

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args()
    print(json.dumps({{"ok": True, "input": args.input}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="skill name (lowercase letters, digits and hyphens)")
    parser.add_argument("--description", required=True, help="what it does and for what kind of messages it is used")
    parser.add_argument("--with-script", action="store_true", help="also create a sample scripts/run.py")
    args = parser.parse_args()

    error = validate_name_description(args.name, args.description)
    if error:
        raise SkillCreatorError(error)
    if find_official_skill(args.name) is not None:
        raise SkillCreatorError(
            f"It has the same name as the official skill '{args.name}'. Use another name, or use fork_skill.py to fix the existing skill."
        )
    ctx = draft_context()
    ref = resolve_draft(ctx, args.name, "write", must_exist=False)
    if ref.dir.exists():
        raise SkillCreatorError(f"A draft with the same name already exists: {ref.full_name}")

    ref.dir.mkdir(parents=True)
    (ref.dir / "SKILL.md").write_text(SKILL_MD_TEMPLATE.format(name=args.name, description=args.description), encoding="utf-8")
    (ref.dir / "references").mkdir()
    (ref.dir / DRAFT_EVALS_DIRNAME).mkdir()
    if args.with_script:
        (ref.dir / "scripts").mkdir()
        (ref.dir / "scripts" / "run.py").write_text(SAMPLE_SCRIPT_TEMPLATE.format(name=args.name), encoding="utf-8")
    write_meta(
        ref,
        {
            "kind": "new",
            "author": ctx.user,
            "last_editor": ctx.user,
            "thread_id": os.environ.get("AGENT_THREAD_ID"),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "status": "draft",
            "tryouts": [],
        },
    )
    print_json(
        {
            "draft": ref.full_name,
            "skill_dir": str(ref.dir),
            "note": "From the next message, you can use this skill in your own conversations. Rewrite the body with write_draft_file.py.",
        }
    )
    return 0


if __name__ == "__main__":
    run_main(main)
