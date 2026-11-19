"""タスクの終わりに、ハーネスがメモリーへの記録を必ず見直させる（[memory] review_enabled）。

LLM はタスクを終えると、ユーザーの訂正・うまくいったやり方をメモリーに残すのを忘れることが多い
（システムプロンプトで「保存せよ」と書くだけでは、最終回答を書いた時点で終わってしまう）。そこで、
ツールを使ったタスクが終わり、そのターンの間にメモリーが1件も保存・更新されていなければ
（メインでもサブエージェントでも。src/memory.py の written_since でファイルの更新日時を見る）、
ハーネスが記録係を1回だけ動かす。

記録係はメインのグラフとは別の、小さな ReAct ループ（最大 [memory] review_max_iterations 回のモデル呼び出し）:
- システムプロンプトは system_prompt/memory_review.md（保存するもの・しないものの基準と手順）。
- 入力はこのターンの記録（ユーザーの依頼・追加指示、ツール呼び出しと結果の要約、最終回答）と、
  関係しそうな既存のメモリー（重複を作らず更新させるため）。
- 使えるツールはメモリー系だけ（search_memory / read_memory / create_memory / update_memory / save_glossary_term）。
- 会話の履歴には何も足さない（記録係のやり取りは次のターンの LLM へ送らない。保存したメモリー自体はファイルに残る）。

失敗しても（LLM に繋がらない・時間切れ等）ターンの結果には影響させず、警告を残すだけにする。
Chainlit には依存しない（ツールが会話のユーザーを cl.user_session から読むのは呼び出し側の文脈のまま）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from . import memory
from .memory_recall import is_recall_message

logger = logging.getLogger(__name__)

DEFAULT_MIN_TOOL_CALLS = 1
DEFAULT_MAX_ITERATIONS = 4
DEFAULT_TIMEOUT_SECONDS = 300

# 記録に載せる量の上限（記録係のプロンプトが長くなりすぎないように）。
_REQUEST_MAX_CHARS = 3000
_ANSWER_MAX_CHARS = 3000
_TOOL_ARGS_MAX_CHARS = 300
_TOOL_RESULT_MAX_CHARS = 300
_TOOL_CALLS_MAX = 40
_RELATED_MAX = 8

# 記録係に渡すツール（メモリー系だけ）。
_REVIEW_TOOL_NAMES = ("search_memory", "read_memory", "create_memory", "update_memory", "save_glossary_term")
# 保存した（書き込みに成功した）とみなすツール。
_WRITE_TOOL_NAMES = frozenset({"create_memory", "update_memory", "save_glossary_term"})
# このターンでメモリーを書いたとみなすツール（メインエージェントが直接呼んだ場合）。
_TURN_WRITE_TOOL_NAMES = _WRITE_TOOL_NAMES | {"delete_memory"}


@dataclass
class ReviewResult:
    """記録係の結果。

    Attributes:
        saved: 保存・更新したメモリー（名前。用語は用語そのもの）。
        reply: 記録係の最後の返事（"Saved: ..." / "Nothing to save" 等）。
        usage: LLM のトークン使用量の合計（input / output / total）。
        error: 失敗したときの理由（成功なら None）。
    """

    saved: list[str] = field(default_factory=list)
    reply: str = ""
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})
    error: str | None = None


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " ...(cut)"


def main_tool_calls(turn_messages: list[BaseMessage]) -> list[dict]:
    """このターンでメインエージェントが呼んだツール（自動検索の分は除く）。"""
    calls: list[dict] = []
    for m in turn_messages:
        if isinstance(m, AIMessage) and not is_recall_message(m):
            calls.extend(m.tool_calls or [])
    return calls


def should_review(config, turn_messages: list[BaseMessage], *, memory_written: bool) -> bool:
    """記録係を動かすか（設定が有効・ツールを十分使ったタスク・このターンでメモリーを書いていない）。"""
    if not getattr(config, "memory_review_enabled", True) or memory_written:
        return False
    calls = main_tool_calls(turn_messages)
    if any(c.get("name") in _TURN_WRITE_TOOL_NAMES for c in calls):
        return False
    return len(calls) >= getattr(config, "memory_review_min_tool_calls", DEFAULT_MIN_TOOL_CALLS)


def build_turn_record(turn_messages: list[BaseMessage]) -> tuple[str, str]:
    """(記録係に渡すこのターンの記録, 関係するメモリーを探すための文) を返す。"""
    requests: list[str] = []
    results: dict[str, str] = {}
    for m in turn_messages:
        if isinstance(m, ToolMessage) and not is_recall_message(m):
            results[m.tool_call_id] = _text_of(m.content)
    tool_lines: list[str] = []
    final_answer = ""
    for m in turn_messages:
        if isinstance(m, HumanMessage):
            text = _text_of(m.content)
            if text.strip():
                requests.append(_cut(text, _REQUEST_MAX_CHARS))
        elif isinstance(m, AIMessage) and not is_recall_message(m):
            for call in m.tool_calls or []:
                if len(tool_lines) >= _TOOL_CALLS_MAX:
                    continue
                try:
                    args = json.dumps(call.get("args") or {}, ensure_ascii=False)
                except (TypeError, ValueError):
                    args = str(call.get("args"))
                result = results.get(call.get("id") or "", "(no result)")
                tool_lines.append(
                    f"- {call.get('name')}({_cut(args, _TOOL_ARGS_MAX_CHARS)})\n  -> {_cut(' '.join(result.split()), _TOOL_RESULT_MAX_CHARS)}"
                )
            if not m.tool_calls and _text_of(m.content).strip():
                final_answer = _text_of(m.content)
    calls_total = len(main_tool_calls(turn_messages))
    parts = ["## User's request and additional instructions", "\n\n---\n\n".join(requests) or "(none)"]
    parts += ["", f"## Work done (tool calls, {calls_total} in total)", "\n".join(tool_lines) or "(none)"]
    if calls_total > len(tool_lines):
        parts.append(f"... ({calls_total - len(tool_lines)} more calls not shown)")
    parts += ["", "## Final answer to the user", _cut(final_answer, _ANSWER_MAX_CHARS) or "(none)"]
    search_text = "\n".join(requests) + "\n" + final_answer[:1000]
    return "\n".join(parts), search_text


def _related_block(memory_root: Path, search_text: str, viewer: str | None) -> str:
    try:
        hits = memory.relevant_memories(memory_root, search_text, viewer=viewer, limit=_RELATED_MAX)
    except Exception:  # noqa: BLE001 - 見つからなくても見直しは続ける
        logger.debug("memory review: failed to find related memories", exc_info=True)
        hits = []
    if not hits:
        return "(none found)"
    policy = memory.policy_for(memory_root)
    return "\n".join(f"- [{m.memory_type}] {m.name}{memory.label_for(m, viewer, policy)}: {m.description}" for m in hits)


def review_system_prompt(config, language_directive: str = "") -> str:
    """記録係のシステムプロンプト（system_prompt/memory_review.md に公開範囲の説明と応答言語の指示を足したもの）。"""
    from .config import render_memory_visibility_block

    path = Path(config.system_prompt_path).parent / "memory_review.md"
    text = path.read_text(encoding="utf-8")
    try:
        visibility = render_memory_visibility_block(config)
    except Exception:  # noqa: BLE001 - テスト用の簡易な設定でも動くように
        visibility = "Visibility is set by the memory type."
    return text.replace("{{memory_visibility}}", visibility) + language_directive


def _review_tools() -> list:
    from .tools.glossary_tools import save_glossary_term
    from .tools.memory_tools import create_memory, read_memory, search_memory, update_memory

    tools = {t.name: t for t in (search_memory, read_memory, create_memory, update_memory, save_glossary_term)}
    return [tools[n] for n in _REVIEW_TOOL_NAMES]


def _add_usage(totals: dict, response: AIMessage) -> None:
    usage = getattr(response, "usage_metadata", None) or {}
    totals["input"] += int(usage.get("input_tokens") or 0)
    totals["output"] += int(usage.get("output_tokens") or 0)
    totals["total"] += int(usage.get("total_tokens") or 0)


async def run_memory_review(
    config,
    turn_messages: list[BaseMessage],
    *,
    viewer: str | None,
    language_directive: str = "",
    model=None,
) -> ReviewResult:
    """このターンの記録を記録係に見せ、残すべきものをメモリーへ保存させる。

    Args:
        config: アプリ設定。
        turn_messages: このターンのメッセージ（ユーザーの発言から最終回答まで）。
        viewer: 会話のユーザー（関係するメモリーを探すのに使う。保存はツールが会話のユーザーとして行う）。
        language_directive: 応答言語の指示（src/i18n.py の response_language_directive）。
        model: テスト用の差し替え口（bind_tools できるチャットモデル）。None なら build_model(config, role="sub")。
    """
    result = ReviewResult()
    record, search_text = build_turn_record(turn_messages)
    memory_root = Path(config.memory_dir)
    human = (
        f"{record}\n\n## Related memories already saved\n{_related_block(memory_root, search_text, viewer)}\n\n"
        "Decide what to save, following your instructions."
    )
    messages: list[BaseMessage] = [SystemMessage(content=review_system_prompt(config, language_directive)), HumanMessage(content=human)]
    tools = _review_tools()
    tools_by_name = {t.name: t for t in tools}
    max_iterations = max(1, int(getattr(config, "memory_review_max_iterations", DEFAULT_MAX_ITERATIONS)))
    timeout = float(getattr(config, "memory_review_timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    try:
        async with asyncio.timeout(timeout if timeout > 0 else None):
            if model is None:
                from .llm import build_model

                model = await build_model(config, role="sub", wait_when_busy=True)
            bound = model.bind_tools(tools)
            for _ in range(max_iterations):
                response = await bound.ainvoke(messages)
                _add_usage(result.usage, response)
                messages.append(response)
                calls = getattr(response, "tool_calls", None) or []
                if not calls:
                    result.reply = _text_of(response.content).strip()
                    break
                for call in calls:
                    name = call.get("name") or ""
                    args = call.get("args") or {}
                    tool = tools_by_name.get(name)
                    if tool is None:
                        output = f"Error: unknown tool {name!r}. Use only: {', '.join(_REVIEW_TOOL_NAMES)}."
                    else:
                        try:
                            output = str(await tool.ainvoke(args))
                        except Exception as exc:  # noqa: BLE001 - 引数の誤り等は記録係に返して直させる
                            output = f"Error: {exc}"
                    if name in _WRITE_TOOL_NAMES and not output.startswith("Error"):
                        saved = args.get("term") if name == "save_glossary_term" else args.get("name")
                        if saved and saved not in result.saved:
                            result.saved.append(str(saved))
                    messages.append(ToolMessage(content=output, tool_call_id=call.get("id") or "", name=name))
    except TimeoutError:
        result.error = f"timed out after {timeout:.0f} seconds"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 見直しの失敗でターンを失敗させない
        logger.warning("Memory review failed", exc_info=True)
        result.error = f"{type(exc).__name__}: {exc}"
    logger.info(
        "Memory review: saved=%s error=%s tokens=%d",
        result.saved,
        result.error,
        result.usage["total"],
    )
    return result
