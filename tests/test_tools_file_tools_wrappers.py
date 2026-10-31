"""Read/Glob/Grep/json_query/search_path_memory（src/tools.py の @tool ラッパー）の回帰テスト。

旧 run_readonly_script 経由の file-tools スクリプトをネイティブツール化した
（ISSUE-003）際の重要な仕様を固定化する:
- 相対パスは Path.cwd()（プロセスcwd）ではなく作業ディレクトリ（_resolve_workdir()）基準
- `@N` パスメモリーの解決・登録
- 重複呼び出しガード（config.ini [file_tools_duplicate_guard]）
- pydantic スキーマに args/kwargs 予約語衝突が無いこと

read_skill/read_skill_file/get_tool_source への重複呼び出しガード適用
（2026-08-10 issue: コンテキスト圧縮でメッセージ履歴から過去の読み込み結果が
消えた後もLLMが同じ大きいSKILL.mdを何度も読み直すthrashingの対策）も
このファイルで検証する。
"""

import json

import pytest

from src import tools


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {"thread_id": "thread-1"}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


@pytest.fixture
def file_tools_env(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    path_memory_dir = tmp_path / "path_memory_data"

    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", path_memory_dir)
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_MAX_ENTRIES", 500)
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", None)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())

    return workdir


class TestPydanticSchema:
    @pytest.mark.parametrize(
        "tool_obj",
        [tools.read_tool, tools.glob_tool, tools.grep_tool, tools.json_query, tools.search_path_memory],
    )
    def test_schema_has_no_pydantic_placeholder_fields(self, tool_obj) -> None:
        schema = tool_obj.args
        assert not any(name in ("args", "kwargs") for name in schema)
        assert not any(name.startswith("v__") for name in schema)


class TestReadTool:
    def test_reads_absolute_path_and_registers_path_memory(self, file_tools_env) -> None:
        f = file_tools_env / "notes.txt"
        f.write_text("line1\nline2\n", encoding="utf-8")

        result = json.loads(tools.read_tool.func(file_path=str(f)))

        assert result["total_lines"] == 2
        assert result["path_memory"] == {"@1": str(f.resolve())}

    def test_relative_path_resolves_against_workdir_not_process_cwd(self, file_tools_env) -> None:
        # work_dir未設定時、相対パスは default_workdir 直下ではなくスレッド
        # 専用フォルダ（_resolve_exec_workdir()）基準で解決される（2026-08-29
        # 修正、別スレッドのデータを拾わないための分離）。
        resolved_workdir = tools._workdir._resolve_exec_workdir()
        f = resolved_workdir / "notes.txt"
        f.write_text("hello\n", encoding="utf-8")

        result = json.loads(tools.read_tool.func(file_path="notes.txt"))

        assert result["path"] == str(f)

    def test_resolves_path_memory_token(self, file_tools_env) -> None:
        f = file_tools_env / "notes.txt"
        f.write_text("hello\n", encoding="utf-8")
        tools.read_tool.func(file_path=str(f))

        result = json.loads(tools.read_tool.func(file_path="@1", offset=0, limit=5))

        assert result["path"] == str(f.resolve())

    def test_unregistered_token_returns_error(self, file_tools_env) -> None:
        result = tools.read_tool.func(file_path="@99")

        assert result.startswith("Error:")
        assert "is not registered" in result

    def test_missing_file_returns_error(self, file_tools_env) -> None:
        result = tools.read_tool.func(file_path=str(file_tools_env / "nope.txt"))

        assert result.startswith("Error:")

    def test_duplicate_call_is_blocked(self, file_tools_env) -> None:
        f = file_tools_env / "notes.txt"
        f.write_text("hello\n", encoding="utf-8")

        first = tools.read_tool.func(file_path=str(f))
        second = tools.read_tool.func(file_path=str(f))

        assert not first.startswith("Error:")
        assert second.startswith("Error:")
        assert "up to the limit" in second


class TestGlobTool:
    def test_lists_files_and_registers_path_memory(self, file_tools_env) -> None:
        (file_tools_env / "a.py").write_text("a", encoding="utf-8")
        (file_tools_env / "sub").mkdir()
        (file_tools_env / "sub" / "b.py").write_text("b", encoding="utf-8")

        result = json.loads(tools.glob_tool.func(pattern="**/*.py"))

        # ** を書いても直下までしか探索しない仕様のため、sub/b.py は対象外。
        assert result["total_matches"] == 1
        assert "path_memory" in result

    def test_empty_path_resolves_to_workdir(self, file_tools_env) -> None:
        # work_dir未設定時、パス省略時の既定検索先はdefault_workdir直下では
        # なくスレッド専用フォルダ（_resolve_exec_workdir()）になる
        # （2026-08-29修正、別スレッドのデータを拾わないための分離）。
        resolved_workdir = tools._workdir._resolve_exec_workdir()
        (resolved_workdir / "a.py").write_text("a", encoding="utf-8")

        result = json.loads(tools.glob_tool.func(pattern="*.py", path=""))

        assert result["base"] == str(resolved_workdir)

    def test_missing_base_returns_error(self, file_tools_env) -> None:
        result = tools.glob_tool.func(pattern="*.py", path=str(file_tools_env / "nope"))

        assert result.startswith("Error:")


class TestGrepTool:
    def test_content_mode(self, file_tools_env) -> None:
        (file_tools_env / "a.py").write_text("before\nTODO: fix\nafter\n", encoding="utf-8")

        result = json.loads(tools.grep_tool.func(pattern="TODO", context=1))

        assert result["matched"] is True
        assert "path_memory" in result
        lines = [m["line"] for m in result["matches"]]
        assert lines == [1, 2, 3]

    def test_large_result_is_capped_by_total_chars(self, file_tools_env) -> None:
        # head_limit を大きくしても、合計文字数上限で切り詰めてヒントを返す
        # （006 で `.*` + head_limit=5000 の1回が45.9万トークンになり異常終了した）。
        for i in range(30):
            (file_tools_env / f"{i:02d}.md").write_text(("x" * 300 + "\n") * 100, encoding="utf-8")

        raw = tools.grep_tool.func(pattern=".*", glob="*.md", head_limit=5000)
        result = json.loads(raw)

        assert result["truncated"] is True
        assert result["total_matches"] == 3000
        assert result["returned"] == len(result["matches"]) < 3000
        assert "Glob" in result["hint"]
        assert len(raw) < 40000

    def test_long_line_is_truncated(self, file_tools_env) -> None:
        (file_tools_env / "min.js").write_text("TODO" + "y" * 5000 + "\n", encoding="utf-8")

        result = json.loads(tools.grep_tool.func(pattern="TODO"))

        assert result["returned"] == 1
        assert len(result["matches"][0]["text"]) < 1000
        assert "hint" not in result

    def test_no_match_has_no_path_memory_key(self, file_tools_env) -> None:
        (file_tools_env / "a.py").write_text("nothing\n", encoding="utf-8")

        result = json.loads(tools.grep_tool.func(pattern="TODO"))

        assert result["matched"] is False
        assert "path_memory" not in result


class TestJsonQuery:
    def test_query_from_file(self, file_tools_env) -> None:
        f = file_tools_env / "data.json"
        f.write_text('{"a": {"b": 1}}', encoding="utf-8")

        result = json.loads(tools.json_query.func(query="a.b", file_path=str(f)))

        assert result == {"result": 1}

    def test_query_from_json_text(self, file_tools_env) -> None:
        result = json.loads(tools.json_query.func(query="a.b", json_text='{"a": {"b": 2}}'))

        assert result == {"result": 2}

    def test_both_and_neither_are_errors(self, file_tools_env) -> None:
        f = file_tools_env / "data.json"
        f.write_text("{}", encoding="utf-8")

        both = tools.json_query.func(query="a", file_path=str(f), json_text="{}")
        neither = tools.json_query.func(query="a")

        assert both.startswith("Error:")
        assert neither.startswith("Error:")

    def test_different_json_text_are_not_treated_as_duplicate(self, file_tools_env) -> None:
        first = tools.json_query.func(query="a.b", json_text='{"a": {"b": 1}}')
        second = tools.json_query.func(query="a.b", json_text='{"a": {"b": 2}}')

        assert not first.startswith("Error:")
        assert not second.startswith("Error:")


class TestForeignTmpDirGuard:
    """他セッションの `_tmp_<thread_id>` への読み取りを拒否するガードの回帰テスト。

    file_tools_env フィクスチャは thread_id="thread-1" でセッションをモックする
    （_FakeUserSession の既定値）。ここでは自セッション用の `_tmp_thread-1` と
    他セッション用の `_tmp_thread-2` を作業ディレクトリ配下へ用意し、
    自分は読める・他人は読めない・無関係なパスは今まで通り無制限、を確認する。
    """

    def test_read_foreign_tmp_dir_is_blocked(self, file_tools_env) -> None:
        foreign = file_tools_env / "_tmp_thread-2"
        foreign.mkdir()
        leaked = foreign / "leaked.txt"
        leaked.write_text("secret", encoding="utf-8")

        result = tools.read_tool.func(file_path=str(leaked))

        assert result.startswith("Error:")
        assert "another session" in result

    def test_read_own_tmp_dir_still_succeeds(self, file_tools_env) -> None:
        own = file_tools_env / f"_tmp_{tools._workdir._exec_tmp_name()}"
        own.mkdir()
        f = own / "own.txt"
        f.write_text("mine", encoding="utf-8")

        result = json.loads(tools.read_tool.func(file_path=str(f)))

        assert result["content"].endswith("mine")

    def test_read_file_named_with_tmp_prefix_is_not_blocked(self, file_tools_env) -> None:
        """`_tmp_` で始まる名前の「ファイル」はセッション作業フォルダ
        （常にディレクトリ）ではないため、他セッション扱いされてはならない
        （2026-08-22 回帰: edit_excel.py が自分で作った `_tmp_ops.json` を
        読めずクラッシュしていたバグの修正確認）。"""
        f = file_tools_env / "_tmp_ops.json"
        f.write_text("[]", encoding="utf-8")

        result = json.loads(tools.read_tool.func(file_path=str(f)))

        assert result["content"].endswith("[]")

    def test_glob_over_workdir_excludes_foreign_tmp_but_keeps_siblings(self, file_tools_env) -> None:
        # work_dir未設定時のパス省略はスレッド専用フォルダを見るため（上記
        # test_empty_path_resolves_to_workdir参照）、ここでは検証したい
        # default_workdir直下を明示的にpathへ指定する。
        (file_tools_env / "notes.txt").write_text("hello", encoding="utf-8")
        foreign = file_tools_env / "_tmp_thread-2"
        foreign.mkdir()
        (foreign / "leaked.txt").write_text("secret", encoding="utf-8")

        result = json.loads(tools.glob_tool.func(pattern="**/*", path=str(file_tools_env)))

        registered_paths = set(result.get("path_memory", {}).values())
        assert not any("leaked.txt" in p for p in registered_paths)
        assert result["directories"] == []
        assert any(p.endswith("notes.txt") for p in registered_paths)

    def test_path_outside_workdir_is_completely_unaffected(self, file_tools_env, tmp_path) -> None:
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        f = unrelated / "notes.txt"
        f.write_text("hello\n", encoding="utf-8")

        result = json.loads(tools.read_tool.func(file_path=str(f)))

        assert result["content"].endswith("hello")


@pytest.fixture
def skill_tools_env(tmp_path, monkeypatch):
    """read_skill/read_skill_file/get_tool_source の重複ガードテスト用環境。

    file_tools_env と同じ cl.user_session/_LLM_CONFIG のモックに加え、
    _SKILLS_ROOTS を一時ディレクトリへ差し替える（test_tools_script_resolution.py
    の skills_root フィクスチャと同じ方式）。
    """
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(tools._state, "_SKILLS_ROOTS", [root])
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", None)
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    return root


class TestReadSkillDuplicateGuard:
    def test_duplicate_call_is_blocked(self, skill_tools_env) -> None:
        skill_dir = skill_tools_env / "demo-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\n---\nbody", encoding="utf-8")

        first = tools.read_skill.func(skill_name="demo-skill")
        second = tools.read_skill.func(skill_name="demo-skill")

        assert not first.startswith("Error:")
        assert second.startswith("Error:")
        assert "up to the limit" in second

    def test_different_skill_name_is_not_treated_as_duplicate(self, skill_tools_env) -> None:
        for name in ("skill-a", "skill-b"):
            skill_dir = skill_tools_env / name
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")

        first = tools.read_skill.func(skill_name="skill-a")
        second = tools.read_skill.func(skill_name="skill-b")

        assert not first.startswith("Error:")
        assert not second.startswith("Error:")


