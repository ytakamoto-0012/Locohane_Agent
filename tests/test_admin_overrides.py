"""admin/overrides.py のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from admin import overrides
from admin.ini_catalog import parse_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_INI_PATH = PROJECT_ROOT / "config.ini"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """実プロジェクトの data/ にテスト用ディレクトリを作らないよう、データ保存先を tmp へ逃がす。"""
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))


@pytest.fixture
def catalog():
    return parse_file(CONFIG_INI_PATH)


@pytest.fixture
def workdir(tmp_path):
    return tmp_path


# base_mtime を省略した場合は「直前に最新状態を読み込んだクライアント」
# （admin.js は保存のたびに configMtime を取り直す）として現在の mtime を使う。
_CURRENT_MTIME = object()


def _save(workdir, catalog, updates=None, resets=None, base_mtime=_CURRENT_MTIME):
    path = workdir / "config_overrides.json"
    if base_mtime is _CURRENT_MTIME:
        base_mtime = overrides.mtime_or_none(path)
    return overrides.save(
        path=path,
        updates=updates or {},
        resets=resets or [],
        base_mtime=base_mtime,
        ini_catalog=catalog,
        config_ini_path=CONFIG_INI_PATH,
        backup_dir=workdir / "backups",
        backup_keep=5,
        audit_log_path=workdir / "audit.log",
        instance_name="test",
        actor="alice",
        remote_addr="127.0.0.1",
    )


def test_save_creates_file_and_returns_overrides(workdir, catalog):
    result = _save(workdir, catalog, updates={("ui", "max_display_messages"): "77"})
    assert result == {"ui": {"max_display_messages": "77"}}
    saved = json.loads((workdir / "config_overrides.json").read_text(encoding="utf-8"))
    assert saved == {"ui": {"max_display_messages": "77"}}


def _default_list(catalog, section, key):
    import ast

    return ast.literal_eval(next(k.default_value for k in catalog.keys if (k.section, k.key) == (section, key)))


def test_additive_list_saves_only_additions(workdir, catalog):
    """スキルの登録系のリストは、画面で既定値を含めた全体を保存しても、既定値に足す分だけを上書きに持つ。"""
    edited = [*_default_list(catalog, "plan", "plan_approval_exempt_scripts"), ["my-skill", "run.py"]]
    result = _save(workdir, catalog, updates={("plan", "plan_approval_exempt_scripts"): json.dumps(edited)})
    assert json.loads(result["plan"]["plan_approval_exempt_scripts"]) == [["my-skill", "run.py"]]
    # 足した分を消して既定値と同じ全体にしたら、上書きは無くなる
    result = _save(workdir, catalog, updates={("plan", "plan_approval_exempt_scripts"): json.dumps(edited[:-1])})
    assert result == {}


@pytest.mark.parametrize("change", ["remove", "limit"])
def test_additive_list_rejects_removing_or_changing_defaults(workdir, catalog, change):
    defaults = _default_list(catalog, "main_agent_tool_guard", "allow_entries")
    edited = defaults[1:] if change == "remove" else [[defaults[0][0], 12345], *defaults[1:]]
    with pytest.raises(overrides.ValidationError, match="allow_entries"):
        _save(workdir, catalog, updates={("main_agent_tool_guard", "allow_entries"): json.dumps(edited)})


def test_config_view_shows_merged_additive_list_and_stale_shared_keys(tmp_path, monkeypatch):
    """設定画面には、足す分だけの上書きを既定値と合成した値を出し、全インスタンス共通になったキーの古い上書きは効かないものとして出す。"""
    from admin import instances as inst
    from admin import server

    root = tmp_path / "instances"
    (root / "a").mkdir(parents=True)
    (root / "a" / "config_overrides.json").write_text(
        json.dumps({"plan": {"plan_approval_exempt_scripts": '[["my-skill", "run.py"]]'}, "vector_db": {"max_file_mb": "200"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "INSTANCES_ROOT", root)
    keys = {(k["section"], k["key"]): k for k in server._config_key_view("a", inst.KIND_APP)["keys"]}
    plan = keys[("plan", "plan_approval_exempt_scripts")]
    assert json.loads(plan["effective"]) == [*_default_list(parse_file(CONFIG_INI_PATH), "plan", "plan_approval_exempt_scripts"), ["my-skill", "run.py"]]
    assert not plan["ignored"]
    stale = keys[("vector_db", "max_file_mb")]
    assert stale["ignored"] and stale["override"] == "200"
    assert ("vector_db", "scan_workers") not in keys


def test_save_same_as_default_removes_key(workdir, catalog):
    default_value = next(k.default_value for k in catalog.keys if k.section == "ui" and k.key == "max_display_messages")
    _save(workdir, catalog, updates={("ui", "max_display_messages"): "999"})
    result = _save(workdir, catalog, updates={("ui", "max_display_messages"): default_value})
    assert result == {}
    assert not (workdir / "config_overrides.json").read_text(encoding="utf-8").strip("{} \n")


def test_reset_removes_key(workdir, catalog):
    _save(workdir, catalog, updates={("ui", "max_display_messages"): "77"})
    result = _save(workdir, catalog, resets=[("ui", "max_display_messages")])
    assert result == {}


def test_unknown_key_rejected(workdir, catalog):
    with pytest.raises(overrides.ValidationError):
        _save(workdir, catalog, updates={("nope_section", "nope_key"): "x"})


def test_admin_section_rejected(workdir, catalog):
    with pytest.raises(overrides.ValidationError):
        _save(workdir, catalog, updates={("admin", "port"): "9999"})


def test_invalid_value_rejected_and_file_unchanged(workdir, catalog):
    _save(workdir, catalog, updates={("ui", "max_display_messages"): "77"})
    before = (workdir / "config_overrides.json").read_text(encoding="utf-8")
    with pytest.raises(overrides.ValidationError):
        _save(workdir, catalog, updates={("llm", "main_routing_strategy"): "not_a_strategy"})
    after = (workdir / "config_overrides.json").read_text(encoding="utf-8")
    assert before == after


def test_conflict_detection(workdir, catalog):
    _save(workdir, catalog, updates={("ui", "max_display_messages"): "77"})
    with pytest.raises(overrides.ConflictError):
        _save(workdir, catalog, updates={("ui", "max_display_messages"): "88"}, base_mtime=0.0)


def test_conflict_detection_when_base_mtime_none_but_file_exists(workdir, catalog):
    """未保存状態で画面を開いた（base_mtime=None）間に別タブが初回保存を済ませていたら競合。"""
    _save(workdir, catalog, updates={("ui", "max_display_messages"): "77"})
    with pytest.raises(overrides.ConflictError):
        _save(workdir, catalog, updates={("ui", "max_display_messages"): "88"}, base_mtime=None)


def test_value_with_percent_sign_can_be_saved(workdir, catalog):
    """configparser の % 補間を無効化しているため、% を含む値も保存・読み込みできる。"""
    result = _save(workdir, catalog, updates={("llm", "reasoning_budget_message"): "残り10%です"})
    assert result == {"llm": {"reasoning_budget_message": "残り10%です"}}


def _make_instance(instances_root: Path, name: str, overrides_data: dict | None = None) -> None:
    d = instances_root / name
    d.mkdir(parents=True)
    (d / "instance.json").write_text("{}", encoding="utf-8")
    if overrides_data is not None:
        (d / "config_overrides.json").write_text(json.dumps(overrides_data), encoding="utf-8")


def test_save_rejects_data_dir_shared_with_other_instance(tmp_path, catalog, monkeypatch):
    """[paths].common_data_dir を他インスタンスと同じにする保存は拒否し、ファイルも作らない。"""
    monkeypatch.delenv("COMMON_DATA_DIR")
    instances_root = tmp_path / "instances"
    _make_instance(instances_root, "a", {"paths": {"common_data_dir": str(tmp_path / "data" / "a")}})
    _make_instance(instances_root, "b")
    target = instances_root / "b" / "config_overrides.json"
    with pytest.raises(overrides.ValidationError, match="重複"):
        overrides.save(
            path=target,
            updates={("paths", "common_data_dir"): str(tmp_path / "data" / "a")},
            resets=[],
            base_mtime=None,
            ini_catalog=catalog,
            config_ini_path=CONFIG_INI_PATH,
            backup_dir=instances_root / "b" / "backups",
            backup_keep=5,
            audit_log_path=tmp_path / "audit.log",
            instance_name="b",
            actor="alice",
            remote_addr="127.0.0.1",
            instances_root=instances_root,
        )
    assert not target.exists()
    with pytest.raises(overrides.ValidationError, match="重複"):
        overrides.preview(
            path=target,
            updates={("paths", "common_data_dir"): str(tmp_path / "data" / "a")},
            resets=[],
            ini_catalog=catalog,
            config_ini_path=CONFIG_INI_PATH,
            instance_name="b",
            instances_root=instances_root,
        )


def test_backup_created_on_second_save(workdir, catalog):
    _save(workdir, catalog, updates={("ui", "max_display_messages"): "77"})
    backups_before = list((workdir / "backups").glob("*.json")) if (workdir / "backups").is_dir() else []
    assert backups_before == []
    _save(
        workdir,
        catalog,
        updates={("ui", "max_display_messages"): "88"},
        base_mtime=(workdir / "config_overrides.json").stat().st_mtime,
    )
    backups_after = list((workdir / "backups").glob("*.json"))
    assert len(backups_after) == 1
    backed_up = json.loads(backups_after[0].read_text(encoding="utf-8"))
    assert backed_up == {"ui": {"max_display_messages": "77"}}


def test_backup_keep_prunes_old_backups(workdir, catalog):
    for i in range(3):
        mtime = (workdir / "config_overrides.json").stat().st_mtime if (workdir / "config_overrides.json").is_file() else None
        overrides.save(
            path=workdir / "config_overrides.json",
            updates={("ui", "max_display_messages"): str(100 + i)},
            resets=[],
            base_mtime=mtime,
            ini_catalog=catalog,
            config_ini_path=CONFIG_INI_PATH,
            backup_dir=workdir / "backups",
            backup_keep=1,
            audit_log_path=workdir / "audit.log",
            instance_name="test",
            actor="alice",
            remote_addr="127.0.0.1",
        )
    backups = list((workdir / "backups").glob("*.json"))
    assert len(backups) == 1


def test_audit_log_records_masked_sensitive_values(workdir, catalog):
    _save(workdir, catalog, updates={("llm", "main_url"): '[{"base_url": "http://x", "api_key": "SECRETVALUE", "model": "m"}]'})
    log_text = (workdir / "audit.log").read_text(encoding="utf-8")
    assert "SECRETVALUE" not in log_text
    assert "config_update" in log_text


def test_audit_log_no_entry_when_no_effective_change(workdir, catalog):
    default_value = next(k.default_value for k in catalog.keys if k.section == "ui" and k.key == "max_display_messages")
    _save(workdir, catalog, updates={("ui", "max_display_messages"): default_value})
    assert not (workdir / "audit.log").exists()


def test_preview_does_not_write_file(workdir, catalog):
    new_data, changes = overrides.preview(
        path=workdir / "config_overrides.json",
        updates={("ui", "max_display_messages"): "77"},
        resets=[],
        ini_catalog=catalog,
        config_ini_path=CONFIG_INI_PATH,
    )
    assert new_data == {"ui": {"max_display_messages": "77"}}
    assert len(changes) == 1
    assert not (workdir / "config_overrides.json").exists()


def test_preview_invalid_value_raises(workdir, catalog):
    with pytest.raises(overrides.ValidationError):
        overrides.preview(
            path=workdir / "config_overrides.json",
            updates={("llm", "main_routing_strategy"): "bogus"},
            resets=[],
            ini_catalog=catalog,
            config_ini_path=CONFIG_INI_PATH,
        )


def test_restore_replaces_content(workdir, catalog):
    _save(workdir, catalog, updates={("ui", "max_display_messages"): "77"})
    backup_file = workdir / "manual_backup.json"
    backup_file.write_text(json.dumps({"ui": {"max_display_messages": "42"}}), encoding="utf-8")
    result = overrides.restore(
        path=workdir / "config_overrides.json",
        backup_file=backup_file,
        config_ini_path=CONFIG_INI_PATH,
        backup_dir=workdir / "backups",
        backup_keep=5,
        audit_log_path=workdir / "audit.log",
        instance_name="test",
        actor="alice",
        remote_addr="127.0.0.1",
    )
    assert result == {"ui": {"max_display_messages": "42"}}
    saved = json.loads((workdir / "config_overrides.json").read_text(encoding="utf-8"))
    assert saved == {"ui": {"max_display_messages": "42"}}


def test_restore_invalid_content_rejected(workdir, catalog):
    backup_file = workdir / "manual_backup.json"
    backup_file.write_text(json.dumps({"llm": {"main_routing_strategy": "bogus"}}), encoding="utf-8")
    with pytest.raises(overrides.ValidationError):
        overrides.restore(
            path=workdir / "config_overrides.json",
            backup_file=backup_file,
            config_ini_path=CONFIG_INI_PATH,
            backup_dir=workdir / "backups",
            backup_keep=5,
            audit_log_path=workdir / "audit.log",
            instance_name="test",
            actor="alice",
            remote_addr="127.0.0.1",
        )


def test_read_returns_empty_dict_when_missing(workdir):
    assert overrides.read(workdir / "nope.json") == {}


def test_mtime_or_none(workdir, catalog):
    assert overrides.mtime_or_none(workdir / "nope.json") is None
    _save(workdir, catalog, updates={("ui", "max_display_messages"): "77"})
    assert overrides.mtime_or_none(workdir / "config_overrides.json") is not None
