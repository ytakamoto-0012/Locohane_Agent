"""設定ダッシュボードの管理アシスタント（admin/assistant.py）のテスト。"""

from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from admin import assistant
from admin.assistant_index import AssistantIndex, EmbeddingProvider
from admin.ini_catalog import KeyInfo, parse
from src.config import PROJECT_ROOT, load_config

_INI = """\
[llm]
# メインエージェントの接続先（llama.cpp server の URL）。
main_url = http://127.0.0.1:8080/v1
# 推論サーバーへ同時に送るリクエストの上限。同時に使う人が多いと待たされる。
max_concurrent_requests = 1

[graph]
# 同時に並列実行するツール呼び出しの最大数。
max_parallel = 4

[auth]
# チャット画面のログイン認証を有効にするか。
enabled = true

[vector_db]
# 社内文書の意味検索（ベクターDB）を有効にするか。
enabled = false
"""


def _keys() -> list[KeyInfo]:
    return parse(_INI).keys


def _views(overrides: dict[tuple[str, str], str] | None = None) -> dict:
    overrides = overrides or {}
    return {
        "keys": [
            {
                "section": k.section,
                "key": k.key,
                "default": k.default_value,
                "override": overrides.get((k.section, k.key)),
                "description": k.description,
                "group_heading": k.group_heading,
                "env_name": None,
                "env_override_active": False,
            }
            for k in _keys()
        ]
    }


def _ids(keys: list[KeyInfo]) -> list[str]:
    return [f"{k.section}.{k.key}" for k in keys]


# ---------------------------------------------------------------------------
# 関係するキーの選び出し
# ---------------------------------------------------------------------------


_README = """# 詳細

## ベクターDB（社内文書の意味検索）

社内文書を検索させるには [vector_db] enabled を true にする。

## 設定リファレンス（config.ini）

| セクション | キー | 説明 | 環境変数 |
|---|---|---|---|
| `[llm]` | `max_concurrent_requests` | 推論サーバーへの同時リクエスト数の上限 | `LLM_MAX_CONCURRENT_REQUESTS` |
"""


def _index(tmp_path) -> AssistantIndex:
    (tmp_path / "config.ini").write_text(_INI, encoding="utf-8")
    (tmp_path / "README_DETAIL.md").write_text(_README, encoding="utf-8")
    (tmp_path / "API_REFERENCE.md").write_text("# API\n\n## 変更履歴\n\n変更履歴を取得する。\n", encoding="utf-8")
    return AssistantIndex(
        config_ini_path=tmp_path / "config.ini",
        readme_path=tmp_path / "README_DETAIL.md",
        api_reference_path=tmp_path / "API_REFERENCE.md",
    )


def _choose(tmp_path, question, history=(), *, max_keys=3, max_docs=2, keys=None):
    return assistant.choose_context(
        _index(tmp_path),
        keys if keys is not None else _keys(),
        question,
        history=list(history),
        max_keys=max_keys,
        max_docs=max_docs,
        embedding=None,
    )


def test_choose_context_ranks_by_question(tmp_path):
    chosen, docs, mode = _choose(tmp_path, "同時に使うと待たされる")
    assert chosen[0] == ("llm", "max_concurrent_requests")
    assert mode == "bigram"


def test_choose_context_returns_related_docs(tmp_path):
    _, docs, _ = _choose(tmp_path, "社内文書を検索させたい")
    assert docs[0].title == "ベクターDB（社内文書の意味検索）"


def test_choose_context_puts_named_keys_first(tmp_path):
    chosen, _, _ = _choose(
        tmp_path, "それはどこで変えますか", history=[("assistant", "[auth] enabled を確認してください。")]
    )
    assert chosen[0] == ("auth", "enabled")


def test_choose_context_only_allowed_keys(tmp_path):
    only_graph = [k for k in _keys() if k.section == "graph"]
    chosen, _, _ = _choose(tmp_path, "同時に使うと待たされる", keys=only_graph)
    assert chosen == [("graph", "max_parallel")]


def test_search_query_adds_previous_user_message():
    history = [("user", "LLMにつながらない"), ("assistant", "モニターを見てください")]
    assert assistant.search_query("それはどこ？", history) == "それはどこ？\nLLMにつながらない"
    assert assistant.search_query("質問", []) == "質問"


def test_referenced_keys_only_known_and_unique():
    text = "[llm] max_concurrent_requests と `[llm] main_url`、もう一度 [llm] max_concurrent_requests。[foo] bar は無い。"
    assert assistant.referenced_keys(text, _keys()) == [("llm", "max_concurrent_requests"), ("llm", "main_url")]


# ---------------------------------------------------------------------------
# プロンプト・整形
# ---------------------------------------------------------------------------


