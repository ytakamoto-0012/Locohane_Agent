#!/usr/bin/env python3
"""
メインエージェントの1ターン目リクエストが実際に何トークンで構成されて
いるかを、構成要素ごとに分解して測定するスクリプト。

compare_tokens.py は system_prompt.md 単体（プレースホルダー未展開）しか
比較しないため、実際に app.py が組み立てる本番の system_prompt（{{skills}}/
{{memory}}/{{agent_types}}/{{plan_approval_exempt_scripts}}/
{{project_instructions}}を差し込み・${...}を展開したもの）や、bind_tools()
で追加される tools フィールドの分がどれだけ効いているかは分からない。

本スクリプトは app.py の @cl.on_chat_start 相当の組み立て手順
（scan_skills → build_system_prompt_from_block → 各種 {{...}} 差込 →
expand_config_vars）をそのまま呼び出し、以下の内訳で実際に llama-server へ
送信して usage.prompt_tokens を比較する:

  1. system_prompt_raw          : system_prompt.md をプレースホルダー未展開のまま
  2. skills_block                : {{skills}}へ差し込まれるスキル一覧テキストのみ
  3. builtin_tools                : メインエージェントへ実際にbindされる
                                    ビルトインツール（tools フィールドのみ、
                                    system messageなし）
  4. production_system_prompt     : 本番同様に全プレースホルダーを展開した
                                    system_prompt単体（toolsフィールドなし）
  5. production_full              : production_system_prompt + builtin_tools
                                    （実際の1ターン目リクエストに最も近い構成）

上記メインエージェント分に加え、dispatch_agent 配下の各サブエージェント種別
（agents/*.md）についても、app.py と同じ手順（{{skills}}/{{agent_types}}/
{{run_script_allowlist}}差込＋末尾に subagent_common.md 連結）で組み立てた
本番相当の system_prompt と、そのagent_typeへ実際にbindされるツール
（frontmatterの tools: 省略時は _SUBAGENT_TOOLS 全量を継承）を、種別ごとに
分けて計測する（[subagent:<name>] raw_body / production_full）。
subagent_common.md 自体も、${...}未展開の生ファイルと展開後の両方を単体で
計測する（[subagent] subagent_common_raw / subagent_common_expanded）。

接続先は config.ini の [llm] main_url（1件目）を使う。事前に llama-server を
起動しておくこと。MCPサーバー由来の動的ツールは含まない（init_mcp_tools()を
呼ばないため、get_all_tools()はビルトイン分のみを返す）。

system_prompt.md の参照元: load_config() が読む config.ini の
[paths] system_prompt_path（既定 ./system_prompt/system_prompt.md）をそのまま
使う。すなわち compressing_test/ 配下のコピー（original/system_prompt.md 等）
ではなく、アプリが実際に読み込む本番の system_prompt/system_prompt.md 本体が
対象になる。compressing_test/ 配下の削減案ドラフト等を測りたい場合は、
config.ini の system_prompt_path を一時的に差し替えるか、
build_production_system_prompt() の呼び出し元で config.system_prompt_path を
上書きしてから呼ぶこと。
"""

from __future__ import annotations

import sys
from pathlib import Path

from langchain_core.utils.function_calling import convert_to_openai_tool
from openai import OpenAI

# Windows のコンソール（既定cp932）でも日本語出力が文字化けしないようにする。
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent_types import render_agent_types_block, scan_agent_types  # noqa: E402
from src.config import (  # noqa: E402
    expand_config_vars,
    load_config,
    render_agent_type_run_script_allowlist_block,
    render_plan_approval_exempt_scripts_block,
)
from src.memory import render_memory_block  # noqa: E402
from src.tools.vector_db_tools import render_document_collections_block  # noqa: E402
from src.project_instructions import render_project_instructions_block  # noqa: E402
from src.skills import (  # noqa: E402
    build_system_prompt_from_block,
    filter_skills_for_agent_type,
    filter_skills_for_main_agent_guard,
    render_skills_block,
    render_skills_block_with_guard_annotation,
    render_skills_block_with_hint,
    scan_skills,
)
from src.tools import (  # noqa: E402
    filter_main_agent_tools,
    get_all_tools,
    list_blocked_tool_names_for_hint,
    registry,
)

# 全ファイル共通で送る簡単な質問。system プロンプト側の差分だけを
# 比較対象にするため、内容はどのファイルでも固定する。
TEST_QUESTION = "こんにちは。あなたの役割を一文で教えてください。"

REQUEST_TIMEOUT_SECONDS = 120.0


def load_endpoint(config) -> tuple[str, str, str]:
    """config.ini の [llm] main_url（1件目）から接続先を読み取る。"""
    first = config.main_endpoints[0]
    return first.base_url, first.api_key or "dummy-not-used", first.model


