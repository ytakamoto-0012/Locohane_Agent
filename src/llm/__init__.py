"""LLM 接続の共通ヘルパー（llama.cpp server / OpenAI 互換）。

graph.py（メインの ReAct ループ）と subagent.py（サブエージェントの
ReAct ループ）の両方から使う。tools.py が subagent.py を import する
関係上、build_model を graph.py 側に置くと循環 import になるため
ここへ切り出している。

このファイルは実装本体を持たない再エクスポート専用のファサード。
中身は機能別に以下へ分割している（保守性のため、実装追加時もここではなく
対応するファイルへ書くこと）:
    - loop_guard.py   : LLM応答の反復ループ検知（ThinkingLoopDetected 等）
    - diagnostics.py  : 診断用ロギング（コールバック・cancel scope監視等）
    - routing.py      : セッション管理・httpxクライアントのライフサイクル・
                        接続先ルーティング（round_robin/random/
                        priority_failover）
    - thinking_control.py : ステップごとの思考レベル切り替え（[thinking_control]）
    - chat_model.py   : ChatLlamaCpp・build_model()（上記3つを組み立てる入口）

`_` 始まりの名前もいくつか再エクスポートしている。これは外部モジュールの
正式なAPIではなく、tests/ が内部状態を直接検査するために参照しているため
（例: tests/test_llm_round_robin_routing.py の llm._select_endpoint など）。
"""

from __future__ import annotations

from .chat_model import ChatLlamaCpp, build_model, enable_thinking_control, init_llm_concurrency
from .dialect import mark_user_turn
from .diagnostics import (
    _CancelScopeBreakageWatcher,
    _register_cancel_scope_watcher,
    describe_current_task,
    recent_cancel_scope_breakage,
)
from .loop_guard import (
    LLM_CONNECTION_ERRORS,
    ThinkingLoopDetected,
    ToolCallLoopDetected,
    tool_loop_nudge_text,
    pick_loop_nudge_message,
)
from .routing import (
    _active_async_clients,
    _select_endpoint,
    aclose_active_llm_clients,
    aclose_model_client,
    forget_session,
    get_current_session,
    get_preferred_sub_model,
    mark_last_endpoint_failed,
    reset_llm_wait_listener,
    reset_preferred_sub_model,
    set_current_session,
    set_llm_wait_listener,
    set_preferred_sub_model,
)

__all__ = [
    "LLM_CONNECTION_ERRORS",
    "ChatLlamaCpp",
    "ThinkingLoopDetected",
    "ToolCallLoopDetected",
    "aclose_active_llm_clients",
    "aclose_model_client",
    "build_model",
    "describe_current_task",
    "enable_thinking_control",
    "forget_session",
    "get_current_session",
    "get_preferred_sub_model",
    "init_llm_concurrency",
    "tool_loop_nudge_text",
    "mark_last_endpoint_failed",
    "mark_user_turn",
    "pick_loop_nudge_message",
    "recent_cancel_scope_breakage",
    "reset_llm_wait_listener",
    "reset_preferred_sub_model",
    "set_current_session",
    "set_llm_wait_listener",
    "set_preferred_sub_model",
]
