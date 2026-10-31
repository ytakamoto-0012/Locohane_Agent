"""maybe_compact() が計画の現在状態を要約LLMの出力とは独立に機械的に
再注入することの回帰テスト。

背景: create_plan で作られた実行計画（steps）は、要約対象メッセージの
tool_calls 引数として存在するが、_messages_to_text() は AIMessage.content
しか見ないため要約LLMからは元々見えない。従って要約結果に計画の状態が
反映される保証が無く、圧縮を跨ぐと「1ファイルの約束」のような計画の
文脈がサブエージェント・メインエージェント双方から失われうる
（本番インシデントの根本原因の1つ）。この再発防止として、maybe_compact
は cl.user_session の plan を直接読み、要約LLMの出力とは無関係に
summary_message へ機械的に追記する。
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
    """要約LLMの代わり。計画に一切言及しない要約テキストを常に返す。"""

    async def ainvoke(self, messages):
        return AIMessage(content="要約結果: 計画には触れていません")


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
async def test_plan_status_is_injected_independently_of_summary_llm(monkeypatch, tmp_path) -> None:
    plan = [
        {"content": "月間版を作る", "activeForm": "月間版を作成中", "status": "completed"},
        {"content": "週間版を作る", "activeForm": "週間版を作成中", "status": "in_progress"},
    ]
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({"plan": plan}))

    result = await maybe_compact(_messages(), _CapturingModel(), _config(tmp_path))

    assert result is not None
    summary_content = result[0].content
    assert "月間版を作る" in summary_content
    assert "週間版を作成中" in summary_content  # in_progress は activeForm で表示
    assert "Approved plan" in summary_content


@pytest.mark.asyncio
async def test_no_plan_appends_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession({}))

    result = await maybe_compact(_messages(), _CapturingModel(), _config(tmp_path))

    assert result is not None
    summary_content = result[0].content
    assert "実行計画" not in summary_content
