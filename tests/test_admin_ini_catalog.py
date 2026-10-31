"""admin/ini_catalog.py のテスト。

config.ini を書き換えず読み取り専用で解析することを確認する。全キー検出漏れが
無いことは、実際の config.ini を対象に検証する（代表例だけで済ませない）。
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

from admin.ini_catalog import parse, parse_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_INI_PATH = PROJECT_ROOT / "config.ini"


def test_parse_detects_all_keys_in_real_config_ini():
    """実際の config.ini から、グレップで数えた総キー数と一致する件数を検出する。"""
    text = CONFIG_INI_PATH.read_text(encoding="utf-8")
    expected_count = len(re.findall(r"^[A-Za-z_][A-Za-z0-9_.]*\s*=", text, flags=re.MULTILINE))

    catalog = parse(text)

    assert len(catalog.keys) == expected_count


def test_parse_matches_configparser_for_every_key():
    """複数行値も含め、値をつなげた内容が configparser の解釈と一致する。

    configparser は継続行を strip() して連結するため、行頭インデントの
    有無や行内コメント専用行（インデントされていても "#" 始まりなら
    フルラインコメントとして除去される）の扱いが異なる。ここでは
    「行ごとにstripした中身の並び」を比較することで、コメントの
    扱いの違いを除いた実質的な整合性を検証する。
    """
    parser = configparser.ConfigParser()
    parser.read(CONFIG_INI_PATH, encoding="utf-8")
    catalog = parse_file(CONFIG_INI_PATH)

    for info in catalog.keys:
        cp_value = parser.get(info.section, info.key)
        cp_lines = [line.strip() for line in cp_value.split("\n") if line.strip()]
        our_lines = [line.strip() for line in info.default_value.split("\n") if line.strip() and not line.strip().startswith("#")]
        assert our_lines == cp_lines, f"[{info.section}].{info.key}"


def test_all_162_original_plus_admin_keys_present():
    """既知のキーが存在し続けることを確認する（代表的なキーのスポットチェック）。"""
    catalog = parse_file(CONFIG_INI_PATH)
    key_pairs = {(info.section, info.key) for info in catalog.keys}
    for expected in [
        ("llm", "main_url"),
        ("llm", "main_routing_strategy"),
        ("ui", "max_display_messages"),
        ("admin", "port"),
        ("admin", "host"),
        ("mcp", "settings_path"),
        ("auth", "enabled"),
    ]:
        assert expected in key_pairs, expected


def test_ui_kind_inference():
    catalog = parse_file(CONFIG_INI_PATH)
    by_pair = {(info.section, info.key): info for info in catalog.keys}
    assert by_pair[("auth", "enabled")].ui_kind == "bool"
    assert by_pair[("admin", "port")].ui_kind == "text"
    assert by_pair[("llm", "main_url")].ui_kind == "list"
    assert by_pair[("paths", "bin_path")].ui_kind == "list"
    assert by_pair[("llm", "main_routing_strategy")].ui_kind == "choice"
    # "[" で始まるがリストとして解釈できない文字列は multiline のまま。
    assert by_pair[("subagent", "token_guard_soft_warning_text")].ui_kind == "multiline"


def test_list_ui_kind_matches_literal_eval_for_every_key():
    """"[" で始まる全キーについて、list 判定が ast.literal_eval の結果と一致する（代表例だけで済ませない）。"""
    import ast

    catalog = parse_file(CONFIG_INI_PATH)
    for info in catalog.keys:
        if not info.default_value.strip().startswith("[") or info.ui_kind == "choice":
            continue
        try:
            is_list = isinstance(ast.literal_eval(info.default_value.strip()), list)
        except (ValueError, SyntaxError):
            is_list = False
        assert (info.ui_kind == "list") == is_list, f"[{info.section}].{info.key}"


def _schema_matches(schema, v) -> bool:
    """admin/static/admin.js の schemaMatches() と同じ判定。"""
    kind = schema["type"]
    if kind == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if kind == "list":
        return isinstance(v, list) and all(_schema_matches(schema["item"], x) for x in v)
    if kind == "tuple":
        items = schema["items"]
        return isinstance(v, (list, tuple)) and len(v) == len(items) and all(_schema_matches(it["schema"], x) for it, x in zip(items, v))
    if kind == "dict":
        if not isinstance(v, dict) or set(v) - {f["name"] for f in schema["fields"]}:
            return False
        return all(_schema_matches(f["schema"], v[f["name"]]) if f["name"] in v else f.get("optional", False) for f in schema["fields"])
    if kind == "grouped":
        return isinstance(v, list) and all(
            isinstance(x, (list, tuple)) and len(x) == 2 and isinstance(x[0], str) and _schema_matches(schema["item"], x[1]) for x in v
        )
    if kind == "oneof":
        return any(_schema_matches(variant["schema"], v) for variant in schema["variants"])
    if kind == "choice":
        return isinstance(v, str) and v in schema["choices"]
    return isinstance(v, str)


def test_schema_matches_default_value_for_every_schema_key():
    """スキーマを持つ全キーについて、config.ini の既定値がスキーマに合致する
    （合致しないと管理画面が汎用エディタへフォールバックしてしまう）。"""
    import ast

    catalog = parse_file(CONFIG_INI_PATH)
    schema_keys = [info for info in catalog.keys if info.schema is not None]
    assert {(info.section, info.key) for info in schema_keys} >= {
        ("plan", "plan_approval_exempt_scripts"),
        ("subagent", "agent_type_run_script_allowlist"),
        ("main_agent_tool_guard", "allow_entries"),
        ("default_workdir", "allow_sandbox_dir"),
        ("llm", "main_url"),
    }
    for info in schema_keys:
        assert info.ui_kind == "list", f"[{info.section}].{info.key}"
        assert _schema_matches(info.schema, ast.literal_eval(info.default_value.strip())), f"[{info.section}].{info.key}"


def test_schema_accepts_documented_allow_sandbox_dir_example():
    """既定値が [] の allow_sandbox_dir は、説明コメントの記入例で形式を検証する。"""
    catalog = parse_file(CONFIG_INI_PATH)
    info = next(i for i in catalog.keys if (i.section, i.key) == ("default_workdir", "allow_sandbox_dir"))
    example = [{"dir": "E:/shared_output", "allow_entries": [["excel-edit", "write_excel.py"], "run_script"]}]
    assert _schema_matches(info.schema, example)


def test_tool_choices_cover_default_allow_entries_tool_names():
    """設定画面のツール名の候補（ビルトインツール）に、既定の allow_entries のツール名がすべてある
    （無いと既定値の欄が「一覧にない現在値」になる。ツールの改名で既定値の更新漏れも見つかる）。"""
    import ast

    from admin.skill_lab.sources import main_agent_tool_names

    catalog = parse_file(CONFIG_INI_PATH)
    info = next(i for i in catalog.keys if (i.section, i.key) == ("main_agent_tool_guard", "allow_entries"))
    names = [target for target, _ in ast.literal_eval(info.default_value.strip()) if isinstance(target, str)]
    assert names
    assert set(names) <= set(main_agent_tool_names())


def test_choices_match_src_config_constants():
    """選択肢の集合が src/config.py の検証用定数と一致し、既定値が選択肢に含まれる。"""
    from src.config import (
        LLM_PROVIDERS,
        LLM_REASONING_EFFORTS,
        LLM_REASONING_FORMATS,
        LLM_ROUTING_STRATEGIES,
        MAIN_AGENT_TOOL_GUARD_MODES,
        MAIN_AGENT_TOOL_GUARD_VISIBILITY_MODES,
        UI_DEFAULT_LANGUAGES,
        VECTOR_DB_BACKENDS,
    )

    catalog = parse_file(CONFIG_INI_PATH)
    by_pair = {(info.section, info.key): info for info in catalog.keys}
    expected = {
        ("llm", "main_routing_strategy"): LLM_ROUTING_STRATEGIES,
        ("llm", "sub_routing_strategy"): LLM_ROUTING_STRATEGIES,
        ("llm", "reasoning_format"): LLM_REASONING_FORMATS | {""},
        ("llm", "reasoning_effort"): LLM_REASONING_EFFORTS | {""},
        ("main_agent_tool_guard", "mode"): MAIN_AGENT_TOOL_GUARD_MODES,
        ("main_agent_tool_guard", "visibility_mode"): MAIN_AGENT_TOOL_GUARD_VISIBILITY_MODES,
        ("log", "level"): {"info", "debug", "none"},
        ("ui", "default_language"): set(UI_DEFAULT_LANGUAGES),
        ("vector_db", "backend"): set(VECTOR_DB_BACKENDS),
        ("vector_db", "embedding_routing_strategy"): LLM_ROUTING_STRATEGIES,
    }
    for pair, values in expected.items():
        info = by_pair[pair]
        assert info.ui_kind == "choice", pair
        assert set(info.choices) == set(values), pair
        assert info.default_value.strip().lower() in info.choices, pair
    for key in ("main_url", "sub_url"):
        fields = {f["name"]: f for f in by_pair[("llm", key)].schema["item"]["fields"]}
        assert set(fields["provider"]["schema"]["choices"]) == LLM_PROVIDERS


def test_group_heading_and_description_present_for_known_key():
    catalog = parse_file(CONFIG_INI_PATH)
    by_pair = {(info.section, info.key): info for info in catalog.keys}
    info = by_pair[("llm", "main_url")]
    assert info.group_heading is not None
    assert len(info.description) > 0


def test_blank_line_breaks_comment_association():
    """空行を挟むと、その前のコメントは次のキーの説明として引き継がれない。"""
    text = "[sec]\n# これは無関係のコメント\n\nfoo = bar\n"
    catalog = parse(text)
    info = catalog.keys[0]
    assert info.key == "foo"
    assert info.description == ""


def test_multiline_value_continuation_lines_captured():
    text = "[sec]\nfoo = [\n    1,\n    2,\n    ]\nbar = baz\n"
    catalog = parse(text)
    by_key = {info.key: info for info in catalog.keys}
    assert by_key["foo"].default_value == "[\n    1,\n    2,\n    ]"
    assert by_key["bar"].default_value == "baz"


def test_parse_file_sets_mtime():
    catalog = parse_file(CONFIG_INI_PATH)
    assert catalog.mtime > 0