def count_prompt_tokens(
    client: OpenAI,
    model: str,
    *,
    system_text: str | None,
    tools: list[dict] | None,
) -> int:
    """system_text/tools を指定した質問と合わせて送信し、prompt_tokensを返す。

    system_text が None の場合は system メッセージ自体を付けない
    （ビルトインツール単体の overhead を測るため）。
    """
    messages = []
    if system_text is not None:
        messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": TEST_QUESTION})
    kwargs: dict = {"model": model, "messages": messages, "max_tokens": 1}
    if tools:
        kwargs["tools"] = tools
    response = client.chat.completions.create(**kwargs)
    if response.usage is None:
        raise RuntimeError("レスポンスに usage 情報が含まれていません（llama-server起動時に有効化されているか確認してください）")
    return response.usage.prompt_tokens


def build_main_skills_block(skills, config) -> str:
    """app.py と同じ分岐で、メインエージェント用の{{skills}}差込テキストを組み立てる。"""
    if config.main_agent_tool_guard_visibility_mode in ("all", "hint"):
        if config.main_agent_tool_guard_visibility_mode == "all":
            return render_skills_block_with_guard_annotation(skills, config)
        return render_skills_block_with_hint(skills, config)
    return render_skills_block(filter_skills_for_main_agent_guard(skills, config))


def build_blocked_tools_hint(config) -> str:
    """app.py と同じ分岐で、{{main_agent_blocked_tools_hint}}差込テキストを組み立てる。"""
    if config.main_agent_tool_guard_visibility_mode not in ("all", "hint"):
        return ""
    blocked_names = list_blocked_tool_names_for_hint(get_all_tools(), config)
    if not blocked_names:
        return ""
    return "以下のビルトインツールは直接呼び出せません（詳細確認・実行が必要な場合は" "dispatch_agentへ委譲してください）: " + "、".join(
        blocked_names
    )


def build_production_system_prompt(config) -> tuple[str, str]:
    """app.py の @cl.on_chat_start と同じ手順で、メインエージェント用の
    本番相当system_promptを組み立てる。

    Returns:
        (完成した system_prompt, {{skills}}へ差し込まれたテキスト単体)
    """
    skills = scan_skills([config.skills_dir, *config.locohane_skills_dirs])
    main_skills_block = build_main_skills_block(skills, config)
    blocked_tools_hint = build_blocked_tools_hint(config)

    system_prompt = build_system_prompt_from_block(main_skills_block, config.system_prompt_path).replace(
        "{{main_agent_blocked_tools_hint}}", blocked_tools_hint
    )

    agent_type_defs = scan_agent_types([config.agents_dir, *config.locohane_agents_dirs])
    agent_types_block = render_agent_types_block(agent_type_defs)

    system_prompt = system_prompt.replace("{{memory}}", render_memory_block(config.memory_dir))
    system_prompt = system_prompt.replace("{{agent_types}}", agent_types_block)
    system_prompt = system_prompt.replace("{{document_collections}}", render_document_collections_block(config))
    system_prompt = system_prompt.replace(
        "{{plan_approval_exempt_scripts}}",
        render_plan_approval_exempt_scripts_block(config.plan_approval_exempt_scripts),
    )
    system_prompt = expand_config_vars(system_prompt, config)
    system_prompt = system_prompt.replace(
        "{{project_instructions}}",
        render_project_instructions_block(config.project_instructions_paths),
    )
    return system_prompt, main_skills_block


def build_main_agent_openai_tools(config) -> list[dict]:
    """メインエージェントへ実際にbindされるビルトインツールを、
    OpenAI Function Calling形式（tools フィールド）へ変換して返す。
    """
    main_tools = filter_main_agent_tools(get_all_tools(), config)
    return [convert_to_openai_tool(t) for t in main_tools]


def build_subagent_common_expanded(config) -> str:
    """subagent_common.md の ${...} を展開したテキストを返す（app.py と同じ
    手順。各エージェント種別のsystem_promptの末尾に連結される形そのもの）。
    """
    return expand_config_vars(
        (config.system_prompt_path.parent / "subagent_common.md").read_text(encoding="utf-8"),
        config,
    )


