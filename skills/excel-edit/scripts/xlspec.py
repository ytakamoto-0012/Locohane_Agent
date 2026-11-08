"""excel-edit スキルの補助モジュール。表にする記録とルールの点検を、作り手（worker）と検証役（verifier）で共有する。

記録・ルールを文章で受け渡し、作り手と検証役がそれぞれ別にコードへ直していたため、曖昧な箇所で
解釈が分かれて検証 NG と修正の往復になっていた（2026-11-07 evals xlkit 9回: 最初の検証の NG の
多くが、月をまたぐ週の数え方・推定/確定のラベルなどの解釈違いと、記録の写し漏れ）。
ここでは次の3つを1か所にする:

- 記録: スレッドノートの ```records ブロック（1行1件の JSON）を `records()` で読む（手で写さない）。
  読めない行は飛ばして報告し、1行に複数の JSON があれば分け、全く同じ行は1件にまとめる
  （不正な1行で全体がエラーになり、worker がノートファイルを手で読み直して膨らぶ実測がある）。
- ルール: 作り手が `rules.py` に関数として1回だけ書き、build スクリプトもチェックも import する。
- 合格基準: `checks.py` に要件1つにつき1つの `@check` を書き、`run_checks()` で両者が同じ判定をする。

使い方（SKILL.md 参照）:

    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.environ["AGENT_SRC_DIR"]), "skills", "excel-edit", "scripts"))
    from xlspec import records, check, run_checks
"""

from __future__ import annotations

import json
import re
import runpy
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

__all__ = ["Book", "check", "records", "run_checks"]

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent
_EXCEL_READ_SCRIPTS = _SKILLS_DIR / "excel-read" / "scripts"

_NOTE_FILE = "_thread_notes.md"
_TOPIC_RE = re.compile(r"^## (.+)\n<!-- (.+?) by (.+?) -->\n", re.MULTILINE)
_RECORDS_RE = re.compile(r"```records[ \t]*\n(.*?)\n```", re.DOTALL)
# 1つのチェックが返す問題のうち、結果に載せる数（多すぎると読み飛ばされるため）
_MAX_PROBLEMS = 10
# 飛ばした行の報告で示す件数（本番で1回の調査で数十行不正だったため、全件ではなく先頭を出す）
_MAX_SKIPPED_REPORTED = 10
_JSON_DECODER = json.JSONDecoder()


def _notes_text(notes_path) -> str:
    path = Path(notes_path) if notes_path else Path.cwd() / _NOTE_FILE
    if not path.is_file():
        raise FileNotFoundError(
            f"No thread notes at {path}. Run the script with execute_python_code (its folder holds {_NOTE_FILE}), "
            "or pass notes_path="
        )
    return path.read_text(encoding="utf-8", errors="replace")


def _record_values_on_line(line: str) -> list[dict]:
    """1行から JSON オブジェクトを読む（続けて2つ書かれていれば分けて返す）。

    Returns:
        その行の JSON オブジェクト（通常1件、同じ行に複数の記録が並ぶ書かれ方をしたときは複数）。

    Raises:
        ValueError: JSON として読めない、または JSON オブジェクトではない。
    """
    text = line.strip().rstrip(",")
    values: list[dict] = []
    pos = 0
    while pos < len(text):
        value, end = _JSON_DECODER.raw_decode(text, pos)
        if not isinstance(value, dict):
            raise ValueError(f"not a JSON object: {text[pos:pos + 120]}")
        values.append(value)
        pos = end
        while pos < len(text) and text[pos] in " ,\t":
            pos += 1
    return values


def records(topic: str | None = None, *, notes_path=None) -> list[dict]:
    """スレッドノートの ```records ブロックの記録を、書かれた順に返す。

    Args:
        topic: このトピック（完全一致）のブロックだけを読む。省略するとすべてのトピックから集める
            （年ごとの調査が別々のトピックに書いた記録をまとめて読むとき）。
        notes_path: スレッドノートのファイル。省略すると今のフォルダの `_thread_notes.md`
            （execute_python_code の実行フォルダ）。

    Returns:
        各行の JSON オブジェクトに、書かれたトピック名 `_topic` を足した dict の一覧。
        書き手が書かなかったキーは存在しない（キーの有無で「記載なし」を区別するため、
        値を読むときは `r.get(key)` を使う）。

    Notes:
        読めない行は例外にするのではなく飛ばして続けます（1行の誤りで全体が読めないと、
        呼び出し側がノートファイルを手で読み直して往復を増やす実測がある）。1行に複数の
        JSON が並ぶ書き方でも読め、全く同じ行は1件にまとめます。飛ばした・まとめた行が
        あるときだけ、標準出力に1行の JSON で報告します（件数の違いを最終回答へ写すため）。

    Raises:
        ValueError: 1件も読めなかった場合（どのトピック・どの行が読めなかったかを示す）。
    """
    text = _notes_text(notes_path)
    matches = list(_TOPIC_RE.finditer(text))
    found: list[dict] = []
    topics_seen: list[str] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    merged = 0
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        topics_seen.append(name)
        if topic is not None and name != topic.strip():
            continue
        body = text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        for block in _RECORDS_RE.finditer(body):
            for line_no, line in enumerate(block.group(1).splitlines(), start=1):
                if not line.strip() or line.strip().startswith("//"):
                    continue
                try:
                    values = _record_values_on_line(line)
                except ValueError as e:
                    skipped.append({"topic": name, "line": line_no, "problem": str(e), "text": line.strip()[:120]})
                    continue
                for value in values:
                    key = json.dumps(value, sort_keys=True, ensure_ascii=False)
                    if key in seen:
                        merged += 1
                        continue
                    seen.add(key)
                    found.append({**value, "_topic": name})
    if not found:
        where = f'topic "{topic}"' if topic else "any topic"
        detail = f" (skipped lines: {len(skipped)}, first: {skipped[0]})" if skipped else ""
        raise ValueError(
            f"No ```records block in {where} of the thread notes (topics: {', '.join(dict.fromkeys(topics_seen)) or 'none'}). "
            "Ask the delegator for the records, or read the facts from the topic text" + detail
        )
    if skipped or merged:
        report: dict = {"records": len(found), "skipped_lines": len(skipped), "duplicate_lines_merged": merged}
        if skipped:
            report["skipped"] = skipped[:_MAX_SKIPPED_REPORTED]
        print(json.dumps(report, ensure_ascii=False))
    return found


