"""archive-tools スキル内の各スクリプトが共有するヘルパー関数。

run_script からは直接実行されない（scripts/ prefix チェックを通らないため）。
scripts/ 配下の各スクリプトが同一ディレクトリから import して使う。

zip/tar の差を吸収して「エントリの一覧」として同じ形で扱えるようにし、
展開先パスの安全化（Zip Slip 対策・Windows で使えない名前の置換）もここに集約する。
外部ライブラリには依存しない（標準ライブラリのみ）。
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import re
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Callable

# 作成時に拡張子から決める形式。長い拡張子から順に照合する（.tar.gz を .gz より先に）。
CREATE_FORMATS: list[tuple[str, str]] = [
    (".tar.gz", "tar.gz"),
    (".tgz", "tar.gz"),
    (".tar.bz2", "tar.bz2"),
    (".tbz2", "tar.bz2"),
    (".tar.xz", "tar.xz"),
    (".txz", "tar.xz"),
    (".tar", "tar"),
    (".zip", "zip"),
]

# 単一ファイルの圧縮形式（tar を含まない .gz/.bz2/.xz）。
_SINGLE_FILE_OPENERS: dict[str, Callable[[str], IO[bytes]]] = {
    "gz": lambda p: gzip.open(p, "rb"),
    "bz2": lambda p: bz2.open(p, "rb"),
    "xz": lambda p: lzma.open(p, "rb"),
}

_WINDOWS_INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ArchiveError(Exception):
    """原因が自明なエラー（メッセージをそのまま stderr へ出せばよいもの）。"""


@dataclass
class Entry:
    name: str  # アーカイブ内のパス（区切りは "/"）
    is_dir: bool
    size: int | None  # 展開後のサイズ（不明なら None）
    compressed_size: int | None
    modified: datetime | None
    encrypted: bool = False
    # 通常ファイル・ディレクトリ以外（シンボリックリンク・デバイス等）は展開しない
    unsupported_reason: str | None = None
    open: Callable[[], IO[bytes]] | None = None


def setup_utf8_stdio() -> None:
    """標準出力/標準エラーをUTF-8に固定する。

    Windows環境ではパイプ経由のPython子プロセスの既定エンコーディングが
    システムのANSIコードページ（例: cp932）になり、run_script側の
    encoding="utf-8" デコードと食い違って日本語が文字化けするため、
    各スクリプトの先頭で必ず呼ぶこと。
    """
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def register_output_path(path, description: str | None = None) -> dict[str, str] | None:
    """生成/更新したファイルをパスメモリーへ登録し、{"@N": 絶対パス} を返す。

    run_script が子プロセスへ注入する AGENT_SRC_DIR 経由で src/path_memory.py
    を import する。AGENT_SRC_DIR未設定やimport失敗時はNone（run_script以外
    から直接実行された場合でもスクリプト自体は失敗させないためのフォールバック）。
    """
    src_dir = os.environ.get("AGENT_SRC_DIR")
    if not src_dir:
        return None
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        import path_memory
    except ImportError:
        return None
    thread_id, pm_dir, max_entries = path_memory.env_params()
    abs_path = str(Path(path).resolve())
    idx = path_memory.register(thread_id, abs_path, pm_dir, max_entries, description=description)
    if idx is None:
        return None
    return {f"@{idx}": abs_path}


def create_format_from_name(path: Path) -> str | None:
    lower = path.name.lower()
    for suffix, fmt in CREATE_FORMATS:
        if lower.endswith(suffix):
            return fmt
    return None


def archive_stem(path: Path) -> str:
    """report.tar.gz → report のように、圧縮形式の拡張子を外した名前を返す。"""
    lower = path.name.lower()
    for suffix in [s for s, _ in CREATE_FORMATS] + [".gz", ".bz2", ".xz"]:
        if lower.endswith(suffix) and len(lower) > len(suffix):
            return path.name[: -len(suffix)]
    return path.stem or path.name


def _decode_zip_name(info: zipfile.ZipInfo) -> str:
    """UTF-8 フラグの無い zip のファイル名を UTF-8 → cp932 の順に読み直す。

    日本語版 Windows のエクスプローラー等で作った zip はファイル名を cp932 で
    格納し UTF-8 フラグを立てないため、zipfile が cp437 として読んだ結果が文字化けする。
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


def _detect_single_file_format(path: Path) -> str | None:
    with open(path, "rb") as f:
        head = f.read(6)
    if head.startswith(b"\x1f\x8b"):
        return "gz"
    if head.startswith(b"BZh"):
        return "bz2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    return None


