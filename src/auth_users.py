"""ログインユーザー（AUTH_USERS）を、本体を再起動せずに読み直す。

設定ダッシュボードの「ユーザー」タブは、インスタンスの .env（instances/<名前>/.env）の AUTH_USERS を書き換える。
以前は本体が起動時に読んだ一覧（Config.auth_users）だけを使っていたため、ユーザーの追加・削除・パスワードの変更が
インスタンスを再起動するまで効かなかった（運用で困るとの指摘）。

ログインのたびに .env の更新日時を確かめ、変わっていれば読み直す（変わっていなければ読まない）。
優先度は起動時と同じ「インスタンスの .env > OS の環境変数 > プロジェクト直下の .env」
（app.py の先頭のコメント参照。OS の環境変数は .env を読む前の値を覚えておく）。
読み直した値が書き間違いで読めないときは、前の一覧のまま WARNING を出す（全員がログインできなくなるのを避ける）。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from dotenv import dotenv_values

from src.config import _parse_auth_users

logger = logging.getLogger(__name__)

AUTH_USERS_KEY = "AUTH_USERS"


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class AuthUsersSource:
    """AUTH_USERS を、.env が変わったときだけ読み直して返す。

    Args:
        instance_env: インスタンスの .env（ダッシュボードの「ユーザー」タブが書く）。
        project_env: プロジェクト直下の .env（インスタンスにユーザーが無いときに引き継ぐ）。
        os_value: .env を読む前の OS の環境変数 AUTH_USERS の値（無ければ None）。
        initial: 起動時に読んだ一覧（Config.auth_users）。読み直すまではこれを返す。
    """

    def __init__(self, instance_env: Path, project_env: Path, os_value: str | None, initial: dict[str, str]):
        self._instance_env = instance_env
        self._project_env = project_env
        self._os_value = os_value
        self._users = dict(initial)
        self._stamp = (_mtime(instance_env), _mtime(project_env))
        self._lock = threading.Lock()
        # ユーザー名 → 起動後にパスワードが変わった（追加された）時刻。これより前に発行したログインは使えない。
        self._changed_at: dict[str, float] = {}

    def _raw_value(self) -> str | None:
        if self._instance_env.is_file():
            value = dotenv_values(self._instance_env).get(AUTH_USERS_KEY)
            if value is not None:
                return value
        if self._os_value is not None:
            return self._os_value
        if self._project_env.is_file():
            return dotenv_values(self._project_env).get(AUTH_USERS_KEY)
        return None

    def get(self) -> dict[str, str]:
        """今のログインユーザーの一覧（ユーザー名 → パスワード）。"""
        stamp = (_mtime(self._instance_env), _mtime(self._project_env))
        with self._lock:
            if stamp == self._stamp:
                return self._users
            try:
                users = _parse_auth_users(self._raw_value() or "")
            except ValueError as exc:
                # 書き間違いで読めない: 前の一覧のまま（次に .env が変わったら読み直す）
                logger.warning("Could not reload AUTH_USERS after .env changed; keeping the previous users: %s", exc)
                self._stamp = stamp
                return self._users
            if users != self._users:
                logger.info("Reloaded sign-in users (AUTH_USERS) from .env: %d user(s)", len(users))
                # パスワードを変えた・削除してから入れ直したユーザーは、その前に発行したログイン（JWT）を使えなくする。
                now = time.time()
                for name, password in users.items():
                    if self._users.get(name) != password:
                        self._changed_at[name] = now
            self._users = users
            self._stamp = stamp
            return self._users

    def session_allowed(self, identifier: str, issued_at: float | None) -> bool:
        """ログイン済みのセッション（ユーザー名・JWT の発行時刻 iat）を今も使ってよいか。

        ダッシュボードで削除したユーザーと、パスワードを変えた後に発行し直していないログインは使えない
        （ログインのときだけ確かめていたので、削除してもログイン中のブラウザは JWT が切れるまで使えていた）。
        iat は秒の整数（PyJWT が切り捨てる）なので、変えた時刻も秒に切り捨てて比べる（変えた直後の
        同じ秒のログインを拒まない）。起動前の変更は分からないので、起動時の一覧にいれば通す。
        """
        users = self.get()
        if identifier not in users:
            return False
        with self._lock:
            changed = self._changed_at.get(identifier)
        if changed is None:
            return True
        return issued_at is not None and issued_at >= int(changed)


def token_issued_at(token: str | None) -> float | None:
    """Chainlit の JWT（CHAINLIT_AUTH_SECRET で署名）の発行時刻 iat。読めなければ None。"""
    if not token:
        return None
    secret = os.environ.get("CHAINLIT_AUTH_SECRET")
    if not secret:
        return None
    try:
        import jwt

        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:  # noqa: BLE001 - 壊れた・期限切れのトークンは発行時刻なし（変更があれば拒む）
        return None
    iat = payload.get("iat")
    return float(iat) if isinstance(iat, (int, float)) else None
