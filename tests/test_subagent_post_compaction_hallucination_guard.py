"""会話圧縮/トークン閾値注意メッセージ注入の直後1手に限り、tool_calls無しの
極端に短い最終応答を無検査で受理しない回帰テスト（src/subagent.py）。

背景（2026-08-23 issue/20260823_021924）: excel-vbaマクロブック作成タスク中、
サブエージェントの会話履歴圧縮が成功した直後の1手で、モデルが実在しない
「1+1=?」という質問への回答（"1. 1+1=**2**..."）を幻覚した。`tool_calls`が
空だったため`run_subagent`はこれを「正常終了」として最終回答扱いで返し、
それまでの実際の作業結果（VBAデータ移行）が丸ごと握りつぶされた。

この対策として、会話圧縮成功直後・トークン閾値注意メッセージ注入直後の
1手に限り、`tool_calls`が空かつ本文が
`_POST_COMPACTION_SUSPICIOUS_RESPONSE_MIN_LENGTH`文字未満の最終応答を
「疑わしい」とみなし、1回だけやり直しを促して再試行するようにした
（`hallucination_retry_used`により2回以上は再試行しない）。
"""

import pytest
from dataclasses import dataclass
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from src import subagent


@dataclass
class _FakeConfig:
    """run_subagent が実際に参照するのは context_trim_subagent_*/
    context_compaction_subagent_* 側（[context_trim.subagent]/
    [context_compaction.subagent] 導入後の src/subagent.py の実装。config.py
    docstring参照）。run_subagent 内の _subagent_compaction_config が
    dataclasses.replace() を使うため dataclass にする必要があり、かつ
    replace() の changes には main側 context_compaction_* のフィールド名が
    そのまま使われるため main側フィールドも定義しておく必要がある。
    """

    thinking_loop_guard_max_retries: int = 0
    # 同じツール呼び出しのループ検知（src/tool_loop_guard.py）。このテストの対象外のため無効。
    tool_loop_guard_enabled: bool = False
    tool_loop_guard_max_repeats: int = 3
    tool_loop_guard_exclude_tools: tuple = ()
    tool_loop_guard_nudge_messages: tuple = ()
    subagent_empty_response_max_retries: int = 0
    subagent_token_guard_enabled: bool = False
    subagent_token_guard_soft_threshold: int = 100
    subagent_token_guard_hard_threshold: int = 200
    subagent_token_guard_soft_warning_text: str = "[閾値注意]"
    track_token_usage: bool = False
    context_trim_subagent_enabled: bool = False
    context_compaction_enabled: bool = False
    context_compaction_token_threshold: int = 0
    context_compaction_single_request_token_threshold: int = 0
    context_compaction_keep_recent_iterations: int = 0
    context_compaction_min_messages_to_compact: int = 0
    context_compaction_prompt_path: str | None = None
    context_compaction_summary_source_max_chars: int = 0
    context_compaction_skill_reattach_max_chars_per_skill: int = 12000
    context_compaction_skill_reattach_total_max_chars: int = 36000
    context_compaction_pre_note_threshold: int = 0
    context_compaction_pre_note_warning_text: str = ""
    context_compaction_require_note_max_skips: int = 0
    context_compaction_subagent_enabled: bool = False
    context_compaction_subagent_token_threshold: int = 0
    context_compaction_subagent_single_request_token_threshold: int = 0
    context_compaction_subagent_keep_recent_iterations: int = 0
    context_compaction_subagent_min_messages_to_compact: int = 0
    context_compaction_subagent_prompt_path: str | None = None
    context_compaction_subagent_summary_source_max_chars: int = 0
    context_compaction_subagent_pre_note_threshold: int = 0
    context_compaction_subagent_pre_note_warning_text: str = ""
    context_compaction_subagent_require_note_max_skips: int = 0


@tool
def dummy_tool() -> str:
    """テスト用の何もしないツール。"""
    return "ok"


