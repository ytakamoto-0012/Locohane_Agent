"""[thinking_loop_guard].target（ループ検知の監視対象）のテスト。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk

from src.llm.chat_model import ChatLlamaCpp
from src.llm.loop_guard import ThinkingLoopDetected, _chunk_delta_parts, _chunk_delta_text, _ThinkingLoopDetector

_LOOP_TEXT = "`approve_plan` を実行。（結果確認）よし。\n"


def _chunk(content: str = "", reasoning: str | None = None) -> ChatGenerationChunk:
    kwargs = {"reasoning_content": reasoning} if reasoning else {}
    return ChatGenerationChunk(message=AIMessageChunk(content=content, additional_kwargs=kwargs))


def test_chunk_delta_text_includes_reasoning_by_default():
    assert _chunk_delta_text(_chunk("A", "R")) == "AR"


def test_chunk_delta_text_content_only():
    assert _chunk_delta_text(_chunk("A", "R"), include_reasoning=False) == "A"
    assert _chunk_delta_text(_chunk("", "R"), include_reasoning=False) == ""


def test_chunk_delta_parts_splits_channels():
    assert _chunk_delta_parts(_chunk("A", "R")) == ("A", "R")
    assert _chunk_delta_parts(_chunk("A", "R"), include_reasoning=False) == ("A", "")
    assert _chunk_delta_parts(_chunk("A", "R"), include_content=False) == ("", "R")


def test_chunk_delta_text_thinking_only():
    assert _chunk_delta_text(_chunk("A", "R"), include_content=False) == "R"
    assert _chunk_delta_text(_chunk("A"), include_content=False) == ""


def _stream_response(field: str) -> httpx.MockTransport:
    """field（"reasoning_content" か "content"）に同じ文を繰り返し流す応答。"""

    def handler(request):
        chunks = [
            {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
             "choices": [{"index": 0, "delta": {field: _LOOP_TEXT}, "finish_reason": None}]}
            for _ in range(80)
        ]
        chunks.append({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                       "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]})
        text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


def _model(field: str, include_reasoning: bool, include_content: bool = True) -> ChatLlamaCpp:
    return ChatLlamaCpp(
        base_url="http://fake/v1",
        api_key="x",
        model="m",
        streaming=True,
        max_retries=0,
        http_async_client=httpx.AsyncClient(transport=_stream_response(field)),
        loop_guard_window_chars=200,
        loop_guard_check_interval_chars=50,
        loop_guard_confirm_count=2,
        loop_guard_max_history_chars=2000,
        loop_guard_match_ratio_threshold=0.6,
        loop_guard_include_reasoning=include_reasoning,
        loop_guard_include_content=include_content,
    )


def _run(model: ChatLlamaCpp):
    return asyncio.run(model.ainvoke([HumanMessage(content="go")]))


@pytest.mark.parametrize("field", ["reasoning_content", "content"])
def test_all_detects_loops_in_both(field):
    with pytest.raises(ThinkingLoopDetected):
        _run(_model(field, include_reasoning=True))


def test_content_only_ignores_reasoning_loop():
    result = _run(_model("reasoning_content", include_reasoning=False))
    assert result.content == "done"


def test_content_only_still_detects_content_loop():
    with pytest.raises(ThinkingLoopDetected):
        _run(_model("content", include_reasoning=False))


def test_thinking_only_ignores_content_loop():
    result = _run(_model("content", include_reasoning=True, include_content=False))
    assert result.content.endswith("done")


def test_thinking_only_still_detects_reasoning_loop():
    with pytest.raises(ThinkingLoopDetected):
        _run(_model("reasoning_content", include_reasoning=True, include_content=False))


@pytest.mark.parametrize(
    ("target", "reasoning", "content"),
    [("all", True, True), ("content_only", False, True), ("thinking_only", True, False)],
)
def test_config_target_maps_to_model_flags(target, reasoning, content):
    from src.config import _as_thinking_loop_guard_target

    assert _as_thinking_loop_guard_target(f" {target} ") == target
    assert (target != "content_only", target != "thinking_only") == (reasoning, content)


def _detector() -> _ThinkingLoopDetector:
    return _ThinkingLoopDetector(
        window_chars=1000, check_interval_chars=150, confirm_count=2, max_history_chars=8000, match_ratio_threshold=0.6
    )


def test_detector_compares_only_within_the_same_channel():
    """同じ文の繰り返しは同じチャネル内なら検知し、もう一方のチャネルの過去とは突き合わせない。"""
    text = "同じ文の繰り返し。" * 300
    reasoning = _detector()
    assert any(reasoning.feed(text[i : i + 50], "reasoning") for i in range(0, len(text), 50))

    detector = _detector()
    assert not any(detector.feed(text[i : i + 50], "reasoning") for i in range(0, 1100, 50))
    assert not any(detector.feed(text[i : i + 50], "content") for i in range(0, 1100, 50))


def _two_channel_response(reasoning: str, content: str) -> httpx.MockTransport:
    def handler(request):
        def chunk(field, text, finish=None):
            return {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                    "choices": [{"index": 0, "delta": {field: text}, "finish_reason": finish}]}

        chunks = [chunk("reasoning_content", reasoning[i : i + 40]) for i in range(0, len(reasoning), 40)]
        chunks += [chunk("content", content[i : i + 40]) for i in range(0, len(content), 40)]
        chunks.append(chunk("content", "", "stop"))
        text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


def test_cross_channel_copy_is_not_a_loop():
    """思考で書いた表を本文でそのまま再列挙しても発火しない（2026-10-25 の docx ケースの誤検知の再現）。"""
    table = "| i | style | text |\n|---|---|---|\n" + "".join(
        f"| {i} | List Bullet | event {i} (date, {i + 10} attendees) |\n" for i in range(1, 60)
    )
    model = ChatLlamaCpp(
        base_url="http://fake/v1",
        api_key="x",
        model="m",
        streaming=True,
        max_retries=0,
        http_async_client=httpx.AsyncClient(transport=_two_channel_response(table, table)),
        loop_guard_window_chars=1000,
        loop_guard_check_interval_chars=150,
        loop_guard_confirm_count=2,
        loop_guard_max_history_chars=8000,
        loop_guard_match_ratio_threshold=0.6,
        loop_guard_include_reasoning=True,
        loop_guard_include_content=True,
    )
    assert _run(model).content == table
