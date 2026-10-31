"""サブエージェントにも [context_trim]/[context_compaction] を適用する変更の回帰テスト。

Claude Codeがメイン会話・サブエージェントでコンテキスト管理機能の有無を
区別しない方式に倣い、src/subagent.py の run_subagent にも同じロジックを
適用した（要望: 「context_trimとcontext_compactionをサブエージェントにも」）。
"""

import pytest
from dataclasses import dataclass
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from src import subagent
from src.context_compaction import _PRE_NOTE_MARKER
from src.subagent import _build_llm_input


@dataclass
class _FakeConfig:
    """_build_llm_input/run_subagent が実際に参照するのは context_trim_subagent_*/
    context_compaction_subagent_* 側（[context_trim.subagent]/
    [context_compaction.subagent] 導入後の src/subagent.py の実装。config.py
    docstring参照）。run_subagent 内の _subagent_compaction_config が
    dataclasses.replace() を使うため、このフェイクも dataclass にする必要があり
    （通常クラスだと "replace() should be called on dataclass instances" で
    落ちる）、かつ replace() の changes には main側 context_compaction_* の
    フィールド名がそのまま使われるため main側フィールドも定義しておく必要がある
    （_subagent_compaction_config は「context_compaction_* を subagent_* の
    値で置き換えたビュー」を作る実装のため）。
    """

    thinking_loop_guard_max_retries: int = 0
    # 同じツール呼び出しのループ検知（src/tool_loop_guard.py）。このテストの対象外のため無効。
    tool_loop_guard_enabled: bool = False
    tool_loop_guard_max_repeats: int = 3
    tool_loop_guard_exclude_tools: tuple = ()
    tool_loop_guard_nudge_messages: tuple = ()
    subagent_empty_response_max_retries: int = 0
    subagent_token_guard_enabled: bool = False
    track_token_usage: bool = False
    context_trim_subagent_enabled: bool = True
    context_trim_subagent_keep_recent_tool_iterations: int = 0
    context_trim_subagent_truncated_max_chars: int = 20
    context_trim_subagent_duplicate_guard_tool_max_chars: int = 20
    context_trim_subagent_ai_messages: bool = False
    context_trim_subagent_keep_recent_ai_iterations: int = 0
    context_trim_subagent_trigger_total_tokens: int = 0
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
    context_compaction_subagent_enabled: bool = False
    context_compaction_subagent_token_threshold: int = 0
    context_compaction_subagent_single_request_token_threshold: int = 0
    context_compaction_subagent_keep_recent_iterations: int = 0
    context_compaction_subagent_min_messages_to_compact: int = 0
    context_compaction_subagent_prompt_path: str | None = None
    context_compaction_subagent_summary_source_max_chars: int = 0
    context_compaction_subagent_pre_note_threshold: int = 0
    context_compaction_subagent_pre_note_warning_text: str = ""
    context_compaction_require_note_max_skips: int = 0
    context_compaction_subagent_require_note_max_skips: int = 0
    subagent_token_guard_soft_threshold: int = 999999999
    subagent_token_guard_hard_threshold: int = 999999999
    subagent_token_guard_soft_warning_text: str = ""


def test_build_llm_input_trims_old_tool_messages_without_mutating_original() -> None:
    """context_trim_enabled=True なら、古い ToolMessage を切り詰めたコピーを返し、
    呼び出し元の messages 本体（run_subagent の永続履歴）は書き換えない。

    サブエージェントの会話は通常ユーザーターン（HumanMessage）が1件しか
    無い（1回のタスク指示の中で何度もツール呼び出しを繰り返す構造）ため、
    find_iteration_cut_index()のフォールバック（ツール往復単位のカウント）が
    効くよう、3回分のラウンドトリップ（各ToolMessageに対応するAIMessage.
    tool_callsを含む現実的な構造）を用意し、keep_recent_tool_iterations=2で
    直近2往復（c0, c1）は全文保持、最古の往復（c_old）だけ切り詰められる
    ことを確認する。
    """
    long_content = "x" * 1000
    messages = [
        SystemMessage(content="sp"),
        HumanMessage(content="task"),
        AIMessage(content="", tool_calls=[{"name": "Read", "args": {}, "id": "c_old"}]),
        ToolMessage(content=long_content, name="Read", tool_call_id="c_old"),
        AIMessage(content="解釈", tool_calls=[{"name": "Read", "args": {}, "id": "c0"}]),
        ToolMessage(content=long_content, name="Read", tool_call_id="c0"),
        AIMessage(content="", tool_calls=[{"name": "Read", "args": {}, "id": "c1"}]),
        ToolMessage(content=long_content, name="Read", tool_call_id="c1"),
    ]
    config = _FakeConfig()
    config.context_trim_subagent_keep_recent_tool_iterations = 2

    llm_input = _build_llm_input(messages, config)

    # 最古の往復（c_old）は切り詰められ、直近2往復（c0, c1）は全文保持される。
    assert llm_input[3].content != long_content
    assert len(llm_input[3].content) < 1000
    assert llm_input[5].content == long_content
    assert llm_input[7].content == long_content
    # 元の messages は書き換えられていない（永続履歴を守る context_trim の方針）。
    assert messages[3].content == long_content


