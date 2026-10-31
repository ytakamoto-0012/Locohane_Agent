"""スキルフォルダの内容ハッシュ・複製の規則（スキル安定化トライアウトと昇格で共有）。

トライアウト（evals/run_all.py --skill-overlay）は評価に使ったスキルの内容と
ケースのハッシュを tryout.json に残し、昇格（promote-skill の promote_helper.py）は
それを今のドラフトと照合する。ドラフトがトライアウト後に1文字でも変わっていれば
合格回数は0に戻ったものとして扱い、昇格させない（試したものと違うものを
正式スキルにしないため）。

skill-creator の scripts/_common.py は Locohane 本体と別の Python 環境で動くことが
あるため同じ規則を複製して持つ（tests/test_skill_creator_drafts.py が一致を確かめる）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# ドラフトごとの来歴（src/skill_drafts.py の DRAFT_META_FILENAME と同じ名前）。
DRAFT_META_FILENAME = "_draft_meta.json"
# ドラフトの eval ケース置き場（昇格時に evals/cases/<スキル名>/ へ移す）。
DRAFT_EVALS_DIRNAME = "evals"
# ケースフォルダ直下の入力ファイル置き場（ケースの work_dir がケースからの相対パスで指す）。
FIXTURES_DIRNAME = "fixtures"
# スキルフォルダ直下にあってもスキル本体ではないもの（来歴・ケース）。
_TOP_LEVEL_EXCLUDE = frozenset({DRAFT_META_FILENAME, DRAFT_EVALS_DIRNAME})
# どの階層にあってもスキル本体ではないもの（キャッシュ）。
_ANY_LEVEL_EXCLUDE = frozenset({"__pycache__"})


def _excluded(rel_parts: tuple[str, ...]) -> bool:
    return rel_parts[0] in _TOP_LEVEL_EXCLUDE or any(part in _ANY_LEVEL_EXCLUDE for part in rel_parts)


def tree_sha256(skill_dir: Path) -> str:
    """スキルフォルダの中身のハッシュ（直下の来歴・evals と、全階層のキャッシュは除く）。"""
    digest = hashlib.sha256()
    for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(skill_dir)
        if _excluded(rel.parts):
            continue
        digest.update(rel.as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def cases_sha256(cases_dir: Path) -> str:
    """ケースフォルダ直下の *.yaml と fixtures/ 配下（ファイル名と内容）のハッシュ。

    fixtures/ が無ければ *.yaml だけのハッシュ（fixtures/ 導入前と同じ値）になる。
    """
    digest = hashlib.sha256()
    for path in sorted(cases_dir.glob("*.yaml")):
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    fixtures = cases_dir / FIXTURES_DIRNAME
    if fixtures.is_dir():
        for path in sorted(p for p in fixtures.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            digest.update(path.relative_to(cases_dir).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    """1ファイルの内容のハッシュ（エージェント定義・設定パッチ用）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_ignore_for(root: Path):
    """root を複製する shutil.copytree の ignore（tree_sha256 と同じ範囲を除く）。

    直下の来歴・evals はスキル本体ではないが、references/evals/ のような
    下の階層の同名フォルダはスキル本体の一部なので複製する。
    """
    root_resolved = Path(root).resolve()

    def _ignore(directory: str, names: list[str]) -> set[str]:
        at_root = Path(directory).resolve() == root_resolved
        return {n for n in names if n in _ANY_LEVEL_EXCLUDE or (at_root and n in _TOP_LEVEL_EXCLUDE)}

    return _ignore
