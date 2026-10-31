"""src.tools._state._SRC_DIR がsrc/path_memory.pyを指すことの回帰テスト。

2026-08-26のtools.py分割リファクタで_SRC_DIRが誤ってsrc/tools/を指すよう
なった際、この前提を検証するテストが無かったため2日間気づかれなかった
（office_shared/*_common.pyのregister_output_pathやexcel_common.pyの
PIDレジストリがフェイルオープンで沈黙的に無効化されていた）。
"""

from src.tools import _state


def test_src_dir_points_to_directory_containing_path_memory():
    assert (_state._SRC_DIR / "path_memory.py").is_file()
