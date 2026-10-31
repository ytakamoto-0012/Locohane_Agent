"""read_skill 結果（スキル本文）のコンテキスト削減からの保護の回帰テスト。

- context_trim: スキル名ごとの最新の read_skill 結果は切り詰めない
  （古い再読込分・エラー応答・他ツールは従来どおり切り詰める）。
- context_compaction: 要約対象に入ったスキル本文は要約させず、要約の後ろへ
  原文のまま再添付する（直近側に同じスキルが残っていれば除く・合計上限を
  超えた古いスキルは名前だけ列挙する）。
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src import tools
from src.context_compaction import _SKILL_REATTACH_SEPARATOR, _render_reattached_skills, maybe_compact
from src.context_trim import trim_old_tool_messages
from src.skills import wrap_skill_content


def _round_trip(name: str, content: str, call_id: str) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}]),
        ToolMessage(content=content, name=name, tool_call_id=call_id),
    ]


def _skill(name: str, body_len: int = 500) -> str:
    return wrap_skill_content(name, name[0] * body_len)


class TestTrimProtection:
    def test_latest_skill_content_is_not_truncated(self) -> None:
        skill = _skill("skill-a")
        messages = [
            *_round_trip("read_skill", skill, "c1"),
            *_round_trip("execute_python_code", "x" * 500, "c2"),
        ]

        result = trim_old_tool_messages(messages, keep_recent_iterations=0, max_chars=50)

        assert result[1].content == skill
        assert len(result[3].content) < 500

    def test_older_reread_and_error_are_truncated(self) -> None:
        old_read = _skill("skill-a")
        new_read = _skill("skill-a")
        error = "エラー: " + "e" * 500
        messages = [
            *_round_trip("read_skill", old_read, "c1"),
            *_round_trip("read_skill", error, "c2"),
            *_round_trip("read_skill", new_read, "c3"),
        ]

        result = trim_old_tool_messages(messages, keep_recent_iterations=0, max_chars=50)

        assert "[truncated" in result[1].content
        assert "[truncated" in result[3].content
        assert result[5].content == new_read

    def test_protection_can_be_disabled(self) -> None:
        messages = _round_trip("read_skill", _skill("skill-a"), "c1")

        result = trim_old_tool_messages(messages, keep_recent_iterations=0, max_chars=50, protect_skill_content=False)

        assert "[truncated" in result[1].content


class TestRenderReattachedSkills:
    def test_skill_also_in_kept_messages_is_skipped(self) -> None:
        old = [*_round_trip("read_skill", _skill("skill-a"), "c1"), *_round_trip("read_skill", _skill("skill-b"), "c2")]
        kept = _round_trip("read_skill", _skill("skill-a"), "c3")

        text = _render_reattached_skills(old, kept, max_chars_per_skill=10_000, total_max_chars=10_000)

        assert 'name="skill-b"' in text
        assert 'name="skill-a"' not in text

    def test_newest_first_and_over_budget_names_only(self) -> None:
        old = [
            *_round_trip("read_skill", _skill("skill-a", 300), "c1"),
            *_round_trip("read_skill", _skill("skill-b", 300), "c2"),
        ]

        text = _render_reattached_skills(old, [], max_chars_per_skill=10_000, total_max_chars=400)

        assert text.startswith('<skill_content name="skill-b">')
        assert 'name="skill-a"' not in text
        assert "omitted" in text and "skill-a" in text

    def test_per_skill_limit_truncates_with_hint(self) -> None:
        old = _round_trip("read_skill", _skill("skill-a", 1000), "c1")

        text = _render_reattached_skills(old, [], max_chars_per_skill=200, total_max_chars=10_000)

        assert "only the first 200 characters" in text
        assert "a" * 1000 not in text

    def test_previous_reattachment_is_carried_over(self) -> None:
        # 2回目の圧縮: 前回の要約メッセージに再添付済みの本文・名前だけの列挙を引き継ぐ
        first = _render_reattached_skills(
            [*_round_trip("read_skill", _skill("skill-a", 300), "c1"), *_round_trip("read_skill", _skill("skill-b", 300), "c2")],
            [],
            max_chars_per_skill=10_000,
            total_max_chars=400,
        )
        summary = HumanMessage(content="[自動要約]\n要約" + _SKILL_REATTACH_SEPARATOR + first)
        old = [summary, *_round_trip("read_skill", _skill("skill-c", 100), "c3")]

        text = _render_reattached_skills(old, [], max_chars_per_skill=10_000, total_max_chars=10_000)

        assert text.startswith('<skill_content name="skill-c">')
        assert _skill("skill-b", 300) in text
        assert text.endswith("omitted (reload with read_skill if needed): skill-a")

    def test_newer_read_overrides_previous_reattachment(self) -> None:
        summary = HumanMessage(content="要約" + _SKILL_REATTACH_SEPARATOR + wrap_skill_content("skill-a", "old"))
        old = [summary, *_round_trip("read_skill", wrap_skill_content("skill-a", "new"), "c1")]

        text = _render_reattached_skills(old, [], max_chars_per_skill=10_000, total_max_chars=10_000)

        assert text == wrap_skill_content("skill-a", "new")

    def test_no_skill_returns_empty(self) -> None:
        old = _round_trip("Read", "content", "c1")

        assert _render_reattached_skills(old, [], max_chars_per_skill=100, total_max_chars=100) == ""


@dataclass
class _FakeConfig:
    context_compaction_keep_recent_iterations: int
    context_compaction_prompt_path: Path
    context_trim_truncated_max_chars: int
    context_compaction_summary_source_max_chars: int
    context_compaction_skill_reattach_max_chars_per_skill: int
    context_compaction_skill_reattach_total_max_chars: int


class _CapturingModel:
    def __init__(self):
        self.prompts: list[str] = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[0].content)
        return AIMessage(content="要約結果")


class _FakeUserSession:
    def get(self, key, default=None):
        return default

    def set(self, key, value):
        pass


@pytest.mark.asyncio
async def test_maybe_compact_reattaches_skill_instead_of_summarizing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    prompt_path = tmp_path / "compaction_prompt.md"
    prompt_path.write_text("以下を要約してください", encoding="utf-8")
    config = _FakeConfig(
        context_compaction_keep_recent_iterations=1,
        context_compaction_prompt_path=prompt_path,
        context_trim_truncated_max_chars=2000,
        context_compaction_summary_source_max_chars=100,
        context_compaction_skill_reattach_max_chars_per_skill=12000,
        context_compaction_skill_reattach_total_max_chars=36000,
    )
    skill = _skill("skill-a", 1000)
    messages = [
        HumanMessage(content="q1"),
        *_round_trip("read_skill", skill, "c1"),
        AIMessage(content="ok1"),
        HumanMessage(content="q2"),
        AIMessage(content="ok2"),
    ]
    model = _CapturingModel()

    result = await maybe_compact(messages, model, config)

    assert result is not None
    # 要約LLMへは切り詰めた分しか渡さず、要約メッセージには原文を再添付する
    assert "a" * 1000 not in model.prompts[0]
    assert skill in result[0].content

    # 2回目の圧縮: 前回の再添付は要約LLMへ渡さず、再度そのまま再添付する
    second = [
        *result,
        HumanMessage(content="q3"),
        AIMessage(content="ok3"),
    ]
    result2 = await maybe_compact(second, model, config)

    assert result2 is not None
    assert "a" * 1000 not in model.prompts[1]
    assert skill in result2[0].content
