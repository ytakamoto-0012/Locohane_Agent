"""src/i18n.py（チャット画面の表示言語の切り替え）のテスト。"""

import pytest

from src import i18n
from src.config import _as_ui_language
from src.tool_labels import tool_label


@pytest.fixture(autouse=True)
def _reset_default_language():
    yield
    i18n.set_default_language("ja")


def test_every_message_has_japanese_and_english() -> None:
    missing = [key for key, entry in i18n.MESSAGES.items() if set(entry) != {"ja", "en"} or not all(entry.values())]
    assert missing == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("ja", "ja"), ("EN", "en"), ("en-US", "en"), ("ja_JP", "ja"), ("fr", None), ("", None), (None, None)],
)
def test_normalize(raw, expected) -> None:
    assert i18n.normalize(raw) == expected


def test_current_lang_outside_session_uses_default_language() -> None:
    assert i18n.current_lang() == "ja"
    i18n.set_default_language("en")
    assert i18n.current_lang() == "en"
    # "auto" はブラウザ任せの意味なので、サーバー側だけで決めるときは日本語にする。
    i18n.set_default_language("auto")
    assert i18n.current_lang() == "ja"


def test_t_formats_and_selects_language() -> None:
    assert i18n.t("notice.generated_files", names="a.xlsx") == "生成ファイル: a.xlsx"
    assert i18n.t("notice.generated_files", "en", names="a.xlsx") == "Generated files: a.xlsx"
    assert i18n.t("notice.input_too_long", "en", total=1234, limit=1000).startswith("Your input is too long (1,234 / 1,000")


def test_t_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        i18n.t("no.such.key")


def test_response_language_directive_names_the_display_language() -> None:
    # プロンプト本体は英語のため、日本語のときも応答言語の指示を返す。
    assert "display language is Japanese" in i18n.response_language_directive("ja")
    directive = i18n.response_language_directive("en")
    assert "## Response language" in directive
    assert "English" in directive


def test_tool_label_follows_language() -> None:
    assert tool_label("read_skill") == "スキル読み込み"
    i18n.set_default_language("en")
    assert tool_label("read_skill") == "Read skill"
    # 対応表に無いツールはツール名そのもの。
    assert tool_label("Glob") == "Glob"


@pytest.mark.parametrize(("raw", "expected"), [("", "auto"), (None, "auto"), ("EN", "en"), (" ja ", "ja")])
def test_as_ui_language(raw, expected) -> None:
    assert _as_ui_language(raw) == expected


def test_as_ui_language_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="default_language"):
        _as_ui_language("fr")
