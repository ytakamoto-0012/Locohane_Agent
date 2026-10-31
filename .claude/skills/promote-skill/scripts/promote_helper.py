"""promote-skill スキルの補助スクリプト（ドラフトの一覧・事前確認・昇格・差し戻し）。

判断（トライアウト結果の judge 判定・ユーザーの承認）は Claude Code が行い、
ここではファイル操作と機械的な確認だけを行う。`python_env.bat` の `PYTHON_DIR` の Python（`<PYTHON_DIR>\\Scripts\\python.exe`）で、
プロジェクトルートから実行する:

    python .claude/skills/promote-skill/scripts/promote_helper.py list
    python .claude/skills/promote-skill/scripts/promote_helper.py check <ドラフトのフォルダ> --instance <名前>
    python .claude/skills/promote-skill/scripts/promote_helper.py install <ドラフトのフォルダ> --instance <名前> --tryout <tryout.json>
    python .claude/skills/promote-skill/scripts/promote_helper.py return <ドラフトのフォルダ> --reason "..." [--reject]

ハッシュ・コピー規則は evals/skill_tree.py（skill-creator の _common.py と同じ規則）を使う。

昇格先は --instance のインスタンス専用の置き場（[skill_lab].instance_locohane_dir の skills/、
既定 instances/<名前>/skills/）だけ。同梱の skills/ や全インスタンス共通の
project_locohane_dir に置くと他のインスタンスまで汚染するため。同梱・共有スキルの改善案も
専用の置き場に同じ名前で置く（そのインスタンスでだけ置き換わる）。昇格したドラフトは
[skill_creator] archive_dir へ移す（管理ツールのスキル研究室と同じ admin/skill_lab/promotion.py の
archive_draft()）。

install は昇格の前に次を確かめる:
- tryout.json（evals/run_all.py が書く）が、今のドラフトの内容（SKILL.md・scripts 等の
  ハッシュとケースのハッシュ）を、全ケース・tryout_repeats 回以上で評価したものか。
  トライアウト後にドラフトが1か所でも変わっていれば合格回数は0に戻ったものとして
  扱い、昇格させない。
- ドラフトのスクリプトは会話では計画承認・[main_agent_tool_guard] を免除されて
  いたため、昇格後も同じ挙動になるよう、--instance の
  インスタンスの config_overrides.json の [plan].plan_approval_exempt_scripts と
  [main_agent_tool_guard].allow_entries（max_calls=-1）へ登録する。トライアウト
  （evals/run_case.py --skill-overlay）も登録済みと同じ状態で評価している。
  config_overrides.json の値はキー単位で config.ini を置き換えるため、今の実効値
  （上書き値、無ければ config.ini の値）に足した全体を書く。保存は管理ツールと同じ
  admin/overrides.py の save() で行う（load_config() による検証・バックアップ・
  instances/admin_changes.log への記録）。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from evals.skill_tree import DRAFT_EVALS_DIRNAME, DRAFT_META_FILENAME, cases_sha256, copy_ignore_for, tree_sha256  # noqa: E402

PROMOTION_LOG = PROJECT_ROOT / "evals" / "promotion_log.md"
CONFIG_INI_PATH = PROJECT_ROOT / "config.ini"

# 昇格時に登録する config_overrides.json のキー（セクション, キー, 対応する環境変数）。
# 環境変数は config_overrides.json より優先されるため、インスタンスの .env に
# あると登録しても効かない（その場合は登録せずに止める）。
_PLAN_EXEMPT_KEY = ("plan", "plan_approval_exempt_scripts", "PLAN_APPROVAL_EXEMPT_SCRIPTS")
_ALLOW_ENTRIES_KEY = ("main_agent_tool_guard", "allow_entries", "MAIN_AGENT_TOOL_GUARD_ALLOW_ENTRIES")


def _instance_names() -> list[str]:
    from src.config import resolve_instances_root

    root = resolve_instances_root(CONFIG_INI_PATH)
    names = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    return names or ["default"]


def _instance_config(name: str) -> dict:
    """インスタンスを適用した実効設定のうち、昇格に要る値を別プロセスで求める。"""
    code = (
        "import json,sys; from evals.instance import apply_instance; apply_instance(sys.argv[1]);"
        "from src.config import load_config; c=load_config();"
        "print(json.dumps({'draft_dir': str(c.skill_draft_dir), 'tryout_repeats': c.skill_tryout_repeats,"
        "'skills_roots': [str(c.skills_dir), *[str(d) for d in c.locohane_skills_dirs]],"
        "'dest_root': str(c.instance_locohane_dir / 'skills'), 'archive_dir': str(c.skill_archive_dir)}))"
    )
    out = subprocess.run([sys.executable, "-c", code, name], cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise SystemExit(f"インスタンス {name} の設定を読めません: {out.stderr.strip()}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def _read_meta(draft: Path) -> dict:
    path = draft / DRAFT_META_FILENAME
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _write_meta(draft: Path, meta: dict) -> None:
    (draft / DRAFT_META_FILENAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _draft_cases(draft: Path) -> list[str]:
    return sorted(p.stem for p in (draft / DRAFT_EVALS_DIRNAME).glob("*.yaml"))


def _register_entries(draft: Path) -> list[tuple[str, str]]:
    """昇格後も計画承認・main_agent_tool_guard を免除するため登録する (スキル名, スクリプト名)。"""
    from src.skill_drafts import guard_exempt_entries_for_skill

    return guard_exempt_entries_for_skill(draft.name, draft)


def _instances_using(dest_roots: list[Path]) -> list[str]:
    """dest_roots のいずれかを正式スキルの走査ルートに持つインスタンス名。"""
    wanted = {r.resolve() for r in dest_roots}
    return [n for n in _instance_names() if wanted & {Path(r).resolve() for r in _instance_config(n)["skills_roots"]}]


def cmd_list(_args) -> int:
    seen: set[str] = set()
    drafts = []
    for name in _instance_names():
        cfg = _instance_config(name)
        root = Path(cfg["draft_dir"])
        if str(root) in seen or not root.is_dir():
            continue
        seen.add(str(root))
        for owner in sorted(p for p in root.iterdir() if p.is_dir()):
            for skill in sorted(p for p in owner.iterdir() if p.is_dir() and not p.name.startswith("_")):
                if not (skill / "SKILL.md").is_file():
                    continue
                meta = _read_meta(skill)
                tryouts = meta.get("tryouts") or []
                drafts.append(
                    {
                        "instance": name,
                        "draft": f"{owner.name}/{skill.name}",
                        "path": str(skill),
                        "kind": meta.get("kind"),
                        "status": meta.get("status", "draft"),
                        "updated_at": meta.get("updated_at"),
                        "eval_cases": _draft_cases(skill),
                        "last_tryout": tryouts[-1] if tryouts else None,
                        "tryout_repeats": cfg["tryout_repeats"],
                        "skills_roots": cfg["skills_roots"],
                        "dest_root": cfg["dest_root"],
                    }
                )
    print(json.dumps(drafts, ensure_ascii=False, indent=2))
    return 0


def _check_problems(draft: Path, instance: str) -> tuple[list[str], list[Path], list[str]]:
    """昇格を止めるべき問題・同名の正式スキル・ケース一覧を返す（check と install で共有）。"""
    meta = _read_meta(draft)
    name = draft.name
    problems: list[str] = []
    official = [Path(r) / name for r in _instance_config(instance)["skills_roots"] if (Path(r) / name / "SKILL.md").is_file()]
    if meta.get("status", "draft") != "draft":
        problems.append(f"status が draft ではありません: {meta.get('status')}")
    if meta.get("kind") == "improve":
        base = Path(meta.get("base_root", "")) / meta.get("base_skill", name)
        if not (base / "SKILL.md").is_file():
            problems.append(f"元にした正式スキルが見つかりません: {base}")
        elif tree_sha256(base) != meta.get("base_sha256"):
            problems.append(f"元にした正式スキルがドラフト作成後に変更されています（上書きすると変更が失われる）: {base}")
    elif official:
        problems.append(f"同じ名前の正式スキルがあります: {[str(p) for p in official]}")
    cases = _draft_cases(draft)
    if not cases:
        problems.append("evals/ にケースがありません（トライアウトできない）")
    return problems, official, cases


def _tryout_problems(draft: Path, tryout: dict, required_repeats: int, judged_pass: bool) -> list[str]:
    """tryout.json が今のドラフトを全ケース・規定回数で合格したものかを確かめる。

    トライアウト後にドラフト（スキル本体・ケース）が1か所でも変われば、それまでの
    合格回数は0に戻ったものとして扱う（試したものと違うものを正式スキルにしない）。
    """
    problems: list[str] = []
    if tryout.get("verdict") != "pass" and not (tryout.get("verdict") == "needs_judge" and judged_pass):
        problems.append(
            f"トライアウトが合格（verdict=pass）ではありません: {tryout.get('verdict')}"
            "（needs_judge は judge を読んで全回合格と判断した場合のみ --judged-pass を付ける）"
        )
    if (tryout.get("repeat") or 0) < required_repeats:
        problems.append(f"トライアウトの回数が足りません: {tryout.get('repeat')} 回（必要: {required_repeats} 回以上）")
    if sorted(tryout.get("case_files") or []) != _draft_cases(draft):
        problems.append(
            f"トライアウトしたケースがドラフトの全ケースと一致しません: {tryout.get('case_files')}（ドラフト: {_draft_cases(draft)}）"
        )
    if tryout.get("cases_sha256") != cases_sha256(draft / DRAFT_EVALS_DIRNAME):
        problems.append("トライアウト後にケースが変更されています（合格回数は0に戻ったため、今の内容でトライアウトし直す）")
    evaluated = [o.get("sha256") for o in tryout.get("skill_overlays") or [] if Path(o.get("path", "")).resolve() == draft.resolve()]
    if not evaluated:
        problems.append("このドラフトを --skill-overlay で評価したトライアウトではありません")
    elif evaluated != [tree_sha256(draft)]:
        problems.append("トライアウト後にドラフトが変更されています（合格回数は0に戻ったため、今の内容でトライアウトし直す）")
    return problems


def cmd_check(args) -> int:
    draft = Path(args.draft).resolve()
    meta = _read_meta(draft)
    problems, official, cases = _check_problems(draft, args.instance)
    print(
        json.dumps(
            {
                "draft": str(draft),
                "kind": meta.get("kind"),
                "official_same_name": [str(p) for p in official],
                "eval_cases": cases,
                "register_entries": [list(e) for e in _register_entries(draft)],
                "ok": not problems,
                "problems": problems,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not problems else 1


def _format_list_literal(items: list) -> str:
    """config.ini と同じ見た目（1要素1行）の Python リストリテラル文字列にする。"""
    if not items:
        return "[]"
    return "[\n" + "".join(f"    {json.dumps(item, ensure_ascii=False)},\n" for item in items) + "]"


def _merged_value(
    current_text: str, entries: list[tuple[str, str]], allow_entries: bool, default_text: str = ""
) -> tuple[str | None, list]:
    """今の値（リストリテラル文字列）に entries を足した新しい値と、足した要素を返す（足すものが無ければ None）。

    default_text（config.ini の既定値）にある要素も足さない。config_overrides.json は既定値に足す分だけを
    持つ（src/config.py の ADDITIVE_OVERRIDE_KEYS）ため、current_text には上書きの値だけを渡す。
    """
    items = ast.literal_eval(current_text.strip()) if current_text.strip() else []
    defaults = ast.literal_eval(default_text.strip()) if default_text.strip() else []
    if allow_entries:
        registered = {tuple(k) if isinstance(k, list) else k for k, _ in [*items, *defaults]}
        added = [[list(e), -1] for e in entries if e not in registered]
    else:
        registered = {tuple(i) for i in [*items, *defaults]}
        added = [list(e) for e in entries if e not in registered]
    if not added:
        return None, []
    return _format_list_literal([*items, *added]), added


def _register_guard_exemptions(instance: str, entries: list[tuple[str, str]], actor: str) -> dict:
    """entries を instance の config_overrides.json の2キーへ登録する（管理ツールと同じ保存処理）。"""
    from dotenv import dotenv_values

    from admin import instances as inst
    from admin import overrides
    from admin.ini_catalog import find_key, parse_file
    from src.config import load_config, merge_additive_override, resolve_instances_root

    instances_root = resolve_instances_root(CONFIG_INI_PATH)
    env_file = inst.env_path(instances_root, instance)
    instance_env = dotenv_values(env_file) if env_file.is_file() else {}
    blocked = [env for _, _, env in (_PLAN_EXEMPT_KEY, _ALLOW_ENTRIES_KEY) if instance_env.get(env)]
    if blocked:
        raise SystemExit(
            f"インスタンス {instance} の .env に {blocked} があり、config_overrides.json への登録が効きません。"
            f"{env_file} の該当行を見直してから昇格し直してください。"
        )
    catalog = parse_file(CONFIG_INI_PATH)
    path = inst.overrides_path(instances_root, instance)
    existing = overrides.read(path)
    updates: dict[tuple[str, str], str] = {}
    added: dict[str, list] = {}
    for section, key, _env in (_PLAN_EXEMPT_KEY, _ALLOW_ENTRIES_KEY):
        info = find_key(catalog, section, key)
        if info is None:
            raise SystemExit(f"config.ini に [{section}].{key} がありません。")
        current = existing.get(section, {}).get(key, "")
        new_value, new_items = _merged_value(current, entries, allow_entries=(key == "allow_entries"), default_text=info.default_value)
        if new_value is not None:
            # overrides.save() には画面と同じく既定値を含めた全体を渡す（足す分だけを取り出して保存する）。
            updates[(section, key)] = merge_additive_override(section, key, info.default_value, new_value)[0]
            added[f"{section}.{key}"] = new_items
    if not updates:
        return {"instance": instance, "added": {}}
    overrides.save(
        path=path,
        updates=updates,
        resets=[],
        base_mtime=overrides.mtime_or_none(path),
        ini_catalog=catalog,
        config_ini_path=CONFIG_INI_PATH,
        backup_dir=inst.backups_dir(instances_root, instance),
        backup_keep=load_config().admin_backup_keep,
        audit_log_path=instances_root / "admin_changes.log",
        instance_name=instance,
        actor=actor,
        remote_addr="local",
        instances_root=instances_root,
    )
    return {"instance": instance, "added": added, "path": str(path)}


def cmd_install(args) -> int:
    draft = Path(args.draft).resolve()
    cfg = _instance_config(args.instance)
    dest_root = Path(cfg["dest_root"]).resolve()
    if args.dest and Path(args.dest).resolve() != dest_root:
        raise SystemExit(
            f"昇格先はインスタンス {args.instance} 専用の置き場（{dest_root}）だけです"
            "（同梱の skills/ や共有の project_locohane_dir に置くと他のインスタンスまで汚染するため）。"
        )
    problems, _, _ = _check_problems(draft, args.instance)
    tryout = json.loads(Path(args.tryout).read_text(encoding="utf-8"))
    problems += _tryout_problems(draft, tryout, cfg["tryout_repeats"], args.judged_pass)
    if problems:
        raise SystemExit("昇格できません: " + " / ".join(problems))
    meta = _read_meta(draft)
    target = dest_root / draft.name
    backup = None
    base_dir = Path(meta.get("base_root", "")) / meta.get("base_skill", draft.name)
    # 改善案は、元の正式スキルが専用の置き場にあればその場で置き換え、同梱・共有にあれば
    # 専用の置き場に同じ名前で置く（そのインスタンスでだけ置き換わる）。
    replaces_base = meta.get("kind") == "improve" and target.resolve() == base_dir.resolve()
    if target.exists() and not replaces_base:
        raise SystemExit(f"昇格先に同名のスキルがあります: {target}")

    # 設定の登録を先に行う（load_config() の検証で失敗したら、スキルを置く前に止める）。
    # スキルは --instance 専用の置き場にだけ置くため、設定もそのインスタンスにだけ登録する
    # （他のインスタンスへの配布は管理ツールのスキル研究室で行う）。
    others = [n for n in args.register_instance if n != args.instance]
    if others:
        raise SystemExit(
            f"--register-instance に {others} は指定できません。スキルは {args.instance} 専用の置き場にだけ置くため、"
            f"設定も {args.instance} にだけ登録します（他のインスタンスへの配布は管理ツールのスキル研究室で行ってください）。"
        )
    entries = _register_entries(draft)
    registrations = []
    if entries:
        for name in [args.instance]:
            registrations.append(_register_guard_exemptions(name, entries, actor=f"promote-skill ({draft.parent.name}/{draft.name})"))

    if target.exists():
        backup = PROJECT_ROOT / "evals" / "history" / "promote" / f"{draft.name}_{datetime.now():%Y%m%d_%H%M%S}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(target, backup)
        shutil.rmtree(target)
    shutil.copytree(draft, target, ignore=copy_ignore_for(draft))
    # 回帰テスト用のケースも、そのインスタンス専用の置き場へ（共有の evals/cases/ には混ぜない）。
    cases_dest = dest_root.parent / "evals" / draft.name
    cases_dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for case in sorted((draft / DRAFT_EVALS_DIRNAME).glob("*.yaml")):
        shutil.copy2(case, cases_dest / case.name)
        copied.append(case.name)
    meta.update({"status": "promoted", "promoted_at": _now(), "promoted_to": str(target)})
    _write_meta(draft, meta)
    owner = draft.parent.name
    skill_sha = tree_sha256(draft)
    from admin.skill_lab.promotion import archive_draft

    archived = archive_draft(draft, Path(cfg["archive_dir"]), extra={"promoted_to": str(target)})
    registered_text = "".join(
        f"- 設定登録（{r['instance']}）: {json.dumps(r['added'], ensure_ascii=False)}\n" for r in registrations if r["added"]
    )
    entry = (
        f"\n## {_now()} {draft.name}（{meta.get('kind')}、作成者 {owner}、インスタンス {args.instance}）\n\n"
        f"- 昇格先: {target}\n- ケース: {', '.join(copied)} → {cases_dest}\n"
        f"- トライアウト: 各{tryout.get('repeat')}回、判定 {tryout.get('verdict')}{'（judge 判定で全回合格）' if args.judged_pass else ''}"
        f"、スキル内容 sha256 {skill_sha[:12]}\n"
        + registered_text
        + (f"- 置き換え前のバックアップ: {backup}\n" if backup else "")
        + f"- アーカイブしたドラフト: {archived}\n"
        + (f"- メモ: {args.note}\n" if args.note else "")
    )
    if not PROMOTION_LOG.exists():
        PROMOTION_LOG.write_text("# スキル昇格ログ\n\npromote-skill スキルで正式化したドラフトの記録。\n", encoding="utf-8")
    with PROMOTION_LOG.open("a", encoding="utf-8") as f:
        f.write(entry)
    print(
        json.dumps(
            {
                "installed": str(target),
                "cases": copied,
                "backup": str(backup) if backup else None,
                "archived_draft": str(archived),
                "registrations": registrations,
                "instances_using_dest": _instances_using([dest_root]),
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_return(args) -> int:
    draft = Path(args.draft).resolve()
    meta = _read_meta(draft)
    meta.update({"status": "rejected" if args.reject else "returned", "returned_reason": args.reason, "returned_at": _now()})
    _write_meta(draft, meta)
    print(json.dumps({"draft": str(draft), "status": meta["status"]}, ensure_ascii=False))
    return 0


def main() -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    os.chdir(PROJECT_ROOT)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    p = sub.add_parser("check")
    p.add_argument("draft")
    p.add_argument("--instance", default="default")
    p = sub.add_parser("install")
    p.add_argument("draft")
    p.add_argument("--dest", help="省略可。指定する場合は --instance 専用の置き場（list の dest_root）と同じにする")
    p.add_argument("--instance", default="default")
    p.add_argument("--tryout", required=True, help="evals/run_all.py が書いた tryout.json")
    p.add_argument("--judged-pass", action="store_true", help="needs_judge を judge 判定で全回合格と判断した")
    p.add_argument(
        "--register-instance",
        action="append",
        default=[],
        help="互換のため残している。指定できるのは --instance と同じ名前だけ（設定は --instance にだけ登録する）",
    )
    p.add_argument("--note", default="")
    p = sub.add_parser("return")
    p.add_argument("draft")
    p.add_argument("--reason", required=True)
    p.add_argument("--reject", action="store_true", help="差し戻しではなく不採用にする")
    args = parser.parse_args()
    return {"list": cmd_list, "check": cmd_check, "install": cmd_install, "return": cmd_return}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
