"""run_subagent の on_iteration コールバック・llm_timeout_max_retries の回帰テスト。

背景: dispatch_agent が「1回のLLM呼び出しタイムアウトでジョブ全体を諦めて
しまう」既存の run_subagent の挙動（TimeoutError/LLM_CONNECTION_ERRORS を
検知したら即座に打ち切りメッセージを返す、src/subagent.py の run_subagent 内
except 節）に対して耐性を持たせるため、_invoke_with_timeout_retry を新設した。
llm_timeout_max_retries=0（既定・明示的に指定しない呼び出し元向けの安全側
デフォルト）では即座に打ち切ることを固定化しつつ、正の値を渡した場合に
モデル再構築・リトライで復旧できることを確認する。

本番incident・2026-08-21: except節が元々 (TimeoutError, httpx.TimeoutException)
という狭いタプルで、openai SDK が httpx の read timeout を独自の
openai.APITimeoutError（TimeoutError にも httpx.TimeoutException にも属さない）
へラップして再送出するケースを取りこぼしていた。このため
background_llm_timeout_max_retries を設定していても実際には一度もリトライが
発動せず、dispatch_agent が生のトレースバックのまま失敗していた
（下記 test_recovers_from_openai_wrapped_timeout がこの実際の例外型での
回帰を防ぐ）。
"""

import httpx
import openai
import pytest
from dataclasses import dataclass, field
from langchain_core.messages import AIMessage

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
    thinking_loop_guard_nudge_messages: list = field(default_factory=lambda: ["繰り返しを避けてください"])
    subagent_empty_response_max_retries: int = 0
    subagent_token_guard_enabled: bool = False
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


def _default_exc() -> Exception:
    return TimeoutError("llama-server busy")


def _openai_api_timeout_exc() -> Exception:
    """openai SDK が httpx の read timeout をラップして送出する実際の例外型。"""
    return openai.APITimeoutError(request=httpx.Request("POST", "http://localhost/v1/chat/completions"))


class _FakeModel:
    """ainvoke() が state["calls"] を進めながら、失敗回数分だけ例外を送出する。"""

    def __init__(self, state: dict, fail_times: int, final_message: AIMessage, make_exc=_default_exc) -> None:
        self._state = state
        self._fail_times = fail_times
        self._final_message = final_message
        self._make_exc = make_exc

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        self._state["calls"] += 1
        if self._state["calls"] <= self._fail_times:
            raise self._make_exc()
        return self._final_message


def _make_fake_build_model(fail_times: int, final_message: AIMessage, make_exc=_default_exc):
    """build_model() の差し替え。呼ばれるたびに新しい _FakeModel を返すが、
    失敗判定は state["calls"]（全体の呼び出し回数）で共有するため、
    「モデルを再構築してから再試行する」実際の挙動を模せる。
    """
    state = {"calls": 0}

    async def _fake_build_model(config, role):
        return _FakeModel(state, fail_times, final_message, make_exc)

    return _fake_build_model, state


_FINAL = AIMessage(content="完了しました")


@pytest.mark.asyncio
async def test_on_iteration_called_with_iteration_and_max_iterations(monkeypatch) -> None:
    fake_build_model, _ = _make_fake_build_model(fail_times=0, final_message=_FINAL)
    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    calls = []
    result = await subagent.run_subagent(
        task="t",
        tools=[],
        system_prompt="sp",
        config=_FakeConfig(),
        max_iterations=5,
        on_iteration=lambda iteration, max_iterations: calls.append((iteration, max_iterations)),
    )

    assert result == "完了しました"
    assert calls == [(1, 5)]


@pytest.mark.asyncio
async def test_on_activity_reports_thinking_before_llm_call(monkeypatch) -> None:
    """on_activity には UI のStepバッジと同じ表示名（"思考中"）が渡る。"""
    fake_build_model, _ = _make_fake_build_model(fail_times=0, final_message=_FINAL)
    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    activities = []
    await subagent.run_subagent(
        task="t",
        tools=[],
        system_prompt="sp",
        config=_FakeConfig(),
        max_iterations=5,
        on_activity=activities.append,
    )

    assert activities == [("thinking",)]


class _ScriptedModel:
    """ainvoke() のたびに responses を先頭から返す（before_each があれば先に呼ぶ）。"""

    def __init__(self, responses: list, before_each=None) -> None:
        self._responses = responses
        self._before_each = before_each

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        if self._before_each is not None:
            self._before_each()
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_on_activity_uses_step_badge_labels_for_tools(monkeypatch) -> None:
    """ツールの表示名は UI のStepバッジと同じ対応表（src/tool_labels.py）から引き、並列の同名呼び出しは1つにまとめる。"""
    tool_calls = [
        {"name": "run_script", "args": {}, "id": "1", "type": "tool_call"},
        {"name": "run_script", "args": {}, "id": "2", "type": "tool_call"},
        {"name": "Grep", "args": {}, "id": "3", "type": "tool_call"},
    ]
    model = _ScriptedModel([AIMessage(content="", tool_calls=tool_calls), _FINAL])

    async def _fake_build_model(config, role):
        return model

    monkeypatch.setattr(subagent, "build_model", _fake_build_model)

    activities = []
    await subagent.run_subagent(
        task="t", tools=[], system_prompt="sp", config=_FakeConfig(), max_iterations=5, on_activity=activities.append
    )

    assert activities == [("thinking",), ("tools", "run_script", "Grep"), ("thinking",)]


