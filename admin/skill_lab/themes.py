"""研究テーマの置き場・ファイル操作・状態の管理。

テーマは対象インスタンス自身のデータフォルダ（`<common_data_dir>/skill_lab/themes/<テーマID>/`）に
置く。組織別・役割別のインスタンスごとに置き場そのものが分かれるため、テーマが混ざらない。

    theme.json          名前・状態・担当の研究室・資産の入手元・反復の記録・タスク
    spec.md             仕様（Purpose・When to use・Expected output・Must not do の見出しで書く）
    config_patch.json   評価と昇格で足す設定（evals/config_patch.py）
    assets/skills/<名前>/      開発中のスキル
    assets/agents/<名前>.md    開発中のサブエージェント
    cases/*.yaml               ケース（複数の資産にまたがってよい）
    cases/fixtures/<名前>/     ケースの入力ファイル（ケースの work_dir: fixtures/<名前>）
    baseline/                  取り込み・複製した時点の資産（レビューの差分の基準）
    runs/<n>/                  評価・AI 修正・AI judge の記録

管理ツール（HTTP リクエストごとのスレッド）とワーカー（別プロセス）が同じ theme.json を
書き換えるため、読み・変更・書き込みはテーマごとのロックファイルで排他し、書き込みは
一時ファイルからの置き換えで行う（update()）。
"""

from __future__ import annotations

import contextlib
import json
import msvcrt
import os
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

import yaml

from evals.case_schema import load_case
from evals.skill_tree import FIXTURES_DIRNAME, cases_sha256, copy_ignore_for, file_sha256, tree_sha256
from admin.i18n import t as _t

THEME_FILENAME = "theme.json"
SPEC_FILENAME = "spec.md"
CONFIG_PATCH_FILENAME = "config_patch.json"
ASSETS_DIRNAME = "assets"
CASES_DIRNAME = "cases"
BASELINE_DIRNAME = "baseline"
RUNS_DIRNAME = "runs"
_LOCK_FILENAME = "theme.lock"

ASSET_TYPES = ("skills", "agents")

# テーマの状態。
STATUS_DRAFT = "draft"  # 人が準備中（ループは動いていない）
STATUS_QUEUED = "queued"  # ループ（またはトライアウト）の開始待ち
STATUS_RUNNING = "running"
STATUS_REVIEW = "review"  # トライアウトに全回合格し、人の確認待ち
STATUS_EXHAUSTED = "exhausted"  # 反復回数・時間の上限に達した
STATUS_ERROR = "error"
STATUS_STOPPED = "stopped"  # 人が止めた
STATUS_PROMOTED = "promoted"
STATUS_RETURNED = "returned"  # 取り込み元のドラフトへ差し戻した
STATUS_CANCELLED = "cancelled"
ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})
CLOSED_STATUSES = frozenset({STATUS_PROMOTED, STATUS_RETURNED, STATUS_CANCELLED})

# スキル・エージェント名の規則（src/skills.py・src/agent_types.py の _validate と同じ）。
NAME_RE = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*$")
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_THEME_ID_RE = re.compile(r"^[0-9]{14}_[0-9a-f]{4}$")
_FIXTURE_PART_RE = re.compile(r"^[^\\/:*?\"<>|]+$")
# Windows の予約デバイス名（拡張子付きでもデバイスを指す）。
_RESERVED_NAME_RE = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)


