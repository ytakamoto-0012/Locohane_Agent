"""取り込み・コピーの間に、循環参照のゴミを決まった間隔で回収する。

httpx の応答は本体のストリームと互いに参照し合う（httpx._client.BoundSyncStream）ため、埋め込みサーバー・Qdrant の
応答は本体ごと循環参照の回収（gc）を待つ。長く生きるオブジェクト（チャンクの本文・ファイルの記録）が多い取り込みでは、
Python の世代別 GC が全体の回収をなかなか行わず、応答がたまり続ける（20万チャンクの1ファイルの取り込みで 1GB 以上）。
"""

from __future__ import annotations

import gc
import time

# 回収の間隔の下限（秒）。
MIN_INTERVAL_SECONDS = 5.0
# 回収にかかった時間の何倍を空けるか（取り込み・コピーを遅くするのを 1/20 程度までにする）。
INTERVAL_FACTOR = 20


class GcPacer:
    def __init__(self) -> None:
        self._next = time.monotonic() + MIN_INTERVAL_SECONDS

    def tick(self) -> None:
        """前回から間隔が空いていれば gc.collect() する（ループの中で何度呼んでもよい）。"""
        now = time.monotonic()
        if now < self._next:
            return
        gc.collect()
        took = time.monotonic() - now
        self._next = time.monotonic() + max(MIN_INTERVAL_SECONDS, took * INTERVAL_FACTOR)


__all__ = ["GcPacer"]