@pytest.mark.asyncio
async def test_on_activity_reports_llm_wait_and_restores(monkeypatch) -> None:
    """LLMの空き待ちの間は "LLMの空き待ち"、抜けたら元の表示に戻す。run_subagent を抜けたら通知先は外れる。"""
    from src.llm import routing

    def _wait_then_acquire():
        routing.notify_llm_wait(True)
        routing.notify_llm_wait(True)
        routing.notify_llm_wait(False)

    model = _ScriptedModel([_FINAL], before_each=_wait_then_acquire)

    async def _fake_build_model(config, role):
        # 接続先の選択は待たなかった（False だけ届く）場合、表示は変えない。
        routing.notify_llm_wait(False)
        return model

    monkeypatch.setattr(subagent, "build_model", _fake_build_model)

    activities = []
    await subagent.run_subagent(
        task="t", tools=[], system_prompt="sp", config=_FakeConfig(), max_iterations=5, on_activity=activities.append
    )

    assert activities == [("thinking",), ("llm_wait",), ("thinking",)]
    routing.notify_llm_wait(True)
    assert activities == [("thinking",), ("llm_wait",), ("thinking",)]


@pytest.mark.asyncio
async def test_default_zero_retries_truncates_immediately_on_timeout(monkeypatch) -> None:
    """llm_timeout_max_retries 既定値0（明示的に指定しない呼び出し元向けの
    安全側デフォルト）では、初回のタイムアウトで即座に打ち切りメッセージを
    返す（回帰確認）。
    """
    fake_build_model, state = _make_fake_build_model(fail_times=1, final_message=_FINAL)
    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    result = await subagent.run_subagent(
        task="t",
        tools=[],
        system_prompt="sp",
        config=_FakeConfig(),
        max_iterations=5,
    )

    assert subagent.is_truncated_result(result)
    assert "timed out" in result
    assert state["calls"] == 1  # リトライしていない


@pytest.mark.asyncio
async def test_positive_retries_recovers_from_transient_timeout(monkeypatch) -> None:
    fake_build_model, state = _make_fake_build_model(fail_times=2, final_message=_FINAL)
    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    result = await subagent.run_subagent(
        task="t",
        tools=[],
        system_prompt="sp",
        config=_FakeConfig(),
        max_iterations=5,
        llm_timeout_max_retries=3,
    )

    assert result == "完了しました"
    assert not subagent.is_truncated_result(result)
    assert state["calls"] == 3  # 2回失敗 + 3回目で成功


@pytest.mark.asyncio
async def test_retries_exhausted_still_truncates(monkeypatch) -> None:
    fake_build_model, state = _make_fake_build_model(fail_times=99, final_message=_FINAL)
    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    result = await subagent.run_subagent(
        task="t",
        tools=[],
        system_prompt="sp",
        config=_FakeConfig(),
        max_iterations=5,
        llm_timeout_max_retries=2,
    )

    assert subagent.is_truncated_result(result)
    assert state["calls"] == 3  # 初回 + リトライ2回 = 3回とも失敗


@pytest.mark.asyncio
async def test_recovers_from_openai_wrapped_timeout(monkeypatch) -> None:
    """本番incident・2026-08-21の回帰確認。

    openai SDK が httpx の read timeout を openai.APITimeoutError へラップして
    送出するケース（TimeoutError にも httpx.TimeoutException にも属さない）でも、
    test_positive_retries_recovers_from_transient_timeout と同様にモデル再構築・
    リトライで復旧できることを確認する。
    """
    fake_build_model, state = _make_fake_build_model(
        fail_times=2, final_message=_FINAL, make_exc=_openai_api_timeout_exc
    )
    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    result = await subagent.run_subagent(
        task="t",
        tools=[],
        system_prompt="sp",
        config=_FakeConfig(),
        max_iterations=5,
        llm_timeout_max_retries=3,
    )

    assert result == "完了しました"
    assert not subagent.is_truncated_result(result)
    assert state["calls"] == 3  # 2回失敗 + 3回目で成功


@pytest.mark.asyncio
async def test_thinking_loop_detected_truncates_like_timeout_instead_of_raising(monkeypatch) -> None:
    """ThinkingLoopDetectedのリトライ上限到達を、通信エラーと同じ「打ち切りメッセージとして
    正常return」扱いにする回帰テスト。

    以前は run_subagent 本体がこの例外を捕捉せず、そのまま呼び出し元（dispatch_agent
    のジョブランナー）まで伝播していた。通信エラー（TimeoutError/LLM_CONNECTION_ERRORS）
    は _build_truncation_message で会話要約を保持したまま正常returnするのに対し、
    ThinkingLoopDetected だけが例外送出で job.status="error" となり会話情報が
    一切引き継がれない非対称な挙動になっていた（2026-09-09 実運用で確認）。
    """
    from src.llm import ThinkingLoopDetected

    async def fake_aclose_model_client(model) -> None:
        return None

    monkeypatch.setattr(subagent, "aclose_model_client", fake_aclose_model_client)

    def _loop_exc() -> Exception:
        return ThinkingLoopDetected("反復ループ", snippet="同じ文の繰り返し")

    fake_build_model, state = _make_fake_build_model(fail_times=99, final_message=_FINAL, make_exc=_loop_exc)
    monkeypatch.setattr(subagent, "build_model", fake_build_model)
    config = _FakeConfig()
    config.thinking_loop_guard_max_retries = 1

    result = await subagent.run_subagent(
        task="t",
        tools=[],
        system_prompt="sp",
        config=config,
        max_iterations=5,
    )

    assert subagent.is_truncated_result(result)
    assert "repetition loop" in result
    assert "同じ文の繰り返し" in result
