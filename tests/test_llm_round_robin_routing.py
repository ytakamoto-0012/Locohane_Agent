"""src/llm/routing.py の round_robin ルーティング戦略の回帰テスト。

round_robin は呼び出しごとに順番に接続先を回すが、provider="llama_cpp" の
接続先だけは選ぶ前に GET /slots で空きスロットの有無を確認する
（_probe_llama_cpp_slots_available）。空きが無ければスキップして次点へ回し、
候補全てが埋まっていれば空きが出るまで待機する（_select_endpoint_with_slots_probe）。
provider="openai_compatible"（既定）の接続先は確認を行わず、従来通り即座に
選ばれる。priority_failover 戦略も同じ空き確認・待機を行う（並び順だけが
「クールダウン中でない接続先の優先順」になる）。

sticky戦略は「ルーティングとして有効ではなかった」ため完全に削除された
（本ファイルが置き換えたテストは以前 tests/test_llm_sticky_routing.py に
あった）。
"""

import pytest

from src import llm
from src.config import LLMEndpoint
from src.llm.routing import SlotCounts


def _slots(free: bool) -> SlotCounts:
    return SlotCounts(free=1 if free else 0, total=1)


def _endpoints(n: int) -> tuple[LLMEndpoint, ...]:
    return tuple(LLMEndpoint(base_url=f"http://host{i}/v1", api_key="dummy", model="m") for i in range(n))


def _llama_cpp_endpoints(n: int) -> tuple[LLMEndpoint, ...]:
    return tuple(LLMEndpoint(base_url=f"http://host{i}/v1", api_key="dummy", model="m", provider="llama_cpp") for i in range(n))


def _unique_session_id(suffix: str) -> str:
    return f"test-round-robin-{suffix}-{id(object())}"


