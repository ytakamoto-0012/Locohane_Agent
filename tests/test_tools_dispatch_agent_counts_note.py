"""dispatch_agent が件数を書いた task に添える注記（src/tools/dispatch_agent.py）のテスト。"""

from src.tools.dispatch_agent import _task_with_counts_note


def test_worker_task_with_record_counts_gets_the_note():
    task = "トピック「2021 records」〜「2025 records」の記録（計40件、名前なし7件）で予定表を作る"
    out = _task_with_counts_note(task, "worker")
    assert out.startswith(task) and "[Note on counts]" in out and "records()" in out
    assert "[Note on counts]" in _task_with_counts_note(task, "verifier")


def test_no_note_for_explore_tasks_without_counts_or_without_records():
    with_counts = "records block の記録を3件ずつ確認する"
    assert _task_with_counts_note(with_counts, "explore") == with_counts
    no_counts = "records ブロックの記録から予定表を作る"
    assert _task_with_counts_note(no_counts, "worker") == no_counts
    docx = "報告書の表を12行に揃える"
    assert _task_with_counts_note(docx, "worker") == docx
