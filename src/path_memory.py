"""パスメモリー（ファイルパスの短縮参照）レジストリの読み書き。

Read/Glob/Grep 等のツールが返す長い絶対パスに、短い数値インデックス（@N）を
自動で割り当てて記録し、以降のツール呼び出しでは @N を渡せば実パスに解決
できるようにする。ローカルLLMが長いパス文字列を複数回のツール呼び出しに
またがって正確に再生成できずタイプミスを頻発させる問題への対策。

src/tools.py が `from . import path_memory` で直接importし、Read/Glob/Grep/
json_query/search_path_memory/analyze_image/run_script の @N 登録・解決に使う
（旧 skills/path-memory/scripts/_registry.py。ISSUE-003 で SKILL.md を持つ
Agent Skill としての公開をやめ、アプリ基盤側の内部実装モジュールとして
src/ へ移設した）。動作パラメータ（thread_id・レジストリ保存先・登録上限）は
関数の引数として明示的に渡す方式に統一し、環境変数の読み取りは run_script
経由でサブプロセスとして起動される他スキルのスクリプトが自己登録したい
場合のための env_params() にのみ閉じる。
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import msvcrt
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"^@(\d+)$")
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_INTERVAL_SECONDS = 0.05
# レジストリの読み直し・差し替えの再試行（_read_entries / _save）。
_READ_RETRIES = 20
_REPLACE_RETRIES = 100
_RETRY_INTERVAL_SECONDS = 0.02


@contextlib.contextmanager
def _locked(lock_path: Path):
    """`register()` の read-modify-write をプロセス・タスク間で排他制御する。

    モデルが同一ターンで複数のツール呼び出し（Read/Glob/Grep等の並列実行、
    または `run_script` 経由で別プロセス起動される他スキルのスクリプト）を
    並列に発行すると、複数の呼び出しが同時にレジストリJSONを読み込み・
    追記・保存するため、ロック無しでは後勝ちで前の登録が失われるrace
    conditionが起きる（tune-prompt調査、020/021ケースで「@N が見つかりません」
    として実際に発生）。

    Windows標準の `msvcrt.locking()` のみを使い、pip追加依存を避ける。
    ロック保持プロセスが異常終了してもOSがハンドルクローズ時に自動で
    ロックを解放するため、stale lock（陳腐化したロックファイル）の
    後始末は不要。

    Args:
        lock_path: サイドカーロックファイルのパス（レジストリ本体の
            JSONファイルとは別ファイル）。

    Yields:
        ロックを取得できたら True、`_LOCK_TIMEOUT_SECONDS` 以内に
        取得できなければ False（呼び出し側は書き込みを諦めること）。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+b")
    try:
        # ファイル末尾を越える範囲もロックできるため、サイズ0のままロックする。
        # 1バイト書き込んでから取ると、同時に作った別の呼び出しがロック済みの
        # バイトへ書き込もうとして PermissionError になる（並列の search_documents
        # で実際に発生）。
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        acquired = False
        while True:
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_INTERVAL_SECONDS)
        try:
            yield acquired
        finally:
            if acquired:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        f.close()


def is_path_memory_token(token: str) -> bool:
    """文字列が `@N`（N は1以上の整数）形式のパスメモリー参照かどうかを判定する。"""
    return bool(_TOKEN_RE.match(token))


def _registry_path(thread_id: str, path_memory_dir: Path) -> Path:
    return path_memory_dir / f"{thread_id}.json"


class _RegistryUnreadable(Exception):
    """レジストリファイルは在るが、読み直しても読めない（壊れている）。"""


