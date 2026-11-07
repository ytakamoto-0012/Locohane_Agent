"""1つの eval ケースをヘッドレスで実行し、結果を1行 JSON として標準出力へ出す。

使い方:
    python -m evals.run_case evals/cases/system_prompt/001_xxx.yaml
    python -m evals.run_case evals/cases/system_prompt/001_xxx.yaml --instance <インスタンス名>

--instance で設定ダッシュボードのインスタンス（instances/<name>/ の
config_overrides.json と .env）を適用してから実行する（evals/instance.py 参照）。

Chainlit サーバーは起動しない。src.tools が依存する cl.* は
evals.headless_chainlit.install() でスタブに差し替えてから
src.* をインポート・実行する。ログ・進捗はすべて stderr へ出し、
stdout は run_all.py が json.loads する1行専用にする。

対象プロンプト資産（system_prompt.md 等）はディスク上の現在の内容を
そのまま読む。チューニングループはこのファイルを直接編集する運用のため、
このスクリプト自体は対象ファイルを一切書き換えない（読むだけ）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import aiosqlite
import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError

# evals/ から見たプロジェクトルートを sys.path に通す
# （`python -m evals.run_case` を別ディレクトリから呼ばれても import src.* できるように）。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Windows のコンソールコードページ（既定 cp932）で標準出力/標準エラーが
# 文字化けするのを防ぐ。run_all.py は subprocess の出力を encoding="utf-8" で
# デコードするため、ここで揃えないと JSON 解析が壊れる。
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from evals.case_schema import EvalCase, Expect, load_case  # noqa: E402
from evals.headless_chainlit import install as install_headless_chainlit  # noqa: E402
from evals.headless_chainlit import patch_ask_relay  # noqa: E402
from evals.instance import apply_instance, llm_env_from_instance, resolve_instance_name  # noqa: E402
from evals import output_inspect  # noqa: E402
from evals.timing_callbacks import LatencyCallbackHandler  # noqa: E402


def _check_llm_reachable(base_url: str) -> str | None:
    """base_url の OpenAI 互換エンドポイントに疎通確認する。

    Args:
        base_url: config.ini の [llm].base_url（例: http://localhost:8080/v1）。

    Returns:
        疎通できれば None、失敗すればエラー内容を表す文字列。
    """
    try:
        httpx.get(f"{base_url.rstrip('/')}/models", timeout=5)
    except httpx.HTTPError as e:
        return str(e)
    return None


def _serialize_messages(messages) -> list[dict]:
    """LangGraph の MessagesState をシリアライズ可能な dict のリストへ変換する。

    Args:
        messages: HumanMessage/AIMessage/ToolMessage 等の列。

    Returns:
        各要素が {"type", "content", "tool_calls"?, "tool_name"?} の dict のリスト。
    """
    out = []
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        entry: dict = {"type": type(msg).__name__, "content": content}
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            entry["tool_calls"] = [{"name": tc.get("name"), "args": tc.get("args")} for tc in tool_calls]
        if isinstance(msg, ToolMessage) and getattr(msg, "name", None):
            entry["tool_name"] = msg.name
        usage = getattr(msg, "usage_metadata", None)
        if usage:
            entry["usage_metadata"] = dict(usage)
        # dispatch_agent 等、content_and_artifact 形式のツールが artifact に
        # token_usage を載せている場合のみ抜き出す（画像データ等は含めない）。
        artifact = getattr(msg, "artifact", None)
        if isinstance(artifact, dict) and "token_usage" in artifact:
            entry["token_usage"] = artifact["token_usage"]
        out.append(entry)
    return out


_USAGE_TOTAL_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _sum_usage(transcript: list[dict]) -> dict:
    """transcript 中の usage_metadata / token_usage を合算する。

    Args:
        transcript: _serialize_messages() が返した会話（の一部）。

    Returns:
        {"input_tokens", "output_tokens", "total_tokens"} の合計値 dict。
        トークン数を取得できたメッセージが1件も無ければ全て0になる
        （config.ini [llm].track_token_usage=false の場合等）。
    """
    total = dict.fromkeys(_USAGE_TOTAL_KEYS, 0)
    for entry in transcript:
        for source in (entry.get("usage_metadata"), entry.get("token_usage")):
            if not isinstance(source, dict):
                continue
            for key in _USAGE_TOTAL_KEYS:
                total[key] += source.get(key, 0) or 0
    return total


# 低パラメータモデルで安定して処理を継続できる、1リクエストあたりのトークン数の上限。
# 累計ではなくリクエスト単位で見る必要がある（累計が何百万になっても、1回ずつが
# 小さければ処理は続けられる。逆に累計が小さくても1回がこの値を超えると詰まる）。
# 当初は64000だったが、委譲・切り詰め等の改善で128000まで安定するようになった。
_PER_CALL_TOKEN_CEILING = 128000


def _max_usage_per_call(transcript: list[dict]) -> dict:
    """メインエージェントのLLM呼び出し1回あたりの最大トークン数を求める。

    _sum_usage() が返す累計だけでは「1リクエストあたりが上限を超えていないか」を
    判定できない。実際に大量ファイル処理が停止した事例では、累計277万トークンの
    うち34回中23回が1回あたり64000を超えており、最後はコンテキスト上限に張り付いて
    いた。この退行を結果JSONだけで検知できるようにするための集計。

    Args:
        transcript: _serialize_messages() が返した会話全体。

    Returns:
        {"input_tokens", "output_tokens", "total_tokens"} それぞれの最大値と、
        "calls"（usage を取得できた呼び出し回数）、
        "calls_over_ceiling"（total_tokens が _PER_CALL_TOKEN_CEILING 以上だった回数）。
    """
    result = dict.fromkeys(_USAGE_TOTAL_KEYS, 0)
    calls = 0
    calls_over_ceiling = 0
    for entry in transcript:
        usage = entry.get("usage_metadata")
        if not isinstance(usage, dict):
            continue
        calls += 1
        for key in _USAGE_TOTAL_KEYS:
            result[key] = max(result[key], usage.get(key, 0) or 0)
        if (usage.get("total_tokens", 0) or 0) >= _PER_CALL_TOKEN_CEILING:
            calls_over_ceiling += 1
    return {
        **result,
        "calls": calls,
        "calls_over_ceiling": calls_over_ceiling,
        "ceiling": _PER_CALL_TOKEN_CEILING,
    }


def _evaluate_expect(expect: Expect, transcript: list[dict], final_answer: str) -> dict:
    """Expect のルールを transcript / final_answer に照らして判定する。

    Args:
        expect: ケースの期待値定義。
        transcript: _serialize_messages() が返した会話全体。
        final_answer: 最後のツール呼び出しを含まない AIMessage の content。

    Returns:
        ルール名 -> {"pass": bool, "detail": str} の dict
        （定義されていないルールはキー自体を含めない）。
    """
    results: dict = {}
    called_tools: set[str] = set()
    args_by_tool: dict[str, list[dict]] = {}
    for entry in transcript:
        for tc in entry.get("tool_calls") or []:
            name = tc.get("name")
            if not name:
                continue
            called_tools.add(name)
            args_by_tool.setdefault(name, []).append(tc.get("args") or {})

    if expect.tool_called_any:
        ok = any(name in called_tools for name in expect.tool_called_any)
        results["tool_called_any"] = {
            "pass": ok,
            "detail": f"expected any of {expect.tool_called_any}, called={sorted(called_tools)}",
        }

    if expect.tool_not_called:
        violated = [n for n in expect.tool_not_called if n in called_tools]
        results["tool_not_called"] = {
            "pass": not violated,
            "detail": f"unexpectedly called: {violated}" if violated else "ok",
        }

    for tool_name, partial in expect.tool_call_args_contains.items():
        calls = args_by_tool.get(tool_name, [])
        ok = any(all(call.get(k) == v for k, v in partial.items()) for call in calls)
        results[f"tool_call_args_contains:{tool_name}"] = {
            "pass": ok,
            "detail": f"expected {partial} in one of {calls}",
        }

    if expect.response_contains:
        missing = [s for s in expect.response_contains if s not in final_answer]
        results["response_contains"] = {
            "pass": not missing,
            "detail": f"missing: {missing}" if missing else "ok",
        }

    if expect.response_not_contains:
        found = [s for s in expect.response_not_contains if s in final_answer]
        results["response_not_contains"] = {
            "pass": not found,
            "detail": f"unexpectedly present: {found}" if found else "ok",
        }

    return results


def _with_overlay_guard_exemptions(config, overlay_root: Path):
    """overlay_root 直下の各スキルのスクリプトを、昇格時と同じく計画承認・
    [main_agent_tool_guard] の免除に登録した Config を返す（src/skill_drafts.py の
    guard_exempt_entries_for_skill / merge_guard_exempt_entries）。"""
    from src import skill_drafts

    entries = [
        entry
        for skill_dir in sorted(p for p in overlay_root.iterdir() if p.is_dir())
        for entry in skill_drafts.guard_exempt_entries_for_skill(skill_dir.name, skill_dir)
    ]
    plan_exempt, allow_entries = skill_drafts.merge_guard_exempt_entries(
        config.plan_approval_exempt_scripts, config.main_agent_tool_guard_allow_entries, entries
    )
    return dataclasses.replace(config, plan_approval_exempt_scripts=plan_exempt, main_agent_tool_guard_allow_entries=allow_entries)


def _resolve_work_dir(case: EvalCase) -> Path:
    """ケースの work_dir を解決する。

    ケースのファイルからの相対パス（スキル研究室のケースが持つ fixtures/... 等）に
    その名前のフォルダがあればそれを、無ければ従来どおりプロジェクトルート相対で解決する。
    """
    return _resolve_case_dir(case, case.work_dir)


def _resolve_case_dir(case: EvalCase, value: str) -> Path:
    """ケースが指すフォルダ（work_dir・memory_seed）を _resolve_work_dir と同じ規則で解決する。"""
    raw = Path(value)
    if raw.is_absolute():
        return raw.resolve()
    beside_case = (case.source_path.parent / raw).resolve()
    if beside_case.is_dir():
        return beside_case
    return (_PROJECT_ROOT / raw).resolve()


def _apply_config_patch(patch_path: Path) -> None:
    """--config-patch の項目を、今の実効値に足した値として環境変数へ入れる（load_config() より前に呼ぶ）。

    昇格時（admin/skill_lab/promotion.py）に config_overrides.json へ書くのと同じ値を作る
    （evals/config_patch.py）。apply_instance() 済みのプロセスで、今の実効値
    （環境変数 > config_overrides.json > config.ini）を読む。
    """
    from evals.config_patch import PATCHABLE_KEYS, load_patch, merged_value
    from evals.instance import raw_setting

    instance_name = os.environ.get("LOCOHANE_INSTANCE", "default")
    for (section, key), items in load_patch(patch_path).items():
        env_name = PATCHABLE_KEYS[(section, key)]
        current = raw_setting(instance_name, section, key, env_name, process_env=True) or ""
        new_value, _ = merged_value(section, key, current, items)
        if new_value is not None:
            os.environ[env_name] = new_value


# subagent_tool_calls に残す引数・委譲文の長さ（Write の content 等が長いため）
_OBSERVED_ARG_CHARS = 1500
_OBSERVED_TASK_CHARS = 200


def _truncate_args(args: dict) -> dict:
    return {k: (v[:_OBSERVED_ARG_CHARS] + "…" if isinstance(v, str) and len(v) > _OBSERVED_ARG_CHARS else v) for k, v in args.items()}


def _subagent_call_recorder() -> tuple[list[dict], object]:
    """サブエージェント内部のツール呼び出しを集めるリストと、src.subagent へ登録する観測者。

    dispatch_agent はサブエージェントの最終回答だけを返すので、委譲先の read_skill・
    execute_python_code は transcript に残らない（2026-11-07 evals xlkit: 判定項目
    「read_skill(excel-edit) して xlkit を import したか」を直接確かめられなかった）。
    """
    calls: list[dict] = []
    task_ids: dict[str, int] = {}

    def observe(record: dict) -> None:
        task = record["task"]
        if task not in task_ids:
            task_ids[task] = len(task_ids) + 1
        calls.append(
            {
                "dispatch": task_ids[task],
                "task": task[:_OBSERVED_TASK_CHARS],
                "iteration": record["iteration"],
                "name": record["name"],
                "args": _truncate_args(record["args"]),
                "is_error": record["is_error"],
                "result": record["result"],
                **({"result_tail": record["result_tail"]} if record.get("result_tail") else {}),
            }
        )

    return calls, observe


async def _run(
    case: EvalCase,
    skill_overlays: list[Path] | None = None,
    exclude_skills: list[str] | None = None,
    agent_overlays: list[Path] | None = None,
    keep_outputs: Path | None = None,
) -> dict:
    """対象ファイルを現在の内容のまま読み込み、1ケースを実行する。

    Args:
        case: 実行する eval ケース。
        skill_overlays: 本番のスキル構成の上に最優先で重ねるスキルフォルダ
            （SKILL.md を持つフォルダ）の一覧。ドラフトスキルの評価に使う
            （--skill-overlay）。一時ディレクトリへコピーしてから重ねる
            （--repeat の全回を同じ内容にする固定は呼び出し元の run_all.py が
            行う）。重ねたスキルのスクリプトは、昇格時に登録されるのと同じく
            計画承認・[main_agent_tool_guard] を免除した設定で評価する。
        exclude_skills: 走査結果から除くスキル名（--exclude-skill。baseline 用）。
        agent_overlays: 本番のエージェント種別の上に最優先で重ねるエージェント定義
            （agents/*.md 形式のファイル）の一覧（--agent-overlay。スキル研究室で
            開発中のサブエージェントの評価に使う）。同名なら重ねた側が優先される。

    Returns:
        run_all.py 向けの結果 dict（1件分）。
    """
    # headless_chainlit.install() 適用後にインポートする
    # （src.tools 内の cl.xxx 参照が呼び出し時に解決されるため順序自体は問わないが、
    #  意図を明確にするためここでインポートする）。
    from dataclasses import replace

    from src import i18n
    from src.agent_types import render_agent_types_block, scan_agent_types
    from src.config import (
        expand_config_vars,
        load_config,
        render_agent_type_run_script_allowlist_block,
        render_memory_visibility_block,
    )
    from src.graph import ainvoke_ensuring_final_text, build_graph
    from src.llm import ThinkingLoopDetected, mark_user_turn
    from src import skill_drafts
    from src.memory import MEMORY_INDEX_MARKER, rebuild_index, render_memory_block
    from src.tools.vector_db_tools import render_document_collections_block
    from src.skills import build_system_prompt, filter_skills_for_agent_type, render_skills_block, scan_skills
    from src.subagent import add_tool_call_observer, remove_tool_call_observer
    from src.tools import get_all_tools, init_tools

    # メモリー系ツール（create_memory 等）を評価すると本番の永続メモリーストア
    # （config.ini 既定の ./data/memory）を汚してしまうため、ケース実行のたびに
    # 使い捨ての一時ディレクトリへ隔離する（src/config.py の MEMORY_DIR 環境変数を
    # 利用、config.ini の値より優先される）。1プロセス=1ケースなので、同一ケース内の
    # 複数ターンをまたぐメモリー操作は同じ一時ディレクトリ内で完結する。
    # パスメモリー（skills/path-memory）も同様の理由で使い捨ての一時
    # ディレクトリへ隔離する（本番の ./data/path_memory を汚さない）。
    # ケースが work_dir を指定している場合、フィクスチャそのものを作業
    # ディレクトリにすると、xlsx生成等の副作用がフィクスチャに残ってしまい
    # 次回実行の前提条件を変えてしまう（annual_schedule.xlsx が生成物として
    # 残ったまま次の実行で「既存ファイルがある」状態から始まってしまった
    # 事故が実際に発生した）。使い捨ての一時ディレクトリへ内容をコピーして
    # からそちらを作業ディレクトリにすることで、フィクスチャを常に読み取り
    # 専用のまま保つ。
    workdir_cm = tempfile.TemporaryDirectory(prefix="evals_workdir_") if case.work_dir else contextlib.nullcontext(None)
    overlay_cm = tempfile.TemporaryDirectory(prefix="evals_skill_overlay_") if skill_overlays else contextlib.nullcontext(None)
    agent_overlay_cm = tempfile.TemporaryDirectory(prefix="evals_agent_overlay_") if agent_overlays else contextlib.nullcontext(None)
    with (
        tempfile.TemporaryDirectory(prefix="evals_memory_") as tmp_memory_dir,
        tempfile.TemporaryDirectory(prefix="evals_path_memory_") as tmp_path_memory_dir,
        workdir_cm as tmp_workdir,
        overlay_cm as tmp_overlay_root,
        agent_overlay_cm as tmp_agent_overlay_root,
    ):
        overlay_roots: list[Path] = []
        if tmp_overlay_root:
            for skill_dir in skill_overlays or []:
                shutil.copytree(skill_dir, Path(tmp_overlay_root) / skill_dir.name)
            overlay_roots = [Path(tmp_overlay_root)]
        agent_overlay_roots: list[Path] = []
        if tmp_agent_overlay_root:
            for agent_file in agent_overlays or []:
                shutil.copy2(agent_file, Path(tmp_agent_overlay_root) / agent_file.name)
            agent_overlay_roots = [Path(tmp_agent_overlay_root)]
        os.environ["MEMORY_DIR"] = tmp_memory_dir
        os.environ["PATH_MEMORY_DIR"] = tmp_path_memory_dir
        # ケースが memory_seed を指定していれば、過去に記録したメモリーがある状態から始める
        # （写しを使うのでシードのフォルダは汚れない）。索引 MEMORY.md も作り直す。
        if case.memory_seed:
            shutil.copytree(_resolve_case_dir(case, case.memory_seed), tmp_memory_dir, dirs_exist_ok=True)
            rebuild_index(Path(tmp_memory_dir))
        # ケースが work_dir（プロジェクトルート相対パス）を指定していれば、
        # run_script/execute_python_code/view_image の既定作業ディレクトリを
        # そこへ固定する（例: 大量ファイル探索シナリオのフィクスチャ）。
        # 1プロセス=1ケースなので他ケースへの影響はない。
        if case.work_dir:
            src_dir = _resolve_work_dir(case)
            dst_dir = Path(tmp_workdir) / src_dir.name
            shutil.copytree(src_dir, dst_dir)
            os.environ["DEFAULT_WORKDIR"] = str(dst_dir)
            workdir_root: Path | None = dst_dir
            workdir_before = output_inspect.snapshot(dst_dir)
        else:
            os.environ.pop("DEFAULT_WORKDIR", None)
            workdir_root = None
            workdir_before = {}
        # ケースが env（yaml の env: {KEY: value}）を指定していれば、
        # config.ini の値を上書きする環境変数として適用する。1プロセス=
        # 1ケースなので他ケースへの影響はない。
        for key, value in case.env.items():
            os.environ[key] = value
        config = load_config()
        # --skill-overlay で重ねたスキルのスクリプトは、昇格時に promote-skill が
        # インスタンスの config_overrides.json へ登録する（計画承認・
        # [main_agent_tool_guard] の免除。会話でドラフトを試したときと同じ扱い）
        # のと同じ状態で評価する。試した挙動・評価した挙動・昇格後の挙動を揃えるため
        # （src/skill_drafts.py の guard_exempt_entries_for_skill）。
        if overlay_roots:
            config = _with_overlay_guard_exemptions(config, overlay_roots[0])

        # app.py の _setup() と同様、config.log_level に従いログをファイルへ出す。
        # eval実行中はChainlitのUIが無く進捗を外部から確認する手段が無いため
        # （PC再起動後の022番ケース再実行が実際に長時間応答なしとなり、フリーズか
        # ループかを判別できなかった経緯がある）、ツール呼び出し等のINFOログを
        # 本番のapp.logとは別ファイル（evals.log）へ出す。run_all.pyから複数ケースが
        # 直列実行されるため追記モード固定（上書きすると直前のケースの記録が消える）。
        log_level = config.log_level
        if log_level != "none":
            root_logger = logging.getLogger()
            root_logger.setLevel(logging.DEBUG if log_level == "debug" else logging.INFO)
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            file_handler = logging.FileHandler(config.log_dir / "evals.log", mode="a", encoding="utf-8")
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            # aiosqlite（checkpointer）・openai/httpx/httpcore（LLM呼び出し）が
            # DEBUGレベルで生のmsgpackペイロードやリクエスト全文（system_prompt
            # 含む）を大量に出力し、ツール呼び出しの進捗を追いたいという本来の
            # 目的を埋もれさせるため、これらのロガーだけWARNING以上に抑制する。
            for _noisy_logger in ("aiosqlite", "openai", "httpx", "httpcore"):
                logging.getLogger(_noisy_logger).setLevel(logging.WARNING)
            logging.getLogger(__name__).info("=== eval case start: %s ===", case.id)

        # main_endpoints の先頭エントリを使って疎通確認する（config.ini の main_url 相当）
        main_url = config.main_endpoints[0].base_url if config.main_endpoints else "http://localhost:8080/v1"
        unreachable = _check_llm_reachable(main_url)
        if unreachable:
            return {
                "case_id": case.id,
                "target": case.target,
                "error": "llm_unreachable",
                "detail": (f"{main_url} に接続できませんでした: {unreachable}。" "llama.cpp server が起動しているか確認してください。"),
            }

        # --skill-overlay は本番の構成（会社専用の project_locohane_dir を含む）を
        # そのまま残し、その上に最優先で重ねる（scan_skills は後方優先）。
        skills = scan_skills([config.skills_dir, *config.locohane_skills_dirs, *overlay_roots])
        if exclude_skills:
            skills = [sk for sk in skills if sk.name not in set(exclude_skills)]
        system_prompt = build_system_prompt(skills, config.system_prompt_path)
        # app.py の _setup() と同じ手順で agent_type_defs を構築し、
        # {{agent_types}}/{{memory}} プレースホルダーを置換する
        # （本番と同じシステムプロンプトで評価するため）。
        # --agent-overlay も同じく本番の構成の上に最優先で重ねる（scan_agent_types は後方優先）。
        agent_type_defs = scan_agent_types([config.agents_dir, *config.locohane_agents_dirs, *agent_overlay_roots])
        agent_types_block = render_agent_types_block(agent_type_defs)
        agent_type_defs = [
            replace(
                a,
                system_prompt=a.system_prompt.replace(
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
                ),
            )
            for a in agent_type_defs
        ]
        # 索引は config 変数の展開の後に差し込む（app.py と同じ）。メモリーの本文に "${" があると、
        # 先に差し込んだ場合だけ expand_config_vars() が未定義変数として起動時に失敗するため。
        system_prompt = system_prompt.replace("{{memory}}", MEMORY_INDEX_MARKER)
        system_prompt = system_prompt.replace("{{agent_types}}", agent_types_block)
        system_prompt = system_prompt.replace("{{document_collections}}", render_document_collections_block(config))
        # config.ini の値を ${変数名} として埋め込めるよう展開する（app.py の
        # _setup() と同じ手順、{{...}}置換完了後に行う）。
        system_prompt = expand_config_vars(system_prompt, config)
        # メモリーのツールはセッション外では匿名ユーザーとして動く（src/tools/memory_tools.py）ため、索引も同じ立場で組み立てる。
        system_prompt = system_prompt.replace(
            MEMORY_INDEX_MARKER, render_memory_block(config.memory_dir, viewer=skill_drafts.draft_owner_name(None))
        )
        # app.py の _setup() と同じく、メモリーの公開範囲の決め方を config 変数の展開の後に差し込む。
        system_prompt = system_prompt.replace("{{memory_visibility}}", render_memory_visibility_block(config))
        # 本番（app.py の _rebuild_graph）と同じく応答言語の指示を末尾に足す。セッションが
        # 無いため [ui].default_language（auto なら日本語）の言語になる。
        system_prompt += i18n.response_language_directive()

        # サブエージェント共通の注意事項を各 agent_type の system_prompt 末尾に
        # 連結する（app.py の _setup() と同じ手順）。
        subagent_common = expand_config_vars(
            (config.system_prompt_path.parent / "subagent_common.md").read_text(encoding="utf-8"),
            config,
        )
        agent_type_defs = [replace(a, system_prompt=f"{a.system_prompt}\n\n{subagent_common}") for a in agent_type_defs]

        # キーワード引数で渡す（init_tools は開発中でシグネチャが変わりうるため、
        # 位置引数だとズレて誤った型を渡してしまう事故が起きやすい）。
        init_tools(
            skills_root=[*overlay_roots, *reversed(config.locohane_skills_dirs), config.skills_dir],
            script_python=config.script_python,
            script_timeout=config.script_timeout,
            llm_config=config,
            agent_type_defs=agent_type_defs,
            subagent_max_iterations=config.subagent_max_iterations,
            default_workdir=config.default_workdir,
            memory_root=config.memory_dir,
            help_path=config.help_path,
            path_memory_dir=config.path_memory_dir,
            path_memory_max_entries=config.path_memory_max_entries,
            path_memory_search_min_score=config.path_memory_search_min_score,
            path_memory_search_filename_weight=config.path_memory_search_filename_weight,
            code_exec_enabled=config.code_exec_enabled,
            approval_timeout_seconds=config.approval_timeout_seconds,
            ask_user_question_timeout_seconds=config.ask_user_question_timeout_seconds,
            ask_user_choice_timeout_seconds=config.ask_user_choice_timeout_seconds,
            dispatch_agent_max_parallel=config.subagent_max_parallel,
            graph_tool_max_parallel=config.graph_tool_max_parallel,
            script_background_max_runtime_seconds=config.script_background_max_runtime_seconds,
            script_background_job_retention_seconds=config.script_background_job_retention_seconds,
            script_background_min_poll_interval_seconds=config.script_background_min_poll_interval_seconds,
            script_background_min_poll_message=config.script_background_min_poll_message,
            script_background_inline_wait_max_seconds=config.script_background_inline_wait_max_seconds,
            script_background_progress_push_interval_seconds=config.script_background_progress_push_interval_seconds,
            script_background_job_output_tail_chars=config.script_background_job_output_tail_chars,
            dispatch_agent_background_job_retention_seconds=config.subagent_background_job_retention_seconds,
            dispatch_agent_background_min_poll_interval_seconds=config.subagent_background_min_poll_interval_seconds,
            dispatch_agent_background_min_poll_message=config.subagent_background_min_poll_message,
            dispatch_agent_background_inline_wait_max_seconds=config.subagent_background_inline_wait_max_seconds,
            dispatch_agent_background_progress_push_interval_seconds=config.subagent_background_progress_push_interval_seconds,
            dispatch_agent_background_llm_timeout_max_retries=config.subagent_background_llm_timeout_max_retries,
            dispatch_agent_batch_max_groups=config.subagent_batch_max_groups,
            dispatch_agent_batch_stop_after_unreachable_groups=config.subagent_batch_stop_after_unreachable_groups,
            plans_dir=config.plans_dir,
            plan_approval_exempt_scripts=config.plan_approval_exempt_scripts,
            agent_type_run_script_allowlist=config.subagent_agent_type_run_script_allowlist,
            plan_badge_allow_unlock=config.plan_badge_allow_unlock,
            plan_reset_approval_on_recreate=config.plan_reset_approval_on_recreate,
            plan_auto_approve=config.plan_auto_approve,
            allow_sandbox_dirs=config.allow_sandbox_dirs,
            skill_names=[s.name for s in skills],
        )
        # install_headless_chainlit() 単体では approve_plan/ask_user_choice/
        # AskUserQuestion の _ask_with_cross_session_relay 差し替えが
        # init_tools() 実行後に失われることがあるため、get_all_tools() が
        # 返す実際のツールオブジェクトに対して改めてパッチする
        # （evals/headless_chainlit.py の patch_ask_relay() docstring参照）。
        patch_ask_relay(get_all_tools())

        thread_id = str(uuid.uuid4())
        # 本番の app.py と同じく、セッションにも thread_id を持たせる。未設定だと
        # スレッド用の一時フォルダ（_tmp_<時刻>_<短縮ID>、thread note・scratch note・
        # 読み取り結果JSONの置き場）が "_no_session" 由来の固定名になり、すべての eval
        # 実行で同じフォルダが使い回されていた（前回の実行の thread note を読んで
        # 委譲を省いた実例: 2026-11-03 の system_prompt_scale/011 の再実行）。
        import chainlit as cl

        cl.user_session.set("thread_id", thread_id)
        # recursion_limit を明示指定する。省略すると LangGraph のデフォルト（25）が
        # 使われてしまい、本番 app.py（config.ini [graph].recursion_limit、既定50）
        # より低い上限で GraphRecursionError が発生する eval 固有の誤検知を生んでいた。
        # config_timeouts ターゲット（evals/analyze_timing.py）向けに、LLM呼び出し・
        # run_script/execute_python_code の所要時間をターン単位で実測する。
        # 本番の run_config には無いキーだが、LangGraph の callbacks は
        # prebuilt グラフ（create_react_agent）・ImageAwareToolNode の双方に
        # そのまま伝播するため、本番グラフ実装（src/graph.py）は変更不要。
        timing_handler = LatencyCallbackHandler()
        run_config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": config.graph_recursion_limit,
            "callbacks": [timing_handler],
        }

        result = None
        token_usage_by_turn: list[dict] = []
        turn_timings: list[dict] = []
        prev_len = 0
        mid_turn_error: str | None = None
        turn_cutoffs: list[dict] = []

        # checkpointer は本番 app.py と同じ AsyncSqliteSaver（シリアライズ経由）を
        # 使う。MemorySaver はオブジェクトをそのまま保持しシリアライズを経由しない
        # ため、AsyncSqliteSaver でのみ顕在化しうる問題（artifact のシリアライズ
        # 不能等）を検出できていなかった。ファイルI/Oを避けるため :memory: を使う
        # （1プロセス=1ケースなので、接続はこのブロックを抜けると自動的に閉じられる）。
        async with aiosqlite.connect(":memory:") as conn:
            checkpointer = AsyncSqliteSaver(conn)
            await checkpointer.setup()
            graph = await build_graph(config, system_prompt, checkpointer)
            subagent_calls, observe_subagent_call = _subagent_call_recorder()
            add_tool_call_observer(observe_subagent_call)

            try:
                for turn_index, turn in enumerate(case.turns):
                    timing_handler.reset()
                    try:
                        result = await ainvoke_ensuring_final_text(
                            graph,
                            {"messages": [mark_user_turn(HumanMessage(content=turn))]},
                            run_config,
                            max_retries=config.thinking_loop_guard_empty_response_max_retries,
                            nudge_messages=config.thinking_loop_guard_nudge_messages,
                            loop_max_retries=config.thinking_loop_guard_max_retries,
                            tool_loop_nudge_messages=config.tool_loop_guard_nudge_messages,
                        )
                    except (GraphRecursionError, ThinkingLoopDetected) as e:
                        # 本番 app.py はこの2例外をターンごとに捕捉し、打ち切り
                        # メッセージを出してそのターンだけ終え、会話全体は継続する
                        # （app.py の on_message、GraphRecursionError/ThinkingLoopDetected
                        # ハンドラ）。以前は下の except Exception で会話全体を
                        # mid_turn_exception として中断させていたため、本番なら継続する
                        # はずの会話が eval だけ失敗扱いになる誤検知があった。
                        reason = "recursion_limit" if isinstance(e, GraphRecursionError) else "thinking_loop"
                        cutoff_entry = {"turn_index": turn_index, "reason": reason}
                        snippet = getattr(e, "snippet", "")
                        if snippet:
                            # thinking_loop 検知時にバッファされていた直近テキスト
                            # （src/llm.py の ThinkingLoopDetected.snippet）。真の反復
                            # ループか、構造化テキストによる誤検知かを事後判別するため
                            # results.json に残す。
                            cutoff_entry["snippet"] = snippet
                        turn_cutoffs.append(cutoff_entry)
                        state = await graph.aget_state(run_config)
                        turn_messages = list(state.values.get("messages", [])) if state else []
                        cutoff_text = f"[eval] {reason} に達したため、このターンを打ち切りました。"
                        # 本番同様、グラフの永続状態には残さない（aupdate_state は
                        # 呼ばない）。このケースの transcript/final_answer 用にのみ
                        # 合成メッセージを積む。
                        turn_messages = turn_messages + [AIMessage(content=cutoff_text)]
                        result = {"messages": turn_messages}

                    turn_messages = result["messages"]
                    token_usage_by_turn.append(_sum_usage(_serialize_messages(turn_messages[prev_len:])))
                    prev_len = len(turn_messages)
                    turn_timings.append({"turn_index": turn_index, **timing_handler.summary()})
            except Exception as e:  # noqa: BLE001 - コンテキスト長超過等でも途中経過を残す
                mid_turn_error = f"{type(e).__name__}: {e}"
                # ainvoke が例外で中断しても、checkpointer にはそこまでの状態が
                # ステップごとに残っているため、デバッグ用に部分的な transcript を拾う。
                state = await graph.aget_state(run_config)
                result = {"messages": state.values.get("messages", [])} if state else None
            finally:
                remove_tool_call_observer(observe_subagent_call)

            messages = result["messages"] if result else []
            transcript = _serialize_messages(messages)
            token_usage_total = _sum_usage(transcript)
            token_usage_max_per_call = _max_usage_per_call(transcript)

            final_answer = ""
            for entry in reversed(transcript):
                if entry["type"] == "AIMessage" and not entry.get("tool_calls"):
                    final_answer = entry["content"]
                    break

            rule_results: dict = {}
            if case.expect is not None:
                rule_results = _evaluate_expect(case.expect, transcript, final_answer)

            out = {
                "case_id": case.id,
                "target": case.target,
                "notes": case.notes,
                "final_answer": final_answer,
                "transcript": transcript,
                "rule_results": rule_results,
                "rules_pass": all(r["pass"] for r in rule_results.values()) if rule_results else None,
                "judge": case.judge,
                "token_usage_by_turn": token_usage_by_turn,
                "token_usage_total": token_usage_total,
                "token_usage_max_per_call": token_usage_max_per_call,
                "turn_timings": turn_timings,
            }
            if subagent_calls:
                out["subagent_tool_calls"] = subagent_calls
            if workdir_root is not None:
                out["output_files"] = output_inspect.describe_outputs(
                    workdir_root, output_inspect.changed_files(workdir_root, workdir_before), keep_outputs
                )
            if turn_cutoffs:
                out["turn_cutoffs"] = turn_cutoffs
            if mid_turn_error is not None:
                # transcript は途中経過として残しつつ、正常完了ではないことを明示する。
                out["error"] = "mid_turn_exception"
                out["detail"] = mid_turn_error
            return out


def main() -> int:
    """CLI エントリポイント。結果 JSON を1行 stdout に出す。"""
    parser = argparse.ArgumentParser(prog="python -m evals.run_case")
    parser.add_argument("case_path", type=Path)
    parser.add_argument(
        "--instance",
        help="実行対象インスタンス名（instances/<name>/。省略時は環境変数 LOCOHANE_INSTANCE、それも無ければ default）",
    )
    parser.add_argument(
        "--skill-overlay",
        action="append",
        default=[],
        type=Path,
        help="本番のスキル構成の上に最優先で重ねるスキルフォルダ（SKILL.md を持つフォルダ）。複数指定可。ドラフトスキルの評価用",
    )
    parser.add_argument(
        "--exclude-skill",
        action="append",
        default=[],
        help="スキル一覧から除くスキル名。複数指定可（baseline 用）",
    )
    parser.add_argument(
        "--agent-overlay",
        action="append",
        default=[],
        type=Path,
        help="本番のエージェント種別の上に最優先で重ねるエージェント定義（agents/*.md 形式のファイル）。複数指定可",
    )
    parser.add_argument(
        "--config-patch",
        type=Path,
        help="今の実効値に項目を足す設定パッチ（スキル研究室の config_patch.json。evals/config_patch.py）",
    )
    parser.add_argument(
        "--llm-from-instance",
        help="LLM の接続先（[llm] main_url/sub_url 等）だけをこのインスタンス（スキル研究室）のものにする",
    )
    parser.add_argument(
        "--keep-outputs",
        type=Path,
        help="作業フォルダで新しく作られた・変わった文書ファイル（xlsx/docx/pptx/pdf・スクリプト）を、一時フォルダが消える前にここへ写す",
    )
    args = parser.parse_args()
    for skill_dir in args.skill_overlay:
        if not (skill_dir / "SKILL.md").is_file():
            parser.error(f"--skill-overlay must be a folder containing SKILL.md: {skill_dir}")
    for agent_file in args.agent_overlay:
        if not (agent_file.is_file() and agent_file.suffix == ".md"):
            parser.error(f"--agent-overlay must be an agent definition .md file: {agent_file}")

    instance_name = resolve_instance_name(args.instance)
    # _run() 内で MEMORY_DIR 等の隔離用環境変数・ケースの env: を設定するより
    # 前に適用する（インスタンス別 .env は override=True で読むため、後に
    # 適用するとそれらを打ち消してしまう）。
    apply_instance(instance_name)
    if args.llm_from_instance:
        # 構成は評価対象インスタンスのまま、接続先だけを研究室のものにする
        # （環境変数は config_overrides.json・インスタンス別 .env より優先される）。
        os.environ.update(llm_env_from_instance(args.llm_from_instance))
    if args.config_patch:
        _apply_config_patch(args.config_patch.resolve())

    case = load_case(args.case_path)
    install_headless_chainlit(case.auto_approve, case.scripted_text_answers)

    try:
        result = asyncio.run(
            _run(
                case,
                [d.resolve() for d in args.skill_overlay],
                args.exclude_skill,
                [f.resolve() for f in args.agent_overlay],
                args.keep_outputs.resolve() if args.keep_outputs else None,
            )
        )
    except Exception as e:  # noqa: BLE001 - モデルの幻覚呼び出し等も診断可能なJSONにする
        result = {
            "case_id": case.id,
            "target": case.target,
            "error": "runtime_exception",
            "detail": f"{type(e).__name__}: {e}",
        }
    result["instance"] = instance_name
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