class ThemeError(Exception):
    """研究テーマの操作失敗（不正な名前・パス・状態等）。"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 置き場
# ---------------------------------------------------------------------------


def themes_root(common_data_dir: Path) -> Path:
    """インスタンスの研究テーマ置き場（`<common_data_dir>/skill_lab/themes`）。"""
    return common_data_dir / "skill_lab" / "themes"


def theme_dir(root: Path, theme_id: str) -> Path:
    """テーマ ID からフォルダを求める（ID の形式を確かめ、置き場の外を指させない）。"""
    if not _THEME_ID_RE.match(theme_id or ""):
        raise ThemeError(_t("テーマ ID が不正です: {0!r}", theme_id))
    path = root / theme_id
    if not (path / THEME_FILENAME).is_file():
        raise ThemeError(_t("テーマが見つかりません: {0}", theme_id))
    return path


def list_theme_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and _THEME_ID_RE.match(p.name) and (p / THEME_FILENAME).is_file())


# ---------------------------------------------------------------------------
# theme.json の排他つき読み書き
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    lock_path = directory / _LOCK_FILENAME
    if not lock_path.exists():
        lock_path.write_bytes(b"0")
    with open(lock_path, "r+b") as f:
        deadline = time.monotonic() + 30
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise ThemeError(_t("テーマのロックを取得できません（他の処理が使用中）: {0}", directory.name)) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    # Windows では、他のプロセス（画面のポーリング等）が読んでいる間は置き換えられない
    # （PermissionError）。読み取りは一瞬なので、少し待って置き換え直す。
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 39:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def read(directory: Path) -> dict:
    path = directory / THEME_FILENAME
    # 置き換えの瞬間に読むと、Windows では開けない（PermissionError）ことがあるため少し待って読み直す。
    for attempt in range(40):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (PermissionError, json.JSONDecodeError):
            if attempt == 39:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")


def read_or_none(directory: Path) -> dict | None:
    """一覧用の read()。theme.json が壊れていれば None（1件のために一覧全体を止めない）。"""
    try:
        return read(directory)
    except (OSError, ValueError):
        return None


def update(directory: Path, fn: Callable[[dict], object]) -> dict:
    """theme.json を排他して読み、fn(data) で書き換えて保存する（fn の戻り値は捨てる）。"""
    with _locked(directory):
        data = read(directory)
        fn(data)
        data["updated_at"] = now_iso()
        _atomic_write_text(directory / THEME_FILENAME, json.dumps(data, ensure_ascii=False, indent=2))
        return data


def add_history(data: dict, actor: str, action: str, detail: str = "") -> None:
    data.setdefault("history", []).append({"at": now_iso(), "actor": actor, "action": action, "detail": detail})


# ---------------------------------------------------------------------------
# 作成
# ---------------------------------------------------------------------------


def create_theme(root: Path, *, instance: str, title: str, lab: str | None, actor: str) -> Path:
    title = (title or "").strip()
    if not title:
        raise ThemeError(_t("テーマ名を入力してください。"))
    root.mkdir(parents=True, exist_ok=True)
    theme_id = f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(2)}"
    directory = root / theme_id
    directory.mkdir()
    for sub in (f"{ASSETS_DIRNAME}/skills", f"{ASSETS_DIRNAME}/agents", f"{CASES_DIRNAME}/{FIXTURES_DIRNAME}", RUNS_DIRNAME):
        (directory / sub).mkdir(parents=True)
    (directory / SPEC_FILENAME).write_text(
        f"# {title}\n\n" + "\n".join(f"## {heading}\n" for heading in SPEC_HEADINGS),
        encoding="utf-8",
    )
    (directory / CONFIG_PATCH_FILENAME).write_text("{}\n", encoding="utf-8")
    data = {
        "id": theme_id,
        "title": title,
        "instance": instance,
        "lab": lab,
        "status": STATUS_DRAFT,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "created_by": actor,
        "sources": {"skills": {}, "agents": {}},
        "iterations": [],
        "tryout": None,
        "loop": None,
        "tasks": [],
        "history": [],
    }
    add_history(data, actor, "create", title)
    _atomic_write_text(directory / THEME_FILENAME, json.dumps(data, ensure_ascii=False, indent=2))
    return directory


# ---------------------------------------------------------------------------
# パスの制限（テーマ内の決まった場所だけを読み書きさせる）
# ---------------------------------------------------------------------------


def _check_part(part: str) -> None:
    if part in ("", ".", "..") or not _FIXTURE_PART_RE.match(part) or part.endswith((".", " ")) or _RESERVED_NAME_RE.match(part):
        raise ThemeError(_t("パスに使えない名前が含まれています: {0!r}", part))


def resolve_path(directory: Path, rel: str, *, for_write: bool = False) -> Path:
    """テーマ内の相対パスを検証して絶対パスにする。

    読み書きできるのは spec.md・config_patch.json・assets/skills/<名前>/...・
    assets/agents/<名前>.md・cases/<ID>.yaml・cases/fixtures/... だけ。
    theme.json・baseline/・runs/ は API からは書き換えさせない（読み取りは別の関数で行う）。
    """
    rel = (rel or "").replace("\\", "/").strip().strip("/")
    if not rel or rel.startswith("/") or ":" in rel:
        raise ThemeError(_t("パスが不正です: {0!r}", rel))
    parts = rel.split("/")
    for part in parts:
        _check_part(part)
    ok = False
    if parts in (["spec.md"], ["config_patch.json"]):
        ok = True
    elif parts[0] == ASSETS_DIRNAME and len(parts) >= 3 and parts[1] == "skills":
        ok = bool(NAME_RE.match(parts[2])) and "__pycache__" not in parts
    elif parts[0] == ASSETS_DIRNAME and len(parts) == 3 and parts[1] == "agents":
        ok = parts[2].endswith(".md") and bool(NAME_RE.match(parts[2][:-3]))
    elif parts[0] == CASES_DIRNAME and len(parts) == 2:
        ok = parts[1].endswith(".yaml") and bool(_CASE_ID_RE.match(parts[1][:-5]))
    elif parts[0] == CASES_DIRNAME and len(parts) >= 3 and parts[1] == FIXTURES_DIRNAME:
        ok = True
    if not ok:
        raise ThemeError(_t("このパスは扱えません: {0}", rel))
    path = (directory / Path(*parts)).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ThemeError(_t("テーマの外を指すパスです: {0}", rel))
    if for_write and parts[0] == ASSETS_DIRNAME and parts[1] == "skills" and len(parts) == 3:
        raise ThemeError(_t("スキルのフォルダそのものではなく、その中のファイルを指定してください。"))
    if for_write and parts[0] == CASES_DIRNAME and len(parts) == 3:
        # 入力ファイルはフォルダ（ケースの work_dir: fixtures/<フォルダ>）の中に置く。
        raise ThemeError(_t("入力ファイルは cases/fixtures/<フォルダ名>/ の中に置いてください。"))
    if for_write and path.is_dir():
        raise ThemeError(_t("同じ名前のフォルダがあります: {0}", rel))
    return path


# ---------------------------------------------------------------------------
# ファイル一覧・読み書き
# ---------------------------------------------------------------------------


def list_files(directory: Path) -> list[dict]:
    """編集できるファイルの一覧（相対パス・サイズ・テキストかどうか）。"""
    result = []
    candidates = [directory / SPEC_FILENAME, directory / CONFIG_PATCH_FILENAME]
    for sub in (f"{ASSETS_DIRNAME}/skills", f"{ASSETS_DIRNAME}/agents", CASES_DIRNAME):
        base = directory / sub
        if base.is_dir():
            candidates += sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    for path in candidates:
        if not path.is_file():
            continue
        rel = path.relative_to(directory).as_posix()
        result.append({"path": rel, "size": path.stat().st_size, "text": _is_text(path)})
    return result


def _is_text(path: Path) -> bool:
    try:
        path.read_bytes()[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def read_text(directory: Path, rel: str) -> str:
    path = resolve_path(directory, rel)
    if not path.is_file():
        raise ThemeError(_t("ファイルが見つかりません: {0}", rel))
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ThemeError(_t("テキストファイルではありません: {0}", rel)) from e


def write_text(directory: Path, rel: str, content: str, *, actor: str) -> None:
    """ファイルを書き、内容が変わったことを記録する（トライアウトの結果は無効になる）。"""
    path = resolve_path(directory, rel, for_write=True)
    validate_content(rel, content)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, content)
    mark_changed(directory, actor, f"edit: {rel}")


def write_bytes(directory: Path, rel: str, content: bytes, *, actor: str) -> None:
    path = resolve_path(directory, rel, for_write=True)
    if not rel.replace("\\", "/").startswith(f"{CASES_DIRNAME}/{FIXTURES_DIRNAME}/"):
        raise ThemeError(_t("ファイルのアップロードは cases/fixtures/ 配下にだけできます。"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    mark_changed(directory, actor, f"input file: {rel}")


def delete_path(directory: Path, rel: str, *, actor: str) -> None:
    path = resolve_path(directory, rel)
    parts = rel.replace("\\", "/").strip().strip("/").split("/")
    if parts in (["spec.md"], ["config_patch.json"]):
        raise ThemeError(_t("{0} は削除できません（中身を空にして保存してください）。", rel))
    if parts[0] == ASSETS_DIRNAME and (parts[1] == "agents" or len(parts) == 3 or parts[3:] == ["SKILL.md"]):
        # 研究対象の本体だけを消すと、入手元の記録（取り込んだドラフト等）と食い違う。
        raise ThemeError(_t("研究対象そのものを除くには、研究対象の「外す」を使ってください。"))
    if not path.exists():
        raise ThemeError(_t("見つかりません: {0}", rel))
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    mark_changed(directory, actor, f"delete: {rel}")


def mark_changed(directory: Path, actor: str, detail: str) -> None:
    """資産・ケース・設定パッチが変わったら、それまでのトライアウトは根拠にならない（0回から）。"""

    def _apply(data: dict) -> None:
        if data.get("tryout"):
            data["tryout"] = None
        if data.get("status") == STATUS_REVIEW:
            data["status"] = STATUS_DRAFT
        add_history(data, actor, "change", detail)

    update(directory, _apply)


def validate_content(rel: str, content: str) -> None:
    """書き込む内容の形式を確かめる（SKILL.md・エージェント定義・ケース・設定パッチ・スクリプト）。

    壊れたものを保存すると評価が全件エラーになり、AI 修正の手がかりにもならないため、
    保存の時点で止める。
    """
    from evals.config_patch import ConfigPatchError, validate_patch

    parts = rel.replace("\\", "/").strip().strip("/").split("/")
    if parts[0] == ASSETS_DIRNAME and parts[1] == "skills" and parts[-1] == "SKILL.md" and len(parts) == 4:
        error = validate_skill_md(content, parts[2])
    elif parts[0] == ASSETS_DIRNAME and parts[1] == "agents":
        error = validate_agent_md(content, parts[2][:-3])
    elif parts[0] == CASES_DIRNAME and len(parts) == 2:
        error = validate_case_yaml(content, parts[1][:-5])
    elif parts == ["config_patch.json"]:
        try:
            validate_patch(json.loads(content or "{}"))
            error = None
        except (json.JSONDecodeError, ConfigPatchError) as e:
            error = f"config_patch.json: {e}"
    elif parts[0] == ASSETS_DIRNAME and parts[-1].endswith(".py"):
        # 入力ファイル（cases/fixtures/）の .py は「直させる題材」として壊れていてもよいため確かめない。
        error = validate_python(content, rel)
    else:
        error = None
    if error:
        raise ThemeError(error)


def validate_skill_md(content: str, dir_name: str) -> str | None:
    from src.skills import _parse_frontmatter, _validate

    fm = _parse_frontmatter(content)
    if fm is None:
        return "SKILL.md has no frontmatter (name and description enclosed in ---) at the top."
    error = _validate(fm.get("name"), fm.get("description"), dir_name)
    return f"SKILL.md: {error}" if error else None


def validate_agent_md(content: str, file_stem: str, known_tools: list[str] | None = None) -> str | None:
    from src.agent_types import _parse_frontmatter, _parse_tools_field, _validate

    parsed = _parse_frontmatter(content)  # (frontmatter, 本文) か None
    if parsed is None:
        return "The agent definition has no frontmatter (name, description and tools enclosed in ---) at the top."
    fm = parsed[0]
    error = _validate(fm.get("name"), fm.get("description"), file_stem)
    if error:
        return f"Agent definition: {error}"
    if known_tools is not None:
        tools = _parse_tools_field(fm.get("tools")) or []
        unknown = [t for t in tools if t not in known_tools]
        if unknown:
            return f"Agent definition: tools has tool names that do not exist: {unknown}"
    return None


def validate_case_yaml(content: str, case_id: str) -> str | None:
    import tempfile

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return f"Case {case_id}: cannot read it as YAML: {e}"
    if isinstance(data, dict) and data.get("id") is not None and not isinstance(data.get("id"), str):
        return f"Case {case_id}: make id a string (quote an id made only of digits, like '{case_id}')."
    if isinstance(data, dict) and data.get("id") not in (None, case_id):
        return f"Case {case_id}: make id ({data.get('id')}) the same as the file name."
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{case_id}.yaml"
        path.write_text(content, encoding="utf-8")
        try:
            case = load_case(path)
        except (ValueError, TypeError) as e:
            return f"Case {case_id}: {str(e).replace(str(path), path.name)}"
    if not case_has_condition(case):
        # expect の項目がすべて空で judge も無いケースは何をしても合格になり、AI 修正の抜け道にもなる。
        return f"Case {case_id}: add at least one way to decide passing (a condition in expect, or judge)."
    return None


def case_has_condition(case) -> bool:
    """ケースに合否を決める条件が1つ以上あるか（evals.case_schema.EvalCase）。"""
    if case.judge and case.judge.strip():
        return True
    e = case.expect
    if e is None:
        return False
    return bool(e.tool_called_any or e.tool_not_called or e.tool_call_args_contains or e.response_contains or e.response_not_contains)


def validate_python(content: str, rel: str) -> str | None:
    import ast

    try:
        ast.parse(content)
    except SyntaxError as e:
        return f"{rel}: Python syntax error (line {e.lineno}): {e.msg}"
    return None


# ---------------------------------------------------------------------------
# 資産
# ---------------------------------------------------------------------------


def asset_path(directory: Path, asset_type: str, name: str) -> Path:
    if asset_type not in ASSET_TYPES:
        raise ThemeError(_t("研究対象の種類が不正です: {0!r}", asset_type))
    if not NAME_RE.match(name or "") or len(name) > 64:
        raise ThemeError(_t("名前は小文字英数字・ハイフン・アンダースコアのみにしてください: {0!r}", name))
    base = directory / ASSETS_DIRNAME / asset_type
    return base / name if asset_type == "skills" else base / f"{name}.md"


def list_assets(directory: Path) -> dict[str, list[str]]:
    base = directory / ASSETS_DIRNAME
    skills = sorted(p.name for p in (base / "skills").iterdir() if (p / "SKILL.md").is_file()) if (base / "skills").is_dir() else []
    agents = sorted(p.stem for p in (base / "agents").glob("*.md")) if (base / "agents").is_dir() else []
    return {"skills": skills, "agents": agents}


def add_asset_copy(directory: Path, asset_type: str, name: str, origin: Path, *, source: dict, actor: str) -> None:
    """既存のスキル（フォルダ）・エージェント（.md）を複製して資産にする（baseline にも写す）。"""
    target = asset_path(directory, asset_type, name)
    if target.exists():
        raise ThemeError(_t("同じ名前の研究対象が既にあります: {0}", name))
    baseline = directory / BASELINE_DIRNAME / ASSETS_DIRNAME / asset_type / target.name
    if asset_type == "skills":
        if not (origin / "SKILL.md").is_file():
            raise ThemeError(_t("スキルが見つかりません: {0}", origin))
        shutil.copytree(origin, target, ignore=copy_ignore_for(origin))
        baseline.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(target, baseline, dirs_exist_ok=True)
        source = {**source, "origin_sha256": tree_sha256(origin)}
    else:
        if not origin.is_file():
            raise ThemeError(_t("エージェント定義が見つかりません: {0}", origin))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
        baseline.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, baseline)
        source = {**source, "origin_sha256": file_sha256(origin)}

    def _apply(data: dict) -> None:
        data["sources"][asset_type][name] = source
        data["tryout"] = None
        add_history(data, actor, "add_asset", f"{asset_type}/{name}（{source.get('kind')}）")

    update(directory, _apply)


def add_asset_new(directory: Path, asset_type: str, name: str, content: str, *, actor: str, description: str = "") -> None:
    """新しいスキル（SKILL.md だけ）・エージェント（.md）を作る。"""
    target = asset_path(directory, asset_type, name)
    if target.exists():
        raise ThemeError(_t("同じ名前の研究対象が既にあります: {0}", name))
    if asset_type == "skills":
        error = validate_skill_md(content, name)
        if error:
            raise ThemeError(error)
        (target / "scripts").mkdir(parents=True)
        (target / "SKILL.md").write_text(content, encoding="utf-8")
    else:
        error = validate_agent_md(content, name)
        if error:
            raise ThemeError(error)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _apply(data: dict) -> None:
        data["sources"][asset_type][name] = {"kind": "new", "description": description}
        data["tryout"] = None
        add_history(data, actor, "add_asset", f"{asset_type}/{name} (new)")

    update(directory, _apply)


def remove_asset(directory: Path, asset_type: str, name: str, *, actor: str) -> None:
    target = asset_path(directory, asset_type, name)
    if not target.exists():
        raise ThemeError(_t("研究対象が見つかりません: {0}", name))
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    baseline = directory / BASELINE_DIRNAME / ASSETS_DIRNAME / asset_type / target.name
    if baseline.is_dir():
        shutil.rmtree(baseline)
    elif baseline.is_file():
        baseline.unlink()

    def _apply(data: dict) -> None:
        data["sources"][asset_type].pop(name, None)
        data["tryout"] = None
        add_history(data, actor, "remove_asset", f"{asset_type}/{name}")

    update(directory, _apply)


# ---------------------------------------------------------------------------
# ケース
# ---------------------------------------------------------------------------


def list_cases(directory: Path) -> list[dict]:
    result = []
    for path in sorted((directory / CASES_DIRNAME).glob("*.yaml")):
        item = {"id": path.stem, "path": f"{CASES_DIRNAME}/{path.name}"}
        try:
            case = load_case(path)
            item.update(
                {
                    "turns": case.turns,
                    "judge": case.judge,
                    "has_expect": case.expect is not None,
                    "work_dir": case.work_dir,
                    "notes": case.notes,
                }
            )
            if not case_has_condition(case):
                item["error"] = "There is no way to decide passing (an expect condition or judge)"
            elif case.work_dir and not _work_dir_exists(directory, case.work_dir):
                item["error"] = f"The folder of files to use, {case.work_dir}, does not exist"
        except Exception as e:  # noqa: BLE001 - 一覧では壊れたケースも理由付きで出す
            item["error"] = str(e)
        result.append(item)
    return result


# 仕様（spec.md）の決まった見出し。画面（skill_lab.js の SPEC_FIELDS）と AI の下書き（drafting.py）も同じ見出しを使う。
# 見出しと「TODO:」は英語圏でも打ちやすい半角にする。日本語の旧見出し・「要確認:」で書かれた仕様も読めるようにしている。
SPEC_HEADINGS = ("Purpose", "When to use", "Expected output", "Must not do")
_SPEC_PURPOSE_HEADINGS = ("Purpose", "目的")
_SPEC_HEADING = re.compile(r"^#{1,6}\s*(.*?)\s*$")
# AI の下書きが資料から読み取れなかった点の印（旧形式の「要確認:」も同じに扱う）。
_SPEC_UNSURE = re.compile(r"^(?:TODO\b|要確認)\s*[:：]?", re.IGNORECASE)
# 節の終わり（# か ## の見出し）。### 以下の小見出しは節の本文に含める。
_SPEC_SECTION_END = re.compile(r"^#{1,2}(?!#)")


def _spec_section_lines(text: str, heading_prefix: str) -> list[str] | None:
    """仕様の `## <見出し>` の本文の行（見出しが無ければ None）。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _SPEC_HEADING.match(line)
        if line.startswith("##") and m and m.group(1).startswith(heading_prefix):
            body = []
            for rest in lines[i + 1 :]:
                if _SPEC_SECTION_END.match(rest):
                    break
                body.append(rest)
            return body
    return None


