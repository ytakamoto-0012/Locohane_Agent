"""src/today.py（システムプロンプト末尾の今日の日付）のテスト。"""

from datetime import date

from src.today import today_block, with_today


def test_today_block_names_the_date_and_weekday():
    block = today_block(date(2026, 11, 8))
    assert "Today is 2026-11-08 (Sun)" in block
    assert "next year" in block


def test_with_today_keeps_the_prompt_prefix_unchanged():
    prompt = "# System\nrules..."
    out = with_today(prompt, date(2027, 1, 4))
    assert out.startswith(prompt)
    assert out.endswith(today_block(date(2027, 1, 4)))
    assert "(Mon)" in out
