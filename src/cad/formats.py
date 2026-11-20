"""3D ビューアが読めるファイル形式（拡張子 → 形式名）。依存ライブラリ無しで import できる。"""

from __future__ import annotations

from pathlib import Path

# 拡張子（小文字）→ 形式名。読み込み方法は export_mesh.load_cad_file / mesh_data.stl_to_viewer_data。
SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".step": "STEP",
    ".stp": "STEP",
    ".iges": "IGES",
    ".igs": "IGES",
    ".brep": "BREP",
    ".brp": "BREP",
    ".bin": "BIN",
    ".dxf": "DXF",
    ".stl": "STL",
    # show_cad_viewer が書き出したビューア用データ（同じ形をもう一度選ばせるとき、作り直さずに使う）。
    ".json": "VIEWER_JSON",
}

# 形式名のうち cadquery（OCP）が要らないもの。
NO_CADQUERY_FORMATS = frozenset({"STL", "VIEWER_JSON"})


def format_of(path: str | Path) -> str | None:
    """拡張子から形式名を返す（対応していなければ None）。"""
    return SUPPORTED_EXTENSIONS.get(Path(path).suffix.lower())


def supported_extensions_text() -> str:
    """エラー文に載せる対応拡張子の一覧。"""
    return ", ".join(sorted(SUPPORTED_EXTENSIONS))
