"""dispatch_agent_batch を停止したとき、次のターンの案内に中断時点の thread note を載せる回帰テスト。

中断時はツールの戻り値がLLMへ届かず、完了済みグループの結果が残らなかった。
退避ファイルは実行中だったグループの分しか無いため、全件をやり直しがちだった。
"""

import app
from src.tools import thread_notes
from src.tools.dispatch_agent_batch import interrupted_note_topic


def _use_notes_file(monkeypatch, tmp_path, text: str | None):
    path = tmp_path / "thread_notes.md"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(thread_notes, "_thread_notes_path", lambda: path)


def test_batch_hint_mentions_interrupted_note_when_saved(monkeypatch, tmp_path) -> None:
    tc = {"id": "call_abc", "name": "dispatch_agent_batch"}
    topic = interrupted_note_topic(tc["id"])
    _use_notes_file(monkeypatch, tmp_path, f"## {topic}\n<!-- 2026-10-03 00:00:00 by main -->\n内容\n\n")
    monkeypatch.setattr(app, "_dispatch_agent_rescue_note_paths", lambda tc: [])

    hint = app._dispatch_agent_rescue_note_hint(tc)

    assert f'thread note "{topic}"' in hint
    assert "do not redo completed groups" in hint


def test_batch_hint_omits_note_when_not_saved(monkeypatch, tmp_path) -> None:
    _use_notes_file(monkeypatch, tmp_path, None)
    monkeypatch.setattr(app, "_dispatch_agent_rescue_note_paths", lambda tc: [])

    hint = app._dispatch_agent_rescue_note_hint({"id": "call_abc", "name": "dispatch_agent_batch"})

    assert "thread note" not in hint


def test_single_dispatch_agent_hint_never_mentions_batch_note(monkeypatch, tmp_path) -> None:
    topic = interrupted_note_topic("call_abc")
    _use_notes_file(monkeypatch, tmp_path, f"## {topic}\n内容\n\n")
    monkeypatch.setattr(app, "_dispatch_agent_rescue_note_paths", lambda tc: [])

    hint = app._dispatch_agent_rescue_note_hint({"id": "call_abc", "name": "dispatch_agent"})

    assert topic not in hint
