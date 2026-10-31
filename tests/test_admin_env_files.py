"""admin/env_files.py のテスト。"""

from __future__ import annotations

import pytest

from admin import env_files


def test_write_and_read_users_roundtrip(tmp_path):
    env_path = tmp_path / ".env"
    env_files.write_users(env_path, {"alice": "pass1", "bob": "pass2"})
    assert env_files.read_users(env_path) == {"alice": "pass1", "bob": "pass2"}


def test_write_users_empty_removes_key(tmp_path):
    env_path = tmp_path / ".env"
    env_files.write_users(env_path, {"alice": "pass1"})
    env_files.write_users(env_path, {})
    assert env_files.read_users(env_path) == {}
    assert "AUTH_USERS" not in env_files.read_values(env_path)


def test_write_users_preserves_other_lines(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("# my comment\nCUSTOM=hello\n", encoding="utf-8")
    env_files.write_users(env_path, {"alice": "pass1"})
    content = env_path.read_text(encoding="utf-8")
    assert "# my comment" in content
    assert "CUSTOM=hello" in content
    assert env_files.read_users(env_path) == {"alice": "pass1"}


def test_password_with_special_characters_roundtrips(tmp_path):
    env_path = tmp_path / ".env"
    tricky_password = 'p@ss "word" with spaces'
    env_files.write_users(env_path, {"alice": tricky_password})
    assert env_files.read_users(env_path) == {"alice": tricky_password}


def test_auth_secret_roundtrip(tmp_path):
    env_path = tmp_path / ".env"
    assert env_files.read_auth_secret(env_path) == ""
    env_files.write_auth_secret(env_path, "sekrit123")
    assert env_files.read_auth_secret(env_path) == "sekrit123"


def test_extra_vars_roundtrip(tmp_path):
    env_path = tmp_path / ".env"
    env_files.write_extra_var(env_path, "TAVILY_API_KEY", "xyz123")
    assert env_files.read_extra_vars(env_path) == {"TAVILY_API_KEY": "xyz123"}
    env_files.delete_extra_var(env_path, "TAVILY_API_KEY")
    assert env_files.read_extra_vars(env_path) == {}


def test_extra_vars_excludes_managed_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_files.write_users(env_path, {"alice": "pass1"})
    env_files.write_auth_secret(env_path, "sekrit")
    env_files.write_extra_var(env_path, "OTHER", "val")
    assert env_files.read_extra_vars(env_path) == {"OTHER": "val"}


def test_write_extra_var_rejects_managed_key(tmp_path):
    env_path = tmp_path / ".env"
    with pytest.raises(env_files.EnvFileError):
        env_files.write_extra_var(env_path, "AUTH_USERS", "x")
    with pytest.raises(env_files.EnvFileError):
        env_files.write_extra_var(env_path, "CHAINLIT_AUTH_SECRET", "x")


def test_write_extra_var_rejects_invalid_name(tmp_path):
    env_path = tmp_path / ".env"
    with pytest.raises(env_files.EnvFileError):
        env_files.write_extra_var(env_path, "not valid!", "x")
    with pytest.raises(env_files.EnvFileError):
        env_files.write_extra_var(env_path, "1STARTSWITHDIGIT", "x")


def test_read_users_missing_file_returns_empty(tmp_path):
    assert env_files.read_users(tmp_path / "nope.env") == {}


def test_read_values_missing_file_returns_empty(tmp_path):
    assert env_files.read_values(tmp_path / "nope.env") == {}


@pytest.mark.parametrize("password", ["pa #ss", "with space", 'quo"te', "back\slash", "日本語#パス"])
def test_write_users_roundtrip_with_special_characters(tmp_path, password):
    """空白・#・引用符を含むパスワードも途中で切れずに読み戻せる（quote_mode="always"）。"""
    env_path = tmp_path / ".env"
    env_files.write_users(env_path, {"alice": password})
    assert env_files.read_users(env_path) == {"alice": password}


def test_write_extra_var_roundtrip_with_hash(tmp_path):
    env_path = tmp_path / ".env"
    env_files.write_extra_var(env_path, "MY_VAR", "a #b")
    assert env_files.read_extra_vars(env_path) == {"MY_VAR": "a #b"}


@pytest.mark.parametrize("key", sorted(env_files.UNTRACKABLE_KEYS) + ["common_data_dir"])
def test_write_extra_var_rejects_untrackable_keys(tmp_path, key):
    """管理ツールの重複チェック・稼働判定と食い違うキーはインスタンス .env に設定させない。"""
    with pytest.raises(env_files.EnvFileError):
        env_files.write_extra_var(tmp_path / ".env", key, "x")


def test_write_extra_var_rejects_non_ascii_key(tmp_path):
    with pytest.raises(env_files.EnvFileError):
        env_files.write_extra_var(tmp_path / ".env", "変数", "x")
