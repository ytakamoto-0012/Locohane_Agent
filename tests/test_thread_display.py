"""会話履歴を画面へ送るときの間引き（src/thread_display.py・thread_store.get_thread_detail）のテスト。

長時間の処理をさせたスレッドは Step が数万件になり、開き直すたびに全部ブラウザへ送って重くなっていた。
"""

from __future__ import annotations

import asyncio
import itertools

from src import thread_store
from src.thread_display import DisplayLimits, cap_step_content, select_elements, select_steps

TOKEN = "🔢 トークン使用量\n"
PLAN = "📋 実行計画\n"
LIMITS = DisplayLimits(max_messages=3, max_side_steps=2, max_step_chars=10, status_prefixes=(TOKEN, PLAN))

_ids = itertools.count()


def _step(type_: str, parent: str | None = None, output: str = "", **extra) -> dict:
    return {"id": f"s{next(_ids)}", "type": type_, "parentId": parent, "output": output, "start": "t", "end": "t", **extra}


def _thread() -> tuple[list[dict], dict]:
    run = _step("run")
    steps = [run]
    named: dict = {"run": run}
    for i in range(5):
        steps.append(_step("user_message", run["id"], f"q{i}"))
        tool = _step("tool", run["id"], "x" * 50)
        steps.append(tool)
        for j in range(4):
            steps.append(_step("tool", tool["id"], f"child{i}-{j}"))
        steps.append(_step("assistant_message", run["id"], f"a{i}" + "y" * 50))
        steps.append(_step("assistant_message", run["id"], TOKEN + str(i)))
        named[f"tool{i}"] = tool
    plan = _step("assistant_message", run["id"], PLAN + "[]")
    steps.insert(2, plan)  # 古い位置にある最新の計画
    running = _step("tool", run["id"], end=None)
    steps.insert(3, running)  # 古いが実行中
    named.update(plan=plan, running=running)
    return steps, named


def test_select_steps_keeps_what_the_screen_draws():
    steps, named = _thread()
    kept = select_steps(steps, LIMITS)
    ids = {s["id"] for s in kept}
    outputs = [s["output"] for s in kept if s["type"] in ("user_message", "assistant_message")]
    # メインの会話は最後の3件（状態メッセージは種類ごとに最新の1件）
    main = [o for o in outputs if not o.startswith((TOKEN, PLAN))]
    assert main == ["a3" + "y" * 50, "q4", "a4" + "y" * 50]
    assert [o for o in outputs if o.startswith(TOKEN)] == [TOKEN + "4"]
    assert named["plan"]["id"] in ids
    # サイドパネルは最後の2件と実行中のもの、中の Step は各2件
    assert {named["tool3"]["id"], named["tool4"]["id"], named["running"]["id"]} <= ids
    assert named["tool2"]["id"] not in ids
    children = [s["output"] for s in kept if s.get("parentId") == named["tool4"]["id"]]
    assert children == ["child4-2", "child4-3"]
    # 親（run）を残す（無いと画面側が子を捨てる）・順序は元のまま
    assert kept[0]["id"] == named["run"]["id"]
    order = [s["id"] for s in steps if s["id"] in ids]
    assert [s["id"] for s in kept] == order


def test_tool_content_is_truncated_but_messages_are_not():
    steps, named = _thread()
    kept = {s["id"]: s for s in select_steps(steps, LIMITS)}
    tool = kept[named["tool4"]["id"]]
    assert tool["output"].startswith("x" * 10) and "40 more characters" in tool["output"]
    assert named["tool4"]["output"] == "x" * 50  # 元は書き換えない
    assert any(s["output"] == "a4" + "y" * 50 for s in kept.values())
    assert cap_step_content({"type": "tool", "input": "abc"}, 10) == {"type": "tool", "input": "abc"}


def test_no_limits_returns_everything():
    steps, _ = _thread()
    assert select_steps(steps, DisplayLimits()) == steps


