"""管理アシスタントの検索（admin/assistant_index.py）のテスト。"""

from __future__ import annotations

import dataclasses
import json
import os

from admin import assistant_index as ai
from admin.ini_catalog import parse
from src.config import PROJECT_ROOT, load_config

_INI = """\
[thread_store]
# 会話スレッド一覧（画面左サイドバー）の有効/無効。
enabled = true
# 保持日数。0以下で無期限。
retention_days = 0

[vector_db]
# 1ファイルあたりの最大サイズ（MB）。これより大きいファイルは取り込まない。
max_file_mb = 50
# 1コレクションに取り込むファイル数の上限。
max_files_per_collection = 100000
"""

_README = """\
# 詳細

## ナレッジ

社内文書の取り込み方法。

```bash
# これはコードブロック内のコメントで、見出しではない
curl ...
```

## ライセンスと商用利用

### 依存パッケージ

ライセンスの説明。

## 設定リファレンス（config.ini）

| セクション | キー | 説明 | 環境変数 |
|---|---|---|---|
| `[vector_db]` | `max_file_mb` | 取り込むファイルの容量の上限 | `VECTOR_DB_MAX_FILE_MB` |
| `[admin]` | `ssl_certfile` / `ssl_keyfile` | HTTPS 用の証明書 | `SSL_CERTFILE` |
"""


def _write_sources(tmp_path, ini=_INI, readme=_README, api="# API\n\n## ログイン\n\nログインする。\n"):
    (tmp_path / "config.ini").write_text(ini, encoding="utf-8")
    (tmp_path / "README_DETAIL.md").write_text(readme, encoding="utf-8")
    (tmp_path / "API_REFERENCE.md").write_text(api, encoding="utf-8")


def _index(tmp_path, cache_dir=None):
    _write_sources(tmp_path)
    return ai.AssistantIndex(
        config_ini_path=tmp_path / "config.ini",
        readme_path=tmp_path / "README_DETAIL.md",
        api_reference_path=tmp_path / "API_REFERENCE.md",
        cache_dir=cache_dir,
    )


# ---------------------------------------------------------------------------
# 文書の分割・索引の中身
# ---------------------------------------------------------------------------


def test_split_markdown_ignores_headings_in_code_and_excluded_sections():
    passages = ai.split_markdown(_README, "README_DETAIL.md", excluded_headings=ai.README_EXCLUDED_HEADINGS)
    titles = [p.title for p in passages]
    assert titles == ["ナレッジ"]
    assert "コードブロック内のコメント" in passages[0].text


def test_split_markdown_splits_long_sections_on_blank_lines():
    para = "あ" * 700
    passages = ai.split_markdown(f"# T\n\n## 長い節\n\n{para}\n\n{para}\n\n{para}\n", "x.md")
    assert len(passages) == 3
    assert all(len(p.text) <= ai.MAX_CHUNK_CHARS for p in passages)
    assert {p.title for p in passages} == {"長い節"}


def test_reference_rows_reads_single_and_multiple_keys():
    rows = ai.reference_rows(_README)
    assert rows[("vector_db", "max_file_mb")] == "取り込むファイルの容量の上限"
    assert rows[("admin", "ssl_certfile")] == rows[("admin", "ssl_keyfile")] == "HTTPS 用の証明書"


def test_config_passages_add_section_intro_and_reference_row():
    passages = ai.config_passages(parse(_INI).keys, ai.reference_rows(_README))
    by_key = {p.key: p for p in passages}
    # セクション冒頭の説明（最初のキーの説明の1行目）が、ほかのキーにも付く。
    assert "会話スレッド一覧" in by_key[("thread_store", "retention_days")].text
    assert "取り込むファイルの容量の上限" in by_key[("vector_db", "max_file_mb")].text
    assert by_key[("vector_db", "max_file_mb")].title == "[vector_db] max_file_mb"


# ---------------------------------------------------------------------------
# 検索
# ---------------------------------------------------------------------------


def test_bigram_search_finds_keys_and_docs(tmp_path):
    index = _index(tmp_path)
    result = index.search("スレッドの保持日数を変えたい", max_keys=1, max_docs=1)
    assert result.mode == "bigram"
    assert result.keys == [("thread_store", "retention_days")]
    result = index.search("社内文書の取り込み", max_keys=0, max_docs=1)
    assert [d.title for d in result.docs] == ["ナレッジ"]


def test_search_respects_allowed_keys(tmp_path):
    index = _index(tmp_path)
    result = index.search("ファイル", max_keys=5, max_docs=0, allowed_keys={("vector_db", "max_files_per_collection")})
    assert result.keys == [("vector_db", "max_files_per_collection")]


def _fake_provider(calls, *, fail=False, cache_id="fake"):
    def embed(texts, kind):
        calls.append((kind, list(texts)))
        if fail:
            raise ConnectionError("embedding server down")
        # 「容量」「サイズ」「MB」を含む文を同じ向きにする（言い回しの違いを吸収する埋め込みのつもり）。
        return [[1.0, 0.0] if any(w in t for w in ("容量", "サイズ", "MB")) else [0.0, 1.0] for t in texts]

    return ai.EmbeddingProvider(embed=embed, cache_id=cache_id, label="fake")