def build_subagent_agent_types(config) -> list[dict]:
    """app.py の @cl.on_chat_start と同じ手順で、agents/*.md（dispatch_agent の
    agent_type）ごとに本番相当の system_prompt・bind対象ツールを組み立てる。

    {{skills}}は主エージェント用ガード絞り込みを掛けず、
    agent_type_run_script_allowlist 登録がある種別だけ登録スキルへ絞った版
    （app.py と同じ filter_skills_for_agent_type、main_skills_block とは別物）を使う。
    tool_names（frontmatterの tools:）が省略されている種別は、
    _SUBAGENT_TOOLS 全量を継承する（src/tools/_state.py の
    _resolve_agent_types() と同じ解決ロジック）。

    Returns:
        [{"name", "raw_body", "production_system_prompt", "openai_tools"}, ...]
        （name の昇順、scan_agent_types() の並びと同じ）。
    """
    skills = scan_skills([config.skills_dir, *config.locohane_skills_dirs])
    agent_type_defs = scan_agent_types([config.agents_dir, *config.locohane_agents_dirs])
    agent_types_block = render_agent_types_block(agent_type_defs)
    subagent_common = build_subagent_common_expanded(config)

    tool_lookup = {t.name: t for t in registry._SUBAGENT_TOOLS}

    results = []
    for a in agent_type_defs:
        expanded = (
            a.system_prompt.replace(
                "{{skills}}",
                render_skills_block(
                    filter_skills_for_agent_type(skills, a.name, config.subagent_agent_type_run_script_allowlist)
                ),
            )
            .replace("{{all_skills}}", render_skills_block(skills))
            .replace("{{agent_types}}", agent_types_block)
            .replace(
                "{{run_script_allowlist}}",
                render_agent_type_run_script_allowlist_block(a.name, config.subagent_agent_type_run_script_allowlist),
            )
        )
        production_system_prompt = f"{expanded}\n\n{subagent_common}"
        if a.tool_names is None:
            tools = list(registry._SUBAGENT_TOOLS)
        else:
            tools = [tool_lookup[n] for n in a.tool_names if n in tool_lookup]
        results.append(
            {
                "name": a.name,
                "raw_body": a.system_prompt,
                "production_system_prompt": production_system_prompt,
                "openai_tools": [convert_to_openai_tool(t) for t in tools],
            }
        )
    return results


def main():
    config = load_config()
    base_url, api_key, model = load_endpoint(config)
    print(f"接続先: {base_url} (model={model})")
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)

    system_prompt_raw = config.system_prompt_path.read_text(encoding="utf-8")
    production_system_prompt, skills_block = build_production_system_prompt(config)
    openai_tools = build_main_agent_openai_tools(config)

    subagent_common_raw = (config.system_prompt_path.parent / "subagent_common.md").read_text(encoding="utf-8")
    subagent_common_expanded = build_subagent_common_expanded(config)
    agent_types = build_subagent_agent_types(config)

    print(f"（[main] bind対象ビルトインツール数: {len(openai_tools)}）")
    for a in agent_types:
        print(f"（[subagent:{a['name']}] bind対象ツール数: {len(a['openai_tools'])}）")
    print()

    rows: list[tuple[str, str | None, list[dict] | None]] = [
        ("[main] 1. system_prompt_raw（プレースホルダー未展開）", system_prompt_raw, None),
        ("[main] 2. skills_block（{{skills}}差込テキストのみ）", skills_block, None),
        ("[main] 3. builtin_tools（toolsフィールドのみ、systemなし）", None, openai_tools),
        ("[main] 4. production_system_prompt（本番相当・toolsなし）", production_system_prompt, None),
        ("[main] 5. production_full（本番相当system_prompt＋tools）", production_system_prompt, openai_tools),
        ("[subagent] subagent_common_raw（${...}未展開）", subagent_common_raw, None),
        ("[subagent] subagent_common_expanded（${...}展開後）", subagent_common_expanded, None),
    ]
    for a in agent_types:
        rows.append((f"[subagent:{a['name']}] raw_body（frontmatter除く本文・未展開）", a["raw_body"], None))
        rows.append(
            (
                f"[subagent:{a['name']}] production_full（system_prompt＋tools）",
                a["production_system_prompt"],
                a["openai_tools"],
            )
        )

    print(f"{'構成':<60} {'トークン数':>10}")
    print("-" * 74)
    results = {}
    for label, system_text, tools in rows:
        n = count_prompt_tokens(client, model, system_text=system_text, tools=tools)
        results[label] = n
        print(f"{label:<60} {n:>10,}")

    print("-" * 74)
    print("参考: [main]4+3 の単純合算（実測[main]5との差分がメッセージ枠組み等のoverhead）")
    naive_sum = (
        results["[main] 4. production_system_prompt（本番相当・toolsなし）"]
        + results["[main] 3. builtin_tools（toolsフィールドのみ、systemなし）"]
    )
    actual = results["[main] 5. production_full（本番相当system_prompt＋tools）"]
    print(f"  4+3合算 = {naive_sum:,} / 実測5 = {actual:,} / 差 = {actual - naive_sum:+,}")


if __name__ == "__main__":
    main()
