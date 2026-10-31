"""pdf-tools スキル内の各スクリプトが共有するヘルパー関数。

run_script からは直接実行されない（scripts/ prefix チェックを通らないため）。
scripts/ 配下の各スクリプトが同一ディレクトリから import して使う。
"""

from __future__ import annotations

import hashlib
import json
import locale
import os
import sys
from datetime import datetime
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


def read_text_with_fallback(path: Path) -> str:
    """UTF-8 で読めなければ OS の ANSI コードページ（日本語版 Windows なら cp932、英語版なら cp1252 等）、
    cp932 の順に読む（本体の src/tools/_file_tools_common.py と同じ順）。

    cp1252 の文書の多くは cp932 としてもエラーなく読めて文字化けに気付けないため、ANSI を先に試す。
    """
    data = path.read_bytes()
    error: UnicodeDecodeError | None = None
    for encoding in dict.fromkeys(("utf-8", locale.getencoding(), "cp932")):
        try:
            return data.decode(encoding).replace("\r\n", "\n")
        except UnicodeDecodeError as e:
            error = error or e
        except LookupError:
            continue
    raise error or UnicodeDecodeError("utf-8", data, 0, len(data), "undecodable")


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


def write_json_result(result: dict, category: str, source_path) -> dict:
    """result全体をJSONファイルへ書き出し、要約に足す {"result_path", "path_memory"} を返す。

    保存先は execute_python_code の中間生成物と同じ `_tmp_<thread_id>/<category>/`
    規約（render_pdf_pages.py 参照）。基準は run_script の cwd ではなく常に
    default_workdir（AGENT_DEFAULT_WORKDIR）。cwd はユーザー指定 work_dir に
    なりうるが、その場合は自動削除対象外のため、cwd基準だと消えずに溜まり続ける。
    """
    thread_id = os.environ.get("AGENT_EXEC_TMP_NAME") or os.environ.get("AGENT_THREAD_ID") or "_no_session"
    base = Path(os.environ.get("AGENT_DEFAULT_WORKDIR") or "./data/temp")
    out_dir = base / f"_tmp_{thread_id}" / category
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(Path(source_path).resolve()).encode("utf-8")).hexdigest()[:8]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_path = out_dir / f"{digest}_{timestamp}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    info: dict = {"result_path": str(out_path)}
    pm = register_output_path(out_path, description=f"full JSON of the {category} read result")
    if pm:
        info["path_memory"] = pm
    return info


def summarize_result(result: dict, omit_keys: list[str]) -> dict:
    """result から omit_keys で指定したキーを除いた要約辞書を返す。

    除いたキーがlist型なら "<key>_count"、str型なら "<key>_length" を
    件数/文字数として残す（本文全体はファイル側にのみ残す）。
    """
    summary = dict(result)
    for key in omit_keys:
        if key not in summary:
            continue
        value = summary.pop(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, str):
            summary[f"{key}_length"] = len(value)
    return summary
