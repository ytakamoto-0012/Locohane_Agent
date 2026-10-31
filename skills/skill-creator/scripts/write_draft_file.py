"""ドラフトスキルの中のファイル（SKILL.md・scripts/・references/ 等）を書く。

skill-creator スキルの実行スクリプト（progressive disclosure 第3段階）。

    # ファイル全体を書く（新規作成・上書き）
    python write_draft_file.py --name my-skill --path SKILL.md --content "...全文..."
    # 長い内容は、作業フォルダに書いたファイルから写す
    python write_draft_file.py --name my-skill --path scripts/run.py --content-file <作業フォルダのファイル>
    # 一部だけ置き換える（old がファイル内で1か所だけ一致する必要がある）
    python write_draft_file.py --name my-skill --path SKILL.md --old "古い文" --new "新しい文"

書き込み先はドラフトのスキルフォルダの中だけ（`..`・絶対パス・_draft_meta.json は拒否）。
SKILL.md を書いた後は frontmatter を検証し、違反していれば書き込みを取り消す
（不正な SKILL.md は Locohane がスキルとして読み込まないため）。

自己完結（標準ライブラリのみ）。依存なし。
"""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath

from _common import (
    DRAFT_META_FILENAME,
    SkillCreatorError,
    draft_context,
    print_json,
    resolve_draft,
    run_main,
    touch_meta,
    validate_skill_md,
)


def _target_path(skill_dir: Path, raw: str) -> Path:
    rel = PurePosixPath(raw.replace("\\", "/"))
    if not raw.strip() or rel.is_absolute() or ".." in rel.parts or ":" in raw:
        raise SkillCreatorError(f"Specify --path relative to the skill folder: {raw!r}")
    if rel.parts[0] == DRAFT_META_FILENAME:
        raise SkillCreatorError(f"{DRAFT_META_FILENAME} cannot be rewritten.")
    target = (skill_dir / Path(*rel.parts)).resolve()
    if not target.is_relative_to(skill_dir.resolve()):
        raise SkillCreatorError(f"Cannot write outside the skill folder: {raw!r}")
    return target


def _new_text(target: Path, args: argparse.Namespace) -> str:
    modes = [args.content is not None, args.content_file is not None, args.old is not None]
    if sum(modes) != 1:
        raise SkillCreatorError("Specify exactly one of --content / --content-file / --old (with --new).")
    if args.content is not None:
        return args.content
    if args.content_file is not None:
        source = Path(args.content_file)
        if not source.is_file():
            raise SkillCreatorError(f"--content-file not found: {source}")
        try:
            return source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise SkillCreatorError(f"Cannot read --content-file: {e}") from e
    if args.new is None:
        raise SkillCreatorError("Specify --new together with --old.")
    if not target.is_file():
        raise SkillCreatorError(f"The file to replace in does not exist: {target.name} (use --content to write the whole file)")
    text = target.read_text(encoding="utf-8")
    count = text.count(args.old)
    if count != 1:
        raise SkillCreatorError(
            f"--old matched {count} places in the file (it must match exactly one place). Include the surrounding text."
        )
    return text.replace(args.old, args.new, 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="draft name (for your own drafts, just the skill name)")
    parser.add_argument("--path", required=True, help="path relative to the skill folder (e.g. SKILL.md, scripts/run.py)")
    parser.add_argument("--content", help="content of the whole file")
    parser.add_argument("--content-file", help="file to copy the content from (such as long content written in the working folder)")
    parser.add_argument("--old", help="the part to replace (must match exactly one place in the file)")
    parser.add_argument("--new", help="content that replaces --old")
    args = parser.parse_args()

    ctx = draft_context()
    ref = resolve_draft(ctx, args.name, "write")
    target = _target_path(ref.dir, args.path)
    text = _new_text(target, args)

    previous = target.read_bytes() if target.is_file() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if target == (ref.dir / "SKILL.md").resolve():
        error = validate_skill_md(ref.dir)
        if error:
            if previous is None:
                target.unlink()
            else:
                target.write_bytes(previous)
            raise SkillCreatorError(f"The write was undone because the frontmatter of SKILL.md is invalid: {error}")
    touch_meta(ctx, ref)
    print_json({"draft": ref.full_name, "path": str(target), "chars": len(text)})
    return 0


if __name__ == "__main__":
    run_main(main)
