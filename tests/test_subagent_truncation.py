"""src/subagent.py の is_truncated_result() の回帰テスト（ISSUE-001）。"""

from src.subagent import (
    _TOOL_RESULT_TOTAL_LIMIT,
    _build_truncation_message,
    _collect_tool_results_summary,
    is_truncated_result,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def test_build_truncation_message_is_detected_as_truncated() -> None:
    messages = [HumanMessage(content="task"), AIMessage(content="調査中...")]
    text = _build_truncation_message("最大反復回数(6)に達した", messages)
    assert is_truncated_result(text) is True


def test_normal_final_answer_is_not_truncated() -> None:
    assert is_truncated_result("調査の結果、問題ありませんでした。") is False


def test_non_string_is_not_truncated() -> None:
    assert is_truncated_result(None) is False
    assert is_truncated_result(123) is False


def test_collect_tool_results_summary_caps_total_length() -> None:
    """大量ファイル探索中の打ち切りで、合計サイズが際限なく増えないことの回帰テスト。

    実例: 30件超のファイルを読んでから打ち切られ、個々のスニペットは
    1500字に切り詰められていても合計が数万字になり、呼び出し元（メイン
    エージェント）自身のトークン上限を圧迫した。合計も上限で切ること。
    """
    messages: list = [HumanMessage(content="task")]
    for i in range(40):
        messages.append(ToolMessage(content="x" * 1500, name="Read", tool_call_id=f"c{i}"))
    summary = _collect_tool_results_summary(messages)
    assert len(summary) <= _TOOL_RESULT_TOTAL_LIMIT + 200  # 省略メッセージ分の余裕


def test_collect_tool_results_summary_keeps_most_recent_snippet() -> None:
    messages: list = [HumanMessage(content="task")]
    for i in range(40):
        messages.append(
            ToolMessage(content=f"marker-{i}" + "x" * 1500, name="Read", tool_call_id=f"c{i}")
        )
    summary = _collect_tool_results_summary(messages)
    assert "marker-39" in summary
    assert "marker-0" not in summary


def test_collect_tool_results_summary_short_input_unchanged() -> None:
    messages: list = [
        HumanMessage(content="task"),
        ToolMessage(content="short result", name="Read", tool_call_id="c0"),
    ]
    summary = _collect_tool_results_summary(messages)
    assert summary == "- tool=Read: short result"
