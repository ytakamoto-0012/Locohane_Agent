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

本番（app.py）と評価（evals/run_case.py）が同じ関数を使う。Chainlit には依存しない。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from . import memory

logger = logging.getLogger(__name__)

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


def is_recall_message(message: BaseMessage) -> bool:
    """自動検索で足したメッセージか。"""
    return bool(getattr(message, "additional_kwargs", {}).get(RECALL_KWARG))
