"""スキルまわりとパスメモリーの類似検索の不具合修正の回帰テスト。"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src import path_memory
from src.agent_types import scan_agent_types
from src.skill_drafts import _scan_owner_dir
from src.skills import scan_skills
from src.tools import _state
from src.tools._proc_tree import run_with_timeout
from src.tools._safe_path import _safe_path, find_script_files
from src.tools.read_skill_file import read_skill_file


def _write_skill(root: Path, name: str, description: str, body: str = "body", *, encoding: str = "utf-8", bom: bool = False) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: {description}\n---\n{body}"
    data = text.encode(encoding)
    (skill_dir / "SKILL.md").write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
    return skill_dir


# --- パスメモリー: 書き込み中の読み込み -----------------------------------------


def test_resolve_never_misses_while_another_thread_registers(tmp_path):
    """書き込み中に読んでも、登録済みの @N が「未登録」にならない（以前は 3000 回中 119 回）。"""
    path_memory.register("t", r"C:\a\first.txt", tmp_path, 10000)
    stop = threading.Event()

    def writer() -> None:
        i = 0
        while not stop.is_set():
            path_memory.register_many("t", [rf"C:\a\f{i}_{j}.txt" for j in range(20)], tmp_path, 10000)
            i += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        misses = sum(path_memory.resolve("t", "@1", tmp_path) is None for _ in range(1500))
    finally:
        stop.set()
        thread.join()
    assert misses == 0


def test_broken_registry_is_kept_aside_instead_of_silently_overwritten(tmp_path):
    (tmp_path / "t.json").write_text("[{broken", encoding="utf-8")
    assert path_memory.register("t", r"C:\a\x.txt", tmp_path, 100) == 1
    assert list(tmp_path.glob("t.json.corrupt-*"))


def test_reregistering_unchanged_path_does_not_rewrite_the_registry(tmp_path):
    path_memory.register("t", r"C:\a\x.txt", tmp_path, 100)
    registry = tmp_path / "t.json"
    before = registry.stat().st_mtime_ns
    time.sleep(0.02)
    assert path_memory.register("t", r"C:\a\x.txt", tmp_path, 100) == 1
    assert registry.stat().st_mtime_ns == before


# --- パスメモリー: 類似度 -------------------------------------------------------


def test_substring_inside_an_ascii_word_is_not_treated_as_a_partial_match():
    real = path_memory.similarity("work", r"C:\work\report.xlsx")
    inside_word = path_memory.similarity("work", r"C:\homework\notes.txt")
    assert real >= 0.8
    assert inside_word < 0.7


@pytest.mark.parametrize("query", ["a", "c", ".", "x"])
def test_single_ascii_character_does_not_match_every_path(query):
    assert path_memory.similarity(query, r"C:\work\data\zzz.txt") < 0.3


def test_japanese_substrings_still_match():
    assert path_memory.similarity("報告", r"C:\work\月次報告書.xlsx") >= 0.8
    assert path_memory.similarity("報", r"C:\work\月次報告書.xlsx") >= 0.8


@pytest.mark.parametrize("query", ["report.xlsx ", '"report.xlsx"', "`report.xlsx`"])
def test_surrounding_spaces_and_quotes_are_ignored(query):
    assert path_memory.similarity(query, r"C:\work\report.xlsx") == 1.0


# --- SKILL.md・agents/*.md の文字コード ------------------------------------------


def test_scan_skips_non_utf8_skill_instead_of_crashing(tmp_path):
    _write_skill(tmp_path, "good", "ok")
    _write_skill(tmp_path, "sjis", "日本語の説明", encoding="cp932")
    assert [s.name for s in scan_skills(tmp_path)] == ["good"]


def test_scan_reads_skill_saved_with_bom(tmp_path):
    _write_skill(tmp_path, "bom", "ok", bom=True)
    assert [s.name for s in scan_skills(tmp_path)] == ["bom"]


def test_draft_scan_skips_non_utf8_draft(tmp_path):
    _write_skill(tmp_path, "good", "ok")
    _write_skill(tmp_path, "sjis", "日本語の説明", encoding="cp932")
    assert [s.name for s in _scan_owner_dir(tmp_path)] == ["good"]


def test_agent_scan_skips_non_utf8_definition(tmp_path):
    (tmp_path / "good.md").write_text("---\nname: good\ndescription: ok\n---\nprompt", encoding="utf-8")
    (tmp_path / "sjis.md").write_bytes("---\nname: sjis\ndescription: 日本語\n---\n".encode("cp932"))
    assert [a.name for a in scan_agent_types(tmp_path)] == ["good"]


# --- run_script のスクリプト名 ---------------------------------------------------


def test_script_filename_is_not_interpreted_as_a_glob(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "_internal.py").write_text("", encoding="utf-8")
    (scripts / "edit.py").write_text("", encoding="utf-8")
    assert find_script_files(scripts, "*.py") == []
    assert find_script_files(scripts, "[a-z]*.py") == []
    assert [p.name for p in find_script_files(scripts, "EDIT.PY")] == ["edit.py"]


# --- 上書きスキルの実体解決 -------------------------------------------------------


@pytest.fixture
def skill_roots(tmp_path, monkeypatch):
    official, override, drafts = tmp_path / "skills", tmp_path / "override", tmp_path / "drafts"
    _write_skill(official, "foo", "official", "OFFICIAL")
    (official / "foo" / "references").mkdir()
    (official / "foo" / "references" / "a.md").write_text("official ref", encoding="utf-8")
    # 上書き版の foo は name が仕様違反で、Discovery ではスキップされる。
    (override / "foo").mkdir(parents=True)
    (override / "foo" / "SKILL.md").write_text("---\nname: Foo\ndescription: x\n---\nOVERRIDE", encoding="utf-8")
    _write_skill(official, "bar", "official", "OFFICIAL")
    _write_skill(override, "bar", "override", "OVERRIDE")
    # スキル名と同じ名前のユーザーのドラフト。
    _write_skill(drafts / "foo", "mine", "draft", "DRAFT")
    skills = scan_skills([official, override])
    monkeypatch.setattr(_state, "_SKILLS_ROOTS", [override.resolve(), official.resolve(), drafts.resolve()])
    monkeypatch.setattr(_state, "_SKILL_DRAFT_ROOT", drafts.resolve())
    monkeypatch.setattr(_state, "_SKILL_DIRS", {s.name: s.dir_path.resolve() for s in skills})
    monkeypatch.setattr(_state, "_LLM_CONFIG", None)
    return official.resolve(), override.resolve()


def test_invalid_override_is_not_read_when_discovery_used_the_official_skill(skill_roots):
    assert _safe_path("foo/SKILL.md").read_text(encoding="utf-8").endswith("OFFICIAL")


def test_valid_override_does_not_fall_back_to_official_files(skill_roots):
    official, override = skill_roots
    assert _safe_path("bar/SKILL.md").read_text(encoding="utf-8").endswith("OVERRIDE")
    missing = _safe_path("bar/references/a.md")
    assert not missing.exists()
    assert missing.is_relative_to(override)


def test_draft_of_user_named_like_a_skill_is_still_reachable(skill_roots):
    assert _safe_path("foo/mine/SKILL.md").read_text(encoding="utf-8").endswith("DRAFT")


def test_traversal_is_still_rejected(skill_roots):
    with pytest.raises(ValueError):
        _safe_path("foo/../../outside.txt")


# --- read_skill_file の秘密ファイル ------------------------------------------------


def test_read_skill_file_refuses_env_files(tmp_path, monkeypatch):
    scripts = tmp_path / "web" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / ".env").write_text("API_KEY=dummy-key-1234", encoding="utf-8")
    monkeypatch.setattr(_state, "_SKILLS_ROOTS", [tmp_path.resolve()])
    monkeypatch.setattr(_state, "_SKILL_DIRS", {})
    monkeypatch.setattr(_state, "_LLM_CONFIG", None)
    result = read_skill_file.invoke({"relative_path": "web/scripts/.env"})
    assert result.startswith("Error:")
    assert "dummy-key-1234" not in result


# --- タイムアウト時の孫プロセス -----------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill による停止は Windows 向け")
def test_timeout_returns_even_if_a_grandchild_holds_the_pipes(tmp_path):
    script = tmp_path / "spawn.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_with_timeout([sys.executable, str(script)], cwd=str(tmp_path), env=None, timeout=2)
    assert time.monotonic() - started < 30
