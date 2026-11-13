"""評価の一時フォルダ（[skill_lab].temp_dir、evals/temp_dirs.py）。

OS の一時フォルダではなくインスタンスの common_data_dir の下に作り、強制終了で残ったものは
次の評価の開始時に、最後の更新から [skill_lab].temp_retention_hours を過ぎたものだけ消す。
"""

import os
import time

from evals import temp_dirs
from evals.case_schema import parse_case


def _age(path, hours):
    old = time.time() - hours * 3600
    for p in [path, *path.rglob("*")]:
        os.utime(p, (old, old))


def test_default_temp_dir_is_under_common_data_dir(monkeypatch, tmp_path):
    from src.config import load_config

    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("SKILL_LAB_TEMP_DIR", raising=False)
    cfg = load_config()
    assert cfg.skill_lab_temp_dir == (tmp_path / "data" / "evals_temp").resolve()
    assert cfg.skill_lab_temp_retention_hours == 24


def test_cleanup_removes_only_stale_leftovers(tmp_path):
    stale = tmp_path / "case_001_old"
    (stale / "workdir").mkdir(parents=True)
    (stale / "workdir" / "out.xlsx").write_text("x", encoding="utf-8")
    _age(stale, 30)
    # 古いフォルダでも、中で最近更新されたファイルがあれば実行中とみなして残す
    running = tmp_path / "run_all_running"
    (running / "cases").mkdir(parents=True)
    _age(running, 30)
    (running / "cases" / "new.yaml").write_text("x", encoding="utf-8")

    assert temp_dirs.cleanup_stale(tmp_path, 24) == [stale]
    assert not stale.exists() and running.exists()
    _age(running, 30)
    assert temp_dirs.cleanup_stale(tmp_path, 0) == []  # 0 以下は消さない
    assert running.exists()


def test_temporary_creates_under_root_and_removes(tmp_path):
    with temp_dirs.temporary(tmp_path, "case_") as path:
        assert os.path.dirname(path) == str(tmp_path)
        (tmp_path / os.path.basename(path) / "a.txt").write_text("x", encoding="utf-8")
    assert list(tmp_path.iterdir()) == []


def test_cleanup_loop_removes_leftovers_periodically(tmp_path, monkeypatch):
    import asyncio

    stale = tmp_path / "case_001_old"
    stale.mkdir()
    _age(stale, 30)
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    async def run():
        task = asyncio.create_task(temp_dirs.cleanup_loop(tmp_path, 24, 1))
        for _ in range(50):
            await real_sleep(0.01)
            if not stale.exists():
                break
        task.cancel()

    asyncio.run(run())
    assert not stale.exists()


def test_worker_sweeps_temp_dirs_of_app_instances(tmp_path, monkeypatch):
    """スキル調整ワーカーは、本体を動かしていないインスタンスの一時フォルダのごみも片付ける。"""
    import json

    from admin import instances as inst
    from admin.skill_lab import worker as worker_mod

    root = tmp_path / "instances"
    monkeypatch.setenv("INSTANCES_DIR", str(root))
    for key in ("LOCOHANE_INSTANCE", "CONFIG_OVERRIDES_PATH", "LOCOHANE_INSTANCE_ENV", "COMMON_DATA_DIR", "SKILL_LAB_TEMP_DIR"):
        monkeypatch.delenv(key, raising=False)
    for name, kind in (("org_a", inst.KIND_APP), ("lab1", inst.KIND_SKILL_LAB)):
        (root / name).mkdir(parents=True)
        inst.write_instance(root, inst.InstanceMeta(name=name, display_name=name, app_port=18001 if name == "org_a" else 0, kind=kind))
        (root / name / "config_overrides.json").write_text(
            json.dumps({"paths": {"common_data_dir": str(tmp_path / "data" / name)}}), encoding="utf-8"
        )
    stale = tmp_path / "data" / "org_a" / "evals_temp" / "case_001_old"
    fresh = tmp_path / "data" / "org_a" / "evals_temp" / "case_002_running"
    stale.mkdir(parents=True)
    fresh.mkdir()
    _age(stale, 30)

    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.lab, w.instances_root, w.shutdown = "lab1", root, False
    w.config = type("C", (), {"skill_lab_temp_cleanup_interval_hours": 1})()
    w._maybe_sweep_temp_dirs()
    assert not stale.exists() and fresh.exists()


def test_case_can_be_validated_without_writing_a_file(tmp_path):
    case = parse_case("id: '001'\ntarget: x\nturns: [a]\njudge: y\n", tmp_path / "001.yaml")
    assert case.id == "001" and not (tmp_path / "001.yaml").exists()
