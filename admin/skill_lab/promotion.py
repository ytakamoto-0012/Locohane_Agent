"""研究テーマの昇格・差し戻しと、ドラフトのアーカイブ。

昇格先は、テーマの対象インスタンス（と、配布先に選んだインスタンス）専用の拡張ディレクトリ
（[skill_lab].instance_locohane_dir、既定 instances/<名前>/）だけ。同梱の skills/・agents/ や、
全インスタンス共通の project_locohane_dir には置かない（他のインスタンスを汚染しないため）。
同梱・共有の資産を改善したものも、専用の置き場に同じ名前で置く（後から走査したものが優先され、
そのインスタンスでだけ置き換わる）。

テーマ全体を1単位として昇格する。事前確認をすべて通ったものだけを扱い、設定は先に検証してから
ファイルを置き、設定の保存に失敗したら置いたファイルを元に戻す。

1. 事前確認: トライアウトが今の内容（資産・ケース・設定パッチ）を規定回数で評価して全回合格したものか、
   複製元の正式資産が複製後に変わっていないか、取り込み元のドラフトが変わっていないか（変わっていれば
   人の確認が要る）、配置先の同名資産、設定の登録が環境変数に邪魔されないか。
2. 設定の登録: スキルのスクリプト（計画承認の免除・main_agent_tool_guard の allow_entries）と
   config_patch.json の項目を、各インスタンスの config_overrides.json へ（admin/overrides.save()。
   検証・バックアップ・変更履歴つき）。評価（evals/run_case.py）で足したのと同じ値になる。
3. 配置: スキル・エージェントを置く（置き換えるものは evals/history/promote/ へ退避）。
4. ケース・入力ファイル・仕様・設定パッチを <instance_locohane_dir>/evals/<テーマID>/ へ（回帰テスト用）。
5. 取り込み元のドラフトを [skill_creator] archive_dir へ移す。
6. evals/promotion_log.md へ記録する。
"""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from evals.config_patch import PATCHABLE_KEYS, load_patch, merged_value
from evals.skill_tree import DRAFT_META_FILENAME, FIXTURES_DIRNAME, copy_ignore_for, file_sha256, tree_sha256
from src.config import PROJECT_ROOT, Config, load_config, merge_additive_override

from .. import instances as inst
from .. import overrides
from ..ini_catalog import find_key, parse_file
from . import sources, themes
from admin.i18n import t as _t

PROMOTION_LOG = PROJECT_ROOT / "evals" / "promotion_log.md"
PROMOTE_HISTORY_DIR = PROJECT_ROOT / "evals" / "history" / "promote"
CONFIG_INI_PATH = PROJECT_ROOT / "config.ini"
WORKSPACE_DIRNAME = "_workspace"

_PLAN_KEY = ("plan", "plan_approval_exempt_scripts")
# 昇格・差し戻しは管理ツールの1プロセスの中で1件ずつ行う（二重クリック等で同じテーマを
# 2回昇格すると、1回目に置いたものを2回目が「置き換え」として退避してしまう）。
_LOCK = threading.Lock()
_ALLOW_KEY = ("main_agent_tool_guard", "allow_entries")


class PromotionError(Exception):
    """昇格・差し戻しができない。"""


@dataclass
class CheckResult:
    problems: list[str] = field(default_factory=list)  # 1つでもあれば昇格できない
    confirmations: list[str] = field(default_factory=list)  # 人の確認（discard_source_changes）が要る
    notices: list[str] = field(default_factory=list)  # 知らせるだけ（同梱スキルの置き換え等）
    placements: dict[str, dict] = field(default_factory=dict)  # インスタンス名 → 配置先と登録内容

    def to_json(self) -> dict:
        return {
            "ok": not self.problems,
            "problems": self.problems,
            "confirmations": self.confirmations,
            "notices": self.notices,
            "placements": self.placements,
        }


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def instance_config(instances_root: Path, name: str) -> Config:
    return load_config(config_path=CONFIG_INI_PATH, overrides_path=inst.overrides_path(instances_root, name), instance_name=name)


