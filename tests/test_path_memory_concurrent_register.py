"""path_memory.register() を並列に呼んだときの回帰テスト。

ロックファイルを初めて作る瞬間に2つの呼び出しが重なると、旧実装は
「サイズ0なら1バイト書いてからロック」していたため、後から書こうとした側が
先にロックされたバイトへ書き込んで PermissionError になっていた
（並列の search_documents の片方が失敗し、UI のStepが「実行中」のまま残った）。
"""

import threading
from pathlib import Path

from src import path_memory


def _register_concurrently(path_memory_dir: Path, n: int) -> tuple[list[int | None], list[BaseException]]:
    barrier = threading.Barrier(n)
    results: list[int | None] = [None] * n
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = path_memory.register("tid", f"C:/not_exist/file{i}.txt", path_memory_dir, 100)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_first_register_does_not_raise(tmp_path: Path) -> None:
    for trial in range(200):
        d = tmp_path / str(trial)
        results, errors = _register_concurrently(d, 2)
        assert errors == []
        assert sorted(results) == [1, 2]
        assert len(path_memory.list_entries("tid", d)) == 2


def test_lock_file_stays_empty(tmp_path: Path) -> None:
    path_memory.register("tid", "C:/not_exist/a.txt", tmp_path, 100)
    assert (tmp_path / "tid.json.lock").stat().st_size == 0
