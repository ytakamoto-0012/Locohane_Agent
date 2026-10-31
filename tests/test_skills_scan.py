"""src/skills.py のスキル走査ログレベルの回帰テスト。

office_shared のような「SKILL.mdを意図的に持たない共用コードディレクトリ」は
起動のたびにWARNINGログを出し続けており、monitor-app-logの自動監視が
毎回誤って不具合候補として拾ってしまう原因になっていた（2026-08-22発見）。
"""

from pathlib import Path

from src import skills


def _write_skill_md(dir_path: Path, name: str) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\nbody\n",
        encoding="utf-8",
    )


class TestKnownNonSkillDirLogging:
    def test_office_shared_missing_skill_md_logs_debug_not_warning(self, tmp_path, caplog) -> None:
        (tmp_path / "office_shared").mkdir()
        _write_skill_md(tmp_path / "real-skill", "real-skill")

        with caplog.at_level("DEBUG", logger="src.skills"):
            result = skills.scan_skills(tmp_path)

        assert [s.name for s in result] == ["real-skill"]
        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert not any("office_shared" in r.getMessage() for r in warning_records)
        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        assert any("office_shared" in r.getMessage() for r in debug_records)

    def test_unknown_dir_missing_skill_md_still_warns(self, tmp_path, caplog) -> None:
        (tmp_path / "typo_skill_dir").mkdir()

        with caplog.at_level("DEBUG", logger="src.skills"):
            result = skills.scan_skills(tmp_path)

        assert result == []
        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("typo_skill_dir" in r.getMessage() for r in warning_records)


def test_all_bundled_skills_are_loaded() -> None:
    """同梱スキル（skills/）が1本も読み飛ばされないこと（frontmatter の YAML の誤り・説明の文字数超過で消えるのを防ぐ）。"""
    root = Path(__file__).resolve().parent.parent / "skills"
    expected = {p.parent.name for p in root.glob("*/SKILL.md")}
    assert {s.name for s in skills.scan_skills(root)} == expected