def _read_entries(registry_path: Path) -> list[dict]:
    """レジストリを読む。無ければ空。一時的に読めない場合は少し待って読み直す。

    書き込みは _save() が一時ファイルからの差し替えで行うため、途中まで書かれた
    JSON を読むことは無いが、Windows では差し替えの瞬間に開くとアクセス拒否に
    なることがある。以前はここで空リストを返していたため、並列のツール呼び出しで
    登録済みの `@N` が「未登録」と判定された（3000回中119回の実測）。

    Raises:
        _RegistryUnreadable: 読み直しても読めない（ファイルが壊れている）場合。
    """
    for attempt in range(_READ_RETRIES):
        if not registry_path.is_file():
            return []
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            if attempt + 1 < _READ_RETRIES:
                time.sleep(_RETRY_INTERVAL_SECONDS)
            continue
        if not isinstance(data, list):
            break
        return [e for e in data if isinstance(e, dict)]
    raise _RegistryUnreadable(str(registry_path))


def _load(registry_path: Path) -> list[dict]:
    """読み取り専用の呼び出し（resolve・一覧・検索）用。壊れていれば空として扱う。"""
    try:
        return _read_entries(registry_path)
    except _RegistryUnreadable:
        logger.warning("Path memory registry is unreadable: %s", registry_path)
        return []


def _load_for_update(registry_path: Path) -> list[dict]:
    """ロック中の書き換え用。壊れたファイルは退避してから空で始める。

    壊れたまま空として上書きすると、元の内容を調べる手掛かりまで失われるため
    `<名前>.corrupt-<時刻>` へ残す（番号は振り直しになる）。
    """
    try:
        return _read_entries(registry_path)
    except _RegistryUnreadable:
        backup = registry_path.with_name(f"{registry_path.name}.corrupt-{time.strftime('%Y%m%d%H%M%S')}")
        logger.warning("Path memory registry is broken; moved it to %s and started a new one", backup)
        with contextlib.suppress(OSError):
            os.replace(registry_path, backup)
        return []


