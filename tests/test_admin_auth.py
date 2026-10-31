"""admin/auth.py のテスト。"""

from __future__ import annotations

import time

from admin import auth


def test_load_admin_users_missing_file_returns_empty(tmp_path):
    assert auth.load_admin_users(tmp_path / "nope.env") == {}


def test_load_admin_users_parses_env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text('ADMIN_USERS=[["alice", "secret1"], ["bob", "secret2"]]\n', encoding="utf-8")
    users = auth.load_admin_users(env_path)
    assert users == {"alice": "secret1", "bob": "secret2"}


def test_load_admin_users_missing_key_returns_empty(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SOMETHING_ELSE=1\n", encoding="utf-8")
    assert auth.load_admin_users(env_path) == {}


def test_verify_password_correct_and_incorrect():
    users = {"alice": "secret1"}
    assert auth.verify_password(users, "alice", "secret1") is True
    assert auth.verify_password(users, "alice", "wrong") is False
    assert auth.verify_password(users, "nobody", "anything") is False


def test_session_store_create_and_validate():
    store = auth.SessionStore(timeout_minutes=60)
    token = store.create("alice")
    assert store.validate(token) == "alice"
    assert store.validate("bogus-token") is None
    assert store.validate(None) is None


def test_session_store_revoke():
    store = auth.SessionStore(timeout_minutes=60)
    token = store.create("alice")
    store.revoke(token)
    assert store.validate(token) is None


def test_session_store_expiry():
    store = auth.SessionStore(timeout_minutes=0)  # 0分 = 即座に期限切れ
    token = store.create("alice")
    time.sleep(0.05)
    assert store.validate(token) is None


def test_login_throttle_locks_after_max_attempts():
    throttle = auth.LoginThrottle(max_attempts=3, lockout_seconds=60)
    ip = "10.0.0.1"
    assert throttle.is_locked_out(ip) is False
    for _ in range(3):
        throttle.record_failure(ip)
    assert throttle.is_locked_out(ip) is True


def test_login_throttle_success_resets():
    throttle = auth.LoginThrottle(max_attempts=2, lockout_seconds=60)
    ip = "10.0.0.2"
    throttle.record_failure(ip)
    throttle.record_failure(ip)
    assert throttle.is_locked_out(ip) is True
    throttle.record_success(ip)
    assert throttle.is_locked_out(ip) is False


def test_login_throttle_expires_after_lockout_window():
    throttle = auth.LoginThrottle(max_attempts=1, lockout_seconds=0.05)
    ip = "10.0.0.3"
    throttle.record_failure(ip)
    assert throttle.is_locked_out(ip) is True
    time.sleep(0.1)
    assert throttle.is_locked_out(ip) is False


def test_login_throttle_is_per_ip():
    throttle = auth.LoginThrottle(max_attempts=1, lockout_seconds=60)
    throttle.record_failure("1.1.1.1")
    assert throttle.is_locked_out("1.1.1.1") is True
    assert throttle.is_locked_out("2.2.2.2") is False


def test_has_csrf_header_present_and_absent():
    assert auth.has_csrf_header({"x-locohane-admin": "1"}) is True
    assert auth.has_csrf_header({}) is False
    assert auth.has_csrf_header({"x-locohane-admin": "0"}) is False


def test_session_store_expired_token_validate_twice_does_not_raise():
    """期限切れトークンの同時検証（2回目は既に掃除済み）でも KeyError にならない。"""
    store = auth.SessionStore(timeout_minutes=60)
    token = store.create("alice")
    # sleep に頼ると Windows の monotonic 分解能（約15ms）で期限切れにならないことがある。
    store._sessions[token].expires_at = time.monotonic() - 1

    class _RacyDict(dict):
        """get() は期限切れセッションを返すが、掃除の直前に別スレッドが消した状況を再現する。"""

        def get(self, key, default=None):
            value = super().get(key, default)
            super().pop(key, None)
            return value

    store._sessions = _RacyDict(store._sessions)
    assert store.validate(token) is None
