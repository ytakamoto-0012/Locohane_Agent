"""スキル研究室の判定が、表記ゆれと「AI が判断する点」で合格にできること（2026-11-11 ユーザー報告）。

「回答に必ず入っているべき言葉」に ver_57 と書くと、AI が ver.57 / ver: 57 / revision:57 と答えただけで
不合格になり、「AI が判断する点」に「意味が同じなら合格」と書いても、言葉の照合で落ちた回には一度も
使われていなかった。
"""

from admin.skill_lab import evaluation, judge
from admin.skill_lab.llm import extract_json


def _result(final_answer: str, judge_text: str | None = None) -> dict:
    return {
        "case_id": "c1",
        "final_answer": final_answer,
        "transcript": [],
        "rules_pass": False,
        "rule_results": {"response_contains": {"pass": False, "detail": "missing: ['ver_57']", "missing": ["ver_57"]}},
        "judge": judge_text,
    }


def test_separator_differences_pass_without_the_llm(monkeypatch):
    monkeypatch.setattr(judge, "ask_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM must not be called")))
    for answer in ("バージョンは ver.57 です", "ver: 57", "Ver-57", "VER 57"):
        [verdict] = judge.judge_results(None, spec="", results=[_result(answer)], excerpt_chars=2000)
        assert verdict["outcome"] == "pass", answer
    # 数字どうしの区切りは意味が変わるので揃えない
    assert not judge._passes_normalized("response_contains", {"missing": ["1.5"]}, "15")


def test_author_criteria_reach_the_review_and_the_judge_overrides_the_text_check(monkeypatch):
    prompts = []

    def fake_ask_json(_config, system, user):
        prompts.append((system, user))
        # 見直し（rule review）は不合格、AI judge は合格と答える
        return {"pass": system == judge._SYSTEM, "reason": "same version"}

    monkeypatch.setattr(judge, "ask_json", fake_ask_json)
    [verdict] = judge.judge_results(
        None, spec="", results=[_result("revision:57 に更新しました", "意味が同じなら合格")], excerpt_chars=2000
    )
    assert verdict["outcome"] == "pass"
    review_user = prompts[0][1]
    judge_user = prompts[1][1]
    assert "意味が同じなら合格" in review_user  # 以前は見直しに渡していなかった
    assert "ver_57" in judge_user  # 落ちた照合も AI judge に見せる


def test_without_judge_text_a_failed_review_stays_failed(monkeypatch):
    monkeypatch.setattr(judge, "ask_json", lambda *a, **k: {"pass": False, "reason": "different"})
    [verdict] = judge.judge_results(None, spec="", results=[_result("revision:58")], excerpt_chars=2000)
    assert verdict["outcome"] == "fail" and "ai_judge" not in verdict


def test_tool_rules_are_not_overridden_by_the_judge(monkeypatch):
    monkeypatch.setattr(judge, "ask_json", lambda *a, **k: {"pass": True, "reason": "ok"})
    result = _result("ver_57", "意味が同じなら合格")
    result["rule_results"] = {"tool_called_any": {"pass": False, "detail": "expected any of ['read_skill']"}}
    [verdict] = judge.judge_results(None, spec="", results=[result], excerpt_chars=2000)
    assert verdict["outcome"] == "fail"


def test_output_files_are_shown_to_the_judge():
    result = {"transcript": [], "output_files": [{"path": "chart.png", "size": 1234}]}
    assert "[output file] chart.png (1234 bytes)" in evaluation.transcript_excerpt(result, 2000)
    # 長い記録を切り詰めても生成物の行は残る
    long = {"transcript": [{"type": "HumanMessage", "content": "x" * 800}] * 20, "output_files": result["output_files"]}
    assert "[output file] chart.png" in evaluation.transcript_excerpt(long, 500)


def test_extract_json_ignores_think_blocks_and_format_echoes():
    assert extract_json('<think>format {"pass": true or false}</think>{"pass": true, "reason": "x"}') == {"pass": True, "reason": "x"}
    assert extract_json('The format is {"pass": true or false}. {"pass": false, "reason": "y"}') == {"pass": False, "reason": "y"}


def test_same_ai_request_is_not_queued_twice(tmp_path):
    """生成中に「AI に依頼」を何度押しても、同じ依頼は1件だけ積む（1回試行は問題ごと、下書きは名前ごと）。"""
    import json as _json

    import pytest

    from admin.skill_lab import themes

    directory = tmp_path / "theme"
    directory.mkdir()
    (directory / themes.THEME_FILENAME).write_text(_json.dumps({"id": "t", "tasks": []}), encoding="utf-8")
    themes.add_task(directory, "draft_spec", {"request": ""}, actor="a")
    with pytest.raises(themes.ThemeError):
        themes.add_task(directory, "draft_spec", {"request": "again"}, actor="a")
    themes.add_task(directory, "trial", {"case_id": "c1"}, actor="a")
    themes.add_task(directory, "trial", {"case_id": "c2"}, actor="a")  # 別の問題は積める
    with pytest.raises(themes.ThemeError):
        themes.add_task(directory, "trial", {"case_id": "c1"}, actor="a")
    # 終わった依頼の後なら、また頼める
    themes.update(directory, lambda d: [t.update(status="done") for t in d["tasks"]])
    themes.add_task(directory, "draft_spec", {"request": ""}, actor="a")
