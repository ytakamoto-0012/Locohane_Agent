"""スキル研究室のケースの判定と点検（2026-11-13 ユーザー指摘、evals/tuning_log.md の教訓）。

- 使ってはいけない場面の試験（tool_call_args_not_contains。委譲先の中の呼び出しも数える）
- 作ったファイルを確かめるスクリプト（verify_script。自己申告を信じない）
- 存在しない名前を確かめるケース（綴り違い・古い名前）の点検
- 評価環境の注記（打ち切り・ガードの拒否・ライブラリ不足）を AI に渡す
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from admin.skill_lab import evaluation, judge, themes
from evals import run_case
from evals.case_schema import Expect, load_case, parse_case


def test_must_not_use_check_counts_calls_inside_subagents():
    expect = Expect(tool_call_args_not_contains={"read_skill": {"skill_name": "my-skill"}})
    main = [{"type": "AIMessage", "tool_calls": [{"name": "dispatch_agent", "args": {"agent_type": "worker"}}]}]
    ok = run_case._evaluate_expect(expect, main, "", [{"name": "read_skill", "args": {"skill_name": "other"}}])
    assert ok["tool_call_args_not_contains:read_skill"]["pass"] is True
    ng = run_case._evaluate_expect(expect, main, "", [{"name": "read_skill", "args": {"skill_name": "my-skill"}}])
    assert ng["tool_call_args_not_contains:read_skill"]["pass"] is False
    # 機械判定のまま（AI の見直しで合格にしない）
    assert judge._is_hard("tool_call_args_not_contains:read_skill") and judge._is_hard("verify_script")


def test_verify_script_checks_the_working_folder(tmp_path, monkeypatch):
    cases = tmp_path / "cases"
    (cases / "fixtures" / "s").mkdir(parents=True)
    (cases / "fixtures" / "s" / "check.py").write_text(
        "import json, sys, pathlib\n"
        "info = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8'))\n"
        "ok = (pathlib.Path(sys.argv[1]) / 'out.txt').read_text(encoding='utf-8') == '42' and '42' in info['final_answer']\n"
        "print('checked' if ok else 'out.txt is wrong')\n"
        "sys.exit(0 if ok else 1)\n",
        encoding="utf-8",
    )
    (cases / "001.yaml").write_text("id: '001'\ntarget: x\nturns: [x]\nverify_script: fixtures/s/check.py\n", encoding="utf-8")
    case = load_case(cases / "001.yaml")
    work = tmp_path / "work"
    work.mkdir()
    import src.tools._workdir as workdir_mod

    monkeypatch.setattr(workdir_mod, "_resolve_exec_workdir", lambda thread_id=None: work)
    config = types.SimpleNamespace(script_python=sys.executable, skill_lab_verify_timeout_seconds=60)
    (work / "out.txt").write_text("42", encoding="utf-8")
    passed = run_case._run_verify_script(case, config, "t", tmp_path, {"final_answer": "答えは42です"})
    assert passed["pass"] is True and "checked" in passed["detail"]
    (work / "out.txt").write_text("41", encoding="utf-8")
    failed = run_case._run_verify_script(case, config, "t", tmp_path, {"final_answer": "答えは42です"})
    assert failed["pass"] is False and "out.txt is wrong" in failed["detail"]


def test_verify_script_alone_is_a_way_to_pass_and_must_exist(tmp_path):
    case = parse_case("id: '001'\ntarget: x\nturns: [x]\nverify_script: fixtures/s/check.py\n", tmp_path / "001.yaml")
    assert themes.case_has_condition(case)


def _theme_with_cases(tmp_path, cases: dict[str, str]) -> Path:
    directory = tmp_path / "theme"
    (directory / "cases").mkdir(parents=True)
    for cid, body in cases.items():
        (directory / "cases" / f"{cid}.yaml").write_text(f"id: '{cid}'\ntarget: skill_lab\nturns: [x]\n{body}", encoding="utf-8")
    return directory


def test_cases_that_check_names_that_do_not_exist_are_reported(tmp_path):
    directory = _theme_with_cases(
        tmp_path,
        {
            "001": "expect:\n  tool_call_args_contains:\n    read_skill: {skill_name: hello_world}\n",
            "002": "expect:\n  tool_call_args_contains:\n    read_skill: {skill_name: hellow_world}\n  tool_called_any: [Glob, NoSuchTool, mcp__x__y]\n",
            "003": "expect:\n  tool_call_args_not_contains:\n    dispatch_agent: {agent_type: old-agent}\n",
        },
    )
    problems = themes.case_reference_problems(directory, skills={"hellow_world"}, agents={"worker"}, tools={"Glob", "read_skill"})
    assert set(problems) == {"001", "002", "003"}
    assert "hello_world" in problems["001"][0]
    assert problems["002"] == [problems["002"][0]] and "NoSuchTool" in problems["002"][0] and "mcp__" not in problems["002"][0]
    assert "old-agent" in problems["003"][0]
    # ツール名の一覧がまだ無いときは、ツール名は確かめない
    assert "002" not in themes.case_reference_problems(directory, skills={"hellow_world"}, agents={"worker"}, tools=None)


def test_required_and_forbidden_same_call_is_impossible(tmp_path):
    case = parse_case(
        "id: '001'\ntarget: x\nturns: [x]\nexpect:\n"
        "  tool_call_args_contains:\n    read_skill: {skill_name: s}\n"
        "  tool_call_args_not_contains:\n    read_skill: {skill_name: s}\n",
        tmp_path / "001.yaml",
    )
    assert any("read_skill" in p for p in themes.impossible_conditions(case))


def test_harness_notes_explain_cutoffs_refusals_and_missing_libraries():
    result = {
        "turn_cutoffs": [{"turn_index": 0, "reason": "thinking_loop", "snippet": "同じ考えを繰り返す"}],
        "turn_timings": [{"turn_index": 0, "llm_calls": [{"total_seconds": 30}], "tool_calls": [{"total_seconds": 5}]}],
        "transcript": [
            {"type": "ToolMessage", "content": "Error: Read is not allowed for the main agent (not registered in main_agent_tool_guard.allow_entries)."},
        ],
        "subagent_tool_calls": [{"name": "run_script", "args": {}, "is_error": True, "result": "ModuleNotFoundError: No module named 'pandas'"}],
    }
    notes = evaluation.harness_notes(result)
    text = "\n".join(notes)
    assert "thinking_loop" in text and "about 35 s" in text
    assert "[main_agent_tool_guard]" in text and "pandas" in text
    assert "[harness note]" in evaluation.transcript_excerpt(result, 300)  # 切り詰めても残る


def test_each_case_run_gets_its_own_memory_plans_and_drafts(tmp_path, monkeypatch):
    """評価の1回ごとに、メモリー・パスメモリー・計画の詳細・ドラフトスキルの置き場を使い捨てにする。

    1回目に書いた答え（メモリー・skill-creator で作ったドラフト）を2回目以降が読む「カンニング」と、
    本番のデータフォルダへの書き込みを防ぐ（2026-11-13 ユーザー指摘）。
    """
    import os

    from src.config import load_config

    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "real_data"))
    real = load_config()
    case_tmp = tmp_path / "case_001_x"
    run_case._isolate_persistent_dirs(case_tmp, memory_dir=case_tmp / "memory", path_memory_dir=case_tmp / "path_memory")
    cfg = load_config()
    for attr in ("memory_dir", "path_memory_dir", "plans_dir", "skill_draft_dir", "skill_archive_dir"):
        value = Path(getattr(cfg, attr)).resolve()
        assert value.is_relative_to(case_tmp.resolve()), attr
        assert not value.is_relative_to(Path(getattr(real, attr)).resolve()), attr
