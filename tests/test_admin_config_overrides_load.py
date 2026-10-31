"""src.config.load_config() の config_overrides.json 適用ロジックのテスト。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from src.config import DEFAULT_CONFIG_PATH, load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """実プロジェクトの data/ にテスト用ディレクトリを作らないよう、データ保存先を tmp へ逃がす。"""
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))


def _write_overrides(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_no_overrides_file_behaves_like_default(tmp_path):
    missing = tmp_path / "nope.json"
    cfg_without = load_config(overrides_path=missing)
    cfg_default = load_config(overrides_path=tmp_path / "also_missing.json")
    assert dataclasses.asdict(cfg_without) == dataclasses.asdict(cfg_default)


def test_override_value_applied(tmp_path):
    ov_path = tmp_path / "overrides.json"
    _write_overrides(ov_path, {"ui": {"max_display_messages": "77"}})
    cfg = load_config(overrides_path=ov_path)
    assert cfg.ui_max_display_messages == 77


def test_env_var_takes_priority_over_override(tmp_path, monkeypatch):
    ov_path = tmp_path / "overrides.json"
    _write_overrides(ov_path, {"ui": {"max_display_messages": "77"}})
    monkeypatch.setenv("UI_MAX_DISPLAY_MESSAGES", "999")
    cfg = load_config(overrides_path=ov_path)
    assert cfg.ui_max_display_messages == 999


def test_unknown_section_ignored_with_warning(tmp_path, caplog):
    ov_path = tmp_path / "overrides.json"
    _write_overrides(ov_path, {"nope_section": {"x": "1"}})
    cfg = load_config(overrides_path=ov_path)
    assert cfg is not None  # 例外を投げず起動を継続する


def test_unknown_key_ignored_with_warning(tmp_path):
    ov_path = tmp_path / "overrides.json"
    _write_overrides(ov_path, {"ui": {"nope_key": "1"}})
    cfg = load_config(overrides_path=ov_path)
    assert cfg.ui_max_display_messages == 50  # 既定値のまま


def test_admin_section_override_ignored(tmp_path):
    ov_path = tmp_path / "overrides.json"
    _write_overrides(ov_path, {"admin": {"port": "9999"}})
    cfg = load_config(overrides_path=ov_path)
    # config.ini の値のまま（値自体は運用で変わるため、上書きなしの読込結果と比べる）
    cfg_default = load_config(overrides_path=tmp_path / "missing.json")
    assert cfg.admin_port == cfg_default.admin_port
    assert cfg.admin_port != 9999


def test_malformed_json_raises(tmp_path):
    ov_path = tmp_path / "overrides.json"
    ov_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(Exception):
        load_config(overrides_path=ov_path)


def test_non_dict_toplevel_raises(tmp_path):
    ov_path = tmp_path / "overrides.json"
    ov_path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(overrides_path=ov_path)


def test_multiline_value_override_applied(tmp_path):
    ov_path = tmp_path / "overrides.json"
    value = '[\n    {"base_url": "http://example/v1", "api_key": "x", "model": "m"},\n    ]'
    _write_overrides(ov_path, {"llm": {"main_url": value}})
    cfg = load_config(overrides_path=ov_path)
    assert len(cfg.main_endpoints) == 1
    assert cfg.main_endpoints[0].base_url == "http://example/v1"


def test_skill_registration_lists_are_added_to_defaults(tmp_path, monkeypatch):
    """スキルの登録系のリストは既定値を置き換えず、既定値に無い項目だけを足す（既定値の項目は消せない）。"""
    for env in ("PLAN_APPROVAL_EXEMPT_SCRIPTS", "SUBAGENT_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST", "MAIN_AGENT_TOOL_GUARD_ALLOW_ENTRIES"):
        monkeypatch.delenv(env, raising=False)
    baseline = load_config(overrides_path=tmp_path / "missing.json")
    ov_path = tmp_path / "overrides.json"
    _write_overrides(
        ov_path,
        {
            "plan": {"plan_approval_exempt_scripts": '[["my-skill", "run.py"]]'},
            "subagent": {"agent_type_run_script_allowlist": '[["my-agent", "excel-read"]]'},
            # Glob は既定値にある対象なので、上限回数を変えようとしても既定値のまま
            "main_agent_tool_guard": {"allow_entries": '[["Glob", 99], [["my-skill", "run.py"], -1]]'},
        },
    )
    cfg = load_config(overrides_path=ov_path)
    assert cfg.plan_approval_exempt_scripts == baseline.plan_approval_exempt_scripts | {("my-skill", "run.py")}
    assert cfg.subagent_agent_type_run_script_allowlist == baseline.subagent_agent_type_run_script_allowlist | {
        ("my-agent", "excel-read")
    }
    allow = dict(cfg.main_agent_tool_guard_allow_entries)
    assert allow["Glob"] == dict(baseline.main_agent_tool_guard_allow_entries)["Glob"]
    assert allow[("my-skill", "run.py")] == -1
    assert set(dict(baseline.main_agent_tool_guard_allow_entries)) <= set(allow)


def test_all_keys_roundtrip_to_identical_config(tmp_path):
    """全キーを自分自身の既定値で上書きすると、上書きなしと完全に同じConfigになる。"""
    from admin.ini_catalog import parse_file

    catalog = parse_file(DEFAULT_CONFIG_PATH)
    overrides_data: dict[str, dict[str, str]] = {}
    for info in catalog.keys:
        if info.section == "admin":
            continue
        overrides_data.setdefault(info.section, {})[info.key] = info.default_value

    ov_path = tmp_path / "overrides.json"
    _write_overrides(ov_path, overrides_data)

    baseline = load_config(overrides_path=tmp_path / "missing.json")
    with_self_overrides = load_config(overrides_path=ov_path)
    assert dataclasses.asdict(baseline) == dataclasses.asdict(with_self_overrides)


def test_instance_placeholder_defaults_to_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCOHANE_INSTANCE", raising=False)
    monkeypatch.delenv("COMMON_DATA_DIR", raising=False)
    ov_path = tmp_path / "overrides.json"
    _write_overrides(ov_path, {"paths": {"common_data_dir": str(tmp_path / "data" / "${instance}")}})
    cfg = load_config(overrides_path=ov_path)
    assert cfg.checkpoint_db == tmp_path / "data" / "default" / "checkpoints.sqlite"


def test_instance_placeholder_from_env_and_argument(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMON_DATA_DIR", raising=False)
    ov_path = tmp_path / "overrides.json"
    _write_overrides(ov_path, {"paths": {"common_data_dir": str(tmp_path / "data" / "${instance}")}})
    monkeypatch.setenv("LOCOHANE_INSTANCE", "fromenv")
    assert load_config(overrides_path=ov_path).checkpoint_db.parent == tmp_path / "data" / "fromenv"
    # 引数は環境変数より優先される。
    assert load_config(overrides_path=ov_path, instance_name="fromarg").checkpoint_db.parent == tmp_path / "data" / "fromarg"


def test_instance_placeholder_in_other_path_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_DIR", raising=False)
    ov_path = tmp_path / "overrides.json"
    _write_overrides(ov_path, {"paths": {"memory_dir": str(tmp_path / "shared_memory" / "${instance}")}})
    cfg = load_config(overrides_path=ov_path, instance_name="qwen")
    assert cfg.memory_dir == tmp_path / "shared_memory" / "qwen"


def test_instance_placeholder_in_common_data_dir_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "envdata" / "${instance}"))
    cfg = load_config(overrides_path=tmp_path / "missing.json", instance_name="x1")
    assert cfg.checkpoint_db.parent == tmp_path / "envdata" / "x1"
