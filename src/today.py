"""システムプロンプトの末尾に今日の日付を付ける。

system_prompt はサーバー起動時に1回だけ組み立てられ、日付を含まない。そのため
LLM は「来年」「今年」「明日」のような相対的な日付を、データに出てくる最新の年
から推測していた（eval 実例: 2021〜2025年の記録から「今年=2025年」とみなし、
2026-11-08 時点の「来年」を 2026年として予定表を作った）。

日付はプロンプトの末尾に付ける。プロンプトの前方は変わらないため推論サーバーの
プレフィックスキャッシュは保たれ、末尾が変わるのも1日1回だけである。
メインエージェント（src/graph.py）とサブエージェント（src/subagent.py）の
両方がこの関数を通す。
"""

from __future__ import annotations

from datetime import date

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def today_block(today: date | None = None) -> str:
    """今日の日付を知らせるプロンプト末尾のブロックを返す。

    Args:
        today: 基準日（テスト用。省略時は実行環境のローカル日付）。

    Returns:
        先頭に空行を含むテキストブロック。
    """
    d = today or date.today()
    return (
        "\n\n# Today\n"
        f"Today is {d:%Y-%m-%d} ({_WEEKDAYS[d.weekday()]}). Resolve relative dates in the request "
        '("this year", "next year", "next month", "tomorrow") from this date, not from the latest '
        "date found in the data."
    )


def with_today(system_prompt: str, today: date | None = None) -> str:
    """system_prompt の末尾に today_block() を付けた文字列を返す。"""
    return system_prompt + today_block(today)
