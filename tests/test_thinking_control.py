"""ステップごとの思考レベル切り替え（src/llm/thinking_control.py）のテスト。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.llm import dialect
from src.llm.chat_model import ChatLlamaCpp, enable_thinking_control
from src.llm.thinking_control import (
    ThinkingControlSettings,
    apply_level,
    previous_step_stats,
    select_level,
    settings_from_config,
    unknown_tool_names,
)

BUDGET_MESSAGE = "STOP-THINKING"


def _settings(**overrides) -> ThinkingControlSettings:
    base = ThinkingControlSettings(
        rule_user_turn="high",
        rule_tool_error="high",
        rule_consecutive_cap="high",
        rule_after_tools="low",
        default_level="high",
        budgets={"low": 2048, "medium": 3072, "high": 4096, "xhigh": -1},
        after_tools=frozenset({"Read", "Grep"}),
        error_prefixes=("エラー",),
        max_consecutive_reduced=3,
        budget_message=BUDGET_MESSAGE,
    )
    return replace(base, **overrides)


_call_seq = 0


def _ai(*names: str, reasoning: str | None = None, content: str = "") -> AIMessage:
    global _call_seq
    calls = []
    for name in names:
        _call_seq += 1
        calls.append({"name": name, "args": {}, "id": f"call_{_call_seq}"})
    kwargs = {"reasoning_content": reasoning} if reasoning is not None else {}
    return AIMessage(content=content, tool_calls=calls, additional_kwargs=kwargs)


def _results(ai: AIMessage, *, error: str | None = None, status: str = "success") -> list[ToolMessage]:
    out = []
    for call in ai.tool_calls:
        text = error if (error and call["name"] == "Read") else "ok"
        out.append(ToolMessage(content=text, tool_call_id=call["id"], name=call["name"], status=status))
    return out


def _turn(*steps) -> list:
    """System + Human の後に、(AIMessage, ToolMessage...) を順に並べる。"""
    messages: list = [SystemMessage(content="sys"), HumanMessage(content="do it")]
    for step in steps:
        messages.extend(step)
    return messages


def _step(*names: str, **kwargs) -> list:
    ai = _ai(*names)
    return [ai, *_results(ai, **kwargs)]


# --- select_level ---


def test_user_turn_uses_rule_user_turn_level():
    assert select_level(_turn(), _settings(rule_user_turn="medium"))[:2] == ("medium", "user_turn")


def test_after_listed_tool_uses_rule_after_tools_level():
    level, reason, prev = select_level(_turn(_step("Read")), _settings())
    assert (level, reason, prev) == ("low", "after_tools", "high")
    assert select_level(_turn(_step("Read", "Grep")), _settings(rule_after_tools="off"))[:2] == ("off", "after_tools")


def test_unlisted_tool_falls_back_to_default():
    assert select_level(_turn(_step("Read", "run_script")), _settings())[:2] == ("high", "default")


def test_tool_error_by_prefix_uses_rule_tool_error_level():
    messages = _turn(_step("Read"), _step("Read", error="エラー: not found"))
    assert select_level(messages, _settings())[:2] == ("high", "tool_error")
    assert select_level(messages, _settings(rule_tool_error="xhigh"))[:2] == ("xhigh", "tool_error")


def test_tool_error_by_status():
    messages = _turn(_step("Read"), _step("Grep", status="error"))
    assert select_level(messages, _settings(rule_tool_error="medium"))[:2] == ("medium", "tool_error")


def _image_followup() -> HumanMessage:
    return HumanMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}])


def test_image_followup_is_not_a_user_turn():
    # analyze_image の結果の後に足される画像だけの HumanMessage は、ツール結果の続きとして扱う
    ai = _ai("Read")
    messages = _turn([ai, *_results(ai), _image_followup()])
    assert select_level(messages, _settings())[:3] == ("low", "after_tools", "high")
    # 連続回数も途切れない
    steps = []
    for _ in range(4):
        ai = _ai("Read")
        steps.append([ai, *_results(ai), _image_followup()])
    assert select_level(_turn(*steps), _settings())[:2] == ("high", "consecutive_cap")


def test_user_message_with_text_and_image_is_a_user_turn():
    messages = _turn(_step("Read"))
    messages.append(HumanMessage(content=[{"type": "text", "text": "これを見て"}, {"type": "image_url", "image_url": {"url": "x"}}]))
    assert select_level(messages, _settings())[:2] == ("high", "user_turn")


def test_consecutive_cap_returns_to_cap_level():
    # high → low → low → low（ここまでで low が3回）→ 次は high に戻す
    messages = _turn(_step("Read"), _step("Read"), _step("Read"))
    assert select_level(messages, _settings())[:2] == ("low", "after_tools")
    messages = _turn(_step("Read"), _step("Read"), _step("Read"), _step("Read"))
    assert select_level(messages, _settings())[:2] == ("high", "consecutive_cap")
    # 戻した次は再び low
    messages = _turn(_step("Read"), _step("Read"), _step("Read"), _step("Read"), _step("Read"))
    assert select_level(messages, _settings())[:2] == ("low", "after_tools")


def test_consecutive_cap_uses_its_own_level():
    messages = _turn(_step("Read"), _step("Read"), _step("Read"), _step("Read"))
    assert select_level(messages, _settings(rule_consecutive_cap="medium"))[:2] == ("medium", "consecutive_cap")
    # low が cap 未満でなければ働かない
    assert select_level(messages, _settings(rule_consecutive_cap="low"))[:2] == ("low", "after_tools")


def test_consecutive_cap_disabled_by_zero():
    messages = _turn(_step("Read"), _step("Read"), _step("Read"), _step("Read"))
    assert select_level(messages, _settings(max_consecutive_reduced=0))[0] == "low"


def test_trailing_human_nudge_resets_to_user_turn_level():
    messages = _turn(_step("Read"))
    messages.append(HumanMessage(content="nudge"))
    assert select_level(messages, _settings())[:2] == ("high", "user_turn")


def test_only_steps_after_last_human_are_considered():
    # 前のユーザーターンで low が続いていても、新しいユーザー発言でリセットされる
    messages = _turn(_step("Read"), _step("Read"))
    messages.append(AIMessage(content="answer"))
    messages.append(HumanMessage(content="next"))
    messages.extend(_step("Read"))
    assert select_level(messages, _settings())[:3] == ("low", "after_tools", "high")


# --- ルールごとのON/OFF（None = 無効） ---


def test_rule_user_turn_disabled_uses_default():
    assert select_level(_turn(), _settings(rule_user_turn=None, default_level="medium"))[:2] == ("medium", "default")


def test_rule_tool_error_disabled_falls_through_to_tool_names():
    messages = _turn(_step("Read", error="エラー: x"))
    assert select_level(messages, _settings(rule_tool_error=None))[:2] == ("low", "after_tools")


def test_rule_consecutive_cap_disabled():
    messages = _turn(_step("Read"), _step("Read"), _step("Read"), _step("Read"))
    assert select_level(messages, _settings(rule_consecutive_cap=None))[:2] == ("low", "after_tools")


def test_rule_after_tools_disabled_uses_default():
    assert select_level(_turn(_step("Read")), _settings(rule_after_tools=None))[:2] == ("high", "default")


def test_all_rules_off_always_default():
    settings = _settings(
        rule_user_turn=None,
        rule_tool_error=None,
        rule_consecutive_cap=None,
        rule_after_tools=None,
        default_level="medium",
    )
    for messages in (_turn(), _turn(_step("Read")), _turn(_step("Read", error="エラー"))):
        assert select_level(messages, settings)[:2] == ("medium", "default")


# --- apply_level ---


@pytest.mark.parametrize("provider", ["llama_cpp", "openai_compatible"])
def test_apply_level_budget_llama(provider):
    base = {"top_k": 20, "reasoning_budget_tokens": 4096, "chat_template_kwargs": {"enable_thinking": True}}
    result = apply_level(base, "low", _settings(), provider)
    assert result == {"top_k": 20, "reasoning_budget_tokens": 2048, "chat_template_kwargs": {"enable_thinking": True}}
    assert base["reasoning_budget_tokens"] == 4096  # 元の dict は変更しない


def test_apply_level_budget_vllm_and_unlimited():
    base = {"thinking_token_budget": 4096}
    assert apply_level(base, "medium", _settings(), "vllm") == {"thinking_token_budget": 3072}
    # -1（無制限）は vLLM へは送らない
    assert apply_level(base, "xhigh", _settings(), "vllm") == {}
    assert apply_level(base, "xhigh", _settings(), "llama_cpp") == {"reasoning_budget_tokens": -1}


def test_apply_level_off_disables_thinking_and_drops_budget():
    base = {"reasoning_budget_tokens": 4096, "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True}}
    result = apply_level(base, "off", _settings(), "llama_cpp")
    assert result == {"chat_template_kwargs": {"enable_thinking": False, "preserve_thinking": True}}
    assert base["chat_template_kwargs"]["enable_thinking"] is True


def test_apply_level_none_budget_sends_nothing():
    settings = _settings(budgets={"low": 1, "medium": 2, "high": None, "xhigh": -1})
    assert apply_level({"reasoning_budget_tokens": 4096}, "high", settings, "llama_cpp") == {}


def test_apply_level_handles_missing_extra_body():
    assert apply_level(None, "low", _settings(), "llama_cpp") == {"reasoning_budget_tokens": 2048}


# --- previous_step_stats ---


def test_previous_step_stats_detects_truncation_and_leak():
    ai = _ai("Read", reasoning=f"thinking... {BUDGET_MESSAGE}")
    stats = previous_step_stats(_turn([ai, *_results(ai)]), _settings())
    assert stats == {"reasoning_chars": len(f"thinking... {BUDGET_MESSAGE}"), "truncated": True, "leaked": False}

    ai = _ai("Read", reasoning="x", content=f"oops {BUDGET_MESSAGE}")
    stats = previous_step_stats(_turn([ai, *_results(ai)]), _settings())
    assert stats["truncated"] is False and stats["leaked"] is True


def test_previous_step_stats_none_without_previous_step():
    assert previous_step_stats(_turn(), _settings()) is None
    assert previous_step_stats(_turn([_ai("Read")]), _settings(budget_message=None))["truncated"] is None


# --- ChatLlamaCpp への組み込み ---


def _model(**kwargs) -> ChatLlamaCpp:
    return ChatLlamaCpp(
        base_url="http://127.0.0.1:1/v1",
        api_key="x",
        model="m",
        extra_body={"reasoning_budget_tokens": 4096, "chat_template_kwargs": {"enable_thinking": True}},
        **kwargs,
    )


def test_payload_is_rewritten_when_thinking_control_set():
    model = _model()
    model.thinking_control = _settings()
    model.thinking_control_role = "main"
    payload = model._get_request_payload(_turn(_step("Read")))
    assert payload["extra_body"]["reasoning_budget_tokens"] == 2048


def test_payload_unchanged_without_thinking_control():
    payload = _model()._get_request_payload(_turn(_step("Read")))
    assert payload["extra_body"]["reasoning_budget_tokens"] == 4096


def test_payload_rewrite_works_with_reasoning_preserve():
    model = _model(preserve_reasoning_content=True)
    model.thinking_control = _settings()
    ai = _ai("Read", reasoning="r1")
    payload = model._get_request_payload(_turn([ai, *_results(ai)]))
    assert payload["extra_body"]["reasoning_budget_tokens"] == 2048
    assert payload["messages"][2]["reasoning_content"] == "r1"


# --- enable_thinking_control ---


@dataclass
class _Cfg:
    thinking_control_enabled: bool = True
    thinking_control_apply_to_main: bool = True
    thinking_control_apply_to_sub: bool = True
    thinking_control_rule_user_turn: str | None = "high"
    thinking_control_rule_tool_error: str | None = "high"
    thinking_control_rule_consecutive_cap: str | None = "high"
    thinking_control_rule_after_tools: str | None = "low"
    thinking_control_default_level: str = "high"
    thinking_control_budget_low: int | None = 2048
    thinking_control_budget_medium: int | None = None
    thinking_control_budget_high: int | None = None
    thinking_control_budget_xhigh: int | None = -1
    thinking_control_after_tools: list[str] = field(default_factory=lambda: ["Read", "Grep"])
    thinking_control_error_prefixes: list[str] = field(default_factory=lambda: ["エラー"])
    thinking_control_max_consecutive_reduced: int = 3
    enable_thinking: bool | None = True
    reasoning_effort: str | None = None
    reasoning_budget: int | None = 4096
    reasoning_budget_message: str | None = None


@pytest.fixture(autouse=True)
def _reset_warned():
    dialect._warned_unsupported.clear()
    yield
    dialect._warned_unsupported.clear()


def test_settings_from_config_falls_back_to_llm_budget():
    settings = settings_from_config(_Cfg())
    assert settings.budgets == {"low": 2048, "medium": 4096, "high": 4096, "xhigh": -1}
    assert settings.after_tools == frozenset({"Read", "Grep"})


def test_enable_thinking_control_injects_settings():
    model = enable_thinking_control(_model(), _Cfg(), "sub")
    assert model.thinking_control is not None and model.thinking_control_role == "sub"


@pytest.mark.parametrize(
    "cfg, role",
    [
        (_Cfg(thinking_control_enabled=False), "main"),
        (_Cfg(thinking_control_apply_to_main=False), "main"),
        (_Cfg(thinking_control_apply_to_sub=False), "sub"),
        (_Cfg(enable_thinking=False), "main"),
        (_Cfg(reasoning_effort="none"), "main"),
    ],
)
def test_enable_thinking_control_skips(cfg, role):
    assert enable_thinking_control(_model(), cfg, role).thinking_control is None


def test_enable_thinking_control_ignores_non_chatllamacpp():
    fake = object()
    assert enable_thinking_control(fake, _Cfg(), "main") is fake


def test_unknown_tool_names():
    cfg = _Cfg(thinking_control_after_tools=["Read", "Raed", "Raed"])
    assert unknown_tool_names(cfg, {"Read", "dispatch_agent"}) == ["Raed"]