class _ScriptedModel:
    """あらかじめ用意したAIMessageを呼び出し順に返す固定シナリオ用モデル。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        return self._responses[self.calls - 1]


def _make_compaction_config() -> _FakeConfig:
    config = _FakeConfig()
    config.context_compaction_subagent_enabled = True
    config.context_compaction_subagent_keep_recent_iterations = 3
    config.track_token_usage = True
    return config


@pytest.mark.asyncio
async def test_short_response_immediately_after_compaction_triggers_one_retry(monkeypatch) -> None:
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "dummy_tool", "args": {}, "id": "call-1"},
                {"name": "write_thread_note", "args": {"topic": "t", "content": "c"}, "id": "call-2"},
            ],
        ),
        AIMessage(content="1+1=2"),  # 圧縮直後の幻覚（短い・タスクと無関係）
        AIMessage(content="これまでの調査結果を踏まえてタスクを完了しました。詳細は以上の通りです。"),
    ]
    fake_model = _ScriptedModel(responses)
    config = _make_compaction_config()
    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    call_state = {"n": 0}

    def fake_should_compact(cumulative_usage, last_usage, message_count, config):
        call_state["n"] += 1
        return call_state["n"] == 1  # 最初のツール実行直後にだけ圧縮を発火させる

    async def fake_maybe_compact(messages, model, config, *, role="sub", pinned_instruction=None):
        return [HumanMessage(content="[要約]圧縮済み")]

    monkeypatch.setattr(subagent, "should_compact", fake_should_compact)
    monkeypatch.setattr(subagent, "maybe_compact", fake_maybe_compact)

    result = await subagent.run_subagent(
        task="t", tools=[dummy_tool], system_prompt="sp", config=config, max_iterations=5
    )

    assert result == responses[2].content
    assert fake_model.calls == 3  # tool_calls -> 幻覚(棄却・再試行) -> 正常な最終回答


@pytest.mark.asyncio
async def test_second_short_response_is_accepted_without_infinite_retry(monkeypatch) -> None:
    """1回リトライしても短いままなら、無限リトライせずそのまま最終回答として受理する。"""
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "dummy_tool", "args": {}, "id": "call-1"},
                {"name": "write_thread_note", "args": {"topic": "t", "content": "c"}, "id": "call-2"},
            ],
        ),
        AIMessage(content="1+1=2"),
        AIMessage(content="はい。"),  # リトライ後も短いが、2回目なので受理される
    ]
    fake_model = _ScriptedModel(responses)
    config = _make_compaction_config()
    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    call_state = {"n": 0}

    def fake_should_compact(cumulative_usage, last_usage, message_count, config):
        call_state["n"] += 1
        return call_state["n"] == 1

    async def fake_maybe_compact(messages, model, config, *, role="sub", pinned_instruction=None):
        return [HumanMessage(content="[要約]圧縮済み")]

    monkeypatch.setattr(subagent, "should_compact", fake_should_compact)
    monkeypatch.setattr(subagent, "maybe_compact", fake_maybe_compact)

    result = await subagent.run_subagent(
        task="t", tools=[dummy_tool], system_prompt="sp", config=config, max_iterations=5
    )

    assert result == "はい。"
    assert fake_model.calls == 3


@pytest.mark.asyncio
async def test_short_response_without_prior_compaction_is_accepted_immediately(monkeypatch) -> None:
    """圧縮/トークン閾値注意が一度も起きていない通常時は、同じ短い応答でも
    即座に最終回答として受理する（誤検知しない）。"""
    responses = [AIMessage(content="はい、完了です。")]
    fake_model = _ScriptedModel(responses)
    config = _FakeConfig()  # context_compaction_enabled=False（既定）
    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    result = await subagent.run_subagent(
        task="t", tools=[dummy_tool], system_prompt="sp", config=config, max_iterations=5
    )

    assert result == "はい、完了です。"
    assert fake_model.calls == 1


@pytest.mark.asyncio
async def test_long_response_immediately_after_compaction_is_accepted_without_retry(monkeypatch) -> None:
    """圧縮直後でも、十分な長さの妥当な最終応答ならそのまま受理する（誤検知しない）。"""
    long_answer = "調査の結果、以下の3点が判明しました。" * 5  # 十分な長さ
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "dummy_tool", "args": {}, "id": "call-1"},
                {"name": "write_thread_note", "args": {"topic": "t", "content": "c"}, "id": "call-2"},
            ],
        ),
        AIMessage(content=long_answer),
    ]
    fake_model = _ScriptedModel(responses)
    config = _make_compaction_config()
    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    call_state = {"n": 0}

    def fake_should_compact(cumulative_usage, last_usage, message_count, config):
        call_state["n"] += 1
        return call_state["n"] == 1

    async def fake_maybe_compact(messages, model, config, *, role="sub", pinned_instruction=None):
        return [HumanMessage(content="[要約]圧縮済み")]

    monkeypatch.setattr(subagent, "should_compact", fake_should_compact)
    monkeypatch.setattr(subagent, "maybe_compact", fake_maybe_compact)

    result = await subagent.run_subagent(
        task="t", tools=[dummy_tool], system_prompt="sp", config=config, max_iterations=5
    )

    assert result == long_answer
    assert fake_model.calls == 2
