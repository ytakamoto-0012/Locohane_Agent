"""パスメモリー（@N参照）関連の共有ヘルパー。"""

from __future__ import annotations

import chainlit as cl
import re
from pathlib import Path

from .. import path_memory

from . import _state


# `@N` 単体、または `@N/子パス`・`@N\子パス`（モデルはフォルダの `@N` の中のファイルをこう書く）。
_PATH_MEMORY_TOKEN_RE = re.compile(r"^@(\d+)(?:[\\/](.+))?$")


def _resolve_path_memory_token(value: str) -> tuple[str, str | None]:
    """value が `@N`（または `@N/子パス`・`@N\\子パス`）なら実パスへ解決する。

    子パスの結合はこのヘルパーで行う。path_memory.resolve() は `@N` 単体だけを扱うため、
    execute_python_code のスクリプト側から呼ぶ path_memory.resolve() は引き続き `@N` 単体しか
    解決しない（skills/SKILLS_README.md 4-7-2 参照）。

    Args:
        value: analyze_image の relative_path や run_script の args の
            要素として渡された文字列。

    Returns:
        (解決後の値, エラーメッセージ) のタプル。
        - `@N` 形式でなければ (value, None)（従来通りそのまま使う）。
        - 登録済み `@N` なら実パスを返す。子パスがあれば、エントリがフォルダの
          場合に限り結合した実パスを返す。エントリがファイルなのに子パスを
          続けた場合はエラー（黙って結合すると「存在しないパス」扱いになり、
          suggest_from_path_memory の類似候補＝別のファイルで処理が進みうるため）。
        - パスメモリーが利用できない・未登録の場合は (value, エラー文)。
          エラー文は常に基となる `@N` の名前で述べる（`@2/photo_01.png` の
          ような文字列全体をそのまま載せない）。
          呼び出し側はこのエラー文をそのまま「エラー: ...」として返すこと。
    """
    match = _PATH_MEMORY_TOKEN_RE.match(value)
    if not match:
        return value, None
    token, rest = f"@{match.group(1)}", match.group(2)
    if _state._PATH_MEMORY_DIR is None:
        return value, f"Path memory is not available: {token}"
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    resolved = path_memory.resolve(thread_id, token, _state._PATH_MEMORY_DIR)
    if resolved is None:
        return value, (f"Path memory {token} is not registered. " "Pass a file name to search_path_memory to find a registered @N.")
    if not rest:
        return resolved, None
    entry = Path(resolved)
    if not entry.is_dir():
        return value, f"Path memory {token} is a file ({resolved}); cannot append a path after it. Pass {token} alone."
    return str(entry / rest), None


def suggest_from_path_memory(query: str, top_k: int = 3) -> str:
    """見つからなかったパスに似た登録済みパスを、エラー文に追記するヒントとして返す。

    ローカルLLMがパスを記憶から手打ちして誤字を起こした場合に、正しい `@N` へ
    誘導するためのもの（`_file_tools_common.suggest_similar_dir` と同じ形式）。
    候補は指定パスとは別のファイルなので、各候補に何が違うかを添え、
    task・ユーザー指定のパスを候補で勝手に置き換えないよう文言で抑える
    （別フォルダの同名ファイル＝旧版・バックアップ等を黙って使うと、
    エラーにならないまま誤ったデータで処理が進むため）。

    存在しないパスは search_entries 側で除外されるため、見つからなかった
    パス自体（表記ゆれ違いを含む）が候補に出ることはない。

    Args:
        query: 見つからなかったパス文字列。
        top_k: 提示する最大件数。

    Returns:
        先頭に半角スペースを付けたヒント文字列。パスメモリーが使えない、
        または実在する類似候補が無ければ空文字列。
    """
    if _state._PATH_MEMORY_DIR is None:
        return ""
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    hits = path_memory.search_entries(
        thread_id,
        query,
        _state._PATH_MEMORY_DIR,
        top_k=top_k,
        min_score=_state._PATH_MEMORY_SEARCH_MIN_SCORE,
        filename_weight=_state._PATH_MEMORY_SEARCH_FILENAME_WEIGHT,
    )
    if not hits:
        return ""
    candidates = ", ".join(f"@{e['index']} ({path_memory.difference_label(query, e['path'])}: {e['path']})" for e in hits)
    return (
        f" There are similar entries in path memory: {candidates}. "
        "If the path you wrote from memory was wrong, use that @N. If it is a path given by the task or the user, do not replace it; report that it was not found."
    )


_PATH_MEMORY_TEXT_TOKEN_RE = re.compile(r"(?<![\w@])@(\d+)\b")


def _resolve_path_memory_tokens_in_text(text: str) -> str:
    """自由記述テキスト中に埋め込まれた `@N` パスメモリー参照を実パスへ置換する。

    dispatch_agent の task 引数のように、モデルが自然文の中でパスに触れる場面では
    文字列全体が `@N` のみであることを前提とする _resolve_path_memory_token は使えない。
    このヘルパーは文中に複数含まれうる `@N` を正規表現で検出し、解決できたものだけを
    実パスへ置き換える。未登録・パスメモリー機能が使えない等で解決できないトークンは
    エラーにせず元の `@N` 文字列のまま残す（自由文の一部が理由で呼び出し自体を
    失敗させないため）。

    Args:
        text: `@N` を含みうる自由記述テキスト（dispatch_agent の task 等）。

    Returns:
        解決できた `@N` を実パスへ置換したテキスト。`@N` を含まない、または
        どれも解決できなかった場合は text をそのまま返す。
    """

    def _replace(match: re.Match) -> str:
        token = match.group(0)
        resolved, error = _resolve_path_memory_token(token)
        return token if error else resolved

    return _PATH_MEMORY_TEXT_TOKEN_RE.sub(_replace, text)

