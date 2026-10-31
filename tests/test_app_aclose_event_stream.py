"""app.py の _aclose_event_stream() の回帰テスト。

_CompactionCheckpoint 経路（コンテキスト圧縮のループ内安全点）で、要約の
LLM呼び出しを含む _run_context_compaction() を呼ぶ前に必ず event_stream を
閉じるようにした修正（2026-08-07、dispatch_agent(verifier)の孤立tool_call
incident）の土台となるヘルパー。閉じずに aupdate_state を呼ぶと、
バックグラウンドで生き続ける event_stream 側のグラフ実行（agent→toolsノード）
と競合し、進行中のツール呼び出しがLangGraph側で強制キャンセルされて
孤立tool_callを生む不具合があったため、このヘルパー自体の閉じる/失敗検知の
挙動を単体で検証する。
"""

import asyncio

import pytest

from app import _aclose_event_stream


class _FakeEventStream:
    def __init__(self, *, raise_exc: Exception | None = None, hang: bool = False) -> None:
        self._raise_exc = raise_exc
        self._hang = hang
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1
        if self._hang:
            await asyncio.sleep(10)
        if self._raise_exc is not None:
            raise self._raise_exc


@pytest.mark.asyncio
async def test_aclose_event_stream_normal_close_returns_false() -> None:
    stream = _FakeEventStream()

    failed = await _aclose_event_stream(stream)

    assert failed is False
    assert stream.aclose_calls == 1


@pytest.mark.asyncio
async def test_aclose_event_stream_swallows_exception_and_returns_true() -> None:
    stream = _FakeEventStream(raise_exc=RuntimeError("boom"))

    failed = await _aclose_event_stream(stream)

    assert failed is True


@pytest.mark.asyncio
async def test_aclose_event_stream_timeout_returns_true() -> None:
    stream = _FakeEventStream(hang=True)

    failed = await _aclose_event_stream(stream)

    assert failed is True


@pytest.mark.asyncio
async def test_aclose_event_stream_is_idempotent() -> None:
    """既にクローズ済みの event_stream に対して呼んでも安全（no-op）。

    _CompactionCheckpoint 経路で except 節内で明示的に閉じた後、共通の
    finally 節でも同じ event_stream に対して呼ばれるため、2回目の呼び出しが
    エラーにならないことを確認する（実際の async generator の aclose() は
    2回目以降は何もせず正常終了するのと同じ挙動を模擬）。
    """

    class _RealLikeStream:
        def __init__(self) -> None:
            self.closed = False
            self.aclose_calls = 0

        async def aclose(self) -> None:
            self.aclose_calls += 1
            self.closed = True  # 2回目以降も例外を出さない

    stream = _RealLikeStream()

    failed_first = await _aclose_event_stream(stream)
    failed_second = await _aclose_event_stream(stream)

    assert failed_first is False
    assert failed_second is False
    assert stream.aclose_calls == 2
