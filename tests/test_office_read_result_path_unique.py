"""読み取りスクリプトの結果JSON（write_json_result）のファイル名が、同じファイルを同時刻に読んでも衝突しないことの回帰テスト。

Windows の datetime.now() は分解能が粗く、同じブックの複数シートを並列に読むと
マイクロ秒まで同じ名前になり、後の結果が先の結果を上書きしていた（本番ログ 2026-11-03）。
explore はそれを避けようとして1シートずつ順番に読むようになり、大幅に遅くなった。
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = [
    _ROOT / "skills" / "office_shared" / "excel_common.py",
    _ROOT / "skills" / "office_shared" / "docx_common.py",
    _ROOT / "skills" / "office_shared" / "pptx_common.py",
    _ROOT / "skills" / "pdf-tools" / "scripts" / "_common.py",
]


def _load(path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(f"_result_path_test_{path.parent.name}_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("module_path", _MODULES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_same_source_at_same_instant_gets_distinct_result_paths(module_path, monkeypatch, tmp_path) -> None:
    module = _load(module_path)
    frozen = datetime(2026, 11, 3, 12, 50, 22, 871193)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(module, "datetime", _FrozenDatetime)
    monkeypatch.setattr(module, "register_output_path", lambda *a, **k: None)
    monkeypatch.setenv("AGENT_DEFAULT_WORKDIR", str(tmp_path))
    monkeypatch.setenv("AGENT_THREAD_ID", "t")
    source = tmp_path / "book.xlsx"

    first = module.write_json_result({"sheet": "年度別比較"}, "read", source)["result_path"]
    second = module.write_json_result({"sheet": "当日の流れ"}, "read", source)["result_path"]

    assert first != second
    assert "年度別比較" in Path(first).read_text(encoding="utf-8")
    assert "当日の流れ" in Path(second).read_text(encoding="utf-8")