_RAW_UNC_PATH_RE = re.compile(r"\\\\[A-Za-z0-9_.\-]+(?:\\[A-Za-z0-9_.\-]+)+")


def register_raw_unc_paths_in_text(text: str) -> str:
    """ユーザー入力テキスト中の生UNCパスを path_memory へ事前登録し `@N` へ置換する。

    低パラメータモデルはツール呼び出しのJSON argsにUNCパス（`\\\\server\\share\\...`）
    を書き起こす際にバックスラッシュのエスケープを誤りやすい（ISSUE-002）。
    ユーザーがチャット本文に生のUNCパスを直接書いた場合、そのままではLLMが
    このパスを手で再構築する必要が生じるため、on_message でLLMに渡す前に
    本文中のUNCパスを検出して path_memory へ登録し、本文中の当該箇所を
    `@N` に置き換える。これによりLLMは以後そのターンから `@N` を使えばよく、
    生のUNCパス文字列を手で書き起こす場面自体を無くす。

    検出対象はUNCパスの正常形のみ（`\\\\server\\share` 以上の深さを要求し、
    セグメントはASCII英数字・`_`・`.`・`-` に限定して地の文の巻き込みを防ぐ）。
    ローカル絶対パス（`C:\\...`）や、既にLLMの誤変換で崩れた二重バックスラッシュ
    形状の検出は今回のスコープ外（将来必要になれば別途拡張する）。

    Args:
        text: ユーザーのメッセージ本文（message.content）。

    Returns:
        検出したUNCパスを `@N` に置換したテキスト。path_memory機能が
        利用できない場合やUNCパスを含まない場合は text をそのまま返す。
    """
    if _state._PATH_MEMORY_DIR is None:
        return text
    thread_id = cl.user_session.get("thread_id") or "_no_session"

    def _replace(match: re.Match) -> str:
        path = match.group(0)
        index = path_memory.register(
            thread_id,
            path,
            _state._PATH_MEMORY_DIR,
            _state._PATH_MEMORY_MAX_ENTRIES,
            description="user input",
        )
        return f"@{index}" if index is not None else path

    return _RAW_UNC_PATH_RE.sub(_replace, text)

def _register_path_memory(paths: list[str], description: str | None = None) -> dict[str, str]:
    """パスの一覧を path_memory レジストリへ登録し、{"@N": path, ...} を返す。

    旧 skills/file-tools/scripts/_common.py の register_paths()（run_script
    サブプロセス経由・環境変数依存）を、同一プロセス内の直接呼び出しに
    置き換えたもの（Read/Glob/Grep/json_query が使う）。

    Args:
        paths: 登録したい絶対パス文字列のリスト。
        description: 各パスに添える短い説明（省略可）。

    Returns:
        {"@N": path, ...} の辞書。path_memory が利用できない環境でも
        例外を投げず空辞書を返す。
    """
    if _state._PATH_MEMORY_DIR is None:
        return {}
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    result: dict[str, str] = {}
    for path in paths:
        index = path_memory.register(thread_id, path, _state._PATH_MEMORY_DIR, _state._PATH_MEMORY_MAX_ENTRIES, description=description)
        if index is not None:
            result[f"@{index}"] = path
    return result


def _dedupe_paths_with_path_memory(result: dict, path_memory_map: dict[str, str]) -> None:
    """Glob 結果に含まれるフルパスの重複を `@N` 参照へ置き換える（in-place）。

    `glob_search()` の戻り値は同じ絶対パスを `files`・`file_details[].path`・
    `directories[].path` に持ち、さらに `path_memory` にも同じパスが載るため、
    1件あたり同じ長い文字列が最大4回会話履歴へ積まれていた。297件のフォルダを
    Glob した実測で1回の ToolMessage が約10万文字に達し、数回の呼び出しだけで
    コンテキスト上限（128k）を使い切って処理が中断する事象が eval
    （レシピ画像297枚ケース）で観測された。

    `@N` はそのまま各ツールの絶対パス引数へ渡せる（`_resolve_path_memory_token`
    が解決する）ため、`path_memory` に対応表がある限り情報は失われない。

    Args:
        result: `glob_search()` の戻り値（この関数が直接書き換える）。
        path_memory_map: `{"@N": 絶対パス}`。空なら何もしない。
    """
    if not path_memory_map:
        return
    token_by_path = {path: token for token, path in path_memory_map.items()}
    result["files"] = [token_by_path.get(p, p) for p in result.get("files", [])]
    for detail in result.get("file_details", []):
        detail["path"] = token_by_path.get(detail["path"], detail["path"])
    for directory in result.get("directories", []):
        directory["path"] = token_by_path.get(directory["path"], directory["path"])
