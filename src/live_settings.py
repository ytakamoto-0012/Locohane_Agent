"""設定ダッシュボードの「ユーザー」タブで変える設定のうち、本体を再起動せずに効かせるもの。

ユーザーのランク（[memory] user_ranks・default_rank。内容が食い違うメモリーをどのユーザーのものに従わせるか）は
config_overrides.json に保存される。以前は起動時に読んだ値だけを使っていたため、ランクを変えてもインスタンスを
再起動するまで効かなかった。会話の送信のたびに config_overrides.json の更新日時を確かめ、変わっていれば
ランクだけを読み直して、メモリーの規則（src/memory.py の policy_for）へ反映する（ほかの設定は起動時の値のまま）。
ログインユーザー（AUTH_USERS）の読み直しは src/auth_users.py。
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
from pathlib import Path

from src import memory
from src.config import Config, default_overrides_path, load_config

logger = logging.getLogger(__name__)


def current_overrides_path() -> Path:
    """本体が読む config_overrides.json（load_config() と同じ決め方）。"""
    raw = os.getenv("CONFIG_OVERRIDES_PATH")
    return Path(raw) if raw else default_overrides_path()


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class MemoryRanksReloader:
    """config_overrides.json が変わったら、ユーザーのランクを読み直してメモリーの規則へ入れる。"""

    def __init__(self, config: Config, overrides_path: Path | None = None):
        self._memory_dir = config.memory_dir
        self._path = overrides_path or current_overrides_path()
        self._stamp = _mtime(self._path)
        self._ranks = (dict(config.memory_user_ranks), config.memory_default_rank)
        self._lock = threading.Lock()

    def refresh(self) -> bool:
        """変わっていれば読み直す。ランクが変わったら True。"""
        stamp = _mtime(self._path)
        with self._lock:
            if stamp == self._stamp:
                return False
            self._stamp = stamp
            # 公開範囲の種類等は再起動で効く設定なので、今の規則のままにし、ランクだけを差し替える。
            # load_config() が規則を登録し直すと、その間のほかのセッションが新しい規則を一瞬見てしまうため、
            # 登録しない（register_memory_policy=False）で読んだものを使い、今の規則へランクだけ入れて戻す。
            current = memory.policy_for(self._memory_dir)
            try:
                cfg = load_config(register_memory_policy=False)
            except Exception as exc:  # noqa: BLE001 - 読めなければ前のランクのまま
                logger.warning("Could not reload the user ranks after config_overrides.json changed; keeping the previous ranks: %s", exc)
                return False
            ranks = (dict(cfg.memory_user_ranks), cfg.memory_default_rank)
            memory.set_policy(self._memory_dir, dataclasses.replace(current, ranks=ranks[0], default_rank=ranks[1]))
            if ranks == self._ranks:
                return False
            self._ranks = ranks
            logger.info("Reloaded the user ranks ([memory] user_ranks) from config_overrides.json: %d user(s)", len(ranks[0]))
            return True