_CHECKS: list[tuple[str, Callable]] = []


def check(name: str):
    """合格基準1つを登録するデコレーター（checks.py で使う）。

    関数は `book`（Book）を受け取り、問題の一覧（文字列のリスト。空なら合格）か bool を返す。
    問題には、どのシート・どのセルか、期待値と実際の値を書く（例: "週間 B7: expected 2026/12, got 2027/1"）。
    """

    def register(func: Callable) -> Callable:
        _CHECKS.append((name, func))
        return func

    return register


class Book:
    """チェックに渡すブック。表は結合セルの値を埋めた dict の行で読める。"""

    def __init__(self, path):
        from openpyxl import load_workbook

        self.path = Path(path)
        self.wb = load_workbook(self.path)
        self._tables: dict[tuple, list[dict]] = {}

    def table(self, sheet, header_row: int | None = None) -> list[dict]:
        """excel-read の read_table と同じ形（{見出し: 値, "_row", "_fill"}）で表の本体を返す。"""
        key = (sheet, header_row)
        if key not in self._tables:
            if str(_EXCEL_READ_SCRIPTS) not in sys.path:
                sys.path.insert(0, str(_EXCEL_READ_SCRIPTS))
            from xlread import read_table

            self._tables[key] = read_table(self.path, sheet, header_row)
        return self._tables[key]

    def texts(self, sheet) -> list[tuple[str, str]]:
        """シートの文字列セルを (番地, 文字列) で返す（注記・凡例の確認用）。"""
        ws = self.wb[sheet] if isinstance(sheet, str) else self.wb.worksheets[sheet]
        return [(c.coordinate, c.value) for row in ws.iter_rows() for c in row if isinstance(c.value, str)]


def run_checks(xlsx_path, checks_file: str = "checks.py") -> dict:
    """checks.py の @check をすべて実行し、結果を1行の JSON で表示して返す。

    要件を満たさない（問題を返した）チェックは `failed`、チェック自体が例外で落ちたものは `errors`
    として分ける（後者はチェックのコードの誤りで、ファイルの不備とは限らない）。

    Returns:
        {"file", "checks", "passed", "failed": [{"check", "problems", "more"?}], "errors": [{"check", "error"}]}
    """
    _CHECKS.clear()
    checks_path = Path(checks_file).resolve()
    if str(checks_path.parent) not in sys.path:
        sys.path.insert(0, str(checks_path.parent))  # checks.py から rules.py を import できるように
    runpy.run_path(str(checks_path))
    if not _CHECKS:
        raise ValueError(f"{checks_path} defines no @check functions")
    book = Book(xlsx_path)
    passed, failed, errors = [], [], []
    for name, func in _CHECKS:
        try:
            outcome = func(book)
        except Exception as e:  # noqa: BLE001 - チェックのコードの誤りもファイルの不備と分けて報告する
            frame = traceback.extract_tb(e.__traceback__)[-1]
            errors.append({"check": name, "error": f"{type(e).__name__}: {e} (line {frame.lineno}: {frame.line})"})
            continue
        problems = [] if outcome is True or outcome is None else (["failed"] if outcome is False else [str(p) for p in outcome])
        if problems:
            entry = {"check": name, "problems": problems[:_MAX_PROBLEMS]}
            if len(problems) > _MAX_PROBLEMS:
                entry["more"] = len(problems) - _MAX_PROBLEMS
            failed.append(entry)
        else:
            passed.append(name)
    result = {
        "file": str(Path(xlsx_path).resolve()),
        "checks": len(_CHECKS),
        "passed": len(passed),
        "failed": failed,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False))
    return result