@pytest.mark.asyncio
async def test_round_robin_cycles_through_openai_compatible_endpoints_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    """provider未指定（openai_compatible）は /slots を一切呼ばず、従来通り順番に回ること。"""

    async def _fail_if_called(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        raise AssertionError("openai_compatible の接続先で /slots を呼んではいけない")

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _fail_if_called)

    endpoints = _endpoints(3)
    session_id = _unique_session_id("cycle")
    try:
        llm.set_current_session(session_id)
        picks = []
        for _ in range(6):
            picked = await llm._select_endpoint("main", endpoints, "round_robin")
            picks.append(picked.base_url)

        # 1周目（先頭3件）で全接続先が重複なく登場し、2周目は同じ並びで
        # 繰り返される（周期性）こと。
        assert set(picks[:3]) == {e.base_url for e in endpoints}
        assert picks[:3] == picks[3:6]
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_round_robin_skips_busy_llama_cpp_endpoint_and_uses_free_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """空きスロットが無い接続先はスキップされ、空きのある接続先が選ばれ続けること。"""

    endpoints = _llama_cpp_endpoints(2)
    busy_url = endpoints[0].base_url
    free_url = endpoints[1].base_url

    async def _fake_probe(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        return _slots(base_url != busy_url)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _fake_probe)

    session_id = _unique_session_id("skip-busy")
    try:
        llm.set_current_session(session_id)
        for _ in range(5):
            picked = await llm._select_endpoint(
                "main",
                endpoints,
                "round_robin",
                probe_timeout_seconds=1.0,
                busy_poll_interval_seconds=0.01,
            )
            assert picked.base_url == free_url
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_round_robin_prefers_llama_cpp_only_when_openai_compatible_alternative_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """llama_cppの接続先が空き無しでも、providerがopenai_compatibleの接続先は確認なしで選ばれること。"""

    llama_endpoint = LLMEndpoint(base_url="http://llama/v1", api_key="dummy", model="m", provider="llama_cpp")
    plain_endpoint = LLMEndpoint(base_url="http://plain/v1", api_key="dummy", model="m")
    endpoints = (llama_endpoint, plain_endpoint)

    async def _always_busy(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        return _slots(False)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _always_busy)

    session_id = _unique_session_id("mixed-provider")
    try:
        llm.set_current_session(session_id)
        for _ in range(4):
            picked = await llm._select_endpoint(
                "main",
                endpoints,
                "round_robin",
                probe_timeout_seconds=1.0,
                busy_poll_interval_seconds=0.01,
            )
            assert picked.base_url == plain_endpoint.base_url
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_round_robin_waits_until_a_llama_cpp_slot_frees_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """候補全ての空きスロットが無い場合は待機し、空きが出た時点で選ばれること。"""

    calls: dict[str, int] = {}

    async def _fake_probe(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        calls[base_url] = calls.get(base_url, 0) + 1
        # 各接続先とも1回目は busy、2回目以降は空きとして扱う。
        return _slots(calls[base_url] > 1)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _fake_probe)

    endpoints = _llama_cpp_endpoints(2)
    session_id = _unique_session_id("wait-for-free")
    try:
        llm.set_current_session(session_id)
        picked = await llm._select_endpoint(
            "main",
            endpoints,
            "round_robin",
            probe_timeout_seconds=1.0,
            busy_poll_interval_seconds=0.01,
        )
        assert picked.base_url in {e.base_url for e in endpoints}
        # 1周目で両方busy判定を受けてから待機し、2周目で空きが見つかった
        # ことを示す（両方とも最低1回はprobeされている）。
        assert set(calls) == {e.base_url for e in endpoints}
        assert sum(calls.values()) >= 3
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_round_robin_single_endpoint_still_waits_for_free_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """接続先が1件しか無くても、round_robinはstrategyに関わらず即座に選ぶ早期リターンを通らず、
    空き確認・待機を行うこと（randomとの違い）。
    """
    call_count = 0

    async def _fake_probe(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        nonlocal call_count
        call_count += 1
        return _slots(call_count > 2)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _fake_probe)

    endpoints = _llama_cpp_endpoints(1)
    session_id = _unique_session_id("single-wait")
    try:
        llm.set_current_session(session_id)
        picked = await llm._select_endpoint(
            "main",
            endpoints,
            "round_robin",
            probe_timeout_seconds=1.0,
            busy_poll_interval_seconds=0.01,
        )
        assert picked.base_url == endpoints[0].base_url
        assert call_count == 3
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_random_strategy_skips_probe_for_single_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """randomは従来通り、接続先が1件なら空き確認なしで即座に選ぶこと。"""

    async def _fail_if_called(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        raise AssertionError("単一接続先ではprobeを呼んではいけない（random戦略）")

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _fail_if_called)

    endpoints = _llama_cpp_endpoints(1)
    session_id = _unique_session_id("fastpath-random")
    try:
        llm.set_current_session(session_id)
        picked = await llm._select_endpoint("main", endpoints, "random")
        assert picked.base_url == endpoints[0].base_url
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_priority_failover_skips_busy_head_and_uses_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """priority_failoverでも、先頭のllama_cpp接続先が空き無しなら次点が選ばれ、
    先頭に空きがあれば常に先頭が選ばれること（round_robinのように回さない）。
    """
    endpoints = _llama_cpp_endpoints(3)
    busy: set[str] = {endpoints[0].base_url}

    async def _fake_probe(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        return _slots(base_url not in busy)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _fake_probe)

    session_id = _unique_session_id("pf-skip-busy")
    try:
        llm.set_current_session(session_id)
        for _ in range(3):
            picked = await llm._select_endpoint(
                "main", endpoints, "priority_failover", probe_timeout_seconds=1.0, busy_poll_interval_seconds=0.01
            )
            assert picked.base_url == endpoints[1].base_url
        busy.clear()
        for _ in range(3):
            picked = await llm._select_endpoint(
                "main", endpoints, "priority_failover", probe_timeout_seconds=1.0, busy_poll_interval_seconds=0.01
            )
            assert picked.base_url == endpoints[0].base_url
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_priority_failover_does_not_probe_openai_compatible_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """priority_failoverでも、provider=openai_compatibleの接続先は確認なしで即座に選ばれること。"""

    async def _fail_if_called(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        raise AssertionError("openai_compatible の接続先で /slots を呼んではいけない")

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _fail_if_called)

    endpoints = _endpoints(2)
    session_id = _unique_session_id("pf-plain")
    try:
        llm.set_current_session(session_id)
        picked = await llm._select_endpoint("main", endpoints, "priority_failover")
        assert picked.base_url == endpoints[0].base_url
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_priority_failover_skips_cooldown_endpoint_before_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    """クールダウン中の接続先は空き確認の対象にもならず、次点から確認されること。"""
    endpoints = _llama_cpp_endpoints(2)
    probed: list[str] = []

    async def _fake_probe(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        probed.append(base_url)
        return _slots(True)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _fake_probe)
    monkeypatch.setattr(llm.routing, "_ENDPOINT_COOLDOWN_UNTIL", {})

    session_id = _unique_session_id("pf-cooldown")
    try:
        llm.set_current_session(session_id)
        first = await llm._select_endpoint("main", endpoints, "priority_failover")
        assert first.base_url == endpoints[0].base_url
        llm.mark_last_endpoint_failed("main")
        probed.clear()
        picked = await llm._select_endpoint("main", endpoints, "priority_failover")
        assert picked.base_url == endpoints[1].base_url
        assert probed == [endpoints[1].base_url]
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_priority_failover_single_endpoint_waits_for_free_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """priority_failoverでも、接続先が1件しか無くても空き確認・待機を行うこと。"""
    call_count = 0

    async def _fake_probe(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        nonlocal call_count
        call_count += 1
        return _slots(call_count > 2)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _fake_probe)

    endpoints = _llama_cpp_endpoints(1)
    session_id = _unique_session_id("pf-single-wait")
    try:
        llm.set_current_session(session_id)
        picked = await llm._select_endpoint(
            "main", endpoints, "priority_failover", probe_timeout_seconds=1.0, busy_poll_interval_seconds=0.01
        )
        assert picked.base_url == endpoints[0].base_url
        assert call_count == 3
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_priority_failover_wait_when_busy_false_picks_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """priority_failoverで全候補ビジーかつwait_when_busy=Falseなら、待たずに優先順の先頭を選ぶこと。"""

    async def _always_busy(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        return _slots(False)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _always_busy)

    endpoints = _llama_cpp_endpoints(2)
    session_id = _unique_session_id("pf-no-wait")
    try:
        llm.set_current_session(session_id)
        picked = await llm._select_endpoint(
            "main",
            endpoints,
            "priority_failover",
            probe_timeout_seconds=1.0,
            busy_poll_interval_seconds=5.0,
            wait_when_busy=False,
        )
        assert picked.base_url == endpoints[0].base_url
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_round_robin_treats_unknown_probe_result_as_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """/slots が確認不能（None、通信エラー等）の場合はフェイルセーフで空きありとみなし、待機しないこと。"""

    calls = 0

    async def _fake_unknown(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _fake_unknown)

    endpoints = _llama_cpp_endpoints(2)
    session_id = _unique_session_id("unknown-probe")
    try:
        llm.set_current_session(session_id)
        picked = await llm._select_endpoint(
            "main",
            endpoints,
            "round_robin",
            probe_timeout_seconds=1.0,
            busy_poll_interval_seconds=0.01,
        )
        assert picked.base_url in {e.base_url for e in endpoints}
        # 最初に確認した1件がNoneのため即選択され、2件目以降は確認され
        # ない（=待機の1周目すら発生しない）はず。
        assert calls == 1
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_round_robin_recomputes_eligible_endpoints_on_each_busy_wait_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """待機の周回ごとに使用可能時間帯（eligible_indices）を再計算すること。

    _select_endpoint_with_slots_probe が候補一覧を最初の1回だけ計算して固定して
    しまうと、空き待ちが長引いて start/end の境界をまたいだ場合に、新しく
    使用可能になった接続先を見逃してしまう（過去に見つかった問題の回帰）。
    """
    eligible_calls: list[list[int]] = []

    def _fake_eligible_indices(endpoints: tuple[LLMEndpoint, ...]) -> list[int]:
        # 1周目は index=1（plain_endpoint）がまだ時間帯外という想定で除外し、
        # 2周目以降は使用可能時間帯に入ったとして両方を候補にする。
        result = [0] if len(eligible_calls) == 0 else [0, 1]
        eligible_calls.append(result)
        return result

    monkeypatch.setattr(llm.routing, "_compute_eligible_indices", _fake_eligible_indices)

    async def _always_busy(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        return _slots(False)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _always_busy)

    busy_endpoint = LLMEndpoint(base_url="http://busy/v1", api_key="dummy", model="m", provider="llama_cpp")
    plain_endpoint = LLMEndpoint(base_url="http://plain/v1", api_key="dummy", model="m")
    endpoints = (busy_endpoint, plain_endpoint)

    index = await llm.routing._select_endpoint_with_slots_probe(
        "main",
        endpoints,
        "round_robin",
        probe_timeout_seconds=1.0,
        busy_poll_interval_seconds=0.01,
    )

    # 1周目はindex=0しか候補になく、それがbusyなので待機。2周目で候補一覧が
    # 再計算され、新たに候補入りしたindex=1（provider未指定なので確認不要）
    # が選ばれるはず。
    assert index == 1
    assert len(eligible_calls) >= 2


