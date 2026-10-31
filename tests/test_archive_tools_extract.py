"""archive-tools スキル（skills/archive-tools/scripts/extract_archive.py）の展開の回帰テスト。

- 単一ファイルの gz/bz2/xz は展開後のサイズが事前に分からないため、書き込みながら上限を数える
  （書き終わってから調べると、高圧縮のファイルでディスクを使い切るまで止まらなかった）。
- ゼロだけのデータを圧縮した .gz は「空の tar」としても読めるため、tar.gz と誤判定して何も展開しなかった。
"""

import gzip
import json
import subprocess
import sys
import tarfile
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "archive-tools" / "scripts"


def _extract(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "extract_archive.py"), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


def _write_gz(path: Path, data: bytes, repeat: int) -> None:
    with gzip.open(path, "wb") as f:
        for _ in range(repeat):
            f.write(data)


def test_single_gz_stops_at_the_limit_while_writing(tmp_path):
    archive = tmp_path / "bomb.gz"
    _write_gz(archive, b"A" * (1 << 20), 20)  # 展開すると 20MB
    dest = tmp_path / "out"
    proc = _extract(str(archive), "--dest", str(dest), "--max-total-mb", "2")
    assert proc.returncode == 1
    assert "exceeded the limit" in proc.stderr
    # 上限を超えた時点で止め、書きかけのファイルは消す。
    assert list(dest.iterdir()) == []


def test_single_gz_within_the_limit_is_extracted(tmp_path):
    archive = tmp_path / "data.txt.gz"
    _write_gz(archive, b"hello\n", 3)
    dest = tmp_path / "out"
    proc = _extract(str(archive), "--dest", str(dest))
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["format"] == "gz"
    assert (dest / "data.txt").read_bytes() == b"hello\n" * 3


def test_gz_of_zeros_is_not_mistaken_for_an_empty_tar(tmp_path):
    archive = tmp_path / "zeros.bin.gz"
    _write_gz(archive, b"\0" * 4096, 4)
    dest = tmp_path / "out"
    proc = _extract(str(archive), "--dest", str(dest))
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["format"] == "gz"
    assert result["extracted_files"] == 1
    assert (dest / "zeros.bin").read_bytes() == b"\0" * 4096 * 4


def test_empty_tar_gz_is_still_a_tar(tmp_path):
    archive = tmp_path / "empty.tar.gz"
    with tarfile.open(archive, "w:gz"):
        pass
    proc = _extract(str(archive), "--dest", str(tmp_path / "out"))
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["format"] == "tar.gz"
    assert result["extracted_files"] == 0
