"""maybe_compact() の接続エラー・ループ検知リトライの回帰テスト。

背景: 要約LLM呼び出し（maybe_compact 内の model.ainvoke）が通信エラー
（LLM_CONNECTION_ERRORS）やループ検知（ThinkingLoopDetected）で失敗した場合、
従来は broad except で握りつぶし、接続の切り替え・リトライを一切行わずに
今回の圧縮をスキップするだけだった。

これにより2つの問題があった:
1. 接続エラー時に app.py の on_message / src/subagent.py の run_subagent
   本編と異なり、接続先を切り替えて再試行する仕組みが無かった
   （ユーザー報告）。
2. ThinkingLoopDetected発生時、ストリームの後始末(aclose)自体が失敗・
   タイムアウトした場合（client_broken=True）でも接続を強制クローズせず
   放置していたため、llama-server側の生成が終わらないまま次のLLM呼び出しが
   応答ヘッダー待ちでハングしうる疑いがあった（ユーザー報告）。

本テストは、両方のケースで接続の切り替え/強制クローズを伴うリトライが
実際に発動し、予算を使い切った場合のみ従来通り None を返すことを確認する。
"""

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src import context_compaction, tools
from src.context_compaction import maybe_compact
from src.llm import ThinkingLoopDetected


@dataclass
class _FakeConfig:
    context_compaction_keep_recent_iterations: int
    context_compaction_prompt_path: Path
    context_trim_truncated_max_chars: int
    context_compaction_summary_source_max_chars: int
    context_compaction_skill_reattach_max_chars_per_skill: int
    context_compaction_skill_reattach_total_max_chars: int
    graph_connection_error_max_retries: int = 3
    subagent_background_llm_timeout_max_retries: int = 3
    thinking_loop_guard_max_retries: int = 2


class _FakeUserSession:
    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def _messages() -> list:
    return [
        HumanMessage(content="q1"),
        AIMessage(content="ok1"),
        HumanMessage(content="q2"),
        AIMessage(content="ok2"),
    ]


def _config(tmp_path: Path, **overrides) -> _FakeConfig:
    prompt_path = tmp_path / "compaction_prompt.md"
    prompt_path.write_text("以下を要約してください", encoding="utf-8")
    kwargs = dict(
        context_compaction_keep_recent_iterations=1,
        context_compaction_prompt_path=prompt_path,
        context_trim_truncated_max_chars=2000,
        context_compaction_summary_source_max_chars=2000,
        context_compaction_skill_reattach_max_chars_per_skill=12000,
        context_compaction_skill_reattach_total_max_chars=36000,
    )
    kwargs.update(overrides)
    return _FakeConfig(**kwargs)


class _ScriptedModel:
    """ainvoke() 呼び出しごとに指定した応答/例外を順番に返す。呼び出し履歴も記録する。"""

    def __init__(self, script: list):
        self._script = list(script)
        self.invocations: list[list] = []

    async def ainvoke(self, messages):
        self.invocations.append(list(messages))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_plan(monkeypatch):
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({}))


@pytest.mark.asyncio
async def test_connection_error_retries_with_endpoint_switch_then_succeeds(monkeypatch, tmp_path) -> None:
    script = [
        httpx.TransportError("connect failed"),
        httpx.TransportError("connect failed again"),
        AIMessage(content="要約結果"),
    ]
    models = [_ScriptedModel([script[0]]), _ScriptedModel([script[1]]), _ScriptedModel([script[2]])]
    build_calls = {"n": 0}
    mark_failed_calls = []

    async def fake_build_model(config, role):
        idx = build_calls["n"]
        build_calls["n"] += 1
        # 最初の呼び出し分（idx=0）は maybe_compact に直接渡す初期モデルなので
        # build_model からは呼ばれない。リトライ2回分（idx=1,2）を返す。
        return models[idx + 1]

    monkeypatch.setattr(context_compaction, "build_model", fake_build_model)
    monkeypatch.setattr(context_compaction, "mark_last_endpoint_failed", lambda role: mark_failed_calls.append(role))

    result = await maybe_compact(_messages(), models[0], _config(tmp_path), role="main")

    assert result is not None
    assert result[0].content.startswith("[Automatic summary:")
    assert "要約結果" in result[0].content
    # 2回とも通信エラー → 2回とも接続先クールダウン + モデル再構築が発動した。
    assert mark_failed_calls == ["main", "main"]
    assert build_calls["n"] == 2


@pytest.mark.asyncio
async def test_connection_error_exhausts_retry_budget_and_skips_compaction(monkeypatch, tmp_path) -> None:
    model = _ScriptedModel([httpx.TransportError("down")] * 10)

    async def fake_build_model(config, role):
        return model

    monkeypatch.setattr(context_compaction, "build_model", fake_build_model)
    monkeypatch.setattr(context_compaction, "mark_last_endpoint_failed", lambda role: None)

    result = await maybe_compact(
        _messages(), model, _config(tmp_path, graph_connection_error_max_retries=2), role="main"
    )

    assert result is None
    # 初回 + リトライ2回 = 3回試行してから諦める。
    assert len(model.invocations) == 3


@pytest.mark.asyncio
async def test_loop_detected_force_closes_client_and_retries_then_succeeds(monkeypatch, tmp_path) -> None:
    loop_exc = ThinkingLoopDetected("loop", snippet="ああああ", client_broken=True)
    script = [loop_exc, AIMessage(content="要約結果2")]
    models = [_ScriptedModel([script[0]]), _ScriptedModel([script[1]])]
    closed_models = []
    build_calls = {"n": 0}

    async def fake_build_model(config, role):
        build_calls["n"] += 1
        return models[build_calls["n"]]

    async def fake_aclose_model_client(model):
        closed_models.append(model)

    monkeypatch.setattr(context_compaction, "build_model", fake_build_model)
    monkeypatch.setattr(context_compaction, "aclose_model_client", fake_aclose_model_client)

    result = await maybe_compact(_messages(), models[0], _config(tmp_path), role="sub")

    assert result is not None
    assert "要約結果2" in result[0].content
    # ループ検知した壊れた可能性のあるモデル（client_broken=True）が
    # 強制クローズされてから、新しいモデルへ差し替わってリトライした。
    assert closed_models == [models[0]]
    assert build_calls["n"] == 1
    # リトライ時は要約対象プロンプトに加え、ループ注意メッセージが追加される。
    assert len(models[1].invocations[0]) == 2


@pytest.mark.asyncio
async def test_loop_detected_exhausts_retry_budget_and_skips_compaction(monkeypatch, tmp_path) -> None:
    model = _ScriptedModel([ThinkingLoopDetected("loop", snippet="x", client_broken=False) for _ in range(10)])
    closed_models = []

    async def fake_aclose_model_client(m):
        closed_models.append(m)

    async def fake_build_model(config, role):
        return model

    monkeypatch.setattr(context_compaction, "build_model", fake_build_model)
    monkeypatch.setattr(context_compaction, "aclose_model_client", fake_aclose_model_client)

    result = await maybe_compact(
        _messages(), model, _config(tmp_path, thinking_loop_guard_max_retries=1), role="sub"
    )

    assert result is None
    # 初回 + リトライ1回 = 2回試行してから諦める。client_broken=Falseでも
    # 無条件でクローズを試みる（_invoke_with_loop_retry と同じ方針）。
    assert len(closed_models) == 2