class TestReadSkillFileDuplicateGuard:
    def test_duplicate_call_is_blocked(self, skill_tools_env) -> None:
        skill_dir = skill_tools_env / "demo-skill"
        skill_dir.mkdir()
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "notes.md").write_text("notes", encoding="utf-8")

        first = tools.read_skill_file.func(relative_path="demo-skill/references/notes.md")
        second = tools.read_skill_file.func(relative_path="demo-skill/references/notes.md")

        assert not first.startswith("Error:")
        assert second.startswith("Error:")
        assert "up to the limit" in second

    def test_missing_skill_prefix_gets_hint(self, skill_tools_env) -> None:
        skill_dir = skill_tools_env / "demo-skill"
        skill_dir.mkdir()
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "notes.md").write_text("notes", encoding="utf-8")

        result = tools.read_skill_file.func(relative_path="references/notes.md")

        assert result.startswith("Error:")
        assert "skill folder name" in result

    def test_valid_skill_prefix_but_missing_file_gets_default_error(self, skill_tools_env) -> None:
        skill_dir = skill_tools_env / "demo-skill"
        skill_dir.mkdir()

        result = tools.read_skill_file.func(relative_path="demo-skill/references/missing.md")

        assert result.startswith("Error:")
        assert "skill folder name" not in result


