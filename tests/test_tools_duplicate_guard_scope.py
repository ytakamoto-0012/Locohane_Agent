"""重複ガードの記録先が実行コンテキストごとに分離されることの回帰テスト。

`analyze_image` の重複ガードは当初 `cl.user_session` の単一集合だけを見ており、
[file_tools_duplicate_guard].carry_over_to_main の設定を参照していなかった。
このため「サブエージェントAが読んだ画像は、他のサブエージェントもメイン
エージェントも二度と読めない」状態になっていた。サブエージェントの会話履歴は
委譲元にも他のサブエージェントにも共有されず、返るのは最終回答テキストだけ
なので、1件目のサブエージェントが読んで返しきれなかった画像を誰も読み直せず、
大量画像処理が詰む実例が eval（レシピ画像297枚ケース）で観測された。

carry_over_to_main=false のとき、メイン／各サブエージェント実行が互いの
重複判定に影響しないことを検証する。
"""

import asyncio
from dataclasses import dataclass

import pytest

from src import tools


@dataclass
class _Cfg:
    """重複ガードの参照する設定値だけを持つスタブ。"""

    file_tools_duplicate_guard_enabled: bool = True
    file_tools_duplicate_guard_max_calls: int = 1
    file_tools_duplicate_guard_carry_over_to_main: bool = False


class _FakeUserSession:
    """dispatch_agent の finally が main_agent_tool_guard カウンタをリセットする際に
    触れる cl.user_session を、Chainlit実行コンテキスト無しでも動くよう差し替える。
    """

    def __init__(self):
        self._data: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _FakeMessage:
    """tools.cl.Message の差し替え。dispatch_agent が内部で起動する進捗push
    タスク（_push_dispatch_agent_progress）が cl.Message(...).send() を呼ぶため、
    Chainlit実行コンテキスト無しでも動くよう差し替える。
    """

    def __init__(self, content: str = "", **kwargs) -> None:
        self.content = content

    async def send(self) -> None:
        pass

    async def update(self) -> None:
        pass


def _setup(monkeypatch, carry_over: bool) -> None:
    monkeypatch.setattr(
        tools._state, "_LLM_CONFIG", _Cfg(file_tools_duplicate_guard_carry_over_to_main=carry_over)
    )
    monkeypatch.setattr(
        tools._state,
        "_AGENT_TYPES",
        {"explore": tools._state.ResolvedAgentType(description="", system_prompt="", tools=[])},
    )
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    monkeypatch.setattr(tools.cl, "Message", _FakeMessage)
    monkeypatch.setattr(tools._dispatch_agent_job, "_DISPATCH_AGENT_JOBS", {})


def test_session_key_is_shared_when_carry_over_enabled(monkeypatch) -> None:
    _setup(monkeypatch, carry_over=True)
    token = tools._state._SUBAGENT_RUN_ID.set("run-1")
    try:
        assert tools._state._duplicate_guard_session_key("k") == "k"
    finally:
        tools._state._SUBAGENT_RUN_ID.reset(token)


def test_session_key_is_isolated_per_subagent_run(monkeypatch) -> None:
    _setup(monkeypatch, carry_over=False)
    # サブエージェント外（メインエージェント）は素のキー。
    assert tools._state._duplicate_guard_session_key("k") == "k"

    token_a = tools._state._SUBAGENT_RUN_ID.set("run-a")
    try:
        key_a = tools._state._duplicate_guard_session_key("k")
    finally:
        tools._state._SUBAGENT_RUN_ID.reset(token_a)
    token_b = tools._state._SUBAGENT_RUN_ID.set("run-b")
    try:
        key_b = tools._state._duplicate_guard_session_key("k")
    finally:
        tools._state._SUBAGENT_RUN_ID.reset(token_b)

    assert key_a != "k"
    assert key_b != "k"
    assert key_a != key_b


@pytest.mark.asyncio
async def test_dispatch_agent_assigns_unique_run_id_per_call(monkeypatch) -> None:
    _setup(monkeypatch, carry_over=False)
    seen: list[str] = []

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        # サブエージェント内から見えるキーを記録する。
        seen.append(tools._state._duplicate_guard_session_key("analyze_image_call_signatures"))
        await asyncio.sleep(0)
        return "ok"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    await tools.dispatch_agent.ainvoke(
        {"name": "dispatch_agent", "args": {"task": "a", "agent_type": "explore"}, "id": "test-tc-a", "type": "tool_call"}
    )
    await tools.dispatch_agent.ainvoke(
        {"name": "dispatch_agent", "args": {"task": "b", "agent_type": "explore"}, "id": "test-tc-b", "type": "tool_call"}
    )

    assert len(seen) == 2
    assert seen[0] != seen[1], "dispatch_agent 呼び出しごとに別の記録先になること"
    # 実行後はメインエージェントのコンテキストへ戻る（ContextVar がリセットされる）。
    assert tools._state._duplicate_guard_session_key("analyze_image_call_signatures") == (
        "analyze_image_call_signatures"
    )
