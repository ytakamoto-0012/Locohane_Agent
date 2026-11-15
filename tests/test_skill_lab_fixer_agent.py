"""スキル研究室の AI 修正の Agent 版（admin/skill_lab/fixer_agent.py）と、評価中の質問への自動回答。

- 評価中にアシスタントが選択肢で質問したら、ケースの scripted_choice_answers に合う選択肢を選ぶ（合わなければ「その他」で答える）。
- AI 修正はケースのうち自動回答（scripted_*_answers）だけを書き換えられる（指示・採点の条件は変えられない）。
- Agent は記録を調べ、修正案を研究対象の写しで試し（研究対象は書き換えない）、結論を submit_fix / report_blockers で返す。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from admin.skill_lab import fixer, fixer_agent, themes


def _skill_md(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: テスト用のスキル\n---\n\n{body}"


@pytest.fixture
def theme(tmp_path):
    directory = themes.create_theme(tmp_path / "themes", instance="org_a", title="t", lab="lab1", actor="admin")
    themes.add_asset_new(directory, "skills", "my-skill", _skill_md("my-skill", "v0\n"), actor="admin")
    themes.write_text(directory, "cases/a.yaml", "id: a\ntarget: skill_lab\nturns: [やって]\njudge: y\n", actor="admin")
    return directory


# --- 評価中の質問への自動回答 -------------------------------------------------------------


class _Action:
    def __init__(self, name, label, value):
        self.name, self.label, self.payload = name, label, {"value": value}


def test_scripted_choice_answers_pick_the_matching_choice_or_other():
    from evals import headless_chainlit as hc

    hc._state.update({"auto_approve": True, "scripted_text_answers": [], "scripted_choice_answers": ["2024年度", "来年の分"]})
    actions = [_Action("choice_0", "2023年度", "2023年度"), _Action("choice_1", "2024年度", "2024年度"),
               _Action("other", "その他", "__other__"), _Action("cancel", "キャンセル", "__cancel__")]
    first = asyncio.run(hc._FakeAskActionMessage(actions=actions).send())
    assert first["label"] == "2024年度"
    # どの選択肢にも合わない答えは「その他」を選び、続く自由記述の質問にその答えを返す
    second = asyncio.run(hc._FakeAskActionMessage(actions=actions).send())
    assert second["label"] == "その他"
    assert asyncio.run(hc._FakeAskUserMessage().send()) == {"output": "来年の分"}
    # 尽きたら以前どおり先頭
    third = asyncio.run(hc._FakeAskActionMessage(actions=actions).send())
    assert third["label"] == "2023年度"
    # 承認ダイアログは自動回答に関係なく承認
    hc._state["scripted_choice_answers"] = ["no"]
    approve = asyncio.run(hc._FakeAskActionMessage(actions=[_Action("a", "承認", "approve"), _Action("d", "拒否", "deny")]).send())
    assert approve["payload"]["value"] == "approve"


def test_case_schema_reads_scripted_choice_answers(tmp_path):
    from evals.case_schema import load_case

    path = tmp_path / "x.yaml"
    path.write_text("id: x\ntarget: t\nturns: [a]\njudge: j\nscripted_choice_answers: [はい]\n", encoding="utf-8")
    assert load_case(path).scripted_choice_answers == ["はい"]


# --- AI 修正はケースの自動回答だけを変えられる ---------------------------------------------------


def test_fix_can_set_only_the_automatic_answers_of_a_case(theme):
    applied, errors = fixer.apply_edits(
        theme, [{"path": "cases/a.yaml", "answers": {"scripted_choice_answers": ["2024年度"], "scripted_text_answers": ["田中"]}}], known_tools=None
    )
    assert errors == [] and applied == ["cases/a.yaml"]
    data = yaml.safe_load((theme / "cases/a.yaml").read_text(encoding="utf-8"))
    assert data["scripted_choice_answers"] == ["2024年度"] and data["scripted_text_answers"] == ["田中"]
    assert data["turns"] == ["やって"] and data["judge"] == "y"
    # 指示・採点の条件は変えられない
    _, errors = fixer.apply_edits(theme, [{"path": "cases/a.yaml", "old": "やって", "new": "答えはX"}], known_tools=None)
    assert errors and "automatic answers" in errors[0]
    _, errors = fixer.apply_edits(theme, [{"path": "cases/a.yaml", "answers": {"turns": ["x"]}}], known_tools=None)
    assert errors
    # 自動回答の設定は研究対象の直しとして数えない（1回で変えてよいファイルの数）
    edits = [{"path": "cases/a.yaml", "answers": {"scripted_text_answers": []}}, {"path": "assets/skills/my-skill/SKILL.md", "content": "x"}]
    assert fixer.too_many_files(edits, 1) is None


def test_snapshot_and_restore_include_cases(theme, tmp_path):
    themes.snapshot_targets(theme, tmp_path / "snap")
    fixer.apply_edits(theme, [{"path": "cases/a.yaml", "answers": {"scripted_text_answers": ["x"]}}], known_tools=None)
    themes.restore_targets(theme, tmp_path / "snap", actor="t", message="m")
    assert "scripted_text_answers" not in (theme / "cases/a.yaml").read_text(encoding="utf-8")


# --- Agent ------------------------------------------------------------------------------------


class _FakeModel:
    """台本どおりにツールを呼ぶモデル（bind_tools は自分を返す）。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def bind_tools(self, tools, **kwargs):
        self.tool_names = [t.name for t in tools]
        return self

    async def ainvoke(self, messages):
        from langchain_core.messages import AIMessage

        self.seen.append(messages)
        name, args = self.script.pop(0)
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{len(self.seen)}"}])


