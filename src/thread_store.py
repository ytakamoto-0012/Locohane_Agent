"""会話スレッド一覧（画面左サイドバー）・過去の会話の再開のための軽量な永続化ストア。

`data/checkpoints.sqlite`（LangGraph の会話状態そのもの、AsyncSqliteSaver が管理）とは
別の、もう1つの SQLite ファイル（`data/chat_threads.sqlite`）に、スレッド一覧の表示・
再開に必要な最小限のメタデータ（スレッド名・更新日時・所有者・Chainlit の Step 履歴）
だけを持つ。

役割はここまで（それだけ）:
1. Chainlit の `BaseDataLayer` を実装し、`@cl.data_layer` 経由で登録すると、Chainlit
   本体が全メッセージ/ツール呼び出しステップを `create_step`/`update_step` 経由で
   自動的にここへ書き込む（呼び出し側の on_message 等は変更不要）。
2. スレッド一覧・リネーム・削除用の薄いヘルパー関数群（app.py の独自 FastAPI ルートが使う）。

所有者（スレッドの持ち主）の解決は `src/chat_log.py` の `resolve_log_username` と
完全に揃える（[auth] enabled=false のときは全スレッドを ANONYMOUS_OWNER に一元化する）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from chainlit.data.base import BaseDataLayer
from chainlit.data.utils import queue_until_user_message
from chainlit.types import Feedback, PageInfo, PaginatedResponse, Pagination, ThreadDict, ThreadFilter
from chainlit.user import PersistedUser, User

from . import chat_log

logger = logging.getLogger(__name__)

ANONYMOUS_OWNER = chat_log.ANONYMOUS_USERNAME

# thread_id -> save_thread のメタデータ統合を直列化する Lock（save_thread 参照）。
# 誰も参照しなくなった Lock は GC で自動的に消える（app.py の _chat_log_locks と同じ方式）。
_save_thread_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()

_DDL = """
CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  name TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  tags_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_threads_owner_updated ON threads(owner, updated_at DESC);

