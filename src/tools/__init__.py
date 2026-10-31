"""LangGraph ツール（progressive disclosure 第2・第3段階）。

仕様: https://agentskills.io/specification

LangChain の @tool として定義する。read_skill/read_skill_file/run_script/analyze_image は
すべて LangGraph のツールコールとして実行され、グラフのトレースに乗る
（Chainlit 側で可視化するため）。dispatch_agent のみ内部で独立した ReAct
ループ（subagent.py）を回す特殊なツールで、その内部の呼び出しはグラフの
トレースに乗らない（意図的、コンテキスト節約のため）。

- read_skill      … 第2段階(Read):    SKILL.md 本文全体を読む
- read_skill_file       … 第3段階(Execute): references/assets 等を必要時に読む
- run_script      … 第3段階(Execute): scripts/ 配下のスクリプトを実行する
- execute_python_code … 第3段階(Execute): LLMが生成したPythonコードをその場で実行する
- get_tool_source … 第3段階(Execute): run_script が失敗した際、原因調査用にスクリプトの絶対パスを返す
- read_tool(Read)/glob_tool(Glob)/grep_tool(Grep) … ローカルファイルシステム上の
  任意の絶対パスに対する読込・ファイル名検索・全文検索（ロジック本体は
  各ファイル内に実装、ClaudeCode の同名ツールに合わせた名前）
- json_query      … JSON/dictへのJMESPathクエリ（ロジック本体は json_query.py 内）
- search_path_memory … 現在の会話のパスメモリー（@N）から似たパスを類似検索する
- analyze_image   … 第3段階(Execute): 画像ファイルをVision対応モデルへ見せ、LLM自身が内容を解析する。
  `show_in_chat=True` を指定すると、解析と同時にチャットUIへもプレビュー表示する
  （表示だけして中身を見ない、という呼び方はできない。ユーザーへの「表示して」「見せて」もこちら）
- dispatch_agent  … タスクをサブエージェントへ委譲し、最終回答のみを受け取る。完了までの間、
  進捗（経過時間・反復回数）を人間向けにチャットへ直接通知する。設定した安全上限を超えても
  なお完了しない場合のみ job_id を返してターンを終える（フォールバック）
- dispatch_agent_batch … フォルダ内の多数のファイルへ同じ処理を行う作業を、ハーネス側で
  ファイル一覧取得・グループ分け・担当ファイル割り当てまで行い、複数のサブエージェントへ
  並列に委譲する（LLMが1回呼ぶだけで並列化される。dispatch_agent と同じジョブ基盤を使う）
- check_dispatch_agent_job / stop_dispatch_agent_job … 上記フォールバック時のみ使う、
  ジョブの状況確認・強制終了
- ask_user_question(AskUserQuestion) … ユーザーへ自由記述で追加質問する。labels省略時は
  単発質問（Chainlit AskUserMessage）、labels指定時は複数項目をまとめて提示する
  フォーム（Chainlit AskElementMessage + CustomElement）
- ask_user_choice … ユーザーへ選択肢形式で追加質問する。multi_select省略/False時は
  単一選択（Chainlit AskActionMessage）、multi_select=True時は複数選択可能な
  チェックボックスフォーム（Chainlit AskElementMessage + CustomElement）
- create_memory / update_memory / delete_memory / read_memory / search_memory /
  list_memories … スレッドをまたぐ永続メモリー（src/memory.py）の読み書き。
  主エージェントのみに公開し、dispatch_agent のサブエージェントには渡さない。
- list_document_collections / search_documents / search_document_paths … ベクターDB（src/vector_db/）に
  取り込んだ社内文書の意味検索（読み取り専用。[vector_db] enabled = false なら無効の旨を返す）。

セキュリティ: read_skill_file / run_script / get_tool_source は必ず skills
ディレクトリ配下に限定する（_safe_path でディレクトリトラバーサルを拒否）。保存・実行の
パスがコードから追える事。analyze_image / read_tool(Read) / glob_tool(Glob) /
grep_tool(Grep) は読み込み系ツールのため、パスの制限は行わない
（_resolve_analyze_image_path / _resolve_file_tools_path）。ただし
`_tmp_<thread_id>`（他セッションの一時ディレクトリ）だけは例外で、
自セッション以外は読み取り不可（_foreign_tmp_dir_error）。
メモリー系ツールも同様に memory.py 側の _safe_memory_path で memory ルート配下に限定する。

設定（skills ルート・Python 実行ファイル・タイムアウト・サブエージェント設定・
メモリールート）はモジュール globals に init_tools() で一度だけ注入する。動的 import
やメタクラス等の仕掛けは使わない。

ファイル構成: 本パッケージはツール1つ（または密結合な小ツール群）につき1ファイルに
分割してある。モジュールレベルの共有状態（config値・セマフォ・レジストリ等）は
それぞれの所有ファイル（主に _state.py）が保持し、他のファイルは必ず
`from . import _state` の形で import して `_state.NAME`（属性アクセス）で参照する
こと。`from ._state import NAME` は使わない（init_tools() の再代入や
テストの monkeypatch が反映されなくなるため。詳細は _state.py の docstring 参照）。
"""

