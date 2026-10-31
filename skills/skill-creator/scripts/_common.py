"""skill-creator の各スクリプトが共有するヘルパー。

- ドラフト置き場（ユーザー別、未検証のスキルの置き場）の解決と権限確認
- 正式スキル名との重複確認・SKILL.md frontmatter の検証
- ドラフトの来歴（_draft_meta.json）の読み書き
- 評価（実際に llama.cpp サーバーへ問い合わせる evals）のバックグラウンド
  起動と、後からのポーリング

ドラフトは `<draft_dir>/<ユーザー名>/<スキル名>/` に置く。draft_dir・
ユーザー名・他ユーザーのドラフトの扱いは、Locohane 本体が run_script の
子プロセスへ環境変数で渡す（src/tools/_subprocess_env.py）:

    AGENT_SKILL_DRAFT_DIR     ドラフト置き場（config.ini [skill_creator].draft_dir）
    AGENT_USER                会話のユーザー名（ドラフトの所有者）
    AGENT_OTHER_USERS_DRAFTS  他ユーザーのドラフトの扱い（hidden/listed/readable/full）
    AGENT_SKILL_ROOTS         正式スキルの走査ルート（os.pathsep 区切り、優先順）

これらが無い（ログインユーザーの会話以外から呼ばれた）場合、ドラフト操作は
すべてエラーにする。

自己完結（標準ライブラリのみ）。Locohane 本体（src/）は import しない
（run_script は本体とは別の Python 環境で動くことがあるため）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Locohane本体（evals/, src/ 以下。langchain/langgraph/chainlit 等に依存）を
# 動かすための Python 実行環境。config.ini の [scripts].python は
# run_script 用の別環境（このファイル自身を実行している環境）であり、
# evals.run_case 等 Locohane 本体のモジュールを import できるとは限らない
# ため区別する。python_env.bat が設定する LOCOHANE_PYTHON（本体起動時に
# 継承される）を使い、未設定なら自身の Python を使う。評価プロセスは書き込み
# ガードを外して起動する（_eval_env）ため、LLM が引数で任意の実行ファイルを
# 指定できないよう、上書き用の引数は設けない。
DEFAULT_MAIN_PYTHON = os.environ.get("LOCOHANE_PYTHON") or sys.executable

# src/skill_drafts.py と同じ名前（本体を import しないため値を複製している）。
DRAFT_META_FILENAME = "_draft_meta.json"
WORKSPACE_DIRNAME = "_workspace"
# ドラフトの evals ケース置き場（昇格時に evals/cases/<スキル名>/ へ移す）。
DRAFT_EVALS_DIRNAME = "evals"

_NAME_RE = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*$")
_NAME_MAX = 64
_DESC_MAX = 1024

# 他ユーザーのドラフトに許す操作（src/skill_drafts.py の _OTHER_USER_OPS と同じ）。
_OTHER_USER_OPS = {
    "hidden": frozenset(),
    "listed": frozenset({"list"}),
    "readable": frozenset({"list", "read"}),
    "full": frozenset({"list", "read", "exec", "write"}),
}
_OP_LABELS = {"list": "listed", "read": "read", "exec": "run", "write": "written or evaluated"}

# ハッシュ・コピーの対象から外すもの（evals/skill_tree.py と同じ規則）。直下の
# 来歴・ケースと、全階層のキャッシュはスキル本体ではない。
_TOP_LEVEL_EXCLUDE = frozenset({DRAFT_META_FILENAME, DRAFT_EVALS_DIRNAME})
_ANY_LEVEL_EXCLUDE = frozenset({"__pycache__"})


class SkillCreatorError(Exception):
    """利用者へそのまま見せるエラー（main() で stderr に出して終了コード1）。"""


def project_root() -> Path:
    """Locohane プロジェクトルートの絶対パスを返す。

    このファイルは skills/skill-creator/scripts/_common.py に置かれる
    前提で、parents[3] がプロジェクトルート（config.ini や evals/ がある
    ディレクトリ）になる。
    """
    return Path(__file__).resolve().parents[3]


def print_json(obj: dict) -> None:
    """契約どおり1行のJSONを標準出力へ書く。"""
    print(json.dumps(obj, ensure_ascii=False))


def run_main(main) -> None:
    """main() を実行し、SkillCreatorError は stderr へ出して終了コード1にする。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        sys.exit(main())
    except SkillCreatorError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# --- ドラフト置き場 ---------------------------------------------------------


@dataclass(frozen=True)
class DraftContext:
    """会話のユーザーから見たドラフト置き場。"""

    root: Path
    user: str
    mode: str

    def allowed(self, owner: str, op: str) -> bool:
        return owner == self.user or op in _OTHER_USER_OPS.get(self.mode, frozenset())


@dataclass(frozen=True)
class DraftRef:
    """1件のドラフト（`<所有者>/<スキル名>`）。"""

    owner: str
    name: str
    dir: Path

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


