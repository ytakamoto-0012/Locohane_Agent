"""同一データディレクトリに対する多重起動を防ぐ、プロセス排他ロック。

data/ 配下の checkpoints.sqlite（LangGraph の会話状態）・chat_threads.sqlite
（スレッド一覧、src/thread_store.py）は、プロセス内で1本の aiosqlite 接続を
アプリ寿命ずっと使い回す設計になっており、複数プロセスから同時に書き込まれる
ことを想定していない。ユーザーが誤って同じ data/ を指す状態で Locohane を
二重起動（例: ターミナルを2つ開いて同じ app.bat を実行）すると、2つの
プロセスが同じ .sqlite ファイルへ別々に書き込み合い、
"database disk image is malformed" 等でファイルが破損する事故が実際に
発生した（2026-08-26 ユーザー報告）。

このモジュールは data/ 直下に app.lock という空ファイルを作り、OSのファイル
ロック（Windows: msvcrt.locking）で排他制御する。ロックはプロセスが保持する
ファイルハンドルに紐づき、プロセス終了時（正常終了・クラッシュ問わず）に
OSが自動的に解放するため、PIDファイル方式のような「前回異常終了時の古い
ロックが残り続けて誤検知する」問題が起きない。
"""

from __future__ import annotations

import msvcrt
import time
from pathlib import Path

# acquire() の再試行回数と間隔（is_locked() の一瞬のロックとの衝突を避ける用）。
_ACQUIRE_ATTEMPTS = 3
_ACQUIRE_RETRY_INTERVAL_SECONDS = 0.1

# プロセス生存中、ハンドルを保持し続けるためのモジュールグローバル。
# ローカル変数のままにすると関数を抜けた時点でGCされ、ロックが即座に
# 解放されてしまう。
_lock_file = None


class InstanceAlreadyRunningError(RuntimeError):
    """同じデータディレクトリに対して、既に別の Locohane プロセスが起動中。"""


def acquire(lock_path: Path) -> None:
    """lock_path に対する排他ロックを取得する。

    既に別プロセスが保持している場合は InstanceAlreadyRunningError を送出する
    （ロックは取得できない＝起動を続けさせない）。呼び出し元はできる限り早い
    タイミング（DBファイルを開く前）で呼ぶこと。
    """
    global _lock_file
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not lock_path.exists() or lock_path.stat().st_size == 0:
        # msvcrt.locking はロック対象バイト範囲が実際にファイル内に存在する
        # ことを要求するため、新規作成時（0バイト）は1バイト書いておく。
        # 既に別プロセスがロック中でもここは新規作成時のみの経路であり
        # 到達しない（作成済みなら st_size>0 でスキップされる）ため競合しない。
        lock_path.write_bytes(b"0")
    f = open(lock_path, "r+b")
    # 管理ツールの is_locked() は判定のために一瞬だけロックを取って手放すため、
    # その瞬間と重なると未起動なのに取得に失敗しうる。数回だけ間を置いて
    # 再試行し、本当に保持され続けている場合のみ多重起動とみなす
    # （2026-09-29 レビューで発見）。
    last_exc: OSError | None = None
    for attempt in range(_ACQUIRE_ATTEMPTS):
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            last_exc = None
            break
        except OSError as exc:
            last_exc = exc
            if attempt + 1 < _ACQUIRE_ATTEMPTS:
                time.sleep(_ACQUIRE_RETRY_INTERVAL_SECONDS)
    if last_exc is not None:
        exc = last_exc
        f.close()
        raise InstanceAlreadyRunningError(
            f"Locohane is already running in another process (data directory: {lock_path.parent}). "
            "Running twice on the same data/ would corrupt checkpoints.sqlite / "
            "chat_threads.sqlite through concurrent writes, so startup was aborted. "
            "Stop the process already running (another terminal/port) and try again."
        ) from exc
    _lock_file = f


def is_locked(lock_path: Path) -> bool:
    """lock_path が別プロセスに保持されているかを、取得を試みてすぐ手放す形で判定する。

    管理ツール（admin/supervisor.py）が、自分が子プロセスとして起動していない
    インスタンスについて「app.bat 等で外部起動中かどうか」を判定するために使う
    読み取り専用の確認。acquire() と異なり、成功してもロックを保持し続けない
    （このプロセス自身の `_lock_file` グローバルには一切触れない。自プロセスが
    acquire() 済みのロックの判定に使うことは想定していない）。

    Returns:
        True: 既に別プロセスが保持中（起動中とみなせる）。
        False: 誰も保持していない（ファイルが存在しない場合も含む＝未起動）。
    """
    if not lock_path.exists() or lock_path.stat().st_size == 0:
        return False
    try:
        f = open(lock_path, "r+b")
    except OSError:
        # ファイルを開けない（権限等）場合も、確実性を優先し「起動中」とみなす
        # （安全側＝多重起動防止側に倒す）。
        return True
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return True
    else:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    finally:
        f.close()


def release() -> None:
    """保持中のロックを解放する（プロセス正常終了時のベストエフォート）。

    未取得なら何もしない。プロセスがクラッシュした場合でもOSがハンドルを
    自動的に閉じるためロックは解放される。
    """
    global _lock_file
    if _lock_file is None:
        return
    try:
        _lock_file.seek(0)
        msvcrt.locking(_lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        _lock_file.close()
        _lock_file = None
