"""3D ビューア用データのキャッシュ（src/cad/viewer_cache.py）のテスト。"""

from __future__ import annotations

import hashlib
import os
import time

from src.cad import viewer_cache


def _key(name: str) -> str:
    """本物のキー（SHA-256 の 16 進 64 桁）と同じ形のキー。"""
    return hashlib.sha256(name.encode()).hexdigest()


def _src(tmp_path, name="data.json", size=100):
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


def test_key_changes_with_the_file_and_the_options(tmp_path) -> None:
    f = _src(tmp_path, "model.step")
    key = viewer_cache.cache_key(f, {"max_triangles": 10})
    assert key == viewer_cache.cache_key(f, {"max_triangles": 10})
    assert key != viewer_cache.cache_key(f, {"max_triangles": 20})
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert key != viewer_cache.cache_key(f, {"max_triangles": 10})


def test_key_includes_the_compared_file(tmp_path) -> None:
    f = _src(tmp_path, "new.step")
    old = _src(tmp_path, "old.step")
    key = viewer_cache.cache_key(f, {"compare_with": str(old)})
    assert key != viewer_cache.cache_key(f, {})
    old.write_bytes(b"y" * 200)
    assert key != viewer_cache.cache_key(f, {"compare_with": str(old)})
    # 読めないファイルはキャッシュしない。
    assert viewer_cache.cache_key(tmp_path / "none.step", {}) is None


def test_store_lookup_and_prune_oldest_first(tmp_path) -> None:
    cache = tmp_path / "cache"
    for i, name in enumerate(("a", "b", "c")):
        key = _key(name)
        viewer_cache.store(cache, key, _src(tmp_path, f"{name}.json"), 1000)
        hit = viewer_cache.lookup(cache, key)
        assert hit is not None and hit.read_bytes() == b"x" * 100
        os.utime(hit, (time.time() - 100 + i, time.time() - 100 + i))
    # "a" を使い直すと、いちばん古いのは "b" になる。
    viewer_cache.lookup(cache, _key("a"))
    assert viewer_cache.prune(cache, 250) == 1
    assert viewer_cache.lookup(cache, _key("b")) is None
    assert viewer_cache.lookup(cache, _key("a")) is not None and viewer_cache.lookup(cache, _key("c")) is not None
    assert viewer_cache.lookup(cache, _key("zzz")) is None


def test_too_large_data_is_not_cached(tmp_path) -> None:
    cache = tmp_path / "cache"
    viewer_cache.store(cache, _key("big"), _src(tmp_path, size=500), 100)
    assert viewer_cache.lookup(cache, _key("big")) is None


def test_prune_removes_stale_temp_files_and_clear_removes_all(tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    stale = cache / ".tmp_old.json"
    fresh = cache / ".tmp_new.json"
    stale.write_text("{}")
    fresh.write_text("{}")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    viewer_cache.prune(cache, 0)
    assert not stale.exists() and fresh.exists()
    viewer_cache.store(cache, _key("k"), _src(tmp_path), 1000)
    assert viewer_cache.clear(cache) == 2
    assert not list(cache.iterdir())


def test_prune_and_clear_leave_files_the_cache_did_not_make(tmp_path) -> None:
    # cache_dir に他のデータの置き場を指定されても、キャッシュの名前（64 桁の 16 進 + .json）でない JSON は消さない。
    cache = tmp_path / "cache"
    cache.mkdir()
    other = cache / "settings.json"
    other.write_bytes(b"y" * 500)
    old = time.time() - 7200
    os.utime(other, (old, old))
    viewer_cache.store(cache, _key("k"), _src(tmp_path), 1000)
    viewer_cache.prune(cache, 10)
    assert other.exists()
    viewer_cache.clear(cache)
    assert other.exists()
