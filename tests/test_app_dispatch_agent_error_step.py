"""app.py の _is_dispatch_agent_error() の回帰テスト。

dispatch_agent（src/tools.py）は失敗時も例外を投げず「エラー: ...」形式の
文字列を正常な戻り値として返すため、on_tool_end はそのままだと常に正常
終了として扱い、UI（StepItem.tsx）上で成功と失敗が区別できなかった。
"""

from app import _is_dispatch_agent_error


def test_detects_dispatch_agent_error_string() -> None:
    assert _is_dispatch_agent_error("dispatch_agent", "Error: unknown agent_type 'foo'.") is True


def test_ignores_dispatch_agent_success() -> None:
    assert _is_dispatch_agent_error("dispatch_agent", "調査の結果、...") is False


def test_ignores_other_tools_even_with_error_prefix() -> None:
    assert _is_dispatch_agent_error("run_script", "Error: something") is False


def test_ignores_non_string_content() -> None:
    assert _is_dispatch_agent_error("dispatch_agent", {"content": "Error:"}) is False
    assert _is_dispatch_agent_error("dispatch_agent", None) is False
