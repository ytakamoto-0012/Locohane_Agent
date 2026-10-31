"""表示設定（public/settings/ = 共通、instances/<name>/settings/ = インスタンス専用）の読み書き。

header.md / tab_title.md / welcome.md（テキスト、app.py が起動時・チャット
開始時に読む）と icon.*/favicon.*（画像）を対象とする。各インスタンスは
instances/<name>/settings/ にファイルがあればそれを、無ければ public/settings/
を使う（app.py の _resolve_settings_file 参照）。

書き込み対象はホワイトリストで固定し、任意のファイル名を受け付けない
（public/settings/ 配下へのパストラバーサル防止）。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from admin.i18n import t as _t

# 編集可能なテキストファイル（app.py の _load_settings_text 参照）。
TEXT_FILE_NAMES = frozenset({"header.md", "tab_title.md", "welcome.md"})
# frontend/src/components/Header.tsx が試す順（この順でファイルを探す）。
ICON_EXTENSIONS = ("png", "svg", "jpg", "jpeg")
# favicon.png / favicon.svg（public/settings/favicon.png 等）。
FAVICON_EXTENSIONS = ("png", "svg", "ico")

_MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2MB


class SettingsFileError(Exception):
    """settings_files.py の操作失敗。"""


def read_text_file(settings_dir: Path, name: str) -> str:
    if name not in TEXT_FILE_NAMES:
        raise SettingsFileError(_t("{0} は編集できません。", name))
    path = settings_dir / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _backup_file(path: Path, backup_dir: Path, keep: int, prefix: str) -> None:
    if not path.is_file():
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"{prefix}_{stamp}{path.suffix}"
    counter = 1
    while dest.exists():
        dest = backup_dir / f"{prefix}_{stamp}_{counter}{path.suffix}"
        counter += 1
    shutil.copy2(path, dest)
    if keep > 0:
        backups = sorted(backup_dir.glob(f"{prefix}_*"), key=lambda p: p.stat().st_mtime)
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)


def write_text_file(settings_dir: Path, name: str, content: str, backup_dir: Path, backup_keep: int) -> None:
    if name not in TEXT_FILE_NAMES:
        raise SettingsFileError(_t("{0} は編集できません。", name))
    path = settings_dir / name
    _backup_file(path, backup_dir, backup_keep, prefix=f"settings_{name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_image(
    settings_dir: Path, kind: str, filename: str, content: bytes, backup_dir: Path, backup_keep: int
) -> str:
    """icon または favicon 画像を書き込む。

    Args:
        kind: "icon" または "favicon"。
        filename: アップロードされた元のファイル名（拡張子の判定にのみ使う）。
        content: 画像バイナリ。

    Returns:
        書き込んだファイル名（例: "icon.png"）。

    Raises:
        SettingsFileError: kind不正・拡張子不許可・サイズ超過。
    """
    if kind not in ("icon", "favicon"):
        raise SettingsFileError(_t("不正な種別です: {0}", kind))
    allowed = ICON_EXTENSIONS if kind == "icon" else FAVICON_EXTENSIONS
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in allowed:
        raise SettingsFileError(_t("許可されていない拡張子です（{0} のいずれか）: {1!r}", ', '.join(allowed), ext))
    if len(content) > _MAX_IMAGE_BYTES:
        raise SettingsFileError(_t("ファイルサイズが大きすぎます（上限 {0}MB）。", _MAX_IMAGE_BYTES // 1024 // 1024))

    # 既存の同種別・別拡張子ファイルを削除する（フロントエンドは拡張子を
    # 順に探して最初に見つかったものを使うため、放置すると新しい方が
    # 表示されない事故が起きる。Header.tsx/App.tsx 参照）。
    for other_ext in allowed:
        other_path = settings_dir / f"{kind}.{other_ext}"
        if other_ext != ext and other_path.is_file():
            _backup_file(other_path, backup_dir, backup_keep, prefix=f"settings_{kind}")
            other_path.unlink()

    dest = settings_dir / f"{kind}.{ext}"
    _backup_file(dest, backup_dir, backup_keep, prefix=f"settings_{kind}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return dest.name


def has_text_file(settings_dir: Path, name: str) -> bool:
    return name in TEXT_FILE_NAMES and (settings_dir / name).is_file()


def image_file(settings_dir: Path, kind: str) -> str | None:
    """settings_dir にある kind（icon/favicon）の画像ファイル名。無ければ None。"""
    allowed = ICON_EXTENSIONS if kind == "icon" else FAVICON_EXTENSIONS
    for ext in allowed:
        if (settings_dir / f"{kind}.{ext}").is_file():
            return f"{kind}.{ext}"
    return None


def delete_file(settings_dir: Path, target: str, backup_dir: Path, backup_keep: int) -> None:
    """テキストファイル名、または画像の種別（icon/favicon の全拡張子）を削除する。

    インスタンス専用の表示設定を消して共通設定（public/settings/）へ戻す用途。
    """
    if target in TEXT_FILE_NAMES:
        paths = [settings_dir / target]
        prefix = f"settings_{target}"
    elif target in ("icon", "favicon"):
        allowed = ICON_EXTENSIONS if target == "icon" else FAVICON_EXTENSIONS
        paths = [settings_dir / f"{target}.{ext}" for ext in allowed]
        prefix = f"settings_{target}"
    else:
        raise SettingsFileError(_t("{0} は削除できません。", target))
    for path in paths:
        if path.is_file():
            _backup_file(path, backup_dir, backup_keep, prefix=prefix)
            path.unlink()
