"""全く同じ応答を連続で繰り返すループの検知（src/tool_loop_guard.py）のテスト。"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.llm import ThinkingLoopDetected, ToolCallLoopDetected, tool_loop_nudge_text
from src.tool_loop_guard import (
    detect_tool_call_loop,
    find_leaked_tool_markup,
    leaked_tool_markup_error,
    raise_if_tool_call_loop,
)

ERROR = "エラー: create_planの直後はapprove_planを呼んでください（他のツールは実行されませんでした）。"


@dataclass
class _Cfg:
    tool_loop_guard_enabled: bool = True
    tool_loop_guard_max_repeats: int = 3
    tool_loop_guard_exclude_tools: list[str] = field(default_factory=lambda: ["check_script_job"])


_seq = 0


def _step(*calls: tuple[str, dict, str], content: str = "", status: str = "success") -> list:
    """(ツール名, 引数, 結果) の組で1ステップ（AIMessage と ToolMessage 群）を作る。"""
    global _seq
    tool_calls, results = [], []
    for name, args, result in calls:
        _seq += 1
        tool_calls.append({"name": name, "args": args, "id": f"c{_seq}"})
        results.append(ToolMessage(content=result, tool_call_id=f"c{_seq}", name=name, status=status))
    return [AIMessage(content=content, tool_calls=tool_calls), *results]


def _history(*steps) -> list:
    messages: list = [SystemMessage(content="sys"), HumanMessage(content="go")]
    for step in steps:
        messages.extend(step)
    return messages


def _err(task: str = "x") -> list:
    return _step(("dispatch_agent", {"task": task}, ERROR))


def test_same_response_three_times_is_a_loop():
    loop = detect_tool_call_loop(_history(_err(), _err(), _err()), _Cfg())
    assert loop is not None and loop.repeats == 3 and loop.tool_names == ("dispatch_agent",)
    assert "create_planの直後" in loop.result_excerpt


def test_two_repeats_is_not_a_loop():
    assert detect_tool_call_loop(_history(_err(), _err()), _Cfg()) is None


def test_same_response_is_a_loop_even_if_results_differ():
    steps = [_step(("run_script", {"s": "a.py"}, f"took {i}ms")) for i in range(3)]
    assert detect_tool_call_loop(_history(*steps), _Cfg()) is not None


def test_success_results_are_not_required():
    steps = [_step(("get_plan_status", {}, "Plan Mode")) for _ in range(3)]
    assert detect_tool_call_loop(_history(*steps), _Cfg()) is not None


def test_different_args_are_not_a_loop_even_with_the_same_error():
    assert detect_tool_call_loop(_history(_err("a"), _err("b"), _err("c")), _Cfg()) is None


def test_different_text_is_a_different_response():
    steps = [_step(("Read", {"p": "a"}, "A"), content=f"試行{i}") for i in range(3)]
    assert detect_tool_call_loop(_history(*steps), _Cfg()) is None


def test_reasoning_is_ignored():
    steps = []
    for i in range(3):
        step = _step(("Read", {"p": "a"}, "A"))
        step[0].additional_kwargs["reasoning_content"] = f"考え{i}"
        steps.append(step)
    assert detect_tool_call_loop(_history(*steps), _Cfg()) is not None


def test_a_different_response_breaks_the_streak():
    assert detect_tool_call_loop(_history(_err(), _err("y"), _err(), _err()), _Cfg()) is None


def test_trailing_human_message_breaks_the_streak():
    messages = _history(_err(), _err(), _err())
    messages.append(HumanMessage(content="注意メッセージ"))
    assert detect_tool_call_loop(messages, _Cfg()) is None
    # 注意メッセージの後も、また3回繰り返せば検知する
    messages.extend(_err() + _err())
    assert detect_tool_call_loop(messages, _Cfg()) is None
    messages.extend(_err())
    assert detect_tool_call_loop(messages, _Cfg()) is not None


def test_image_followups_are_skipped():
    followup = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])
    steps = [_step(("analyze_image", {"p": "a.png"}, "説明")) + [followup] for _ in range(3)]
    assert detect_tool_call_loop(_history(*steps), _Cfg()) is not None


def test_excluded_tools_are_not_counted():
    steps = [_step(("check_script_job", {"job": "1"}, "running")) for _ in range(5)]
    assert detect_tool_call_loop(_history(*steps), _Cfg()) is None


def test_parallel_calls_are_compared_as_a_set():
    steps = [_step(("Read", {"p": "a"}, "A"), ("Grep", {"q": "b"}, "B")) for _ in range(3)]
    loop = detect_tool_call_loop(_history(*steps), _Cfg())
    assert loop is not None and loop.tool_names == ("Read", "Grep")


@pytest.mark.parametrize("cfg", [_Cfg(tool_loop_guard_enabled=False), _Cfg(tool_loop_guard_max_repeats=1)])
def test_disabled(cfg):
    assert detect_tool_call_loop(_history(_err(), _err(), _err()), cfg) is None


def test_raise_if_tool_call_loop_raises_thinking_loop_subclass():
    with pytest.raises(ToolCallLoopDetected) as info:
        raise_if_tool_call_loop(_history(_err(), _err(), _err()), _Cfg())
    assert isinstance(info.value, ThinkingLoopDetected)
    assert info.value.client_broken is False
    assert "dispatch_agent" in info.value.detail
    raise_if_tool_call_loop(_history(_err()), _Cfg())  # 検知しなければ何もしない


def test_tool_loop_nudge_text_uses_own_messages_and_appends_detail():
    exc = ToolCallLoopDetected("（自動検知: 詳細）")
    assert tool_loop_nudge_text(["注意A", "注意B"], 1, exc) == "注意B\n\n（自動検知: 詳細）"
    # 空なら組み込みの既定文言＋詳細
    text = tool_loop_nudge_text([], 0, exc)
    assert text.startswith("You are repeating the exact same tool call") and text.endswith("（自動検知: 詳細）")


def test_detail_has_no_instruction_only_facts():
    loop = detect_tool_call_loop(_history(_err(), _err(), _err()), _Cfg())
    assert loop.detail().startswith("(Auto-detected: you repeated the exact same tool call (dispatch_agent) in the last 3 attempts.")
    assert "switch" not in loop.detail()


def _with_ids(messages: list) -> list:
    for i, m in enumerate(messages):
        m.id = f"m{i}"
    return messages


def test_loop_reports_the_repeated_tail_for_removal():
    messages = _with_ids(_history(_err("a"), _err(), _err(), _err()))
    loop = detect_tool_call_loop(messages, _Cfg())
    # SystemMessage・HumanMessage・_err("a")（2件）の後ろ、繰り返した3回分（AIMessage+ToolMessage ×3）
    assert loop.start_index == 4
    assert loop.message_ids == tuple(m.id for m in messages[4:])
    assert "were removed" in loop.detail(removed=True)
    assert "were removed" not in loop.detail()


def test_loop_tail_includes_image_followups():
    followup = HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}])
    steps = [_step(("analyze_image", {"p": "a.png"}, "説明")) + [followup.model_copy()] for _ in range(3)]
    messages = _with_ids(_history(*steps))
    loop = detect_tool_call_loop(messages, _Cfg())
    assert loop.start_index == 2 and len(loop.message_ids) == 9


def test_no_ids_to_remove_if_any_message_lacks_an_id():
    messages = _with_ids(_history(_err(), _err(), _err()))
    messages[-1].id = None
    loop = detect_tool_call_loop(messages, _Cfg())
    assert loop is not None and loop.message_ids == ()


def test_raise_if_tool_call_loop_carries_remove_ids():
    messages = _with_ids(_history(_err(), _err(), _err()))
    with pytest.raises(ToolCallLoopDetected) as info:
        raise_if_tool_call_loop(messages, _Cfg())
    assert info.value.remove_ids == tuple(m.id for m in messages[2:])
    assert "were removed" in info.value.detail
    # id が無ければ取り除かず、その旨も書かない
    with pytest.raises(ToolCallLoopDetected) as info:
        raise_if_tool_call_loop(_history(_err(), _err(), _err()), _Cfg())
    assert info.value.remove_ids == () and "were removed" not in info.value.detail


# 2026-10-04 本番ログで write_thread_note の content に漏れていた値
_LEAKED = "</parameter>\n</function>\n</tool_call>\n<tool_call>\n<function=write_thread_note>\n<parameter=topic>\n画像ファイルの一覧"


@pytest.mark.parametrize(
    "args, expected",
    [
        ({"topic": "mdファイルの一覧", "content": _LEAKED}, "content"),
        ({"a": "値\n</parameter>\n<parameter=b>\n値2"}, "a"),
        ({"items": [{"x": _LEAKED}]}, "items"),
        ({"content": "普通の本文"}, None),
        # 書式を説明する文章（閉じタグ単体）は誤検知しない
        ({"content": "Qwenは </parameter> で引数を閉じる"}, None),
        # 開きと閉じが対応した書式の例（文書・テストコードの書き込み）は弾かない
        ({"content": "例:\n<function=Read>\n<parameter=path>\na.txt\n</parameter>\n</function>"}, None),
        # 正しい例の後ろに漏れがあれば検知する
        ({"content": "<parameter=a>\nx\n</parameter>\n<parameter=b>\ny\n</parameter>\n</function>\n</parameter>\n</function>"}, "content"),
        ({}, None),
        (None, None),
    ],
)
def test_find_leaked_tool_markup(args, expected):
    assert find_leaked_tool_markup(args) == expected


def test_tool_node_blocks_leaked_markup_without_running_the_tool():
    from src.tools.tool_node import _guard_leaked_tool_markup

    call = {"name": "write_thread_note", "args": {"topic": "t", "content": _LEAKED}, "id": "c1"}
    blocked = _guard_leaked_tool_markup({"__type": "tool_call_with_context", "tool_call": call, "state": {}})
    message = blocked["messages"][0]
    assert message.status == "error" and message.tool_call_id == "c1"
    assert message.content == leaked_tool_markup_error("write_thread_note", "content")
    ok = {"name": "write_thread_note", "args": {"topic": "t", "content": "x"}, "id": "c2"}
    assert _guard_leaked_tool_markup({"__type": "tool_call_with_context", "tool_call": ok, "state": {}}) is None


# --- サブエージェント（src/subagent.py の run_subagent） ---


class _LoopingModel:
    """常に同じツールを呼ぶ偽モデル。受け取った入力を記録する。"""

    def __init__(self) -> None:
        self.inputs: list[list] = []

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        self.inputs.append(list(messages))
        n = len(self.inputs)
        return AIMessage(content="", tool_calls=[{"name": "dispatch_agent", "args": {"task": "t"}, "id": f"s{n}", "type": "tool_call"}])


class _ErrorTool:
    name = "dispatch_agent"

    async def ainvoke(self, call):
        return ToolMessage(content=ERROR, tool_call_id=call["id"], name="dispatch_agent", status="success")


@pytest.mark.asyncio
async def test_subagent_nudges_then_truncates(monkeypatch):
    from dataclasses import replace

    from src import subagent
    from test_subagent_timeout_retry import _FakeConfig

    model = _LoopingModel()

    async def fake_build_model(config, role):
        return model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)
    config = replace(
        _FakeConfig(),
        thinking_loop_guard_max_retries=1,
        thinking_loop_guard_nudge_messages=["思考ループ用"],
        tool_loop_guard_nudge_messages=("ツールループ用1",),
        tool_loop_guard_enabled=True,
    )
    result = await subagent.run_subagent(task="t", tools=[_ErrorTool()], system_prompt="sp", config=config, max_iterations=20)

    # 3回目の後に注意メッセージ（詳細付き）が入り、また3回繰り返したところで打ち切られる
    nudged = [i for i, msgs in enumerate(model.inputs) if isinstance(msgs[-1], HumanMessage) and "Auto-detected" in msgs[-1].content]
    assert nudged == [3]
    assert model.inputs[3][-1].content.startswith("ツールループ用1")
    # 繰り返した応答は取り除かれ、注意メッセージの直前にはもう無い
    assert not any(isinstance(m, AIMessage) for m in model.inputs[3])
    assert len(model.inputs) == 6
    assert "the exact same tool call (dispatch_agent) kept repeating" in result


@pytest.mark.asyncio
async def test_subagent_leaked_markup_result_has_tool_name():
    from src.subagent import _run_one_tool_call

    call = {"name": "dispatch_agent", "args": {"task": _LEAKED}, "id": "c1", "type": "tool_call"}
    message, _ = await _run_one_tool_call(call, {"dispatch_agent": _ErrorTool()})
    assert message.name == "dispatch_agent" and message.status == "error"
    assert message.content == leaked_tool_markup_error("dispatch_agent", "task")


@pytest.mark.asyncio
async def test_subagent_tool_exception_result_has_tool_name_and_emits_on_tool_error():
    """ツール内の例外はエラーの ToolMessage になり、on_tool_end ではなく on_tool_error が届く
    （app.py はこれで「実行中」のStepを閉じる）。"""
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tools import tool

    from src.subagent import _run_one_tool_call

    @tool
    def boom(query: str) -> str:
        """Always fails."""
        raise PermissionError(13, "Permission denied")

    call = {"name": "boom", "args": {"query": "q"}, "id": "c1", "type": "tool_call"}

    async def run(_):
        message, _ = await _run_one_tool_call(call, {"boom": boom})
        return message

    kinds = []
    message = None
    async for event in RunnableLambda(run).astream_events(None, version="v2"):
        if event["name"] == "boom":
            kinds.append(event["event"])
        if event["event"] == "on_chain_end" and event["name"] == "run":
            message = event["data"]["output"]
    assert kinds == ["on_tool_start", "on_tool_error"]
    assert message.name == "boom" and message.status == "error"
    assert message.content.startswith("Error: tool execution failed:")


@pytest.mark.asyncio
async def test_prebuilt_hook_detects_loop_even_when_token_guard_appends(monkeypatch):
    """トークンガードが末尾に HumanMessage を足す状況でも、ループ検知は効く。"""
    from langchain_core.messages import HumanMessage as _Human

    from src import graph as graph_mod

    captured = {}

    def fake_create_react_agent(model, tools, prompt, pre_model_hook, checkpointer):
        captured["hook"] = pre_model_hook
        return object()

    async def fake_build_model(config, role, wait_when_busy=True):
        return object()

    monkeypatch.setattr(graph_mod, "create_react_agent", fake_create_react_agent)
    monkeypatch.setattr(graph_mod, "build_model", fake_build_model)
    monkeypatch.setattr(graph_mod, "enable_thinking_control", lambda m, c, r: m)
    monkeypatch.setattr(graph_mod, "filter_main_agent_tools", lambda tools, c: [])
    monkeypatch.setattr(graph_mod, "ImageAwareToolNode", lambda tools: None)
    monkeypatch.setattr(graph_mod, "maybe_append_token_guard", lambda m, c: [*m, _Human(content="引継ぎ")])

    from types import SimpleNamespace

    cfg = SimpleNamespace(
        context_trim_enabled=False,
        graph_token_guard_enabled=True,
        context_compaction_enabled=False,
        context_compaction_pre_note_threshold=0,
        tool_loop_guard_enabled=True,
        tool_loop_guard_max_repeats=3,
        tool_loop_guard_exclude_tools=[],
    )
    await graph_mod._build_prebuilt_graph(cfg, "sp", None)
    with pytest.raises(ToolCallLoopDetected):
        await captured["hook"]({"messages": _history(_err(), _err(), _err())}, {})
