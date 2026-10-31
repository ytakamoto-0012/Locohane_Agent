"""インスタンス別 config_overrides.json の読み書き・検証・バックアップ・監査記録。

config.ini は既定値として扱い、このモジュールは書き換えない。UIでの変更は
instances/<name>/config_overrides.json（{"<section>": {"<key>": "<値>"}}の
JSON）にのみ記録する。優先度は 環境変数 > config_overrides.json > config.ini
（詳細は src/config.py の load_config() docstring 参照）。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from src.config import ADDITIVE_OVERRIDE_KEYS, ADMIN_SETTING_KEYS, additive_override_additions, load_config

from . import audit
from . import instances as inst
from .ini_catalog import IniCatalog
from admin.i18n import t as _t

# [admin] セクションは管理ツール自身の起動設定であり、インスタンス別の
# 上書き対象外（src/config.py の _apply_config_overrides も同じ理由で無視する。
# ここでは無視するのではなく、保存時点で明示的なエラーとして拒否する）。
# [data_backup] も全インスタンス共通の管理ツールの設定（ダッシュボードの「データバックアップ」画面で編集する。
# admin/data_backup.py）のため対象外。
_EXEMPT_SECTIONS = frozenset({"admin", "data_backup"})
# src/config.py の ADMIN_SETTING_KEYS（取り込みの並列数・「AIに相談」のインスタンス）も全インスタンス共通の
# 設定のため、インスタンス別には保存させない（「管理者設定」画面で変える。admin/admin_settings.py）。


class OverridesError(Exception):
    """overrides.py の操作失敗の基底クラス。"""


class ConflictError(OverridesError):
    """他のセッションが先に保存しており、base_mtime が古い。"""


class ValidationError(OverridesError):
    """新しい設定値で load_config() の検証が失敗した、または不正な入力。"""


def read(path: Path) -> dict[str, dict[str, str]]:
    """config_overrides.json を読む。ファイルが無ければ空dict。"""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValidationError(_t("{0}: トップレベルはオブジェクトである必要があります。", path))
    return data


def mtime_or_none(path: Path) -> float | None:
    """path の mtime。存在しなければ None（「まだ一度も保存されていない」)。"""
    return path.stat().st_mtime if path.is_file() else None


def _backup(path: Path, backup_dir: Path, keep: int) -> None:
    """path の保存前スナップショットを backup_dir へコピーし、古い世代を削除する。

    path が未作成（初回保存前）なら何もしない。
    """
    if not path.is_file():
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"config_overrides_{stamp}.json"
    counter = 1
    while dest.exists():
        dest = backup_dir / f"config_overrides_{stamp}_{counter}.json"
        counter += 1
    shutil.copy2(path, dest)
    if keep > 0:
        backups = sorted(backup_dir.glob("config_overrides_*.json"), key=lambda p: p.stat().st_mtime)
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)


def _diff(old: dict, new: dict) -> list[dict]:
    """old→new の変更点を [{"section","key","old","new"}] の形で返す（機密値はマスク済み）。"""
    changes: list[dict] = []
    for section in sorted(set(old) | set(new)):
        old_section = old.get(section, {})
        new_section = new.get(section, {})
        for key in sorted(set(old_section) | set(new_section)):
            old_value = old_section.get(key)
            new_value = new_section.get(key)
            if old_value != new_value:
                changes.append(
                    {
                        "section": section,
                        "key": key,
                        "old": audit.mask(key, old_value),
                        "new": audit.mask(key, new_value),
                    }
                )
    return changes


def _build_new_data(
    existing: dict[str, dict[str, str]],
    updates: dict[tuple[str, str], str],
    resets: list[tuple[str, str]],
    ini_catalog: IniCatalog,
) -> dict[str, dict[str, str]]:
    """既存の overrides に updates/resets を適用した、新しい overrides dict を組み立てる（未保存）。

    Raises:
        ValidationError: 存在しないキーが指定された、または [admin] セクションが指定された。
    """
    new_data: dict[str, dict[str, str]] = {section: dict(kv) for section, kv in existing.items()}
    key_index = {(info.section, info.key): info for info in ini_catalog.keys}

    for (section, key), value in updates.items():
        if (section, key) in ADMIN_SETTING_KEYS:
            raise ValidationError(
                _t("[{0}].{1} は全インスタンス共通の設定のため、インスタンス別には変更できません（「管理者設定」画面で変更してください）。", section, key)
            )
        if section == "admin":
            raise ValidationError(_t("[admin] セクションは管理ツールからは変更できません（config.ini を直接編集してください）。"))
        if section in _EXEMPT_SECTIONS:
            raise ValidationError(_t("[{0}] セクションは全インスタンス共通の設定です。「データバックアップ」画面で変更してください。", section))
        info = key_index.get((section, key))
        if info is None:
            raise ValidationError(_t("[{0}].{1} は config.ini に存在しないキーです。", section, key))
        if (section, key) in ADDITIVE_OVERRIDE_KEYS:
            # 画面は既定値を含めた全体を編集するため、既定値に足す分だけを保存する（src/config.py の merge_additive_override）。
            additions, removed, changed = additive_override_additions(section, key, info.default_value, value)
            if removed or changed:
                raise ValidationError(
                    _t(
                        "[{0}].{1} の config.ini の既定値の項目は、インスタンス別には消したり変えたりできません（足すことだけができます）: {2}",
                        section, key, ", ".join(json.dumps(i, ensure_ascii=False) for i in [*removed, *changed]),
                    )
                )
            if additions is None:
                new_data.get(section, {}).pop(key, None)
            else:
                new_data.setdefault(section, {})[key] = additions
            continue
        if value == info.default_value:
            new_data.get(section, {}).pop(key, None)
        else:
            new_data.setdefault(section, {})[key] = value

    for section, key in resets:
        new_data.get(section, {}).pop(key, None)

    # 空になったセクションは書き出さない。
    return {section: kv for section, kv in new_data.items() if kv}


def _check_data_dir_conflict(
    instances_root: Path | None, config_ini_path: Path, *, instance_name: str | None, tmp_path: Path
) -> None:
    """instances_root が指定されていれば、他インスタンスとのデータ保存先重複を検証する（無ければ何もしない）。

    load_config() 自体の検証（型・enum等）が成功した直後に呼ぶこと
    （2026-09-29 レビューで発見：[paths].common_data_dir を他インスタンスと
    同じ値に書き換えて保存できてしまい、後でそちらのインスタンスを削除すると
    相手のデータごと消える事故があった。admin/instances.py の
    check_data_dir_conflict_for_overrides() 参照）。instances_root は
    テスト等でこのモジュールを単体利用する際の後方互換のため省略可能。
    """
    if instances_root is None or instance_name is None:
        return
    try:
        inst.check_data_dir_conflict_for_overrides(
            instances_root, config_ini_path, name=instance_name, overrides_tmp_path=tmp_path
        )
    except inst.InstanceError as exc:
        raise ValidationError(str(exc)) from exc


def preview(
    *,
    path: Path,
    updates: dict[tuple[str, str], str],
    resets: list[tuple[str, str]],
    ini_catalog: IniCatalog,
    config_ini_path: Path,
    instance_name: str | None = None,
    instances_root: Path | None = None,
) -> tuple[dict[str, dict[str, str]], list[dict]]:
    """保存せずに、適用結果の overrides dict と変更点の差分を返す（「変更を確認」用）。

    保存時と同じ load_config() 検証を行うが、ファイルは一切書き換えない。

    Args:
        instances_root: 指定すると、他インスタンスとのデータ保存先重複も検証する
            （_check_data_dir_conflict() 参照）。

    Returns:
        (新しい overrides dict, _diff() と同形式の変更点リスト)。

    Raises:
        ValidationError: save() と同じ検証エラー。
    """
    existing = read(path)
    new_data = _build_new_data(existing, updates, resets, ini_catalog)

    fd, tmp_name = tempfile.mkstemp(prefix=".config_overrides_preview_", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            load_config(config_path=config_ini_path, overrides_path=tmp_path, instance_name=instance_name)
        except Exception as exc:  # noqa: BLE001 - 原因をそのままUIへ伝える
            raise ValidationError(_t("設定の検証に失敗しました: {0}", exc)) from exc
        _check_data_dir_conflict(instances_root, config_ini_path, instance_name=instance_name, tmp_path=tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return new_data, _diff(existing, new_data)


def save(
    *,
    path: Path,
    updates: dict[tuple[str, str], str],
    resets: list[tuple[str, str]],
    base_mtime: float | None,
    ini_catalog: IniCatalog,
    config_ini_path: Path,
    backup_dir: Path,
    backup_keep: int,
    audit_log_path: Path,
    instance_name: str,
    actor: str,
    remote_addr: str,
    instances_root: Path | None = None,
) -> dict[str, dict[str, str]]:
    """updates/resets を既存の overrides に適用し、検証・保存・監査記録する。

    Args:
        updates: {(section, key): 新しい値の文字列, ...}。値が config.ini の
            既定値と一致する場合は、上書きとして記録せず overrides から
            該当キーを削除する（既定値のまま持たせない）。
        resets: [(section, key), ...]。「既定値に戻す」ボタンで、対応する
            エントリを問答無用で overrides から削除する。
        base_mtime: 編集開始時に取得した path の mtime。None は「競合チェック
            をしない」ではなく「新規作成（path が未作成である前提）」を意味する。
            実際の mtime と食い違えば ConflictError（他セッションが先に保存済み）。
        instances_root: 指定すると、他インスタンスとのデータ保存先重複も検証する
            （_check_data_dir_conflict() 参照）。

    Returns:
        保存後の overrides dict 全体（API レスポンス用）。

    Raises:
        ConflictError: 保存直前の実際の mtime が base_mtime と一致しない。
            base_mtime が None（「新規作成のつもり」）でも、実際には既に
            ファイルが存在していれば同様に競合とみなす（他セッションが
            先に「初回保存」を済ませていた場合の検出漏れを防ぐ）。
        ValidationError: 存在しないキーが指定された、[admin] セクションが
            指定された、または新しい設定値で load_config() の検証が失敗した。
    """
    actual_mtime = mtime_or_none(path)
    if base_mtime is None:
        if actual_mtime is not None:
            raise ConflictError(
                _t("他のセッション/ブラウザタブで既に保存されています。画面を再読み込みしてから編集し直してください。")
            )
    elif actual_mtime is not None and abs(actual_mtime - base_mtime) > 1e-6:
        raise ConflictError(
            _t("他のセッション/ブラウザタブで既に保存されています。画面を再読み込みしてから編集し直してください。")
        )

    existing = read(path)
    new_data = _build_new_data(existing, updates, resets, ini_catalog)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config_overrides_", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            # load_config() 本体の型変換・enumバリデーション等をそのまま
            # 検証に使う（新しいconfig.iniキーが増えても個別対応不要）。
            # common_data_dir 配下のディレクトリ作成という副作用が起きるが、
            # このインスタンスがいずれ起動すればどのみち必要になるディレクトリ
            # なので実害はない。
            load_config(config_path=config_ini_path, overrides_path=tmp_path, instance_name=instance_name)
        except Exception as exc:  # noqa: BLE001 - 原因をそのままUIへ伝える
            raise ValidationError(_t("設定の検証に失敗しました: {0}", exc)) from exc
        _check_data_dir_conflict(instances_root, config_ini_path, instance_name=instance_name, tmp_path=tmp_path)

        _backup(path, backup_dir, backup_keep)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    changes = _diff(existing, new_data)
    if changes:
        audit.append(
            audit_log_path,
            {
                "instance": instance_name,
                "actor": actor,
                "remote_addr": remote_addr,
                "action": "config_update",
                "changes": changes,
            },
        )
    return new_data


def restore(
    *,
    path: Path,
    backup_file: Path,
    config_ini_path: Path,
    backup_dir: Path,
    backup_keep: int,
    audit_log_path: Path,
    instance_name: str,
    actor: str,
    remote_addr: str,
    instances_root: Path | None = None,
) -> dict[str, dict[str, str]]:
    """バックアップファイルの内容で config_overrides.json を丸ごと置き換える。

    復元前の現状も _backup() で退避してから上書きする（誤って古いバックアップを
    選んでも、直前の状態に戻せるようにするため）。

    Args:
        instances_root: 指定すると、他インスタンスとのデータ保存先重複も検証する
            （_check_data_dir_conflict() 参照）。

    Raises:
        ValidationError: backup_file が存在しない、JSON構文が不正、
            またはその内容で load_config() の検証が失敗した場合。
    """
    if not backup_file.is_file():
        raise ValidationError(_t("バックアップが見つかりません: {0}", backup_file))
    new_data = json.loads(backup_file.read_text(encoding="utf-8"))
    if not isinstance(new_data, dict):
        raise ValidationError(_t("{0}: トップレベルはオブジェクトである必要があります。", backup_file))

    existing = read(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config_overrides_restore_", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2, sort_keys=True)
        try:
            load_config(config_path=config_ini_path, overrides_path=tmp_path, instance_name=instance_name)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(_t("バックアップの検証に失敗しました: {0}", exc)) from exc
        _check_data_dir_conflict(instances_root, config_ini_path, instance_name=instance_name, tmp_path=tmp_path)

        _backup(path, backup_dir, backup_keep)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    changes = _diff(existing, new_data)
    audit.append(
        audit_log_path,
        {
            "instance": instance_name,
            "actor": actor,
            "remote_addr": remote_addr,
            "action": "config_restore",
            "backup_file": backup_file.name,
            "changes": changes,
        },
    )
    return new_data
