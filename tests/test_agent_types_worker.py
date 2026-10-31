"""agents/worker.md（書き込み可能な作業用サブエージェント）の回帰テスト。

explore は読み取り専用の境界（execute_python_code は持たない）を維持する
必要がある一方、worker はそれに加えて execute_python_code も持ち、承認済み
計画のもとで成果ファイルを書き出せる必要がある。explore は office/PDF調査用に
run_script も持つが、_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST により読み取り専用の
read_*.py/render_*.py 系スキルのみへ制限されている（書き込み系スクリプトは
コード側でもブロックされる。詳細は test_tools_run_script_agent_type_skill_allowlist.py）。
scan_agent_types() がこれらを正しく読み分け、_resolve_agent_types() が
意図したツール集合へ解決することを検証する。
"""

from pathlib import Path

from src.agent_types import scan_agent_types
from src.tools import _resolve_agent_types

_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


def test_worker_agent_definition_is_discovered() -> None:
    agent_types = scan_agent_types(_AGENTS_DIR)
    names = {a.name for a in agent_types}

    assert "worker" in names


def test_worker_resolves_with_write_tools() -> None:
    agent_types = scan_agent_types(_AGENTS_DIR)
    resolved = _resolve_agent_types(agent_types)

    worker_tool_names = {t.name for t in resolved["worker"].tools}

    assert "execute_python_code" in worker_tool_names
    assert "run_script" in worker_tool_names
    assert "analyze_image" in worker_tool_names


def test_explore_remains_read_only() -> None:
    # worker追加が既存の読み取り専用境界を壊していないことの回帰確認。
    # explore は office/PDF調査用に run_script を持つが、書き込み系
    # execute_python_code は持たない。run_script 自体も
    # _AGENT_TYPE_RUN_SCRIPT_ALLOWLIST で読み取り専用スキルのみへ制限される
    # （test_tools_run_script_agent_type_skill_allowlist.py 参照）。
    agent_types = scan_agent_types(_AGENTS_DIR)
    resolved = _resolve_agent_types(agent_types)

    explore_tool_names = {t.name for t in resolved["explore"].tools}

    assert "execute_python_code" not in explore_tool_names
    assert "run_script" in explore_tool_names
