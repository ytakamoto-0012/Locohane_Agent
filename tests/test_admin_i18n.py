"""設定ダッシュボードの表示言語（admin/i18n.py・admin/static/i18n*.js）のテスト。"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from admin import i18n as admin_i18n
from admin.i18n import t as _t
from src.config import PROJECT_ROOT

STATIC_DIR = PROJECT_ROOT / "admin" / "static"
_JP = re.compile(r"[぀-ヿ一-鿿]")


def _js_unescape(body: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)


def _english_keys() -> set[str]:
    """i18n_en.js の I18N_EN のキー（1行1エントリ・JSON 文字列のキーで生成している）。"""
    keys = set()
    for line in (STATIC_DIR / "i18n_en.js").read_text(encoding="utf-8").splitlines():
        m = re.match(r'\s*("(?:\\.|[^"\\])*"):', line)
        if m:
            keys.add(json.loads(m.group(1)))
    return keys


def _t_call_keys(source: str) -> set[str]:
    """_t("...") / _t(`...`) の第1引数（リテラルのもの）を集める。"""
    pattern = re.compile(r"_t\((\"|'|`)((?:\\.|(?!\1).)*?)\1", re.S)
    return {_js_unescape(m.group(2)) for m in pattern.finditer(source)}


def test_every_translated_js_string_has_english() -> None:
    english = _english_keys()
    missing = {}
    for name in ("admin.js", "skill_lab.js", "knowledge.js", "assistant.js", "data_backup.js", "admin_settings.js"):
        keys = _t_call_keys((STATIC_DIR / name).read_text(encoding="utf-8"))
        lacking = sorted(k for k in keys if k not in english)
        if lacking:
            missing[name] = lacking
    assert missing == {}


def test_every_japanese_template_text_has_english() -> None:
    """index.html のテンプレートの文言は clone() が applyI18n() で訳すため、英訳が要る。"""
    english = _english_keys()
    texts: list[str] = []

    class _Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.in_script = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.in_script += 1
            texts.extend(v for k, v in attrs if k in ("title", "placeholder", "aria-label") and v and _JP.search(v))

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.in_script -= 1

        def handle_data(self, data):
            if not self.in_script and _JP.search(data.strip()):
                texts.append(data.strip())

    _Collector().feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    assert sorted({t for t in texts if t not in english}) == []


def test_english_placeholders_match_japanese() -> None:
    for line in (STATIC_DIR / "i18n_en.js").read_text(encoding="utf-8").splitlines():
        m = re.match(r'\s*("(?:\\.|[^"\\])*"): ("(?:\\.|[^"\\])*"),$', line)
        if m:
            ja, en = json.loads(m.group(1)), json.loads(m.group(2))
            assert sorted(re.findall(r"\{\d+\}", ja)) == sorted(re.findall(r"\{\d+\}", en)), ja


def test_server_messages_have_matching_placeholders() -> None:
    pattern = r"\{\d+(?:![rsa])?(?::[^}]*)?\}"
    for ja, en in admin_i18n.EN.items():
        assert sorted(re.findall(pattern, ja)) == sorted(re.findall(pattern, en)), ja


def test_t_defaults_to_japanese_outside_requests() -> None:
    assert _t("インスタンス {0!r} が見つかりません。", "x") == "インスタンス 'x' が見つかりません。"


def test_t_translates_for_english_requests() -> None:
    token = admin_i18n.set_request_lang("en-US")
    try:
        assert _t("インスタンス {0!r} が見つかりません。", "x") == "Instance 'x' not found."
        # 訳の無い文言は原文のまま
        assert _t("未登録の文言") == "未登録の文言"
        assert "English" in admin_i18n.response_language_instruction()
    finally:
        admin_i18n.reset_request_lang(token)
    assert "answer in Japanese" in admin_i18n.response_language_instruction()


def test_middleware_sets_language_from_header() -> None:
    app = FastAPI()
    app.add_middleware(admin_i18n.LanguageMiddleware)

    @app.get("/boom")
    async def boom():
        raise HTTPException(status_code=404, detail=_t("スレッドが見つかりません。"))

    client = TestClient(app)
    assert client.get("/boom").json()["detail"] == "スレッドが見つかりません。"
    assert client.get("/boom", headers={"X-Locohane-Lang": "en"}).json()["detail"] == "Thread not found."


def test_ui_config_script_exposes_default_language() -> None:
    from admin import server

    res = TestClient(server.app).get("/ui-config.js")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/javascript")
    assert res.text == f"window.LOCOHANE_DEFAULT_LANGUAGE = {json.dumps(server._cfg.ui_default_language)};\n"
