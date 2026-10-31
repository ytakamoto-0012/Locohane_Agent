"""ツールのスキーマに無い引数名の tool_call を実行させないガード。

@tool のツールの引数スキーマ（pydantic）は既定で未知のキーを黙って捨てる。
そのため引数名を書き間違えると、その引数が無いものとして実行される。
例えば run_script の script_args を skill_args と書くと、スクリプトが引数なしで
実行されて「pdf_path is required」で落ちる。しかも argparse のエラーには
書き間違いの手掛かりが無いので、モデルは @N の解決が壊れていると誤解して
同じ書き間違いのまま呼び方を変え続けた（2026-10-31 eval、explore が4回以上失敗）。

ここでは未知の引数名を見つけたら実行せず、正しい引数名（近い名前があれば
その候補）を添えたエラーを返す。メインエージェントの ImageAwareToolNode
（src/tools/tool_node.py）とサブエージェントの _run_one_tool_call
（src/subagent.py）の両方から使う。
"""

from __future__ import annotations

import difflib

from langchain_core.tools import BaseTool


def _accepts_extra_args(tool: BaseTool) -> bool:
    """スキーマが未知の引数を受け付けるか。

    MCP ツール等の JSON Schema（dict）は、additionalProperties を書かなければ
    未知の引数を許す（JSON Schema の既定）ので、明示的に false のときだけ弾く。
    """
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        # スキーマを持たない（引数を確かめられない）ツールは対象外。
        return True
    if isinstance(schema, dict):
        return schema.get("additionalProperties", True) is not False
    model_config = getattr(schema, "model_config", None) or {}
    return model_config.get("extra") == "allow"


def find_unknown_tool_args(tool: BaseTool | None, args) -> list[str]:
    """args のうち tool のスキーマに無い引数名を返す（無ければ空リスト）。"""
    if tool is None or not isinstance(args, dict) or _accepts_extra_args(tool):
        return []
    known = tool.args
    return [key for key in args if key not in known]


def unknown_tool_args_error(tool: BaseTool, unknown: list[str], args: dict) -> str:
    """find_unknown_tool_args が見つけた場合に返すエラー文。

    書き間違えた引数の候補は、まだ渡していない引数から選ぶ（skill_args なら、
    渡し済みの skill_name ではなく script_args）。
    """
    known = list(tool.args)
    missing = [key for key in known if key not in args]
    hints = []
    for key in unknown:
        match = difflib.get_close_matches(key, missing, n=1, cutoff=0.5)
        if match:
            hints.append(f"{key} -> {match[0]}")
    hint = f" Did you mean: {', '.join(hints)}?" if hints else ""
    return (
        f"Error: {tool.name} has no argument {', '.join(unknown)}. The tool was not run."
        f" Valid arguments: {', '.join(known) or '(none)'}.{hint} Call it again with the correct argument names."
    )
