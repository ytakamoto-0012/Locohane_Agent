"""admin/api_docs.py（APIリファレンス md のHTML化）のテスト。"""

from __future__ import annotations

import pytest

from admin import api_docs


def test_bundled_reference_renders():
    result = api_docs.render()
    assert result["markdown"].startswith("# ")
    assert "<h1>" in result["html"]
    # 表（ステータスコード一覧）が table として変換されること
    assert "<table>" in result["html"]


def test_reflects_file_changes_without_cache(tmp_path):
    md = tmp_path / "ref.md"
    md.write_text("# v1", encoding="utf-8")
    assert "v1" in api_docs.render(md)["html"]
    md.write_text("# v2", encoding="utf-8")
    assert "v2" in api_docs.render(md)["html"]


def test_raw_html_is_escaped(tmp_path):
    md = tmp_path / "ref.md"
    md.write_text("<script>alert(1)</script>\n", encoding="utf-8")
    html = api_docs.render(md)["html"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        api_docs.render(tmp_path / "missing.md")


def test_reference_path_follows_display_language():
    """英語表示では英訳を返す（日本語の原本と同じ見出しの数で、日本語を含まない）。"""
    import re

    assert api_docs.reference_path("ja") == api_docs.API_REFERENCE_PATH
    assert api_docs.reference_path("en") == api_docs.API_REFERENCE_EN_PATH
    ja = api_docs.API_REFERENCE_PATH.read_text(encoding="utf-8")
    en = api_docs.API_REFERENCE_EN_PATH.read_text(encoding="utf-8")
    headings = lambda text: re.findall(r"^#{1,3} ", text, re.M)  # noqa: E731
    assert len(headings(en)) == len(headings(ja))
    assert not re.search(r"[぀-ヿ一-鿿]", en)