# ---------------------------------------------------------------------------
# 設定の登録内容
# ---------------------------------------------------------------------------


def settings_to_register(directory: Path) -> dict[tuple[str, str], list]:
    """テーマを昇格したときに config_overrides.json へ足す項目（評価で足したものと同じ）。"""
    from src.skill_drafts import guard_exempt_entries_for_skill

    result: dict[tuple[str, str], list] = {}
    for name in themes.list_assets(directory)["skills"]:
        for skill, script in guard_exempt_entries_for_skill(name, directory / themes.ASSETS_DIRNAME / "skills" / name):
            result.setdefault(_PLAN_KEY, []).append([skill, script])
            result.setdefault(_ALLOW_KEY, []).append([[skill, script], -1])
    for key, items in load_patch(directory / themes.CONFIG_PATCH_FILENAME).items():
        for item in items:
            if item not in result.setdefault(key, []):
                result[key].append(item)
    return result


def _current_override_text(instances_root: Path, name: str, section: str, key: str, catalog) -> tuple[str, str]:
    """(config_overrides.json の今の値（無ければ空）, config.ini の既定値)。

    登録先のキーは config.ini の既定値に足す分だけを上書きに持つ（src/config.py の ADDITIVE_OVERRIDE_KEYS）ため、
    既定値を上書きへ写さない（写すと、アップデートで既定値から外した項目が残り続ける）。
    """
    existing = overrides.read(inst.overrides_path(instances_root, name))
    info = find_key(catalog, section, key)
    if info is None:
        raise PromotionError(_t("config.ini に [{0}].{1} がありません。", section, key))
    return existing.get(section, {}).get(key, ""), info.default_value


def _env_blocked(instances_root: Path, name: str, keys) -> list[str]:
    """登録しても効かない（インスタンスの .env・管理ツールの環境で上書きされている）環境変数名。"""
    import os

    from dotenv import dotenv_values

    env_file = inst.env_path(instances_root, name)
    values = dotenv_values(env_file) if env_file.is_file() else {}
    return [PATCHABLE_KEYS[k] for k in keys if values.get(PATCHABLE_KEYS[k]) or os.environ.get(PATCHABLE_KEYS[k])]


def compute_updates(instances_root: Path, name: str, register: dict[tuple[str, str], list]) -> tuple[dict, dict]:
    """インスタンス name の config_overrides.json へ書く更新（キー → 新しい値）と、足す項目。"""
    catalog = parse_file(CONFIG_INI_PATH)
    updates: dict[tuple[str, str], str] = {}
    added: dict[str, list] = {}
    for (section, key), items in register.items():
        current, default = _current_override_text(instances_root, name, section, key, catalog)
        new_value, new_items = merged_value(section, key, current, items, default)
        if new_value is not None:
            # overrides.save() には画面と同じく既定値を含めた全体を渡す（足す分だけを取り出して保存する）。
            updates[(section, key)] = merge_additive_override(section, key, default, new_value)[0]
            added[f"{section}.{key}"] = new_items
    return updates, added


# ---------------------------------------------------------------------------
# 事前確認
# ---------------------------------------------------------------------------