def _save(registry_path: Path, entries: list[dict]) -> None:
    """一時ファイルへ書いてから差し替える（読み込み側が書きかけの JSON を見ないように）。"""
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = registry_path.with_name(f"{registry_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    for _ in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, registry_path)
            return
        except PermissionError:
            # Windows では読み込み中のファイルへは差し替えられないため、読み終わるのを待つ。
            time.sleep(_RETRY_INTERVAL_SECONDS)
    try:
        os.replace(tmp, registry_path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def register(
    thread_id: str,
    path: str,
    path_memory_dir: Path,
    max_entries: int,
    description: str | None = None,
) -> int | None:
    """パスをレジストリへ登録し、1始まりのインデックスを返す。

    Args:
        thread_id: 登録先の会話を識別する文字列。
        path: 登録する絶対パス文字列。
        path_memory_dir: レジストリファイルの保存先ディレクトリ。
        max_entries: 1会話あたりの登録上限件数。
        description: このパスに添える短い説明（例: "execute_python_codeが
            新規作成"）。省略時（None）は付与しない。

    Returns:
        登録（または既存エントリの再利用）に成功すればそのインデックス
        （1始まり）。上限に達していて新規登録できない場合、または
        ロック取得がタイムアウトした場合は None。

    Notes:
        同一パスが既に登録済みの場合は新規追加せず、そのインデックスを
        再利用しつつ `valid`（ファイルが実在するか）を最新の状態へ更新する。
        この場合 `description` を渡していれば既存エントリの説明も
        上書きする（省略時は既存の説明を保持する）。
        読み込みから書き込みまでをファイルロックで排他制御しており、
        並列呼び出し時のrace conditionによる登録消失を防ぐ（`_locked`
        docstring参照）。
    """
    return register_many(thread_id, [path], path_memory_dir, max_entries, description=description)[0]


def register_many(
    thread_id: str,
    paths: list[str],
    path_memory_dir: Path,
    max_entries: int,
    description: str | None = None,
) -> list[int | None]:
    """複数のパスを1回のロック・読み込み・書き込みでまとめて登録する。

    各パスの扱いは register() と同じ（登録済みなら再利用して valid・説明を更新、
    未登録なら上限まで追加）。register() を1件ずつ呼ぶと、パスごとにレジストリ
    全体（最大 max_entries 件）の読み込みと書き込みが起き、Glob/Grep の結果
    数百件の登録だけで数十秒かかるため、こちらを使う。

    Returns:
        paths と同じ順・同じ件数のインデックス（1始まり）の一覧。上限に達して
        登録できなかったパスは None。ロック取得がタイムアウトしたら全て None。
    """
    if not paths:
        return []
    # 実在確認はロックの外で済ませる。UNC パス数百件ではロック中に数秒〜数十秒かかり、
    # その間に来た別の登録がロック待ち（_LOCK_TIMEOUT_SECONDS）で諦めて None になり、
    # パスに `@N` が付かないまま黙って進んでいたため。
    valid_by_path = {path: _exists(path) is True for path in dict.fromkeys(paths)}
    registry_path = _registry_path(thread_id, path_memory_dir)
    lock_path = registry_path.parent / f"{registry_path.name}.lock"
    with _locked(lock_path) as acquired:
        if not acquired:
            return [None] * len(paths)
        entries = _load_for_update(registry_path)
        index_by_path = {entry.get("path"): i for i, entry in enumerate(entries)}
        indexes: list[int | None] = []
        changed = False
        for path in paths:
            valid = valid_by_path[path]
            i = index_by_path.get(path)
            if i is not None:
                if entries[i].get("valid") != valid:
                    entries[i]["valid"] = valid
                    changed = True
                if description is not None and entries[i].get("description") != description:
                    entries[i]["description"] = description
                    changed = True
                indexes.append(i + 1)
                continue
            if len(entries) >= max_entries:
                indexes.append(None)
                continue
            entries.append({"path": path, "valid": valid, "description": description})
            index_by_path[path] = len(entries) - 1
            indexes.append(len(entries))
            changed = True
        # 何も変わらない再登録（同じファイルを読み直しただけ等）では書き直さない。
        if changed:
            _save(registry_path, entries)
        return indexes


def resolve(thread_id: str, token: str, path_memory_dir: Path) -> str | None:
    """`@N` 形式のトークンを実パスへ解決する。

    Args:
        thread_id: 会話を識別する文字列。
        token: 解決したい文字列（`@N` 形式でなければ即 None）。
        path_memory_dir: レジストリファイルの保存先ディレクトリ。

    Returns:
        該当インデックスのパス。トークンが `@N` 形式でない、または該当する
        登録が無い場合は None。
    """
    m = _TOKEN_RE.match(token)
    if not m:
        return None
    index = int(m.group(1))
    entries = _load(_registry_path(thread_id, path_memory_dir))
    if 1 <= index <= len(entries):
        return entries[index - 1].get("path")
    return None


def list_entries(thread_id: str, path_memory_dir: Path) -> list[dict]:
    """登録済み全件を返す。

    Args:
        thread_id: 会話を識別する文字列。
        path_memory_dir: レジストリファイルの保存先ディレクトリ。

    Returns:
        `[{"index": int, "path": str, "valid": bool, "description": str | None}, ...]`
        （登録順）。
    """
    entries = _load(_registry_path(thread_id, path_memory_dir))
    return [
        {
            "index": i + 1,
            "path": e.get("path"),
            "valid": bool(e.get("valid", False)),
            "description": e.get("description"),
        }
        for i, e in enumerate(entries)
    ]


def _normalize(s: str) -> str:
    """類似度比較用にパス文字列を正規化する（全角半角・大文字小文字・区切り文字の揺れを吸収）。"""
    s = unicodedata.normalize("NFKC", s).casefold().replace("\\", "/")
    return s.rstrip("/")


def _bigrams(s: str) -> Counter[str]:
    """正規化済み文字列を文字2-gramの出現頻度に変換する（1文字なら1-gram）。

    Python の str は Unicode 単位で扱うため、2バイト文字も1文字として分解される。
    文字コード値をそのままベクトル成分にする方式と違い、1文字の挿入・削除で
    以降の全成分がずれることがなく、部分一致や表記ゆれにも反応する。
    """
    if len(s) < 2:
        return Counter([s]) if s else Counter()
    return Counter(s[i : i + 2] for i in range(len(s) - 1))


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(count * b[gram] for gram, count in a.items() if gram in b)
    if dot == 0:
        return 0.0
    norm_a = math.sqrt(sum(c * c for c in a.values()))
    norm_b = math.sqrt(sum(c * c for c in b.values()))
    return dot / (norm_a * norm_b)


def bigram_vector(text: str) -> Counter[str]:
    """任意の文字列を正規化して文字2-gramの出現頻度にする（パス以外の類似度検索用。admin/assistant_index.py）。"""
    return _bigrams(_normalize(text))


def cosine_similarity(a: Counter[str], b: Counter[str]) -> float:
    """bigram_vector() 同士（または各成分に重みを掛けたもの）のコサイン類似度（0.0〜1.0）。"""
    return _cosine(a, b)


def _split(normalized: str) -> tuple[str, str]:
    """正規化済みパスを (フォルダ部分, ファイル名部分) に分ける（区切りが無ければフォルダ部分は空）。"""
    folder, _, name = normalized.rpartition("/")
    return folder, name


def _strip_query(query: str) -> str:
    """検索語の前後の空白と、LLM が付けがちな引用符・バッククォートを除く。"""
    return query.strip().strip("\"'`").strip()


def _is_ascii_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _contains_at_boundary(text: str, q: str) -> bool:
    """q が text の部分文字列で、英数字の語の途中から始まったり途中で終わったりしないか。

    `work` が `homework` の途中に当たる、`c` がドライブ名 `c:` に当たる、といった一致を
    部分一致（0.8 以上）から外すための判定。日本語等は語の区切りが無いため、隣が ASCII
    英数字でなければ区切りとみなす（`報告` は `月次報告書` に部分一致する）。
    """
    start = text.find(q)
    while start != -1:
        end = start + len(q)
        before_ok = start == 0 or not (_is_ascii_alnum(text[start - 1]) and _is_ascii_alnum(q[0]))
        after_ok = end == len(text) or not (_is_ascii_alnum(text[end]) and _is_ascii_alnum(q[-1]))
        if before_ok and after_ok:
            return True
        start = text.find(q, start + 1)
    return False


def similarity(query: str, path: str, filename_weight: float = 0.7) -> float:
    """検索語と登録パスの類似度（0.0〜1.0）を返す。

    - 完全一致（フルパスまたはファイル名）は 1.0。
    - 部分文字列として含まれる場合は 0.8〜1.0 未満（検索語がファイル名・パスに
      占める割合が大きいほど高い）。`report.xlsx` で `old_report.xlsx` より
      `report.xlsx` 本体を上に並べるため、完全一致とは区別する。ただし英数字の語の
      途中に当たる一致（`work` と `homework`）と ASCII 1文字の検索語は部分一致とせず、
      下の2-gram の類似度で比べる（`_contains_at_boundary` 参照）。
    - 検索語の前後の空白・引用符は無視する。
    - 検索語がフルパスなら「ファイル名同士×filename_weight + フォルダ同士×残り」。
      フルパス全体で比べると、同じフォルダの無関係なファイルが共通の長い
      フォルダ部分だけで高得点になり、別フォルダの同名ファイルより上に来てしまう。
    - 検索語がファイル名・フォルダ名だけなら、ファイル名との類似度と
      フォルダ名の各要素との類似度（ファイル名一致を優先するため 0.9 倍）の大きい方。

    Args:
        query: 検索語。
        path: 登録パス。
        filename_weight: 検索語がフルパスの場合に、ファイル名同士の類似度に掛ける
            重み（残りはフォルダ部分同士の類似度）。
    """
    q = _normalize(_strip_query(query))
    p = _normalize(path)
    if not q or not p:
        return 0.0
    p_folder, p_name = _split(p)
    if q in (p, p_name):
        return 1.0
    # ASCII 1文字（`a`・`c`・`.`）はほぼ全パスに含まれ雑音になるため部分一致にしない。
    # 漢字1文字（`表`）は検索語として意味を持つため対象に残す。
    if (len(q) >= 2 or not q.isascii()) and _contains_at_boundary(p, q):
        return 0.8 + 0.2 * len(q) / len(p_name if _contains_at_boundary(p_name, q) else p)
    q_folder, q_name = _split(q)
    q_grams = _bigrams(q_name)
    name_score = _cosine(q_grams, _bigrams(p_name))
    if q_folder:
        folder_score = _cosine(_bigrams(q_folder), _bigrams(p_folder))
        return filename_weight * name_score + (1 - filename_weight) * folder_score
    segment_score = max((_cosine(q_grams, _bigrams(s)) for s in p_folder.split("/") if s), default=0.0)
    return max(name_score, segment_score * 0.9)


def difference_label(query: str, path: str) -> str:
    """検索語（見つからなかったパス）と候補パスの違いを短い説明にして返す。

    エラー時の候補提示で、LLM が候補を指定パスの言い換えだと誤解して
    そのまま置き換えないよう、何が違うかを明示するために使う。
    """
    q_folder, q_name = _split(_normalize(_strip_query(query)))
    p_folder, p_name = _split(_normalize(path))
    folder_differs = bool(q_folder) and q_folder != p_folder
    name_differs = q_name != p_name
    if folder_differs and name_differs:
        return "different folder and file name"
    if folder_differs:
        return "different folder"
    return "different file name"


def _exists(path: str | None) -> bool | None:
    """パスが実在するか。空パス・アクセス拒否等で判定できなければ None。"""
    if not path:
        return None
    try:
        return Path(path).exists()
    except (OSError, ValueError):
        return None


def _take_existing(thread_id: str, path_memory_dir: Path, candidates: list[dict], top_k: int) -> list[dict]:
    """candidates を先頭から実在確認し、実在するものだけを top_k 件まで返す。

    登録時点の `valid` は古い可能性があるため、ここで改めて確認する。確認は
    必要な件数がそろった時点で打ち切る（登録上限は数千件規模で、UNCパスの
    存在確認は1件ずつ遅いことがあるため）。確認結果が登録内容と違えば
    レジストリの `valid` を更新する。存在しない登録を削除しないのは、
    削除すると以降の `@N` の番号が繰り上がり、会話履歴中の `@N` が別の
    ファイルを指してしまうため。
    """
    result: list[dict] = []
    changes: list[tuple[int, str, bool]] = []
    for entry in candidates:
        if len(result) >= top_k:
            break
        exists = _exists(entry["path"])
        if exists is None:
            continue
        if exists != entry["valid"]:
            changes.append((entry["index"], entry["path"], exists))
        if exists:
            result.append({**entry, "valid": True})
    if changes:
        _update_valid(thread_id, path_memory_dir, changes)
    return result


def _update_valid(thread_id: str, path_memory_dir: Path, changes: list[tuple[int, str, bool]]) -> None:
    """(index, path, valid) の組で登録の `valid` を書き換える（path が一致する場合のみ）。"""
    registry_path = _registry_path(thread_id, path_memory_dir)
    lock_path = registry_path.parent / f"{registry_path.name}.lock"
    with _locked(lock_path) as acquired:
        if not acquired:
            return
        entries = _load_for_update(registry_path)
        updated = False
        for index, path, valid in changes:
            if 1 <= index <= len(entries) and entries[index - 1].get("path") == path:
                entries[index - 1]["valid"] = valid
                updated = True
        if updated:
            _save(registry_path, entries)


def search_entries(
    thread_id: str,
    query: str,
    path_memory_dir: Path,
    top_k: int = 5,
    min_score: float = 0.3,
    filename_weight: float = 0.7,
) -> list[dict]:
    """登録済みパスのうち実在するものを、検索語との類似度順に返す。

    Args:
        thread_id: 会話を識別する文字列。
        query: 検索語（ファイル名・フォルダ名の一部やフルパス）。
        path_memory_dir: レジストリファイルの保存先ディレクトリ。
        top_k: 返す最大件数。
        min_score: この類似度未満の登録は返さない。
        filename_weight: `similarity()` の同名引数。

    Returns:
        `list_entries()` の各要素に `score`（小数2桁）を加えたもの（類似度の降順）。
        現在存在しないパスは含まない（`_take_existing` 参照）。
    """
    scored = []
    for entry in list_entries(thread_id, path_memory_dir):
        score = similarity(query, entry["path"] or "", filename_weight)
        if score >= min_score:
            scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    candidates = [{**entry, "score": round(score, 2)} for score, entry in scored]
    return _take_existing(thread_id, path_memory_dir, candidates, top_k)


def recent_entries(thread_id: str, path_memory_dir: Path, top_k: int = 5) -> list[dict]:
    """登録済みパスのうち実在するものを、新しい順に top_k 件返す。"""
    return _take_existing(thread_id, path_memory_dir, list_entries(thread_id, path_memory_dir)[::-1], top_k)


def exec_tmp_dir(category: str | None = None) -> Path:
    """execute_python_code の中間生成物と同じ `_tmp_<thread_id>/` を返す（無ければ作成する）。

    run_script 経由でサブプロセス起動される他スキルのスクリプトが、自分専用の
    中間生成物置き場を作りたい場合に使う。基準は run_script の cwd
    （_resolve_workdir() の結果である作業ディレクトリ）ではなく、常に
    default_workdir（AGENT_DEFAULT_WORKDIR）にする。cwd はユーザーが
    ChatSettings で指定した work_dir になりうるが、work_dir は保持日数ベースの
    自動削除（default_workdir の retention_days）の対象外のため、cwd を基準に
    すると中間生成物が消えずに溜まり続ける事故につながる（過去に実際に
    発生。cwd基準はバグであり仕様ではない）。ディレクトリ名は
    AGENT_EXEC_TMP_NAME（`_exec_tmp_name()` が生成する作成時刻プレフィックス
    付きthread_id。メインプロセスの `_resolve_exec_workdir()` と同じ名前）を
    読み、未設定時は AGENT_THREAD_ID（env_params() と同じ、生のthread_id）
    へフォールバックし、どちらも無ければ "_no_session" にフォールバックする。

    Args:
        category: `_tmp_<name>` 直下にさらに切るサブディレクトリ名
            （例: "pdf_rendered"）。省略時は `_tmp_<name>` 自体を返す。

    Returns:
        作成済みの絶対パス。
    """
    name = os.environ.get("AGENT_EXEC_TMP_NAME") or os.environ.get("AGENT_THREAD_ID") or "_no_session"
    base = Path(os.environ.get("AGENT_DEFAULT_WORKDIR") or "./data/temp")
    out_dir = base / f"_tmp_{name}"
    if category:
        out_dir = out_dir / category
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def env_params() -> tuple[str, Path, int]:
    """環境変数から (thread_id, path_memory_dir, max_entries) を読む。

    run_script 経由でサブプロセスとして起動される他スキルのスクリプトが、
    自身の出力パスを自己登録したい場合に使う（src/path_memory.py を
    sys.path 経由でimportし、この関数でパラメータを取得する）。
    src/tools.py からの直接import（同一プロセス内呼び出し）はこの関数を
    使わず、パラメータを直接引数で渡す。

    Returns:
        (thread_id, path_memory_dir, max_entries) のタプル。環境変数が
        未設定の場合はそれぞれ "_no_session" / "./data/path_memory" / 500
        にフォールバックする。
    """
    thread_id = os.environ.get("AGENT_THREAD_ID") or "_no_session"
    path_memory_dir = Path(os.environ.get("AGENT_PATH_MEMORY_DIR") or "./data/path_memory")
    max_entries = int(os.environ.get("AGENT_PATH_MEMORY_MAX_ENTRIES") or "500")
    return thread_id, path_memory_dir, max_entries
