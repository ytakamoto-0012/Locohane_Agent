"""ターンの始めに、ハーネスが永続メモリーを必ず検索して会話へ入れる（[memory] recall_enabled）。

LLM（特に低パラメータのローカルモデル）は、依頼を受けても search_memory を呼ばずに作業を始めることがあり、
覚えているはずの訂正・やり方を使わないまま同じ失敗を繰り返していた。メモリーの索引をシステムプロンプトに
載せるのもやめた（件数が増えるとプロンプトが膨らみ、会話を開く・最初の応答が遅くなるため）ので、
検索はハーネスが機械的に行う。

ユーザーの発言を受けたら、その本文で関係しそうなメモリーを探し（src/memory.py の relevant_memories）、
その結果を「search_memory を呼んで結果が返った」形（AIMessage の tool_call ＋ ToolMessage）で発言の直後に足す。
LLM から見ると自分で検索した後と同じ状態になるため、続けて read_memory で詳しく読む・別の語で
search_memory し直すことが自然にできる。結果には上位のメモリーの本文の先頭も添える（読みに行かなくても
訂正・やり方が目に入るように）。

本番（app.py）と評価（evals/run_case.py）が同じ関数を使う。build_recall_messages は Chainlit には依存しない。

[memory] recall_mode = explore（既定）では、検索結果をそのまま会話へ入れず、explore サブエージェントへ
強制的に委譲する（abuild_recall_messages）。キーワード検索の候補を渡して explore に read_memory で読ませ、
依頼に当てはまるメモリーの要点だけを返させる。候補の説明・抜粋の一覧をそのまま入れると、依頼と関係の
無いメモリーまでメインの会話に残り、コンテキストを汚していたため。会話へは「dispatch_agent(explore) を
呼んで結果が返った」形で入れる。

自動検索は会話の最初の発言だけで行う（app.py・evals/run_case.py が判定する）。同じ会話の2通目以降や、
スレッドを開き直して続きを送ったときは、最初の検索結果が会話に残っているため入れ直さない。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from . import memory

logger = logging.getLogger(__name__)

RECALL_MODE_EXPLORE = "explore"
RECALL_MODE_DIRECT = "direct"
RECALL_MODES = (RECALL_MODE_EXPLORE, RECALL_MODE_DIRECT)
# explore へ渡す依頼文の長さ（貼り付けた長文で委譲の task が膨らみすぎないように）。
_EXPLORE_QUERY_MAX_CHARS = 2000
# 会話に見せる dispatch_agent の task（本当の task は候補の一覧を含むため、会話には短い説明だけを残す）。
_EXPLORE_SHOWN_TASK = "Check persistent memory for what applies to the user's request (automatic, at the start of the conversation)."
_EXPLORE_NONE_ANSWER = "No memories apply to this request."

# 自動検索で足した AIMessage / ToolMessage の additional_kwargs の目印（ツール呼び出しの数え方から除くため）。
RECALL_KWARG = "locohane_memory_recall"
# 自動検索の tool_call id の接頭辞。
RECALL_ID_PREFIX = "memrecall_"
# search_memory の query として見せる依頼文の長さ（画面・ログ用。検索自体は依頼文全体で行う）。
_QUERY_SHOWN_CHARS = 200
# 検索に使う依頼文の長さ（貼り付けた長文で手がかりが増えすぎないように）。
_QUERY_MAX_CHARS = 4000

DEFAULT_MAX_RESULTS = 8
DEFAULT_EXCERPT_CHARS = 300


def recall_text(memory_root: Path, query: str, *, viewer: str | None, max_results: int, excerpt_chars: int) -> tuple[str, int]:
    """自動検索の結果の本文（ToolMessage の content）と、当たった件数。"""
    hits = memory.relevant_memories(memory_root, query[:_QUERY_MAX_CHARS], viewer=viewer, limit=max_results)
    if not hits:
        return (
            "[Automatic memory search at the start of the request] No related memories. If the request uses a "
            "term, a file or a project name you do not know, search_memory it before asking the user.",
            0,
        )
    policy = memory.policy_for(memory_root)
    lines = [
        f"[Automatic memory search at the start of the request] {len(hits)} possibly related memories "
        "(most relevant first). Use the ones that apply; read a memory in full with read_memory(name) before "
        "relying on details the excerpt does not show:"
    ]
    for mem in hits:
        lines.append(f"- [{mem.memory_type}] {mem.name}{memory.label_for(mem, viewer, policy)}: {mem.description}")
        if mem.in_system_prompt:
            # 本文はシステムプロンプトに載っている（同じ本文を毎ターン会話へ重ねて足さない）。
            lines.append("  > (its content is in the system prompt)")
        elif excerpt_chars > 0:
            flat = " ".join(mem.content.split())
            if flat:
                excerpt = flat if len(flat) <= excerpt_chars else flat[:excerpt_chars] + " ..."
                lines.append(f"  > {excerpt}")
    return "\n".join(lines), len(hits)


def build_recall_messages(
    config,
    query: str,
    viewer: str | None,
) -> tuple[list[BaseMessage], int]:
    """依頼文 query でメモリーを検索し、会話へ足す (AIMessage(tool_call), ToolMessage) と当たった件数を返す。

    [memory] recall_enabled = false・query が空・メモリーのフォルダが無い場合は ([], 0)。
    検索に失敗しても会話は止めない（警告を残して空を返す）。
    """
    if not getattr(config, "memory_recall_enabled", True) or not query.strip():
        return [], 0
    max_results = getattr(config, "memory_recall_max_results", DEFAULT_MAX_RESULTS)
    if max_results <= 0:
        # 0 件にした = 自動検索を止めた。「関係するメモリーは無い」と偽りの結果を入れない。
        return [], 0
    memory_root = getattr(config, "memory_dir", None)
    if memory_root is None or not Path(memory_root).is_dir():
        return [], 0
    try:
        content, count = recall_text(
            Path(memory_root),
            query,
            viewer=viewer,
            max_results=max_results,
            excerpt_chars=getattr(config, "memory_recall_excerpt_chars", DEFAULT_EXCERPT_CHARS),
        )
    except Exception:  # noqa: BLE001 - 検索できなくても依頼の処理は続ける
        logger.warning("Automatic memory search failed", exc_info=True)
        return [], 0
    call_id = f"{RECALL_ID_PREFIX}{uuid.uuid4().hex[:12]}"
    shown = " ".join(query.split())[:_QUERY_SHOWN_CHARS]
    call = AIMessage(
        content="",
        tool_calls=[{"name": "search_memory", "args": {"query": shown}, "id": call_id, "type": "tool_call"}],
        additional_kwargs={RECALL_KWARG: True},
    )
    result = ToolMessage(content=content, tool_call_id=call_id, name="search_memory", additional_kwargs={RECALL_KWARG: True})
    logger.info("Automatic memory search: viewer=%s hits=%d", viewer, count)
    return [call, result], count


def explore_task(query: str, hits: list, viewer: str | None, policy) -> str:
    """explore に渡す task（依頼文・キーワード検索の候補・返し方）。"""
    request = query.strip()
    if len(request) > _EXPLORE_QUERY_MAX_CHARS:
        request = request[:_EXPLORE_QUERY_MAX_CHARS] + " ..."
    lines = [
        "Check the persistent memory for what applies to the user's request below, and report only that. "
        "This runs automatically before the main assistant starts working on the request.",
        "",
        "User's request:",
        "<<<",
        request,
        ">>>",
        "",
        "Candidate memories from a keyword search (most relevant first; owner labels in parentheses):",
    ]
    for mem in hits:
        lines.append(f"- [{mem.memory_type}] {mem.name}{memory.label_for(mem, viewer, policy)}: {mem.description}")
    lines += [
        "",
        "Steps:",
        "1. read_memory the candidates that may apply to the request (skip the ones whose description clearly does not). "
        "If the request uses a term, a file or a project name that none of the candidates covers, search_memory it once.",
        "2. Use only the memory tools (read_memory, search_memory, list_memories). Do not open any other file, "
        "and do not create, update or delete memories.",
        "",
        "Final answer: for each memory that applies, one block `- name (owner label): ` followed by the facts, "
        "corrections or procedures in it that matter for this request, keeping the wording of rules and corrections "
        "(a few lines each; no general summary of the memory). If two memories conflict, say so and which one takes "
        "precedence according to the owner rank shown. Leave out memories that do not apply. "
        f'If none apply, answer only "{_EXPLORE_NONE_ANSWER}"',
    ]
    return "\n".join(lines)


async def abuild_recall_messages(config, query: str, viewer: str | None) -> tuple[list[BaseMessage], int]:
    """会話の最初の自動メモリー検索。[memory] recall_mode に従い、会話へ足すメッセージと候補の件数を返す。

    explore（既定）: キーワード検索の候補を explore サブエージェントへ渡し（dispatch_agent ツールを
    そのまま呼ぶ。進捗表示・同時実行数の制限も通常の委譲と同じになる）、その最終回答だけを
    (AIMessage(dispatch_agent の tool_call), ToolMessage) として返す。候補が無ければ委譲せず ([], 0)
    （LLM を1回余分に動かさないため。メインは必要なら自分で search_memory できる）。
    委譲に失敗したら会話は止めず ([], 0)。
    direct: build_recall_messages（検索結果をそのまま入れる従来の動き）。
    """
    if getattr(config, "memory_recall_mode", RECALL_MODE_EXPLORE) != RECALL_MODE_EXPLORE:
        return await asyncio.to_thread(build_recall_messages, config, query, viewer)
    if not getattr(config, "memory_recall_enabled", True) or not query.strip():
        return [], 0
    max_results = getattr(config, "memory_recall_max_results", DEFAULT_MAX_RESULTS)
    memory_root = getattr(config, "memory_dir", None)
    if max_results <= 0 or memory_root is None or not Path(memory_root).is_dir():
        return [], 0
    memory_root = Path(memory_root)
    try:
        hits = await asyncio.to_thread(
            memory.relevant_memories, memory_root, query[:_QUERY_MAX_CHARS], viewer=viewer, limit=max_results
        )
    except Exception:  # noqa: BLE001 - 検索できなくても依頼の処理は続ける
        logger.warning("Automatic memory search failed", exc_info=True)
        return [], 0
    # 本文がシステムプロンプトに載っているものは読ませに行かない。
    hits = [mem for mem in hits if not mem.in_system_prompt]
    if not hits:
        logger.info("Automatic memory search (explore): viewer=%s hits=0, not delegating", viewer)
        return [], 0
    task = explore_task(query, hits, viewer, memory.policy_for(memory_root))
    call_id = f"{RECALL_ID_PREFIX}{uuid.uuid4().hex[:12]}"
    try:
        # src.tools は本モジュールを（間接的に）import するため、循環を避けて関数内で import する。
        from .tools.dispatch_agent import dispatch_agent  # noqa: PLC0415

        tool_message = await dispatch_agent.ainvoke(
            {"name": "dispatch_agent", "args": {"task": task, "agent_type": "explore"}, "id": call_id, "type": "tool_call"}
        )
    except Exception:  # noqa: BLE001 - 委譲できなくても依頼の処理は続ける
        logger.warning("Automatic memory search via explore failed", exc_info=True)
        return [], 0
    answer = str(getattr(tool_message, "content", tool_message)).strip()
    if not answer or answer.startswith("Error:"):
        logger.warning("Automatic memory search via explore returned no usable answer: %r", answer[:300])
        return [], 0
    content = (
        "[Automatic memory check at the start of the conversation, done by the explore sub-agent] "
        "What applies to this request from persistent memory (read a memory in full with read_memory(name) "
        "if you need details this does not show):\n" + answer
    )
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "dispatch_agent",
                "args": {"task": _EXPLORE_SHOWN_TASK, "agent_type": "explore"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
        additional_kwargs={RECALL_KWARG: True},
    )
    result = ToolMessage(content=content, tool_call_id=call_id, name="dispatch_agent", additional_kwargs={RECALL_KWARG: True})
    logger.info("Automatic memory search (explore): viewer=%s candidates=%d", viewer, len(hits))
    return [call, result], len(hits)


def is_recall_message(message: BaseMessage) -> bool:
    """自動検索で足したメッセージか。"""
    return bool(getattr(message, "additional_kwargs", {}).get(RECALL_KWARG))