def test_build_llm_input_noop_when_disabled() -> None:
    config = _FakeConfig()
    config.context_trim_subagent_enabled = False
    messages = [SystemMessage(content="sp"), HumanMessage(content="task")]

    assert _build_llm_input(messages, config) is messages


class _ToolCallThenFinalModel:
    """1回目は tool_calls を含む応答、2回目は最終回答を返す固定シナリオ。"""

    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            # write_thread_note も同時に呼ばせておく（is_compaction_blocked_by_missing_note
            # に見送られず、本テストの主目的＝SystemMessage除外の検証まで到達させるため。
            # tools=[dummy_tool]しか渡されないためwrite_thread_note自体は「未知のツール」
            # エラーになるが、判定はAIMessage.tool_callsの有無だけを見るため実害は無い）。
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "dummy_tool", "args": {}, "id": "call-1"},
                    {"name": "write_thread_note", "args": {"topic": "t", "content": "c"}, "id": "call-2"},
                ],
            )
        return AIMessage(content="完了しました")


@tool
def dummy_tool() -> str:
    """テスト用の何もしないツール。"""
    return "ok"


@pytest.mark.asyncio
async def test_compaction_excludes_leading_system_message(monkeypatch) -> None:
    """圧縮対象から SystemMessage（messages[0]）を除外して maybe_compact に渡すことの回帰テスト。

    graph.py のメインエージェントは system_prompt を state["messages"] に含めない
    構造だが、run_subagent の messages はローカルリストの先頭に SystemMessage を
    積む構造が異なる。除外せずに圧縮対象へ渡すと、要約後にサブエージェントが
    システムプロンプトを失う（本テストが検知したい退行）。
    """
    config = _FakeConfig()
    config.context_trim_subagent_enabled = False
    config.context_compaction_subagent_enabled = True
    config.context_compaction_subagent_keep_recent_iterations = 3
    config.track_token_usage = True

    fake_model = _ToolCallThenFinalModel()

    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    # should_compact は初回のツール実行直後にだけ True を返す（無限圧縮ループ回避）。
    call_state = {"should_compact_calls": 0}

    def fake_should_compact(cumulative_usage, last_usage, message_count, config):
        call_state["should_compact_calls"] += 1
        return call_state["should_compact_calls"] == 1

    captured_maybe_compact_args = {}

    async def fake_maybe_compact(messages, model, config, *, role="sub", pinned_instruction=None):
        captured_maybe_compact_args["messages"] = list(messages)
        return [HumanMessage(content="[要約]圧縮済み")]

    monkeypatch.setattr(subagent, "should_compact", fake_should_compact)
    monkeypatch.setattr(subagent, "maybe_compact", fake_maybe_compact)

    result = await subagent.run_subagent(
        task="t",
        tools=[dummy_tool],
        system_prompt="サブエージェント専用システムプロンプト",
        config=config,
        max_iterations=5,
    )

    assert result == "完了しました"
    assert "messages" in captured_maybe_compact_args
    passed_messages = captured_maybe_compact_args["messages"]
    # SystemMessage が圧縮対象（=要約に飲み込まれて消える可能性のある側）に
    # 含まれていないこと。
    assert not any(isinstance(m, SystemMessage) for m in passed_messages)


