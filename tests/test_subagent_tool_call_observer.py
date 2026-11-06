"""サブエージェントのツール呼び出しの観測（src/subagent.py）と、eval の生成物の記録（evals/output_inspect.py）のテスト。"""

from langchain_core.messages import ToolMessage

from evals import output_inspect
from evals.run_case import _subagent_call_recorder
from src import subagent


def test_observer_receives_each_tool_call_and_can_be_removed() -> None:
    calls, observe = _subagent_call_recorder()
    subagent.add_tool_call_observer(observe)
    try:
        tool_calls = [
            {"name": "read_skill", "args": {"skill_name": "excel-edit"}, "id": "a"},
            {"name": "execute_python_code", "args": {"code": "from xlkit import save\n" + "x" * 3000}, "id": "b"},
        ]
        results = [
            (ToolMessage(content="# excel-edit", tool_call_id="a", name="read_skill"), None),
            (ToolMessage(content="Error: boom", tool_call_id="b", name="execute_python_code"), None),
        ]
        subagent._notify_tool_call_observers("task one", 3, tool_calls, results)
        subagent._notify_tool_call_observers("task two", 1, tool_calls[:1], results[:1])
    finally:
        subagent.remove_tool_call_observer(observe)
    subagent._notify_tool_call_observers("task three", 1, tool_calls[:1], results[:1])

    assert [(c["dispatch"], c["name"], c["is_error"]) for c in calls] == [
        (1, "read_skill", False),
        (1, "execute_python_code", True),
        (2, "read_skill", False),
    ]
    assert calls[1]["args"]["code"].startswith("from xlkit import save")
    assert len(calls[1]["args"]["code"]) < 2000


def test_a_failing_observer_does_not_break_the_others() -> None:
    seen = []

    def broken(record):
        raise RuntimeError("x")

    subagent.add_tool_call_observer(broken)
    subagent.add_tool_call_observer(seen.append)
    try:
        subagent._notify_tool_call_observers(
            "t", 1, [{"name": "Read", "args": {}}], [(ToolMessage(content="ok", tool_call_id="r"), None)]
        )
    finally:
        subagent.remove_tool_call_observer(broken)
        subagent.remove_tool_call_observer(seen.append)
    assert [r["name"] for r in seen] == ["Read"]


def test_output_inspect_lists_new_files_and_summarizes_xlsx(tmp_path) -> None:
    import openpyxl
    from openpyxl.styles import Alignment, Font

    (tmp_path / "input.md").write_text("old", encoding="utf-8")
    before = output_inspect.snapshot(tmp_path)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"] = "日本語"
    ws["A1"].font = Font(name="Yu Gothic")
    ws["A2"] = "折り返す長い文"
    ws["A2"].alignment = Alignment(wrap_text=True)
    wb.save(tmp_path / "out.xlsx")
    scratch = tmp_path / "_tmp_thread" / "scripts"
    scratch.mkdir(parents=True)
    (scratch / "intermediate.json").write_text("{}", encoding="utf-8")

    rels = output_inspect.changed_files(tmp_path, before)
    assert rels == ["out.xlsx"]
    keep = tmp_path.parent / (tmp_path.name + "_kept")
    described = output_inspect.describe_outputs(tmp_path, rels, keep)
    sheet = described[0]["xlsx"][0]
    assert sheet["fonts"] == {"Yu Gothic": 1, "Calibri": 1}
    assert sheet["wrapped_rows_without_height"] == [2]
    assert (keep / "out.xlsx").exists()
