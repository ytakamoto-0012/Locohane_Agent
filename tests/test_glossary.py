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
    assert out.startswith("Saved") and "Other entries for this term were left unchanged" in out and "(user: alice)" in out
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
        SimpleNamespace(source_path=r"D:\docs\a.pdf", locator="p.3", text="今月の受注残は前月より増えた。"),
        SimpleNamespace(source_path=r"D:\docs\b.pdf", locator="", text="関係ない文書"),
    ]
    monkeypatch.setattr(glossary_tools, "_enabled_config", lambda: SimpleNamespace(thread_store_enabled=False))
    monkeypatch.setattr(glossary_tools.vector_search, "search", lambda cfg, q, **kw: SimpleNamespace(hits=hits))
    out = _save(term="受注残", meaning="未出荷の注文の残高。")
    assert "[Knowledge] Passages in the ingested documents that mention this term:" in out
    assert r"- D:\docs\a.pdf p.3: 今月の受注残は前月より増えた。" in out
    assert "b.pdf" not in out


# --- コードレビューで見つかった不具合の回帰テスト ---


def _common_term(root, name, term, meaning):
    entry = glossary.GlossaryEntry(term=term, meaning=meaning, sources=["explained by the admin (2026-11-17)"])
    memory.create_memory(root, name, glossary.render_description(entry), "glossary", glossary.render_content(entry), owner=None)


def test_common_entry_is_not_merged_into_a_users_entry(as_user):
    root, current = as_user
    _common_term(root, "glossary-bom", "BOM", "製品を構成する部品と数量の一覧。")
    _save(term="部品表", meaning="製品を作るのに要る部品の一覧。")
    out = _save(term="部品表", aliases=["BOM"], meaning="製品を作るのに要る部品の一覧。")
    assert "Merged entries" not in out
    assert "glossary-bom (common)" in out
    assert memory.find_memory(root, "glossary-bom", None) is not None
    current["user"] = "bob"
    assert "glossary-bom" in [m.name for m in memory.list_memories(root, "glossary", viewer="bob")]