class _ToolCallWithUsageThenFinalModel:
    """1回目はusage_metadata付きtool_calls応答を返し、2回目に渡された入力を
    記録した上で最終回答を返す固定シナリオ。"""

    def __init__(self, total_tokens: int) -> None:
        self.calls = 0
        self.total_tokens = total_tokens
        self.captured_second_input: list | None = None

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            msg = AIMessage(content="", tool_calls=[{"name": "dummy_tool", "args": {}, "id": "call-1"}])
            msg.usage_metadata = {
                "input_tokens": self.total_tokens - 10,
                "output_tokens": 10,
                "total_tokens": self.total_tokens,
            }
            return msg
        self.captured_second_input = list(messages)
        return AIMessage(content="完了しました")


@pytest.mark.asyncio
async def test_pre_note_nudge_injected_when_soft_threshold_not_reached(monkeypatch) -> None:
    """[context_compaction.subagent].pre_note_threshold 到達時、次のLLM呼び出しの
    入力へ write_thread_note を促す HumanMessage が差し込まれる。

    以前は maybe_append_precompact_note_nudge が src/subagent.py から一度も
    呼ばれておらず、[context_compaction.subagent].pre_note_threshold が
    設定として存在するのに何の効果も持たない実装漏れになっていた（この
    テストはその回帰防止）。
    """
    config = _FakeConfig()
    config.context_trim_subagent_enabled = False
    config.track_token_usage = True
    config.subagent_token_guard_enabled = True
    config.subagent_token_guard_soft_threshold = 100000  # 到達しない水準
    config.subagent_token_guard_hard_threshold = 200000
    config.context_compaction_subagent_enabled = True
    config.context_compaction_subagent_pre_note_threshold = 1000  # 到達する水準
    config.context_compaction_subagent_keep_recent_iterations = 3
    config.context_compaction_subagent_min_messages_to_compact = 9999  # should_compactは発火させない

    fake_model = _ToolCallWithUsageThenFinalModel(total_tokens=1500)

    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    result = await subagent.run_subagent(
        task="t",
        tools=[dummy_tool],
        system_prompt="サブエージェント専用システムプロンプト",
        config=config,
        max_iterations=5,
    )

    assert result == "完了しました"
    assert fake_model.captured_second_input is not None
    assert any(
        isinstance(m, HumanMessage) and _PRE_NOTE_MARKER in m.content for m in fake_model.captured_second_input
    )


class _RepeatedToolCallModel:
    """max_calls回目に達するまで毎回dummy_tool呼び出しのtool_callsを返し続ける
    （write_thread_noteは一度も呼ばない）固定シナリオ。"""

    def __init__(self, max_calls: int = 99) -> None:
        self.calls = 0
        self.max_calls = max_calls

    def bind_tools(self, tools, tool_choice=None):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls >= self.max_calls:
            return AIMessage(content="完了しました")
        return AIMessage(content="", tool_calls=[{"name": "dummy_tool", "args": {}, "id": f"call-{self.calls}"}])


@pytest.mark.asyncio
async def test_compaction_skipped_when_note_never_called(monkeypatch) -> None:
    """write_thread_noteが一度も呼ばれず、force_write_thread_noteによる強制実行も
    失敗した場合は、require_note_max_skips=0（無期限に待つ設定）の間は
    should_compactがTrueであってもmaybe_compactが一度も呼ばれない
    （is_compaction_blocked_by_missing_noteの実装漏れ修正に対するend-to-end回帰）。

    ここで使う _RepeatedToolCallModel は write_thread_note ではなく dummy_tool を
    呼ぶ応答を返すため、force_write_thread_note は topic/content を得られず
    Noneを返す（＝従来の見送りへフォールバックする）。
    """
    config = _FakeConfig()
    config.context_trim_subagent_enabled = False
    config.context_compaction_subagent_enabled = True
    config.context_compaction_subagent_keep_recent_iterations = 3
    config.track_token_usage = True
    config.context_compaction_subagent_require_note_max_skips = 0

    fake_model = _RepeatedToolCallModel()

    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)
    monkeypatch.setattr(subagent, "should_compact", lambda *a, **k: True)

    compact_calls = {"count": 0}

    async def fake_maybe_compact(messages, model, config, *, role="sub", pinned_instruction=None):
        compact_calls["count"] += 1
        return [HumanMessage(content="[要約]")]

    monkeypatch.setattr(subagent, "maybe_compact", fake_maybe_compact)

    result = await subagent.run_subagent(
        task="t",
        tools=[dummy_tool],
        system_prompt="サブエージェント専用システムプロンプト",
        config=config,
        max_iterations=3,
    )

    assert compact_calls["count"] == 0
    assert "maximum number of iterations" in result


