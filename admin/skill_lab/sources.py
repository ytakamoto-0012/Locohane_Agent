"""研究テーマへ取り込める資産の一覧（対象インスタンスの中に限る）。

- 利用者のドラフト: そのインスタンスの [skill_creator] draft_dir（<ユーザー名>/<スキル名>/）
- 正式スキル: そのインスタンスが走査するもの（同梱 skills/・共有の project_locohane_dir・専用の instance_locohane_dir）
- 正式エージェント: 同じく agents/
- サブエージェントに割り当てられるツール名（src/tools/registry.py の _SUBAGENT_TOOLS）
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from src.config import PROJECT_ROOT, Config

from . import themes

# ドラフトの状態のうち、取り込みの候補にしないもの（昇格済み・不採用）。
_CLOSED_DRAFT_STATUSES = frozenset({"promoted", "rejected"})


def origin_label(cfg: Config, path: Path) -> str:
    """正式資産の置き場の種類: builtin（同梱）/ instance（このインスタンス専用）/ shared（共有の project_locohane_dir）。"""
    resolved = path.resolve()
    if resolved.is_relative_to(cfg.instance_locohane_dir.resolve()):
        return "instance"
    if resolved.is_relative_to(cfg.skills_dir.resolve()) or resolved.is_relative_to(cfg.agents_dir.resolve()):
        return "builtin"
    return "shared"


def list_drafts(cfg: Config) -> list[dict]:
    root = cfg.skill_draft_dir
    if not root.is_dir():
        return []
    result = []
    for owner in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        for skill in sorted(p for p in owner.iterdir() if p.is_dir() and not p.name.startswith("_")):
            if not (skill / "SKILL.md").is_file():
                continue
            meta_path = skill / "_draft_meta.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
            except (OSError, ValueError):
                meta = {}
            status = meta.get("status", "draft")
            if status in _CLOSED_DRAFT_STATUSES:
                continue
            result.append(
                {
                    "owner": owner.name,
                    "name": skill.name,
                    "path": str(skill),
                    "status": status,
                    "kind": meta.get("kind"),
                    "updated_at": meta.get("updated_at"),
                    "returned_reason": meta.get("returned_reason"),
                    "lab_theme": (meta.get("lab") or {}).get("theme_id"),
                }
            )
    return result


def list_official_skills(cfg: Config) -> list[dict]:
    from src.skills import scan_skills

    return [
        {"name": s.name, "description": s.description, "path": str(s.dir_path), "origin": origin_label(cfg, s.dir_path)}
        for s in scan_skills([cfg.skills_dir, *cfg.locohane_skills_dirs])
    ]


def find_official_agent_file(cfg: Config, name: str) -> Path | None:
    """正式エージェント name の定義ファイル（後方のディレクトリが優先）。"""
    for root in reversed([cfg.agents_dir, *cfg.locohane_agents_dirs]):
        if (root / f"{name}.md").is_file():
            return root / f"{name}.md"
    return None


def list_official_agents(cfg: Config) -> list[dict]:
    from src.agent_types import scan_agent_types

    result = []
    for a in scan_agent_types([cfg.agents_dir, *cfg.locohane_agents_dirs]):
        path = find_official_agent_file(cfg, a.name)
        result.append(
            {
                "name": a.name,
                "description": a.description,
                "tools": a.tool_names,
                "path": str(path) if path else None,
                "origin": origin_label(cfg, path) if path else None,
            }
        )
    return result


def config_candidates(cfg: Config) -> dict:
    """設定画面でスキル名・スクリプト名・agent_type を選ばせる候補（admin/ini_catalog.py の "source"）。

    スクリプトは run_script の実行対象と同じ scripts/*.py（src/skills.py の _skill_has_scripts 参照）。
    "_" で始まるもの（_common.py 等、スクリプト同士で共用する補助モジュール）は候補に出さない（画面の手入力では指定できる）。
    """
    skills = []
    for s in list_official_skills(cfg):
        scripts_dir = Path(s["path"]) / "scripts"
        scripts = sorted(p.name for p in scripts_dir.glob("*.py") if not p.name.startswith("_")) if scripts_dir.is_dir() else []
        skills.append({"name": s["name"], "description": s["description"], "origin": s["origin"], "scripts": scripts})
    agents = [{"name": a["name"], "description": a["description"], "origin": a["origin"]} for a in list_official_agents(cfg)]
    return {"skills": skills, "agent_types": agents}


def find_official_skill_dir(cfg: Config, name: str) -> Path | None:
    for root in reversed([cfg.skills_dir, *cfg.locohane_skills_dirs]):
        if (root / name / "SKILL.md").is_file():
            return root / name
    return None


@functools.lru_cache(maxsize=1)
def subagent_tool_names() -> tuple[str, ...]:
    """サブエージェントに割り当てられるツール名（frontmatter の tools に書けるもの）。"""
    return _registry_tool_names("_SUBAGENT_TOOLS")


# main_agent_tool_names の結果（別プロセスで求めるため、一度求めたら使い回す）。失敗は _TOOL_NAMES_RETRY_SECONDS 秒の間
# 覚えておき、その間は求め直さずに同じエラーにする（設定画面を開く・保存するたびに最大 120 秒の別プロセスを待たないため）。
_TOOL_NAMES_RETRY_SECONDS = 300.0
_main_tool_names: tuple[str, ...] | None = None
_main_tool_names_error: tuple[float, Exception] | None = None
_main_tool_names_lock = threading.Lock()
_main_tool_names_done = threading.Event()
_main_tool_names_running = False


def _compute_main_agent_tool_names() -> None:
    global _main_tool_names, _main_tool_names_error, _main_tool_names_running
    try:
        names = _registry_tool_names("_BASE_TOOLS")
    except Exception as exc:  # noqa: BLE001 - 呼び出し側で投げ直す
        with _main_tool_names_lock:
            _main_tool_names_error = (time.monotonic(), exc)
    else:
        with _main_tool_names_lock:
            _main_tool_names, _main_tool_names_error = names, None
    finally:
        with _main_tool_names_lock:
            _main_tool_names_running = False
        _main_tool_names_done.set()


def prefetch_main_agent_tool_names() -> None:
    """main_agent_tool_names をバックグラウンドで求め始める（管理ツールの起動時。求め済み・求めている途中なら何もしない）。"""
    global _main_tool_names_running
    with _main_tool_names_lock:
        if _main_tool_names is not None or _main_tool_names_running:
            return
        if _main_tool_names_error is not None and time.monotonic() - _main_tool_names_error[0] < _TOOL_NAMES_RETRY_SECONDS:
            return
        _main_tool_names_running = True
        _main_tool_names_done.clear()
    threading.Thread(target=_compute_main_agent_tool_names, name="main_agent_tool_names", daemon=True).start()


def main_agent_tool_names(timeout: float | None = None) -> tuple[str, ...] | None:
    """メインエージェントのビルトインツール名（[main_agent_tool_guard] allow_entries に書けるもの。MCP の動的ツールは含まない）。

    timeout 秒（None なら終わるまで）待っても求め終わらなければ None（求めるのはバックグラウンドで続ける）。
    求められなかったときは RuntimeError（_TOOL_NAMES_RETRY_SECONDS 秒の間は求め直さずに同じエラー）。
    """
    prefetch_main_agent_tool_names()
    if not _main_tool_names_done.wait(timeout):
        return None
    with _main_tool_names_lock:
        if _main_tool_names is not None:
            return _main_tool_names
        if _main_tool_names_error is not None:
            raise RuntimeError(str(_main_tool_names_error[1])) from _main_tool_names_error[1]
    return None


def _registry_tool_names(attr: str) -> tuple[str, ...]:
    """src/tools/registry.py の attr に並ぶツールの名前。

    src.tools は chainlit 等を読み込むため、管理ツールのプロセスには持ち込まず別プロセスで求める。
    """
    code = f"import json; from src.tools import registry; print(json.dumps([t.name for t in registry.{attr}]))"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120
    )
    if out.returncode != 0:
        raise RuntimeError(f"Cannot get the list of tool names: {out.stderr.strip()[-500:]}")
    return tuple(json.loads(out.stdout.strip().splitlines()[-1]))


def imported_drafts(root: Path) -> dict[str, str]:
    """このインスタンスのテーマに取り込まれているドラフト（パス → テーマ ID、終わったテーマは除く）。"""
    result = {}
    for directory in themes.list_theme_dirs(root):
        data = themes.read_or_none(directory)
        if data is None:
            continue
        if data.get("status") in themes.CLOSED_STATUSES:
            continue
        for src in (data.get("sources") or {}).get("skills", {}).values():
            if src.get("kind") == "draft" and src.get("origin"):
                result[str(Path(src["origin"]).resolve())] = data["id"]
    return result