class TestGetToolSourceDuplicateGuard:
    def test_duplicate_call_is_blocked(self, skill_tools_env) -> None:
        scripts_dir = skill_tools_env / "demo-skill" / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "count.py").write_text("print('hi')", encoding="utf-8")

        first = tools.get_tool_source.func(skill_name="demo-skill", script_filename="count.py")
        second = tools.get_tool_source.func(skill_name="demo-skill", script_filename="count.py")

        assert not first.startswith("Error:")
        assert second.startswith("Error:")
        assert "up to the limit" in second


class TestSearchPathMemory:
    def test_empty_query_lists_recent_entries(self, file_tools_env) -> None:
        f = file_tools_env / "notes.txt"
        f.write_text("hello\n", encoding="utf-8")
        tools.read_tool.func(file_path=str(f))

        result = json.loads(tools.search_path_memory.func())

        assert result["entries"] == [
            {"index": 1, "path": str(f.resolve()), "valid": True, "description": None}
        ]

    def test_empty_when_nothing_registered(self, file_tools_env) -> None:
        result = json.loads(tools.search_path_memory.func())

        assert result["entries"] == []
        assert "Glob" in result["hint"]

    def test_empty_query_returns_newest_first_limited_by_top_k(self, file_tools_env) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            f = file_tools_env / name
            f.write_text("x\n", encoding="utf-8")
            tools.read_tool.func(file_path=str(f))

        result = json.loads(tools.search_path_memory.func(top_k=2))

        assert [e["index"] for e in result["entries"]] == [3, 2]

    def test_query_finds_similar_path(self, file_tools_env) -> None:
        target = file_tools_env / "月次報告書.txt"
        other = file_tools_env / "notes.txt"
        for f in (target, other):
            f.write_text("x\n", encoding="utf-8")
            tools.read_tool.func(file_path=str(f))

        result = json.loads(tools.search_path_memory.func(query="月次報告署"))

        assert result["entries"][0]["path"] == str(target.resolve())
        assert all(e["path"] != str(other.resolve()) for e in result["entries"])

    def test_min_score_from_config_is_applied(self, file_tools_env, monkeypatch) -> None:
        f = file_tools_env / "月次報告書.txt"
        f.write_text("x\n", encoding="utf-8")
        tools.read_tool.func(file_path=str(f))
        monkeypatch.setattr(tools._state, "_PATH_MEMORY_SEARCH_MIN_SCORE", 0.99)

        result = json.loads(tools.search_path_memory.func(query="月次報告署"))

        assert result["entries"] == []

    def test_read_not_found_suggests_similar_registered_path(self, file_tools_env) -> None:
        target = file_tools_env / "月次報告書.txt"
        target.write_text("x\n", encoding="utf-8")
        tools.read_tool.func(file_path=str(target))

        result = tools.read_tool.func(file_path=str(file_tools_env / "月次報告署.txt"))

        assert result.startswith("Error: File not found")
        assert f"@1 (different file name: {target.resolve()})" in result
        assert "do not replace it" in result

    def test_read_not_found_without_similar_entry_has_no_hint(self, file_tools_env) -> None:
        result = tools.read_tool.func(file_path=str(file_tools_env / "missing.txt"))

        assert result.startswith("Error: File not found")
        assert "similar entries in path memory" not in result

    def test_read_not_found_does_not_suggest_deleted_registered_path(self, file_tools_env) -> None:
        target = file_tools_env / "月次報告書.txt"
        target.write_text("x\n", encoding="utf-8")
        tools.read_tool.func(file_path=str(target))
        target.unlink()

        # 同一引数だと重複ガードに止められるため offset を変える
        result = tools.read_tool.func(file_path=str(target), offset=1)

        assert result.startswith("Error: File not found")
        assert "similar entries in path memory" not in result

    def test_empty_query_excludes_deleted_entries(self, file_tools_env) -> None:
        kept = file_tools_env / "a.txt"
        gone = file_tools_env / "b.txt"
        for f in (kept, gone):
            f.write_text("x\n", encoding="utf-8")
            tools.read_tool.func(file_path=str(f))
        gone.unlink()

        result = json.loads(tools.search_path_memory.func())

        assert [e["index"] for e in result["entries"]] == [1]
