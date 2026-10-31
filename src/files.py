"""ツール実行結果から生成ファイルのパスを抽出する共通ヘルパー。

app.py（Chainlit UIへのダウンロード添付表示）から使う。ファイルを生成する
各スキルのスクリプト（例: pdf-tools/create_pdf.py, pptx-create/create_pptx.py）は
正常終了時のJSON出力に "output_path" キー（生成した絶対パス、単一ファイル）
または "output_paths" キー（絶対パスのリスト、複数ファイル）を含める慣習が
あり（skills/SKILLS_README.md 参照）、run_script はその標準出力を含む文字列を
そのままツール結果として返す。また provide_download のように run_script を
介さないツールは、素の JSON 文字列（"[標準出力]" のラップなし）をそのまま
返すことでも同じ経路に乗れる。ここではどちらの形式からも生成ファイルの
パス一覧を取り出す処理だけを集約する。ツール名やスキル名による分岐は
行わないため、この慣習さえ守れば新しい生成スキル・ツールを追加しても
app.py 側の変更は不要になる。
"""

from __future__ import annotations

import json
from pathlib import Path

_STDOUT_MARKER = "[stdout]\n"
_STDERR_MARKER = "\n[stderr]"


def extract_generated_files(tool_output: str) -> list[Path]:
    """ツール結果文字列から、生成されたファイルのパス一覧を抽出する。

    run_script の "[標準出力]\n<json>" 形式、または素の JSON 文字列の
    いずれかから dict を取り出し、"output_paths"（複数ファイル、文字列の
    リスト）があればそれを、無ければ "output_path"（単一ファイル、文字列）を
    1件のリストとして返す。両キーが同時に存在する場合は "output_paths" を
    優先する。JSON解析失敗、キー不在の場合は空リストを返す。実在しない
    パスは結果から除外し、順序は維持する（例外は送出しない）。

    Args:
        tool_output: ToolMessage.content（run_script の戻り値文字列や、
            provide_download 等が返す素のJSON文字列）。

    Returns:
        実在する生成ファイルの絶対パスのリスト（1件も無ければ空リスト）。
    """
    idx = tool_output.find(_STDOUT_MARKER)
    if idx != -1:
        stdout_text = tool_output[idx + len(_STDOUT_MARKER):].split(_STDERR_MARKER, 1)[0].strip()
    else:
        stdout_text = tool_output.strip()

    try:
        data = json.loads(stdout_text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    paths_value = data.get("output_paths")
    if isinstance(paths_value, list):
        raw_paths = [p for p in paths_value if isinstance(p, str) and p]
    else:
        path_str = data.get("output_path")
        raw_paths = [path_str] if isinstance(path_str, str) and path_str else []

    result: list[Path] = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_file() and path not in result:
            result.append(path)
    return result