def check(instances_root: Path, directory: Path, *, distribute_to: list[str] | None = None) -> CheckResult:
    data = themes.read(directory)
    instance = data["instance"]
    result = CheckResult()
    if data.get("status") in themes.CLOSED_STATUSES:
        result.problems.append(f"This theme has already ended ({data.get('status')})")
        return result
    if data.get("status") in themes.ACTIVE_STATUSES:
        result.problems.append("Cannot promote while a loop or tryout is running (stop it or wait for it to finish)")
    assets = themes.list_assets(directory)
    if not assets["skills"] and not assets["agents"]:
        result.problems.append("There are no research targets (skills or sub-agents) to promote")

    target_cfg = instance_config(instances_root, instance)
    tryout = data.get("tryout")
    result.problems += themes.tryout_matches(directory, tryout)
    if tryout:
        if (tryout.get("repeat") or 0) < target_cfg.skill_tryout_repeats:
            result.problems.append(
                f"Not enough tryout runs: {tryout.get('repeat')} (required: {target_cfg.skill_tryout_repeats} or more)"
            )
        if tryout.get("ai_verdict") != "pass":
            result.problems.append("The tryout did not pass every run")
        if tryout.get("instance") != instance:
            result.problems.append(f"The tryout was evaluated against a different instance: {tryout.get('instance')}")

    # 取り込み元・複製元
    for asset_type in themes.ASSET_TYPES:
        for name, src in (data.get("sources") or {}).get(asset_type, {}).items():
            kind = src.get("kind")
            origin = Path(src["origin"]) if src.get("origin") else None
            if kind == "draft" and origin is not None:
                if not (origin / "SKILL.md").is_file():
                    result.notices.append(f"The source draft {src.get('owner')}/{name} no longer exists (archiving skipped)")
                elif tree_sha256(origin) != src.get("origin_sha256"):
                    result.confirmations.append(
                        f"The source draft {src.get('owner')}/{name} was changed by its author after it was imported"
                        " (promoting will not include the author's changes, and the draft will be archived)"
                    )
            elif kind == "official" and origin is not None:
                exists = (origin / "SKILL.md").is_file() if asset_type == "skills" else origin.is_file()
                now = (tree_sha256(origin) if asset_type == "skills" else file_sha256(origin)) if exists else None
                if now is not None and now != src.get("origin_sha256"):
                    result.problems.append(
                        f"The official {name} ({origin}) it was copied from was changed after copying (overwriting would lose the changes, so copy it again)"
                    )

    # 配置先（対象インスタンス + 配布先）
    targets = [instance, *[n for n in dict.fromkeys(distribute_to or []) if n != instance]]
    register = settings_to_register_safe(directory, result)
    for name in targets:
        try:
            meta = inst.read_instance(instances_root, name)
        except inst.InstanceError as e:
            result.problems.append(str(e))
            continue
        if meta.is_skill_lab:
            result.problems.append(f"{name} is a Skill Lab, so it cannot be a placement target")
            continue
        cfg = target_cfg if name == instance else instance_config(instances_root, name)
        dest = cfg.instance_locohane_dir
        placement = {"dir": str(dest), "skills": [], "agents": [], "replaces": [], "overrides_builtin": []}
        for asset_type in themes.ASSET_TYPES:
            for asset in assets[asset_type]:
                src = (data.get("sources") or {}).get(asset_type, {}).get(asset, {})
                target = dest / asset_type / (asset if asset_type == "skills" else f"{asset}.md")
                placement[asset_type].append(str(target))
                origin = Path(src["origin"]).resolve() if src.get("origin") else None
                if target.exists():
                    if src.get("kind") == "official" and origin == target.resolve() and name == instance:
                        placement["replaces"].append(f"{asset_type}/{asset}")
                    else:
                        result.problems.append(
                            f"A {asset} with the same name already exists in the dedicated location of {name} ({target}). "
                            "Make a theme that copies and improves it, or change the name"
                        )
                elif _exists_in_shared(cfg, asset_type, asset):
                    placement["overrides_builtin"].append(f"{asset_type}/{asset}")
                    result.notices.append(f"In {name}, the bundled/shared {asset} will be replaced by this theme's {asset} (other instances do not change)")
        blocked = _env_blocked(instances_root, name, register.keys())
        if blocked:
            result.problems.append(
                f"The .env of {name} (or the admin tool's environment variables) has {blocked}, so registering the settings has no effect. Review .env"
            )
        try:
            updates, added = compute_updates(instances_root, name, register)
            if updates:
                overrides.preview(
                    path=inst.overrides_path(instances_root, name),
                    updates=updates,
                    resets=[],
                    ini_catalog=parse_file(CONFIG_INI_PATH),
                    config_ini_path=CONFIG_INI_PATH,
                    instance_name=name,
                )
            placement["register"] = added
        except (overrides.ValidationError, PromotionError, ValueError) as e:
            result.problems.append(f"Cannot validate the settings to register for {name}: {e}")
        result.placements[name] = placement
    return result


