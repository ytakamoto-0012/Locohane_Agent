"""3D ビューア用データ（show_cad_viewer が CAD ファイルから作る JSON）のキャッシュ。

大きな STEP はビューア用データにするまで 1 分以上かかるので、作った JSON を [cad_viewer].cache_dir
（既定 ${common_data_dir}/cad_cache）に取っておき、同じファイルを同じ設定でもう一度開くときはそれを使う。

キーに入れるもの（どれかが変われば作り直す）:
- ファイルの絶対パス・サイズ・更新時刻（ナノ秒）
- 三角形化などの設定（src/tools/cad_viewer.py の _options）
- compare_with で比べる前の版のファイルのパス・サイズ・更新時刻
- 変換のコード（src/cad/*.py）の中身のハッシュ（アプリを更新したら作り直す）

置き場の合計が [cad_viewer].cache_max_mb を超えたら、使った時刻（ファイルの更新時刻。使うたびに更新する）の
古いものから消す。書き込みの途中で強制終了して残った一時ファイル（.tmp_*）も、古いものは片付けで消す。
キャッシュは作り直せるものなので、データのバックアップには入れない（admin/data_backup.py）。
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CAD_DIR = Path(__file__).resolve().parent
_SUFFIX = ".json"
_TMP_PREFIX = ".tmp_"
# この時間より古い一時ファイルは書き込みの途中で止まったものとみなして消す（秒）。
_STALE_TMP_SECONDS = 3600


@functools.lru_cache(maxsize=1)
def converter_digest() -> str:
    """変換のコード（src/cad/*.py）の中身のハッシュ。コードが変われば前のキャッシュは使わない。"""
    h = hashlib.sha256()
    for p in sorted(_CAD_DIR.glob("*.py")):
        h.update(p.name.encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _file_sig(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {"path": os.path.normcase(str(path.resolve())), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def cache_key(path: Path, options: dict[str, Any]) -> str | None:
    """path を options でビューア用データにした結果のキー。ファイルを読めなければ None（キャッシュを使わない）。

    options["compare_with"] があれば、そのファイルも中身の目印（サイズ・更新時刻）ごとキーに入れる。
    """
    try:
        opts = dict(options)
        compare = opts.pop("compare_with", "")
        payload = {
            "file": _file_sig(path),
            "compare_with": _file_sig(Path(compare)) if compare else None,
            "options": opts,
            "converter": converter_digest(),
        }
    except OSError:
        return None
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def lookup(cache_dir: Path, key: str) -> Path | None:
    """キャッシュがあればそのパス（使った時刻として更新時刻を今にする）。無ければ None。"""
    p = cache_dir / f"{key}{_SUFFIX}"
    if not p.is_file():
        return None
    try:
        os.utime(p)
    except OSError:
        pass
    return p


def store(cache_dir: Path, key: str, src: Path, max_bytes: int) -> None:
    """src（作ったビューア用データ）をキャッシュに写し、合計が max_bytes を超えた分を古いものから消す。

    失敗してもビューアの表示には関係ないので、例外は出さずに警告を残すだけにする。
    """
    try:
        size = src.stat().st_size
        if max_bytes > 0 and size > max_bytes:
            logger.info("cad viewer cache: %s (%d bytes) is larger than the cache limit, so it is not cached", src.name, size)
            return
        cache_dir.mkdir(parents=True, exist_ok=True)
        # 書いている途中のものを使わないよう、同じフォルダの一時ファイルに写してから置き換える。
        fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=_SUFFIX, dir=str(cache_dir))
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, cache_dir / f"{key}{_SUFFIX}")
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        prune(cache_dir, max_bytes)
    except OSError:
        logger.warning("cad viewer cache: could not store %s", src, exc_info=True)


def prune(cache_dir: Path, max_bytes: int) -> int:
    """古い一時ファイルを消し、合計が max_bytes 以下になるまで使った時刻の古いものから消す。消した数を返す。"""
    if not cache_dir.is_dir():
        return 0
    removed = 0
    now = time.time()
    entries: list[tuple[float, int, Path]] = []
    for p in cache_dir.iterdir():
        try:
            st = p.stat()
        except OSError:
            continue
        if not p.is_file():
            continue
        if p.name.startswith(_TMP_PREFIX):
            if now - st.st_mtime > _STALE_TMP_SECONDS:
                p.unlink(missing_ok=True)
                removed += 1
            continue
        if p.suffix == _SUFFIX:
            entries.append((st.st_mtime, st.st_size, p))
    total = sum(size for _, size, _ in entries)
    if max_bytes <= 0:
        return removed
    for _, size, p in sorted(entries):
        if total <= max_bytes:
            break
        try:
            p.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
    return removed


def clear(cache_dir: Path) -> int:
    """キャッシュを全部消す。消した数を返す。"""
    if not cache_dir.is_dir():
        return 0
    removed = 0
    for p in cache_dir.iterdir():
        if p.is_file() and (p.suffix == _SUFFIX or p.name.startswith(_TMP_PREFIX)):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed
