"""src/tools/_read_volume_guard.py（大量のファイルを読む前の確認）のテスト。"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from src.tools import _read_volume_guard as guard


class _Session(dict):
    def set(self, key, value):
        self[key] = value


def _make_tree(root, years=5, images=8, md=4):
    for y in range(years):
        d = root / f"20{21 + y}"
        (d / "ocr_md").mkdir(parents=True)
        for i in range(images):
            (d / f"photo_{i}.png").write_bytes(b"x")
        for i in range(md):
            (d / "ocr_md" / f"md_photo_{i}.md").write_text("x", encoding="utf-8")
    (root / "_tmp_old").mkdir()
    (root / "_tmp_old" / "leftover.png").write_bytes(b"x")
    (root / "notes.bin").write_bytes(b"x")


def _setup(monkeypatch, tmp_path, answers):
    session = _Session()
    monkeypatch.setattr(guard.cl, "user_session", session)
    monkeypatch.setattr(
        guard._state,
        "_LLM_CONFIG",
        SimpleNamespace(
            read_volume_guard_enabled=True,
            read_volume_guard_threshold_files=30,
            read_volume_guard_seconds_per_file=15,
            read_volume_guard_sample_files_per_folder=2,
            subagent_max_parallel=1,
        ),
    )
    monkeypatch.setattr("src.tools._workdir._resolve_workdir", lambda need_write=False: tmp_path)
    asked = []

    async def fake_ask(question, choices):
        asked.append((question, choices))
        return answers(choices)

    import sys

    import src.tools.ask_user_choice  # noqa: F401

    # src.tools は同名のツールを公開しているため、モジュールは sys.modules から引く。
    monkeypatch.setattr(sys.modules["src.tools.ask_user_choice"].ask_user_choice, "coroutine", fake_ask)
    return session, asked


def _ai(*agent_types, msg_id="ai-1"):
    calls = [
        {"name": "dispatch_agent", "args": {"agent_type": a, "task": "read"}, "id": f"c{i}"}
        for i, a in enumerate(agent_types)
    ]
    return AIMessage(content="", tool_calls=calls, id=msg_id)


def _input(ai, index=0):
    return {"__type": "tool_call_with_context", "tool_call": ai.tool_calls[index], "state": {"messages": [ai]}}


def test_count_source_files_skips_session_tmp_and_unknown_types(tmp_path):
    _make_tree(tmp_path)
    inv = guard.count_source_files(tmp_path)
    assert inv == {"images": 40, "documents": 0, "text": 20, "total": 60, "folders": 10, "truncated": False}


def test_estimate_minutes_divides_by_parallel_and_rounds_up():
    assert guard.estimate_minutes(60, 15, 1) == 15
    assert guard.estimate_minutes(60, 15, 4) == 4
    assert guard.estimate_minutes(1, 15, 1) == 1
    assert guard.estimate_minutes(0, 15, 1) == 0


def test_parallel_read_delegations_ask_once_and_sample_blocks_all_siblings(monkeypatch, tmp_path):
    _make_tree(tmp_path)
    session, asked = _setup(monkeypatch, tmp_path, lambda choices: choices[1])
    ai = _ai("explore", "explore", "explore")

    async def run():
        return await asyncio.gather(*(guard.guard_read_volume(_input(ai, i)) for i in range(3)))

    results = asyncio.run(run())
    assert len(asked) == 1
    question, choices = asked[0]
    assert "60" in question and "15" in question
    assert "20" not in choices[0] and "5" in choices[1]  # 代表 2件×10フォルダ=20件 → 約5分
    for i, result in enumerate(results):
        msg = result["messages"][0]
        assert msg.tool_call_id == f"c{i}" and "representative" in msg.content
    # 次の応答の委譲は通す（同じ質問を繰り返さない）
    later = _ai("explore", "explore", msg_id="ai-2")
    assert asyncio.run(guard.guard_read_volume(_input(later))) is None
    assert len(asked) == 1


def test_read_all_or_timeout_runs_the_delegations(monkeypatch, tmp_path):
    from src.tools._ask_echo import ASK_TIMEOUT_MESSAGE

    _make_tree(tmp_path)
    for answer in (lambda c: c[0], lambda c: ASK_TIMEOUT_MESSAGE):
        _, asked = _setup(monkeypatch, tmp_path, answer)
        ai = _ai("explore", "worker")
        assert asyncio.run(guard.guard_read_volume(_input(ai, 0))) is None
        assert asyncio.run(guard.guard_read_volume(_input(ai, 1))) is None
        assert len(asked) == 1


def test_stop_and_free_text_answers_are_passed_back(monkeypatch, tmp_path):
    _make_tree(tmp_path)
    _setup(monkeypatch, tmp_path, lambda c: c[2])
    ai = _ai("explore", "explore")
    assert "chose not to read" in asyncio.run(guard.guard_read_volume(_input(ai)))["messages"][0].content
    _setup(monkeypatch, tmp_path, lambda c: "2023年だけ読んで")
    ai = _ai("explore", "explore")
    assert "2023年だけ読んで" in asyncio.run(guard.guard_read_volume(_input(ai)))["messages"][0].content


def test_single_delegation_or_small_folder_does_not_ask(monkeypatch, tmp_path):
    _make_tree(tmp_path)
    _, asked = _setup(monkeypatch, tmp_path, lambda c: c[2])
    assert asyncio.run(guard.guard_read_volume(_input(_ai("explore")))) is None
    assert asyncio.run(guard.guard_read_volume(_input(_ai("verifier", "verifier")))) is None
    batch = AIMessage(
        content="",
        tool_calls=[{"name": "dispatch_agent_batch", "args": {"agent_type": "explore", "files": ["@1", "@2"]}, "id": "b"}],
        id="ai-b",
    )
    assert asyncio.run(guard.guard_read_volume(_input(batch))) is None
    assert asked == []


def test_batch_over_the_whole_folder_asks(monkeypatch, tmp_path):
    _make_tree(tmp_path)
    _, asked = _setup(monkeypatch, tmp_path, lambda c: c[2])
    batch = AIMessage(
        content="",
        tool_calls=[{"name": "dispatch_agent_batch", "args": {"agent_type": "explore", "pattern": "*/*.png"}, "id": "b"}],
        id="ai-b",
    )
    assert asyncio.run(guard.guard_read_volume(_input(batch))) is not None
    assert len(asked) == 1


def _glob_record(task, base):
    import json

    return {"name": "Glob", "task": f"[Working directory]\n{{}}\n\n{task}", "is_error": False,
            "result": json.dumps({"base": str(base), "base_contents": {}}, ensure_ascii=False)}


def _explore_input(task="Survey the Datas folder"):
    ai = AIMessage(content="", tool_calls=[{"name": "dispatch_agent", "args": {"agent_type": "explore", "task": task}, "id": "s"}], id="ai-s")
    return _input(ai)


def test_survey_explore_asks_right_after_it_returns_and_appends_the_answer(monkeypatch, tmp_path):
    from langchain_core.messages import ToolMessage

    _make_tree(tmp_path / "Datas")
    session, asked = _setup(monkeypatch, tmp_path, lambda c: c[1])
    recorder = guard.survey_recorder(_explore_input())
    assert recorder is not None
    recorder(_glob_record("Survey the Datas folder", tmp_path / "Datas"))
    recorder(_glob_record("Survey the Datas folder", tmp_path / "Datas" / "2021"))  # 入れ子は親だけ数える
    recorder(_glob_record("another session's task", tmp_path))  # 別の委譲の記録は拾わない
    result = {"messages": [ToolMessage(content="5 year folders, 60 files", tool_call_id="s")]}
    result = asyncio.run(guard.ask_after_survey(recorder, result))
    assert len(asked) == 1 and "60" in asked[0][0]
    content = result["messages"][0].content
    assert content.startswith("5 year folders") and "[Reading volume]" in content and "representative" in content
    # 確認済みなので、後の explore も並列の委譲も聞かない
    assert guard.survey_recorder(_explore_input()) is None
    assert asyncio.run(guard.guard_read_volume(_input(_ai("explore", "explore", msg_id="ai-3")))) is None
    assert len(asked) == 1


def test_survey_without_glob_or_with_few_files_does_not_ask(monkeypatch, tmp_path):
    from langchain_core.messages import ToolMessage

    _make_tree(tmp_path / "Datas", years=1, images=3, md=2)
    _, asked = _setup(monkeypatch, tmp_path, lambda c: c[2])
    for bases in ([], [tmp_path / "Datas"]):
        recorder = guard.survey_recorder(_explore_input())
        for base in bases:
            recorder(_glob_record("Survey the Datas folder", base))
        result = {"messages": [ToolMessage(content="ok", tool_call_id="s")]}
        assert asyncio.run(guard.ask_after_survey(recorder, result))["messages"][0].content == "ok"
    assert asked == []