def settings_to_register_safe(directory: Path, result: CheckResult) -> dict:
    try:
        return settings_to_register(directory)
    except Exception as e:  # noqa: BLE001 - 事前確認では理由を並べて返す
        result.problems.append(f"Cannot determine the settings to register: {e}")
        return {}


def _exists_in_shared(cfg: Config, asset_type: str, name: str) -> bool:
    if asset_type == "skills":
        roots = [cfg.skills_dir, *[d for d in cfg.locohane_skills_dirs if d != cfg.instance_locohane_dir / "skills"]]
        return any((r / name / "SKILL.md").is_file() for r in roots)
    roots = [cfg.agents_dir, *[d for d in cfg.locohane_agents_dirs if d != cfg.instance_locohane_dir / "agents"]]
    return any((r / f"{name}.md").is_file() for r in roots)


# ---------------------------------------------------------------------------
# 昇格
# ---------------------------------------------------------------------------


def promote(
    instances_root: Path,
    directory: Path,
    *,
    distribute_to: list[str] | None,
    discard_source_changes: bool,
    note: str,
    actor: str,
    remote_addr: str,
    backup_keep: int,
) -> dict:
    if not _LOCK.acquire(blocking=False):
        raise PromotionError(_t("ほかの昇格・差し戻しを処理中です。終わってからやり直してください。"))
    try:
        return _promote(
            instances_root,
            directory,
            distribute_to=distribute_to,
            discard_source_changes=discard_source_changes,
            note=note,
            actor=actor,
            remote_addr=remote_addr,
            backup_keep=backup_keep,
        )
    finally:
        _LOCK.release()