def test_select_elements():
    elements = [{"id": "e1", "forId": "a"}, {"id": "e2", "forId": "b"}, {"id": "e3"}, {"id": "e4", "type": "tasklist", "forId": "b"}]
    assert [e["id"] for e in select_elements(elements, {"a"})] == ["e1", "e3", "e4"]


def test_get_thread_detail_trims_with_sqlite(tmp_path):
    async def scenario():
        conn = await thread_store.init_db(tmp_path / "t.sqlite")
        try:
            await thread_store.upsert_thread_stub(conn, "t1", "alice")
            steps, named = _thread()
            for i, s in enumerate(steps):
                await thread_store.upsert_step_row(conn, {**s, "threadId": "t1", "createdAt": f"2026-11-18T00:00:{i:02d}Z"})
            full = await thread_store.get_thread_detail(conn, "t1")
            trimmed = await thread_store.get_thread_detail(conn, "t1", LIMITS)
            expected = [s["id"] for s in select_steps(steps, LIMITS)]
            return full, trimmed, expected, named
        finally:
            await conn.close()

    full, trimmed, expected, named = asyncio.run(scenario())
    assert len(full["steps"]) == len(_thread()[0])
    assert [s["id"] for s in trimmed["steps"]] == expected
    tool = next(s for s in trimmed["steps"] if s["id"] == named["tool4"]["id"])
    assert "more characters" in tool["output"]


def test_data_layer_caps_saved_tool_content(tmp_path):
    async def scenario():
        conn = await thread_store.init_db(tmp_path / "t.sqlite")
        try:
            layer = thread_store.ChatThreadDataLayer(conn, tmp_path / "elements", DisplayLimits(max_step_chars=5))
            await thread_store.upsert_thread_stub(conn, "t1", "alice")
            # queue_until_user_message を通さずに保存の本体だけを確かめる
            await thread_store.ChatThreadDataLayer.create_step.__wrapped__(
                layer, {"id": "x", "threadId": "t1", "type": "tool", "output": "0123456789"}
            )
            await thread_store.ChatThreadDataLayer.create_step.__wrapped__(
                layer, {"id": "m", "threadId": "t1", "type": "assistant_message", "output": "0123456789"}
            )
            return (await thread_store.get_thread_detail(conn, "t1"))["steps"]
        finally:
            await conn.close()

    steps = {s["id"]: s for s in asyncio.run(scenario())}
    assert steps["x"]["output"].startswith("01234") and "5 more characters" in steps["x"]["output"]
    assert steps["m"]["output"] == "0123456789"


def test_get_older_messages(tmp_path):
    # 会話欄を上へスクロールしたとき、表示中の一番上より前のメインの会話を新しい方から読み足す
    async def scenario():
        conn = await thread_store.init_db(tmp_path / "t.sqlite")
        try:
            await thread_store.upsert_thread_stub(conn, "t1", "alice")
            steps, _ = _thread()
            for i, s in enumerate(steps):
                await thread_store.upsert_step_row(conn, {**s, "threadId": "t1", "createdAt": f"2026-11-18T00:00:{i:02d}Z"})
            await thread_store.upsert_element_meta(conn, "e1", "t1", steps[1]["id"], {"id": "e1", "forId": steps[1]["id"], "type": "image"})
            visible = select_steps(steps, LIMITS)
            first_main = next(s for s in visible if s["type"] == "assistant_message" and not s["output"].startswith((TOKEN, PLAN)))
            page1 = await thread_store.get_older_messages(conn, "t1", first_main["id"], 4, (TOKEN, PLAN))
            page2 = await thread_store.get_older_messages(conn, "t1", page1[0][0]["id"], 4, (TOKEN, PLAN))
            missing = await thread_store.get_older_messages(conn, "t1", "nope", 4, (TOKEN, PLAN))
            return page1, page2, missing
        finally:
            await conn.close()

    page1, page2, missing = asyncio.run(scenario())
    messages, elements, has_more = page1
    # 状態メッセージ（トークン使用量・計画）・ツールの Step は含めない。古い順に並べる
    assert [m["output"][:2] for m in messages] == ["a1", "q2", "a2", "q3"]
    assert has_more and elements == []
    messages, elements, has_more = page2
    assert [m["output"][:2] for m in messages] == ["q0", "a0", "q1"]
    assert not has_more and [e["id"] for e in elements] == ["e1"]
    assert missing == ([], [], False)


