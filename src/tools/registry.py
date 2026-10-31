"""組み込みツール一覧の組み立てと MCP 動的ツールの登録。"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from .read_skill import read_skill
from .read_skill_file import read_skill_file
from .provide_download import provide_download
from .get_tool_source import get_tool_source
from .check_work_dir_status import check_work_dir_status
from .write_scratch_note import write_scratch_note
from .analyze_image import analyze_image
from .run_script import run_script
from .read_tool import read_tool
from .glob_tool import glob_tool
from .grep_tool import grep_tool
from .json_query import json_query
from .search_path_memory import search_path_memory
from .run_script_background import run_script_background
from .check_script_job import check_script_job
from .stop_script_job import stop_script_job
from .execute_python_code import execute_python_code
from .execute_python_code_readonly import execute_python_code_readonly
from .execute_python_code_background import execute_python_code_background
from .dispatch_agent import dispatch_agent
from .dispatch_agent_batch import dispatch_agent_batch
from .check_dispatch_agent_job import check_dispatch_agent_job
from .stop_dispatch_agent_job import stop_dispatch_agent_job
from .create_plan import create_plan
from .approve_plan import approve_plan
from .update_task_progress import update_task_progress
from .get_plan_status import get_plan_status
from .lock_plan_mode import lock_plan_mode
from .ask_user_question import ask_user_question
from .ask_user_choice import ask_user_choice
from .memory_tools import create_memory, update_memory, delete_memory, read_memory, search_memory, list_memories
from .show_help import show_help
from .thread_notes import write_thread_note, list_thread_notes, read_thread_note
from .vector_db_tools import list_document_collections, search_document_paths, search_documents


_SUBAGENT_TOOLS: list = [
    read_skill,
    read_skill_file,
    provide_download,
    run_script,
    run_script_background,
    check_script_job,
    stop_script_job,
    execute_python_code,
    execute_python_code_background,
    execute_python_code_readonly,
    get_tool_source,
    check_work_dir_status,
    analyze_image,
    read_tool,
    glob_tool,
    grep_tool,
    json_query,
    search_path_memory,
    write_scratch_note,
    write_thread_note,
    list_thread_notes,
    read_thread_note,
    create_memory,
    update_memory,
    delete_memory,
    read_memory,
    search_memory,
    list_memories,
    list_document_collections,
    search_documents,
    search_document_paths,
]


# グラフに渡す組み込みツール一覧（3 段階の progressive disclosure + サブエージェント委譲 +
# ユーザー質問 + 永続メモリー + ヘルプ）。MCPサーバー由来の動的ツール（_MCP_TOOLS）とは
# 別管理にし、get_all_tools() で合流させる（詳細は _MCP_TOOLS 定義箇所参照）。
_BASE_TOOLS: list[BaseTool] = [
    read_skill,
    read_skill_file,
    provide_download,
    run_script,
    run_script_background,
    check_script_job,
    stop_script_job,
    execute_python_code,
    execute_python_code_background,
    get_tool_source,
    check_work_dir_status,
    analyze_image,
    read_tool,
    glob_tool,
    grep_tool,
    json_query,
    search_path_memory,
    write_thread_note,
    list_thread_notes,
    read_thread_note,
    dispatch_agent,
    dispatch_agent_batch,
    check_dispatch_agent_job,
    stop_dispatch_agent_job,
    ask_user_question,
    ask_user_choice,
    create_plan,
    approve_plan,
    update_task_progress,
    get_plan_status,
    lock_plan_mode,
    create_memory,
    update_memory,
    delete_memory,
    read_memory,
    search_memory,
    list_memories,
    list_document_collections,
    search_documents,
    search_document_paths,
    show_help,
]

# MCPサーバーから取得した動的ツール（init_mcp_tools() が起動時に1回だけ設定する）。
# src/mcp_client.py が register_mcp_tools() 経由で書き込む。
_MCP_TOOLS: list[BaseTool] = []


def register_mcp_tools(mcp_tools: list[BaseTool]) -> None:
    """MCPサーバーから取得した動的ツール一覧を差し替える。

    src.mcp_client.init_mcp_tools()（app.py の @cl.on_app_startup から
    アプリ起動時に1回だけ呼ばれる）が呼ぶ。

    Args:
        mcp_tools: 接続に成功した全MCPサーバーのツールをフラットにまとめたリスト。
    """
    global _MCP_TOOLS
    _MCP_TOOLS = list(mcp_tools)


def get_all_tools() -> list[BaseTool]:
    """メインエージェントに渡す全ツール（組み込み + MCP動的登録分）を返す。

    src/graph.py の build_graph() がセッション構築の都度これを呼ぶため、
    MCP接続が完了する前に呼ばれても（register_mcp_tools() 未実行なら）
    組み込みツールのみで安全にグラフを構築できる。
    """
    return [*_BASE_TOOLS, *_MCP_TOOLS]