def _results(case_id: str, ok: list[bool], text: str) -> tuple[list[dict], list[dict]]:
    results = [
        {"case_id": case_id, "repeat_index": i + 1, "final_answer": text,
         "transcript": [{"type": "HumanMessage", "content": "やって"},
                        {"type": "AIMessage", "content": "", "tool_calls": [{"name": "read_skill", "args": {"skill_name": "my-skill"}}]}],
         "system_prompt": "SYSTEM PROMPT with skills list"}
        for i in range(len(ok))
    ]
    verdicts = [{"case_id": case_id, "repeat_index": i + 1, "outcome": "pass" if o else "fail",
                 "ai_judge": {"pass": o, "reason": "ok" if o else "違う答え"}} for i, o in enumerate(ok)]
    return results, verdicts


def test_agent_investigates_tries_and_submits_without_touching_the_targets(theme, tmp_path, monkeypatch):
    run_dir = theme / "runs" / "001" / "x"
    run_dir.mkdir(parents=True)
    results, verdicts = _results("a", [False, True], "v0")
    (run_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    iteration = {"n": 1, "phase": "eval", "repeat": 2, "verdict": "fail", "cases": {"a": {"pass": 1, "fail": 1}},
                 "out_dir": str(run_dir), "verdicts": verdicts}
    edit = {"path": "assets/skills/my-skill/SKILL.md", "old": "v0", "new": "v1"}
    model = _FakeModel([
        ("run_overview", {"source": "1"}),
        ("read_run", {"ref": "1/a/1"}),
        ("read_assistant_prompt", {}),
        ("search_runs", {"text": "my-skill"}),
        ("try_fix", {"edits": [edit]}),
        ("submit_fix", {"analysis": "原因と根拠", "edits": [edit]}),
    ])

    async def fake_build_model(_config, **_kw):
        return model

    import src.llm

    monkeypatch.setattr(src.llm, "build_model", fake_build_model)
    evaluated = []

    def evaluate(*, case_ids, repeat, results_root, assets_base=None, config_patch_file=None, cases_dir=None):
        # 試しは研究対象の写しで行う（写しには修正案が当たっている）
        evaluated.append((case_ids, repeat, (assets_base / "skills/my-skill/SKILL.md").read_text(encoding="utf-8")))
        return _results("a", [True] * repeat, "v1")

    proposal = fixer_agent.propose(
        None,
        theme,
        system_knowledge="knowledge",
        task="task",
        work_dir=theme / "runs" / "001" / "agent",
        target_cfg=None,
        iterations=[iteration],
        runner=fixer_agent.Runner(evaluate=evaluate),
        limits=fixer_agent.AgentLimits(max_steps=10, max_trials=2, trial_repeats=3),
        known_tools=None,
        temp_root=None,
        max_files=2,
    )
    assert proposal["edits"] == [edit] and proposal["analysis"] == "原因と根拠" and proposal["blockers"] == []
    assert evaluated and evaluated[0][0] == ["a"] and evaluated[0][1] == 3 and "v1" in evaluated[0][2]
    assert proposal["trials"][0]["summary"] == "case a: passed 3/3"
    # 研究対象はそのまま（試しは写しで行った）
    assert "v0" in (theme / "assets/skills/my-skill/SKILL.md").read_text(encoding="utf-8")
    # 調べた結果が AI に渡っている（全体の流れ・全文・システムプロンプト・出現回数）
    tool_results = [str(m.content) for m in model.seen[-1] if type(m).__name__ == "ToolMessage"]
    assert "1/a/1: fail" in tool_results[0] and "read_skill" in tool_results[0]
    assert "違う答え" in tool_results[1]
    assert "SYSTEM PROMPT" in tool_results[2]
    assert "appears in 2 of 2 runs" in tool_results[3]
    assert "passed 3/3" in tool_results[4]


def test_agent_submit_rejects_edits_that_cannot_be_applied_and_lets_it_retry(theme, monkeypatch):
    model = _FakeModel([
        ("submit_fix", {"analysis": "a", "edits": [{"path": "assets/skills/my-skill/SKILL.md", "old": "存在しない", "new": "x"}]}),
        ("report_blockers", {"analysis": "b", "blockers": [{"kind": "case", "case_id": "zzz", "problem": "p"}]}),
        ("report_blockers", {"analysis": "b", "blockers": [{"kind": "case", "case_id": "a", "problem": "指示があいまい", "suggestion": "名前を書く"}]}),
    ])

    async def fake_build_model(_config, **_kw):
        return model

    import src.llm

    monkeypatch.setattr(src.llm, "build_model", fake_build_model)
    proposal = fixer_agent.propose(
        None, theme, system_knowledge="k", task="t", work_dir=theme / "runs" / "agent", target_cfg=None, iterations=[],
        runner=fixer_agent.Runner(evaluate=lambda **kw: ([], [])), limits=fixer_agent.AgentLimits(max_steps=5),
        known_tools=None, temp_root=None, max_files=2,
    )
    assert proposal["edits"] == [] and proposal["blockers"][0]["case_id"] == "a"
    tool_results = [str(m.content) for m in model.seen[-1] if type(m).__name__ == "ToolMessage"]
    assert tool_results[0].startswith("error: the edits cannot be applied")
    assert tool_results[1].startswith("error: no valid blocker")


def test_agent_finishes_when_the_step_budget_is_used_up(theme, monkeypatch):
    model = _FakeModel([("list_theme_files", {})] * 3 + [("report_blockers", {"analysis": "b", "blockers": [{"kind": "spec", "problem": "矛盾"}]})])

    async def fake_build_model(_config, **_kw):
        return model

    import src.llm

    monkeypatch.setattr(src.llm, "build_model", fake_build_model)
    proposal = fixer_agent.propose(
        None, theme, system_knowledge="k", task="t", work_dir=theme / "runs" / "agent", target_cfg=None, iterations=[],
        runner=fixer_agent.Runner(evaluate=lambda **kw: ([], [])), limits=fixer_agent.AgentLimits(max_steps=3),
        known_tools=None, temp_root=None, max_files=2,
    )
    assert proposal["blockers"][0]["kind"] == "spec"
    assert any("step budget is used up" in str(m.content) for m in model.seen[-1])


def test_agent_stops_when_the_worker_says_so(theme, monkeypatch):
    from admin.skill_lab import evaluation

    model = _FakeModel([("list_theme_files", {})] * 5)

    async def fake_build_model(_config, **_kw):
        return model

    import src.llm

    monkeypatch.setattr(src.llm, "build_model", fake_build_model)
    with pytest.raises(evaluation.EvaluationStopped):
        fixer_agent.propose(
            None, theme, system_knowledge="k", task="t", work_dir=theme / "runs" / "agent", target_cfg=None, iterations=[],
            runner=fixer_agent.Runner(evaluate=lambda **kw: ([], []), should_stop=lambda: True), limits=fixer_agent.AgentLimits(),
            known_tools=None, temp_root=None, max_files=2,
        )


def test_case_variant_trial_changes_only_the_copy(theme, monkeypatch):
    seen = {}

    def evaluate(*, case_ids, repeat, results_root, assets_base=None, config_patch_file=None, cases_dir=None):
        seen["turns"] = yaml.safe_load((cases_dir / "a.yaml").read_text(encoding="utf-8"))["turns"]
        return _results("a", [True] * repeat, "ok")

    state = fixer_agent._State(
        directory=theme, work_dir=theme / "runs" / "agent", target_cfg=None, iterations=[], limits=fixer_agent.AgentLimits(),
        runner=fixer_agent.Runner(evaluate=evaluate), known_tools=None, temp_root=None, max_files=2,
    )
    out = fixer_agent._try_case_variant(state, "a", "my-skill を使って")
    assert "passed 3/3" in out and seen["turns"] == ["my-skill を使って"]
    assert yaml.safe_load((theme / "cases/a.yaml").read_text(encoding="utf-8"))["turns"] == ["やって"]


# --- 一番よかった内容の選び方 ---------------------------------------------------------------


def test_best_state_discounts_few_runs_and_merges_the_same_state():
    """評価1回で1回合格した版より、トライアウト6回中5回合格した版を「よい」とする（2026-11-15 仮想研究室で発覚）。"""
    from admin.skill_lab.worker import _state_confidence

    iterations = [
        {"state_sha": "v0", "cases": {"a": {"pass": 1}}},
        {"state_sha": "v0", "cases": {"a": {"pass": 3, "fail": 2}}},
        {"state_sha": "v1", "cases": {"a": {"pass": 1}}},
        {"state_sha": "v1", "cases": {"a": {"pass": 5, "fail": 1}}},
    ]
    assert _state_confidence(iterations[:1], "v0") < _state_confidence(iterations[3:], "v1")
    # 同じ内容の評価は合算する（v0 は 6回中4回）
    assert _state_confidence(iterations, "v0") < _state_confidence(iterations, "v1")


def test_lab_llm_call_retries_after_a_repetition_loop(monkeypatch):
    """研究室の LLM 呼び出しが反復ループで打ち切られたら、作り直してやり直す（以前はテーマごとエラーで止まっていた）。"""
    from langchain_core.messages import AIMessage, HumanMessage

    import src.llm.routing
    from admin.skill_lab import llm as lab_llm
    from src.llm import ThinkingLoopDetected

    closed = []

    async def fake_close(model):
        closed.append(model)

    monkeypatch.setattr(src.llm.routing, "aclose_model_client", fake_close)
    calls = []

    class Model:
        async def ainvoke(self, messages):
            calls.append(messages)
            if len(calls) == 1:
                raise ThinkingLoopDetected("loop", snippet="x")
            return AIMessage(content="ok")

    async def build():
        return Model()

    response = asyncio.run(lab_llm.invoke_resilient(build, [HumanMessage(content="q")]))
    assert response.content == "ok" and len(closed) == 1
    assert "repetition loop" in str(calls[1][-1].content)


def test_subagent_llm_connection_failures_are_an_environment_problem():
    """全部の回でサブエージェントの LLM 呼び出しが接続エラーで止まったら、AI 修正に回さず環境の問題として止める。"""
    from admin.skill_lab import evaluation

    failed = {"case_id": "a", "rules_pass": False, "transcript": [
        {"type": "ToolMessage", "content": "[Sub-agent: stopped because the LLM call timed out(Connection error.)]"}]}
    ok = {"case_id": "b", "rules_pass": True, "transcript": [{"type": "ToolMessage", "content": "fine"}]}
    assert "inference server" in evaluation.infra_error([failed, dict(failed, case_id="c")])
    assert evaluation.infra_error([failed, ok]) is None
    assert any("environment problem" in n for n in evaluation.harness_notes(failed))


def test_fix_can_raise_the_time_limit_of_a_case_within_bounds(theme):
    applied, errors = fixer.apply_edits(theme, [{"path": "cases/a.yaml", "answers": {"timeout_seconds": 1800}}], known_tools=None)
    assert errors == [] and yaml.safe_load((theme / "cases/a.yaml").read_text(encoding="utf-8"))["timeout_seconds"] == 1800
    for bad in (30, 99999, "600", True):
        _, errors = fixer.apply_edits(theme, [{"path": "cases/a.yaml", "answers": {"timeout_seconds": bad}}], known_tools=None)
        assert errors, bad


def test_run_case_keeps_the_record_when_the_time_limit_is_hit():
    """会話が制限時間を過ぎたら、そこまでの記録を付けて error=timeout を返す（evals/run_case.py の time_limit）。"""
    import inspect

    from evals import run_all, run_case

    assert "time_limit" in inspect.signature(run_case._run).parameters
    assert run_all.CASE_TIMEOUT_GRACE_SECONDS > 0


def test_agent_blockers_can_carry_allowed_case_settings(theme):
    state = fixer_agent._State(
        directory=theme, work_dir=theme / "runs" / "agent", target_cfg=None, iterations=[], limits=fixer_agent.AgentLimits(),
        runner=fixer_agent.Runner(evaluate=lambda **kw: ([], [])), known_tools=None, temp_root=None, max_files=2,
    )
    blocker = [{"kind": "case", "case_id": "a", "problem": "p", "suggestion": "s"}]
    out = fixer_agent._report_blockers(state, "a", blocker, [{"path": "assets/skills/my-skill/SKILL.md", "content": "x"}])
    assert out.startswith("error") and state.final is None
    out = fixer_agent._report_blockers(state, "a", blocker, [{"path": "cases/a.yaml", "answers": {"timeout_seconds": 5}}])
    assert out.startswith("error: the case settings cannot be applied")
    setting = {"path": "cases/a.yaml", "answers": {"timeout_seconds": 1500}}
    assert fixer_agent._report_blockers(state, "a", blocker, [setting]) == "accepted"
    assert state.final["edits"] == [setting] and state.final["blockers"][0]["case_id"] == "a"
    # 研究対象・ケースはまだ書き換えない（ワーカーが止めた後に適用する）
    assert "timeout_seconds" not in (theme / "cases/a.yaml").read_text(encoding="utf-8")
