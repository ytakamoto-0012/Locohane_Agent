"""admin/ini_catalog_en.py（config.ini の説明の英訳カタログ）のテスト。

config.ini のキー・見出しを足したのに英訳を忘れると、英語表示の設定画面に日本語の説明が出る。
実際の config.ini の全キーを対象に確かめる。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from admin import i18n as admin_i18n
from admin import ini_catalog_en
from admin.ini_catalog import parse_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_INI_PATH = PROJECT_ROOT / "config.ini"
_JAPANESE = re.compile(r"[぀-ヿ一-鿿]")
# 埋め込みモデルに実際に付ける日本語の接頭辞（訳さずに値として載せている）。
_ALLOWED_JAPANESE = ("検索クエリ: ", "クエリ: ", "検索文書: ", "文章: ")


def test_every_config_key_has_english_description() -> None:
    catalog = parse_file(CONFIG_INI_PATH)
    keys = {f"{info.section}.{info.key}" for info in catalog.keys}
    assert sorted(keys - set(ini_catalog_en.DESCRIPTIONS)) == []
    assert sorted(set(ini_catalog_en.DESCRIPTIONS) - keys) == []


def test_every_group_heading_has_english() -> None:
    catalog = parse_file(CONFIG_INI_PATH)
    headings = {info.group_heading for info in catalog.keys if info.group_heading}
    assert sorted(headings - set(ini_catalog_en.HEADINGS)) == []


def test_english_descriptions_contain_no_japanese() -> None:
    for key, text in ini_catalog_en.DESCRIPTIONS.items():
        for allowed in _ALLOWED_JAPANESE:
            text = text.replace(allowed, "")
        assert not _JAPANESE.search(text), key
    for heading in ini_catalog_en.HEADINGS.values():
        assert not _JAPANESE.search(heading), heading


def test_config_api_returns_english_descriptions_for_english_requests() -> None:
    from admin import server

    token = admin_i18n.set_request_lang("en")
    try:
        view = server._config_key_view("default", "app")
    finally:
        admin_i18n.reset_request_lang(token)
    main_url = next(k for k in view["keys"] if (k["section"], k["key"]) == ("llm", "main_url"))
    assert main_url["description"] == ini_catalog_en.DESCRIPTIONS["llm.main_url"]
    assert main_url["group_heading"] == "Endpoints for the main agent"
    labels = {f["name"]: f["schema"].get("placeholder") for f in main_url["schema"]["item"]["fields"]}
    assert labels["base_url"] == "e.g. http://localhost:8080/v1"

    japanese = server._config_key_view("default", "app")
    main_url_ja = next(k for k in japanese["keys"] if (k["section"], k["key"]) == ("llm", "main_url"))
    assert _JAPANESE.search(main_url_ja["description"])


def test_every_admin_t_call_has_english() -> None:
    """admin/ 配下の _t("...") に渡す日本語の文言には、すべて英訳がある。"""
    missing = []
    for path in (PROJECT_ROOT / "admin").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_t"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and _JAPANESE.search(node.args[0].value)
                and node.args[0].value not in admin_i18n.EN
            ):
                missing.append(f"{path.name}:{node.lineno} {node.args[0].value}")
    assert missing == []
