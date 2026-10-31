"""アーカイブ（zip/tar 等）の中身を展開せずに一覧して JSON で出力する。

archive-tools スキルの実行スクリプト（progressive disclosure 第3段階）。
run_script ツールから
    python list_archive.py <archive_path> [--max-entries N]
の形で呼ばれる。読み取り専用（ファイルを書き込まない）。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from _common import Archive, ArchiveError, iso, setup_utf8_stdio


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("archive_path")
    parser.add_argument("--max-entries", type=int, default=200)
    args = parser.parse_args()

    path = Path(args.archive_path)
    try:
        with Archive(path) as archive:
            entries = archive.entries()
            fmt = archive.format
    except ArchiveError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - 想定外の破損等は traceback ごと返す
        print(f"Failed to read the archive: {e}\n{traceback.format_exc()}", file=sys.stderr)
        return 1

    files = [e for e in entries if not e.is_dir]
    sizes = [e.size for e in files]
    max_entries = max(args.max_entries, 1)
    result = {
        "path": str(path.resolve()),
        "format": fmt,
        "archive_size_bytes": path.stat().st_size,
        "total_entries": len(entries),
        "file_count": len(files),
        "dir_count": len(entries) - len(files),
        "total_size_bytes": sum(sizes) if None not in sizes else None,
        "encrypted": any(e.encrypted for e in entries),
        "entries": [
            {
                "name": e.name,
                "is_dir": e.is_dir,
                "size": e.size,
                "modified": iso(e.modified),
                **({"encrypted": True} if e.encrypted else {}),
                **({"not_extractable": e.unsupported_reason} if e.unsupported_reason else {}),
            }
            for e in entries[:max_entries]
        ],
        "truncated": len(entries) > max_entries,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