from __future__ import annotations

import asyncio  # noqa: F401 - テストが monkeypatch.setattr(tools.asyncio, ...) で参照する
import chainlit as cl  # noqa: F401 - テストが monkeypatch.setattr(tools.cl, ...) で参照する

from . import registry
from ._dispatch_agent_job import cancel_dispatch_agent_jobs_for_thread
from ._script_job import cancel_background_script_jobs_for_thread
from ._state import init_tools
from .lock_plan_mode import toggle_plan_mode_from_ui
from .check_work_dir_status import WorkDirAccessStatus, probe_workdir_access
from ._duplicate_guard import reset_call_history_guards_after_compaction
from ._path_memory_helpers import register_raw_unc_paths_in_text
from ._plan_render import current_plan_status_text
from ._state import _resolve_agent_types, forget_session_tool_semaphores
from .thread_notes import thread_note_status_text
from .tool_node import ImageAwareToolNode, filter_main_agent_tools, list_blocked_tool_names_for_hint
from .registry import get_all_tools, register_mcp_tools

# 個々の @tool オブジェクトも、分割前と同じく `tools.<ツール名>` で直接参照できる
# ようにする（テストが `tools.read_skill.ainvoke(...)` 等の形で個別ツールへ直接
# アクセスするため）。実体は registry.py が各ファイルから import 済み。
from .registry import (  # noqa: F401
    ask_user_choice,
    ask_user_question,
    analyze_image,
    approve_plan,
    check_dispatch_agent_job,
    check_script_job,
    check_work_dir_status,
    create_memory,
    create_plan,
    delete_memory,
    dispatch_agent,
    dispatch_agent_batch,
    execute_python_code,
    execute_python_code_background,
    execute_python_code_readonly,
    get_plan_status,
    get_tool_source,
    glob_tool,
    grep_tool,
    json_query,
    list_document_collections,
    list_memories,
    search_path_memory,
    list_thread_notes,
    lock_plan_mode,
    provide_download,
    read_memory,
    read_skill,
    read_skill_file,
    read_thread_note,
    read_tool,
    run_script,
    run_script_background,
    search_document_paths,
    search_documents,
    search_memory,
    show_help,
    stop_dispatch_agent_job,
    stop_script_job,
    update_memory,
    update_task_progress,
    write_scratch_note,
    write_thread_note,
)

__all__ = [
    "ImageAwareToolNode",
    "WorkDirAccessStatus",
    "cancel_background_script_jobs_for_thread",
    "cancel_dispatch_agent_jobs_for_thread",
    "current_plan_status_text",
    "filter_main_agent_tools",
    "forget_session_tool_semaphores",
    "get_all_tools",
    "init_tools",
    "list_blocked_tool_names_for_hint",
    "probe_workdir_access",
    "register_mcp_tools",
    "register_raw_unc_paths_in_text",
    "reset_call_history_guards_after_compaction",
    "thread_note_status_text",
    "toggle_plan_mode_from_ui",
]
