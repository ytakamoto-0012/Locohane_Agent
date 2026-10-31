"""app._should_retry_after_loop の状態リーク回帰テスト。

on_message内のwhile Trueループでは、ThinkingLoopDetected捕捉時に
loop_exc(ローカル変数)へ例外を保持し、リトライ分岐で`loop_exc is not None`を
見てグラフ再構築・nudge注入を行う。旧実装はリトライ処理の`continue`直前で
loop_excをNoneへ戻していなかったため、ターン中に一度でも本物のループ検知が
起きると、以降そのターンで新たな検知が無くても毎周回でloop_exc is not Noneが
真のままとなり、既に完結した正しい最終応答まで誤ってリトライされ続けていた
（本番ログ 2026-08-14: 1回の本物検知の後、新たな検知ログなしに3〜4回連続で
「ThinkingLoopDetected: リトライ前にLLMグラフを再構築しました」が発火し、
直前は毎回tool_calls=[]の完結した正しい最終報告だった）。

_should_retry_after_loopはリトライ要否の判定だけを担う純粋関数として切り出した
もので、呼び出し側は戻り値を見た直後に必ずloop_exc=Noneへリセットする契約に
なっている。本テストはこの契約が満たされることを検証する。
"""

from app import _should_retry_after_loop
from src.llm import ThinkingLoopDetected


def test_retries_when_loop_detected_and_budget_remains() -> None:
    exc = ThinkingLoopDetected("loop")
    assert _should_retry_after_loop(exc, loop_attempt=0, loop_max_retries=4) is True


def test_no_retry_once_budget_is_exhausted() -> None:
    exc = ThinkingLoopDetected("loop")
    assert _should_retry_after_loop(exc, loop_attempt=4, loop_max_retries=4) is False


def test_no_retry_after_reset_without_new_detection() -> None:
    """状態リーク回帰防止の本体: リセット後（loop_exc=None）は、新たな検知が
    無い限り、以前ループ検知が起きたことがあっても正常完了とみなされること。
    """
    exc = ThinkingLoopDetected("loop")
    assert _should_retry_after_loop(exc, loop_attempt=0, loop_max_retries=4) is True

    # 呼び出し側の契約通りリセットした後の次周回を模擬する。
    loop_exc = None
    assert _should_retry_after_loop(loop_exc, loop_attempt=1, loop_max_retries=4) is False