def _unsupported_format_message(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z archives are not supported (supported: zip, tar, tar.gz, tar.bz2, tar.xz, gz, bz2, xz)."
    if head.startswith(b"Rar!"):
        return "RAR archives are not supported (supported: zip, tar, tar.gz, tar.bz2, tar.xz, gz, bz2, xz)."
    return f"Not a supported archive (supported: zip, tar, tar.gz, tar.bz2, tar.xz, gz, bz2, xz): {path}"


class Archive:
    """読み込み用にアーカイブを開き、形式を問わず Entry の列として扱う。"""

    def __init__(self, path: Path, password: str | None = None):
        self.path = path
        self.format: str
        self._zip: zipfile.ZipFile | None = None
        self._tar: tarfile.TarFile | None = None
        self._single: str | None = None
        if not path.exists():
            raise ArchiveError(f"File not found: {path}")
        if path.is_dir():
            raise ArchiveError(f"The specified path is a directory (archive files only): {path}")

        if zipfile.is_zipfile(path):
            try:
                self._zip = zipfile.ZipFile(path)
            except zipfile.BadZipFile as e:
                raise ArchiveError(f"Could not open it as a zip (it may be broken): {e}") from e
            if password:
                self._zip.setpassword(password.encode("utf-8"))
            self.format = "zip"
        elif tarfile.is_tarfile(path):
            try:
                self._tar = tarfile.open(path, "r:*")
            except (tarfile.TarError, OSError, EOFError) as e:
                raise ArchiveError(f"Could not open it as a tar (it may be broken): {e}") from e
            single = _detect_single_file_format(path)
            if single is not None and create_format_from_name(path) is None and self._is_empty_tar():
                # ゼロだけのデータを圧縮した .gz 等は「空の tar」としても読めてしまうため、
                # 拡張子が tar 系でなければ単一ファイルの圧縮として扱う。
                self._tar.close()
                self._tar = None
                self._single = single
                self.format = single
            else:
                self.format = {"gz": "tar.gz", "bz2": "tar.bz2", "xz": "tar.xz"}.get(single or "", "tar")
        else:
            self._single = _detect_single_file_format(path)
            if self._single is None:
                raise ArchiveError(_unsupported_format_message(path))
            self.format = self._single

    def _is_empty_tar(self) -> bool:
        try:
            return not self._tar.getmembers()
        except (tarfile.TarError, OSError, EOFError):
            return False  # 壊れた tar は entries() でエラーにする

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
        if self._tar is not None:
            self._tar.close()

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def entries(self) -> list[Entry]:
        if self._zip is not None:
            return [self._zip_entry(info) for info in self._zip.infolist()]
        if self._tar is not None:
            try:
                members = self._tar.getmembers()
            except (tarfile.TarError, OSError, EOFError) as e:
                raise ArchiveError(f"Failed to read the tar (it may be broken): {e}") from e
            return [self._tar_entry(m) for m in members]
        return [self._single_entry()]

    def _zip_entry(self, info: zipfile.ZipInfo) -> Entry:
        zf = self._zip
        assert zf is not None
        try:
            modified = datetime(*info.date_time)
        except ValueError:
            modified = None
        return Entry(
            name=_decode_zip_name(info).replace("\\", "/"),
            is_dir=info.is_dir(),
            size=info.file_size,
            compressed_size=info.compress_size,
            modified=modified,
            encrypted=bool(info.flag_bits & 0x1),
            open=lambda: zf.open(info),
        )

    def _tar_entry(self, member: tarfile.TarInfo) -> Entry:
        tf = self._tar
        assert tf is not None
        reason = None
        if member.issym() or member.islnk():
            reason = "links are not extracted"
        elif not (member.isfile() or member.isdir()):
            reason = "special files (devices, FIFOs) are not extracted"
        return Entry(
            name=member.name,
            is_dir=member.isdir(),
            size=member.size if member.isfile() else None,
            compressed_size=None,
            modified=datetime.fromtimestamp(member.mtime) if member.mtime else None,
            unsupported_reason=reason,
            open=lambda: tf.extractfile(member),
        )

    def _single_entry(self) -> Entry:
        opener = _SINGLE_FILE_OPENERS[self._single or ""]
        path = self.path
        stat = path.stat()
        return Entry(
            name=archive_stem(path),
            is_dir=False,
            size=None,
            compressed_size=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime),
            open=lambda: opener(str(path)),
        )


def safe_relative_path(name: str) -> tuple[Path | None, str | None]:
    """アーカイブ内の名前を、展開先からの安全な相対パスに直す。

    絶対パス・ドライブ指定・".." を含むもの（Zip Slip）は None と理由を返す。
    Windows で使えない文字は "_" に置き換え、予約デバイス名は先頭に "_" を付け、
    末尾のドット・空白は除く（そのままでは書き込めない、または別名になるため）。
    """
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return None, "absolute paths are not extracted"
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None, "paths containing '..' are not extracted (they would escape the destination)"
        part = _WINDOWS_INVALID_CHARS.sub("_", part).rstrip(". ") or "_"
        if part.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
            part = "_" + part
        parts.append(part)
    if not parts:
        return None, "empty name"
    return Path(*parts), None


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None
