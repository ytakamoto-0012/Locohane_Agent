"""LLM呼び出し・対象ツール実行の所要時間を実測するLangChainコールバック。

`config.ini` のtimeout系設定（`[llm].request_timeout_seconds` /
`[llm].stream_chunk_timeout_seconds` / `[scripts].timeout`）の適正値を実測ベースで
算出するために `evals/run_case.py` から利用する。本番コード（`src/` 配下）は
一切変更しない。

使い方:
    handler = LatencyCallbackHandler()
    run_config["callbacks"] = [handler]
    ...
    handler.reset()  # ターン開始前
    await graph.ainvoke(..., run_config)
    timing = handler.summary()  # ターン終了後
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

_TARGET_TOOL_NAMES = {"run_script", "execute_python_code"}


class LatencyCallbackHandler(BaseCallbackHandler):
    """1ターン分のLLM呼び出し・対象ツール実行の所要時間を計測する。"""

    def __init__(self) -> None:
        self.llm_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self._llm_starts: dict[UUID, float] = {}
        self._llm_chunk_times: dict[UUID, list[float]] = {}
        self._tool_starts: dict[UUID, tuple[str, float]] = {}

    def reset(self) -> None:
        """ターン開始前に呼び出し、直前ターンの計測データをクリアする。"""
        self.llm_calls.clear()
        self.tool_calls.clear()
        self._llm_starts.clear()
        self._llm_chunk_times.clear()
        self._tool_starts.clear()

    # --- LLM呼び出し ---
    # ChatOpenAI系（本番は ChatLlamaCpp）は BaseChatModel 経由のため
    # on_chat_model_start が呼ばれる。ハンドラが on_chat_model_start を
    # 実装していないと LangChain 側で on_llm_start へフォールバックされるが、
    # 挙動をライブラリのフォールバックに委ねず両方を明示的に実装しておく。

    def on_llm_start(self, serialized: dict, prompts: list[str], *, run_id: UUID, **kwargs: Any) -> None:
        self._start_llm_call(run_id)

    def on_chat_model_start(self, serialized: dict, messages: list, *, run_id: UUID, **kwargs: Any) -> None:
        self._start_llm_call(run_id)

    def _start_llm_call(self, run_id: UUID) -> None:
        self._llm_starts[run_id] = time.monotonic()
        self._llm_chunk_times[run_id] = []

    def on_llm_new_token(self, token: str, *, run_id: UUID, **kwargs: Any) -> None:
        chunk_times = self._llm_chunk_times.get(run_id)
        if chunk_times is not None:
            chunk_times.append(time.monotonic())

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish_llm_call(run_id)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish_llm_call(run_id)

    def _finish_llm_call(self, run_id: UUID) -> None:
        start = self._llm_starts.pop(run_id, None)
        chunk_times = self._llm_chunk_times.pop(run_id, [])
        if start is None:
            return
        end = time.monotonic()
        total_seconds = end - start
        if chunk_times:
            prefill_seconds = chunk_times[0] - start
            gaps = [b - a for a, b in zip(chunk_times, chunk_times[1:])]
            gaps.append(end - chunk_times[-1])
            max_chunk_gap_seconds = max([prefill_seconds, *gaps])
        else:
            # トークン単位のコールバックが発火しなかった場合（非ストリーミング
            # 応答等）は、開始→終了の全体を1つの「間隔」とみなす。
            prefill_seconds = total_seconds
            max_chunk_gap_seconds = total_seconds
        self.llm_calls.append(
            {
                "total_seconds": total_seconds,
                "prefill_seconds": prefill_seconds,
                "max_chunk_gap_seconds": max_chunk_gap_seconds,
                "chunk_count": len(chunk_times),
            }
        )

    # --- 対象ツール（run_script / execute_python_code）実行 ---

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id: UUID, **kwargs: Any) -> None:
        name = (serialized or {}).get("name", "")
        if name in _TARGET_TOOL_NAMES:
            self._tool_starts[run_id] = (name, time.monotonic())

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish_tool_call(run_id)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish_tool_call(run_id)

    def _finish_tool_call(self, run_id: UUID) -> None:
        entry = self._tool_starts.pop(run_id, None)
        if entry is None:
            return
        name, start = entry
        self.tool_calls.append({"name": name, "total_seconds": time.monotonic() - start})

    # --- 集計 ---

    def summary(self) -> dict[str, Any]:
        """直近ターン（前回の reset() 以降）の計測値を集計する。"""
        llm_totals = [c["total_seconds"] for c in self.llm_calls]
        llm_gaps = [c["max_chunk_gap_seconds"] for c in self.llm_calls]
        tool_totals = [c["total_seconds"] for c in self.tool_calls]
        return {
            "max_llm_total_seconds": max(llm_totals) if llm_totals else None,
            "max_stream_chunk_gap_seconds": max(llm_gaps) if llm_gaps else None,
            "max_script_seconds": max(tool_totals) if tool_totals else None,
            "llm_calls": list(self.llm_calls),
            "tool_calls": list(self.tool_calls),
        }