@pytest.mark.asyncio
async def test_force_write_thread_note_runs_before_skipping_compaction(monkeypatch) -> None:
    """write_thread_note未呼び出しでも、見送る前にforce_write_thread_noteで
    強制実行し、成功したらその回のうちに圧縮まで進むこと。

    メインエージェント（app.py の _run_context_compaction）にだけ強制実行が
    入っていてサブエージェント側が見送りのままだと、「委譲元へ返すのは要約、
    具体的な事実は thread note へ」という前提で動くサブエージェントの方こそ
    事実退避の機会を失う。require_note_max_skips=0（無期限に待つ設定）でも
    圧縮まで到達することで、強制実行が効いていることを確認する。
    """
    config = _FakeConfig()
    config.context_trim_subagent_enabled = False
    config.context_compaction_subagent_enabled = True
    config.context_compaction_subagent_keep_recent_iterations = 3
    config.track_token_usage = True
    config.context_compaction_subagent_require_note_max_skips = 0

    fake_model = _RepeatedToolCallModel()

    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)
    monkeypatch.setattr(subagent, "should_compact", lambda *a, **k: True)

    forced_calls = {"count": 0}

    async def fake_force_write_thread_note(messages, model, config):
        forced_calls["count"] += 1
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "write_thread_note", "args": {"topic": "t", "content": "c"}, "id": "fc-1"}],
        )
        return ai, [ToolMessage(content="書き込みました", tool_call_id="fc-1")]

    monkeypatch.setattr(subagent, "force_write_thread_note", fake_force_write_thread_note)

    compact_inputs: list[list] = []

    async def fake_maybe_compact(messages, model, config, *, role="sub", pinned_instruction=None):
        compact_inputs.append(list(messages))
        return [HumanMessage(content="[要約]")]

    monkeypatch.setattr(subagent, "maybe_compact", fake_maybe_compact)

    await subagent.run_subagent(
        task="t",
        tools=[dummy_tool],
        system_prompt="サブエージェント専用システムプロンプト",
        config=config,
        max_iterations=3,
    )

    assert forced_calls["count"] >= 1
    assert len(compact_inputs) >= 1
    # 強制実行の結果が圧縮対象の履歴へ積まれており、かつ tool_calls と
    # ToolMessage が全件対応していること（1件でも欠けるとOpenAI互換APIが
    # 以降のリクエストを拒否する）。
    compacted = compact_inputs[0]
    assert any(
        isinstance(m, AIMessage) and any(tc["id"] == "fc-1" for tc in (m.tool_calls or [])) for m in compacted
    )
    issued = {tc["id"] for m in compacted if isinstance(m, AIMessage) for tc in (m.tool_calls or [])}
    answered = {m.tool_call_id for m in compacted if isinstance(m, ToolMessage)}
    assert issued == answered


@pytest.mark.asyncio
async def test_compaction_forced_after_max_skips(monkeypatch) -> None:
    """write_thread_note未呼び出しでも、見送り回数がrequire_note_max_skipsに
    達したら記録が無くても圧縮を強制する（安全弁。LLMが指示を無視し続けても
    コンテキスト上限に張り付くのを防ぐ）。
    """
    config = _FakeConfig()
    config.context_trim_subagent_enabled = False
    config.context_compaction_subagent_enabled = True
    config.context_compaction_subagent_keep_recent_iterations = 3
    config.track_token_usage = True
    config.context_compaction_subagent_require_note_max_skips = 2

    fake_model = _RepeatedToolCallModel()

    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)
    monkeypatch.setattr(subagent, "should_compact", lambda *a, **k: True)

    compact_calls = {"count": 0}

    async def fake_maybe_compact(messages, model, config, *, role="sub", pinned_instruction=None):
        compact_calls["count"] += 1
        return [HumanMessage(content="[要約]")]

    monkeypatch.setattr(subagent, "maybe_compact", fake_maybe_compact)

    await subagent.run_subagent(
        task="t",
        tools=[dummy_tool],
        system_prompt="サブエージェント専用システムプロンプト",
        config=config,
        max_iterations=3,
    )

    # iter1: skip_count 0->1（見送り）, iter2: skip_count 1->2（見送り）,
    # iter3: skip_count>=max_skips(2)のため強制的に圧縮を1回だけ実行する。
    assert compact_calls["count"] == 1


