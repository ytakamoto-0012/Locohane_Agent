"""本文に書かれた未宣言ツールの呼び出しを tool_call に組み直す処理のテスト（src/llm/undeclared_tool_call.py）。"""

from __future__ import annotations

import asyncio
import json

import httpx
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool

from src.llm.chat_model import ChatLlamaCpp
from src.llm.undeclared_tool_call import (
    _split_at_marker,
    declared_tool_names,
    parse_tool_calls_text,
    recover_undeclared_tool_calls,
)

# 2026-10-31 本番で Strata 0.2.8 が本文のまま返した応答。
_XML = "<tool_call>\n<function=search_documents>\n<parameter=query>\n2025年9月 携帯電話 料金\n</parameter>\n</function>\n</tool_call>"


def test_parse_qwen_xml():
    assert parse_tool_calls_text(_XML) == [{"name": "search_documents", "args": {"query": "2025年9月 携帯電話 料金"}}]


def test_parse_qwen_xml_several_calls_and_missing_close():
    text = (
        "<tool_call>\n<function=a>\n<parameter=x>\n1\n</parameter>\n</function>\n</tool_call>\n"
        "<tool_call>\n<function=b>\n<parameter=y>\n2\n<parameter=z>\n3\n"
    )
    assert parse_tool_calls_text(text) == [{"name": "a", "args": {"x": "1"}}, {"name": "b", "args": {"y": "2", "z": "3"}}]


def test_parse_hermes_json():
    text = '<tool_call>\n{"name": "search_documents", "arguments": {"query": "q", "top_k": 5}}\n</tool_call>'
    assert parse_tool_calls_text(text) == [{"name": "search_documents", "args": {"query": "q", "top_k": 5}}]


def test_parse_rejects_text_around_markup():
    assert parse_tool_calls_text(_XML + "\n以上の書式で呼び出します。") is None
    assert parse_tool_calls_text("<tool_call>\n{broken\n</tool_call>") is None
    assert parse_tool_calls_text("<tool_call></tool_call>") is None


def test_split_at_marker_holds_partial_marker():
    assert _split_at_marker("調べます。<tool_call>\n<fun") == ("調べます。", "<tool_call>\n<fun")
    assert _split_at_marker("調べます。<to") == ("調べます。", "<to")
    assert _split_at_marker("a < b") == ("a < b", "")


def test_declared_tool_names():
    tools = [{"type": "function", "function": {"name": "dispatch_agent"}}, {"name": "x"}, "bad"]
    assert declared_tool_names(tools) == frozenset({"dispatch_agent", "x"})
    assert declared_tool_names(None) == frozenset()


def _chunk(content: str = "", **kwargs) -> ChatGenerationChunk:
    return ChatGenerationChunk(message=AIMessageChunk(content=content, **kwargs))


def _collect(pieces: list[ChatGenerationChunk], declared: frozenset[str]) -> AIMessageChunk:
    async def source():
        for piece in pieces:
            yield piece

    async def run():
        out = [c async for c in recover_undeclared_tool_calls(source(), declared)]
        merged = out[0].message
        for c in out[1:]:
            merged = merged + c.message
        return merged

    return asyncio.run(run())


def _pieces(text: str, size: int = 7) -> list[ChatGenerationChunk]:
    return [_chunk(text[i : i + size]) for i in range(0, len(text), size)]


def test_stream_recovers_undeclared_call():
    reasoning = _chunk("", additional_kwargs={"reasoning_content": "Search knowledge first."})
    message = _collect([reasoning, *_pieces(_XML)], frozenset({"dispatch_agent"}))
    assert message.content == ""
    assert [(c["name"], c["args"]) for c in message.tool_calls] == [("search_documents", {"query": "2025年9月 携帯電話 料金"})]
    assert message.additional_kwargs["reasoning_content"] == "Search knowledge first."


def test_stream_keeps_text_before_markup():
    message = _collect(_pieces("ナレッジを検索します。\n" + _XML), frozenset({"dispatch_agent"}))
    assert message.content == "ナレッジを検索します。\n"
    assert [c["name"] for c in message.tool_calls] == ["search_documents"]


def test_stream_leaves_declared_call_as_text():
    # 宣言済みのツールをサーバーが読めなかった場合は対象外（従来どおり本文のまま）。
    message = _collect(_pieces(_XML), frozenset({"search_documents"}))
    assert message.content == _XML
    assert message.tool_calls == []


def test_stream_leaves_explanation_as_text():
    text = "書式の例: <function=foo> のように書きます。"
    message = _collect(_pieces(text), frozenset({"dispatch_agent"}))
    assert message.content == text
    assert message.tool_calls == []


def test_stream_without_declared_tools_is_untouched():
    message = _collect(_pieces(_XML), frozenset())
    assert message.content == _XML


def test_stream_with_server_tool_calls_is_untouched():
    server_call = _chunk("", tool_call_chunks=[{"name": "dispatch_agent", "args": "{}", "id": "call_1", "index": 0}])
    message = _collect([_chunk("<tool"), server_call, _chunk("_call>")], frozenset({"dispatch_agent"}))
    assert message.content == "<tool_call>"
    assert [c["name"] for c in message.tool_calls] == ["dispatch_agent"]


@tool
def dispatch_agent(task: str) -> str:
    """Delegate a task."""
    return task


def _stream_response(text: str) -> httpx.MockTransport:
    def handler(request):
        body = json.loads(request.content)
        assert [t["function"]["name"] for t in body["tools"]] == ["dispatch_agent"]
        chunks = [
            {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
             "choices": [{"index": 0, "delta": {"content": text[i : i + 5]}, "finish_reason": None}]}
            for i in range(0, len(text), 5)
        ]
        chunks.append({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        sse = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


def test_chat_model_recovers_undeclared_call():
    model = ChatLlamaCpp(
        base_url="http://fake/v1",
        api_key="x",
        model="m",
        streaming=True,
        max_retries=0,
        http_async_client=httpx.AsyncClient(transport=_stream_response(_XML)),
    ).bind_tools([dispatch_agent])
    response = asyncio.run(model.ainvoke([HumanMessage(content="2025年9月の携帯電話料金は？")]))
    assert response.content == ""
    assert [(c["name"], c["args"]) for c in response.tool_calls] == [("search_documents", {"query": "2025年9月 携帯電話 料金"})]
