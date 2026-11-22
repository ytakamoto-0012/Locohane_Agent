"""再起動せずに効かせるユーザーの設定（src/auth_users.py・src/live_settings.py）のテスト。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src import memory
from src.auth_users import AuthUsersSource
from src.config import load_config
from src.live_settings import MemoryRanksReloader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_INI_PATH = PROJECT_ROOT / "config.ini"


_TOUCH_COUNT = [0]


def _touch_later(path: Path) -> None:
    """更新日時を、呼ぶたびに確実に進める（同じ秒に2回書くと変化を見落とす環境があるため）。"""
    _TOUCH_COUNT[0] += 1
    stamp = time.time() + 10 * _TOUCH_COUNT[0]
    os.utime(path, (stamp, stamp))


def _write_users(path: Path, users: list[list[str]]) -> None:
    path.write_text(f"AUTH_USERS='{json.dumps(users)}'\n", encoding="utf-8")
    _touch_later(path)


def test_auth_users_reload_when_instance_env_changes(tmp_path):
    instance_env = tmp_path / "instance.env"
    project_env = tmp_path / "project.env"
    _write_users(instance_env, [["alice", "pw1"]])
    source = AuthUsersSource(instance_env, project_env, None, {"alice": "pw1"})
    assert source.get() == {"alice": "pw1"}

    # ユーザーの追加・パスワードの変更が、作り直さずに次の get() から効く
    _write_users(instance_env, [["alice", "pw2"], ["bob", "pw3"]])
    assert source.get() == {"alice": "pw2", "bob": "pw3"}

    # 削除も効く
    _write_users(instance_env, [["bob", "pw3"]])
    assert source.get() == {"bob": "pw3"}


def test_auth_users_falls_back_to_os_then_project_env(tmp_path):
    instance_env = tmp_path / "instance.env"
    project_env = tmp_path / "project.env"
    _write_users(project_env, [["proj", "p"]])
    source = AuthUsersSource(instance_env, project_env, None, {})
    _touch_later(project_env)
    # インスタンスの .env に AUTH_USERS が無ければ、プロジェクト直下の .env を引き継ぐ
    assert source.get() == {"proj": "p"}

    # OS の環境変数はプロジェクト直下の .env より優先
    source = AuthUsersSource(instance_env, project_env, '[["os", "o"]]', {})
    _touch_later(project_env)
    assert source.get() == {"os": "o"}

    # インスタンスの .env がいちばん優先
    _write_users(instance_env, [["inst", "i"]])
    assert source.get() == {"inst": "i"}


def test_auth_users_keeps_previous_list_when_env_is_broken(tmp_path):
    instance_env = tmp_path / "instance.env"
    _write_users(instance_env, [["alice", "pw1"]])
    source = AuthUsersSource(instance_env, tmp_path / "project.env", None, {"alice": "pw1"})
    instance_env.write_text("AUTH_USERS='not a list'\n", encoding="utf-8")
    _touch_later(instance_env)
    assert source.get() == {"alice": "pw1"}


@pytest.fixture
def isolated_instance(tmp_path, monkeypatch):
    """実プロジェクトの data/ に触れないよう、保存先と上書きの置き場を tmp へ逃がす。"""
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data"))
    overrides = tmp_path / "config_overrides.json"
    monkeypatch.setenv("CONFIG_OVERRIDES_PATH", str(overrides))
    return overrides


def test_memory_ranks_reload_without_restart(isolated_instance):
    overrides = isolated_instance
    cfg = load_config(config_path=CONFIG_INI_PATH)
    reloader = MemoryRanksReloader(cfg, overrides)
    before = memory.policy_for(cfg.memory_dir)
    assert reloader.refresh() is False  # 変わっていなければ読まない

    overrides.write_text(json.dumps({"memory": {"user_ranks": json.dumps({"boss": 9})}}), encoding="utf-8")
    _touch_later(overrides)
    assert reloader.refresh() is True
    after = memory.policy_for(cfg.memory_dir)
    assert after.rank_of("boss") == 9
    # ランク以外の規則は起動時のまま
    assert after.type_visibility == before.type_visibility
    assert after.checks == before.checks


def test_load_config_can_read_without_reregistering_the_policy(isolated_instance):
    """register_memory_policy=False の読み直しは規則を登録し直さない（再起動で効く設定を、再読み込みの間に
    ほかのセッションが見てしまわないため）。"""
    overrides = isolated_instance
    cfg = load_config(config_path=CONFIG_INI_PATH)
    before = memory.policy_for(cfg.memory_dir)
    current = cfg.memory_type_visibility.get("feedback", "private")
    flipped = "shared" if current == "private" else "private"
    overrides.write_text(json.dumps({"memory": {"feedback_visibility": flipped}}), encoding="utf-8")

    cfg2 = load_config(config_path=CONFIG_INI_PATH, register_memory_policy=False)
    assert cfg2.memory_type_visibility.get("feedback") == flipped  # 値は読めている
    assert memory.policy_for(cfg.memory_dir) == before  # 登録は変わらない

    # 登録する通常呼び出しは規則を変える（呼び出し方の違いを確かめる）
    load_config(config_path=CONFIG_INI_PATH)
    assert memory.policy_for(cfg.memory_dir).type_visibility.get("feedback") == flipped


def test_memory_ranks_keep_previous_when_overrides_are_broken(isolated_instance):
    overrides = isolated_instance
    cfg = load_config(config_path=CONFIG_INI_PATH)
    reloader = MemoryRanksReloader(cfg, overrides)
    before = memory.policy_for(cfg.memory_dir)
    overrides.write_text("{not json", encoding="utf-8")
    _touch_later(overrides)
    assert reloader.refresh() is False
    assert memory.policy_for(cfg.memory_dir) == before
