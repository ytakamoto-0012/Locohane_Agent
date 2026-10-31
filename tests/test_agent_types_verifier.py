"""agents/verifier.md（検証専用のサブエージェント）の回帰テスト。

背景（2026-08-24）: verifier は生成・編集済み成果物の検証を担当するにも
関わらず、`search_memory`/`list_memories`/`read_memory` を一切持っていなかった。
一方で全サブエージェント共通の system_prompt/subagent_common.md は
「作業前にsearch_memory/list_memoriesで関連する過去メモリーの有無を確認する」
ことを条件句なしの必須ルールとして課しており、verifierだけこれに構造的に
従えない状態だった。explore/analyze-docsと同様、メモリーの参照系3ツールを
付与し、書き込み系（create_memory/update_memory/delete_memory）は持たない
「参照専用」の位置付けに揃える。
"""

from pathlib import Path

from src.agent_types import scan_agent_types
from src.tools import _resolve_agent_types

_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


def test_verifier_agent_definition_is_discovered() -> None:
    agent_types = scan_agent_types(_AGENTS_DIR)
    names = {a.name for a in agent_types}

    assert "verifier" in names


def test_verifier_resolves_with_read_only_memory_tools() -> None:
    agent_types = scan_agent_types(_AGENTS_DIR)
    resolved = _resolve_agent_types(agent_types)

    verifier_tool_names = {t.name for t in resolved["verifier"].tools}

    assert "search_memory" in verifier_tool_names
    assert "list_memories" in verifier_tool_names
    assert "read_memory" in verifier_tool_names
    assert "create_memory" not in verifier_tool_names
    assert "update_memory" not in verifier_tool_names
    assert "delete_memory" not in verifier_tool_names


def test_verifier_resolves_without_write_or_execution_tools() -> None:
    agent_types = scan_agent_types(_AGENTS_DIR)
    resolved = _resolve_agent_types(agent_types)

    verifier_tool_names = {t.name for t in resolved["verifier"].tools}

    assert "execute_python_code" not in verifier_tool_names
    assert "run_script" in verifier_tool_names