@pytest.mark.asyncio
async def test_pre_note_nudge_not_injected_when_soft_threshold_reached(monkeypatch) -> None:
    """token_guardのソフト警告が発動する場合は、同じ呼び出しでpre_noteを
    差し込まない（「これ以上調べるな」と「write_thread_noteを呼べ」が
    矛盾するのを避けるsrc/graph.pyと同じ排他方針の回帰）。
    """
    config = _FakeConfig()
    config.context_trim_subagent_enabled = False
    config.track_token_usage = True
    config.subagent_token_guard_enabled = True
    config.subagent_token_guard_soft_threshold = 1000  # 到達する水準
    config.subagent_token_guard_hard_threshold = 200000
    config.subagent_token_guard_soft_warning_text = "ソフト警告文言"
    config.context_compaction_subagent_enabled = True
    config.context_compaction_subagent_pre_note_threshold = 1000  # softと同時に到達する水準
    config.context_compaction_subagent_keep_recent_iterations = 3
    config.context_compaction_subagent_min_messages_to_compact = 9999

    fake_model = _ToolCallWithUsageThenFinalModel(total_tokens=1500)

    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    result = await subagent.run_subagent(
        task="t",
        tools=[dummy_tool],
        system_prompt="サブエージェント専用システムプロンプト",
        config=config,
        max_iterations=5,
    )

    assert result == "完了しました"
    assert fake_model.captured_second_input is not None
    assert any(
        isinstance(m, HumanMessage) and m.content == "ソフト警告文言" for m in fake_model.captured_second_input
    )
    assert not any(
        isinstance(m, HumanMessage) and _PRE_NOTE_MARKER in m.content for m in fake_model.captured_second_input
    )


@pytest.mark.asyncio
async def test_hard_threshold_triggers_without_prior_soft_warning(monkeypatch) -> None:
    """急速なトークン爆発でsoft_thresholdを経ずに一気にhard_thresholdへ到達した
    場合でも、その場でon_cancelledを呼んでスクラッチノートへ退避した上で
    打ち切りメッセージを返す（以前はsoft警告発火済みであることが前提の
    判定だったため、この飛び越えケースでガードが効かず打ち切られない
    事象があった。この回帰テスト）。
    """
    config = _FakeConfig()
    config.context_trim_subagent_enabled = False
    config.track_token_usage = True
    config.subagent_token_guard_enabled = True
    config.subagent_token_guard_soft_threshold = 100000
    config.subagent_token_guard_hard_threshold = 150000

    fake_model = _ToolCallWithUsageThenFinalModel(total_tokens=200000)

    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    captured: list = []
    captured_reasons: list[str] = []

    def _on_cancelled(messages: list, reason: str) -> None:
        captured.append(list(messages))
        captured_reasons.append(reason)

    result = await subagent.run_subagent(
        task="t",
        tools=[dummy_tool],
        system_prompt="サブエージェント専用システムプロンプト",
        config=config,
        max_iterations=5,
        on_cancelled=_on_cancelled,
    )

    assert "token usage reached the limit" in result
    # 2回目のainvoke（ツール実行後の続き）は発生せず、その場で打ち切られる。
    assert fake_model.captured_second_input is None
    assert len(captured) == 1
    # 退避の見出しが「停止ボタンによる強制終了」と混ざらないこと
    # （原因が全く異なるため、後からスクラッチノートを読む人が取り違える）。
    assert captured_reasons == [subagent.RESCUE_REASON_TOKEN_GUARD]


@pytest.mark.asyncio
async def test_hard_threshold_without_on_cancelled_still_truncates(monkeypatch) -> None:
    """on_cancelled未指定でも例外なく打ち切りメッセージが返る。"""
    config = _FakeConfig()
    config.context_trim_subagent_enabled = False
    config.track_token_usage = True
    config.subagent_token_guard_enabled = True
    config.subagent_token_guard_soft_threshold = 100000
    config.subagent_token_guard_hard_threshold = 150000

    fake_model = _ToolCallWithUsageThenFinalModel(total_tokens=200000)

    async def fake_build_model(config, role):
        return fake_model

    monkeypatch.setattr(subagent, "build_model", fake_build_model)

    result = await subagent.run_subagent(
        task="t",
        tools=[dummy_tool],
        system_prompt="サブエージェント専用システムプロンプト",
        config=config,
        max_iterations=5,
    )

    assert "token usage reached the limit" in result
