"""P0-1: build_model() が request_timeout を ChatLlamaCpp に設定する回帰テスト。

2026-07-28 incident で判明した構造的な欠陥の回帰防止:
langchain_openai の ChatOpenAI は内部属性 self.timeout を持っており、
既定値は None。openai SDK は毎リクエスト build_request(timeout=...) で
self.timeout を httpx リクエストに渡す。self.timeout が None だと、
httpx.AsyncClient に設定した httpx.Timeout が「無視される」のではなく、
openai SDK 側で「明示的な無制限指定」として扱われる（2026-07-28 incident）。

本テストは、build_model() が生成したモデルの request_timeout が
None でない（httpx.Timeout オブジェクトである）ことを確認する。
"""

import httpx
import pytest

from src import llm
from src.config import load_config


@pytest.mark.asyncio
async def test_build_model_sets_request_timeout() -> None:
    """build_model() が生成した ChatLlamaCpp の request_timeout が None でないこと。"""
    config = load_config()
    session_id = f"test-session-timeout-{id(object())}"
    llm.set_current_session(session_id)
    model = await llm.build_model(config)
    try:
        # request_timeout が httpx.Timeout である（None でない）ことを確認。
        # これにより openai SDK が毎リクエスト self.timeout で httpx の
        # timeout を上書きする際に「無制限指定」ではなく実値が使われる。
        assert model.request_timeout is not None
        assert isinstance(model.request_timeout, httpx.Timeout)
        # config.ini の [llm].request_timeout_seconds の値が反映されている。
        assert model.request_timeout.read == config.request_timeout_seconds
        assert model.request_timeout.connect == 10.0
    finally:
        await llm.aclose_active_llm_clients(session_id)


@pytest.mark.asyncio
async def test_build_model_httpx_client_has_timeout() -> None:
    """build_model() が生成する httpx.AsyncClient の timeout が None でないこと。"""
    config = load_config()
    session_id = f"test-session-httpx-timeout-{id(object())}"
    llm.set_current_session(session_id)
    model = await llm.build_model(config)
    try:
        assert model.http_async_client.timeout is not None
        assert isinstance(model.http_async_client.timeout, httpx.Timeout)
        assert model.http_async_client.timeout.read == config.request_timeout_seconds
    finally:
        await llm.aclose_active_llm_clients(session_id)


def test_describe_current_task_returns_non_empty() -> None:
    """describe_current_task() が空でない診断文字列を返すこと。

    同期テストでは asyncio.current_task() が None を返すため、
    "task=NONE" が含まれる。
    """
    desc = llm.describe_current_task()
    assert isinstance(desc, str)
    assert len(desc) > 0
    # 同期コンテキストでは task=NONE、非同期では name= を含む。
    assert "name=" in desc or "task=NONE" in desc


def test_cancel_scope_watcher_pattern() -> None:
    """_CancelScopeBreakageWatcher.PATTERN が正しい値を持つこと。"""
    watcher = llm._CancelScopeBreakageWatcher()
    assert watcher.PATTERN == "Attempted to exit cancel scope in a different task"


def test_recent_cancel_scope_breakage_returns_int() -> None:
    """recent_cancel_scope_breakage() が int を返すこと。"""
    count = llm.recent_cancel_scope_breakage()
    assert isinstance(count, int)
    assert count >= 0
