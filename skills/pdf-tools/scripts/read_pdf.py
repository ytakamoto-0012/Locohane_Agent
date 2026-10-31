"""PDFファイルからテキストを抽出して JSON で出力する。

pdf-tools スキルの実行スクリプト（progressive disclosure 第3段階）。
run_script ツールから
    python read_pdf.py <pdf_path> [--start-page N] [--max-pages N]
の形で呼ばれる。

read_file.py の offset/limit と同じ考え方をページ単位に適用し、
1回の呼び出しで返すページ数の上限を設けて巨大な出力を避ける。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import setup_utf8_stdio, summarize_result, write_json_result
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args()

    path = Path(args.pdf_path)
    if not path.exists():
        print(f"File not found: {args.pdf_path}", file=sys.stderr)
        return 1
    if path.is_dir():
        print(f"The specified path is a directory (files only): {args.pdf_path}", file=sys.stderr)
        return 1

    try:
        reader = PdfReader(str(path))
    except PdfReadError as e:
        print(f"Could not load it as a PDF (it may be broken): {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"Failed to read the file: {e}", file=sys.stderr)
        return 1

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001 - 復号失敗はすべてエラー扱いにする
            pass

    try:
        total_pages = len(reader.pages)
    except FileNotDecryptedError:
        print("The PDF is password-protected (decryption with an empty password failed).", file=sys.stderr)
        return 1

    meta = reader.metadata
    metadata = {
        "title": getattr(meta, "title", None) if meta else None,
        "author": getattr(meta, "author", None) if meta else None,
        "subject": getattr(meta, "subject", None) if meta else None,
    }

    start_idx = max(args.start_page, 1) - 1
    end_idx = min(start_idx + max(args.max_pages, 1), total_pages)

    pages = []
    for i in range(start_idx, end_idx):
        try:
            text = reader.pages[i].extract_text() or ""
            page_entry = {"page": i + 1, "text": text}
        except Exception as e:  # noqa: BLE001 - 1ページの破損で全体を失敗させない
            page_entry = {"page": i + 1, "text": "", "error": f"Failed to extract this page: {e}"}

        # ページサイズをpt単位で取得
        try:
            mediabox = reader.pages[i].mediabox
            width_pt = float(mediabox[2] - mediabox[0])
            height_pt = float(mediabox[3] - mediabox[1])
            page_entry["width_pt"] = round(width_pt, 2)
            page_entry["height_pt"] = round(height_pt, 2)
        except Exception:
            # mediabox取得に失敗した場合はスキップ
            pass

        pages.append(page_entry)

    result = {
        "path": str(path),
        "total_pages": total_pages,
        "start_page": start_idx + 1 if pages else None,
        "end_page": end_idx if pages else None,
        "metadata": metadata,
        "pages": pages,
    }
    summary = summarize_result(result, ["pages"])
    summary.update(write_json_result(result, "pdf_read", path))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