CREATE TABLE IF NOT EXISTS steps (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  created_at TEXT,
  data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_thread ON steps(thread_id, created_at);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  identifier TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS elements (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  for_id TEXT,
  kind TEXT NOT NULL DEFAULT 'chainlit',
  file_path TEXT,
  mime TEXT,
  created_at TEXT,
  data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_elements_thread ON elements(thread_id, created_at);
"""


def resolve_owner(user: "User | PersistedUser | None") -> str:
    """cl.User/PersistedUser からスレッド所有者バケット名を解決する。

    src/chat_log.py の resolve_log_username と完全に同じ挙動（未ログイン/
    identifier無しは ANONYMOUS_OWNER に一元化）。会話ログとスレッド一覧の
    「誰の会話として扱うか」を1箇所のロジックに揃えるための薄いラッパー。
    """
    return chat_log.resolve_log_username(user.identifier if user else None)


def _current_owner() -> str:
    """呼び出し時点の Chainlit セッションから所有者を解決する。

    create_step/update_step は StepDict しか受け取らずユーザー情報を持たない
    ため、Chainlit のコンテキスト（chainlit.context.context）から都度解決する。
    asyncio.create_task() は呼び出し時点の contextvars を引き継ぐため、
    Chainlit本体がスケジュールするタスク内からでも到達できる。コンテキスト外
    （テスト等）では ChainlitContextException 等を握りつぶし匿名扱いにする。
    """
    try:
        from chainlit.context import context as cl_context

        user = cl_context.session.user
    except Exception:  # noqa: BLE001 - コンテキスト外は匿名扱いにするだけで十分
        user = None
    return resolve_owner(user)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db(db_path: Path) -> aiosqlite.Connection:
    """スレッドストア用の SQLite 接続を開き、テーブルを（未作成なら）作成して返す。

    app.py の _build_checkpointer() と同じ形。アプリ起動時に1回だけ呼び、
    以後はプロセス寿命中ずっとこの接続を使い回す想定。
    """
    conn = await aiosqlite.connect(str(db_path))
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.executescript(_DDL)
    await conn.commit()
    return conn


async def upsert_thread_stub(conn: aiosqlite.Connection, thread_id: str, owner: str) -> None:
    """スレッド行が無ければ、最小限の内容（owner のみ）で作成する。

    steps.thread_id は threads(id) への外部キーのため、create_step が
    update_thread より先に走った場合でも FK 制約を満たせるように、
    ステップ書き込み前に必ず呼ぶ（既に行があれば何もしない）。
    """
    now = _now()
    await conn.execute(
        "INSERT OR IGNORE INTO threads (id, owner, name, created_at, updated_at, metadata_json) "
        "VALUES (?, ?, NULL, ?, ?, '{}')",
        (thread_id, owner, now, now),
    )
    await conn.commit()


async def save_thread(
    conn: aiosqlite.Connection,
    thread_id: str,
    *,
    owner: str | None = None,
    name: str | None = None,
    metadata: dict | None = None,
    tags: list[str] | None = None,
) -> None:
    """スレッドのメタデータを upsert する（新規作成 or 既存行の統合更新）。

    metadata は既存の値へ dict.update() で統合する（丸ごと置き換えない）。
    on_message 側の毎ターンのスナップショット（work_dir/plan/token使用量）と
    Chainlit本体からの update_thread(name=...) 呼び出しの両方がこの関数を
    経由するため、片方が持つキーをもう片方が消してしまわないようにするため。

    「行の有無をSELECTしてからINSERT/UPDATEを選ぶ」を1つの関数内で完結させると、
    同じ thread_id に対して create_step（upsert_thread_stub）とこの関数が
    ほぼ同時に呼ばれた場合に競合する。process_message()（chainlit本体）は
    最初のユーザーメッセージ受信時、message._create()（→create_step経由で
    upsert_thread_stub）と init_thread()（→flush_thread_queues経由で
    この関数）を asyncio.create_task で同時に2つ起動するため、新規スレッドの
    最初のメッセージでは必ずこの競合が起こりうる。両者のSELECTが互いの
    INSERT前に割り込むと、後からINSERTした側が
    `UNIQUE constraint failed: threads.id` で失敗する（Chainlit本体側の
    flush_thread_queuesはこの例外をログに残すだけで再送しないため、
    スレッド名が永久に「無題の会話」のまま確定しなくなる。
    2026-08-19 ユーザー報告で実際に再現・確認済み）。

    このため「行の存在を保証する」部分を upsert_thread_stub と全く同じ
    `INSERT OR IGNORE`（原子的・競合しても安全）に分離し、続けて
    （この時点で行の存在が保証された状態で）無条件の UPDATE のみで
    統合更新する。UPDATE側にはINSERT分岐が無いためUNIQUE制約に触れない。
    """
    now = _now()
    await conn.execute(
        "INSERT OR IGNORE INTO threads (id, owner, name, created_at, updated_at, metadata_json) "
        "VALUES (?, ?, NULL, ?, ?, '{}')",
        (thread_id, owner or ANONYMOUS_OWNER, now, now),
    )
    await conn.commit()

    # SELECT→dict.update()→UPDATE の read-modify-write は await を挟むため、同じ
    # thread_id へ並行して呼ばれると後勝ちで他方のキーを消す（lost update。
    # 20並行で19キーが消えることを確認済み）。トークン累計を LLM 呼び出しごとに
    # 保存するようにした（app.py _persist_token_usage、2026-10-08）ことで、
    # plan の即時保存等と並行する頻度が上がったため、thread_id 単位で直列化する。
    lock = _save_thread_locks.get(thread_id)
    if lock is None:
        lock = _save_thread_locks[thread_id] = asyncio.Lock()
    async with lock:
        cursor = await conn.execute("SELECT owner, metadata_json, tags_json FROM threads WHERE id = ?", (thread_id,))
        row = await cursor.fetchone()
        existing_owner, existing_metadata_json, existing_tags_json = row
        merged_metadata = json.loads(existing_metadata_json or "{}")
        if metadata:
            merged_metadata.update(metadata)
        new_tags_json = json.dumps(tags, ensure_ascii=False) if tags is not None else existing_tags_json
        await conn.execute(
            "UPDATE threads SET owner = ?, name = COALESCE(?, name), updated_at = ?, "
            "metadata_json = ?, tags_json = ? WHERE id = ?",
            (
                owner or existing_owner,
                name,
                now,
                json.dumps(merged_metadata, ensure_ascii=False),
                new_tags_json,
                thread_id,
            ),
        )
        await conn.commit()


async def rename_thread(conn: aiosqlite.Connection, thread_id: str, name: str) -> None:
    await conn.execute("UPDATE threads SET name = ?, updated_at = ? WHERE id = ?", (name, _now(), thread_id))
    await conn.commit()


def _remove_thread_elements_dir(elements_dir: Path, thread_id: str) -> None:
    """data/elements/<thread_id>/ を丸ごと削除する（要素ファイルの孤児化防止）。

    ON DELETE CASCADE は elements テーブルの行を消すだけで、
    persist_element_file が書いた実ファイルには触れないため、
    スレッド削除の都度ここで明示的に削除する。
    """
    thread_dir = elements_dir / thread_id
    try:
        shutil.rmtree(thread_dir, ignore_errors=True)
    except OSError:
        logger.warning("Failed to delete the elements directory: %s", thread_dir, exc_info=True)


async def delete_thread_row(conn: aiosqlite.Connection, thread_id: str, elements_dir: Path | None = None) -> None:
    """スレッド行を削除する（ON DELETE CASCADE で紐づく steps/elements も消える）。

    LangGraph 側の checkpoints.sqlite には触れない（一覧に出なくなるだけで、
    チェックポイント自体は孤立データとして残る。意図したトレードオフ）。
    """
    await conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
    await conn.commit()
    if elements_dir is not None:
        _remove_thread_elements_dir(elements_dir, thread_id)


async def get_thread_owner(conn: aiosqlite.Connection, thread_id: str) -> str | None:
    cursor = await conn.execute("SELECT owner FROM threads WHERE id = ?", (thread_id,))
    row = await cursor.fetchone()
    return row[0] if row else None


async def get_thread_detail(conn: aiosqlite.Connection, thread_id: str) -> ThreadDict | None:
    """再開・表示に使う ThreadDict を組み立てる。

    "id" は必ず引数の thread_id をそのまま返す。@chainlit/react-client の
    resume_thread ハンドラは thread.id が要求した thread_id と異なると
    window.location.href='/thread/<id>' へハードナビゲートする（このSPAには
    存在しないルート）ため、ここでズレると再開が壊れる。
    """
    cursor = await conn.execute(
        "SELECT owner, name, created_at, updated_at, metadata_json, tags_json FROM threads WHERE id = ?",
        (thread_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    owner, name, created_at, _updated_at, metadata_json, tags_json = row

    steps_cursor = await conn.execute(
        "SELECT data_json FROM steps WHERE thread_id = ? ORDER BY created_at", (thread_id,)
    )
    step_rows = await steps_cursor.fetchall()
    steps = [json.loads(r[0]) for r in step_rows]

    elements = await get_elements_for_thread(conn, thread_id)

    return {
        "id": thread_id,
        "createdAt": created_at,
        "name": name,
        "userId": owner,
        "userIdentifier": owner,
        "tags": json.loads(tags_json) if tags_json else None,
        "metadata": json.loads(metadata_json or "{}"),
        "steps": steps,
        "elements": elements,
    }


async def list_threads_summary(
    conn: aiosqlite.Connection, owner: str, limit: int = 30, before: str | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """サイドバー表示用の軽量な一覧（{id,name,updatedAt}のみ）を返す。

    before に渡した thread_id より updated_at が古いものだけを返す
    （カーソルページネーション。フロントは戻り値の next_cursor を次回の
    before として渡す）。updated_at が同一時刻に揃うケース（短時間に複数
    スレッドが作成された場合。datetime.now() の分解能次第で起こりうる）に
    備え、rowid（挿入順）を副次キーにして順序を決定的にする。
    """
    params: list[Any] = [owner]
    query = "SELECT id, name, updated_at FROM threads WHERE owner = ?"
    if before:
        query += " AND updated_at < (SELECT updated_at FROM threads WHERE id = ?)"
        params.append(before)
    query += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
    params.append(limit + 1)

    cursor = await conn.execute(query, params)
    rows = await cursor.fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [{"id": r[0], "name": r[1], "updatedAt": r[2]} for r in rows]
    next_cursor = items[-1]["id"] if has_more and items else None
    return items, next_cursor


async def upsert_step_row(conn: aiosqlite.Connection, step_dict: dict) -> None:
    """StepDict をそのまま JSON blob として保存する（正規化しない＝実装を軽く保つ）。

    紐づく thread 行が無ければ、直前で upsert_thread_stub 済みであること
    （呼び出し元の ChatThreadDataLayer.create_step/update_step 参照）。
    """
    step_id = step_dict["id"]
    thread_id = step_dict.get("threadId")
    created_at = step_dict.get("createdAt") or _now()
    await conn.execute(
        "INSERT INTO steps (id, thread_id, created_at, data_json) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET thread_id = excluded.thread_id, "
        "created_at = excluded.created_at, data_json = excluded.data_json",
        (step_id, thread_id, created_at, json.dumps(step_dict, ensure_ascii=False)),
    )
    await conn.commit()


async def delete_step_row(conn: aiosqlite.Connection, step_id: str) -> None:
    await conn.execute("DELETE FROM steps WHERE id = ?", (step_id,))
    await conn.commit()


_WINDOWS_RESERVED_CHARS_RE = re.compile(r'[\\/:*?"<>|]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _sanitize_filename_component(name: str) -> str:
    """ファイル名・ディレクトリ名の1要素として安全な文字列にする（パストラバーサル対策）。

    LLMやツールが渡す element.name/thread_id はディレクトリ区切りや `..` を
    含みうる。`Path(name).name` でディレクトリ部分を剥がし、それでも空・
    `.`/`..` になる場合は固定のフォールバック名にする。さらにWindowsで
    ファイル名に使えない文字（`: * ? " < > |` 等）・予約デバイス名
    （CON/NUL/COM1 等）も無害な文字へ置き換える（本アプリはWindows環境で
    運用されるため、これを怠ると dest.write_bytes() が OSError で失敗し、
    要素の永続化が静かにスキップされる）。
    """
    candidate = Path(name).name
    if not candidate or candidate in (".", ".."):
        return "file"
    candidate = _WINDOWS_RESERVED_CHARS_RE.sub("_", candidate)
    stem = candidate.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        candidate = f"_{candidate}"
    return candidate


async def _read_element_bytes(element: Any) -> bytes | None:
    """Chainlit Element からバイト列を取り出す（path優先、次点でcontent）。

    element.url のみを持つ要素（外部URL）はローカルに実体が無いため None を返す
    （呼び出し元はファイル保存をスキップし、ElementDict のメタデータだけ保存する）。
    """
    path = getattr(element, "path", None)
    if path:
        try:
            return await asyncio.to_thread(Path(path).read_bytes)
        except OSError:
            logger.exception("Failed to read the element file: path=%s", path)
            return None
    content = getattr(element, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    return None


async def persist_element_file(
    conn: aiosqlite.Connection,
    elements_dir: Path,
    *,
    thread_id: str,
    element_id: str,
    name: str,
    content: bytes,
    mime: str | None,
    for_id: str | None,
    kind: str,
    extra_data: dict,
) -> Path:
    """要素の実体を data/elements/<thread_id>/<element_id>_<name> へ保存し、DB行をupsertする。

    provide_download/analyze_image(show_in_chat=True) の添付（kind="chainlit"、
    ChatThreadDataLayer.create_element 経由）と、回答本文への画像埋め込み
    （kind="inline"、app.py の _embed_local_images_as_session_urls 経由）の
    両方がこの関数を共有する。extra_data（ElementDict相当の辞書）の "url" を
    配信エンドポイントのURLへ書き換えてから data_json として保存する
    （Chainlit標準の chainlitKey はプロセスメモリ上の一時配信専用で
    スレッド再開・プロセス再起動に耐えないため使わない）。

    呼び出し元は事前に upsert_thread_stub 等でスレッド行の存在を保証しておくこと
    （steps テーブルと同じ FK 制約のため）。
    """
    thread_dir = elements_dir / _sanitize_filename_component(thread_id)
    dest = thread_dir / f"{element_id}_{_sanitize_filename_component(name)}"
    await asyncio.to_thread(thread_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(dest.write_bytes, content)

    data = dict(extra_data)
    data["url"] = f"/locohane/elements/{element_id}"
    created_at = _now()
    await conn.execute(
        "INSERT INTO elements (id, thread_id, for_id, kind, file_path, mime, created_at, data_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET thread_id = excluded.thread_id, for_id = excluded.for_id, "
        "kind = excluded.kind, file_path = excluded.file_path, mime = excluded.mime, "
        "data_json = excluded.data_json",
        (element_id, thread_id, for_id, kind, str(dest), mime, created_at, json.dumps(data, ensure_ascii=False)),
    )
    await conn.commit()
    return dest


async def upsert_element_meta(conn: aiosqlite.Connection, element_id: str, thread_id: str, for_id: str, data: dict) -> None:
    """実ファイルを持たない要素（url指定など）のメタデータのみをDBへ保存する。"""
    created_at = _now()
    await conn.execute(
        "INSERT INTO elements (id, thread_id, for_id, kind, file_path, mime, created_at, data_json) "
        "VALUES (?, ?, ?, 'chainlit', NULL, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET thread_id = excluded.thread_id, for_id = excluded.for_id, "
        "file_path = NULL, mime = excluded.mime, data_json = excluded.data_json",
        (element_id, thread_id, for_id, data.get("mime"), created_at, json.dumps(data, ensure_ascii=False)),
    )
    await conn.commit()


async def get_element_row(conn: aiosqlite.Connection, element_id: str, thread_id: str | None = None) -> dict | None:
    """要素1件のメタデータを取得する（配信エンドポイント・get_element の両方が使う）。"""
    if thread_id is not None:
        cursor = await conn.execute(
            "SELECT thread_id, file_path, mime, data_json FROM elements WHERE id = ? AND thread_id = ?",
            (element_id, thread_id),
        )
    else:
        cursor = await conn.execute(
            "SELECT thread_id, file_path, mime, data_json FROM elements WHERE id = ?", (element_id,)
        )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {"thread_id": row[0], "file_path": row[1], "mime": row[2], "data": json.loads(row[3])}


async def delete_element_row(conn: aiosqlite.Connection, element_id: str, thread_id: str | None = None) -> None:
    """要素のDB行と実ファイルを削除する（ask_user_choice/ask_user_question のフォーム後片付け等）。"""
    row = await get_element_row(conn, element_id, thread_id)
    if row is None:
        return
    if thread_id is not None:
        await conn.execute("DELETE FROM elements WHERE id = ? AND thread_id = ?", (element_id, thread_id))
    else:
        await conn.execute("DELETE FROM elements WHERE id = ?", (element_id,))
    await conn.commit()
    if row["file_path"]:
        try:
            Path(row["file_path"]).unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to delete the element file: %s", row["file_path"], exc_info=True)


async def get_elements_for_thread(conn: aiosqlite.Connection, thread_id: str, kind: str = "chainlit") -> list[dict]:
    """get_thread_detail が使う、スレッドに紐づく要素一覧（ElementDictのリスト）。"""
    cursor = await conn.execute(
        "SELECT data_json FROM elements WHERE thread_id = ? AND kind = ? ORDER BY created_at", (thread_id, kind)
    )
    rows = await cursor.fetchall()
    return [json.loads(r[0]) for r in rows]


async def get_user_row(conn: aiosqlite.Connection, identifier: str) -> PersistedUser | None:
    cursor = await conn.execute("SELECT id, identifier, created_at FROM users WHERE identifier = ?", (identifier,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return PersistedUser(id=row[0], identifier=row[1], createdAt=row[2])


async def create_user_row(conn: aiosqlite.Connection, user: User) -> PersistedUser:
    """id == identifier とする（別途opaque idを振る必要が無いため）。"""
    now = _now()
    await conn.execute(
        "INSERT INTO users (id, identifier, created_at, metadata_json) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(identifier) DO NOTHING",
        (user.identifier, user.identifier, now, json.dumps(user.metadata or {}, ensure_ascii=False)),
    )
    await conn.commit()
    persisted = await get_user_row(conn, user.identifier)
    assert persisted is not None
    return persisted


async def cleanup_old_threads(
    conn: aiosqlite.Connection, retention_days: int, elements_dir: Path | None = None
) -> int:
    """updated_at が retention_days より古いスレッド行を削除する（steps/elementsはCASCADE）。

    src/cleanup.py の cleanup_old_files と同じ「0以下で無効化」の約束を守る。
    LangGraph 側の checkpoints.sqlite は削除しない。elements_dir を渡した場合、
    削除した各スレッドの data/elements/<thread_id>/ も合わせて削除する
    （delete_thread_row と同じ孤児化防止）。

    Returns:
        削除したスレッド数。
    """
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    cursor = await conn.execute("SELECT id FROM threads WHERE updated_at < ?", (cutoff_iso,))
    rows = await cursor.fetchall()
    thread_ids = [row[0] for row in rows]
    if thread_ids:
        await conn.execute("DELETE FROM threads WHERE updated_at < ?", (cutoff_iso,))
        await conn.commit()
        logger.info("Deleted expired threads: %d", len(thread_ids))
        if elements_dir is not None:
            for thread_id in thread_ids:
                _remove_thread_elements_dir(elements_dir, thread_id)
    return len(thread_ids)


async def run_cleanup_loop(
    conn: aiosqlite.Connection, retention_days: int, interval_hours: float, elements_dir: Path | None = None
) -> None:
    """src/cleanup.py の run_cleanup_loop と同じ形の常駐タスク。0以下なら即return。"""
    import asyncio

    if retention_days <= 0:
        return
    interval_seconds = interval_hours * 3600
    while True:
        await asyncio.sleep(interval_seconds)
        await cleanup_old_threads(conn, retention_days, elements_dir)


class ChatThreadDataLayer(BaseDataLayer):
    """Chainlit BaseDataLayer の薄いアダプタ。実処理は上記モジュール関数へ委譲する。"""

    def __init__(self, conn: aiosqlite.Connection, elements_dir: Path):
        self._conn = conn
        self._elements_dir = elements_dir

    async def get_user(self, identifier: str) -> PersistedUser | None:
        return await get_user_row(self._conn, identifier)

    async def create_user(self, user: User) -> PersistedUser | None:
        return await create_user_row(self._conn, user)

    async def delete_feedback(self, feedback_id: str) -> bool:
        return True

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return feedback.id or str(uuid.uuid4())

    @queue_until_user_message()
    async def create_element(self, element) -> None:
        """要素（cl.Image/cl.File/cl.CustomElement等）の実体を data/elements/ へ永続化する。

        Chainlit本体（chainlit/element.py の Element._create）はこれを
        asyncio.create_task() でfire-and-forget起動するだけで、ライブ表示自体は
        別途 session.persist_file() が担う（chainlit_key経由）。つまりここでの
        永続化はスレッド再開・プロセス再起動後の復元にのみ効き、送信直後の
        ライブ表示には影響しない。

        for_id が無い要素（Chainlit公式のTaskList等）は永続化しない
        （chainlit公式実装のchainlit_data_layer.create_elementと同じガード。
        本アプリはTaskListを使っていないが将来の事故防止として踏襲する）。
        """
        for_id = getattr(element, "for_id", None)
        if not for_id:
            return
        thread_id = element.thread_id
        if not thread_id:
            return
        await upsert_thread_stub(self._conn, thread_id, _current_owner())

        content = await _read_element_bytes(element)
        element_dict = element.to_dict()
        if content is None:
            # url のみを持つ要素（外部URL）等。ファイルは保存せずメタデータだけ残す。
            await upsert_element_meta(self._conn, element.id, thread_id, for_id, element_dict)
            return
        try:
            await persist_element_file(
                self._conn,
                self._elements_dir,
                thread_id=thread_id,
                element_id=element.id,
                name=element.name,
                content=content,
                mime=element.mime,
                for_id=for_id,
                kind="chainlit",
                extra_data=element_dict,
            )
        except Exception:
            # Chainlit側がasyncio.create_task()でfire-and-forget呼び出しするため、
            # ここで捕捉し損ねると診断ログが一切残らないまま要素が消失する。
            # OSError（ファイルI/O）以外にaiosqlite.Error（DB書き込み）等も
            # 起こりうるため、ファイル/DBを問わず全例外を対象にする。
            logger.exception("Failed to persist the element file: id=%s", element.id)

    async def get_element(self, thread_id: str, element_id: str):
        row = await get_element_row(self._conn, element_id, thread_id)
        return row["data"] if row else None

    @queue_until_user_message()
    async def delete_element(self, element_id: str, thread_id: str | None = None) -> None:
        await delete_element_row(self._conn, element_id, thread_id)

    @queue_until_user_message()
    async def create_step(self, step_dict: dict) -> None:
        """StepDict を永続化する。

        `chainlit.data.base.BaseDataLayer` は create_step/update_step/
        delete_step/create_element/delete_element を
        `@queue_until_user_message()` で装飾した状態の**抽象**メソッドとして
        定義しているが、Python のデコレータは抽象メソッド定義に付けても
        オーバーライドしたサブクラスの実装には引き継がれない（抽象メソッド
        は単なるプレースホルダーであり、実際に呼ばれるのはこのサブクラス側の
        実装そのものであるため）。装飾し忘れると、ユーザーが1文字も送信して
        いない新規タブでも on_chat_start のウェルカムメッセージ等が即座に
        永続化され、「新規チャット」ボタンを押すたびに無題のスレッドが
        サイドバーに溜まり続けるバグになる（2026-08-19 実機で確認）。
        このため各メソッドへ明示的に再度 `@queue_until_user_message()` を
        付け直し、Chainlit本体が意図する「最初のユーザー発言まで永続化を
        保留する」動作を回復させている。

        metadata.ephemeral_progress が立っている StepDict は永続化しない。
        src/tools.py の _push_background_job_progress/_push_dispatch_agent_progress
        がバックグラウンドジョブの実行中に一定間隔で送る「実行中です（経過N秒・
        job_id=xxx）。」等の進捗pushで、実行時点でしか意味を持たない。以前は
        他のメッセージ同様に全件永続化しており、スレッド再開（on_chat_resume）の
        たびに get_thread_detail が返す steps 経由でこの古い進捗表示がそのまま
        再生されてしまっていた（2026-08-21 ユーザー報告）。ライブ表示は
        cl.Message.send() 内の emitter.send_step 側で行われるため、ここで
        永続化をスキップしてもリアルタイム表示には影響しない。
        """
        if (step_dict.get("metadata") or {}).get("ephemeral_progress"):
            return
        thread_id = step_dict.get("threadId")
        if thread_id:
            await upsert_thread_stub(self._conn, thread_id, _current_owner())
        await upsert_step_row(self._conn, step_dict)

    @queue_until_user_message()
    async def update_step(self, step_dict: dict) -> None:
        # create_step も同じ @queue_until_user_message() ガードを持つため、
        # ここまで到達した時点（=ガードを通過済み）で呼べば二重にキューされる
        # ことはない（ガードはメソッド単位ではなく毎回のセッション状態を見る
        # だけなので、通過済みの呼び出しをさらに素通しするだけになる）。
        await self.create_step(step_dict)

    @queue_until_user_message()
    async def delete_step(self, step_id: str) -> None:
        await delete_step_row(self._conn, step_id)

    async def get_thread_author(self, thread_id: str) -> str:
        owner = await get_thread_owner(self._conn, thread_id)
        if owner is None:
            raise ValueError(f"Thread {thread_id} not found")
        return owner

    async def delete_thread(self, thread_id: str) -> None:
        await delete_thread_row(self._conn, thread_id, self._elements_dir)

    async def list_threads(self, pagination: Pagination, filters: ThreadFilter) -> PaginatedResponse[ThreadDict]:
        owner = filters.userId or ANONYMOUS_OWNER
        items, next_cursor = await list_threads_summary(self._conn, owner, pagination.first, pagination.cursor)
        threads: list[ThreadDict] = []
        for item in items:
            thread = await get_thread_detail(self._conn, item["id"])
            if thread is not None:
                threads.append(thread)
        return PaginatedResponse(
            pageInfo=PageInfo(hasNextPage=next_cursor is not None, startCursor=None, endCursor=next_cursor),
            data=threads,
        )

    async def get_thread(self, thread_id: str) -> ThreadDict | None:
        return await get_thread_detail(self._conn, thread_id)

    async def update_thread(
        self,
        thread_id: str,
        name: str | None = None,
        user_id: str | None = None,
        metadata: dict | None = None,
        tags: list[str] | None = None,
    ) -> None:
        owner = user_id or _current_owner()
        await save_thread(self._conn, thread_id, owner=owner, name=name, metadata=metadata, tags=tags)

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        await self._conn.close()

    async def get_favorite_steps(self, user_id: str) -> list:
        return []
