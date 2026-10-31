"""admin/supervisor.py のテスト（実際の子プロセスは起動せず Popen をモックする）。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from admin import instances as inst
from admin import supervisor as sv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_INI_PATH = PROJECT_ROOT / "config.ini"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """実プロジェクトの data/ にテスト用ディレクトリを作らないよう、データ保存先を tmp へ逃がす。"""
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data" / "${instance}"))
    monkeypatch.delenv("LOCOHANE_INSTANCE", raising=False)


class _FakeProc:
    def __init__(self, calls: list, *args, **kwargs):
        calls.append(kwargs)
        self.pid = len(calls)

    def poll(self):
        return None


@pytest.fixture
def popen_calls(monkeypatch):
    calls: list = []
    monkeypatch.setattr(sv.subprocess, "Popen", lambda *a, **k: _FakeProc(calls, *a, **k))
    return calls


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "instances"
    inst.write_instance(r, inst.InstanceMeta(name="a", display_name="a", app_port=18001))
    return r


def test_concurrent_start_launches_only_once(root, popen_calls, monkeypatch):
    """起動ボタンの連打等で start() が並行に呼ばれても Popen は1回だけ。"""
    real_check = inst.check_conflicts

    def slow_check(*args, **kwargs):
        time.sleep(0.2)
        return real_check(*args, **kwargs)

    monkeypatch.setattr(sv.inst, "check_conflicts", slow_check)
    s = sv.Supervisor(root, CONFIG_INI_PATH)
    errors: list[Exception] = []

    def run():
        try:
            s.start("a")
        except sv.SupervisorError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(popen_calls) == 1
    assert len(errors) == 1
    assert s.status("a").state == sv.InstanceState.RUNNING


def test_start_converts_instance_error_to_supervisor_error(root, tmp_path, popen_calls, monkeypatch):
    """データ保存先の重複（InstanceError）は SupervisorError として送出される。"""
    monkeypatch.delenv("COMMON_DATA_DIR")
    shared = str(tmp_path / "data" / "shared")
    inst.overrides_path(root, "a").write_text(json.dumps({"paths": {"common_data_dir": shared}}), encoding="utf-8")
    inst.write_instance(root, inst.InstanceMeta(name="b", display_name="b", app_port=18002))
    inst.overrides_path(root, "b").write_text(json.dumps({"paths": {"common_data_dir": shared}}), encoding="utf-8")
    s = sv.Supervisor(root, CONFIG_INI_PATH)
    with pytest.raises(sv.SupervisorError, match="重複"):
        s.start("b")
    assert popen_calls == []


def test_status_reports_error_when_overrides_broken(root, popen_calls):
    """config_overrides.json が壊れていても status() は例外を投げず ERROR を返す。"""
    inst.overrides_path(root, "a").write_text("{broken", encoding="utf-8")
    s = sv.Supervisor(root, CONFIG_INI_PATH)
    assert s.status("a").state == sv.InstanceState.ERROR
    with pytest.raises(sv.SupervisorError):
        s.start("a")
    assert popen_calls == []


def test_start_sets_utf8_mode_for_child(root, popen_calls):
    """子プロセスの stdout がファイルでも cp932 にならないよう PYTHONUTF8=1 を渡す。"""
    s = sv.Supervisor(root, CONFIG_INI_PATH)
    s.start("a")
    env = popen_calls[0]["env"]
    assert env["PYTHONUTF8"] == "1"
    assert env["LOCOHANE_INSTANCE"] == "a"
