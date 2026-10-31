"""read_memory が元ファイルの照合結果を本文の後ろに付けることのテスト（src/tools/memory_tools.py）。"""

from pathlib import Path

from src import memory
from src.tools import _state, memory_tools


def test_read_memory_reports_unchanged_and_changed(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    monkeypatch.setattr(_state, "_MEMORY_ROOT", root)
    src = tmp_path / "bill.pdf"
    src.write_bytes(b"1234")
    memory.create_memory(root, "phone-bill", "d", "reference", "2025-09 total 1,234", [src])

    out = memory_tools.read_memory.invoke({"name": "phone-bill"})
    assert out.startswith("[reference] phone-bill\nd\n\n2025-09 total 1,234")
    assert f"- {src}: unchanged" in out
    assert "without opening them again" in out

    src.write_bytes(b"99999")
    out = memory_tools.read_memory.invoke({"name": "phone-bill"})
    assert f"- {src}: CHANGED" in out
    assert "Do not use the values" in out


def test_read_memory_without_sources_has_no_section(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    monkeypatch.setattr(_state, "_MEMORY_ROOT", root)
    memory.create_memory(root, "plain", "d", "user", "body")
    assert memory_tools.read_memory.invoke({"name": "plain"}) == "[user] plain\nd\n\nbody"


def test_create_memory_tool_returns_error_for_missing_source(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    monkeypatch.setattr(_state, "_MEMORY_ROOT", root)
    # パス解決（@N・作業ディレクトリ）は Chainlit のセッションを使うため、テストでは絶対パスをそのまま返す。
    monkeypatch.setattr(memory_tools, "_resolve_file_tools_path", lambda raw: (Path(raw), None))
    out = memory_tools.create_memory.invoke(
        {
            "name": "m",
            "description": "d",
            "memory_type": "reference",
            "content": "body",
            "source_files": [str(tmp_path / "nope.pdf")],
        }
    )
    assert out.startswith("Error: ")
    assert "source file not found" in out
