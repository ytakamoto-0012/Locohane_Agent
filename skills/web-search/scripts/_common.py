"""web-search スキル内の各スクリプトが共有するヘルパー関数。

run_script からは直接実行されない（scripts/ prefix チェックを通らないため）。
scripts/ 配下の各スクリプトが同一ディレクトリから import して使う。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def setup_utf8_stdio() -> None:
    """標準出力/標準エラーをUTF-8に固定する。

    Windows環境ではパイプ経由のPython子プロセスの既定エンコーディングが
    システムのANSIコードページ（例: cp932）になり、run_script側の
    encoding="utf-8" デコードと食い違って日本語が文字化けするため、
    各スクリプトの先頭で必ず呼ぶこと。
    """
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def load_local_env() -> None:
    """このファイルと同じディレクトリの .env を読み、os.environ にまだ無いキーだけ設定する。

    プロジェクトルートの .env とは別に、スキル単位でAPIキー等を管理するための
    簡易ローダー（python-dotenv非依存。標準ライブラリのみ）。

    値が `[` で始まり同じ行で `]` が閉じていない場合、config.ini の
    project_locohane_dir 等と同じJSON/Python風リスト形式（角カッコ＋改行複数行OK）
    とみなし、`]`で角カッコの数が閉じるまで後続行を連結する。連結後の文字列は
    ast.literal_eval でパースできる形のまま os.environ に格納する（パース自体は
    呼び出し側が行う）。
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.count("[") > value.count("]"):
            collected = [value]
            open_count = value.count("[")
            close_count = value.count("]")
            while i < len(lines) and open_count > close_count:
                next_line = lines[i].strip()
                collected.append(next_line)
                open_count += next_line.count("[")
                close_count += next_line.count("]")
                i += 1
            value = "\n".join(collected)
        else:
            value = value.strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
