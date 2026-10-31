"""read_skill の出力整形・read_skill_file の `@N` 受付・skill_name の enum 制約の回帰テスト。

- read_skill は frontmatter を除いた本文を `<skill_content name="...">` で囲み、
  references/ のファイル一覧を `@N` 付きで末尾に添える（中身は読まない）。
- read_skill_file は その `@N` をそのまま受け付けるが、skills ルート外を指す
  `@N` は拒否する（サンドボックス境界の維持）。
- apply_skill_name_enum() で LLM へ送る tools スキーマに enum が載り、
  一覧外の名前はツール本体に届く前に検証エラーになる。
"""

import re

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from src import tools
from src.skills import skill_content_name
from src.tools.read_skill import apply_skill_name_enum


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {"thread_id": "thread-1"}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(tools._state, "_SKILLS_ROOTS", [root])
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", tmp_path / "path_memory_data")
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_MAX_ENTRIES", 500)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", None)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    return root


def _make_skill(root, name: str, references: dict[str, str] | None = None):
    skill_dir = root / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: demo description\n---\n\n# 手順\n本文です", encoding="utf-8"
    )
    for rel, content in (references or {}).items():
        path = skill_dir / "references" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return skill_dir


class TestReadSkillOutput:
    def test_frontmatter_is_stripped_and_wrapped(self, skills_root) -> None:
        _make_skill(skills_root, "demo-skill")

        result = tools.read_skill.func(skill_name="demo-skill")

        assert skill_content_name(result) == "demo-skill"
        assert result.endswith("</skill_content>")
        assert "description: demo description" not in result
        assert "# 手順\n本文です" in result
        # references/ が無いスキルでは一覧の節自体を出さない
        assert "references/ のファイル" not in result

    def test_references_are_listed_with_path_memory_tokens(self, skills_root) -> None:
        _make_skill(skills_root, "demo-skill", {"b.md": "B", "sub/a.md": "A"})

        result = tools.read_skill.func(skill_name="demo-skill")

        lines = [line for line in result.splitlines() if "demo-skill/references/" in line]
        assert [line.split(" ", 1)[1] for line in lines] == [
            "demo-skill/references/b.md",
            "demo-skill/references/sub/a.md",
        ]
        assert all(re.match(r"^@\d+ ", line) for line in lines)
        # 一覧に載せるだけで中身は読まない
        assert "\nB\n" not in result and "\nA\n" not in result

    def test_references_without_path_memory_fall_back_to_relative_paths(self, skills_root, monkeypatch) -> None:
        monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", None)
        _make_skill(skills_root, "demo-skill", {"notes.md": "N"})

        result = tools.read_skill.func(skill_name="demo-skill")

        assert "\ndemo-skill/references/notes.md" in result

    def test_missing_skill_returns_error_without_wrapper(self, skills_root) -> None:
        result = tools.read_skill.func(skill_name="no-such-skill")

        assert result.startswith("Error:")
        assert skill_content_name(result) is None


class TestReadSkillFilePathMemoryToken:
    def test_token_from_read_skill_is_readable(self, skills_root) -> None:
        _make_skill(skills_root, "demo-skill", {"notes.md": "参照資料の中身"})
        listing = tools.read_skill.func(skill_name="demo-skill")
        token = re.search(r"^(@\d+) demo-skill/references/notes.md$", listing, re.M).group(1)

        assert tools.read_skill_file.func(relative_path=token) == "参照資料の中身"

    def test_token_outside_skills_root_is_rejected(self, skills_root, tmp_path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        mapping = tools._path_memory_helpers._register_path_memory([str(outside)])
        token = next(iter(mapping))

        result = tools.read_skill_file.func(relative_path=token)

        assert result.startswith("Error:")
        assert "secret" not in result

    def test_unregistered_token_is_error(self, skills_root) -> None:
        result = tools.read_skill_file.func(relative_path="@999")

        assert result.startswith("Error:")


class TestSkillNameEnum:
    @pytest.fixture(autouse=True)
    def _restore_schema(self, monkeypatch):
        # apply_skill_name_enum はモジュール共有の read_skill を書き換えるため、テスト後に戻す
        monkeypatch.setattr(tools.read_skill, "args_schema", tools.read_skill.args_schema)

    def test_enum_is_sent_in_tool_schema(self) -> None:
        apply_skill_name_enum(["skill-a", "skill-b", "skill-a"])

        params = convert_to_openai_tool(tools.read_skill)["function"]["parameters"]

        assert params["properties"]["skill_name"]["enum"] == ["skill-a", "skill-b"]
        assert params["required"] == ["skill_name"]

    def test_unknown_name_is_rejected_before_tool_body(self) -> None:
        apply_skill_name_enum(["skill-a"])

        with pytest.raises(Exception, match="skill-a"):
            tools.read_skill.invoke({"skill_name": "hallucinated-skill"})

    def test_empty_names_keep_free_string(self) -> None:
        apply_skill_name_enum([])

        params = convert_to_openai_tool(tools.read_skill)["function"]["parameters"]

        assert "enum" not in params["properties"]["skill_name"]
