"""excel-editスキル（skills/excel-edit/scripts/_ops.py）のdelete_chart op。

背景: add_chartの再試行で同一シートにグラフが重複生成された際、excel-editには
削除する手段が無く（add_sheet/delete_sheet、add_table/remove_tableのような
対になるopがadd_chartには存在しなかった）、LLMが「SKILL.mdにdelete_chartと
書いてある」と誤って思い込みopを呼び出しては失敗する、という展開になった
（2026-08-22 app.log調査）。実際には他のadd/delete系opと同様の欠落であり、
削除自体はopenpyxlの`ws._charts`（リスト）から該当要素を除くだけで実現できる。
"""

import sys
from pathlib import Path

import openpyxl
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "excel-edit" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _ops import apply_op  # noqa: E402


def _make_wb_with_two_charts():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    apply_op(
        wb,
        {
            "op": "set_range",
            "sheet": "Sheet1",
            "start_cell": "A1",
            "rows": [["月", "収入"], ["1月", 100], ["2月", 120]],
        },
    )
    apply_op(
        wb,
        {"op": "add_chart", "sheet": "Sheet1", "type": "bar", "data_range": "A1:B3", "anchor": "D1", "title": "Chart One"},
    )
    apply_op(
        wb,
        {"op": "add_chart", "sheet": "Sheet1", "type": "bar", "data_range": "A1:B3", "anchor": "D20", "title": "Chart Two"},
    )
    return wb, ws


class TestDeleteChartByIndex:
    def test_deletes_the_targeted_chart_and_keeps_the_other(self):
        wb, ws = _make_wb_with_two_charts()
        apply_op(wb, {"op": "delete_chart", "sheet": "Sheet1", "chart_index": 0})

        assert len(ws._charts) == 1

    def test_out_of_range_index_raises(self):
        wb, ws = _make_wb_with_two_charts()
        with pytest.raises(ValueError, match="chart_index does not exist"):
            apply_op(wb, {"op": "delete_chart", "sheet": "Sheet1", "chart_index": 5})


class TestDeleteChartByTitle:
    def test_deletes_the_matching_chart(self):
        wb, ws = _make_wb_with_two_charts()
        apply_op(wb, {"op": "delete_chart", "sheet": "Sheet1", "title": "Chart Two"})

        assert len(ws._charts) == 1

    def test_no_match_raises(self):
        wb, ws = _make_wb_with_two_charts()
        with pytest.raises(ValueError, match="No chart with a matching title"):
            apply_op(wb, {"op": "delete_chart", "sheet": "Sheet1", "title": "存在しないタイトル"})

    def test_ambiguous_title_raises(self):
        wb, ws = _make_wb_with_two_charts()
        apply_op(
            wb,
            {"op": "add_chart", "sheet": "Sheet1", "type": "bar", "data_range": "A1:B3", "anchor": "D40", "title": "Chart Two"},
        )
        with pytest.raises(ValueError, match="Several charts"):
            apply_op(wb, {"op": "delete_chart", "sheet": "Sheet1", "title": "Chart Two"})


def test_neither_chart_index_nor_title_raises():
    wb, ws = _make_wb_with_two_charts()
    with pytest.raises(ValueError, match="Either 'chart_index' or 'title'"):
        apply_op(wb, {"op": "delete_chart", "sheet": "Sheet1"})
