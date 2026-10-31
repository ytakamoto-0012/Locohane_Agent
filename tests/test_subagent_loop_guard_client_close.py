"""ThinkingLoopDetected発生時、旧モデルのhttpx.AsyncClientが強制クローズされることの回帰テスト。

背景（2026-08-26 ユーザー報告）: サブエージェント実行中に思考ループガードが発火すると、
「接続は切れるがLLMの生成が止まらない」不具合があった。src/context_compaction.py の
maybe_compact は既に aclose_model_client() で旧クライアントを無条件強制クローズしていたが、
src/subagent.py の _invoke_with_loop_retry（dispatch_agent本体のReActループが使う方）には
同じ処理が欠けていた。build_model() で新しいクライアントに差し替えるだけでは、
ChatLlamaCpp._astream_guarded の finally節でのagen.aclose()（5秒タイムアウト）が
失敗・タイムアウトした場合に、旧クライアントの接続・llama-server側の生成が
生きたまま残り続けうる。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src import subagent
from src.llm import ThinkingLoopDetected


class _FakeConfig:
    thinking_loop_guard_max_retries = 2
    thinking_loop_guard_nudge_messages: list[str] = []


class _FakeModel:
    """id で個体識別できる、build_model() が呼ばれるたびに新しく作られるモデル。

    失敗するかどうかは全インスタンス共有の state["calls"]（累計呼び出し回数）で
    判定する。「モデルを再構築してから同じ内容で再試行する」実際の挙動
    （失敗したモデルは二度と使われない）を模す。
    """

    _next_id = 0

    def __init__(self, state: dict, fail_until: int, final_message: AIMessage) -> None:
        self.id = _FakeModel._next_id
        _FakeModel._next_id += 1
        self._state = state
        self._fail_until = fail_until
        self._final_message = final_message

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        self._state["calls"] += 1
        if self._state["calls"] <= self._fail_until:
            raise ThinkingLoopDetected("反復ループ", snippet="同じ文の繰り返し")
        return self._final_message


_FINAL = AIMessage(content="完了しました")


def _install_fake_build_model(monkeypatch, state: dict, fail_until: int) -> list[_FakeModel]:
    created: list[_FakeModel] = []

    async def _fake_build_model(config, role):
        model = _FakeModel(state, fail_until, _FINAL)
        created.append(model)
        return model

    monkeypatch.setattr(subagent, "build_model", _fake_build_model)
    return created


@pytest.mark.asyncio
async def test_loop_detected_closes_old_client_before_retry(monkeypatch) -> None:
    """ループ検知でリトライする際、次のモデルへ差し替える前に必ず旧モデルのクライアントを閉じる。"""
    closed: list[int] = []

    async def fake_aclose_model_client(model):
        closed.append(model.id)

    monkeypatch.setattr(subagent, "aclose_model_client", fake_aclose_model_client)
    state = {"calls": 0}
    # 累計2回失敗、3回目で成功する。
    created = _install_fake_build_model(monkeypatch, state, fail_until=2)

    initial_model = _FakeModel(state, fail_until=2, final_message=_FINAL)
    response, final_model = await subagent._invoke_with_loop_retry(
        initial_model, [HumanMessage(content="task")], _FakeConfig(), tools=[]
    )

    assert response.content == "完了しました"
    # initial_model(1回目、失敗) → created[0](2回目、失敗) → created[1](3回目、成功)。
    assert final_model is created[1]
    # 失敗した2個（initial_model, created[0]）のクライアントが、次のモデルへ
    # 差し替える前にそれぞれクローズされていること。
    assert closed == [initial_model.id, created[0].id]


@pytest.mark.asyncio
async def test_loop_detected_closes_old_client_even_when_retries_exhausted(monkeypatch) -> None:
    """リトライ予算を使い切って諦める（raise）最後の1回も、後始末を必ず行う。

    このガードが無いと、リトライ予算を使い切った最後の1回だけ後始末されずに
    終わり、ストリームの後始末自体が失敗してllama-server側の生成が終わらない
    まま残り続ける（context_compaction.py の maybe_compact と同じ理由）。
    """
    closed: list[int] = []

    async def fake_aclose_model_client(model):
        closed.append(model.id)

    monkeypatch.setattr(subagent, "aclose_model_client", fake_aclose_model_client)
    state = {"calls": 0}
    # 常に失敗し続ける（呼び出し回数の上限を十分大きくする）。
    created = _install_fake_build_model(monkeypatch, state, fail_until=99)

    config = _FakeConfig()
    config.thinking_loop_guard_max_retries = 1  # 初回 + リトライ1回 = 合計2回まで許容
    initial_model = _FakeModel(state, fail_until=99, final_message=_FINAL)

    with pytest.raises(ThinkingLoopDetected):
        await subagent._invoke_with_loop_retry(initial_model, [HumanMessage(content="task")], config, tools=[])

    # 初回(initial_model) + リトライ1回目(created[0]) の合計2個分すべて
    # クローズされていること（最後に諦めた1回も含む）。
    assert closed == [initial_model.id, created[0].id]
