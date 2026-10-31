"""作業フォルダ入力欄（frontend/src/components/WorkDirButton.tsx）のパス候補・入力検証。

作業フォルダのパスはブラウザではなく Locohane をホスティングしているPC（サーバー）
から見たパスとして使われる。ブラウザだけ別PCという構成では、ユーザーが自分のPCの
感覚で入力したパスがサーバーからは存在しないことがあるため、候補もサーバー側の
ファイルシステムを列挙して返す（app.py の suggest_work_dir / pick_work_dir
action_callback から呼ばれる）。
"""

from __future__ import annotations

import os
import socket
import stat
import string
import sys
from pathlib import Path

from src.i18n import t
from src.tools.check_work_dir_status import WorkDirAccessStatus

# 候補の最大件数の既定値（実際の件数は config.ini [ui].work_dir_max_suggestions）。
MAX_SUGGESTIONS = 100
# 1ディレクトリあたり走査するエントリ数の上限。巨大なフォルダ（数万件）で
# 入力のたびに全件列挙して応答が遅れるのを防ぐ。
MAX_SCAN_ENTRIES = 5000


def _is_windows() -> bool:
    return sys.platform == "win32"


# Windows の隠し属性・システム属性（$RECYCLE.BIN・System Volume Information 等）。
_HIDDEN_ATTRIBUTES = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2) | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)


def _is_hidden(entry: os.DirEntry) -> bool:
    """隠しフォルダ・システムフォルダか（名前が "." 始まり、または Windows の隠し/システム属性）。"""
    if entry.name.startswith("."):
        return True
    try:
        # Windows では scandir の結果に属性がキャッシュされており追加のI/Oは発生しない。
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _HIDDEN_ATTRIBUTES)


def _list_subdirs(parent: Path) -> list[Path]:
    """parent 直下のディレクトリを列挙する（読めない場合は空。隠し・システムフォルダは除く）。"""
    result: list[Path] = []
    try:
        with os.scandir(parent) as it:
            for i, entry in enumerate(it):
                if i >= MAX_SCAN_ENTRIES:
                    break
                try:
                    if entry.is_dir() and not _is_hidden(entry):
                        result.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return []
    return result


def _windows_drive_roots() -> list[Path]:
    return [Path(f"{letter}:\\") for letter in string.ascii_uppercase if os.path.isdir(f"{letter}:\\")]


def _rank(candidates: list[Path], keyword: str, limit: int) -> list[str]:
    """名前が keyword で始まるものを先に、次に keyword を含むものを並べる（大文字小文字無視）。"""
    kw = keyword.casefold()
    prefix: list[str] = []
    contains: list[str] = []
    for path in candidates:
        name = (path.name or str(path)).casefold()
        if name.startswith(kw):
            prefix.append(str(path))
        elif kw in name:
            contains.append(str(path))
    prefix.sort(key=str.casefold)
    contains.sort(key=str.casefold)
    return (prefix + contains)[:limit]


def suggest_work_dirs(text: str, limit: int = MAX_SUGGESTIONS) -> list[str]:
    """入力途中の文字列から、サーバーPC上に実在するディレクトリの候補を返す。

    - 区切り文字（/ や \\）を含む場合: 最後の区切りより前を親フォルダとみなし、
      その直下のフォルダのうち最後の区切りより後ろの文字列で始まる/含むものを返す
      （区切りで終わっていれば親フォルダ直下をそのまま返す）。
    - 区切り文字を含まない場合: ドライブ（Windows。例: "c" → C:\\）と、
      サーバーPCのホームフォルダ直下から名前で探す。

    Args:
        text: 入力欄の文字列（未加工）。
        limit: 候補の最大件数（0以下なら候補を返さない）。

    Returns:
        絶対パス文字列の候補（最大 limit 件）。
    """
    text = text.strip().strip('"')
    if not text or limit <= 0:
        return []
    seps = "/\\" if _is_windows() else "/"
    cut = max(text.rfind(c) for c in seps)
    if cut >= 0:
        parent_text, keyword = text[: cut + 1], text[cut + 1 :]
        parent = Path(parent_text).expanduser()
        if not parent.is_absolute() or not parent.is_dir():
            return []
        subdirs = _list_subdirs(parent)
        if not keyword:
            return sorted((str(p) for p in subdirs), key=str.casefold)[:limit]
        return _rank(subdirs, keyword, limit)

    candidates: list[Path] = []
    if _is_windows():
        drive_kw = text.rstrip(":").casefold()
        if len(drive_kw) == 1:
            candidates.extend(p for p in _windows_drive_roots() if p.drive[0].casefold() == drive_kw)
    try:
        home = Path.home()
    except RuntimeError:
        home = None
    ranked = _rank(_list_subdirs(home), text, limit) if home is not None else []
    return ([str(p) for p in candidates] + ranked)[:limit]


def work_dir_error_message(resolved: str, status: WorkDirAccessStatus) -> str | None:
    """作業フォルダとして受け付けられない場合のエラーメッセージ（受け付けられるなら None）。

    存在しない・読み取れないフォルダは拒否する。読み取りのみ可能（書き込み不可）な
    フォルダは従来どおり受け付ける（書き込みは既定フォルダへ自動的に回される）。
    """
    if not status.exists:
        return t("workdir.not_exist", path=resolved)
    if not status.readable:
        return t("workdir.not_readable", path=resolved)
    return None


def server_hostname() -> str:
    """サーバーPCのホスト名（取得できなければ空文字）。"""
    try:
        return socket.gethostname().strip()
    except OSError:
        return ""