def _promote(
    instances_root: Path,
    directory: Path,
    *,
    distribute_to: list[str] | None,
    discard_source_changes: bool,
    note: str,
    actor: str,
    remote_addr: str,
    backup_keep: int,
) -> dict:
    result = check(instances_root, directory, distribute_to=distribute_to)
    if result.problems:
        raise PromotionError(_t("昇格できません: {0}", " / ".join(result.problems)))
    if result.confirmations and not discard_source_changes:
        raise PromotionError(_t("確認が必要です: {0}", " / ".join(result.confirmations)))
    data = themes.read(directory)
    instance = data["instance"]
    assets = themes.list_assets(directory)
    register = settings_to_register(directory)
    stamp = now_stamp()
    backup_root = PROMOTE_HISTORY_DIR / f"{data['id']}_{stamp}"

    placed: list[tuple[Path, Path | None]] = []  # (配置したパス, 退避先 or None)
    saved_instances: list[str] = []
    try:
        for name, placement in result.placements.items():
            dest = Path(placement["dir"])
            for asset_type in themes.ASSET_TYPES:
                for asset in assets[asset_type]:
                    src = directory / themes.ASSETS_DIRNAME / asset_type / (asset if asset_type == "skills" else f"{asset}.md")
                    target = dest / asset_type / src.name
                    backup = None
                    if target.exists():
                        backup = backup_root / name / asset_type / src.name
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target), str(backup))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if asset_type == "skills":
                        shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__"))
                    else:
                        shutil.copy2(src, target)
                    placed.append((target, backup))
            # 回帰テスト用のケース一式
            cases_dest = dest / "evals" / data["id"]
            if cases_dest.exists():
                shutil.rmtree(cases_dest)
            cases_dest.mkdir(parents=True)
            for case in sorted((directory / themes.CASES_DIRNAME).glob("*.yaml")):
                shutil.copy2(case, cases_dest / case.name)
            fixtures = directory / themes.CASES_DIRNAME / FIXTURES_DIRNAME
            if fixtures.is_dir():
                shutil.copytree(fixtures, cases_dest / FIXTURES_DIRNAME)
            for extra in (themes.SPEC_FILENAME, themes.CONFIG_PATCH_FILENAME):
                if (directory / extra).is_file():
                    shutil.copy2(directory / extra, cases_dest / extra)
            placed.append((cases_dest, None))
        for name in result.placements:
            updates, _ = compute_updates(instances_root, name, register)
            if not updates:
                continue
            ov_path = inst.overrides_path(instances_root, name)
            overrides.save(
                path=ov_path,
                updates=updates,
                resets=[],
                base_mtime=overrides.mtime_or_none(ov_path),
                ini_catalog=parse_file(CONFIG_INI_PATH),
                config_ini_path=CONFIG_INI_PATH,
                backup_dir=inst.backups_dir(instances_root, name),
                backup_keep=backup_keep,
                audit_log_path=instances_root / "admin_changes.log",
                instance_name=name,
                actor=_t("{0}（スキル研究室: {1}）", actor, data['title']),
                remote_addr=remote_addr,
                instances_root=instances_root,
            )
            saved_instances.append(name)
    except Exception as e:
        # 置いたファイルを戻す（設定は保存済みのものだけ残るが、足した項目は無害な免除・許可のみ）。
        for target, backup in reversed(placed):
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
            if backup is not None and backup.exists():
                shutil.move(str(backup), str(target))
        raise PromotionError(
            _t("昇格に失敗したため、置いたファイルを元に戻しました（設定を保存済みのインスタンス: {0}）: {1}", saved_instances or _t('なし'), e)
        ) from e

    # 取り込み元のドラフトをアーカイブ（配置と設定の登録は済んでいるため、ここで失敗しても昇格は取り消さず、
    # 失敗したものを記録に残す。取り消すと、置いたものと記録が食い違い、やり直しも名前の重複で止まる）。
    target_cfg = instance_config(instances_root, instance)
    archived = []
    archive_errors = []
    for name, src in (data.get("sources") or {}).get("skills", {}).items():
        if name in assets["skills"] and src.get("kind") == "draft" and src.get("origin") and (Path(src["origin"]) / "SKILL.md").is_file():
            try:
                archived.append(
                    str(
                        archive_draft(
                            Path(src["origin"]),
                            target_cfg.skill_archive_dir,
                            extra={"promoted_to": str(target_cfg.instance_locohane_dir / "skills" / name), "lab_theme": data["id"]},
                        )
                    )
                )
            except (OSError, ValueError) as e:
                archive_errors.append(f"{src.get('owner')}/{name}: {e}")

    record = {
        "at": themes.now_iso(),
        "by": actor,
        "instances": list(result.placements),
        "placements": result.placements,
        "archived_drafts": archived,
        "archive_errors": archive_errors,
        "backup": str(backup_root) if backup_root.exists() else None,
        "note": note,
        "tryout": {k: data["tryout"].get(k) for k in ("repeat", "out_dir", "ai_judged", "iteration")},
        "discarded_source_changes": result.confirmations if discard_source_changes else [],
    }

    def _apply(d: dict) -> None:
        d["status"] = themes.STATUS_PROMOTED
        d["promotion"] = record
        themes.add_history(d, actor, "promote", ", ".join(result.placements))

    themes.update(directory, _apply)
    _append_log(data, record)
    return record


