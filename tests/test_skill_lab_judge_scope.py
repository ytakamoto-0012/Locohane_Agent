"""スキル研究室の判定・下書きの不具合（2026-11-13 ユーザー報告）の回帰テスト。

- 委譲先（サブエージェント）の中で検証対象のスキルを read_skill しても「読んでいない」として不合格になった。
- 判断の観点にも「やってはいけないこと」にも無いことで AI judge が不合格にした。
- LLM の書く文章がインスタンスの言語設定に関係なく英語になった。
"""

from types import SimpleNamespace

from admin import i18n
from admin.skill_lab import evaluation, judge, llm


def _subagent_result(rule_results: dict, judge_text: str | None = None) -> dict:
    return {
        "case_id": "c1",
        "final_answer": "グラフを作成しました",
        "transcript": [{"type": "AIMessage", "tool_calls": [{"name": "dispatch_agent", "args": {"agent_type": "worker"}}]}],
        "subagent_tool_calls": [
            {"dispatch": 1, "task": "t", "iteration": 1, "name": "read_skill", "args": {"skill_name": "fee-chart"}, "is_error": False, "result": "<skill_content>"},
        ],
        "rules_pass": False,
        "rule_results": rule_results,
        "judge": judge_text,
    }


def test_skill_read_inside_a_subagent_satisfies_tool_checks(monkeypatch):
    monkeypatch.setattr(judge, "ask_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM must not be called")))
    rules = {
        "tool_called_any": {"pass": False, "detail": "x", "expected": ["read_skill"]},
        "tool_call_args_contains:read_skill": {"pass": False, "detail": "x", "expected": {"skill_name": "fee-chart"}, "calls": []},
    }
    [verdict] = judge.judge_results(None, spec="", results=[_subagent_result(rules)], excerpt_chars=2000)
    assert verdict["outcome"] == "pass"
    assert verdict["rule_review"]["how"] == "subagent"


def test_other_skill_or_failed_read_in_a_subagent_does_not_count(monkeypatch):
    monkeypatch.setattr(judge, "ask_json", lambda *a, **k: {"pass": True, "reason": "ok"})
    rules = {"tool_call_args_contains:read_skill": {"pass": False, "detail": "x", "expected": {"skill_name": "other"}, "calls": []}}
    result = _subagent_result(rules)
    assert not judge._passes_with_subagents("tool_call_args_contains:read_skill", rules["tool_call_args_contains:read_skill"], result)
    result["subagent_tool_calls"][0]["is_error"] = True
    assert not judge._passes_with_subagents("tool_called_any", {"expected": ["read_skill"]}, result)
    # tool_not_called は委譲したかを確かめる用途があるため、サブエージェントの呼び出しでは見直さない
    assert not judge._passes_with_subagents("tool_not_called", {"expected": ["read_skill"]}, _subagent_result({}))


def test_subagent_calls_are_shown_to_the_judge():
    text = evaluation.transcript_excerpt(_subagent_result({}), 2000)
    assert '[sub-agent 1 tool call] read_skill {"skill_name": "fee-chart"}' in text


def test_judge_failure_without_a_cited_requirement_is_treated_as_pass(monkeypatch):
    monkeypatch.setattr(judge, "ask_json", lambda *a, **k: {"pass": False, "basis": "", "reason": "フォントを確認していない"})
    verdict = judge.judge_run(None, spec="", judge_instruction="表とグラフがある", result={"transcript": []}, excerpt_chars=1000)
    assert verdict["pass"] is True and "フォントを確認していない" in verdict["reason"]

    monkeypatch.setattr(judge, "ask_json", lambda *a, **k: {"pass": False, "basis": "表とグラフがある", "reason": "表が無い"})
    verdict = judge.judge_run(None, spec="", judge_instruction="表とグラフがある", result={"transcript": []}, excerpt_chars=1000)
    assert verdict == {"pass": False, "reason": "表が無い", "basis": "表とグラフがある"}


def test_judge_prompt_limits_fail_reasons_to_viewpoint_and_spec():
    assert '"Must not do"' in judge._SYSTEM and "not a reason to fail" in judge._SYSTEM


def test_output_language_follows_instance_setting_then_dashboard():
    assert i18n.output_language("ja", "en") == "ja"
    assert i18n.output_language("en", "ja") == "en"
    assert i18n.output_language("auto", "en") == "en"
    assert i18n.output_language("auto", None) == "ja"
    assert "Japanese" in i18n.output_language_rule("ja")


def test_lab_llm_calls_get_the_output_language(monkeypatch):
    seen = []

    class FakeModel:
        async def ainvoke(self, messages):
            seen.append(messages[0].content)
            return SimpleNamespace(content='{"pass": true}', usage_metadata=None)

    async def fake_build_model(_config):
        return FakeModel()

    import src.llm

    monkeypatch.setattr(src.llm, "build_model", fake_build_model)
    with llm.output_language("ja"):
        llm.ask_json(None, "system", "user")
    llm.ask_json(None, "system", "user")
    assert "## Output language" in seen[0] and "Japanese" in seen[0]
    assert "## Output language" not in seen[1]


