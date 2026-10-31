"""src/llm.py のセッション別クライアント管理の回帰テスト。

以前は build_model() が生成する httpx.AsyncClient を、プロセス全体で
共有する1つの weakref.WeakSet にまとめて登録しており、on_stop の
aclose_active_llm_clients() がそれを無差別に一括クローズしていた。
そのため、あるタブ（セッション）で停止ボタンを押すと、別タブで実行中の
リクエストまで "Cannot send a request, as the client has been closed"
で巻き添え停止する不具合があった。

本テストは、set_current_session() で設定したセッションIDごとに
クライアントが分けて登録され、aclose_active_llm_clients(session_id) が
指定セッション分だけをクローズすることを確認する。
"""

import pytest

from src import llm
from src.config import load_config


def _unique_session_id(suffix: str) -> str:
    # テスト間で _active_async_clients の辞書キーが衝突しないよう、
    # テストごとに一意のセッションIDを使う。
    return f"test-session-{suffix}-{id(object())}"


@pytest.mark.asyncio
async def test_build_model_registers_client_under_current_session() -> None:
    config = load_config()
    session_id = _unique_session_id("register")
    llm.set_current_session(session_id)
    model = await llm.build_model(config)
    try:
        clients = llm._active_async_clients.get(session_id)
        assert clients is not None
        assert len(clients) == 1
        assert model.http_async_client in clients
    finally:
        await llm.aclose_active_llm_clients(session_id)


@pytest.mark.asyncio
async def test_aclose_active_llm_clients_only_closes_target_session() -> None:
    config = load_config()
    session_a = _unique_session_id("a")
    session_b = _unique_session_id("b")

    llm.set_current_session(session_a)
    model_a = await llm.build_model(config)
    llm.set_current_session(session_b)
    model_b = await llm.build_model(config)

    try:
        await llm.aclose_active_llm_clients(session_a)

        assert model_a.http_async_client.is_closed is True
        assert model_b.http_async_client.is_closed is False
        assert session_a not in llm._active_async_clients
        assert session_b in llm._active_async_clients
    finally:
        await llm.aclose_active_llm_clients(session_b)


@pytest.mark.asyncio
async def test_forget_session_removes_dict_key_without_closing() -> None:
    config = load_config()
    session_id = _unique_session_id("forget")
    llm.set_current_session(session_id)
    model = await llm.build_model(config)

    try:
        llm.forget_session(session_id)

        assert session_id not in llm._active_async_clients
        assert model.http_async_client.is_closed is False
    finally:
        await model.http_async_client.aclose()
