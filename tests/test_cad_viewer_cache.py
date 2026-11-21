"""3D ビューア用データのキャッシュ（src/cad/viewer_cache.py）のテスト。"""

from __future__ import annotations

import os
import time

from src.cad import viewer_cache


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
    for i, key in enumerate(("a", "b", "c")):
        viewer_cache.store(cache, key, _src(tmp_path, f"{key}.json"), 1000)
        hit = viewer_cache.lookup(cache, key)
        assert hit is not None and hit.read_bytes() == b"x" * 100
        os.utime(hit, (time.time() - 100 + i, time.time() - 100 + i))
    # "a" を使い直すと、いちばん古いのは "b" になる。
    viewer_cache.lookup(cache, "a")
    assert viewer_cache.prune(cache, 250) == 1
    assert viewer_cache.lookup(cache, "b") is None
    assert viewer_cache.lookup(cache, "a") is not None and viewer_cache.lookup(cache, "c") is not None
    assert viewer_cache.lookup(cache, "zzz") is None


def test_too_large_data_is_not_cached(tmp_path) -> None:
    cache = tmp_path / "cache"
    viewer_cache.store(cache, "big", _src(tmp_path, size=500), 100)
    assert viewer_cache.lookup(cache, "big") is None


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
    viewer_cache.store(cache, "k", _src(tmp_path), 1000)
    assert viewer_cache.clear(cache) == 2
    assert not list(cache.iterdir())
