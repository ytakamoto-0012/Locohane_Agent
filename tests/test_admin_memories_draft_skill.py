"""設定ダッシュボードの「メモリー」からドラフトスキルを作る機能（admin/memories.py の draft-skill）のテスト。"""

from __future__ import annotations

import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin import i18n as admin_i18n
from admin import instances as inst
from admin import memories
from src import memory as mem_store
from src.skills import _parse_frontmatter

_SKILL_MD = """Here is the skill:
```markdown
---
name: {name}
description: "Checks the monthly report: totals and format"
extra: ignored
---

# Monthly report check

1. Open the report.
```
"""


class _FakeModel:
    """LLM の代わり。受け取ったプロンプトを記録し、決まった SKILL.md を返す。"""

    calls: list = []
    reply_name = "monthly-report-check"

    def __init__(self, config):
        self.config = config

    async def ainvoke(self, messages):
        _FakeModel.calls.append((self.config.name, messages[0].content, messages[1].content))
        return types.SimpleNamespace(content=f"<think>x</think>{_SKILL_MD.format(name=_FakeModel.reply_name)}")


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "instances"
    (root / "default").mkdir(parents=True)
    inst.write_instance(root, inst.InstanceMeta(name="default", display_name="default", app_port=18001))
    memory_dir = tmp_path / "memory"
    draft_dir = tmp_path / "skill_drafts"
    skills_dir = tmp_path / "skills"
    (skills_dir / "official-one").mkdir(parents=True)
    (skills_dir / "official-one" / "SKILL.md").write_text("---\nname: official-one\ndescription: d\n---\n", encoding="utf-8")
    audit_log = tmp_path / "audit.log"
    cfg = lambda name: types.SimpleNamespace(  # noqa: E731
        memory_dir=memory_dir,
        name=name,
        skill_draft_dir=draft_dir,
        skills_dir=skills_dir,
        locohane_skills_dirs=[],
        skill_other_users_drafts="hidden",
    )
    app = FastAPI()
    app.add_middleware(admin_i18n.LanguageMiddleware)
    app.include_router(
        memories.build_router(
            require_login=lambda: "admin",
            require_csrf=lambda: None,
            instances_root=root,
            audit_log_path=audit_log,
            client_ip=lambda _r: "127.0.0.1",
            config_loader=cfg,
            backup_keep=2,
            llm_instance=lambda name: "llm-inst",
            model_factory=lambda config: _FakeModel(config),
            user_candidates=lambda name: ["Bob", "alice"],
        )
    )
    _FakeModel.calls = []
    _FakeModel.reply_name = "monthly-report-check"
    return {"client": TestClient(app), "memory_dir": memory_dir, "draft_dir": draft_dir, "audit": audit_log}


def test_owner_candidates_default_anonymous(env):
    mem_store.create_memory(env["memory_dir"], "m", "d", "feedback", "c", owner="carol")
    (env["draft_dir"] / "dave" / "x").mkdir(parents=True)
    (env["draft_dir"] / "_workspace").mkdir()
    data = env["client"].get("/api/instances/default/memories/draft-skill-owners").json()
    assert data["default"] == "anonymous"
    assert data["owners"] == ["anonymous", "bob", "alice", "dave", "carol"]


