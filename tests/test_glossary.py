"""用語集の登録（src/glossary.py・src/tools/glossary_tools.py の save_glossary_term）のテスト。

辞書ファイルを「学習して」と頼むと、LLM が create_memory で reference に1件にまとめ、意味を書かずに
ファイルを指すだけの記録をしていた。用語は専用ツールで 1語1ファイル・意味と出典つきで保存させ、
同じ意味の用語は1件にまとめ、関連用語は互いの本文に載せる。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import glossary, memory, skill_drafts
from src.tools import _state, glossary_tools, memory_tools


@pytest.fixture
def as_user(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    monkeypatch.setattr(_state, "_MEMORY_ROOT", root)
    monkeypatch.setattr(_state, "_LLM_CONFIG", None)  # ナレッジは無効（検索しない）
    current = {"user": "alice"}
    monkeypatch.setattr(skill_drafts, "current_draft_user", lambda: current["user"])
    return root, current


def _save(**kwargs):
    kwargs.setdefault("sources", ["explained by the user (2026-11-17)"])
    return glossary_tools.save_glossary_term.invoke(kwargs)


def _glossary(root, owner="alice"):
    return [m for m in memory._iter_memories(root, "glossary", owner)]


def test_japanese_term_gets_ascii_name_and_structured_body(as_user):
    root, _ = as_user
    out = _save(term="受注残", meaning="受注したがまだ出荷していない注文の残高。", aliases=["じゅちゅうざん"], usage="月次の営業会議で使う。")
    assert out.startswith('Saved the glossary term "受注残" as a new entry (glossary-')
    [mem] = _glossary(root)
    assert mem.name.isascii() and mem.name.startswith("glossary-")
    assert mem.description == "受注残 (じゅちゅうざん): 受注したがまだ出荷していない注文の残高。"
    assert "**Meaning:** 受注したがまだ出荷していない注文の残高。" in mem.content
    assert "**Sources:**\n- explained by the user (2026-11-17)" in mem.content
    entry = glossary.parse_entry(mem)
    assert (entry.term, entry.aliases, entry.usage) == ("受注残", ["じゅちゅうざん"], "月次の営業会議で使う。")


def test_ascii_term_name_is_readable(as_user):
    root, _ = as_user
    _save(term="KPI Board", meaning="部署ごとの重要業績評価指標を並べた社内ダッシュボード。")
    assert [m.name for m in _glossary(root)] == ["glossary-kpi-board"]


@pytest.mark.parametrize(
    "meaning",
    ["terms.xlsx を参照", "D:\\docs\\用語集.xlsx に記載", "See terms.csv", "@3 参照", "  "],
)
def test_meaning_that_only_points_to_a_file_is_rejected(as_user, meaning):
    root, _ = as_user
    out = _save(term="受注残", meaning=meaning)
    assert out.startswith("Error: meaning")
    assert _glossary(root) == []


def test_meaning_with_a_definition_and_a_file_name_is_accepted(as_user):
    out = _save(term="受注残", meaning="受注済みで未出荷の注文の金額。集計方法は用語集.xlsx にもある。")
    assert out.startswith("Saved")


def test_sources_are_required(as_user):
    out = glossary_tools.save_glossary_term.invoke({"term": "受注残", "meaning": "未出荷の注文の残高。", "sources": []})
    assert out.startswith("Error: sources is empty")


def test_same_term_or_alias_updates_one_entry(as_user):
    root, _ = as_user
    _save(term="受注残", meaning="未出荷の注文の残高。", sources=["a.xlsx (row 3)"])
    out = _save(term="受注残高", meaning="受注したがまだ出荷していない注文の残高（金額）。", aliases=["受注残"], sources=["b.pdf p.2"])
    assert out.startswith('Updated the existing glossary entry "受注残"')
    assert '"受注残高" was added to its aliases' in out
    assert "Previous meaning" in out
    [mem] = _glossary(root)
    entry = glossary.parse_entry(mem)
    assert entry.aliases == ["受注残高"]
    assert entry.meaning == "受注したがまだ出荷していない注文の残高（金額）。"
    assert entry.sources == ["a.xlsx (row 3)", "b.pdf p.2"]


def test_entries_with_the_same_meaning_are_merged(as_user):
    root, _ = as_user
    _save(term="BOM", meaning="製品を作るのに要る部品の一覧（部品表）。")
    _save(term="部品表", meaning="製品を構成する部品と数量の一覧。")
    assert len(_glossary(root)) == 2
    out = _save(term="BOM", aliases=["部品表", "Bill of Materials"], meaning="製品を構成する部品と数量の一覧（部品表）。")
    assert "Merged entries with the same meaning" in out
    [mem] = _glossary(root)
    entry = glossary.parse_entry(mem)
    assert entry.term == "BOM"
    assert set(entry.aliases) == {"部品表", "Bill of Materials"}


def test_related_terms_are_linked_both_ways(as_user):
    root, _ = as_user
    _save(term="受注残", meaning="未出荷の注文の残高。")
    out = _save(term="出荷", meaning="倉庫から顧客へ製品を送り出すこと。", related_terms=["受注残", "検収"])
    assert "Linked both ways with the related terms: 受注残" in out
    assert "Related terms not registered yet: 検収" in out
    by_term = {glossary.parse_entry(m).term: glossary.parse_entry(m) for m in _glossary(root)}
    assert by_term["出荷"].related == ["受注残", "検収"]
    assert by_term["受注残"].related == ["出荷"]
    # あとから登録した関連用語も、相手に載る
    _save(term="検収", meaning="納品物を受け取って内容を確かめること。")
    _save(term="検収", meaning="納品物を受け取って内容を確かめること。", related_terms=["出荷"])
    by_term = {glossary.parse_entry(m).term: glossary.parse_entry(m) for m in _glossary(root)}
    assert by_term["検収"].related == ["出荷"]
    assert by_term["出荷"].related == ["受注残", "検収"]


def test_legacy_glossary_memory_is_matched_and_restructured(as_user):
    root, _ = as_user
    memory.create_memory(root, "term-abc", "ABC（エービーシー）: 社内の受発注システム", "glossary", "受発注を管理する社内システム。", owner="alice")
    out = _save(term="ABC", meaning="受発注を管理する社内システム（2020年導入）。")
    assert out.startswith('Updated the existing glossary entry "ABC" (term-abc)')
    entry = glossary.parse_entry(memory.read_memory(root, "term-abc", "alice"))
    assert entry.meaning == "受発注を管理する社内システム（2020年導入）。"
    assert "受発注を管理する社内システム。" in entry.notes


def test_other_users_entries_are_not_changed(as_user):
    root, current = as_user
    _save(term="受注残", meaning="未出荷の注文の残高。")
    current["user"] = "bob"
    out = _save(term="受注残", meaning="出荷前の受注の合計金額。")
    assert out.startswith("Saved") and "Other users also have an entry for this term" in out
    assert glossary.parse_entry(_glossary(root, "alice")[0]).meaning == "未出荷の注文の残高。"


def test_source_file_is_fingerprinted(as_user, tmp_path, monkeypatch):
    root, _ = as_user
    # パス解決（@N・作業ディレクトリ）は Chainlit のセッションを使うため、テストでは絶対パスをそのまま返す。
    monkeypatch.setattr(glossary_tools, "_resolve_file_tools_path", lambda raw: (Path(raw), None))
    f = tmp_path / "terms.csv"
    f.write_text("受注残,未出荷の注文の残高\n", encoding="utf-8")
    _save(term="受注残", meaning="未出荷の注文の残高。", sources=[f"{f} (row 1)"])
    [mem] = _glossary(root)
    assert [s.path for s in mem.sources] == [str(f)]
    assert f"- {f} (row 1)" in mem.content


def test_create_and_update_memory_refuse_glossary(as_user):
    root, _ = as_user
    out = memory_tools.create_memory.invoke({"name": "t", "description": "d", "memory_type": "glossary", "content": "c"})
    assert out.startswith("Error: glossary terms are saved only with save_glossary_term")
    _save(term="受注残", meaning="未出荷の注文の残高。")
    name = _glossary(root)[0].name
    out = memory_tools.update_memory.invoke({"name": name, "content": "x"})
    assert out.startswith("Error: glossary terms are saved only with save_glossary_term")


def test_excerpt_around_finds_term_ignoring_width_and_case():
    text = "前置き " * 30 + "今月の ＢＯＭ を更新した。" + " 後ろ" * 30
    excerpt = glossary.excerpt_around(text, {glossary.normalize("bom")})
    assert excerpt is not None and "ＢＯＭ" in excerpt
    assert glossary.excerpt_around("関係ない文", {"bom"}) is None


def test_result_shows_knowledge_passages_that_mention_the_term(as_user, monkeypatch):
    from types import SimpleNamespace

    hits = [
        SimpleNamespace(source_path="D:\docs\a.pdf", locator="p.3", text="今月の受注残は前月より増えた。"),
        SimpleNamespace(source_path="D:\docs\b.pdf", locator="", text="関係ない文書"),
    ]
    monkeypatch.setattr(glossary_tools, "_enabled_config", lambda: SimpleNamespace(thread_store_enabled=False))
    monkeypatch.setattr(glossary_tools.vector_search, "search", lambda cfg, q, **kw: SimpleNamespace(hits=hits))
    out = _save(term="受注残", meaning="未出荷の注文の残高。")
    assert "[Knowledge] Passages in the ingested documents that mention this term:" in out
    assert "- D:\docs\a.pdf p.3: 今月の受注残は前月より増えた。" in out
    assert "b.pdf" not in out
