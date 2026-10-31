"""GET /slots による空き確認を補う、自プロセス側の使用状況（src/llm/routing.py）の回帰テスト。

- 予約（reserve_slot）: build_model() が選んでからまだ最初のリクエストを
  送っていない接続先を、同時に選択中の他の build_model() が空きとみなさない
  こと（priority_failover では全員が先頭を優先するため、予約が無いと全員が
  先頭を選んでしまう）。
"""

import asyncio

import httpx
import pytest

from src import llm
from src.config import LLMEndpoint
from src.llm.routing import SlotCounts, release_slot_reservation, reserve_slot


def _llama_cpp_endpoints(n: int) -> tuple[LLMEndpoint, ...]:
    return tuple(LLMEndpoint(base_url=f"http://host{i}/v1", api_key="dummy", model="m", provider="llama_cpp") for i in range(n))


@pytest.fixture(autouse=True)
def _fresh_usage_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.routing, "_SLOT_RESERVATIONS", {})
    monkeypatch.setattr(llm.routing, "_ENDPOINT_COOLDOWN_UNTIL", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["priority_failover", "round_robin"])
async def test_concurrent_selections_do_not_pick_the_same_free_slot(monkeypatch: pytest.MonkeyPatch, strategy: str) -> None:
    """同時に選択した2件が、1スロットずつ空いている2台を1台ずつ使うこと。"""

    async def _free_one(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        # 両方の選択が probe 待ちで並ぶよう、制御を一度返す。
        await asyncio.sleep(0)
        return SlotCounts(free=1, total=1)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _free_one)
    endpoints = _llama_cpp_endpoints(2)

    async def _select_and_reserve() -> str:
        # build_model() と同じく、選択直後に await を挟まず予約する。
        endpoint = await llm._select_endpoint("sub", endpoints, strategy, busy_poll_interval_seconds=0.01)
        reserve_slot(endpoint)
        return endpoint.base_url

    picks = await asyncio.gather(_select_and_reserve(), _select_and_reserve())
    assert sorted(picks) == [endpoints[0].base_url, endpoints[1].base_url]


@pytest.mark.asyncio
async def test_reservation_is_released_when_the_request_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """最初のリクエスト送信で予約が解除され、送信後はサーバー側の空き状況だけで判定されること。"""
    endpoints = _llama_cpp_endpoints(2)
    head_free = True

    async def _probe(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        if base_url == endpoints[0].base_url:
            return SlotCounts(free=1 if head_free else 0, total=1)
        return SlotCounts(free=1, total=1)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _probe)

    first = await llm._select_endpoint("main", endpoints, "priority_failover")
    token = reserve_slot(first)
    assert first.base_url == endpoints[0].base_url
    # 予約中は先頭を避けて次点へ。
    assert (await llm._select_endpoint("main", endpoints, "priority_failover")).base_url == endpoints[1].base_url

    release_slot_reservation(first.base_url, token)
    head_free = False  # 送信後はサーバーが生成中を返す。
    assert llm.routing._SLOT_RESERVATIONS["http://host0"] == {}
    assert (await llm._select_endpoint("main", endpoints, "priority_failover")).base_url == endpoints[1].base_url

    head_free = True
    assert (await llm._select_endpoint("main", endpoints, "priority_failover")).base_url == endpoints[0].base_url


@pytest.mark.asyncio
async def test_unused_reservation_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """送信されないまま捨てられたモデルの予約は、有効期限で失効すること。"""
    endpoints = _llama_cpp_endpoints(2)

    async def _free(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        return SlotCounts(free=1, total=1)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _free)
    monkeypatch.setattr(llm.routing, "_SLOT_RESERVATION_TTL_SECONDS", -1.0)

    reserve_slot(endpoints[0])
    assert (await llm._select_endpoint("main", endpoints, "priority_failover")).base_url == endpoints[0].base_url


@pytest.mark.asyncio
async def test_endpoint_busy_waits_until_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """全候補が埋まっている場合は、空きが出るまで待つこと（複数スロットのサーバーでも同じ）。"""
    endpoints = _llama_cpp_endpoints(1)
    probes = 0

    async def _busy_then_free(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        nonlocal probes
        probes += 1
        return SlotCounts(free=0, total=2) if probes == 1 else SlotCounts(free=1, total=2)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _busy_then_free)

    picked = await llm._select_endpoint("main", endpoints, "priority_failover", busy_poll_interval_seconds=0.01)
    assert picked.base_url == endpoints[0].base_url
    assert probes == 2


@pytest.mark.asyncio
async def test_probe_counts_free_and_total_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /slots の応答から空き数・総数を数え、スロット0件・不正な形式は確認不能（None）にすること。"""
    responses: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/slots"
        return httpx.Response(200, json=responses[request.url.host])

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        llm.routing.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(_handler), **kwargs),
    )

    responses["a"] = [{"is_processing": True}, {"is_processing": False}, {"is_processing": False}]
    responses["b"] = []
    responses["c"] = {"error": "x"}
    responses["d"] = ["not-a-dict"]
    probe = llm.routing._probe_llama_cpp_slots_available
    assert await probe("http://a/v1", 1.0) == SlotCounts(free=2, total=3)
    assert await probe("http://b/v1", 1.0) is None
    assert await probe("http://c/v1", 1.0) is None
    assert await probe("http://d/v1", 1.0) is None


@pytest.mark.asyncio
async def test_chat_model_releases_reservation_on_first_request() -> None:
    """ChatLlamaCpp は最初のリクエストで予約を解除し、2回目以降は予約を持たないこと。"""
    endpoint = _llama_cpp_endpoints(1)[0]
    token = reserve_slot(endpoint)
    model = llm.ChatLlamaCpp(base_url=endpoint.base_url, api_key="dummy", model="m", slot_reservation_token=token)

    model._release_slot_reservation()
    assert llm.routing._SLOT_RESERVATIONS["http://host0"] == {}
    assert model.slot_reservation_token is None
