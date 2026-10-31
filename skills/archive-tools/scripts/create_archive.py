"""ファイル・フォルダを圧縮してアーカイブ（zip/tar/tar.gz/tar.bz2/tar.xz）を作り、JSON で結果を出力する。

archive-tools スキルの実行スクリプト（progressive disclosure 第3段階）。
run_script ツールから
    python create_archive.py <output_path> <input_path> [<input_path> ...] [--overwrite]
の形で呼ばれる。形式は output_path の拡張子で決まる。

フォルダはフォルダ名ごと格納する（C:\\work\\data → アーカイブ内の data/...）。
zip のファイル名は UTF-8（UTF-8 フラグ付き）で格納する。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import traceback
import zipfile
from pathlib import Path

from _common import CREATE_FORMATS, create_format_from_name, register_output_path, setup_utf8_stdio


def _collect(inputs: list[Path], output: Path) -> list[tuple[Path, str]]:
    """(実ファイル/フォルダ, アーカイブ内の名前) の列を返す。出力ファイル自身は含めない。"""
    items: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for src in inputs:
        base = src.parent
        candidates = [src]
        if src.is_dir():
            candidates += sorted(src.rglob("*"))
        for p in candidates:
            if p.resolve() == output:
                continue
            arcname = p.relative_to(base).as_posix()
            if arcname in seen:
                raise ValueError(f"Two inputs would be stored under the same name in the archive: {arcname}")
            seen.add(arcname)
            items.append((p, arcname))
    return items


def _write_zip(output: Path, items: list[tuple[Path, str]]) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p, arcname in items:
            # フォルダは ZipInfo.from_file が末尾に "/" を付けて空のエントリとして格納する
            zf.write(p, arcname)


def _write_tar(output: Path, items: list[tuple[Path, str]], fmt: str) -> None:
    mode = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}[fmt]
    with tarfile.open(output, mode) as tf:
        for p, arcname in items:
            tf.add(p, arcname, recursive=False)


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("output_path")
    parser.add_argument("input_paths", nargs="+")
    parser.add_argument("--overwrite", action="store_true", help="出力先に同名ファイルがあれば上書きする")
    args = parser.parse_args()

    output = Path(args.output_path).resolve()
    fmt = create_format_from_name(output)
    if fmt is None:
        supported = ", ".join(s for s, _ in CREATE_FORMATS)
        print(f"Cannot tell the format from the extension of the output file (supported: {supported}): {output}",
              file=sys.stderr)
        return 1
    if output.is_dir():
        print(f"The output path is an existing folder: {output}", file=sys.stderr)
        return 1
    if output.exists() and not args.overwrite:
        print(f"The output file already exists: {output}. Confirm with the user, then pass --overwrite to replace it.",
              file=sys.stderr)
        return 1

    inputs = [Path(p).resolve() for p in args.input_paths]
    missing = [str(p) for p in inputs if not p.exists()]
    if missing:
        print(f"Input not found: {', '.join(missing)}", file=sys.stderr)
        return 1

    try:
        items = _collect(inputs, output)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    tmp = output.with_name(output.name + ".partial")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "zip":
            _write_zip(tmp, items)
        else:
            _write_tar(tmp, items, fmt)
        os.replace(tmp, output)
    except PermissionError as e:
        tmp.unlink(missing_ok=True)
        print(f"Cannot write to {output} ({e}). Specify an output path under the working directory.", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - 想定外の失敗は traceback ごと返す
        tmp.unlink(missing_ok=True)
        print(f"Failed to create the archive: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1

    files = [p for p, _ in items if p.is_file()]
    result: dict = {
        "output_path": str(output),
        "format": fmt,
        "file_count": len(files),
        "dir_count": len(items) - len(files),
        "total_size_bytes": sum(p.stat().st_size for p in files),
        "size_bytes": output.stat().st_size,
    }
    pm = register_output_path(output, description=f"{fmt} archive")
    if pm:
        result["path_memory"] = pm
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
