"""maybe_compact() が thread note の現在の状態を要約LLMの出力とは独立に
機械的に再注入することの回帰テスト。

背景: write_thread_note のToolMessage（「書き込みました: topic="X"...」）は
_messages_to_text() には含まれるものの、それを要約に残すかどうかは要約LLM
（低パラメータモデル）の判断任せだった。要約に残らなければ、圧縮後は
thread noteの存在自体をモデルが知る手段が無くなる（[[locohane_...]] 系の
plan_status再注入と同根の問題）。この再発防止として、maybe_compact は
thread note ファイルを直接読み、要約LLMの出力とは無関係に summary_message
へ機械的に追記する。
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src import tools
from src.context_compaction import maybe_compact


@dataclass
class _FakeConfig:
    context_compaction_keep_recent_iterations: int
    context_compaction_prompt_path: Path
    context_trim_truncated_max_chars: int
    context_compaction_summary_source_max_chars: int
    context_compaction_skill_reattach_max_chars_per_skill: int
    context_compaction_skill_reattach_total_max_chars: int


class _CapturingModel:
    """要約LLMの代わり。thread noteに一切言及しない要約テキストを常に返す。"""

    async def ainvoke(self, messages):
        return AIMessage(content="要約結果: 特筆すべき点はありません")


class _FakeUserSession:
    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def _messages() -> list:
    return [
        HumanMessage(content="q1"),
        AIMessage(content="ok1"),
        HumanMessage(content="q2"),
        AIMessage(content="ok2"),
    ]


def _config(tmp_path: Path) -> _FakeConfig:
    prompt_path = tmp_path / "compaction_prompt.md"
    prompt_path.write_text("以下を要約してください", encoding="utf-8")
    return _FakeConfig(
        context_compaction_keep_recent_iterations=1,
        context_compaction_prompt_path=prompt_path,
        context_trim_truncated_max_chars=2000,
        context_compaction_summary_source_max_chars=2000,
        context_compaction_skill_reattach_max_chars_per_skill=12000,
        context_compaction_skill_reattach_total_max_chars=36000,
    )


@pytest.mark.asyncio
async def test_thread_note_status_is_injected_independently_of_summary_llm(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"thread_id": "t1"}))
    tools.write_thread_note.invoke({"topic": "調査結果", "content": "件数は123件だった"})

    result = await maybe_compact(_messages(), _CapturingModel(), _config(tmp_path))

    assert result is not None
    summary_content = result[0].content
    assert "調査結果" in summary_content
    assert "thread note" in summary_content
    assert "read_thread_note" in summary_content


@pytest.mark.asyncio
async def test_no_thread_note_appends_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", tmp_path)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"thread_id": "t1"}))

    result = await maybe_compact(_messages(), _CapturingModel(), _config(tmp_path))

    assert result is not None
    summary_content = result[0].content
    assert "thread note" not in summary_content