def draft_context() -> DraftContext:
    """環境変数からドラフト置き場を求める。無ければ SkillCreatorError。"""
    root = os.environ.get("AGENT_SKILL_DRAFT_DIR")
    user = os.environ.get("AGENT_USER")
    if not root or not user:
        raise SkillCreatorError(
            "The draft location is not available. Use skill-creator from the Locohane chat (a conversation of a logged-in user)."
        )
    return DraftContext(Path(root), user, os.environ.get("AGENT_OTHER_USERS_DRAFTS", "hidden"))


def resolve_draft(ctx: DraftContext, name: str, op: str, *, must_exist: bool = True) -> DraftRef:
    """`<スキル名>`（自分のドラフト）または `<所有者>/<スキル名>` を解決し、op の権限を確かめる。"""
    owner, _, skill = name.strip().replace("\\", "/").rpartition("/")
    owner = owner or ctx.user
    bad_owner = not owner or owner in (".", "..") or owner.startswith("_") or any(c in owner for c in ("/", "\\", ":"))
    if bad_owner or not _NAME_RE.match(skill or "-"):
        raise SkillCreatorError(f"Invalid draft name: {name!r} (specify \"skill name\" or \"username/skill name\")")
    if not ctx.allowed(owner, op):
        raise SkillCreatorError(f"Other users' drafts ({owner}/{skill}) cannot be {_OP_LABELS[op]}.")
    ref = DraftRef(owner, skill, ctx.root / owner / skill)
    if must_exist and not (ref.dir / "SKILL.md").is_file():
        raise SkillCreatorError(f"Draft not found: {ref.full_name} (check the list with list_drafts.py)")
    return ref


def workspace_dir(ctx: DraftContext, ref: DraftRef) -> Path:
    """ドラフトの評価ジョブ・結果の置き場（`<所有者>/_workspace/<スキル名>/`、昇格対象外）。"""
    ws = ctx.root / ref.owner / WORKSPACE_DIRNAME / ref.name
    (ws / "jobs").mkdir(parents=True, exist_ok=True)
    return ws


def official_roots() -> list[Path]:
    """正式スキルの走査ルート（優先順）。"""
    raw = os.environ.get("AGENT_SKILL_ROOTS", "")
    return [Path(p) for p in raw.split(os.pathsep) if p]


def find_official_skill(name: str) -> Path | None:
    """正式スキル name のフォルダ（優先順で最初に見つかったもの）。無ければ None。"""
    for root in official_roots():
        if (root / name / "SKILL.md").is_file():
            return root / name
    return None


# --- SKILL.md の検証 --------------------------------------------------------


