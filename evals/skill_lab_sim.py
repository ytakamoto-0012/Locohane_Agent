"""仮想研究室: 本番のインスタンスに触れずに、スキル研究室のループ（評価 → AI 修正 → 再評価 → トライアウト）を本物の LLM で回す。

研究室の仕組み（AI 修正の Agent・判定・上限など）を変えたときに、実際に研究テーマを合格まで持っていけるかを確かめる開発用の道具。

- 隔離したインスタンス置き場（既定: 本番の instances と同じ階層の lab_sim/）に、本体インスタンス sim_app と
  スキル調整ワーカー sim_lab を作る。設定は本番の --source-instance / --lab の config_overrides.json を写し、
  データの置き場（[paths] common_data_dir）だけを仮想研究室の中に向ける（メモリー・テーマ・評価の一時フォルダも中に作られる）。
- 研究テーマは --theme-dir（本番のテーマのフォルダ）から仕様・ケース・設定パッチ・研究対象を写して作る。
  --assets-dir を渡すと研究対象はそちらから写す（最初の版から試したいとき）。
- ワーカーを同じプロセスで動かし（Worker.run_theme）、終わったら反復ごとの結果・AI 修正の分析・Agent の試しを表示する。

使い方:
    python -m evals.skill_lab_sim --source-instance yt001 --lab skill_lab_1 --theme-dir <テーマのフォルダ> [--assets-dir <assets>]
        [--tryout-repeats 10] [--max-iterations 8] [--max-hours 2] [--keep]

OS の一時フォルダは使わない（CLAUDE.md の規則）。前回の仮想研究室は --keep を付けない限り開始時に消す。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

APP = "sim_app"
LAB = "sim_lab"
_INSTANCE_ENV_KEYS = ("LOCOHANE_INSTANCE", "CONFIG_OVERRIDES_PATH", "LOCOHANE_INSTANCE_ENV", "COMMON_DATA_DIR", "PROJECT_LOCOHANE_DIR", "SKILL_LAB_TEMP_DIR")


def _overrides(root: Path, name: str) -> dict:
    path = root / name / "config_overrides.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def setup(sim_root: Path, source_root: Path, source_instance: str, lab: str, args) -> None:
    from admin import instances as inst

    instances = sim_root / "instances"
    for name, kind, src in ((APP, inst.KIND_APP, source_instance), (LAB, inst.KIND_SKILL_LAB, lab)):
        (instances / name).mkdir(parents=True, exist_ok=True)
        inst.write_instance(instances, inst.InstanceMeta(name=name, display_name=name, app_port=18999 if kind == inst.KIND_APP else 0, kind=kind))
        overrides = _overrides(source_root, src)
        overrides.setdefault("paths", {})["common_data_dir"] = str(sim_root / "data" / name)
        # インスタンス専用の拡張ディレクトリは本番の instances を指さないよう、仮想研究室の中にする
        overrides.setdefault("skill_lab", {})["instance_locohane_dir"] = str(instances / name)
        if kind == inst.KIND_APP:
            if args.tryout_repeats:
                overrides["skill_lab"]["tryout_repeats"] = str(args.tryout_repeats)
            if args.max_iterations:
                overrides["skill_lab"]["max_iterations"] = str(args.max_iterations)
            if args.max_hours:
                overrides["skill_lab"]["max_hours"] = str(args.max_hours)
        (instances / name / "config_overrides.json").write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")


def create_theme(theme_src: Path, assets_dir: Path | None, title: str) -> Path:
    from admin.skill_lab import promotion, themes
    from src.config import resolve_instances_root

    cfg = promotion.instance_config(resolve_instances_root(), APP)
    directory = themes.create_theme(themes.themes_root(cfg.common_data_dir), instance=APP, title=title, lab=LAB, actor="sim")
    for name in (themes.SPEC_FILENAME, themes.CONFIG_PATCH_FILENAME):
        if (theme_src / name).is_file():
            shutil.copy2(theme_src / name, directory / name)
    if (directory / themes.CASES_DIRNAME).exists():
        shutil.rmtree(directory / themes.CASES_DIRNAME)
    shutil.copytree(theme_src / themes.CASES_DIRNAME, directory / themes.CASES_DIRNAME)
    assets = assets_dir or theme_src / themes.ASSETS_DIRNAME
    if (directory / themes.ASSETS_DIRNAME).exists():
        shutil.rmtree(directory / themes.ASSETS_DIRNAME)
    shutil.copytree(assets, directory / themes.ASSETS_DIRNAME, ignore=shutil.ignore_patterns("__pycache__"))
    themes.update(directory, lambda d: d.update({"status": themes.STATUS_QUEUED, "loop": {"mode": "loop", "by": "sim", "lang": "ja"}}))
    return directory


def report(directory: Path) -> None:
    from admin.skill_lab import fixer, themes

    data = themes.read(directory)
    loop = data.get("loop") or {}
    print("\n==== 結果 ====")
    print(f"状態: {data.get('status')}")
    print(f"メッセージ: {loop.get('message')}")
    for it in data.get("iterations") or []:
        print(f"- 反復 {it['n']} {it['phase']} x{it.get('repeat')}: {it['verdict']} {fixer.failed_cases_text(it)}")
        fix = it.get("fix") or {}
        if fix:
            print(f"    修正 agent={fix.get('agent', False)} steps={fix.get('agent_steps')} applied={fix.get('applied')} errors={fix.get('errors')}")
            for t in fix.get("agent_trials") or []:
                print(f"    試し {t['id']} {t['kind']}: {t['summary']}")
            if fix.get("blockers"):
                print(f"    blockers: {json.dumps(fix['blockers'], ensure_ascii=False)}")
            print(f"    分析: {(fix.get('analysis') or '')[:800]}")
    print(f"テーマ: {directory}")


def main() -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m evals.skill_lab_sim")
    parser.add_argument("--source-instance", required=True, help="設定を写す本体インスタンス")
    parser.add_argument("--lab", required=True, help="LLM の接続先を写すスキル調整ワーカー")
    parser.add_argument("--theme-dir", required=True, type=Path, help="仕様・ケース・研究対象を写す研究テーマのフォルダ")
    parser.add_argument("--assets-dir", type=Path, help="研究対象（assets/ 相当）を別の場所から写す")
    parser.add_argument("--title", default="仮想研究室のテーマ")
    parser.add_argument("--root", type=Path, help="仮想研究室の置き場（既定: 本番の instances と同じ階層の lab_sim）")
    parser.add_argument("--tryout-repeats", type=int)
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--max-hours", type=float)
    parser.add_argument("--keep", action="store_true", help="前回の仮想研究室を消さない")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.chdir(PROJECT_ROOT)

    from src.config import resolve_instances_root

    for key in _INSTANCE_ENV_KEYS:
        os.environ.pop(key, None)
    source_root = resolve_instances_root()
    sim_root = (args.root or source_root.parent / "lab_sim").resolve()
    if sim_root == source_root or source_root.is_relative_to(sim_root):
        raise SystemExit(f"The simulation root must not contain the real instances: {sim_root}")
    if sim_root.exists() and not args.keep:
        shutil.rmtree(sim_root)
    setup(sim_root, source_root, args.source_instance, args.lab, args)
    # 以後（評価の子プロセスも）仮想研究室のインスタンスだけを見る
    os.environ["INSTANCES_DIR"] = str(sim_root / "instances")
    directory = create_theme(args.theme_dir.resolve(), args.assets_dir.resolve() if args.assets_dir else None, args.title)

    base_env = dict(os.environ)
    from evals.instance import apply_instance

    apply_instance(LAB)
    from src.llm import init_llm_concurrency

    init_llm_concurrency(0)
    from admin.skill_lab.worker import Worker

    worker = Worker(LAB, base_env)
    worker.run_theme(APP, directory)
    report(directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
