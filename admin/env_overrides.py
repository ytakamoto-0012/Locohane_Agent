"""環境変数で上書き中の config.ini キーを検出する（src/config.py のソースを静的解析）。

管理ツールで値を変更しても、対応する環境変数が設定されている限りその変更は
反映されない（優先度: 環境変数 > config_overrides.json > config.ini。
src/config.py の load_config() docstring 参照）。UIでバッジ表示するための
「(section, key) -> 環境変数名」対応表をここで作る。

ベストエフォートの静的解析であり、取りこぼしても実害は「バッジが出ない」
だけ（実際の優先度自体は src/config.py の実装がそのまま担保するため安全）。
新しい設定キーを追加する際、load_config() が
``os.getenv("ENV_NAME", section_var.get("key_name", default))`` という
定型のネスト呼び出し（[admin]セクション等、既存キー全て同じ形）を保つ限り、
このモジュールを変更する必要はない。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# セクション変数の宣言: `llm = parser["llm"] if parser.has_section("llm") else {}` の
# 左辺（変数名）と右辺の文字列リテラル（セクション名）を対応付ける。
_SECTION_VAR_RE = re.compile(r'^\s*(\w+)\s*=\s*parser\["([\w.]+)"\]', re.MULTILINE)
# `os.getenv("ENV_NAME", some_section_var.get("key_name", default))` の形。
# 呼び出しが複数行にまたがっていても \s* が改行にマッチするため検出できる。
_ENV_GETENV_RE = re.compile(r'os\.getenv\(\s*"(\w+)"\s*,\s*(\w+)\.get\(\s*"([\w.]+)"')


def build_mapping(config_py_source: str) -> dict[tuple[str, str], str]:
    """src/config.py のソーステキストから (section, key) -> 環境変数名 の対応表を作る。"""
    var_to_section = dict(_SECTION_VAR_RE.findall(config_py_source))
    mapping: dict[tuple[str, str], str] = {}
    for env_name, var_name, key_name in _ENV_GETENV_RE.findall(config_py_source):
        section = var_to_section.get(var_name)
        if section is None:
            continue
        mapping[(section, key_name)] = env_name
    return mapping


def build_mapping_from_file(config_py_path: Path) -> dict[tuple[str, str], str]:
    """src/config.py ファイルから対応表を作る。"""
    return build_mapping(config_py_path.read_text(encoding="utf-8"))


def active_overrides(
    mapping: dict[tuple[str, str], str], env: dict[str, str] | None = None
) -> dict[tuple[str, str], str]:
    """実際に値が設定されている環境変数に対応する (section, key) だけを返す。

    Args:
        mapping: build_mapping() の結果。
        env: 検査対象の環境（省略時は os.environ）。テスト用に差し替え可能。
    """
    source_env = env if env is not None else os.environ
    return {section_key: env_name for section_key, env_name in mapping.items() if source_env.get(env_name)}
