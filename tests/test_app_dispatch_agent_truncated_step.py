"""app.py の _is_dispatch_agent_truncated() の回帰テスト（ISSUE-001）。

src/subagent.py の run_subagent() は max_iterations到達・空応答連続・
トークン閾値超過・LLMタイムアウトによる打ち切りを例外にせず、
「[サブエージェント: ...ため打ち切りました]」形式の文字列を正常な
戻り値として返す。on_tool_end はそのままだと常に正常終了として扱い、
UI（StepItem.tsx）上で「停止」バッジが付かず、ユーザーへの通知も
行われなかった。
"""

from app import _is_dispatch_agent_truncated


def test_detects_dispatch_agent_truncated_string() -> None:
    content = "[Sub-agent: stopped because the maximum number of iterations (6) was reached]\n続き"
    assert _is_dispatch_agent_truncated("dispatch_agent", content) is True


def test_ignores_dispatch_agent_success() -> None:
    assert _is_dispatch_agent_truncated("dispatch_agent", "調査の結果、...") is False


def test_ignores_dispatch_agent_error() -> None:
    assert _is_dispatch_agent_truncated("dispatch_agent", "Error: unknown agent_type 'foo'.") is False


def test_ignores_other_tools_even_with_truncation_prefix() -> None:
    content = "[Sub-agent: stopped because the maximum number of iterations (6) was reached]"
    assert _is_dispatch_agent_truncated("run_script", content) is False


def test_ignores_non_string_content() -> None:
    assert _is_dispatch_agent_truncated("dispatch_agent", {"content": "[サブエージェント:"}) is False
    assert _is_dispatch_agent_truncated("dispatch_agent", None) is False
