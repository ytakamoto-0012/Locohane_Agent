"""_resolve_script_filename の回帰テスト。

run_script/get_tool_source の script_filename 引数は、呼び出し側がファイル名
のみ（scripts/ プレフィックス無し）を渡す前提に変更した。その解決ロジック
（scripts/ 配下の再帰探索・同名ファイルの浅い階層優先・旧形式 "scripts/xxx.py"
入力の吸収）を固定化する。
"""

import pytest

from src import tools


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(tools._state, "_SKILLS_ROOTS", [root])
    return root


@pytest.mark.parametrize("tool_obj", [tools.run_script])
def test_tool_schema_has_no_pydantic_placeholder_fields(tool_obj) -> None:
    """公開ツールの引数名が pydantic の *args/**kwargs 予約名（"args"/"kwargs"）
    と衝突すると、生成されるスキーマのフィールド名が "v__args" 等に化けて
    実行時に `TypeError: ...() got an unexpected keyword argument 'v__args'`
    になる（本番で実際に発生した障害）。script_args という引数名がその
    地雷を踏んでいないことを固定化する。
    """
    schema = tool_obj.args

    assert set(schema.keys()) == {"skill_name", "script_filename", "script_args"}
    assert not any(name.startswith("v__") for name in schema)


def test_resolves_file_directly_under_scripts(skills_root) -> None:
    scripts_dir = skills_root / "demo-skill" / "scripts"
    scripts_dir.mkdir(parents=True)
    target = scripts_dir / "count.py"
    target.write_text("print('hi')", encoding="utf-8")

    resolved = tools._safe_path._resolve_script_filename("demo-skill", "count.py")

    assert resolved == target.resolve()


def test_prefers_shallowest_match_when_duplicated(skills_root) -> None:
    scripts_dir = skills_root / "demo-skill" / "scripts"
    nested_dir = scripts_dir / "sub"
    nested_dir.mkdir(parents=True)
    shallow = scripts_dir / "read_file.py"
    shallow.write_text("shallow", encoding="utf-8")
    deep = nested_dir / "read_file.py"
    deep.write_text("deep", encoding="utf-8")

    resolved = tools._safe_path._resolve_script_filename("demo-skill", "read_file.py")

    assert resolved == shallow.resolve()


def test_legacy_scripts_prefix_is_absorbed_via_basename(skills_root) -> None:
    scripts_dir = skills_root / "demo-skill" / "scripts"
    scripts_dir.mkdir(parents=True)
    target = scripts_dir / "read_file.py"
    target.write_text("print('hi')", encoding="utf-8")

    resolved = tools._safe_path._resolve_script_filename("demo-skill", "scripts/read_file.py")

    assert resolved == target.resolve()


def test_missing_file_raises_value_error(skills_root) -> None:
    scripts_dir = skills_root / "demo-skill" / "scripts"
    scripts_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="not found"):
        tools._safe_path._resolve_script_filename("demo-skill", "nope.py")


def test_missing_scripts_dir_raises_value_error(skills_root) -> None:
    (skills_root / "demo-skill").mkdir()

    with pytest.raises(ValueError, match="scripts"):
        tools._safe_path._resolve_script_filename("demo-skill", "count.py")
