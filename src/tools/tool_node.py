"""ImageAwareToolNode と、メイングラフのツール呼び出しガード。"""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import Field, create_model
from langgraph.prebuilt import ToolNode
import chainlit as cl
import logging

from .. import skill_drafts
from ..tool_errors import looks_like_error
from ..tool_args_guard import find_unknown_tool_args, unknown_tool_args_error
from ..tool_loop_guard import find_leaked_tool_markup, leaked_tool_markup_error
from . import _state
from ._safe_path import _is_guard_exempt_script, _safe_path
from ._duplicate_guard import _record_and_check_duplicate
from ._state import _IN_SUBAGENT, _tool_call_semaphore_wrap
from .analyze_image import _with_image_followups

logger = logging.getLogger(__name__)


def _extract_tool_call_from_node_input(input) -> dict | None:  # noqa: A002
    """ImageAwareToolNode.invoke/ainvoke が受け取る入力から、今回実行対象の
    単一 tool_call を取り出す。

    LangGraph は並列 tool_calls 実行のため、AIMessage の tool_calls を
    まとめてバッチで渡すのではなく、個々の tool_call を1件ずつ
    {"__type": "tool_call_with_context", "tool_call": {...}, "state": {...}}
    という形で渡してくる（ToolNode の公開 docstring が説明する
    dict/list/tool_calls-list の3形式には無い、実際に確認した挙動）。
    この形でなければ None を返す。
    """
    if isinstance(input, dict) and input.get("__type") == "tool_call_with_context":
        return input.get("tool_call")
    return None


