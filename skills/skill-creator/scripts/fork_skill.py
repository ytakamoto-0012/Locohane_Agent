"""正式スキルを複製して、改善案のドラフトを作る。

skill-creator スキルの実行スクリプト（progressive disclosure 第3段階）。

    python fork_skill.py --name excel-read

正式スキル（skills/ や project_locohane_dir 配下）は書き換えず、会話のユーザー
自身のドラフト置き場 `<draft_dir>/<ユーザー名>/<name>/` へ複製する。以後の編集は
ドラフト側だけに入り、自分の会話ではドラフト（`<ユーザー名>/<name>`）として試せる。
正式スキルに evals/cases/<name>/ のケースがあれば、ドラフトの evals/ へ写す
（改善前後で同じケースを回せるようにするため）。

元にした正式スキルのハッシュを _draft_meta.json に残す。昇格（promote-skill）時に
正式スキルが変わっていれば、上書き事故を防ぐため昇格を止める。

自己完結（標準ライブラリのみ）。依存なし。
"""

from __future__ import annotations

import argparse
import os
import shutil

from _common import (
    DRAFT_EVALS_DIRNAME,
    SkillCreatorError,
    copy_ignore_for,
    draft_context,
    find_official_skill,
    now_iso,
    print_json,
    project_root,
    resolve_draft,
    run_main,
    tree_sha256,
    write_meta,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="name of the official skill you want to improve")
    args = parser.parse_args()

    base_dir = find_official_skill(args.name)
    if base_dir is None:
        raise SkillCreatorError(f"Official skill not found: {args.name}")
    ctx = draft_context()
    ref = resolve_draft(ctx, args.name, "write", must_exist=False)
    if ref.dir.exists():
        raise SkillCreatorError(f"A draft with the same name already exists: {ref.full_name} (continue by editing that draft)")

    shutil.copytree(base_dir, ref.dir, ignore=copy_ignore_for(base_dir))
    evals_dir = ref.dir / DRAFT_EVALS_DIRNAME
    evals_dir.mkdir(exist_ok=True)
    official_cases = project_root() / "evals" / "cases" / args.name
    copied_cases = []
    if official_cases.is_dir():
        for case in sorted(official_cases.glob("*.yaml")):
            shutil.copy2(case, evals_dir / case.name)
            copied_cases.append(case.name)

    write_meta(
        ref,
        {
            "kind": "improve",
            "base_skill": args.name,
            "base_root": str(base_dir.parent),
            "base_sha256": tree_sha256(base_dir),
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
            "base_dir": str(base_dir),
            "copied_cases": copied_cases,
            "note": f"From the next message, you can try your improvements as {ref.full_name} in your own conversations (the official skill stays as is).",
        }
    )
    return 0


if __name__ == "__main__":
    run_main(main)
