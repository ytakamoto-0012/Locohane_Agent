"""アーカイブ（zip/tar/tar.gz/tar.bz2/tar.xz/gz/bz2/xz）を展開して JSON で結果を出力する。

archive-tools スキルの実行スクリプト（progressive disclosure 第3段階）。
run_script ツールから
    python extract_archive.py <archive_path> [--dest DIR] [--overwrite]
                              [--password PW] [--max-total-mb N]
の形で呼ばれる。

展開先の外へ書き出すエントリ（絶対パス・".."。いわゆる Zip Slip）とリンク・特殊ファイルは
展開せず skipped に理由を残す。展開後の合計サイズが上限を超えるもの（zip bomb 等）は
1ファイルも書き出さずにエラーにする。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import zipfile
from pathlib import Path

from _common import Archive, ArchiveError, archive_stem, register_output_path, safe_relative_path, setup_utf8_stdio

_MAX_SKIPPED_IN_OUTPUT = 50
_COPY_CHUNK = 1024 * 1024


class _LimitExceeded(Exception):
    """書き込み中に展開後の合計サイズが上限を超えた。"""


def _copy_limited(src, dst, remaining: int) -> int:
    """src を dst へ写し、写したバイト数を返す。remaining を超えたら _LimitExceeded。

    単一ファイルの gz/bz2/xz は展開後のサイズが事前に分からないため、書き込みながら数える
    （書き終わってから調べると、高圧縮のファイルでディスクを使い切るまで止まらない）。
    """
    written = 0
    while chunk := src.read(_COPY_CHUNK):
        written += len(chunk)
        if written > remaining:
            raise _LimitExceeded()
        dst.write(chunk)
    return written


def _set_mtime(target: Path, entry) -> None:
    if entry.modified is None:
        return
    try:
        ts = entry.modified.timestamp()
        os.utime(target, (ts, ts))
    except (OSError, OverflowError, ValueError):
        pass


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("archive_path")
    parser.add_argument("--dest", help="展開先フォルダ（省略時はアーカイブと同じ場所の <アーカイブ名> フォルダ）")
    parser.add_argument("--overwrite", action="store_true", help="同名ファイルがあれば上書きする")
    parser.add_argument("--password", help="暗号化 zip（ZipCrypto）のパスワード")
    parser.add_argument("--max-total-mb", type=float, default=4096, help="展開後の合計サイズの上限（MB）")
    args = parser.parse_args()

    path = Path(args.archive_path)
    dest = Path(args.dest) if args.dest else path.resolve().parent / archive_stem(path)

    try:
        archive = Archive(path, password=args.password)
    except ArchiveError as e:
        print(str(e), file=sys.stderr)
        return 1

    with archive:
        try:
            entries = archive.entries()
        except ArchiveError as e:
            print(str(e), file=sys.stderr)
            return 1

        if any(e.encrypted for e in entries) and not args.password:
            print(
                "The zip is password-protected. Ask the user for the password and pass it with --password.",
                file=sys.stderr,
            )
            return 1

        declared = sum(e.size or 0 for e in entries if not e.is_dir)
        limit = int(args.max_total_mb * 1024 * 1024)
        if declared > limit:
            print(
                f"The extracted size would be {declared / 1024 / 1024:.1f} MB, which exceeds the limit "
                f"({args.max_total_mb:g} MB). Nothing was extracted. If this is intended, raise --max-total-mb.",
                file=sys.stderr,
            )
            return 1

        try:
            dest.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            print(
                f"Cannot write to the destination folder: {dest} ({e}). "
                "Specify a folder under the working directory with --dest.",
                file=sys.stderr,
            )
            return 1
        dest = dest.resolve()

        extracted = 0
        dir_count = 0
        written_bytes = 0
        skipped: list[dict] = []
        for entry in entries:
            rel, reason = safe_relative_path(entry.name)
            if rel is None:
                skipped.append({"name": entry.name, "reason": reason})
                continue
            if entry.unsupported_reason:
                skipped.append({"name": entry.name, "reason": entry.unsupported_reason})
                continue
            target = dest / rel
            if entry.is_dir:
                target.mkdir(parents=True, exist_ok=True)
                dir_count += 1
                continue
            if target.exists() and not args.overwrite:
                skipped.append({"name": entry.name, "reason": "already exists (use --overwrite to replace)"})
                continue
            if target.is_dir():
                skipped.append({"name": entry.name, "reason": "a folder with the same name already exists"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with entry.open() as src, open(target, "wb") as dst:
                    written_bytes += _copy_limited(src, dst, limit - written_bytes)
            except _LimitExceeded:
                target.unlink(missing_ok=True)
                print(
                    f"Stopped because the extracted size exceeded the limit ({args.max_total_mb:g} MB). "
                    f"{extracted} files were extracted to {dest} (the file being written was removed). "
                    "If this is intended, raise --max-total-mb.",
                    file=sys.stderr,
                )
                return 1
            except RuntimeError as e:
                # zipfile は暗号化 zip のパスワード違いを RuntimeError("Bad password ...") で知らせる
                target.unlink(missing_ok=True)
                if "password" in str(e).lower():
                    print(f"The password is wrong: {e}", file=sys.stderr)
                    return 1
                raise
            except NotImplementedError as e:
                target.unlink(missing_ok=True)
                print(
                    f"This zip uses an unsupported compression/encryption method (e.g. AES encryption): {e}",
                    file=sys.stderr,
                )
                return 1
            except PermissionError as e:
                target.unlink(missing_ok=True)
                print(
                    f"Cannot write to {target} ({e}). Specify a folder under the working directory with --dest.",
                    file=sys.stderr,
                )
                return 1
            except (zipfile.BadZipFile, EOFError, OSError) as e:
                target.unlink(missing_ok=True)
                skipped.append({"name": entry.name, "reason": f"failed to extract (the archive may be broken): {e}"})
                continue
            _set_mtime(target, entry)
            extracted += 1

    result: dict = {
        "archive_path": str(path.resolve()),
        "format": archive.format,
        "output_dir": str(dest),
        "extracted_files": extracted,
        "created_dirs": dir_count,
        "total_size_bytes": written_bytes,
        "skipped_count": len(skipped),
        "skipped": skipped[:_MAX_SKIPPED_IN_OUTPUT],
    }
    pm = register_output_path(dest, description=f"folder extracted from {path.name}")
    if pm:
        result["path_memory"] = pm
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - 想定外の失敗は traceback ごと返す
        print(f"Failed to extract the archive: {e}\n{traceback.format_exc()}", file=sys.stderr)
        sys.exit(1)