def test_embedding_search_bridges_wording_and_caches(tmp_path):
    cache = tmp_path / "cache"
    index = _index(tmp_path, cache_dir=cache)
    calls = []
    result = index.search("ファイル容量の上限", max_keys=1, max_docs=0, embedding=_fake_provider(calls))
    assert result.mode == "embedding"
    assert result.keys == [("vector_db", "max_file_mb")]
    n_docs = len(calls[0][1])
    assert [c[0] for c in calls] == ["document", "query"]

    # 同じプロセスでは段落を埋め込み直さない。
    calls.clear()
    index.search("ファイル容量", max_keys=1, max_docs=0, embedding=_fake_provider(calls))
    assert [c[0] for c in calls] == ["query"]

    # 再起動後（新しい索引）もディスクのキャッシュを使う。
    calls.clear()
    fresh = _index(tmp_path, cache_dir=cache)
    fresh.search("ファイル容量", max_keys=1, max_docs=0, embedding=_fake_provider(calls))
    assert [c[0] for c in calls] == ["query"]
    cached = json.loads(next(cache.glob("embeddings_*.json")).read_text(encoding="utf-8"))
    assert len(cached) == n_docs


def test_changed_sources_rebuild_and_embed_only_new_passages(tmp_path):
    cache = tmp_path / "cache"
    index = _index(tmp_path, cache_dir=cache)
    calls = []
    index.search("x", max_keys=1, max_docs=1, embedding=_fake_provider(calls))
    api = tmp_path / "API_REFERENCE.md"
    api.write_text("# API\n\n## ログイン\n\nログインする。\n\n## 追加の節\n\n新しい説明。\n", encoding="utf-8")
    stat = api.stat()
    os.utime(api, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    calls.clear()
    result = index.search("新しい説明", max_keys=0, max_docs=1, embedding=_fake_provider(calls))
    assert calls[0] == ("document", ["追加の節\n新しい説明。"])
    assert result.docs[0].title == "追加の節"


def test_embedding_failure_falls_back_to_bigram(tmp_path):
    index = _index(tmp_path)
    result = index.search("スレッドの保持日数を変えたい", max_keys=1, max_docs=0, embedding=_fake_provider([], fail=True))
    assert result.mode == "bigram"
    assert result.keys == [("thread_store", "retention_days")]


def test_provider_from_config_requires_embedding_url():
    cfg = dataclasses.replace(load_config(), vector_db_embedding_endpoints=())
    assert ai.provider_from_config(cfg, "default") is None


def test_shipped_documents_are_indexed(tmp_path):
    """同梱の config.ini・README_DETAIL.md・API_REFERENCE.md で索引を作れ、2-gram だけでも本命が上位に入る。"""
    index = ai.AssistantIndex(
        config_ini_path=PROJECT_ROOT / "config.ini",
        readme_path=PROJECT_ROOT / "README_DETAIL.md",
        api_reference_path=PROJECT_ROOT / "admin" / "API_REFERENCE.md",
    )
    for question, expected in [
        ("2人同時に使うと待たされる", ("llm", "max_concurrent_requests")),
        ("社内文書を検索させたい", ("vector_db", "enabled")),
        ("チャット画面にログインできない", ("auth", "enabled")),
    ]:
        assert expected in index.search(question, max_keys=12, max_docs=0).keys, question
    docs = index.search("管理ツールのログイン", max_keys=0, max_docs=4).docs
    assert docs and all("ライセンス" not in d.title for d in docs)


def test_shipped_english_documents_are_indexed():
    """英語表示の索引（README_DETAIL.en.md・API_REFERENCE.en.md・キーの説明の英訳）で、英語の相談が当たる。"""
    index = ai.AssistantIndex(
        config_ini_path=PROJECT_ROOT / "config.ini",
        readme_path=PROJECT_ROOT / "README_DETAIL.en.md",
        api_reference_path=PROJECT_ROOT / "admin" / "API_REFERENCE.en.md",
        language="en",
    )
    for question, expected in [
        ("I want it to search internal documents", ("vector_db", "enabled")),
        ("Enable sign-in for the chat screen", ("auth", "enabled")),
        ("How many days are conversation threads kept?", ("thread_store", "retention_days")),
    ]:
        assert expected in index.search(question, max_keys=12, max_docs=0).keys, question
    docs = index.search("sign in to the admin tool", max_keys=0, max_docs=4).docs
    assert docs and all(d.source in ("README_DETAIL.en.md", "API_REFERENCE.en.md") for d in docs)
    assert all("License" not in d.title for d in docs)


def test_languages_keep_separate_embedding_caches(tmp_path):
    """日本語と英語の索引が同じ cache_dir を使っても、保存時に互いの段落の埋め込みを消さない。"""
    _write_sources(tmp_path)
    cache = tmp_path / "cache"
    (tmp_path / "README_DETAIL.en.md").write_text("# Detail\n\n## Knowledge\n\nIngest documents.\n", encoding="utf-8")
    ja = _index(tmp_path, cache_dir=cache)
    en = ai.AssistantIndex(
        config_ini_path=tmp_path / "config.ini",
        readme_path=tmp_path / "README_DETAIL.en.md",
        api_reference_path=tmp_path / "API_REFERENCE.md",
        cache_dir=cache,
        language="en",
    )
    ja.search("x", max_keys=1, max_docs=1, embedding=_fake_provider([]))
    en.search("x", max_keys=1, max_docs=1, embedding=_fake_provider([]))
    files = sorted(p.name for p in cache.glob("embeddings_*.json"))
    assert len(files) == 2 and files[1].endswith("_en.json")