def test_create_draft_skill_from_memories(env):
    client = env["client"]
    mem_store.create_memory(env["memory_dir"], "rule-a", "合計の確認", "feedback", "合計は税込で確認する", owner="alice")
    mem_store.create_memory(env["memory_dir"], "common-b", "書式", "reference", "書式は A4 縦")
    res = client.post(
        "/api/instances/default/memories/draft-skill",
        json={"items": [{"name": "rule-a", "owner": "alice"}, {"name": "common-b"}], "owner": "Bob", "request": "月次用"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert (data["owner"], data["skill_name"]) == ("bob", "monthly-report-check")
    skill_dir = env["draft_dir"] / "bob" / "monthly-report-check"
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    fm = _parse_frontmatter(text)
    assert fm == {"name": "monthly-report-check", "description": "Checks the monthly report: totals and format"}
    assert "1. Open the report." in text and "```" not in text
    assert (skill_dir / "references").is_dir() and (skill_dir / "evals").is_dir()
    meta = json.loads((skill_dir / "_draft_meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "draft" and meta["author"].endswith("(dashboard)")
    assert meta["source_memories"] == [{"name": "rule-a", "owner": "alice"}, {"name": "common-b", "owner": None}]
    # LLM へメモリーの本文と補足が渡り、翻訳と同じ LLM のインスタンスが使われる
    llm_name, _system, prompt = _FakeModel.calls[0]
    assert llm_name == "llm-inst"
    assert "合計は税込で確認する" in prompt and "書式は A4 縦" in prompt and "月次用" in prompt
    entry = json.loads(env["audit"].read_text(encoding="utf-8").splitlines()[-1])
    assert entry["action"] == "memory_draft_skill" and entry["draft"] == "bob/monthly-report-check"
    # alice の個人用メモリー（user 以外の種類は既定で shared）は含まない
    assert data["private_memories_of_others"] == []


def test_default_owner_is_anonymous_and_given_name_wins(env):
    mem_store.create_memory(env["memory_dir"], "p", "d", "user", "c", owner="alice", visibility="private")
    res = env["client"].post(
        "/api/instances/default/memories/draft-skill",
        json={"items": [{"name": "p", "owner": "alice"}], "skill_name": "my-skill"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert (data["owner"], data["skill_name"]) == ("anonymous", "my-skill")
    assert _parse_frontmatter((env["draft_dir"] / "anonymous" / "my-skill" / "SKILL.md").read_text(encoding="utf-8"))["name"] == "my-skill"
    assert data["private_memories_of_others"] == ["alice/p"]
    assert "use exactly 'my-skill'" in _FakeModel.calls[0][1]


def test_rejects_bad_requests_before_calling_llm(env):
    client = env["client"]
    mem_store.create_memory(env["memory_dir"], "m", "d", "feedback", "c")
    url = "/api/instances/default/memories/draft-skill"
    assert client.post(url, json={"items": []}).status_code == 400
    assert client.post(url, json={"items": [{"name": "nope"}]}).status_code == 404
    assert client.post(url, json={"items": [{"name": "m"}], "skill_name": "Bad Name"}).status_code == 400
    assert client.post(url, json={"items": [{"name": "m"}], "skill_name": "official-one"}).status_code == 409
    (env["draft_dir"] / "anonymous" / "taken").mkdir(parents=True)
    assert client.post(url, json={"items": [{"name": "m"}], "skill_name": "taken"}).status_code == 409
    many = [{"name": f"x{i}"} for i in range(memories.DRAFT_SKILL_MAX_MEMORIES + 1)]
    assert client.post(url, json={"items": many}).status_code == 400
    assert _FakeModel.calls == []


def test_llm_chosen_name_is_normalized_and_made_unique(env):
    # 名前を LLM に任せたときは、形式の崩れ・正式スキルや既存ドラフトとの重複で生成結果を捨てない
    mem_store.create_memory(env["memory_dir"], "m", "d", "feedback", "c")
    _FakeModel.reply_name = "official-one"
    res = env["client"].post("/api/instances/default/memories/draft-skill", json={"items": [{"name": "m"}]})
    assert res.status_code == 200
    assert not (env["draft_dir"] / "anonymous" / "official-one").exists()
    made = env["draft_dir"] / "anonymous" / "official-one-2" / "SKILL.md"
    assert made.is_file() and "name: official-one-2" in made.read_text(encoding="utf-8")
    _FakeModel.reply_name = "Not Valid"
    res = env["client"].post("/api/instances/default/memories/draft-skill", json={"items": [{"name": "m"}]})
    assert res.status_code == 200
    assert (env["draft_dir"] / "anonymous" / "not-valid" / "SKILL.md").is_file()


def test_description_with_triple_dash_is_kept_whole():
    text = "---\nname: monthly-check\ndescription: Monthly check --- use when closing the month\n---\n\n# Body\n"
    name, description, skill_md = memories._build_skill_md(text, None)
    assert description == "Monthly check --- use when closing the month"
    assert skill_md.split("\n---\n", 1)[1].strip() == "# Body"
