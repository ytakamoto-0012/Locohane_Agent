"""ファイルから読んだ内容を記録するメモリーの、元ファイルの sha256 の記録と照合のテスト。

ナレッジの単一値の質問（「2025年9月の携帯電話料金」）で、毎回 explore が検索から PDF を
読み直していた。元ファイルの sha256 を記録し、一致すれば開き直さずにメモリーの内容を
使えるようにする（src/memory.py の sources）。
"""

import hashlib

import pytest

from src import memory


def _write(path, data: bytes):
    path.write_bytes(data)
    return path


def test_create_records_sha256_and_unchanged_when_same(tmp_path):
    root = tmp_path / "mem"
    src = _write(tmp_path / "bill.pdf", b"total 1234")
    memory.create_memory(root, "phone-bill", "d", "reference", "2025-09 total 1,234", [src])

    mem = memory.read_memory(root, "phone-bill")
    assert mem.sources == (memory.SourceFile(str(src), hashlib.sha256(b"total 1234").hexdigest(), 10),)
    assert [c.state for c in memory.check_sources(mem)] == ["unchanged"]


def test_changed_same_size_and_different_size_and_missing(tmp_path):
    root = tmp_path / "mem"
    a = _write(tmp_path / "a.pdf", b"aaaa")
    b = _write(tmp_path / "b.pdf", b"bbbb")
    c = _write(tmp_path / "c.pdf", b"cccc")
    memory.create_memory(root, "m", "d", "reference", "body", [a, b, c])

    a.write_bytes(b"AAAA")  # 同じサイズで内容だけ違う → ハッシュで検出する
    b.write_bytes(b"bbbbb")
    c.unlink()

    states = [x.state for x in memory.check_sources(memory.read_memory(root, "m"))]
    assert states == ["changed", "changed", "missing"]


def test_update_keeps_sources_when_omitted_and_replaces_or_clears(tmp_path):
    root = tmp_path / "mem"
    a = _write(tmp_path / "a.pdf", b"v1")
    memory.create_memory(root, "m", "d", "reference", "body", [a])

    memory.update_memory(root, "m", "body2")
    assert memory.read_memory(root, "m").sources[0].sha256 == hashlib.sha256(b"v1").hexdigest()

    a.write_bytes(b"v2")
    memory.update_memory(root, "m", "body3", [a])
    mem = memory.read_memory(root, "m")
    assert mem.sources[0].sha256 == hashlib.sha256(b"v2").hexdigest()
    assert [c.state for c in memory.check_sources(mem)] == ["unchanged"]

    memory.update_memory(root, "m", "body4", [])
    assert memory.read_memory(root, "m").sources == ()


def test_memory_without_sources_is_unchanged_format(tmp_path):
    root = tmp_path / "mem"
    path = memory.create_memory(root, "plain", "d", "user", "likes short answers")
    assert "sources" not in path.read_text(encoding="utf-8")
    mem = memory.read_memory(root, "plain")
    assert mem.sources == ()
    assert memory.check_sources(mem) == []


def test_missing_source_file_is_rejected_and_nothing_written(tmp_path):
    root = tmp_path / "mem"
    with pytest.raises(ValueError, match="source file not found"):
        memory.create_memory(root, "m", "d", "reference", "body", [tmp_path / "nope.pdf"])
    with pytest.raises(ValueError):
        memory.read_memory(root, "m")


def test_sourced_content_limit_applies_only_with_sources(tmp_path):
    root = tmp_path / "mem"
    a = _write(tmp_path / "a.pdf", b"x")
    long_body = "x" * (memory.SOURCED_CONTENT_MAX_CHARS + 1)
    with pytest.raises(ValueError, match="at most"):
        memory.create_memory(root, "m", "d", "reference", long_body, [a])
    memory.create_memory(root, "plain", "d", "reference", long_body)
    with pytest.raises(ValueError, match="at most"):
        memory.update_memory(root, "plain", long_body, [a])


def test_too_many_source_files(tmp_path):
    root = tmp_path / "mem"
    files = [_write(tmp_path / f"{i}.pdf", b"x") for i in range(memory.SOURCE_FILES_MAX + 1)]
    with pytest.raises(ValueError, match="at most"):
        memory.create_memory(root, "m", "d", "reference", "body", files)


def test_broken_sources_entry_is_ignored(tmp_path):
    root = tmp_path / "mem"
    memory.ensure_dirs(root)
    (root / "reference" / "m.md").write_text(
        "---\nname: m\ndescription: d\ntype: reference\nsources:\n- path: x\n- 3\n---\n\nbody\n", encoding="utf-8"
    )
    assert memory.read_memory(root, "m").sources == ()
