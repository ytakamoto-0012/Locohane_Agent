"""本文に書かれた「宣言していないツール」の呼び出しを tool_call に組み直す。

モデルがリクエストで宣言していないツール（例: [main_agent_tool_guard] で
メインエージェントから外した search_documents）を、システムプロンプトや
過去の履歴に釣られて呼ぶことがある。推論サーバーのツールコールのパーサーは
宣言済みのツールしか tool_calls にしないため（Strata 0.2.8 以降は明示的に
`strata_rejected_tool_calls: [{"reason": "undeclared"}]` を付けて本文のまま
返す）、`<tool_call><function=..>` の XML が本文として届き、tool_calls が
空なので最終回答として画面に出てターンが終わっていた（2026-10-31 本番）。

ここでは本文のうち tool_call の書式の部分を画面へ流さずに保留し、ストリームの
終わりに、保留した部分が**全て宣言していないツールの呼び出し**として読めた
場合だけ tool_call_chunks に置き換える。tool_call になれば、実行側
（メインの _guard_main_agent_tool_limit・ToolNode・サブエージェントの
_run_one_tool_call）が「そのツールは使えない」というエラーの ToolMessage を
返し、モデルは次の手で委譲などに切り替えられる。宣言済みのツールが混ざる・
書式として読めない場合は、保留した本文をそのまま流す（従来どおり）。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Iterable

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

logger = logging.getLogger(__name__)

# 本文の中で tool_call の書式が始まる目印（Qwen 系の XML 書式・Hermes 系の JSON 書式）。
_MARKERS = ("<tool_call>", "<function=")

# Qwen 系: <function=NAME> <parameter=P>VALUE</parameter> ... </function>
# （</function> の書き忘れは末尾まで、</parameter> の書き忘れは次の <parameter= / </function> まで）。
_FUNCTION_RE = re.compile(r"<function=([^>\s]+)>(.*?)(?:</function>|(?=<function=)|$)", re.DOTALL)
_PARAMETER_RE = re.compile(r"<parameter=([^>\s]+)>(.*?)(?:</parameter>|(?=<parameter=)|(?=</function>)|$)", re.DOTALL)
# Hermes 系: <tool_call>{"name": ..., "arguments": {...}}</tool_call>
_JSON_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|$)", re.DOTALL)
# 読み取った呼び出しを取り除いた残りに許す文字（包みのタグと空白だけ）。
_WRAPPER_RE = re.compile(r"</?tool_call>|\s")


def declared_tool_names(tools: Iterable | None) -> frozenset[str]:
    """リクエストの tools（OpenAI 形式の dict の列）から宣言したツール名を取り出す。"""
    names = set()
    for tool in tools or ():
        if isinstance(tool, dict):
            name = (tool.get("function") or {}).get("name") or tool.get("name")
            if name:
                names.add(name)
    return frozenset(names)


def _strip_value(value: str) -> str:
    # 書式上の改行（<parameter=x>\nVALUE\n</parameter>）だけを落とす。
    if value.startswith("\n"):
        value = value[1:]
    if value.endswith("\n"):
        value = value[:-1]
    return value


def parse_tool_calls_text(text: str) -> list[dict] | None:
    """本文の tool_call の書式を読み、[{"name", "args"}] を返す。

    呼び出しを取り除いた残りに包みのタグ・空白以外が残る場合（説明文の中の
    書式の例など）や、1件も読めない場合は None。
    """
    calls: list[dict] = []
    rest = text
    if "<function=" in text:
        for match in _FUNCTION_RE.finditer(text):
            body = match.group(2)
            if _PARAMETER_RE.sub("", body).strip():
                return None
            args = {p.group(1): _strip_value(p.group(2)) for p in _PARAMETER_RE.finditer(body)}
            calls.append({"name": match.group(1), "args": args})
        rest = _FUNCTION_RE.sub("", text)
    else:
        for match in _JSON_CALL_RE.finditer(text):
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
            args = data.get("arguments") if isinstance(data, dict) else None
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    return None
            if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not isinstance(args, (dict, type(None))):
                return None
            calls.append({"name": data["name"], "args": args or {}})
        rest = _JSON_CALL_RE.sub("", text)
    if not calls or _WRAPPER_RE.sub("", rest):
        return None
    return calls


def _split_at_marker(text: str) -> tuple[str, str]:
    """text を (そのまま流してよい部分, 保留する部分) に分ける。

    目印が現れたらそこから後ろを保留する。目印が無くても、末尾が目印の書き出し
    （"<tool" など）になっていれば、次のチャンクで目印になりうるので保留する。
    """
    positions = [i for i in (text.find(m) for m in _MARKERS) if i >= 0]
    if positions:
        i = min(positions)
        return text[:i], text[i:]
    for i in range(max(0, len(text) - max(map(len, _MARKERS)) + 1), len(text)):
        if any(m.startswith(text[i:]) for m in _MARKERS):
            return text[:i], text[i:]
    return text, ""


def _content_chunk(template: ChatGenerationChunk, text: str) -> ChatGenerationChunk:
    return ChatGenerationChunk(message=AIMessageChunk(content=text, id=template.message.id))


async def recover_undeclared_tool_calls(
    chunks: AsyncIterator[ChatGenerationChunk], declared: frozenset[str]
) -> AsyncIterator[ChatGenerationChunk]:
    """ストリームの本文から未宣言ツールの呼び出しを tool_call_chunks に組み直す。

    本文に tool_call の書式が現れたら、そこから後ろの本文を保留して流さない
    （思考・usage など本文以外はそのまま流す）。サーバーが tool_calls を返した
    応答は対象にしない。

    Args:
        chunks: ChatOpenAI._astream が返すチャンクの列。
        declared: リクエストで宣言したツール名。空なら何もしない。

    Yields:
        チャンク。組み直した場合は最後に tool_call_chunks だけを持つチャンク。
    """
    if not declared:
        async for chunk in chunks:
            yield chunk
        return
    held = ""
    holding = False  # 目印を見つけ、以後の本文を全て保留している
    server_tool_calls = False
    last: ChatGenerationChunk | None = None
    async for chunk in chunks:
        last = chunk
        message = chunk.message
        if getattr(message, "tool_call_chunks", None) and not server_tool_calls:
            # サーバーが tool_calls を返した応答は組み直さない。保留分も本文へ戻す。
            server_tool_calls = True
            if held:
                yield _content_chunk(chunk, held)
                held, holding = "", False
        content = message.content if isinstance(message.content, str) else ""
        if not content or server_tool_calls:
            yield chunk
            continue
        if holding:
            held += content
            passthrough = ""
        else:
            passthrough, held = _split_at_marker(held + content)
            holding = any(held.startswith(m) for m in _MARKERS)
        if passthrough == content:
            yield chunk
            continue
        # 本文以外（思考・usage・response_metadata）を失わないよう、本文だけを差し替えて流す。
        yield ChatGenerationChunk(
            message=message.model_copy(update={"content": passthrough}),
            generation_info=chunk.generation_info,
        )
    if not held or last is None:
        return
    calls = parse_tool_calls_text(held) if holding else None
    if calls and not any(c["name"] in declared for c in calls):
        logger.warning(
            "Recovered tool calls to undeclared tools from the response text (the server left them as text): %s",
            [c["name"] for c in calls],
        )
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                id=last.message.id,
                tool_call_chunks=[
                    {
                        "name": c["name"],
                        "args": json.dumps(c["args"], ensure_ascii=False),
                        "id": f"call_{uuid.uuid4().hex[:24]}",
                        "index": i,
                    }
                    for i, c in enumerate(calls)
                ],
            )
        )
        return
    yield _content_chunk(last, held)