def validate_name_description(name: object, description: object, dir_name: str | None = None) -> str | None:
    """src/skills.py の _validate() と同一ルール。違反の理由 or None。"""
    if not isinstance(name, str) or not name:
        return "name is missing or not a string"
    if len(name) > _NAME_MAX:
        return f"name exceeds {_NAME_MAX} characters"
    if "--" in name or "__" in name or "-_" in name or "_-" in name:
        return "name contains consecutive separators (- or _)"
    if not _NAME_RE.match(name):
        return "name may contain only lowercase letters, digits, hyphens and underscores, and cannot start or end with a separator"
    if dir_name is not None and name != dir_name:
        return f"name '{name}' does not match the folder name '{dir_name}'"
    if not isinstance(description, str) or not description.strip():
        return "description is missing or empty"
    if len(description) > _DESC_MAX:
        return f"description exceeds {_DESC_MAX} characters"
    return None


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """先頭の `---\\n...\\n---` ブロックから name/description/license を拾う。

    src/skills.py は PyYAML でパースするが、ここでは単純な `key: value` 行のみを
    対象にした簡易パーサーで代用する（事前検証が目的で、最終的な合否は
    Locohane 本体の走査が唯一の正）。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    result: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key in ("name", "description", "license") and value:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key] = value
    return result


def validate_skill_md(skill_dir: Path) -> str | None:
    """skill_dir/SKILL.md の frontmatter を検証する。違反の理由 or None。"""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return f"SKILL.md not found: {skill_md}"
    fm = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    if fm is None:
        return "frontmatter (the --- block at the top) not found"
    return validate_name_description(fm.get("name"), fm.get("description"), skill_dir.name)


# --- 来歴（_draft_meta.json） ------------------------------------------------


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_meta(ref: DraftRef) -> dict:
    path = ref.dir / DRAFT_META_FILENAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_meta(ref: DraftRef, meta: dict) -> None:
    (ref.dir / DRAFT_META_FILENAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def touch_meta(ctx: DraftContext, ref: DraftRef, **updates) -> dict:
    """ドラフトの中身（スキル本体・ケース）を変更したときに呼び、meta を更新して返す。

    最終編集者・更新日時（と updates）を反映するほか、次の2つを行う:
    - スキル安定化トライアウトの記録（tryouts）を空にする。中身が変われば
      それまでの合格回数は今のドラフトの根拠にならないため、0から数え直す。
    - 差し戻し（promote-skill が status=returned にする）中なら status を
      draft に戻す。修正すれば再び昇格の候補になる（不採用の rejected は戻さない）。
    """
    meta = read_meta(ref)
    meta.update(updates)
    meta["last_editor"] = ctx.user
    meta["updated_at"] = now_iso()
    if meta.get("tryouts"):
        meta["tryouts"] = []
        meta["tryouts_reset_at"] = meta["updated_at"]
    if meta.get("status") == "returned":
        meta["status"] = "draft"
        meta["resubmitted_at"] = meta["updated_at"]
    write_meta(ref, meta)
    return meta


def _excluded(rel_parts: tuple[str, ...]) -> bool:
    return rel_parts[0] in _TOP_LEVEL_EXCLUDE or any(part in _ANY_LEVEL_EXCLUDE for part in rel_parts)


def tree_sha256(skill_dir: Path) -> str:
    """スキルフォルダの中身のハッシュ（直下の来歴・evals と、全階層のキャッシュは除く）。

    改善案ドラフトの元にした正式スキルが昇格までの間に変わっていないかの確認と、
    トライアウトで評価した内容が今のドラフトと同じかの確認に使う
    （evals/skill_tree.py の tree_sha256 と同じ規則）。
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(skill_dir)
        if _excluded(rel.parts):
            continue
        digest.update(rel.as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def cases_sha256(cases_dir: Path) -> str:
    """ケースフォルダ直下の *.yaml と fixtures/ 配下のハッシュ（evals/skill_tree.py の cases_sha256 と同じ規則）。"""
    digest = hashlib.sha256()
    for path in sorted(cases_dir.glob("*.yaml")):
        digest.update(path.name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    fixtures = cases_dir / "fixtures"
    if fixtures.is_dir():
        for path in sorted(p for p in fixtures.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            digest.update(path.relative_to(cases_dir).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def copy_ignore_for(root: Path):
    """root を複製する shutil.copytree の ignore（tree_sha256 と同じ範囲を除く）。

    直下の来歴・evals だけを除き、references/evals/ のような下の階層の同名
    フォルダはスキル本体の一部として複製する。
    """
    root_resolved = Path(root).resolve()

    def _ignore(directory: str, names: list[str]) -> set[str]:
        at_root = Path(directory).resolve() == root_resolved
        return {n for n in names if n in _ANY_LEVEL_EXCLUDE or (at_root and n in _TOP_LEVEL_EXCLUDE)}

    return _ignore


# --- 評価のバックグラウンド実行 ---------------------------------------------


def _eval_env() -> dict[str, str]:
    """評価プロセス（evals/run_all.py → evals.run_case）へ渡す環境変数。

    run_script は子プロセスへ書き込みガード（PYTHONPATH 先頭の sitecustomize、
    src/tools/_subprocess_env.py）を注入するが、評価プロセスは固定の評価コード
    （LLM が書いたコードではない）で、ログ・結果・OS の一時フォルダへ書く必要が
    あるため、このガードを外して起動する。ガード用フォルダは run_script の終了時に
    消えるため、残しておくと評価中に挙動が変わる問題もある。評価対象エージェント
    自身のツール実行には、評価プロセス内で改めて同じガードがかかる。
    """
    env = dict(os.environ)
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and not Path(p).name.startswith("agent_fs_guard_")]
    if parts:
        env["PYTHONPATH"] = os.pathsep.join(parts)
    else:
        env.pop("PYTHONPATH", None)
    return env


def start_background(cmd: list[str], workspace: Path, extra_meta: dict | None = None) -> dict:
    """cmd をバックグラウンドで起動し、ポーリング用のジョブ情報を返す。

    呼び出し元プロセス（run_script 経由で起動された本スクリプト自体）が
    終了しても子プロセスが生き続けるよう、Windows では新しいプロセス
    グループとして起動する。

    Returns:
        `{"job_id", "pid", "log_path", "status": "started"}`。
    """
    job_id = uuid.uuid4().hex[:12]
    job_dir = workspace / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = job_dir / "output.log"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root()),
            env=_eval_env(),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    meta = {"job_id": job_id, "pid": proc.pid, "cmd": cmd, "log_path": str(log_path), **(extra_meta or {})}
    (job_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"job_id": job_id, "pid": proc.pid, "log_path": str(log_path), "status": "started"}


def is_process_alive(pid: int) -> bool:
    """Windows の tasklist で PID の生存を確認する（追加依存ライブラリ不要）。"""
    # tasklist の出力は OEM コードページ（言語により異なる）。UTF-8 モードでも落ちないよう明示する。
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, encoding="oem", errors="replace"
    )
    return str(pid) in result.stdout


def load_job(workspace: Path, job_id: str) -> dict:
    """ジョブの meta を返す。無ければ SkillCreatorError。"""
    meta_path = workspace / "jobs" / job_id / "meta.json"
    if not meta_path.is_file():
        raise SkillCreatorError(f"Job not found: {job_id}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def log_tail(job: dict, lines: int = 40) -> str:
    path = Path(job["log_path"])
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return "\n".join(text.splitlines()[-lines:])


def latest_results_dir(results_root: Path) -> Path | None:
    """evals/run_all.py --results-dir の直下にできた最新のタイムスタンプフォルダ。"""
    if not results_root.is_dir():
        return None
    dirs = sorted(p for p in results_root.iterdir() if p.is_dir() and (p / "results.json").is_file())
    return dirs[-1] if dirs else None
