"""会話ログ（ユーザー発言とAIの最終応答）のテキストファイル書き出し。

役割はここまで（それだけ）:

1. ログイン中のユーザー識別子（未ログインなら "anonymous"）ごとにディレクトリを分ける。
2. <chat_log_dir>/<ユーザー名>/<日付>_<thread_id>.log という1セッション1ファイルへ、
   ユーザー発言とAIの最終応答を1ターンずつ追記する。

Chainlit には依存しない（呼び出し側の app.py が cl.User から identifier 文字列を
取り出して渡す）。データベースは使わず、素朴なテキスト追記のみを行う。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

ANONYMOUS_USERNAME = "anonymous"

# ディレクトリ名として使えない文字（Windowsの予約文字）を置換する。
_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


def resolve_log_username(identifier: str | None) -> str:
    """ログイン中のユーザー識別子をログ用のディレクトリ名へ変換する。

    Args:
        identifier: cl.User.identifier（未ログインなら None）。

    Returns:
        identifier をそのまま使えない文字だけ "_" に置換した文字列。
        identifier が None または空文字なら ANONYMOUS_USERNAME
        （[auth] enabled=false 等、未ログイン時に全ユーザー分を1つにまとめる）。
    """
    if not identifier:
        return ANONYMOUS_USERNAME
    return _UNSAFE_CHARS_RE.sub("_", identifier)


def build_log_path(chat_log_dir: Path, username: str, thread_id: str) -> Path:
    """<chat_log_dir>/<username>/<YYYY-MM-DD_HH-MM-SS>_<thread_id>.log のパスを組み立てる。

    日時はこの関数を呼んだ時点のローカル日時。呼び出し側（app.py の
    on_chat_start）がセッション開始時に1回だけ呼んでパスを確定させ、
    以降はセッション中ずっと同じファイルに追記する想定
    （日をまたいでもファイルは変わらない）。

    Args:
        chat_log_dir: 会話ログの保存先ルートディレクトリ（config.ini の
            [chat_log].dir）。
        username: resolve_log_username() で解決済みのユーザー名。
        thread_id: このチャットセッションの thread_id。

    Returns:
        ログファイルの絶対パス(親ディレクトリの作成はしない。
        実際に書き込む append_turn() 側で行う)。
    """
    date_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return chat_log_dir / username / f"{date_str}_{thread_id}.log"


def find_existing_log_path(chat_log_dir: Path, username: str, thread_id: str) -> Path | None:
    """同じ thread_id で過去に作成済みのログファイルを探す（スレッド再開用）。

    ファイル名は build_log_path() が付ける日時プレフィックスを含むため、
    thread_id だけでは決め打ちできず glob で検索する必要がある。

    Args:
        chat_log_dir: 会話ログの保存先ルートディレクトリ（config.ini の
            [chat_log].dir）。
        username: resolve_log_username() で解決済みのユーザー名。
        thread_id: 再開先スレッドの thread_id。

    Returns:
        見つかった既存ログファイルの絶対パス。複数該当する場合は最新
        （ファイル名の日時プレフィックスが最も新しいもの）。無ければ None。
    """
    candidates = sorted((chat_log_dir / username).glob(f"*_{thread_id}.log"))
    return candidates[-1] if candidates else None


def append_resume_marker(log_path: Path) -> None:
    """スレッド再開時に「いつ再開したか」が分かる区切り行を追記する。

    Args:
        log_path: build_log_path()/find_existing_log_path() で確定した
            ログファイルの絶対パス。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"----- resumed {timestamp} -----\n\n")


def append_turn(
    log_path: Path,
    user_text: str,
    ai_text: str,
    token_usage_cumulative: dict | None = None,
) -> None:
    """1ターン分（ユーザー発言・AIの最終応答）をタイムスタンプ付きで追記する。

    Args:
        log_path: build_log_path() で組み立てたログファイルの絶対パス。
        user_text: ユーザーが送信したメッセージ本文。
        ai_text: そのターンでのAIの最終応答本文。
        token_usage_cumulative: トークン使用量の会話累計（サブエージェント含む）。
            {"input", "output", "total"} を持つ dict。None なら追記しない
            （[llm].track_token_usage=false 等、集計されていない場合）。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] USER: {user_text}\n")
        f.write(f"[{timestamp}] AI: {ai_text}\n")
        if token_usage_cumulative is not None:
            f.write(
                f"[{timestamp}] Cumulative conversation token usage (including sub-agents) "
                f"input: {token_usage_cumulative['input']:,} / "
                f"output: {token_usage_cumulative['output']:,} / "
                f"total: {token_usage_cumulative['total']:,}\n"
            )
        f.write("\n")
