"""ツールの表示名（UIのStepバッジとサブエージェントの進捗表示で共有する）。

UIのStepバッジは「ツール: <表示名>」（frontend/src/components/StepItem.tsx が
"ツール: " を前置する）、dispatch_agent の進捗表示（src/subagent.py の
on_activity）も同じ「ツール: <表示名>」を出す。両方がこの対応表を引くことで、
進捗表示とStepバッジの表記が食い違わないようにする。対応表に無いツール
（Read/Glob/Grep 等）はツール名そのものを表示名にする。
表示名の文言（日本語 / 英語）は src/i18n.py の "tool.<ツール名>" に置く。
"""

from __future__ import annotations

from src.i18n import MESSAGES, t

TOOL_LABEL_PREFIX = "tool."


def tool_label(name: str, lang: str | None = None) -> str:
    """ツール名から表示名を返す（対応表に無ければツール名そのもの）。lang 省略時はセッションの言語。"""
    key = TOOL_LABEL_PREFIX + name
    return t(key, lang) if key in MESSAGES else name
