"""スキーマに無い引数名の tool_call を実行させないガードのテスト（src/tool_args_guard.py）。"""

from __future__ import annotations

import asyncio

from langchain_core.tools import StructuredTool, tool

from src.subagent import _run_one_tool_call
from src.tool_args_guard import find_unknown_tool_args, unknown_tool_args_error
from src.tools.run_script import run_script
from src.tools.tool_node import _guard_unknown_tool_args

_CALLS: list[dict] = []


@tool
def fake_script(skill_name: str, script_filename: str, script_args: list[str] | None = None) -> str:
    """Run a script."""
    _CALLS.append({"skill_name": skill_name, "script_filename": script_filename, "script_args": script_args})
    return "ok"


def test_find_unknown_args_on_run_script():
    # 2026-10-31 eval で explore が script_args を skill_args と書いた呼び出し。
    args = {"skill_args": ["@17"], "script_filename": "read_pdf.py", "skill_name": "pdf-tools"}
    assert find_unknown_tool_args(run_script, args) == ["skill_args"]
    assert find_unknown_tool_args(run_script, {"skill_name": "a", "script_filename": "b", "script_args": []}) == []
    assert find_unknown_tool_args(None, args) == []


def test_error_suggests_close_name():
    args = {"skill_args": ["@17"], "script_filename": "read_pdf.py", "skill_name": "pdf-tools"}
    message = unknown_tool_args_error(run_script, ["skill_args"], args)
    assert "run_script has no argument skill_args" in message
    assert "Valid arguments: skill_name, script_filename, script_args" in message
    assert "skill_args -> script_args" in message


def test_json_schema_tools_follow_additional_properties():
    def _func(**kwargs):
        return "ok"

    def _tool(schema: dict) -> StructuredTool:
        return StructuredTool(name="mcp__x__t", description="t", args_schema=schema, func=_func)

    base = {"type": "object", "properties": {"q": {"type": "string"}}}
    assert find_unknown_tool_args(_tool(base), {"q": "a", "extra": 1}) == []
    assert find_unknown_tool_args(_tool({**base, "additionalProperties": False}), {"q": "a", "extra": 1}) == ["extra"]


def test_tool_node_guard_blocks_unknown_args():
    call = {"name": "fake_script", "args": {"skill_name": "s", "script_filename": "f.py", "skill_args": ["x"]}, "id": "c1"}
    blocked = _guard_unknown_tool_args({"__type": "tool_call_with_context", "tool_call": call}, {"fake_script": fake_script})
    message = blocked["messages"][0]
    assert message.status == "error"
    assert message.tool_call_id == "c1"
    assert "skill_args -> script_args" in message.content
    ok_call = {**call, "args": {"skill_name": "s", "script_filename": "f.py", "script_args": ["x"]}}
    assert _guard_unknown_tool_args({"__type": "tool_call_with_context", "tool_call": ok_call}, {"fake_script": fake_script}) is None


def test_subagent_does_not_run_with_unknown_args():
    _CALLS.clear()
    call = {"name": "fake_script", "args": {"skill_name": "s", "script_filename": "f.py", "skill_args": ["x"]}, "id": "c1"}
    message, _ = asyncio.run(_run_one_tool_call(call, {"fake_script": fake_script}))
    assert _CALLS == []
    assert message.status == "error"
    assert "skill_args -> script_args" in message.content


def test_script_args_hint_for_empty_args():
    from src.tools._script_job import _EMPTY_SCRIPT_ARGS_HINT, _SCRIPT_ARGS_HINT, _script_args_hint

    assert _script_args_hint(None) == _EMPTY_SCRIPT_ARGS_HINT
    assert _script_args_hint([]) == _EMPTY_SCRIPT_ARGS_HINT
    assert _script_args_hint(["@17"]) == _SCRIPT_ARGS_HINT
