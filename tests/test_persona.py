"""AI の人物設定（[persona] name / gender / personality）をシステムプロンプトの Identity に差し込むこと。

- 3つとも空（既定）なら何も差し込まず、{{persona}} の行ごと消える（従来どおり Locohane として振る舞う）。
- 設定した項目だけを差し込み、性格は手順より優先しない旨を一緒に書く。
- インスタンス別の config_overrides.json（設定ダッシュボードの「表示設定」タブが保存する）でも変えられる。
"""

from __future__ import annotations

import json
import os
from dataclasses import replace

from admin import ini_catalog
from src.config import PROJECT_ROOT, render_persona_block


def _config(monkeypatch, tmp_path, overrides: dict | None = None):
    from src.config import load_config

    monkeypatch.setattr(os, "environ", dict(os.environ))
    for name in ("PERSONA_NAME", "PERSONA_GENDER", "PERSONA_PERSONALITY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "config_overrides.json"
    if overrides is not None:
        path.write_text(json.dumps(overrides, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CONFIG_OVERRIDES_PATH", str(path))
    return load_config()


def _prompt(cfg) -> str:
    from src.agent_types import scan_agent_types
    from src.skills import scan_skills
    from src.system_prompt_builder import build_prompts, fill_session_parts

    skills = scan_skills([cfg.skills_dir, *cfg.locohane_skills_dirs])
    prompt, _agents = build_prompts(cfg, skills, scan_agent_types([cfg.agents_dir, *cfg.locohane_agents_dirs]))
    return fill_session_parts(prompt, cfg, drafts=[], memory_viewer="anonymous")


def test_default_config_inserts_nothing(monkeypatch, tmp_path):
    cfg = _config(monkeypatch, tmp_path)
    assert (cfg.persona_name, cfg.persona_gender, cfg.persona_personality) == ("", "", "")
    prompt = _prompt(cfg)
    assert "{{persona}}" not in prompt and "## Persona" not in prompt
    # 目印の行ごと消え、Identity と次の見出しの間に余計な空行を残さない
    assert "without skipping anything.\n\n# Basic flow" in prompt


def test_overrides_insert_persona_into_identity(monkeypatch, tmp_path):
    cfg = _config(
        monkeypatch,
        tmp_path,
        {"persona": {"name": "こはね", "gender": "女性", "personality": "明るく丁寧\n結論から短く話す。${not_a_var}"}},
    )
    assert cfg.persona_name == "こはね"
    prompt = _prompt(cfg)
    identity = prompt[: prompt.index("# Basic flow")]
    assert "- Name: **こはね**." in identity
    assert "- Gender: 女性" in identity
    # 自由記述のため ${...} は展開しない（展開の後に差し込む）
    assert "- Personality and speaking style: 明るく丁寧\n  結論から短く話す。${not_a_var}" in identity
    assert "never changes the procedures" in identity


def test_only_set_items_are_rendered():
    from types import SimpleNamespace

    block = render_persona_block(SimpleNamespace(persona_name="", persona_gender="", persona_personality="おっとり"))
    assert "Name:" not in block and "Gender:" not in block
    assert "- Personality and speaking style: おっとり" in block
    # 属性の無い Config 相当（テスト用の簡易設定）でも落ちない
    assert render_persona_block(SimpleNamespace()) == ""


def test_config_dataclass_defaults_are_empty(monkeypatch, tmp_path):
    cfg = _config(monkeypatch, tmp_path)
    assert render_persona_block(replace(cfg, persona_name="  ")) == ""


def test_dashboard_save_path_stores_multiline_personality(monkeypatch, tmp_path):
    """「表示設定」タブの保存（PUT /api/instances/{name}/config と同じ overrides.save）で複数行の性格も保存・反映できる。"""
    from admin import overrides

    ini = PROJECT_ROOT / "config.ini"
    inst_dir = tmp_path / "instances" / "t1"
    path = inst_dir / "config_overrides.json"
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data"))
    overrides.save(
        path=path,
        updates={("persona", "name"): "こはね", ("persona", "personality"): "明るく丁寧\n結論から話す"},
        resets=[],
        base_mtime=None,
        ini_catalog=ini_catalog.parse_file(ini),
        config_ini_path=ini,
        backup_dir=inst_dir / "backups",
        backup_keep=3,
        audit_log_path=tmp_path / "audit.log",
        instance_name="t1",
        actor="tester",
        remote_addr="127.0.0.1",
        instances_root=tmp_path / "instances",
    )
    assert json.loads(path.read_text(encoding="utf-8"))["persona"]["name"] == "こはね"
    cfg = _config(monkeypatch, tmp_path, json.loads(path.read_text(encoding="utf-8")))
    assert (cfg.persona_name, cfg.persona_personality) == ("こはね", "明るく丁寧\n結論から話す")


def test_welcome_template_placeholders():
    from types import SimpleNamespace

    from src.config import fill_welcome_template

    template = "{ai_name}（{ai_gender}）です。{ai_personality}\nスキル: {skills} / {other} {}"
    cfg = SimpleNamespace(persona_name="こはね", persona_gender="女性", persona_personality="明るく丁寧")
    assert fill_welcome_template(template, skills="a、b", config=cfg) == "こはね（女性）です。明るく丁寧\nスキル: a、b / {other} {}"
    # 未設定なら名前は Locohane、性別・性格は空
    empty = SimpleNamespace(persona_name="", persona_gender="", persona_personality="")
    assert fill_welcome_template("{ai_name}[{ai_gender}{ai_personality}]", skills="", config=empty) == "Locohane[]"
    # 差し込んだ値（スキルの一覧・性格の文）に偶然ある差し込み口は、置き換えない（1 回の置き換えで埋める）
    odd = SimpleNamespace(persona_name="こはね", persona_gender="", persona_personality="{skills} と書く")
    assert fill_welcome_template("{skills} / {ai_personality}", skills="x{ai_name}", config=odd) == "x{ai_name} / {skills} と書く"


def test_cad_viewer_timeout_env_vars_win_over_config_ini(monkeypatch, tmp_path):
    """3D ビューアのタイムアウト: 同梱の config.ini に [cad_viewer] timeout_seconds があっても、以前の環境変数
    SHOW_CAD_VIEWER_TIMEOUT_SECONDS が効く（新しい CAD_VIEWER_TIMEOUT_SECONDS が先）。"""
    for name in ("CAD_VIEWER_TIMEOUT_SECONDS", "SHOW_CAD_VIEWER_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    assert _config(monkeypatch, tmp_path).show_cad_viewer_timeout_seconds == 3600
    monkeypatch.setenv("SHOW_CAD_VIEWER_TIMEOUT_SECONDS", "120")
    assert _config(monkeypatch, tmp_path).show_cad_viewer_timeout_seconds == 120
    monkeypatch.setenv("CAD_VIEWER_TIMEOUT_SECONDS", "60")
    assert _config(monkeypatch, tmp_path).show_cad_viewer_timeout_seconds == 60


def test_app_uses_persona_for_welcome_and_chat_screen_name():
    """welcome.md の差し込みと、チャット画面に出す名前（/locohane/ui-config の assistant_name）。"""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    assert "fill_welcome_template(welcome_template, skills=names, config=_config)" in source
    assert '"assistant_name": _config.persona_name' in source
    thread = (PROJECT_ROOT / "frontend/src/components/MessageThread.tsx").read_text(encoding="utf-8")
    assert "useAssistantName()" in thread


def test_catalog_lists_persona_keys_for_the_dashboard():
    catalog = ini_catalog.parse_file(PROJECT_ROOT / "config.ini")
    keys = {k.key: k for k in catalog.keys if k.section == "persona"}
    assert set(keys) == {"name", "gender", "personality"}
    assert keys["personality"].ui_kind == "multiline"
    assert keys["name"].ui_kind == "text" and keys["name"].default_value == ""