def test_format_key_block_masks_sensitive_values_and_shows_override():
    views = {(v["section"], v["key"]): v for v in _views({("llm", "max_concurrent_requests"): "3"})["keys"]}
    views[("llm", "main_url")]["override"] = '[{"base_url": "http://x", "api_key": "sk-secret"}]'
    block = assistant.format_key_block([views[("llm", "main_url")], views[("llm", "max_concurrent_requests")]])
    assert "sk-secret" not in block and "127.0.0.1:8080" not in block
    assert "hidden because it is confidential" in block
    assert "Current value (changed on the screen): 3" in block


def test_format_key_block_lists_choices():
    view = dict(_views()["keys"][0], choices=["", "round_robin", "priority_failover"])
    assert "Allowed values: (empty), round_robin, priority_failover" in assistant.format_key_block([view])


def test_format_key_block_notes_env_override():
    view = dict(_views()["keys"][0], env_override_active=True, env_name="LLM_MAIN_URL")
    block = assistant.format_key_block([view])
    assert "environment variable LLM_MAIN_URL" in block


def test_build_system_prompt_contains_rules_and_context():
    prompt = assistant.build_system_prompt(
        guide="# ガイド本文",
        instance="org_a",
        instance_kind="app",
        instances=[{"name": "org_a", "display_name": "営業部", "kind": "app", "state": "running"}],
        view={"tab": "インスタンス", "instance": "org_a", "subview": "config.ini"},
        key_block="### [llm] main_url",
        doc_block="### README_DETAIL.md > ベクターDB",
    )
    assert "You cannot change settings" in prompt
    assert "Do not assert that" in prompt
    assert "### README_DETAIL.md > ベクターDB" in prompt
    assert "# ガイド本文" in prompt and "### [llm] main_url" in prompt
    assert "営業部" in prompt and "running" in prompt
    assert 'sub tab "config.ini"' in prompt


@pytest.mark.parametrize(
    "chunks, expected",
    [
        (["<think>考え中</think>答え"], "答え"),
        (["<thi", "nk>考え", "中</th", "ink>答", "え"], "答え"),
        (["前置き<think>x</think>本文"], "前置き本文"),
        (["a < b の比較"], "a < b の比較"),
        (["<think>終わらない思考"], ""),
    ],
)
def test_think_filter(chunks, expected):
    f = assistant.ThinkFilter()
    assert "".join(f.feed(c) for c in chunks) + f.flush() == expected


def test_shipped_guide_mentions_only_existing_keys():
    """ASSISTANT_GUIDE.md に書いた [section] key は、同梱の config.ini に実在する。"""
    keys = parse((PROJECT_ROOT / "config.ini").read_text(encoding="utf-8")).keys
    known = {(k.section, k.key) for k in keys}
    guide = assistant.GUIDE_PATH.read_text(encoding="utf-8")
    refs = assistant._KEY_REF_RE.findall(guide)
    assert refs
    assert [r for r in refs if r not in known] == []


def test_config_defaults():
    cfg = load_config()
    assert cfg.admin_assistant_enabled is True
    assert cfg.admin_assistant_max_keys >= 1
    assert cfg.admin_assistant_max_history_messages >= 1


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class _Chunk:
    def __init__(self, content):
        self.content = content


class FakeModel:
    def __init__(self, chunks=None, error=None):
        self.chunks = chunks or []
        self.error = error
        self.calls: list[list] = []

    async def astream(self, messages):
        self.calls.append(messages)
        for c in self.chunks:
            yield _Chunk(c)
        if self.error:
            raise self.error


def _make_client(tmp_path, model, *, enabled=True, llm_instance="", max_history=20, embedding_provider=None):
    guide = tmp_path / "guide.md"
    guide.write_text("# 画面ガイド\n- モニターを見る", encoding="utf-8")
    admin_config = dataclasses.replace(
        load_config(),
        admin_assistant_enabled=enabled,
        admin_assistant_llm_instance=llm_instance,
        admin_assistant_max_keys=3,
        admin_assistant_max_history_messages=max_history,
    )
    kinds = {"org_a": "app", "lab1": "skill_lab"}
    loaded: list[str] = []

    def instance_kind(name):
        if name not in kinds:
            raise HTTPException(status_code=404, detail=f"インスタンス {name!r} がありません。")
        return kinds[name]

    def config_loader(name):
        loaded.append(name)
        return admin_config

    app = FastAPI()
    app.include_router(
        assistant.build_router(
            require_login=lambda: "admin",
            require_csrf=lambda: None,
            admin_config=admin_config,
            catalog_keys=lambda name: _keys(),
            config_view=lambda name: _views(),
            list_instances=lambda: [{"name": n, "display_name": n, "kind": k, "state": "running"} for n, k in kinds.items()],
            instance_kind=instance_kind,
            config_loader=config_loader,
            index=_index(tmp_path),
            embedding_provider=embedding_provider or (lambda target, llm: None),
            model_factory=lambda cfg: model,
            guide_path=guide,
        )
    )
    return TestClient(app), loaded


