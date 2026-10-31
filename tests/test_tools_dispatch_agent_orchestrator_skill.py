"""dispatch_agent / dispatch_agent_batch の orchestrator_skill 空文字の扱いのテスト。

LLMが「省略」のつもりで orchestrator_skill="" を渡すと、"/SKILL.md" として
_safe_path に渡りサンドボックス外エラーで委譲自体が失敗していた（2026-10-03）。
"""

import pytest

from src.tools.dispatch_agent import _task_with_orchestrator_skill_hint


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_orchestrator_skill_is_treated_as_unspecified(value: str) -> None:
    assert _task_with_orchestrator_skill_hint("TASK", value) == ("TASK", None)
