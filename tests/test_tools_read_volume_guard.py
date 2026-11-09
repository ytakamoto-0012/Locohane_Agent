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
            read_volume_guard_ask_timeout_seconds=30,
            read_volume_guard_sample_files_per_folder=2,
            subagent_max_parallel=1,
        ),
    )
    monkeypatch.setattr("src.tools._workdir._resolve_workdir", lambda need_write=False: tmp_path)
    asked = []

    async def fake_ask(question, choices, multi_select=False, timeout_seconds=None):
        asked.append((question, choices, timeout_seconds))
        return answers(choices)

    import sys

    import src.tools.ask_user_choice  # noqa: F401

    # src.tools は同名のツールを公開しているため、モジュールは sys.modules から引く。
    monkeypatch.setattr(sys.modules["src.tools.ask_user_choice"], "ask_choice", fake_ask)
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


def test_question_uses_its_own_timeout_and_shows_no_minutes(monkeypatch, tmp_path):
    _make_tree(tmp_path)
    _, asked = _setup(monkeypatch, tmp_path, lambda choices: choices[0])
    ai = _ai("explore", "explore")
    assert asyncio.run(guard.guard_read_volume(_input(ai))) is None
    question, choices, timeout_seconds = asked[0]
    # 全体の ask_user_choice_seconds（無期限でも）ではなく、この確認専用の秒数で待つ
    assert timeout_seconds == 30
    # 所要時間の数字は出さない
    for text in (question, *choices):
        assert "分" not in text.replace("分かかります", "") and "min" not in text


def test_parallel_read_delegations_ask_once_and_sample_blocks_all_siblings(monkeypatch, tmp_path):
    _make_tree(tmp_path)
    session, asked = _setup(monkeypatch, tmp_path, lambda choices: choices[1])
    ai = _ai("explore", "explore", "explore")

    async def run():
        return await asyncio.gather(*(guard.guard_read_volume(_input(ai, i)) for i in range(3)))

    results = asyncio.run(run())
    assert len(asked) == 1
    question, choices, _ = asked[0]
    assert "60" in question
    assert "2" in choices[1]  # フォルダごとに代表2件
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


def test_recorder_sees_only_subagents_started_inside_observe_tool_calls(tmp_path):
    """観測の範囲は task 文ではなくコンテキストで決まる（task 文が書き換わっても・空でも外れない）。"""
    from langchain_core.messages import ToolMessage

    from src import subagent

    recorder = guard.SurveyRecorder("")
    glob = {"name": "Glob", "id": "g1", "args": {}}

    def result(base):
        return [(ToolMessage(content='{"base": "%s", "files": []}' % str(base).replace("\\", "\\\\"), tool_call_id="g1"), None)]

    async def inside():
        with subagent.observe_tool_calls(recorder):
            # dispatch_agent はジョブを create_task で作る。作った時点のコンテキストを引き継ぐ
            await asyncio.create_task(_notify("[Work dir hint] rewritten task @1 resolved", tmp_path / "mine"))

    async def _notify(task, base):
        subagent._notify_tool_call_observers(task, 1, [glob], result(base))

    asyncio.run(inside())
    asyncio.run(_notify("another session", tmp_path / "other"))  # 範囲の外（別のセッション・別の委譲）
    assert recorder.bases == {str(tmp_path / "mine")}


def test_ai_message_without_id_does_not_block_every_later_delegation(monkeypatch, tmp_path):
    """構造調査の答え（ai_id=None）と、id の無い AIMessage が一致して以後の委譲が全部止まらない。"""
    _make_tree(tmp_path)
    session, asked = _setup(monkeypatch, tmp_path, lambda c: c[2])
    session[guard._DECISION_KEY] = {"kind": "stop", "answer": "stop", "ai_id": None, "total": 60, "per_folder": 2}
    ai = _ai("explore", "explore", msg_id=None)
    ai.id = None
    ai.tool_calls[0]["id"] = None
    ai.tool_calls[1]["id"] = None
    assert asyncio.run(guard.guard_read_volume(_input(ai))) is None
    assert asked == []


def test_ai_message_without_id_still_blocks_the_siblings_of_its_own_question(monkeypatch, tmp_path):
    _make_tree(tmp_path)
    _, asked = _setup(monkeypatch, tmp_path, lambda c: c[2])  # 中止
    ai = _ai("explore", "explore", msg_id=None)
    ai.id = None
    first = asyncio.run(guard.guard_read_volume(_input(ai, 0)))
    second = asyncio.run(guard.guard_read_volume(_input(ai, 1)))
    assert len(asked) == 1
    assert first is not None and second is not None  # ツール呼び出しの id の並びで同じ応答と分かる


def test_delegation_that_cannot_read_much_does_not_scan_the_work_dir(monkeypatch, tmp_path):
    _make_tree(tmp_path)
    _setup(monkeypatch, tmp_path, lambda c: c[0])
    scanned = []
    monkeypatch.setattr(guard, "count_source_files", lambda root, *a, **k: scanned.append(root) or {})
    single = {"name": "dispatch_agent", "args": {"agent_type": "verifier", "task": "check"}, "id": "v1"}
    ai = AIMessage(content="", tool_calls=[single], id="ai-v")
    assert asyncio.run(guard.guard_read_volume(_input(ai))) is None
    assert scanned == []


def test_question_marks_a_truncated_count(monkeypatch, tmp_path):
    _make_tree(tmp_path)
    _, asked = _setup(monkeypatch, tmp_path, lambda c: c[0])
    real = guard.count_source_files
    monkeypatch.setattr(guard, "count_source_files", lambda root, *a, **k: {**real(root), "truncated": True})
    asyncio.run(guard.guard_read_volume(_input(_ai("explore", "explore"))))
    assert "60+" in asked[0][0]
