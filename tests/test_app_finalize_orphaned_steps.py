"""app.py の _finalize_orphaned_steps() の回帰テスト。

evals/ のヘッドレスハーネス（run_case.py）は src/graph.py の
ainvoke_ensuring_final_text() を直接呼ぶ経路のため、app.py の on_message() に
ある cl.Step 管理コード（本テストの対象）を経由しない。そのため、UI側で
ThinkingLoopDetected/GraphRecursionError 発生時に進行中の Step
（dispatch_agent 等）が「実行中」のまま孤立するバグの再発防止は、
evals ではなくここで検証する。
"""

import pytest

from app import _finalize_orphaned_steps


class _FakeStep:
    def __init__(self) -> None:
        self.metadata: dict | None = None
        self.end = None
        self.update_calls = 0

    async def update(self) -> None:
        self.update_calls += 1


@pytest.mark.asyncio
async def test_finalize_orphaned_steps_sets_end_and_stopped_reason() -> None:
    step = _FakeStep()
    steps = {"run-1": step}

    await _finalize_orphaned_steps(steps, "loop_detected")

    assert step.end is not None
    assert step.metadata == {"stopped_reason": "loop_detected"}
    assert step.update_calls == 1


@pytest.mark.asyncio
async def test_finalize_orphaned_steps_finalizes_all_and_clears_dict() -> None:
    steps = {"run-1": _FakeStep(), "run-2": _FakeStep()}

    await _finalize_orphaned_steps(steps, "recursion_limit")

    assert steps == {}


@pytest.mark.asyncio
async def test_finalize_orphaned_steps_noop_on_empty_dict() -> None:
    steps: dict = {}

    await _finalize_orphaned_steps(steps, "interrupted")

    assert steps == {}