def _log_tool_calls_debug(input) -> None:  # noqa: A002
    """メイングラフのツール呼び出し（呼び出し前）を DEBUG レベルで記録する。

    config.ini の [log].level が "debug" のときのみ実際にログへ出る
    （logger.isEnabledFor で早期リターンし、通常時はほぼゼロオーバーヘッド）。
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    call = _extract_tool_call_from_node_input(input)
    if not call:
        return
    logger.debug("tool_call: name=%s args=%r id=%s", call.get("name"), call.get("args"), call.get("id"))


def _log_tool_results_debug(result: dict, call_args: dict | None = None) -> None:
    """メイングラフのツール呼び出し結果を DEBUG レベルで記録する（全文、未省略）。

    execute_python_* 系ツールは成功時も WARNING（スキル開発アイデアの
    シグナルとして記録。代替スキルが作られれば LLM は呼ばなくなる）。
    エラーを示す戻り値（src/tool_errors.py の looks_like_error）も WARNING。
    execute_python_code の場合は call_args に code が含まれるため、
    WARNINGログにも含める（monitor-app-log スキルでissue起票可能にする）。
    """
    for msg in result.get("messages", []):
        name = getattr(msg, "name", None) or ""
        content = msg.content or ""
        is_execute_python = name.startswith("execute_python_")
        has_error = looks_like_error(content)

        if is_execute_python or has_error:
            if is_execute_python and call_args and "code" in call_args:
                logger.warning(
                    "tool_result: name=%s args_code=%r content=%r",
                    name,
                    call_args["code"][:1000],
                    content[:500],
                )
            else:
                logger.warning("tool_result: name=%s content=%r", name, content[:500])
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("tool_result: name=%s content=%r", name, content)


# MCPサーバー由来の動的ツール名のプレフィックス。命名規則の実体は
# src/mcp_client.py の _sanitize_tool_name() だが、mcp_client.py が起動時に
# src/tools/__init__.py（本モジュールを含むパッケージ）を import する依存
# 方向のため、逆方向の import による循環を避けてこちらに定義し、
# mcp_client.py 側がこの定数を import して使う。
MCP_TOOL_NAME_PREFIX = "mcp__"


def _is_mcp_tool_name(name: str) -> bool:
    """MCPサーバー由来の動的ツール名かどうかを判定する。

    src/mcp_client.py の _sanitize_tool_name() が "mcp__<server>__<tool>"
    形式で命名する（先頭5文字は64文字切り詰め後も必ず残る）ため、この
    プレフィックスの有無で判定する。
    """
    return name.startswith(MCP_TOOL_NAME_PREFIX)


def _mcp_tool_always_allowed(name: str, guard_mode: str) -> bool:
    """[main_agent_tool_guard].mode="tools_skills_only" 時、そのツール名を
    常に許可（ガード対象外）とみなすか。

    _guard_main_agent_tool_limit・filter_main_agent_tools・
    list_blocked_tool_names_for_hint の3箇所が同じ判定を使う共通ゲート。
    """
    return guard_mode == "tools_skills_only" and _is_mcp_tool_name(name)


def _guard_leaked_tool_markup(input):  # noqa: A002
    """引数に tool_call の XML が漏れた呼び出しを実行させず、エラーの ToolMessage に差し替える。

    Returns:
        ブロックする場合は {"messages": [ToolMessage]}、問題なければ None。
    """
    call = _extract_tool_call_from_node_input(input)
    if not call:
        return None
    key = find_leaked_tool_markup(call.get("args"))
    if key is None:
        return None
    name = call.get("name") or ""
    return {
        "messages": [
            ToolMessage(
                content=leaked_tool_markup_error(name, key),
                name=name,
                tool_call_id=call.get("id"),
                status="error",
            )
        ]
    }


def _guard_unknown_tool_args(input, tools_by_name: dict[str, BaseTool]):  # noqa: A002
    """スキーマに無い引数名の呼び出しを実行させず、エラーの ToolMessage に差し替える（src/tool_args_guard.py 参照）。

    Returns:
        ブロックする場合は {"messages": [ToolMessage]}、問題なければ None。
    """
    call = _extract_tool_call_from_node_input(input)
    if not call:
        return None
    tool = tools_by_name.get(call.get("name"))
    unknown = find_unknown_tool_args(tool, call.get("args"))
    if not unknown:
        return None
    return {
        "messages": [
            ToolMessage(
                content=unknown_tool_args_error(tool, unknown, call.get("args")),
                name=tool.name,
                tool_call_id=call.get("id"),
                status="error",
            )
        ]
    }


_ALLOWED_WHILE_AWAITING_APPROVAL = {"approve_plan", "get_plan_status", "lock_plan_mode"}
# このうち呼ばれたらガードのフラグ自体を解除するもの（それ以外
# （get_plan_status）は読み取り専用の確認だけなので、フラグは維持したまま
# 通過させる）。
_CLEARS_AWAITING_APPROVAL = {"approve_plan", "lock_plan_mode"}


def _guard_awaiting_approve_plan(input):  # noqa: A002
    """create_plan 直後、approve_plan/get_plan_status/lock_plan_mode 以外の
    tool_calls を実行させず、合成 ToolMessage に差し替える。

    本番ログ・evalで、create_plan の直後に規定の approve_plan ではなく
    ask_user_choice 等を自作して承認確認の代わりに使ってしまう事例が確認された
    （システムプロンプトの指示のみでは確実性が無いため、コード側でも強制する）。
    ただし、モデルが調査不足に自分で気づき lock_plan_mode で計画をやり直そうと
    する自己修正（evalで実際に観測済み）は正当な経路として許可する。
    ガード対象でなければ None を返す（呼び出し側はこの場合、通常通り
    super().ainvoke()/invoke() を呼ぶこと）。

    Args:
        input: ImageAwareToolNode.invoke/ainvoke がそのまま受け取った入力
            （_extract_tool_call_from_node_input 参照）。

    Returns:
        ブロックする場合は ToolNode.invoke/ainvoke と同じ形式の結果
        （{"messages": [ToolMessage]}）。ガード対象外・許可リスト内の
        呼び出しの場合は None。
    """
    if not cl.user_session.get("awaiting_approve_plan_call"):
        return None
    call = _extract_tool_call_from_node_input(input)
    if not call:
        return None
    name = call.get("name")
    if name in _ALLOWED_WHILE_AWAITING_APPROVAL:
        if name in _CLEARS_AWAITING_APPROVAL:
            cl.user_session.set("awaiting_approve_plan_call", False)
        return None
    # フラグはクリアしない（モデルが approve_plan/lock_plan_mode を呼ぶまで、
    # 次の呼び出しも引き続きブロックする。plan_approved ガードと同様、
    # 明示的に解除されるまでブロックし続ける設計）。
    return {
        "messages": [
            ToolMessage(
                content=("Error: call approve_plan right after create_plan" " (the other tools were not run)."),
                name=name,
                tool_call_id=call.get("id"),
                status="error",
            )
        ]
    }


def _guard_main_agent_tool_limit(input):  # noqa: A002
    """[main_agent_tool_guard] の許可リストに基づき、メインエージェントが直接
    呼べるツールとその回数を制限し、dispatch_agentへの委譲を促すガード。

    旧実装は Glob専用（_check_main_agent_glob_limit）・run_script専用
    （旧 _check_main_agent_run_script_limit）と対象ごとにコードを決め打ちして
    いたが、実際にトークン上限へ追い込んだ主因は run_script の連打ではなく
    その後 analyze_image をメインエージェント自身が委譲せずページ数分連打した
    ことだった（英検3級PDF調査、2026-08-10）。ビルトインツールを一切問わず
    任意の名前を登録できるよう、ImageAwareToolNode.invoke/ainvoke の共通
    差し込みポイント（_guard_awaiting_approve_plan と同じ場所）でツール名
    そのものを判定する汎用ガードに統合し、Globガードもここへ統合済み。

    本ガードは許可リスト（ホワイトリスト）方式: [main_agent_tool_guard].allow_entries
    に登録されていないツール名・run_scriptスキルスクリプトは、メインエージェント
    から一切呼び出せない（登録＝完全ブロックの0や、登録すらされていない場合を
    区別しない。どちらも「呼べない」という結果は同じ）。そのためメインエージェントの
    基本運用に必須なツール（dispatch_agent・create_plan・ask_user_question等）も
    含め、config.ini 側で明示的に登録しておく必要がある（多くは max_calls=-1
    の無制限指定で登録する）。

    登録形式（[main_agent_tool_guard].allow_entries）の各要素は
    [対象, max_calls] の2要素で、対象は2種類を許容する:
      - 文字列1件（例: "Glob", "analyze_image"）: そのツール名の呼び出し
        そのものを引数を問わず対象にする。
      - [スキル名, スクリプトファイル名] の2要素（例:
        ["pdf-tools","render_pdf_pages.py"]）: name が run_script/
        run_script_background で、かつ args の skill_name/script_filename が
        一致する場合のみ対象にする。
    max_calls はエントリごとに個別指定する（ツールごとに許容回数を変えたい
    ケースがあるため、旧実装のような全体共通の1値ではない）。
      - 0  : 登録はするが完全ブロック（1回も呼べない）。
      - -1 : 登録した上で回数無制限に許可する。
      - 1以上: その回数まで許可し、超過分を拒否する。
    他の呼び出し回数ガード（_check_file_tools_duplicate が使う
    _record_and_check_duplicate）は0以下を「無制限（ガード無効）」として
    扱うが、本ガードは0と-1の意味が逆になる点に注意（このガード固有の
    意味論であり、他の呼び出し回数ガードには影響しない）。

    サブエージェント（dispatch_agent経由）内部での呼び出しは対象外
    （_IN_SUBAGENT が True の間はガードしない。調査そのものが役目のため）。

    [main_agent_tool_guard].mode="tools_skills_only" のときは、MCP動的
    ツール（_mcp_tool_always_allowed参照）を常に許可し、それ以外（ビルトイン
    ツール・run_script経由のスキル）は mode="all" と同じ許可リスト判定を
    行う。mode="false" ならこのガード自体を素通りする。

    Args:
        input: ImageAwareToolNode.invoke/ainvoke がそのまま受け取った入力
            （_extract_tool_call_from_node_input 参照）。

    Returns:
        呼び出せない場合はブロックする結果（{"messages": [ToolMessage]}）、
        呼び出してよい場合は None（呼び出し側は通常通りツールを実行してよい）。
    """
    cfg = _state._LLM_CONFIG
    guard_mode = cfg.main_agent_tool_guard_mode if cfg else "false"
    if guard_mode == "false":
        return None
    if _IN_SUBAGENT.get():
        return None
    entries = cfg.main_agent_tool_guard_allow_entries if cfg else frozenset()
    entries_by_key = dict(entries)
    call = _extract_tool_call_from_node_input(input)
    if not call:
        return None
    name = call.get("name")
    if _mcp_tool_always_allowed(name, guard_mode):
        return None
    args = call.get("args") or {}
    if name in ("run_script", "run_script_background") and _is_guard_exempt_script(
        args.get("skill_name"), args.get("script_filename")
    ):
        # ドラフトスキル・skill-creator 本体は allow_entries の登録に関係なく常に許可する
        # （src/skill_drafts.py の is_guard_exempt_script 参照）。
        return None
    signature: str | None = None
    guard_max_calls: int | None = None
    if name in entries_by_key:
        signature = name
        guard_max_calls = entries_by_key[name]
    elif name in ("run_script", "run_script_background"):
        pair = (args.get("skill_name"), args.get("script_filename"))
        if pair in entries_by_key:
            signature = f"{name}:{pair[0]}/{pair[1]}"
            guard_max_calls = entries_by_key[pair]
    if signature is None:
        # 許可リストに存在しない＝メインエージェントからは一切呼び出せない。
        content = f"Error: {name} is not allowed for the main agent (not registered in main_agent_tool_guard.allow_entries)."
    elif guard_max_calls == -1:
        return None
    elif guard_max_calls <= 0:
        content = f"Error: {name} is forbidden for the main agent (max_calls=0)."
    elif not _record_and_check_duplicate("main_agent_tool_guard_call_count", signature, guard_max_calls):
        return None
    else:
        content = f"Error: {name} has already reached the call limit for the main agent ({guard_max_calls} times)."
    # agent_type一覧はagents/*.mdの走査結果（_state._AGENT_TYPES）から動的に組み立てる。
    # 種別を文言へ決め打ちすると、将来 agent_type が増減しても案内が追従せず、
    # 存在しない/古い種別へ誘導してしまう（dispatch_agentの不明agent_typeエラー
    # メッセージと同じ組み立て方、2972行目付近参照）。
    available_agent_types = ", ".join(sorted(_state._AGENT_TYPES)) or "see the agent_type list of dispatch_agent"
    return {
        "messages": [
            ToolMessage(
                content=(
                    f"{content} Do not run any more yourself; delegate the remaining investigation/processing to dispatch_agent (agent_type: {available_agent_types})."
                ),
                name=name,
                tool_call_id=call.get("id"),
                status="error",
            )
        ]
    }


def _guard_exempt_skill_names(config) -> list[str]:
    """allow_entries 無しでもメインエージェントが直接実行できるスキル名の一覧。

    skills_dir 配下の skill-creator 本体（同名の .locohane 側スキルが優先される
    構成では対象外。src/skill_drafts.py の is_builtin_skill_creator）と、
    会話のユーザーに見えて実行できる、scripts/ を持つドラフト。
    """
    names: list[str] = []
    try:
        scripts_dir = _safe_path(f"{skill_drafts.SKILL_CREATOR_NAME}/scripts", op="exec")
    except (ValueError, RuntimeError):
        scripts_dir = None
    if scripts_dir is not None and scripts_dir.is_dir() and skill_drafts.is_builtin_skill_creator(scripts_dir, config):
        names.append(skill_drafts.SKILL_CREATOR_NAME)
    drafts = skill_drafts.scan_visible_drafts(config, skill_drafts.current_draft_user())
    names.extend(d.skill.name for d in drafts if d.executable and d.skill.has_scripts)
    return names


def _restrict_skill_name(tool: BaseTool, names: tuple[str, ...]) -> BaseTool:
    """tool の skill_name 引数を names の選択肢（Literal）に制約した複製を返す。

    モジュール共通の run_script/run_script_background（サブエージェントも共有）は
    書き換えず、メインエージェント用の複製だけを制約する（read_skill の
    session_read_skill() と同じ方式）。skill_name を持たないツールはそのまま返す。
    """
    schema = getattr(tool, "args_schema", None)
    fields = getattr(schema, "model_fields", None)
    if not fields or "skill_name" not in fields or not hasattr(tool, "model_copy"):
        return tool
    new_fields = {}
    for field_name, info in fields.items():
        if field_name == "skill_name":
            new_fields[field_name] = (Literal[names], Field(description=info.description))
        else:
            new_fields[field_name] = (info.annotation, info)
    return tool.model_copy(update={"args_schema": create_model(tool.name, **new_fields)})


def filter_main_agent_tools(tools: list[BaseTool], config) -> list[BaseTool]:
    """[main_agent_tool_guard] 有効時、メインエージェントへ bind するツールを
    許可されたものだけに絞り込む。

    _guard_main_agent_tool_limit は呼び出し時にしか効かないため、そのままだと
    未登録・max_calls=0 のツールも docstring 由来の description ごと LLM に
    見え続け、「見えているのに呼べない」ツールを試みては拒否される無駄な
    往復（dispatch_agentへの再委譲を促されるだけの1往復）が発生する
    （issue/20260823_144320_*、issue/20260813_163000_* 参照）。本関数は
    _guard_main_agent_tool_limit と同じ allow_entries 解釈ロジックで、
    そもそも呼び出せないツールをあらかじめ tools リストから除外する。

    呼び出し時のブロック自体（_guard_main_agent_tool_limit）はこの関数とは
    独立して常に有効なままにする（フィルタ漏れがあっても最終防衛線として
    機能させるため）。

    Args:
        tools: フィルタ前のツール一覧（get_all_tools() の戻り値を想定）。
            サブエージェント側の tools リスト（_SUBAGENT_TOOLS 由来）には
            使わない（本ガード自体がサブエージェント内部の呼び出しを
            対象外にしているため）。
        config: main_agent_tool_guard_mode / main_agent_tool_guard_allow_entries
            を持つ Config。

    Returns:
        mode="false" 時は tools をそのまま返す。mode="tools_skills_only" 時は
        MCP動的ツール（_mcp_tool_always_allowed参照）を無条件で残し、それ以外は
        mode="all" と同じ判定（名前が allow_entries に max_calls≠0 で
        登録されているツールのみ）を行う。
        run_script/run_script_background は裸のツール名としては登録しない
        運用（config.ini 参照）のため、[skill_name, script_filename] ペアの
        エントリ（script_filenameが空文字列でないもの）が1件でも max_calls≠0
        で登録されていれば残す（個別スキルの可否は _guard_main_agent_tool_limit
        が呼び出し時に判定する）。[skill_name, ""] は scripts/を持たないスキルを
        {{skills}} 一覧へ表示するためだけのダミーエントリ（src/skills.py の
        is_skill_directly_runnable 参照）で、run_script/run_script_background の
        args.script_filename と一致することが無いため、ここではカウントしない
        （このエントリだけしか無い場合に run_script ツール自体を無意味にbindして
        しまわないようにするため）。
        ドラフトスキル・skill-creator 本体（_guard_exempt_skill_names）も
        直接実行できるスキルとして数える。bind する run_script/
        run_script_background は、skill_name をこれら直接実行できるスキル名の
        選択肢に制約した複製にする（skill-creator は常に同梱されているため、
        制約しないと allow_entries 未登録の構成でも全スキルが呼べそうに見え、
        拒否されるだけの往復が起きる）。
    """
    guard_mode = config.main_agent_tool_guard_mode
    if guard_mode == "false":
        return tools
    entries_by_key = dict(config.main_agent_tool_guard_allow_entries)
    runnable_skill_names = tuple(
        dict.fromkeys(
            [
                *(key[0] for key, max_calls in entries_by_key.items() if isinstance(key, tuple) and key[1] != "" and max_calls != 0),
                *_guard_exempt_skill_names(config),
            ]
        )
    )
    filtered = []
    for t in tools:
        if _mcp_tool_always_allowed(t.name, guard_mode):
            filtered.append(t)
            continue
        if t.name in ("run_script", "run_script_background"):
            if runnable_skill_names:
                filtered.append(_restrict_skill_name(t, runnable_skill_names))
            continue
        max_calls = entries_by_key.get(t.name)
        if max_calls is not None and max_calls != 0:
            filtered.append(t)
    return filtered


def list_blocked_tool_names_for_hint(tools: list[BaseTool], config) -> list[str]:
    """[main_agent_tool_guard] visibility_mode=hint 用に、メインエージェントが
    直接呼び出せないビルトインツール名の一覧を返す。

    filter_main_agent_tools() と対になる関数で、同じ許可判定ロジックのうち
    「除外される側」の名前だけを返す。hintモードではツール自体をbindせず
    （bindすると「呼べそうに見えて拒否される」無駄な往復が発生するため）、
    この名前一覧をシステムプロンプトへテキストとして差し込むだけに留める
    （app.py 参照）。

    run_script/run_script_background はスキル単位の可否が {{skills}} 側の
    render_skills_block_with_hint() で個別に示されるため、ここでは対象外
    （一括で「呼べない」と案内すると、許可されたスキルスクリプトがあっても
    誤解を招くため）。

    mode="tools_skills_only" のときはMCP動的ツール（_mcp_tool_always_allowed参照）
    を常に許可扱いとし、blocked一覧に含めない。mode="all" のときは
    tools（get_all_tools()の戻り値＝ビルトイン+MCP）全体を対象に判定する
    ため、allow_entries未登録のMCP動的ツール名もblocked一覧に混ざりうる。

    Args:
        tools: フィルタ前のツール一覧（get_all_tools() の戻り値を想定）。
        config: main_agent_tool_guard_mode / main_agent_tool_guard_allow_entries
            を持つ Config。

    Returns:
        mode="false" 時は空リスト。それ以外は、allow_entries に未登録、または
        max_calls=0 で登録されているツール名をソートしたリスト
        （run_script/run_script_background、mode="tools_skills_only"時の
        MCP動的ツールは含まない）。
    """
    guard_mode = config.main_agent_tool_guard_mode
    if guard_mode == "false":
        return []
    entries_by_key = dict(config.main_agent_tool_guard_allow_entries)
    blocked = []
    for t in tools:
        if _mcp_tool_always_allowed(t.name, guard_mode):
            continue
        if t.name in ("run_script", "run_script_background"):
            continue
        max_calls = entries_by_key.get(t.name)
        if max_calls is None or max_calls == 0:
            blocked.append(t.name)
    return sorted(blocked)


class ImageAwareToolNode(ToolNode):
    """analyze_image の実行結果（画像）を、後続の HumanMessage として自動追加する ToolNode。

    OpenAI互換API の tool role メッセージは文字列content しか持てないため、
    画像を持つ ToolMessage.artifact をそのまま次のモデル呼び出しに含めることは
    できない。そこで ToolNode 実行後に _with_image_followups() で後処理し、
    画像を content に持つ HumanMessage を会話履歴へ追加する。
    handwritten/prebuilt いずれのグラフ実装でも、素の ToolNode の代わりに
    このクラスを使うだけで画像受け渡しに対応できる。

    また ToolNode の公式拡張点 awrap_tool_call 経由で、全ツール呼び出しを
    セッション毎の Semaphore（_state._TOOL_CALL_SEMAPHORES）によりガードする
    （_tool_call_semaphore_wrap 参照。ToolNode._afunc が同一AIMessage内の
    複数tool_callsを asyncio.gather() で完全並列実行する挙動への対策。
    dispatch_agent 専用の _state._DISPATCH_AGENT_SEMAPHORES と同じ理由づけの
    メインエージェント版）。
    """

    def __init__(self, tools, **kwargs):
        kwargs.setdefault("awrap_tool_call", _tool_call_semaphore_wrap)
        super().__init__(tools, **kwargs)

    def invoke(self, input, config=None, **kwargs):  # noqa: A002
        _log_tool_calls_debug(input)
        call_args = _extract_tool_call_from_node_input(input)
        if call_args:
            call_args = call_args.get("args", {})
        blocked = (
            _guard_leaked_tool_markup(input)
            or _guard_unknown_tool_args(input, self.tools_by_name)
            or _guard_awaiting_approve_plan(input)
            or _guard_main_agent_tool_limit(input)
        )
        if blocked is not None:
            _log_tool_results_debug(blocked, call_args)
            return _with_image_followups(blocked)
        result = super().invoke(input, config, **kwargs)
        _log_tool_results_debug(result, call_args)
        return _with_image_followups(result)

    async def ainvoke(self, input, config=None, **kwargs):  # noqa: A002
        _log_tool_calls_debug(input)
        call_args = _extract_tool_call_from_node_input(input)
        if call_args:
            call_args = call_args.get("args", {})
        blocked = (
            _guard_leaked_tool_markup(input)
            or _guard_unknown_tool_args(input, self.tools_by_name)
            or _guard_awaiting_approve_plan(input)
            or _guard_main_agent_tool_limit(input)
        )
        if blocked is not None:
            _log_tool_results_debug(blocked, call_args)
            return _with_image_followups(blocked)
        result = await super().ainvoke(input, config, **kwargs)
        _log_tool_results_debug(result, call_args)
        return _with_image_followups(result)
