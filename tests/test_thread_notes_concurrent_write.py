"""同じ応答で並列に呼ばれた write_thread_note の追記が消えないことのテスト。

並列のツール呼び出しは別スレッドで同時に追記するため、Windows では追記どうしが
同じ位置に書かれて片方が消えることがあった（evals の scale ケースで、同時に書いた
2トピックのうち1つが一覧に出ず、サブエージェントが書き直していた）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from src.tools import thread_notes


def test_parallel_writes_are_all_kept(tmp_path, monkeypatch) -> None:
    path = tmp_path / "_thread_notes.md"
    monkeypatch.setattr(thread_notes, "_thread_notes_path", lambda: path)
    topics = [f"topic_{i}" for i in range(40)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda t: thread_notes.write_thread_note.invoke({"topic": t, "content": "x" * 2000}), topics))
    assert all(r.startswith("Written:") for r in results)
    blocks = thread_notes._parse_thread_notes(path.read_text(encoding="utf-8"))
    assert sorted(b.topic for b in blocks) == sorted(topics)
    assert all(b.content == "x" * 2000 for b in blocks)