def _events(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_chat_streams_answer_and_referenced_keys(tmp_path):
    model = FakeModel(["<think>…</think>1. config.ini で ", "[llm] max_concurrent_requests を 2 に", "します。"])
    client, loaded = _make_client(tmp_path, model)
    res = client.post(
        "/api/assistant/chat",
        json={
            "instance": "org_a",
            "messages": [{"role": "user", "content": "同時に使うと待たされる"}],
            "view": {"tab": "インスタンス", "instance": "org_a", "subview": "モニター"},
        },
    )
    assert res.status_code == 200
    events = _events(res)
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "<think>" not in deltas and deltas.startswith("1. config.ini")
    done = events[-1]
    assert done["type"] == "done"
    assert done["keys"] == [{"section": "llm", "key": "max_concurrent_requests"}]
    assert "<ol>" in done["html"]
    assert done["search"] == "bigram"
    assert loaded == ["org_a"]
    assert [e["type"] for e in events[:2]] == ["status", "status"]

    system = model.calls[0][0].content
    assert "# Screen guide" in system
    assert "### [llm] max_concurrent_requests" in system
    assert 'sub tab "モニター"' in system


def test_chat_uses_embedding_and_reports_sources(tmp_path):
    asked = []

    def embed(texts, kind):
        # 「社内文書」を含む文だけ同じ向きにする（言い回しの違いは埋め込みが吸収する想定）。
        return [[1.0, 0.0] if ("社内文書" in t or "ナレッジ" in t) else [0.0, 1.0] for t in texts]

    def provider(target, llm):
        asked.append((target, llm))
        return EmbeddingProvider(embed=embed, cache_id="fake", label="fake")

    model = FakeModel(["[vector_db] enabled を true にします。"])
    client, _ = _make_client(tmp_path, model, embedding_provider=provider)
    res = client.post("/api/assistant/chat", json={"instance": "org_a", "messages": [{"role": "user", "content": "ナレッジを使いたい"}]})
    done = _events(res)[-1]
    assert done["search"] == "embedding"
    assert asked == [("org_a", "org_a")]
    assert {"source": "README_DETAIL.md", "title": "ベクターDB（社内文書の意味検索）"} in done["sources"]
    system = model.calls[0][0].content
    assert "### [vector_db] enabled" in system
    assert "社内文書を検索させるには" in system


def test_chat_uses_fixed_llm_instance_and_trims_history(tmp_path):
    model = FakeModel(["はい"])
    client, loaded = _make_client(tmp_path, model, llm_instance="lab1", max_history=2)
    messages = [
        {"role": "user", "content": "1つ目"},
        {"role": "assistant", "content": "回答1"},
        {"role": "user", "content": "2つ目"},
    ]
    res = client.post("/api/assistant/chat", json={"instance": "org_a", "messages": messages})
    assert _events(res)[-1]["type"] == "done"
    assert loaded == ["lab1"]
    # system + 直近2件
    assert [m.content for m in model.calls[0][1:]] == ["回答1", "2つ目"]


def test_chat_reports_llm_failure_as_error_event(tmp_path):
    client, _ = _make_client(tmp_path, FakeModel(["途中まで"], error=ConnectionError("refused")))
    res = client.post("/api/assistant/chat", json={"instance": "org_a", "messages": [{"role": "user", "content": "x"}]})
    events = _events(res)
    assert events[-1]["type"] == "error"
    assert "refused" in events[-1]["message"]


def test_chat_empty_answer_is_error(tmp_path):
    client, _ = _make_client(tmp_path, FakeModel(["<think>考えただけ</think>"]))
    res = client.post("/api/assistant/chat", json={"instance": "org_a", "messages": [{"role": "user", "content": "x"}]})
    assert _events(res)[-1] == {"type": "error", "message": "LLM の応答が空でした。"}


def test_chat_rejects_bad_requests(tmp_path):
    client, _ = _make_client(tmp_path, FakeModel(["x"]))
    last_assistant = {"instance": "org_a", "messages": [{"role": "assistant", "content": "x"}]}
    assert client.post("/api/assistant/chat", json=last_assistant).status_code == 400
    unknown = {"instance": "nope", "messages": [{"role": "user", "content": "x"}]}
    assert client.post("/api/assistant/chat", json=unknown).status_code == 404
    too_long = {"instance": "org_a", "messages": [{"role": "user", "content": "あ" * (assistant.MAX_MESSAGE_CHARS + 1)}]}
    assert client.post("/api/assistant/chat", json=too_long).status_code == 422


def test_disabled_assistant(tmp_path):
    client, _ = _make_client(tmp_path, FakeModel(["x"]), enabled=False)
    assert client.get("/api/assistant/status").json() == {"enabled": False, "llm_instance": None}
    res = client.post("/api/assistant/chat", json={"instance": "org_a", "messages": [{"role": "user", "content": "x"}]})
    assert res.status_code == 404
