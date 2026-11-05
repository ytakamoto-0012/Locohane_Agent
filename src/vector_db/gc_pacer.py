"""取り込み・コピーの間に、循環参照のゴミを決まった間隔で回収する。

httpx の応答は本体のストリームと互いに参照し合う（httpx._client.BoundSyncStream）ため、埋め込みサーバー・Qdrant の
応答は本体ごと循環参照の回収（gc）を待つ。長く生きるオブジェクト（チャンクの本文・ファイルの記録）が多い取り込みでは、
Python の世代別 GC が全体の回収をなかなか行わず、応答がたまり続ける（20万チャンクの1ファイルの取り込みで 1GB 以上）。

gc.collect() はその間プロセス全体（設定ダッシュボードの応答を含む）を止める。回収の間隔はプロセス全体で1つにする
（取り込み・コピーごとに数えると、同時に動かす数だけ回収が増え、7コレクションの同時取り込みでは止まっている時間が
7倍になってダッシュボードの操作が遅れるため。1回の回収でプロセス全体のゴミが片付くので、まとめても回収の効果は変わらない）。
"""

from __future__ import annotations

import gc
import threading
import time

# 回収の間隔の下限（秒）。
MIN_INTERVAL_SECONDS = 5.0
# 回収にかかった時間の何倍を空けるか（取り込み・コピーを遅くするのを 1/20 程度までにする）。
INTERVAL_FACTOR = 20

# 次に回収してよい時刻（time.monotonic()）。全 GcPacer で共有する。
_lock = threading.Lock()
_next = 0.0


class GcPacer:
    def __init__(self) -> None:
        global _next
        with _lock:
            # 始めてすぐには回収しない（ほかの取り込みが回収したばかりなら、その間隔を守る）。
            _next = max(_next, time.monotonic() + MIN_INTERVAL_SECONDS)

    def tick(self) -> None:
        """前回（どの GcPacer かによらない）から間隔が空いていれば gc.collect() する（ループの中で何度呼んでもよい）。"""
        global _next
        now = time.monotonic()
        if now < _next:
            return
        # 同時に間隔が空いたと判断した取り込みのうち1つだけが回収する。
        if not _lock.acquire(blocking=False):
            return
        try:
            if time.monotonic() < _next:
                return
            start = time.monotonic()
            gc.collect()
            took = time.monotonic() - start
            _next = time.monotonic() + max(MIN_INTERVAL_SECONDS, took * INTERVAL_FACTOR)
        finally:
            _lock.release()


__all__ = ["GcPacer"]