def spec_filled(directory: Path) -> bool:
    """仕様の Purpose（目的）が書かれているか。

    仕様は AI 修正と AI judge の正解の基準で、無いと AI がケースに通るだけの直し方をしても
    気づけないため、開始の条件にする。見出しだけ・AI の下書きが「TODO:」しか書けなかった行は
    書かれていないとみなす。Purpose の見出しが無い自由形式の仕様は、見出し以外の行があればよい。
    """
    try:
        text = (directory / SPEC_FILENAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    body = next((b for h in _SPEC_PURPOSE_HEADINGS if (b := _spec_section_lines(text, h)) is not None), None)
    if body is None:
        body = [line for line in text.splitlines() if not line.startswith("#")]
    return any(line.strip() and not _SPEC_UNSURE.match(line.strip()) for line in body)


def start_missing(directory: Path) -> list[str]:
    """ループ・トライアウトを始めるのに足りないもの（画面の ①②③ の言葉で）。"""
    missing = []
    assets = list_assets(directory)
    if not assets["skills"] and not assets["agents"]:
        missing.append(_t("① 研究対象（スキルかサブエージェントを1つ以上）"))
    if not spec_filled(directory):
        missing.append(_t("② やってほしいことの「目的」"))
    cases = list_cases(directory)
    if not cases:
        missing.append(_t("③ 試験問題（1問以上）"))
    broken = [c["id"] for c in cases if c.get("error")]
    if broken:
        # 読めないケースがあると評価がエラーになり、AI 修正の手がかりにもならない。
        missing.append(_t("③ 試験問題の直し（読めない・条件や使うファイルが足りない: {0}）", ', '.join(broken)))
    return missing


def _work_dir_exists(directory: Path, work_dir: str) -> bool:
    """ケースの work_dir（fixtures/<フォルダ>）がテーマの中にあるか。

    fixtures/ で始まらない書き方（プロジェクトルートからの相対パス・絶対パス）は確かめない。
    """
    raw = Path(work_dir)
    if raw.is_absolute() or raw.parts[:1] != (FIXTURES_DIRNAME,):
        return True
    return (directory / CASES_DIRNAME / raw).is_dir()


def list_fixtures(directory: Path) -> list[str]:
    """入力ファイルのフォルダ（ケースの work_dir に指定する `fixtures/<名前>`）の一覧。"""
    base = directory / CASES_DIRNAME / FIXTURES_DIRNAME
    return sorted(f"{FIXTURES_DIRNAME}/{p.name}" for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


# ---------------------------------------------------------------------------
# ハッシュ（トライアウトと昇格の照合）
# ---------------------------------------------------------------------------


def content_hashes(directory: Path) -> dict:
    """今の資産・ケース・設定パッチのハッシュ。

    evals/run_all.py が tryout.json に残すもの（skill_overlays・agent_overlays・
    cases_sha256・config_patch）と同じ規則で求め、昇格時に照合する。
    """
    assets = list_assets(directory)
    base = directory / ASSETS_DIRNAME
    patch = directory / CONFIG_PATCH_FILENAME
    return {
        "skills": {n: tree_sha256(base / "skills" / n) for n in assets["skills"]},
        "agents": {n: file_sha256(base / "agents" / f"{n}.md") for n in assets["agents"]},
        "cases": cases_sha256(directory / CASES_DIRNAME),
        "config_patch": file_sha256(patch) if patch.is_file() else None,
    }


def tryout_matches(directory: Path, tryout: dict | None) -> list[str]:
    """tryout（evals/run_all.py の tryout.json）が今の内容を評価したものか。食い違いの説明を返す。"""
    if not tryout:
        return ["There are no tryout results"]
    now = content_hashes(directory)
    problems = []
    if tryout.get("cases_sha256") != now["cases"]:
        problems.append("The cases (including input files) changed after the tryout")
    if sorted(tryout.get("case_files") or []) != sorted(p.stem for p in (directory / CASES_DIRNAME).glob("*.yaml")):
        problems.append("The cases in the tryout do not match all the current cases")
    evaluated_skills = {o.get("name"): o.get("sha256") for o in tryout.get("skill_overlays") or []}
    evaluated_agents = {o.get("name"): o.get("sha256") for o in tryout.get("agent_overlays") or []}
    if evaluated_skills != now["skills"]:
        problems.append("The skills changed after the tryout")
    if evaluated_agents != now["agents"]:
        problems.append("The sub-agents changed after the tryout")
    if ((tryout.get("config_patch") or {}).get("sha256")) != now["config_patch"]:
        problems.append("The settings patch (config_patch.json) changed after the tryout")
    return problems


# ---------------------------------------------------------------------------
# タスク（ワーカーが処理する依頼: 1回試行・AI による下書き等）
# ---------------------------------------------------------------------------

TASK_TYPES = ("trial", "draft_spec", "draft_cases", "draft_asset")


def add_task(directory: Path, task_type: str, params: dict, *, actor: str) -> str:
    if task_type not in TASK_TYPES:
        raise ThemeError(_t("不明なタスクです: {0}", task_type))
    task_id = f"{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(2)}"

    def _apply(data: dict) -> None:
        data.setdefault("tasks", []).append(
            {"id": task_id, "type": task_type, "params": params, "status": "queued", "created_at": now_iso(), "by": actor}
        )
        # 古い完了済みタスクは残しすぎない（直近50件）。
        done = [t for t in data["tasks"] if t["status"] in ("done", "error")]
        if len(done) > 50:
            drop = {t["id"] for t in done[: len(done) - 50]}
            data["tasks"] = [t for t in data["tasks"] if t["id"] not in drop]

    update(directory, _apply)
    return task_id
