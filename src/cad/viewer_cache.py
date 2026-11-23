"""3D ビューア用データ（show_cad_viewer が CAD ファイルから作る JSON）のキャッシュ。

大きな STEP はビューア用データにするまで 1 分以上かかるので、作った JSON を [cad_viewer].cache_dir
（既定 ${common_data_dir}/cad_cache）に取っておき、同じファイルを同じ設定でもう一度開くときはそれを使う。

キーに入れるもの（どれかが変われば作り直す）:
- ファイルの絶対パス・サイズ・更新時刻（ナノ秒）
- 三角形化などの設定（src/tools/cad_viewer.py の _options）
- compare_with で比べる前の版のファイルのパス・サイズ・更新時刻
- 変換のコード（src/cad/*.py）の中身のハッシュ（アプリを更新したら作り直す）
- 変換に使う Python の cadquery・cadquery-ocp・ezdxf の版（更新すると面・辺の ID の並びが変わりうるため）

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
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CAD_DIR = Path(__file__).resolve().parent
_SUFFIX = ".json"
_TMP_PREFIX = ".tmp_"
# この時間より古い一時ファイルは書き込みの途中で止まったものとみなして消す（秒）。
_STALE_TMP_SECONDS = 3600
# キャッシュの項目の名前（cache_key の SHA-256 16 進 64 桁 + .json）。片付け・全消去はこの名前のものだけを消す
# （cache_dir に他のデータの置き場を指定されても、キャッシュ以外の JSON を消さないため）。
_ENTRY_NAME = re.compile(r"[0-9a-f]{64}\.json")
_TMP_NAME = re.compile(r"\.tmp_.*\.json")


def _is_entry(p: Path) -> bool:
    return bool(_ENTRY_NAME.fullmatch(p.name))


def _is_tmp(p: Path) -> bool:
    return bool(_TMP_NAME.fullmatch(p.name))


@functools.lru_cache(maxsize=1)
def converter_digest() -> str:
    """変換のコード（src/cad/*.py）の中身のハッシュ。コードが変われば前のキャッシュは使わない。"""
    h = hashlib.sha256()
    for p in sorted(_CAD_DIR.glob("*.py")):
        h.update(p.name.encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


# 三角形化・面や辺の並び（ID）を左右するライブラリ。版が変わればキャッシュの ID が今の形と合わなくなる。
_LIBRARIES = ("cadquery", "cadquery-ocp", "ezdxf")
_VERSION_PROBE = (
    "import importlib.metadata as m, json, sys\n"
    "out = {}\n"
    "for n in sys.argv[1:]:\n"
    "    try:\n"
    "        out[n] = m.version(n)\n"
    "    except Exception:\n"
    "        out[n] = None\n"
    "print(json.dumps(out))\n"
)


# library_versions の結果を覚えておく秒数。[scripts] の Python はアプリを動かしたまま更新できるので、
# ずっとは覚えない（聞けなかったときは覚えない）。
_VERSIONS_TTL_SEC = 300.0
_versions_memo: dict[str | None, tuple[float, str]] = {}
_versions_lock = threading.Lock()


def library_versions(python: str | None = None) -> str:
    """ビューア用データを作る Python（[scripts] の Python。None ならこのプロセス）に入っている CAD ライブラリの版。

    変換はサブプロセスの Python で動くので、アプリの Python ではなくそちらに聞く（OCP を import しない
    importlib.metadata で読むだけなので軽い）。聞けなければ空文字（cache_key はキャッシュを使わない）。
    聞けた結果は _VERSIONS_TTL_SEC 秒だけ覚える。
    """
    now = time.monotonic()
    with _versions_lock:
        memo = _versions_memo.get(python)
    if memo is not None and now - memo[0] < _VERSIONS_TTL_SEC:
        return memo[1]
    value = _probe_library_versions(python)
    if value:
        with _versions_lock:
            _versions_memo[python] = (now, value)
    return value


def _clear_library_versions() -> None:
    with _versions_lock:
        _versions_memo.clear()


library_versions.cache_clear = _clear_library_versions  # type: ignore[attr-defined]


def _probe_library_versions(python: str | None) -> str:
    if python is None:
        import importlib.metadata as md

        found: dict[str, str | None] = {}
        for name in _LIBRARIES:
            try:
                found[name] = md.version(name)
            except md.PackageNotFoundError:
                found[name] = None
        return json.dumps(found, sort_keys=True)
    import subprocess

    try:
        proc = subprocess.run(
            [python, "-I", "-c", _VERSION_PROBE, *_LIBRARIES],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        found = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 and proc.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        found = None
    if not isinstance(found, dict):
        logger.warning("cad viewer cache: could not read the CAD library versions from %s", python)
        return ""
    return json.dumps(found, sort_keys=True)


def _file_sig(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {"path": os.path.normcase(str(path.resolve())), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _compare_sig(compare: str) -> list[dict[str, Any]]:
    """比べる前のファイルの目印（サイズ・更新時刻）。

    比べる前がビューア用データ（.json）のときは、JSON そのものではなく、その JSON が指すモデルファイルの
    形を比べる（src/cad/compare.py の load_previous）ので、参照先（source.brep・source.path。参照の連鎖）も
    目印に足す。参照先をその場で直したとき、古い比較結果を返さないため。
    """
    sigs: list[dict[str, Any]] = []
    seen: set[str] = set()
    p = Path(compare)
    for _ in range(10):
        key = os.path.normcase(str(p.resolve()))
        if key in seen:
            break
        seen.add(key)
        sigs.append(_file_sig(p))  # 先頭のファイルが読めなければ OSError（cache_key が None を返す）
        if p.suffix.lower() != ".json":
            break
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            break
        src = data.get("source") if isinstance(data, dict) else None
        if not isinstance(src, dict):
            break
        ref = src.get("brep") or src.get("path")
        if not ref:
            break
        p = Path(ref)
        if not p.is_file():
            break
    return sigs


def cache_key(path: Path, options: dict[str, Any], python: str | None = None) -> str | None:
    """path を options でビューア用データにした結果のキー。ファイルを読めなければ None（キャッシュを使わない）。

    options["compare_with"] があれば、そのファイルも中身の目印（サイズ・更新時刻）ごとキーに入れる。
    python はビューア用データを作る Python（その CAD ライブラリの版をキーに入れる）。
    """
    libraries = library_versions(python)
    if not libraries:
        # 版が分からないと、ライブラリを更新した後も前の ID のキャッシュを使ってしまうので使わない。
        return None
    try:
        opts = dict(options)
        compare = opts.pop("compare_with", "")
        payload = {
            "file": _file_sig(path),
            "compare_with": _compare_sig(compare) if compare else None,
            "options": opts,
            "converter": converter_digest(),
            "libraries": libraries,
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
    """古い一時ファイルを消し、合計が max_bytes 以下になるまで使った時刻の古いものから消す。消した数を返す。

    max_bytes が 0 以下（キャッシュオフ）のときは、項目をすべて消す（溜まった分を置いておいても、ディスクの
    空きを取り戻せないので）。
    """
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
        if _is_tmp(p):
            if now - st.st_mtime > _STALE_TMP_SECONDS:
                p.unlink(missing_ok=True)
                removed += 1
            continue
        if _is_entry(p):
            entries.append((st.st_mtime, st.st_size, p))
    total = sum(size for _, size, _ in entries)
    if max_bytes <= 0:
        # 上限 0（キャッシュオフ）は、すでに溜まっている分も消す（ディスクの空きを取り戻すため）。
        for _, _, p in entries:
            try:
                p.unlink()
            except OSError:
                continue
            removed += 1
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
        if p.is_file() and (_is_entry(p) or _is_tmp(p)):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed
