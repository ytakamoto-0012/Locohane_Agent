"""サブエージェントのモデル指定（agents/*.md の model / dispatch_agent の model 引数）の回帰テスト。

指定モデルの接続先だけを候補にルーティング戦略で選び、一致する接続先が
無ければ指定を無視して通常のルーティングに従うこと（src/llm/routing.py の
_select_endpoint の preferred_model）。
"""

import pytest

from src import llm
from src.agent_types import scan_agent_types
from src.config import LLMEndpoint


def _endpoints() -> tuple[LLMEndpoint, ...]:
    return (
        LLMEndpoint(base_url="http://a0/v1", api_key="dummy", model="A"),
        LLMEndpoint(base_url="http://b1/v1", api_key="dummy", model="B"),
        LLMEndpoint(base_url="http://a2/v1", api_key="dummy", model="A"),
        LLMEndpoint(base_url="http://c3/v1", api_key="dummy", model="C"),
    )


def _unique_session_id(suffix: str) -> str:
    return f"test-sub-model-{suffix}-{id(object())}"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["round_robin", "random", "priority_failover"])
async def test_preferred_model_limits_candidates(strategy: str) -> None:
    session_id = _unique_session_id(strategy)
    try:
        llm.set_current_session(session_id)
        picks = {(await llm._select_endpoint("sub", _endpoints(), strategy, preferred_model="A")).base_url for _ in range(8)}
        assert picks <= {"http://a0/v1", "http://a2/v1"}
        if strategy == "round_robin":
            assert picks == {"http://a0/v1", "http://a2/v1"}
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_unknown_model_falls_back_to_normal_routing() -> None:
    session_id = _unique_session_id("unknown")
    try:
        llm.set_current_session(session_id)
        picks = {(await llm._select_endpoint("sub", _endpoints(), "round_robin", preferred_model="Z")).base_url for _ in range(4)}
        assert picks == {e.base_url for e in _endpoints()}
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_model_outside_time_window_falls_back_to_normal_routing() -> None:
    endpoints = (
        LLMEndpoint(base_url="http://a0/v1", api_key="dummy", model="M"),
        LLMEndpoint(base_url="http://x1/v1", api_key="dummy", model="X", start=0.0, end=0.0001),
    )
    session_id = _unique_session_id("time-window")
    try:
        llm.set_current_session(session_id)
        picked = await llm._select_endpoint("sub", endpoints, "priority_failover", preferred_model="X")
        assert picked.base_url == "http://a0/v1"
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_inherit_from_main_is_skipped_when_model_differs() -> None:
    endpoints = _endpoints()
    session_id = _unique_session_id("inherit")
    try:
        llm.set_current_session(session_id)
        main_pick = await llm._select_endpoint("main", endpoints, "priority_failover")
        assert main_pick.model == "A"
        # 継承先（A）が指定モデルと一致すればそのまま継承する。
        same = await llm._select_endpoint("sub", endpoints, "priority_failover", inherit_from_role="main", preferred_model="A")
        assert same.base_url == main_pick.base_url
        # 一致しなければ継承せず指定モデルの接続先から選ぶ。
        other = await llm._select_endpoint("sub", endpoints, "priority_failover", inherit_from_role="main", preferred_model="C")
        assert other.base_url == "http://c3/v1"
    finally:
        llm.forget_session(session_id)


def test_preferred_sub_model_contextvar_set_and_reset() -> None:
    assert llm.get_preferred_sub_model() is None
    token = llm.set_preferred_sub_model("  A  ")
    assert llm.get_preferred_sub_model() == "A"
    llm.reset_preferred_sub_model(token)
    token = llm.set_preferred_sub_model("")
    assert llm.get_preferred_sub_model() is None
    llm.reset_preferred_sub_model(token)


def test_agent_type_frontmatter_model(tmp_path) -> None:
    (tmp_path / "with-model.md").write_text("---\nname: with-model\ndescription: d\nmodel: QWEN3.6_35B-A3B\n---\nbody\n", encoding="utf-8")
    (tmp_path / "no-model.md").write_text("---\nname: no-model\ndescription: d\n---\nbody\n", encoding="utf-8")
    by_name = {a.name: a for a in scan_agent_types(tmp_path)}
    assert by_name["with-model"].model == "QWEN3.6_35B-A3B"
    assert by_name["no-model"].model is None


@pytest.mark.asyncio
async def test_preferred_model_match_ignores_case_and_spaces() -> None:
    session_id = _unique_session_id("case")
    try:
        llm.set_current_session(session_id)
        picked = await llm._select_endpoint("sub", _endpoints(), "priority_failover", preferred_model=" c ")
        assert picked.base_url == "http://c3/v1"
    finally:
        llm.forget_session(session_id)


def test_agent_type_frontmatter_model_inherit_is_unspecified(tmp_path) -> None:
    (tmp_path / "inherit-model.md").write_text("---\nname: inherit-model\ndescription: d\nmodel: inherit\n---\nbody\n", encoding="utf-8")
    (tmp_path / "empty-model.md").write_text("---\nname: empty-model\ndescription: d\nmodel: \"\"\n---\nbody\n", encoding="utf-8")
    by_name = {a.name: a for a in scan_agent_types(tmp_path)}
    assert by_name["inherit-model"].model is None
    assert by_name["empty-model"].model is None


