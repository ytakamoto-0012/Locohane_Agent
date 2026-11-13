"""評価（evals/run_case.py）が本番（app.py）と同じ条件で動くこと（2026-11-13 ユーザー指摘）。

- システムプロンプトは本番と同じ関数（src/system_prompt_builder.py）で組み立てる。以前は評価だけ
  {{project_instructions}}・{{plan_approval_exempt_scripts}}・{{main_agent_blocked_tools_hint}} が残り、スキル一覧も
  [main_agent_tool_guard] visibility_mode を無視していた。
- 利用者のドラフトスキルは評価に出さない。
- 評価のメモリーは空から始めるが、本番の用語辞書（glossary）だけは写す（誰でも読めるものだけ）。
"""

from __future__ import annotations

import os
from pathlib import Path

from src import memory


def _config(monkeypatch, tmp_path):
    from src.config import load_config

    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("COMMON_DATA_DIR", str(tmp_path / "data"))
    return load_config()


def test_eval_prompt_has_no_unfilled_placeholders_and_matches_visibility_mode(monkeypatch, tmp_path):
    from src.agent_types import scan_agent_types
    from src.skills import render_skills_block_with_hint, scan_skills
    from src.system_prompt_builder import build_prompts, fill_session_parts

    cfg = _config(monkeypatch, tmp_path)
    skills = scan_skills([cfg.skills_dir, *cfg.locohane_skills_dirs])
    prompt, agents = build_prompts(cfg, skills, scan_agent_types([cfg.agents_dir, *cfg.locohane_agents_dirs]))
    prompt = fill_session_parts(prompt, cfg, drafts=[], memory_viewer="anonymous")
    assert "{{" not in prompt, prompt[prompt.index("{{") : prompt.index("{{") + 60]
    assert "<<DRAFT_SKILLS>>" not in prompt and "Draft skills" not in prompt  # ドラフトは出さない
    assert all("{{skills}}" not in a.system_prompt and "{{agent_types}}" not in a.system_prompt for a in agents)
    if cfg.main_agent_tool_guard_visibility_mode == "hint":
        # 本番と同じく、直接扱えないスキルは「委譲する」案内に差し替わる
        assert render_skills_block_with_hint(skills, cfg) in prompt


def test_run_case_uses_the_shared_prompt_builder():
    source = Path("evals/run_case.py").read_text(encoding="utf-8")
    assert "build_prompts(" in source and "fill_session_parts(" in source
    assert "build_system_prompt(" not in source  # 評価だけの組み立ては残さない


def _write_memory(root: Path, memory_type: str, name: str, owner: str | None, visibility: str | None, body: str) -> None:
    folder = root / (f"users/{owner}" if owner else "") / memory_type
    folder.mkdir(parents=True, exist_ok=True)
    vis = f"visibility: {visibility}\n" if visibility else ""
    (folder / f"{name}.md").write_text(f"---\nname: {name}\ndescription: {name}\ntype: {memory_type}\n{vis}---\n\n{body}\n", encoding="utf-8")


def test_glossary_is_copied_from_production_but_nothing_else(monkeypatch, tmp_path):
    from evals import run_case

    cfg = _config(monkeypatch, tmp_path)
    prod = Path(cfg.memory_dir)
    _write_memory(prod, "glossary", "kpi", None, None, "KPI は重要業績評価指標")
    _write_memory(prod, "glossary", "team-term", "tanaka", "shared", "社内略語の意味")
    _write_memory(prod, "glossary", "my-note", "tanaka", "private", "個人用の用語メモ")
    _write_memory(prod, "feedback", "answer", None, None, "前回の答え")
    dest = tmp_path / "eval_memory"
    dest.mkdir()
    copied = {p.relative_to(dest).as_posix() for p in run_case._copy_production_memories(cfg, dest)}
    assert copied == {"glossary/kpi.md", "users/tanaka/glossary/team-term.md"}
    assert not (dest / "feedback").exists()
    # 写したものは評価中のアシスタント（匿名ユーザー）から見える
    names = {m.name for m in memory.list_memories(dest, viewer="anonymous")}
    assert {"kpi", "team-term"} <= names and "my-note" not in names


def test_copy_memory_types_can_be_changed_or_disabled(monkeypatch, tmp_path):
    from evals import run_case
    from src.config import load_config

    cfg = _config(monkeypatch, tmp_path)
    _write_memory(Path(cfg.memory_dir), "glossary", "kpi", None, None, "x")
    monkeypatch.setenv("SKILL_LAB_COPY_MEMORY_TYPES", "")
    assert run_case._copy_production_memories(load_config(), tmp_path / "d1") == []
    monkeypatch.setenv("SKILL_LAB_COPY_MEMORY_TYPES", "glossary, nosuch")
    try:
        load_config()
    except ValueError as e:
        assert "nosuch" in str(e)
    else:
        raise AssertionError("unknown memory type must be rejected")


def test_eval_applies_the_instances_llm_concurrency():
    """評価でも [llm] max_concurrent_requests を適用する（以前は評価だけ既定の 1 のままだった）。"""
    source = Path("evals/run_case.py").read_text(encoding="utf-8")
    assert "init_llm_concurrency(config.llm_max_concurrent_requests)" in source
