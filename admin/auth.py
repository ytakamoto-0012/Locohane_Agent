"""管理ツール自身の認証（ADMIN_USERS）・セッション管理・ログイン試行制限。

Locohane本体のログインユーザー（instances/<name>/.env の AUTH_USERS、
[auth]セクション）とは別の名前空間。管理者は、プロジェクト直下 .env の
ADMIN_USERS（``[["user","pass"], ...]`` 形式）に列挙されたユーザーのみで、
パスワードは常に必須（本体の [auth].require_password の設定には左右されない。
管理ツールは本体より強い権限を持つため）。
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import dotenv

from src.config import _parse_auth_users

# Chainlit本体が使う access_token Cookie と衝突しないよう、固有の名前にする
# （同じホスト名の別ポートで本体と管理ツールを同時に開いた場合の混線防止）。
SESSION_COOKIE_NAME = "locohane_admin_session"
# 更新系リクエストのCSRF対策として必須にするヘッダ名（値は "1" 固定）。
# SameSite=Strict Cookie と併用し、他サイトからの偽装フォーム送信等を防ぐ。
CSRF_HEADER_NAME = "x-locohane-admin"

# ログイン試行制限: 同一IPからの失敗がこの回数に達すると、この秒数拒否する。
DEFAULT_MAX_LOGIN_ATTEMPTS = 5
DEFAULT_LOGIN_LOCKOUT_SECONDS = 300.0


class AuthError(Exception):
    """auth.py の操作失敗。"""


def load_admin_users(project_env_path: Path) -> dict[str, str]:
    """プロジェクト直下 .env の ADMIN_USERS を {ユーザー名: パスワード} で返す。

    ファイルが無い、または ADMIN_USERS が未設定なら空dict（呼び出し元の
    admin/server.py が、空なら起動を拒否する）。
    """
    if not project_env_path.is_file():
        return {}
    values = dotenv.dotenv_values(str(project_env_path))
    raw = values.get("ADMIN_USERS") or ""
    return _parse_auth_users(raw)


def verify_password(admin_users: dict[str, str], username: str, password: str) -> bool:
    """定数時間比較でパスワードを検証する（hmac.compare_digestでタイミング攻撃を軽減）。"""
    stored = admin_users.get(username)
    if stored is None:
        # ユーザーが存在しない場合も、存在確認の時間差を減らすためダミー比較を行う。
        hmac.compare_digest(password, "")
        return False
    return hmac.compare_digest(password, stored)


@dataclass
class _Session:
    username: str
    expires_at: float


class SessionStore:
    """メモリ上のログインセッション管理。プロセス再起動で全セッションが失効する。"""

    def __init__(self, timeout_minutes: int):
        self._timeout_seconds = timeout_minutes * 60
        self._sessions: dict[str, _Session] = {}

    def create(self, username: str) -> str:
        """新しいセッショントークンを発行する。"""
        token = secrets.token_urlsafe(32)
        self._sessions[token] = _Session(username=username, expires_at=time.monotonic() + self._timeout_seconds)
        return token

    def validate(self, token: str | None) -> str | None:
        """トークンが有効ならユーザー名を返す。無効・期限切れなら None（期限切れは掃除する）。

        有効なアクセスのたびに有効期限を延長する（スライディングタイムアウト）。
        """
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at < time.monotonic():
            # pop: 同じ期限切れトークンでの同時リクエスト（スレッドプールで並行実行）
            # で del が KeyError になり、401 ではなく 500 を返すのを防ぐ。
            self._sessions.pop(token, None)
            return None
        session.expires_at = time.monotonic() + self._timeout_seconds
        return session.username

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)


class LoginThrottle:
    """IPごとのログイン失敗回数を記録し、一定回数を超えたら一時的に拒否する。"""

    def __init__(
        self,
        max_attempts: int = DEFAULT_MAX_LOGIN_ATTEMPTS,
        lockout_seconds: float = DEFAULT_LOGIN_LOCKOUT_SECONDS,
    ):
        self._max_attempts = max_attempts
        self._lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = {}

    def is_locked_out(self, remote_addr: str) -> bool:
        now = time.monotonic()
        attempts = [t for t in self._failures.get(remote_addr, []) if now - t < self._lockout_seconds]
        self._failures[remote_addr] = attempts
        return len(attempts) >= self._max_attempts

    def record_failure(self, remote_addr: str) -> None:
        self._failures.setdefault(remote_addr, []).append(time.monotonic())

    def record_success(self, remote_addr: str) -> None:
        self._failures.pop(remote_addr, None)


def has_csrf_header(headers) -> bool:
    """更新系リクエストのCSRF対策ヘッダ（X-Locohane-Admin: 1）の有無を確認する。

    Args:
        headers: `.get(name)` を持つマッピング（FastAPI/Starletteの
            Headers は大文字小文字を区別せず取得できる。プレーンな辞書を
            渡すテストのために、代表的な大文字始まりの綴りもフォールバック
            で試す）。
    """
    value = headers.get(CSRF_HEADER_NAME)
    if value is None:
        value = headers.get("X-Locohane-Admin")
    return value == "1"