class _StopBuild(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_model", "default_model", "expected"),
    [(None, "B", (None, "B")), ("A", "B", ("A", "B")), (None, None, (None, None))],
)
async def test_build_model_uses_sub_default_model_when_unspecified(monkeypatch, tmp_path, job_model, default_model, expected) -> None:
    import dataclasses

    from src.config import load_config
    from src.llm import chat_model

    config = dataclasses.replace(load_config(overrides_path=tmp_path / "missing.json"), sub_default_model=default_model)
    captured: dict = {}

    async def _fake_select_endpoint(*args, **kwargs):
        captured["models"] = (kwargs.get("preferred_model"), kwargs.get("fallback_model"))
        raise _StopBuild

    monkeypatch.setattr(chat_model, "_select_endpoint", _fake_select_endpoint)
    token = llm.set_preferred_sub_model(job_model)
    try:
        with pytest.raises(_StopBuild):
            await llm.build_model(config, role="sub")
        assert captured["models"] == expected
        # メインエージェントには適用しない。
        with pytest.raises(_StopBuild):
            await llm.build_model(config, role="main")
        assert captured["models"] == (None, None)
    finally:
        llm.reset_preferred_sub_model(token)


def test_sub_default_model_config_parsing(monkeypatch, tmp_path) -> None:
    from src.config import load_config

    monkeypatch.setenv("LLM_SUB_DEFAULT_MODEL", "  B  ")
    assert load_config(overrides_path=tmp_path / "missing.json").sub_default_model == "B"
    monkeypatch.setenv("LLM_SUB_DEFAULT_MODEL", "")
    assert load_config(overrides_path=tmp_path / "missing.json").sub_default_model is None


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["round_robin", "random", "priority_failover"])
async def test_fallback_model_used_when_unspecified(strategy: str) -> None:
    session_id = _unique_session_id(f"fallback-{strategy}")
    try:
        llm.set_current_session(session_id)
        picks = {(await llm._select_endpoint("sub", _endpoints(), strategy, fallback_model="a")).base_url for _ in range(8)}
        assert picks <= {"http://a0/v1", "http://a2/v1"}
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_unknown_preferred_model_falls_back_to_default_model() -> None:
    session_id = _unique_session_id("fallback-unknown")
    try:
        llm.set_current_session(session_id)
        picks = {
            (await llm._select_endpoint("sub", _endpoints(), "round_robin", preferred_model="Z", fallback_model="B")).base_url
            for _ in range(4)
        }
        assert picks == {"http://b1/v1"}
        # 指定モデルが有効ならそちらが優先される。
        picked = await llm._select_endpoint("sub", _endpoints(), "priority_failover", preferred_model="C", fallback_model="B")
        assert picked.base_url == "http://c3/v1"
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["round_robin", "priority_failover"])
async def test_preferred_model_outside_time_window_falls_back_to_default_model(strategy: str) -> None:
    endpoints = (
        LLMEndpoint(base_url="http://a0/v1", api_key="dummy", model="A"),
        LLMEndpoint(base_url="http://b1/v1", api_key="dummy", model="B"),
        LLMEndpoint(base_url="http://x2/v1", api_key="dummy", model="X", start=0.0, end=0.0001),
    )
    session_id = _unique_session_id(f"fallback-window-{strategy}")
    try:
        llm.set_current_session(session_id)
        for _ in range(3):
            picked = await llm._select_endpoint("sub", endpoints, strategy, preferred_model="X", fallback_model="B")
            assert picked.base_url == "http://b1/v1"
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_unknown_default_model_falls_back_to_normal_routing() -> None:
    session_id = _unique_session_id("fallback-none")
    try:
        llm.set_current_session(session_id)
        picks = {(await llm._select_endpoint("sub", _endpoints(), "round_robin", fallback_model="Z")).base_url for _ in range(4)}
        assert picks == {e.base_url for e in _endpoints()}
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_inherit_from_main_with_default_model() -> None:
    endpoints = _endpoints()
    session_id = _unique_session_id("fallback-inherit")
    try:
        llm.set_current_session(session_id)
        main_pick = await llm._select_endpoint("main", endpoints, "priority_failover")
        assert main_pick.model == "A"
        # メインが既定モデルと同じなら継承する。
        same = await llm._select_endpoint("sub", endpoints, "priority_failover", inherit_from_role="main", fallback_model="A")
        assert same.base_url == main_pick.base_url
        # 別モデルなら継承せず既定モデルの接続先から選ぶ。
        other = await llm._select_endpoint("sub", endpoints, "priority_failover", inherit_from_role="main", fallback_model="B")
        assert other.base_url == "http://b1/v1"
    finally:
        llm.forget_session(session_id)


@pytest.mark.asyncio
async def test_inherit_kept_when_models_outside_time_window() -> None:
    endpoints = (
        LLMEndpoint(base_url="http://a0/v1", api_key="dummy", model="A"),
        LLMEndpoint(base_url="http://b1/v1", api_key="dummy", model="B"),
        LLMEndpoint(base_url="http://x2/v1", api_key="dummy", model="X", start=0.0, end=0.0001),
    )
    session_id = _unique_session_id("inherit-window")
    try:
        llm.set_current_session(session_id)
        main_pick = await llm._select_endpoint("main", endpoints, "priority_failover")
        # 指定・既定モデルが全て時間帯外なら、モデル指定が無いのと同じくメインを継承する。
        for _ in range(4):
            picked = await llm._select_endpoint("sub", endpoints, "round_robin", inherit_from_role="main", preferred_model="X")
            assert picked.base_url == main_pick.base_url
    finally:
        llm.forget_session(session_id)
