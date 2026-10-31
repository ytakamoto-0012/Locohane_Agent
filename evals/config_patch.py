"""評価と昇格で同じ設定を足すための「設定パッチ」（スキル研究室の config_patch.json）。

スキル研究室の研究テーマは、開発中のサブエージェントが run_script で呼べるスキルの
ホワイトリスト等、config.ini のリスト型の設定へ項目を足して使うことがある。
評価（evals/run_case.py --config-patch）で足した設定と、昇格（admin/skill_lab/promotion.py）で
インスタンスの config_overrides.json へ書く設定を揃えるため、どちらもここで
「今の実効値（リストリテラル文字列）に項目を足した新しいリストリテラル文字列」を作る。

評価では作った文字列を対応する環境変数へ入れてから load_config() を呼ぶ
（config.ini と同じ解析処理を通るため、評価した設定と昇格後の設定が同じになる）。

config_patch.json の形式（足す項目だけを書く。config.ini と同じ要素の形）:

    {
      "subagent": {"agent_type_run_script_allowlist": [["my-agent", "excel-read"], ["my-agent", ["pdf-tools", "read_pdf.py"]]]},
      "plan": {"plan_approval_exempt_scripts": [["my-skill", "run.py"]]},
      "main_agent_tool_guard": {"allow_entries": [[["my-skill", "run.py"], -1]]}
    }
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

# 足してよいキー: (セクション, キー) -> 対応する環境変数（config.py の os.getenv 名）。
PATCHABLE_KEYS: dict[tuple[str, str], str] = {
    ("subagent", "agent_type_run_script_allowlist"): "SUBAGENT_AGENT_TYPE_RUN_SCRIPT_ALLOWLIST",
    ("plan", "plan_approval_exempt_scripts"): "PLAN_APPROVAL_EXEMPT_SCRIPTS",
    ("main_agent_tool_guard", "allow_entries"): "MAIN_AGENT_TOOL_GUARD_ALLOW_ENTRIES",
}


class ConfigPatchError(ValueError):
    """config_patch.json の形式が不正。"""


def _freeze(item):
    """重複判定用に、リストを入れ子ごとタプルにする。"""
    if isinstance(item, list):
        return tuple(_freeze(x) for x in item)
    return item


def _identity(section: str, key: str, item) -> object:
    """重複判定の単位。allow_entries は対象（[対象, 上限回数] の対象）だけで判定する。"""
    if (section, key) == ("main_agent_tool_guard", "allow_entries") and isinstance(item, list) and item:
        return _freeze(item[0])
    return _freeze(item)


def load_patch(path: Path) -> dict[tuple[str, str], list]:
    """config_patch.json を読み、{(セクション, キー): [足す項目...]} を返す（無ければ空）。

    Raises:
        ConfigPatchError: JSON として読めない、対象外のキーがある、値がリストでない場合。
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise ConfigPatchError(f"{path.name} を JSON として読めません: {e}") from e
    return validate_patch(data)


def validate_patch(data: object) -> dict[tuple[str, str], list]:
    """config_patch.json の中身を検証し、{(セクション, キー): [項目...]} にする。"""
    if not isinstance(data, dict):
        raise ConfigPatchError("config_patch.json はオブジェクト（{セクション: {キー: [...]}}）にしてください")
    result: dict[tuple[str, str], list] = {}
    for section, keys in data.items():
        if not isinstance(keys, dict):
            raise ConfigPatchError(f"[{section}] の値はオブジェクトにしてください")
        for key, items in keys.items():
            if (section, key) not in PATCHABLE_KEYS:
                allowed = ", ".join(f"[{s}].{k}" for s, k in PATCHABLE_KEYS)
                raise ConfigPatchError(f"[{section}].{key} は足せません（足せるのは {allowed}）")
            if not isinstance(items, list):
                raise ConfigPatchError(f"[{section}].{key} の値はリストにしてください")
            if items:
                _validate_items(section, key, items)
                result[(section, key)] = items
    return result


def _validate_items(section: str, key: str, items: list) -> None:
    """項目の形を、config.ini の値を読むときと同じ解析処理で確かめる。

    形の違う項目を足すと、評価では設定の読み込みで全ケースがエラーになり、
    昇格では config_overrides.json の保存が検証で止まるため、書いた時点で止める。
    """
    from src import config as config_mod

    def _plain(x) -> bool:
        return isinstance(x, (str, int, float, bool)) or x is None or (isinstance(x, list) and all(_plain(i) for i in x))

    for item in items:
        if not _plain(item):
            raise ConfigPatchError(f"[{section}].{key} の項目に使えない値があります（文字列・数値・リストだけ）: {item!r}")
    parser = {
        ("subagent", "agent_type_run_script_allowlist"): config_mod._parse_agent_type_run_script_allowlist,
        ("plan", "plan_approval_exempt_scripts"): config_mod._parse_plan_approval_exempt_scripts,
        ("main_agent_tool_guard", "allow_entries"): config_mod._parse_main_agent_tool_guard_allow_entries,
    }[(section, key)]
    try:
        parser(format_list_literal(items))
    except ValueError as e:
        raise ConfigPatchError(f"[{section}].{key}: {e}") from e


def format_list_literal(items: list) -> str:
    """config.ini と同じ見た目（1要素1行）の Python リストリテラル文字列にする。"""
    if not items:
        return "[]"
    return "[\n" + "".join(f"    {json.dumps(item, ensure_ascii=False)},\n" for item in items) + "]"


def merged_value(section: str, key: str, current_text: str, items: list, default_text: str = "") -> tuple[str | None, list]:
    """今の値（リストリテラル文字列）に items を足した新しい値と、実際に足した項目を返す。

    既にある項目（allow_entries は対象が同じもの）は足さない。足すものが無ければ (None, [])。
    default_text（config.ini の既定値）を渡すと、そこにある項目も足さない。config_overrides.json は
    既定値に足す分だけを持つため（src/config.py の ADDITIVE_OVERRIDE_KEYS）、昇格では current_text に
    上書きの値だけを、default_text に config.ini の値を渡す。
    """
    current = _literal_list(section, key, current_text)
    seen = {_identity(section, key, _as_json_like(i)) for i in [*current, *_literal_list(section, key, default_text)]}
    added = []
    for item in items:
        ident = _identity(section, key, item)
        if ident in seen:
            continue
        seen.add(ident)
        added.append(item)
    if not added:
        return None, []
    return format_list_literal([*(_as_json_like(i) for i in current), *added]), added


def _literal_list(section: str, key: str, text: str) -> list:
    value = ast.literal_eval(text.strip()) if text and text.strip() else []
    if not isinstance(value, list):
        raise ConfigPatchError(f"[{section}].{key} の今の値がリストではありません: {text!r}")
    return value


def _as_json_like(item):
    """ast.literal_eval の結果（タプルを含みうる）を JSON と同じ形（リスト）にそろえる。"""
    if isinstance(item, (list, tuple)):
        return [_as_json_like(x) for x in item]
    return item
