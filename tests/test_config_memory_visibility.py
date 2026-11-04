"""config.ini [memory]（永続メモリーの公開範囲の規則）の読み込みと、システムプロンプトへの差し込みのテスト。"""

from __future__ import annotations

import json

import pytest

from src import memory
from src.config import (
    _as_memory_criteria,
    _parse_memory_force_private_checks,
    _parse_memory_type_visibility,
    load_config,
    render_memory_visibility_block,
)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """実プロジェクトの data/ にテスト用ディレクトリを作らないよう、データ保存先を tmp へ逃がす。"""
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))
    monkeypatch.setattr(memory, "_POLICIES", {})


def test_defaults_of_config_ini_match_code_defaults(tmp_path):
    cfg = load_config(overrides_path=tmp_path / "missing.json")
    assert cfg.memory_type_visibility == memory.DEFAULT_TYPE_VISIBILITY
    assert cfg.memory_shared_criteria == memory.DEFAULT_SHARED_CRITERIA
    assert cfg.memory_private_criteria == memory.DEFAULT_PRIVATE_CRITERIA
    assert cfg.memory_force_private_checks == memory.DEFAULT_PRIVATE_CHECKS
    # load_config() が memory_dir に規則を登録する
    assert memory.policy_for(cfg.memory_dir) == memory.MemoryPolicy(
        type_visibility=memory.DEFAULT_TYPE_VISIBILITY, checks=memory.DEFAULT_PRIVATE_CHECKS
    )


def test_overrides_are_registered_and_rendered(tmp_path):
    ov = tmp_path / "overrides.json"
    ov.write_text(
        json.dumps(
            {
                "memory": {
                    "project_visibility": "private",
                    "private_criteria": "- 顧客名\n- 金額",
                    "force_private_checks": '["email"]',
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(overrides_path=ov)
    policy = memory.policy_for(cfg.memory_dir)
    assert policy.private_types() == ("user", "project")
    assert policy.checks == frozenset({"email"})
    block = render_memory_visibility_block(cfg)
    assert cfg.memory_private_criteria == "- 顧客名\n- 金額"
    assert '- "private" for:\n  - 顧客名\n  - 金額\n' in block
    assert "type user/project are always private" in block
    assert "email address" in block and "credential" not in block


def test_parse_type_visibility():
    assert _parse_memory_type_visibility({"feedback_visibility": " Private "})["feedback"] == "private"
    with pytest.raises(ValueError, match=r"\[memory\]\.user_visibility"):
        _parse_memory_type_visibility({"user_visibility": "public"})


def test_criteria_keep_line_breaks_and_fall_back_to_default():
    assert _as_memory_criteria("- a  \n\n- b ", "x") == "- a\n- b"
    assert _as_memory_criteria("   ", "x") == "x"


def test_criteria_continuation_lines_in_config_ini(tmp_path, monkeypatch):
    # config.ini に直接書くときは、2行目以降を字下げした継続行で改行を表す
    from src import config as config_module

    ini = config_module.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8").replace(
        "private_criteria = anything about the user personally or their family",
        "private_criteria = - 本人のこと\n    - 家族のこと",
    )
    path = tmp_path / "config.ini"
    path.write_text(ini, encoding="utf-8")
    cfg = load_config(path, overrides_path=tmp_path / "missing.json")
    assert cfg.memory_private_criteria == "- 本人のこと\n- 家族のこと"


def test_parse_force_private_checks():
    assert _parse_memory_force_private_checks(None) == memory.DEFAULT_PRIVATE_CHECKS
    assert _parse_memory_force_private_checks("[]") == frozenset()
    assert _parse_memory_force_private_checks('["phone", "email",]') == frozenset({"phone", "email"})
    with pytest.raises(ValueError, match="force_private_checks"):
        _parse_memory_force_private_checks('["ssn"]')


def test_system_prompt_has_placeholder():
    from src.config import DEFAULT_CONFIG_PATH

    prompt = (DEFAULT_CONFIG_PATH.parent / "system_prompt" / "system_prompt.md").read_text(encoding="utf-8")
    assert "{{memory_visibility}}" in prompt


def test_parse_user_ranks():
    from src.config import _parse_memory_user_ranks

    assert _parse_memory_user_ranks("") == {}
    assert _parse_memory_user_ranks('{"Alice": 3, "bob": "2",}') == {"alice": 3, "bob": 2}
    for bad in ("[1]", '{"a": -1}', '{"a": "x"}', '{"a": true}'):
        with pytest.raises(ValueError, match="user_ranks"):
            _parse_memory_user_ranks(bad)


def test_ranks_are_registered(tmp_path):
    ov = tmp_path / "overrides.json"
    ov.write_text(json.dumps({"memory": {"user_ranks": '{"Boss": 5}', "default_rank": "2"}}), encoding="utf-8")
    cfg = load_config(overrides_path=ov)
    policy = memory.policy_for(cfg.memory_dir)
    assert (policy.rank_of("boss"), policy.rank_of("staff"), policy.rank_of(None)) == (5, 2, 2)


def test_empty_rank_env_is_treated_as_unset(tmp_path, monkeypatch):
    # .env の "MEMORY_USER_RANKS=" のような空の環境変数で、ini・ダッシュボードのランクを無視しない
    monkeypatch.setenv("MEMORY_USER_RANKS", "")
    ov = tmp_path / "overrides.json"
    ov.write_text(json.dumps({"memory": {"user_ranks": '{"Boss": 5}'}}), encoding="utf-8")
    cfg = load_config(overrides_path=ov)
    assert cfg.memory_user_ranks == {"boss": 5}