def test_last_user_message_is_kept_even_beyond_the_limit():
    # 1回の指示で回答が表示件数を超えても、最後のユーザー発言は残す（画面は送信済みか・中断の後に発言があるかを
    # 最後のユーザー発言で判断する）
    run = _step("run")
    user = _step("user_message", run["id"], "q")
    answers = [_step("assistant_message", run["id"], f"a{i}") for i in range(10)]
    kept = select_steps([run, user, *answers], LIMITS)
    assert [s["output"] for s in kept if s["type"] != "run"] == ["q", "a7", "a8", "a9"]


def test_stopped_or_background_steps_are_not_kept_as_running():
    # end の無い Step でも、止めたもの・裏で続くものは実行中に数えない（画面の isRunningStep と同じ）
    run = _step("run")
    stopped = [_step("tool", run["id"], end=None, metadata={"stopped_reason": "interrupted"}) for _ in range(5)]
    background = _step("tool", run["id"], end=None, metadata={"background": True})
    running = _step("tool", run["id"], end=None)
    steps = [run, *stopped, background, running, *(_step("tool", run["id"]) for _ in range(3))]
    ids = {s["id"] for s in select_steps(steps, LIMITS)}
    assert running["id"] in ids
    assert not ({s["id"] for s in stopped} | {background["id"]}) & ids


def test_trimmed_thread_from_sqlite_skips_stopped_steps_and_loads_only_needed_elements(tmp_path):
    async def scenario():
        conn = await thread_store.init_db(tmp_path / "t.sqlite")
        try:
            await thread_store.upsert_thread_stub(conn, "t1", "alice")
            run = _step("run")
            old = _step("tool", run["id"], end=None, metadata={"stopped_reason": "interrupted"})
            tools = [_step("tool", run["id"]) for _ in range(3)]
            for i, s in enumerate([run, old, *tools]):
                await thread_store.upsert_step_row(conn, {**s, "threadId": "t1", "createdAt": f"2026-11-18T00:00:{i:02d}Z"})
            await thread_store.upsert_element_meta(conn, "e-old", "t1", old["id"], {"id": "e-old", "forId": old["id"], "type": "image"})
            await thread_store.upsert_element_meta(conn, "e-new", "t1", tools[-1]["id"], {"id": "e-new", "forId": tools[-1]["id"], "type": "image"})
            await thread_store.upsert_element_meta(conn, "e-free", "t1", None, {"id": "e-free", "type": "file"})
            await thread_store.upsert_element_meta(conn, "e-task", "t1", old["id"], {"id": "e-task", "forId": old["id"], "type": "tasklist"})
            return await thread_store.get_thread_detail(conn, "t1", LIMITS), old
        finally:
            await conn.close()

    thread, old = asyncio.run(scenario())
    assert old["id"] not in {s["id"] for s in thread["steps"]}
    assert sorted(e["id"] for e in thread["elements"]) == ["e-free", "e-new", "e-task"]


def test_list_threads_returns_the_stored_created_at_metadata_and_tags(tmp_path):
    from chainlit.types import Pagination, ThreadFilter

    async def scenario():
        conn = await thread_store.init_db(tmp_path / "t.sqlite")
        try:
            await thread_store.save_thread(conn, "t1", owner="alice", name="n", metadata={"k": "v"}, tags=["x"])
            layer = thread_store.ChatThreadDataLayer(conn, tmp_path / "elements", DisplayLimits())
            page = await layer.list_threads(Pagination(first=10), ThreadFilter(userId="alice"))
            cursor = await conn.execute("SELECT created_at FROM threads WHERE id = 't1'")
            return page.data, (await cursor.fetchone())[0]
        finally:
            await conn.close()

    [thread], created_at = asyncio.run(scenario())
    assert thread["createdAt"] == created_at
    assert thread["metadata"] == {"k": "v"} and thread["tags"] == ["x"]