def archive_draft(draft: Path, archive_dir: Path, *, extra: dict | None = None) -> Path:
    """ドラフト（と <owner>/_workspace/<skill>/）を archive_dir/<owner>/<skill>_<日時>/ へ移す。"""
    owner = draft.parent.name
    dest = archive_dir / owner / f"{draft.name}_{now_stamp()}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta_path = draft / DRAFT_META_FILENAME
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    meta.update({"status": "promoted", "promoted_at": themes.now_iso(), "archived_from": str(draft), **(extra or {})})
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.move(str(draft), str(dest))
    workspace = draft.parent / WORKSPACE_DIRNAME / draft.name
    if workspace.is_dir():
        shutil.move(str(workspace), str(dest / WORKSPACE_DIRNAME))
    return dest


def _append_log(data: dict, record: dict) -> None:
    lines = [
        f"\n## {record['at']} {data['title']}（スキル研究室、テーマ {data['id']}、対象インスタンス {data['instance']}）\n",
    ]
    for name, placement in record["placements"].items():
        lines.append(f"- 配置（{name}）: {', '.join(placement['skills'] + placement['agents'])}")
        if placement.get("register"):
            lines.append(f"- 設定登録（{name}）: {json.dumps(placement['register'], ensure_ascii=False)}")
        if placement.get("overrides_builtin"):
            lines.append(f"- 同梱・共有を置き換え（{name}）: {', '.join(placement['overrides_builtin'])}")
    t = record["tryout"]
    lines.append(f"- トライアウト: 各{t.get('repeat')}回、全回合格{'（judge は AI 判定を承認者が確認）' if t.get('ai_judged') else ''}、結果 {t.get('out_dir')}")
    lines.append(f"- 承認: {record['by']}")
    if record["archived_drafts"]:
        lines.append(f"- アーカイブしたドラフト: {', '.join(record['archived_drafts'])}")
    if record.get("archive_errors"):
        lines.append(f"- アーカイブできなかったドラフト（手で移してください）: {' / '.join(record['archive_errors'])}")
    if record["backup"]:
        lines.append(f"- 置き換え前のバックアップ: {record['backup']}")
    if record["discarded_source_changes"]:
        lines.append(f"- 作成者の変更を反映せずに昇格: {' / '.join(record['discarded_source_changes'])}")
    if record["note"]:
        lines.append(f"- メモ: {record['note']}")
    if not PROMOTION_LOG.exists():
        PROMOTION_LOG.write_text("# スキル昇格ログ\n\nスキル研究室・promote-skill スキルで正式化したものの記録。\n", encoding="utf-8")
    with PROMOTION_LOG.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# 差し戻し・不採用・取りやめ
# ---------------------------------------------------------------------------


def return_to_creators(directory: Path, *, reason: str, apply_fixes: bool, reject: bool, actor: str) -> dict:
    """取り込み元のドラフトへ差し戻す（apply_fixes なら研究室での修正をドラフトへ写す）。"""
    if not _LOCK.acquire(blocking=False):
        raise PromotionError(_t("ほかの昇格・差し戻しを処理中です。終わってからやり直してください。"))
    try:
        return _return_to_creators(directory, reason=reason, apply_fixes=apply_fixes, reject=reject, actor=actor)
    finally:
        _LOCK.release()


