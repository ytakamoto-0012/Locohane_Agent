"""eval ケース（yaml）の読み込みと検証。

ケース1件 = 1つの会話スレッドに対する期待動作の定義。判定は2種類:
- expect: ツール呼び出しの有無・引数・最終回答文字列に対するルールベース判定
  （run_case.py がその場で pass/fail を出す）。
- judge: 自由記述の判定基準。ClaudeCode が transcript を読んで合否判定する
  （run_case.py 側では判定せず、指示文をそのまま結果 JSON に含めて返す）。
両方を併用してもよいし、どちらか一方だけでもよい（両方無いケースは無効）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Expect:
    """ルールベース判定の期待値。すべて省略可（空リスト/空dictなら該当ルールをスキップ）。

    Attributes:
        tool_called_any: このいずれかのツール名が1回以上呼ばれていれば合格。
        tool_not_called: これらのツール名が1回も呼ばれていなければ合格。
        tool_call_args_contains: {ツール名: {引数キー: 期待値}}。該当ツールの
            いずれかの呼び出しで、指定した引数キーがすべて期待値と一致すれば合格。
        response_contains: 最終回答にすべて含まれていれば合格。
        response_not_contains: 最終回答にどれも含まれていなければ合格。
    """

    tool_called_any: list[str] = field(default_factory=list)
    tool_not_called: list[str] = field(default_factory=list)
    tool_call_args_contains: dict[str, dict] = field(default_factory=dict)
    response_contains: list[str] = field(default_factory=list)
    response_not_contains: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvalCase:
    """1つの eval ケース。load_case() によってのみ構築される。

    Attributes:
        id: ケースの一意な識別子（yaml の id）。
        target: チューニング対象カテゴリ（例: "system_prompt"）。
            evals/cases/<target>/ のディレクトリ名と一致させる運用。
        turns: 1スレッドの中でユーザーが順に送るメッセージ本文のリスト。
        expect: ルールベース判定の期待値。未指定なら None。
        judge: ClaudeCode が transcript を読んで判定するための自由記述の指示文。
            未指定なら None。
        auto_approve: run_script/execute_python_code/approve_plan の
            承認ダイアログを自動承認するか拒否するか
            （evals/headless_chainlit.install に渡す）。
        scripted_text_answers: AskUserQuestion が labels 省略（単一質問）で
            呼ばれるたびに1件ずつ消費して返す回答のリスト。
        work_dir: run_script/execute_python_code/view_image が使う作業
            ディレクトリ（_resolve_workdir）をこのケース専用に固定したい
            場合のプロジェクトルート相対パス（例:
            "evals/fixtures/annual_schedule"）。未指定なら config.ini の
            [default_workdir].dir をそのまま使う。
        env: run_case.py がケース実行前に os.environ へ設定する追加の
            環境変数（{"KEY": "value"} の形式）。config.ini の値を
            ${変数名} で参照する設定（expand_config_vars）や
            src/config.py が読む環境変数を、ケースごとに上書きしたい
            場合に使う。未指定なら何も設定しない。
        memory_seed: ケース開始時に使い捨てのメモリーディレクトリへ写すメモリー一式
            （<type>/<name>.md を並べたフォルダ。work_dir と同じくケースのファイルからの
            相対パス、無ければプロジェクトルート相対）。過去の会話で記録したメモリーが
            ある状態から始めるケース用。未指定なら空のメモリーから始める。
        timeout_seconds: run_all.py がこのケースをサブプロセス実行する際の
            タイムアウト秒数。未指定なら run_all.py の既定値
            （CASE_TIMEOUT_SECONDS）を使う。大量ファイルを扱う重量級ケース
            など、既定値では完走できないケース専用の上書き。
        notes: 人間向けの補足メモ（判定には使わない）。
        source_path: 読み込み元の yaml ファイルパス。
    """

    id: str
    target: str
    turns: list[str]
    expect: Expect | None
    judge: str | None
    auto_approve: bool
    scripted_text_answers: list[str]
    work_dir: str | None
    env: dict[str, str]
    timeout_seconds: int | None
    notes: str
    source_path: Path
    memory_seed: str | None = None


def load_case(path: Path) -> EvalCase:
    """yaml ファイルを読み込み、検証した上で EvalCase を返す。

    Args:
        path: eval ケースの yaml ファイルパス。

    Returns:
        検証済みの EvalCase。

    Raises:
        ValueError: yaml がオブジェクトでない、id/target/turns が無い、
            expect と judge のどちらも無い、のいずれか。
        FileNotFoundError: path が存在しない場合。
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: yaml のトップレベルがオブジェクトではありません")

    case_id = data.get("id")
    if not case_id or not isinstance(case_id, str):
        raise ValueError(f"{path}: id（文字列）が必須です")

    target = data.get("target")
    if not target or not isinstance(target, str):
        raise ValueError(f"{path}: target（文字列）が必須です")

    turns = data.get("turns")
    if not turns or not isinstance(turns, list):
        raise ValueError(f"{path}: turns は1件以上の文字列リストが必須です")

    expect_raw = data.get("expect")
    expect: Expect | None = None
    if expect_raw:
        if not isinstance(expect_raw, dict):
            raise ValueError(f"{path}: expect はオブジェクトである必要があります")
        expect = Expect(
            tool_called_any=list(expect_raw.get("tool_called_any", []) or []),
            tool_not_called=list(expect_raw.get("tool_not_called", []) or []),
            tool_call_args_contains=dict(expect_raw.get("tool_call_args_contains", {}) or {}),
            response_contains=list(expect_raw.get("response_contains", []) or []),
            response_not_contains=list(expect_raw.get("response_not_contains", []) or []),
        )

    judge = data.get("judge")
    if judge is not None and not isinstance(judge, str):
        raise ValueError(f"{path}: judge は文字列である必要があります")

    if expect is None and not judge:
        raise ValueError(f"{path}: expect と judge のどちらも無いケースは無効です")

    env_raw = data.get("env")
    if env_raw is not None and not isinstance(env_raw, dict):
        raise ValueError(f"{path}: env はオブジェクトである必要があります")
    env = {str(k): str(v) for k, v in (env_raw or {}).items()}

    return EvalCase(
        id=case_id,
        target=target,
        turns=[str(t) for t in turns],
        expect=expect,
        judge=judge,
        auto_approve=bool(data.get("auto_approve", True)),
        scripted_text_answers=[str(a) for a in (data.get("scripted_text_answers", []) or [])],
        work_dir=str(data["work_dir"]) if data.get("work_dir") else None,
        env=env,
        timeout_seconds=int(data["timeout_seconds"]) if data.get("timeout_seconds") else None,
        notes=str(data.get("notes", "")),
        source_path=path,
        memory_seed=str(data["memory_seed"]) if data.get("memory_seed") else None,
    )