def test_thinking_and_text_with_tool_calls_reach_the_ai_and_limits_are_configurable():
    """要因分析の AI には、思考（末尾）とツールを呼ぶときの本文も見せる。切り詰めは設定で変えられる。"""
    result = {
        "transcript": [
            {"type": "HumanMessage", "content": "合言葉を教えて"},
            {"type": "AIMessage", "content": "スキルを読みます", "reasoning": "前置き" * 1500 + "合言葉のスキルがあるので読む",
             "tool_calls": [{"name": "read_skill", "args": {"skill_name": "secret-word"}}]},
        ]
    }
    text = evaluation.transcript_excerpt(result, 10000)
    assert "[AI thinking] …" in text and text.index("合言葉のスキルがあるので読む") < text.index("[tool call] read_skill")
    assert "[AI] スキルを読みます" in text
    with evaluation.limits(evaluation.ExcerptLimits(thinking_chars=0, ai_note_chars=3)):
        text = evaluation.transcript_excerpt(result, 10000)
    assert "[AI thinking]" not in text and "[AI] スキル\n" in text


def test_limits_come_from_config(monkeypatch, tmp_path):
    from src.config import load_config

    monkeypatch.setenv("SKILL_LAB_EXCERPT_THINKING_CHARS", "123")
    monkeypatch.setenv("SKILL_LAB_FIXER_HISTORY_ITERATIONS", "9")
    limits = evaluation.ExcerptLimits.from_config(load_config())
    assert limits.thinking_chars == 123 and limits.fixer_history_iterations == 9 and limits.judge_spec_chars == 12000


def test_run_case_records_clipped_thinking():
    from langchain_core.messages import AIMessage

    from evals import run_case

    msg = AIMessage(content="", additional_kwargs={"reasoning_content": "a" * 50 + "結論"})
    [entry] = run_case._serialize_messages([msg], reasoning_chars=20)
    assert entry["reasoning"].endswith("結論") and "omitted" in entry["reasoning"]
    assert "reasoning" not in run_case._serialize_messages([msg], reasoning_chars=0)[0]


def test_judging_runs_in_parallel_with_the_workers_concurrency(monkeypatch):
    """結果の判定は、ワーカーの並列数まで同時に行う（順序・同時数の上限・使用量の記録・進み具合を保つ）。"""
    import threading
    import time

    seen_usage = []
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def fake_ask_json(_config, _system, user):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        sink = llm._USAGE_SINK.get()
        if sink:
            sink({"total_tokens": 1})
        with lock:
            state["now"] -= 1
        return {"pass": True, "basis": "", "reason": "ok"}

    monkeypatch.setattr(judge, "ask_json", fake_ask_json)
    results = [{"case_id": f"c{i}", "repeat_index": 1, "judge": "観点", "transcript": [], "rules_pass": True} for i in range(8)]
    progress = []
    with llm.usage_sink(lambda u: seen_usage.append(u)):
        verdicts = judge.judge_results(
            None, spec="", results=results, excerpt_chars=1000, max_workers=3, on_progress=lambda done, total, cid: progress.append((done, total))
        )
    assert [v["case_id"] for v in verdicts] == [f"c{i}" for i in range(8)]  # 結果と同じ順
    assert all(v["outcome"] == "pass" for v in verdicts)
    assert 1 < state["peak"] <= 3  # 並列に動き、上限を超えない
    assert len(seen_usage) == 8  # スレッドでも使用量の記録が呼び出し側へ届く
    assert sorted(progress) == [(i, 8) for i in range(1, 9)]


def test_judging_with_one_worker_is_sequential(monkeypatch):
    order = []
    monkeypatch.setattr(judge, "ask_json", lambda *a, **k: order.append(1) or {"pass": True, "basis": "", "reason": ""})
    results = [{"case_id": "a", "judge": "x", "transcript": [], "rules_pass": True}] * 3
    judge.judge_results(None, spec="", results=results, excerpt_chars=100)
    assert len(order) == 3


def test_truncated_excerpt_still_lists_every_tool_call():
    """長い記録の途中を省いても、全部のツール呼び出しの一覧は残す（以前は AI judge が、実際に呼んだ provide_download を
    「記録に無い」として不合格にしていた）。"""
    transcript = [{"type": "HumanMessage", "content": "作って"}]
    for i in range(60):
        transcript.append({"type": "AIMessage", "content": "", "tool_calls": [{"name": f"step_{i}", "args": {"i": i}}]})
        transcript.append({"type": "ToolMessage", "tool_name": f"step_{i}", "content": "x" * 500})
    transcript.append({"type": "AIMessage", "content": "", "tool_calls": [{"name": "provide_download", "args": {"path": "a.xlsx"}}]})
    transcript.append({"type": "AIMessage", "content": "完了"})
    result = {"transcript": transcript, "subagent_tool_calls": [{"dispatch": 1, "name": "search_documents", "args": {"q": "x"}}]}
    text = evaluation.transcript_excerpt(result, 6000)
    assert len(text) <= 6000 + 200
    assert "(snip" in text and "step_30" in text and "provide_download" in text and "search_documents" in text
    assert "[all tool calls of the assistant, in order]" in text