@pytest.mark.asyncio
async def test_round_robin_wait_when_busy_false_skips_wait_and_picks_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """wait_when_busy=False なら、全候補ビジーでも待機せず即座にフェイルセーフ選択すること。

    2026-08-26 ユーザー報告: 唯一の接続先が他タブの生成でビジーな間、
    on_chat_resume（会話履歴の切り替え）が固まる不具合の回帰テスト。
    on_chat_start/on_chat_resume は wait_when_busy=False を渡す
    （app.py _rebuild_graph docstring参照）。
    """
    calls = 0

    async def _always_busy(base_url: str, timeout_seconds: float) -> SlotCounts | None:
        nonlocal calls
        calls += 1
        return _slots(False)

    monkeypatch.setattr(llm.routing, "_probe_llama_cpp_slots_available", _always_busy)

    endpoints = _llama_cpp_endpoints(1)
    session_id = _unique_session_id("no-wait-busy")
    try:
        llm.set_current_session(session_id)
        picked = await llm._select_endpoint(
            "main",
            endpoints,
            "round_robin",
            probe_timeout_seconds=1.0,
            busy_poll_interval_seconds=5.0,
            wait_when_busy=False,
        )
        assert picked.base_url == endpoints[0].base_url
        # 待機ループ（busy_poll_interval_seconds=5秒）に入らず、1周目のprobeのみで返ること。
        assert calls == 1
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_last_selected_index_is_scoped_per_tab_not_per_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """同じ thread_id を複数タブで同時に開いた場合でも、_LAST_SELECTED_INDEX
    （ひいては role="sub" の inherit_from_role 継承）がタブ間で混線しないこと。

    2026-08-26 監査で発見: 以前は _LAST_SELECTED_INDEX を (role, thread_id) で
    キーイングしていたため、同じ会話を別タブで同時に開く（あるいは
    _stop_thread_generating で他タブから操作する）と、タブAのmain接続先選択が
    タブBの直近選択を上書きし、タブBのdispatch_agent（role="sub",
    inherit_from_role="main"）が誤ってタブAの接続先を継承しうる不具合があった。
    set_current_session(thread_id, tab_id=...) でタブ単位のキーに分離して修正した。
    """
    endpoints = _endpoints(2)  # provider未指定=openai_compatible。/slotsは呼ばれない。
    thread_id = _unique_session_id("shared-thread")
    tab_a = f"{thread_id}-tabA"
    tab_b = f"{thread_id}-tabB"

    try:
        # タブA・タブBが同じ thread_id（同じ会話）を開き、それぞれ独立に
        # role="main" の接続先を選ぶ（_rebuild_graph相当）。
        llm.set_current_session(thread_id, tab_id=tab_a)
        picked_a = await llm._select_endpoint("main", endpoints, "round_robin")

        llm.set_current_session(thread_id, tab_id=tab_b)
        picked_b = await llm._select_endpoint("main", endpoints, "round_robin")

        # 2件の接続先を交互に回すround_robinの性質上、連続する2回の呼び出しは
        # 異なる接続先を選ぶはず（前提の健全性チェック）。
        assert picked_a.base_url != picked_b.base_url

        # タブAのコンテキストに戻ってrole="sub"を選ぶと、タブBの選択ではなく
        # タブA自身が直近選んだ接続先を継承すること。
        llm.set_current_session(thread_id, tab_id=tab_a)
        picked_sub_a = await llm._select_endpoint(
            "sub", endpoints, "round_robin", inherit_from_role="main"
        )
        assert picked_sub_a.base_url == picked_a.base_url

        # 同様にタブBのコンテキストではタブB自身の選択を継承すること。
        llm.set_current_session(thread_id, tab_id=tab_b)
        picked_sub_b = await llm._select_endpoint(
            "sub", endpoints, "round_robin", inherit_from_role="main"
        )
        assert picked_sub_b.base_url == picked_b.base_url
    finally:
        llm.forget_session(thread_id)


def test_sticky_is_no_longer_a_valid_routing_strategy() -> None:
    """sticky戦略は完全に削除され、config.pyのバリデーションも受け付けないこと。"""
    from src.config import LLM_ROUTING_STRATEGIES

    assert "sticky" not in LLM_ROUTING_STRATEGIES
    assert LLM_ROUTING_STRATEGIES == {"round_robin", "random", "priority_failover"}
