"""SQLite を置くパスをローカルディスクに限るガード（src/config.py の is_network_path・_validate_local_data_paths）のテスト。"""

from __future__ import annotations

import json
import sys
import types

import pytest

from src import config as config_module
from src.config import is_network_path, load_config, resolve_instances_root


@pytest.mark.parametrize(
    "path",
    [r"\\fileserver\share\data", "//fileserver/share/data", r"\\?\UNC\fileserver\share\data", r"\\FILESERVER\Share"],
)
def test_unc_paths_are_network_paths(path):
    assert is_network_path(path)


@pytest.mark.parametrize("path", [r"C:\Locohane\data", r"\\?\C:\Locohane\data", "./data/default", "relative"])
def test_local_paths_are_not_network_paths(path, monkeypatch):
    fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=types.SimpleNamespace(GetDriveTypeW=lambda root: 3)))
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    assert not is_network_path(path)


def test_mapped_network_drive_is_a_network_path(monkeypatch):
    calls = []

    def get_drive_type(root):
        calls.append(root)
        return 4 if root.upper() == "Z:\\" else 3

    fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=types.SimpleNamespace(GetDriveTypeW=get_drive_type)))
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    monkeypatch.setattr(config_module.sys, "platform", "win32")
    assert is_network_path(r"Z:\locohane\data")
    assert not is_network_path(r"C:\locohane\data")
    assert calls == ["Z:\\", "C:\\"]


def _overrides(tmp_path, data: dict):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("section", "key", "label"),
    [
        ("paths", "common_data_dir", "common_data_dir"),
        ("paths", "checkpoint_db", "checkpoint_db"),
        ("thread_store", "db", "[thread_store].db"),
        ("vector_db", "dir", "[vector_db].dir"),
    ],
)
def test_load_config_rejects_sqlite_paths_on_a_share(tmp_path, section, key, label):
    overrides = _overrides(tmp_path, {section: {key: r"\\fileserver\share\locohane\x"}})
    with pytest.raises(ValueError, match="local disk") as info:
        load_config(overrides_path=overrides, instance_name="inst1")
    assert label in str(info.value)


def test_load_config_rejects_network_common_data_dir_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMON_DATA_DIR", "//fileserver/share/${instance}")
    with pytest.raises(ValueError, match="common_data_dir"):
        load_config(overrides_path=tmp_path / "missing.json", instance_name="inst1")


def test_local_data_paths_are_accepted(tmp_path):
    cfg = load_config(overrides_path=_overrides(tmp_path, {"paths": {"common_data_dir": str(tmp_path / "data")}}), instance_name="inst1")
    assert cfg.common_data_dir == tmp_path / "data"


def test_instances_dir_on_a_share_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCES_DIR", r"\\fileserver\share\instances")
    with pytest.raises(ValueError, match=r"\[admin\]\.instances_dir"):
        resolve_instances_root()
    monkeypatch.setenv("INSTANCES_DIR", str(tmp_path / "instances"))
    assert resolve_instances_root() == tmp_path / "instances"


def test_dashboard_save_rejects_network_common_data_dir(tmp_path):
    from admin import overrides
    from admin.ini_catalog import parse_file
    from src.config import DEFAULT_CONFIG_PATH

    with pytest.raises(overrides.ValidationError, match="local disk"):
        overrides.preview(
            path=tmp_path / "config_overrides.json",
            updates={("paths", "common_data_dir"): r"\\fileserver\share\data"},
            resets=[],
            ini_catalog=parse_file(DEFAULT_CONFIG_PATH),
            config_ini_path=DEFAULT_CONFIG_PATH,
            instance_name="inst1",
        )


def test_dashboard_save_rejects_data_backup_section(tmp_path):
    from admin import overrides
    from admin.ini_catalog import parse_file
    from src.config import DEFAULT_CONFIG_PATH

    with pytest.raises(overrides.ValidationError, match="data_backup"):
        overrides._build_new_data({}, {("data_backup", "keep"): "3"}, [], parse_file(DEFAULT_CONFIG_PATH))