def _return_to_creators(directory: Path, *, reason: str, apply_fixes: bool, reject: bool, actor: str) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise PromotionError(_t("差し戻しの理由を書いてください（作成者の skill-creator に表示されます）。"))
    data = themes.read(directory)
    if data.get("status") in themes.CLOSED_STATUSES:
        raise PromotionError(_t("このテーマは既に終了しています（{0}）", data.get('status')))
    if data.get("status") in themes.ACTIVE_STATUSES:
        raise PromotionError(_t("ループ・トライアウトの実行中は差し戻せません（先に停止してください）"))
    drafts = [
        (name, src)
        for name, src in (data.get("sources") or {}).get("skills", {}).items()
        if src.get("kind") == "draft" and src.get("origin") and (Path(src["origin"]) / "SKILL.md").is_file()
    ]
    if not drafts:
        raise PromotionError(_t("差し戻せる取り込み元のドラフトがありません（テーマを終えるには「取りやめ」を使ってください）。"))
    returned = []
    for name, src in drafts:
        draft = Path(src["origin"])
        work = directory / themes.ASSETS_DIRNAME / "skills" / name
        if apply_fixes and (work / "SKILL.md").is_file():
            try:
                _replace_draft_with(draft, work)
            except OSError as e:
                raise PromotionError(
                    _t("ドラフト {0}/{1} へ修正を写せませんでした（ドラフトは元のままです。作成者がファイルを開いていないか確かめてください。差し戻し済み: {2}）: {3}", src.get('owner'), name, returned or _t('なし'), e)
                ) from e
        meta_path = draft / DRAFT_META_FILENAME
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        if apply_fixes and meta.get("tryouts"):
            meta["tryouts"] = []
            meta["tryouts_reset_at"] = themes.now_iso()
        meta.update(
            {
                "status": "rejected" if reject else "returned",
                "returned_reason": reason,
                "returned_at": themes.now_iso(),
                "lab": {"theme_id": data["id"], "applied_fixes": apply_fixes},
            }
        )
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        returned.append(f"{src.get('owner')}/{name}")

    def _apply(d: dict) -> None:
        d["status"] = themes.STATUS_RETURNED
        d["returned"] = {"at": themes.now_iso(), "by": actor, "reason": reason, "apply_fixes": apply_fixes, "reject": reject, "drafts": returned}
        themes.add_history(d, actor, "reject" if reject else "return", reason[:500])

    themes.update(directory, _apply)
    return {"drafts": returned}


def _replace_draft_with(draft: Path, work: Path) -> None:
    """ドラフトの中身を研究室で直したもの（work）に置き換える（来歴・ケースはドラフトのものを残す）。

    先に隣へ新しい中身を組み立ててから入れ替えるため、途中で失敗してもドラフトは元のまま残る。
    """
    # 「_」で始まる名前は、ドラフトの一覧（sources.list_drafts・skill-creator）に出ない。
    staging = draft.with_name(f"_lab_new_{draft.name}")
    old = draft.with_name(f"_lab_old_{draft.name}")
    for leftover in (staging, old):
        shutil.rmtree(leftover, ignore_errors=True)
    shutil.copytree(work, staging, ignore=copy_ignore_for(work))
    if (draft / "evals").is_dir():
        shutil.copytree(draft / "evals", staging / "evals")
    if (draft / DRAFT_META_FILENAME).is_file():
        shutil.copy2(draft / DRAFT_META_FILENAME, staging / DRAFT_META_FILENAME)
    draft.rename(old)
    try:
        staging.rename(draft)
    except OSError:
        old.rename(draft)
        raise
    shutil.rmtree(old, ignore_errors=True)


def mark_imported(draft: Path, theme_id: str) -> None:
    """取り込んだドラフトの来歴に、研究室のテーマを記録する（作成者の list_drafts.py に出る）。"""
    meta_path = draft / DRAFT_META_FILENAME
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    meta["lab"] = {"theme_id": theme_id, "imported_at": themes.now_iso()}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def unmark_imported(directory: Path, only: str | None = None) -> None:
    """テーマを取りやめた・ドラフトを研究対象から外したとき、取り込み元のドラフトから研究室の印を外す
    （作成者の一覧に誤った案内を残さない）。only を指定すると、そのスキル名のドラフトだけ。"""
    data = themes.read(directory)
    for name, src in (data.get("sources") or {}).get("skills", {}).items():
        if src.get("kind") != "draft" or not src.get("origin") or (only is not None and name != only):
            continue
        meta_path = Path(src["origin"]) / DRAFT_META_FILENAME
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (meta.get("lab") or {}).get("theme_id") == data["id"]:
            meta.pop("lab", None)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def official_sources(cfg: Config, asset_type: str, name: str) -> Path | None:
    return sources.find_official_skill_dir(cfg, name) if asset_type == "skills" else sources.find_official_agent_file(cfg, name)
