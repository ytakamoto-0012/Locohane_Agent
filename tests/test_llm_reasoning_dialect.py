"""推論サーバーの reasoning 方言（src/llm/dialect.py）と ChatLlamaCpp の正規化のテスト。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from src import config as config_module
from src.context_trim import trim_old_ai_messages
from src.llm import dialect
from src.llm.chat_model import ChatLlamaCpp


@dataclass
class _FakeConfig:
    top_k: int | None = None
    repeat_penalty: float | None = None
    dry_multiplier: float | None = None
    dry_base: float | None = None
    dry_allowed_length: int | None = None
    dry_penalty_last_n: int | None = None
    dry_sequence_breakers: list[str] | None = None
    enable_thinking: bool | None = None
    reasoning_preserve: bool | None = None
    reasoning_effort: str | None = None
    reasoning_format: str | None = None
    reasoning_budget: int | None = None
    reasoning_budget_message: str | None = None


def _full_config() -> _FakeConfig:
    return _FakeConfig(
        top_k=20,
        repeat_penalty=1.1,
        dry_multiplier=0.8,
        dry_base=1.75,
        dry_allowed_length=2,
        dry_penalty_last_n=-1,
        dry_sequence_breakers=["\n"],
        enable_thinking=True,
        reasoning_preserve=True,
        reasoning_effort="low",
        reasoning_format="deepseek",
        reasoning_budget=1024,
        reasoning_budget_message="stop",
    )


@pytest.fixture(autouse=True)
def _reset_warned():
    dialect._warned_unsupported.clear()
    yield
    dialect._warned_unsupported.clear()


# --- extract_reasoning ---


@pytest.mark.parametrize(
    "fields, expected",
    [
        (None, None),
        ({}, None),
        ({"content": "x"}, None),
        ({"reasoning_content": "a"}, "a"),
        ({"reasoning": "b"}, "b"),
        ({"reasoning": "b", "reasoning_content": "b"}, "b"),  # vLLM互換モード: 連結しない
        ({"reasoning": "", "reasoning_content": "a"}, "a"),
        ({"reasoning": None, "reasoning_content": "a"}, "a"),
    ],
)
def test_extract_reasoning(fields, expected):
    assert dialect.extract_reasoning(fields) == expected


# --- build_extra_body ---


@pytest.mark.parametrize("provider", ["llama_cpp", "openai_compatible"])
def test_build_extra_body_llama_style(provider):
    body = dialect.build_extra_body(_full_config(), provider)
    assert body == {
        "top_k": 20,
        "repeat_penalty": 1.1,
        "dry_multiplier": 0.8,
        "dry_base": 1.75,
        "dry_allowed_length": 2,
        "dry_penalty_last_n": -1,
        "dry_sequence_breakers": ["\n"],
        "chat_template_kwargs": {"enable_thinking": True, "preserve_reasoning": True, "preserve_thinking": True},
        "reasoning_effort": "low",
        "reasoning_format": "deepseek",
        "reasoning_budget_tokens": 1024,
        "reasoning_budget_message": "stop",
    }


def test_build_extra_body_vllm(caplog):
    with caplog.at_level(logging.WARNING, logger="src.llm.dialect"):
        body = dialect.build_extra_body(_full_config(), "vllm")
    assert body == {
        "top_k": 20,
        "repetition_penalty": 1.1,
        "chat_template_kwargs": {"enable_thinking": True, "preserve_reasoning": True, "preserve_thinking": True},
        "reasoning_effort": "low",
        "thinking_token_budget": 1024,
    }
    warned = {r.getMessage().split(" ")[0] for r in caplog.records}
    assert warned == {
        f"[llm].{k}"
        for k in (
            "dry_multiplier",
            "dry_base",
            "dry_allowed_length",
            "dry_penalty_last_n",
            "dry_sequence_breakers",
            "reasoning_format",
            "reasoning_budget_message",
        )
    }


def test_build_extra_body_vllm_warns_once(caplog):
    cfg = _FakeConfig(dry_multiplier=0.8)
    with caplog.at_level(logging.WARNING, logger="src.llm.dialect"):
        dialect.build_extra_body(cfg, "vllm")
        dialect.build_extra_body(cfg, "vllm")
    assert len(caplog.records) == 1


def test_build_extra_body_empty_config():
    for provider in ("llama_cpp", "vllm", "openai_compatible"):
        assert dialect.build_extra_body(_FakeConfig(), provider) == {}


@pytest.mark.parametrize("provider", ["llama_cpp", "vllm", "openai_compatible"])
def test_build_extra_body_reasoning_effort_default_is_not_sent(provider):
    assert "reasoning_effort" not in dialect.build_extra_body(_FakeConfig(reasoning_effort="default"), provider)
    assert dialect.build_extra_body(_FakeConfig(reasoning_effort="none"), provider)["reasoning_effort"] == "none"


@pytest.mark.parametrize("provider", ["llama_cpp", "vllm", "openai_compatible"])
@pytest.mark.parametrize("enable_thinking", [None, True, False])
def test_build_extra_body_reasoning_effort_none_disables_thinking(provider, enable_thinking, caplog):
    cfg = _FakeConfig(reasoning_effort="none", enable_thinking=enable_thinking)
    with caplog.at_level(logging.WARNING, logger="src.llm.dialect"):
        body = dialect.build_extra_body(cfg, provider)
    assert body["reasoning_effort"] == "none"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert len(caplog.records) == (1 if enable_thinking else 0)


def test_build_extra_body_vllm_unlimited_budget_is_not_sent():
    assert dialect.build_extra_body(_FakeConfig(reasoning_budget=-1), "vllm") == {}
    assert dialect.build_extra_body(_FakeConfig(reasoning_budget=-1), "llama_cpp") == {"reasoning_budget_tokens": -1}


def test_build_extra_body_enable_thinking_only():
    body = dialect.build_extra_body(_FakeConfig(enable_thinking=False), "llama_cpp")
    assert body == {"chat_template_kwargs": {"enable_thinking": False}}


# --- ChatLlamaCpp ---


def _model(**kwargs) -> ChatLlamaCpp:
    return ChatLlamaCpp(base_url="http://localhost:1/v1", api_key="dummy", model="m", **kwargs)


def _history() -> list:
    return [
        HumanMessage("q1"),
        AIMessage(
            "",
            additional_kwargs={"reasoning_content": "think1"},
            tool_calls=[{"name": "x", "args": {}, "id": "t1"}],
        ),
        ToolMessage("ok", tool_call_id="t1"),
        AIMessage("a1", additional_kwargs={"reasoning_content": "think2"}),
        HumanMessage("q2"),
    ]


@pytest.mark.parametrize(
    "provider, keys",
    [
        ("llama_cpp", {"reasoning_content"}),
        ("vllm", {"reasoning"}),
        ("openai_compatible", {"reasoning_content", "reasoning"}),
    ],
)
def test_request_payload_history_reasoning_keys(provider, keys):
    payload = _model(preserve_reasoning_content=True, reasoning_dialect=provider)._get_request_payload(_history())
    assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert [{k: m[k] for k in keys} for m in assistants] == [
        {k: "think1" for k in keys},
        {k: "think2" for k in keys},
    ]
    for m in payload["messages"]:
        assert not ({"reasoning", "reasoning_content"} - keys) & m.keys()


def test_request_payload_drops_reasoning_before_last_user_turn():
    """最後のユーザー発話より前のターンの thinking は載せず、今のツールループ内は全件載せる。
    nudge 等の目印なし HumanMessage は境界にならない。"""
    messages = [
        dialect.mark_user_turn(HumanMessage("q1")),
        AIMessage("a1", additional_kwargs={"reasoning_content": "old"}),
        dialect.mark_user_turn(HumanMessage("q2")),
        AIMessage("", additional_kwargs={"reasoning_content": "t1"}, tool_calls=[{"name": "x", "args": {}, "id": "c1"}]),
        ToolMessage("ok", tool_call_id="c1"),
        HumanMessage("nudge"),
        AIMessage("", additional_kwargs={"reasoning_content": "t2"}, tool_calls=[{"name": "x", "args": {}, "id": "c2"}]),
        ToolMessage("ok", tool_call_id="c2"),
    ]
    payload = _model(preserve_reasoning_content=True)._get_request_payload(messages)
    assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert [m.get("reasoning_content") for m in assistants] == [None, "t1", "t2"]
    for m in payload["messages"]:
        assert dialect.USER_TURN_KWARG not in m


def test_request_payload_without_preserve_has_no_reasoning():
    payload = _model(reasoning_dialect="openai_compatible")._get_request_payload(_history())
    for m in payload["messages"]:
        assert "reasoning" not in m and "reasoning_content" not in m


@pytest.mark.parametrize(
    "delta, expected",
    [
        ({"reasoning_content": "a"}, "a"),
        ({"reasoning": "b"}, "b"),
        ({"reasoning": "b", "reasoning_content": "b"}, "b"),
        ({"content": "c"}, None),
    ],
)
def test_stream_chunk_reasoning_is_normalized(delta, expected):
    chunk = {"choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]}
    gen = _model()._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, {})
    assert gen.message.additional_kwargs.get("reasoning_content") == expected
    assert "reasoning" not in gen.message.additional_kwargs


@pytest.mark.parametrize("field_name", ["reasoning", "reasoning_content"])
def test_non_stream_result_reasoning_is_normalized(field_name):
    response = {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "2", field_name: "think"},
                "finish_reason": "stop",
            }
        ],
    }
    result = _model()._create_chat_result(response)
    assert result.generations[0].message.additional_kwargs["reasoning_content"] == "think"


# --- context_trim ---


def test_trim_old_ai_messages_drops_old_reasoning_only():
    messages = [
        HumanMessage("q1"),
        AIMessage("a1", additional_kwargs={"reasoning_content": "old", "other": 1}),
        HumanMessage("q2"),
        AIMessage("a2", additional_kwargs={"reasoning_content": "new"}),
    ]
    trimmed = trim_old_ai_messages(messages, keep_recent_iterations=1, max_chars=1000)
    assert trimmed[1].additional_kwargs == {"other": 1}
    assert trimmed[3].additional_kwargs == {"reasoning_content": "new"}
    assert messages[1].additional_kwargs["reasoning_content"] == "old"  # 元の履歴は書き換えない


# --- config ---


def test_llm_endpoints_accept_vllm_provider():
    endpoints = config_module._as_llm_endpoints(
        '[{"base_url": "http://h/v1", "api_key": "k", "model": "m", "provider": "vllm"}]', "main_url"
    )
    assert endpoints[0].provider == "vllm"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("high", "high"), ("none", "none"), ("", "low"), (None, "low"), ("bogus", "low"), (3, "low")],
)
def test_with_reasoning_effort_overrides_only_valid_values(requested, expected):
    # チャット画面で選んだ値（メッセージの metadata）だけを上書きし、空・不正値は config.ini の値を使う。
    base = _FakeConfig(reasoning_effort="low")
    result = config_module.with_reasoning_effort(base, requested)
    assert result.reasoning_effort == expected
    assert base.reasoning_effort == "low"  # 元の Config は変更しない
    if expected == "low":
        assert result is base