def test_concurrent_saves_do_not_overwrite_each_other(as_user):
    import threading

    root, _ = as_user
    barrier = threading.Barrier(2)

    def save(term):
        barrier.wait()
        _save(term=term, meaning=f"{term} は社内で使うプログラミング言語の名前。")

    threads = [threading.Thread(target=save, args=(t,)) for t in ("C++", "C#")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(glossary.parse_entry(m).term for m in _glossary(root)) == ["C#", "C++"]


def test_recorded_fingerprint_of_a_changed_file_is_kept(as_user, tmp_path, monkeypatch):
    root, _ = as_user
    monkeypatch.setattr(glossary_tools, "_resolve_file_tools_path", lambda raw: (Path(raw), None))
    a = tmp_path / "terms.csv"
    b = tmp_path / "b.csv"
    a.write_text("ABC,受発注システム\n", encoding="utf-8")
    b.write_text("ABC,受発注を管理するシステム\n", encoding="utf-8")
    _save(term="ABC", meaning="受発注を管理する社内システム。", sources=[f"{a} (row 1)"])
    a.write_text("ABC,別の意味に変わった\n", encoding="utf-8")
    _save(term="ABC", meaning="受発注を管理する社内システム。", sources=[f"{b} (row 1)"])
    [mem] = _glossary(root)
    states = {Path(c.source.path).name: c.state for c in memory.check_sources(mem)}
    assert states == {"terms.csv": "changed", "b.csv": "unchanged"}
    a.unlink()
    _save(term="ABC", meaning="受発注を管理する社内システム。", sources=[f"{b} (row 2)"])
    [mem] = _glossary(root)
    states = {Path(c.source.path).name: c.state for c in memory.check_sources(mem)}
    assert states == {"terms.csv": "missing", "b.csv": "unchanged"}


@pytest.fixture
def glossary_private(as_user, monkeypatch):
    """glossary_visibility = private の設定にする。"""
    root, _ = as_user
    monkeypatch.setattr(memory, "_POLICIES", {})
    memory.set_policy(root, memory.MemoryPolicy(type_visibility={**memory.DEFAULT_TYPE_VISIBILITY, "glossary": "private"}))
    return root


def test_private_term_name_is_not_written_into_entries_visible_to_others(as_user, glossary_private):
    root, _ = as_user
    _common_term(root, "glossary-juchu", "受注残", "未出荷の注文の残高。")
    out = _save(term="極秘案件Z", meaning="来期に始める予定の新規事業の社内コード名。", related_terms=["受注残"])
    assert "Not linked back" in out
    assert "極秘案件Z" not in memory.find_memory(root, "glossary-juchu", None).content


def test_visibility_follows_glossary_visibility_and_is_not_chosen_by_the_llm(as_user):
    # 公開範囲は [memory] glossary_visibility で決まり、LLM は選べない（引数が無い）
    root, _ = as_user
    assert "visibility" not in glossary_tools.save_glossary_term.args
    out = _save(term="パスワード変更", meaning="API Key を再発行して古い鍵を無効にする手順。")
    [mem] = _glossary(root)
    assert mem.visibility == "shared" and "Visibility: shared" in out


def test_updating_a_private_entry_makes_it_follow_the_setting(as_user):
    # 以前 private で保存された用語も、次の更新で設定（既定 shared）どおりになる
    root, _ = as_user
    _save(term="受注残", meaning="未出荷の注文の残高。")
    [mem] = _glossary(root)
    memory.edit_memory(root, mem.name, description=mem.description, memory_type="glossary", content=mem.content, owner="alice", visibility="private")
    assert _glossary(root)[0].visibility == "private"
    _save(term="受注残", meaning="未出荷の注文の残高。")
    assert _glossary(root)[0].visibility == "shared"


def test_entries_are_private_when_glossary_visibility_is_private(as_user, glossary_private):
    root, _ = as_user
    _save(term="BOM", meaning="製品を作るのに要る部品の一覧（部品表）。")
    _save(term="BOM", aliases=["部品表"], meaning="製品を構成する部品と数量の一覧（部品表）。")
    [mem] = _glossary(root)
    assert mem.visibility == "private"


@pytest.mark.parametrize("meaning", ["terms.xlsxを参照", "用語一覧表.xlsxに記載", "terms.csvを見て", "D:\\docs\\用語集.xlsxに記載"])
def test_file_reference_followed_by_japanese_is_rejected(as_user, meaning):
    assert _save(term="受注残", meaning=meaning).startswith("Error: meaning")


def test_definition_before_an_attached_file_name_is_accepted(as_user):
    out = _save(term="受注残", meaning="受注済み未出荷注文の残高を示す用語でterms.xlsx にある")
    assert out.startswith("Saved")


def test_alias_with_a_comma_stays_one_alias(as_user):
    root, _ = as_user
    _save(term="SG&A", meaning="販売費及び一般管理費。", aliases=["Selling, General and Administrative"])
    [mem] = _glossary(root)
    assert glossary.parse_entry(mem).aliases == ["Selling, General and Administrative"]
    out = _save(term="Selling", meaning="販売すること（営業活動）。")
    assert out.startswith("Saved")
    assert len(_glossary(root)) == 2


def test_legacy_comma_separated_aliases_are_still_read(tmp_path):
    memory.create_memory(
        tmp_path, "glossary-x", "X: 意味", "glossary",
        "**Term:** X\n**Aliases:** エックス, ex\n**Meaning:** 社内の略語。\n**Sources:**\n- s", owner="alice",
    )
    assert glossary.parse_entry(memory.read_memory(tmp_path, "glossary-x", "alice")).aliases == ["エックス", "ex"]


@pytest.mark.parametrize(
    ("rel", "suffix"),
    [
        (r"Program Files (x86)\a.xlsx", " (row 2)"),
        (r"docs\terms (2024).xlsx", " (Sheet1)"),
        (r"docs\terms.xlsx", " p.3"),
        (r"docs\terms.xlsx", ", Sheet1"),
        (r"docs\terms.xlsx", ""),
    ],
)
def test_source_path_with_spaces_and_parentheses_is_resolved(tmp_path, monkeypatch, rel, suffix):
    monkeypatch.setattr(glossary_tools, "_resolve_file_tools_path", lambda raw: (Path(raw), None))
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x", encoding="utf-8")
    texts, files = glossary_tools._resolve_sources([f"{f}{suffix}"])
    assert files == [f]
    assert texts == [f"{f}{suffix}"]


def test_repeated_saves_stay_within_the_size_limit(as_user, tmp_path, monkeypatch):
    root, _ = as_user
    monkeypatch.setattr(glossary_tools, "_resolve_file_tools_path", lambda raw: (Path(raw), None))
    f = tmp_path / "terms.csv"
    f.write_text("x", encoding="utf-8")
    for i in range(60):
        out = _save(term="受注残", meaning="未出荷の注文の残高。" + "詳" * 40, sources=[f"{f} (row {i}, {'補足' * 60})"])
        assert not out.startswith("Error"), out
    [mem] = _glossary(root)
    assert len(mem.content) <= memory.SOURCED_CONTENT_MAX_CHARS
    assert len(glossary.parse_entry(mem).sources) <= 10


def test_glossary_does_not_push_other_memories_out_of_the_index(tmp_path):
    for i in range(300):
        memory.create_memory(tmp_path, f"glossary-t{i:03d}", f"t{i}: 意味", "glossary", "c", owner="alice")
    memory.create_memory(tmp_path, "zz-project", "進行中の案件", "project", "c", owner="alice")
    memory.create_memory(tmp_path, "common-ref", "共通の資料の場所", "reference", "c")
    block = memory.render_memory_block(tmp_path, viewer="alice")
    assert "zz-project" in block and "common-ref" in block
    assert sum(1 for line in block.splitlines() if line.startswith("- [glossary]")) == 198
    assert "102 more glossary terms not shown" in block


def test_translations_are_written_per_language(as_user):
    # 他の言語の訳語は、言語ごとに見出し付きの1行にする（以前は別名に混ざり、何語か分からない単語の羅列になった）
    root, _ = as_user
    out = _save(
        term="受注残",
        meaning="受注したがまだ出荷していない注文の残高。",
        aliases=["受注残高", "order backlog"],
        translations={"英語": ["order backlog", "backlog"], "de": "Auftragsbestand", "中国語": ["订单积压"]},
    )
    assert "Translations: English: order backlog / backlog; German: Auftragsbestand; Chinese: 订单积压" in out
    [mem] = _glossary(root)
    assert (
        "**Aliases:**\n- 受注残高\n**Translations:**\n- English: order backlog / backlog\n- German: Auftragsbestand\n"
        "- Chinese: 订单积压\n**Meaning:**"
    ) in mem.content
    # 登録し直すと言語ごとに足し、訳語でも同じ用語として見つかる（重複を作らない）
    _save(term="Auftragsbestand", meaning="受注したがまだ出荷していない注文の残高。", translations={"German": ["Auftragsrückstand"], "French": "carnet de commandes"})
    [mem] = _glossary(root)
    entry = glossary.parse_entry(mem)
    assert entry.translations == {
        "English": ["order backlog", "backlog"],
        "German": ["Auftragsbestand", "Auftragsrückstand"],
        "Chinese": ["订单积压"],
        "French": ["carnet de commandes"],
    }
    assert entry.term == "受注残"


def test_translations_parse_bracket_and_plain_lines():
    entry = glossary.clean_translations(["【英語】order backlog", "German: Auftragsbestand / Rückstand", "[zh] 订单积压"])
    assert entry == {"English": ["order backlog"], "German": ["Auftragsbestand", "Rückstand"], "Chinese": ["订单积压"]}
    mem = memory.Memory(
        name="g", description="d", memory_type="glossary", path=Path("g.md"),
        content="**Term:** X\n**Translations:**\n- English: a / b\n- loose word\n**Meaning:** m",
    )
    assert glossary.parse_entry(mem).translations == {"English": ["a", "b"], "Other": ["loose word"]}
