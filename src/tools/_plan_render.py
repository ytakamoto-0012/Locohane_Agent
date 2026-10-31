"""実行計画（plan）の整形・保存ヘルパー。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import chainlit as cl
import json

from . import _state


def current_plan_status_text() -> str:
    """現在の実行計画の状態を、プロース（要約LLMの再構成・委譲元の手書き）に頼らず
    cl.user_session から直接読んで機械的に整形する。

    context_compaction.py（圧縮再注入）と _task_with_plan_hint（dispatch_agent
    委譲時の注入）の両方が同じ整形結果を共有するための single source of truth。
    計画が未作成、またはchainlitセッション文脈外（テスト・evals等で
    cl.user_session.get() が ChainlitContextException を送出する場合）は
    空文字列を返す。
    """
    try:
        plan = cl.user_session.get("plan")
    except Exception:
        return ""
    if not plan:
        return ""
    return _render_plan(plan)

_STATUS_MARKERS = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "completed": "[x]",
}


def _render_plan(plan: list[dict], *, finished: bool = False, heading: str = "### Plan") -> str:
    """計画（ステップ一覧）をチェックリスト形式の Markdown へ整形する。

    create_plan / update_task_progress の両方から呼ばれる表示専用の純関数。

    Args:
        plan: {"content": str, "activeForm": str,
            "status": "pending"|"in_progress"|"completed"} の辞書のリスト。
        finished: 全ステップ完了時に完了メッセージを付記するかどうか。
        heading: 先頭の見出し行。画面にだけ出す場合（approve_plan の確認）は
            表示言語の見出しを渡す（LLM へ返す場合は既定の日本語のまま）。

    Returns:
        「### 実行計画」見出しに続けて各ステップをチェックリストで並べた Markdown 文字列。
        "in_progress" のステップは content の代わりに activeForm を表示する。
    """
    lines = [heading]
    for step in plan:
        marker = _STATUS_MARKERS[step["status"]]
        text = step["activeForm"] if step["status"] == "in_progress" else step["content"]
        lines.append(f"- {marker} {text}")
    if finished:
        lines.append("\n✅ Plan completed")
    return "\n".join(lines)


# frontend/src/utils/messageTree.ts の PLAN_PREFIX と一致させる
# （サイドパネルの実行計画カード用マーカー。TOKEN_USAGE_PREFIX/WORK_DIR_PREFIX と同じ方式）。
PLAN_PREFIX = "📋 実行計画\n"


def _render_plan_payload(plan: list[dict], *, finished: bool = False, approved: bool = False) -> str:
    """サイドパネル表示用に plan を JSON 化し、プレフィックス付き文字列として返す。

    フロントエンドはこのプレフィックスでメインチャット窓への表示を除外し、
    専用の PlanCard コンポーネント、および送信ボタン付近の PlanModeBadge が
    JSON 部分をパースして描画する（approved は PlanModeBadge の Plan Mode /
    Edit Automatically 表示に使う）。
    """
    payload = {"steps": plan, "finished": finished, "approved": approved}
    return PLAN_PREFIX + json.dumps(payload, ensure_ascii=False)


def _plan_detail_path() -> Path | None:
    """create_plan が detail_markdown を書き出すファイルの絶対パスを決める。

    ファイル名は thread_id から機械的に決め打ちするため、呼び出し側（LLM）が
    任意パスへ書き込むことはできない。_state._PLANS_DIR 未設定（init_tools() 未実行
    や evals 等の簡易経路）なら None を返し、保存をスキップする。
    """
    if _state._PLANS_DIR is None:
        return None
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    return _state._PLANS_DIR / f"plan_{thread_id}.md"


def _write_plan_detail(plan: list[dict], detail_markdown: str) -> Path | None:
    """steps のスナップショットと detail_markdown を1ファイルへまとめて上書き保存する。

    create_plan が呼ばれるたびに全文を上書きする（update_task_progress では
    更新しないため、常に「直近の create_plan 呼び出し時点」のスナップショット
    になる）。
    """
    path = _plan_detail_path()
    if path is None:
        return None
    lines = [
        "# Plan",
        "",
        f"- Thread ID: {cl.user_session.get('thread_id') or '_no_session'}",
        f"- Created: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- Steps: {len(plan)}",
        "",
        "## Steps",
        "",
        *[f"{i + 1}. {s['content']}" for i, s in enumerate(plan)],
        "",
        "## Details",
        "",
        detail_markdown.rstrip(),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
