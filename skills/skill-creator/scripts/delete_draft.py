"""ドラフトスキル（またはその中の1ファイル）を削除する。

skill-creator スキルの実行スクリプト（progressive disclosure 第3段階）。

    # ドラフトごと削除する（評価結果の置き場も消す）
    python delete_draft.py --name my-skill
    # ドラフトの中の1ファイルだけ削除する
    python delete_draft.py --name my-skill --path references/old.md

正式スキルには影響しない。SKILL.md と _draft_meta.json は単体では消せない
（消したいときはドラフトごと削除する）。

自己完結（標準ライブラリのみ）。依存なし。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import PurePosixPath

from _common import (
    DRAFT_META_FILENAME,
    WORKSPACE_DIRNAME,
    SkillCreatorError,
    draft_context,
    print_json,
    resolve_draft,
    run_main,
    touch_meta,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="draft name (for your own drafts, just the skill name)")
    parser.add_argument("--path", help="path of the file to delete, relative to the skill folder (if omitted, the whole draft is deleted)")
    args = parser.parse_args()

    ctx = draft_context()
    ref = resolve_draft(ctx, args.name, "write")
    if args.path is None:
        shutil.rmtree(ref.dir)
        shutil.rmtree(ctx.root / ref.owner / WORKSPACE_DIRNAME / ref.name, ignore_errors=True)
        print_json({"deleted": ref.full_name, "note": "It will no longer appear in the skill list from the next message."})
        return 0

    rel = PurePosixPath(args.path.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or ":" in args.path or not rel.parts:
        raise SkillCreatorError(f"Specify --path relative to the skill folder: {args.path!r}")
    if rel.as_posix() in ("SKILL.md", DRAFT_META_FILENAME):
        raise SkillCreatorError(f"{rel.as_posix()} cannot be deleted on its own (to delete the whole draft, omit --path).")
    target = ref.dir.joinpath(*rel.parts)
    if not target.is_file():
        raise SkillCreatorError(f"File not found: {rel.as_posix()}")
    target.unlink()
    touch_meta(ctx, ref)
    print_json({"draft": ref.full_name, "deleted_file": rel.as_posix()})
    return 0


if __name__ == "__main__":
    run_main(main)
