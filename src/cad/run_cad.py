"""show_cad_viewer のサブプロセス側の入口。形を読み（または CadQuery コードの結果を受け取り）ビューア用 JSON を書く。

ファイルを読む場合（src/tools/cad_viewer.py が起動する）::

    python run_cad.py --file <CADファイル> --out <JSON> --options '<JSON>'

CadQuery コードの場合は、ツールが「書き込みガード + CODE_PROLOGUE + 利用者のコード + CODE_EPILOGUE」を 1 つの
スクリプトにして実行する。コードは ``result`` 変数に形を入れるか、``show_object(形)`` を呼ぶ
（CQ-editor・cq-server と同じ書き方）。

終了コード 0 で JSON を書いたら、標準出力の最後の行に ``CAD_VIEWER_OK <統計の JSON>`` を出す。
失敗したら標準エラーに理由を書いて終了コード 2（ツールはその文をそのまま LLM へ返す）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # スクリプトとして起動されたとき（python run_cad.py）は、プロジェクトのルートを import 先に足す。
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.cad.formats import NO_CADQUERY_FORMATS, format_of, supported_extensions_text  # noqa: E402
from src.cad.mesh_data import write_viewer_json  # noqa: E402

OK_MARKER = "CAD_VIEWER_OK"
EXIT_FAILED = 2

# 利用者のコードの前に置く部分。{src_root} はプロジェクトのルート（src の親）。
CODE_PROLOGUE = """
import sys as _cad_sys
_cad_sys.path.insert(0, {src_root!r})
from src.cad.run_cad import CodeCollector as _CadCollector
_cad_collector = _CadCollector()
show_object = _cad_collector.show_object
debug = _cad_collector.debug
del _cad_sys
# ---- ここから利用者のコード ----
"""

# 利用者のコードの後に置く部分。
CODE_EPILOGUE = """
# ---- ここまで利用者のコード ----
_cad_collector.finish(globals(), {out_path!r}, {options!r})
"""


def _options(raw: dict[str, Any]):
    from src.cad.export_mesh import Options

    return Options(
        linear_deflection_ratio=float(raw.get("linear_deflection_ratio", 0.001)),
        angular_deflection_deg=float(raw.get("angular_deflection_deg", 20.0)),
        max_triangles=int(raw.get("max_triangles", 500_000)),
    )


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(EXIT_FAILED)


def _done(data: dict[str, Any], out_path: str) -> None:
    write_viewer_json(data, out_path)
    print(f"{OK_MARKER} {json.dumps(data['stats'])}")


class CodeCollector:
    """CadQuery コードから形を受け取る（show_object / result）。"""

    def __init__(self) -> None:
        self.objects: list[Any] = []

    def show_object(self, obj: Any, name: str | None = None, options: Any = None, **_kwargs: Any) -> None:
        self.objects.append(obj)

    def debug(self, obj: Any, name: str | None = None, **_kwargs: Any) -> None:
        """CQ-editor の debug()。ビューアには出さない（呼んでもエラーにしないため）。"""

    def finish(self, namespace: dict[str, Any], out_path: str, raw_options: dict[str, Any]) -> None:
        try:
            from src.cad.export_mesh import CadLoadError, shape_to_viewer_data, to_shape
            from src.cad.mesh_data import TooManyTrianglesError
        except ImportError as e:
            _fail(f"Error: cadquery is not installed ({e}). Install it with pip install -r requirements.txt.")
            return
        if self.objects:
            target, where = self.objects, "show_object(...)"
        elif "result" in namespace:
            target, where = namespace["result"], "result"
        else:
            _fail("Error: the code made no shape to show. Put the shape in a variable named `result`, or call show_object(shape).")
            return
        shape = to_shape(target)
        if shape is None:
            _fail(
                f"Error: {where} is not a shape (got {type(target).__name__}). Pass a cadquery Workplane, Shape, Sketch, "
                "Assembly, Vector/Location (a point) or a list of them."
            )
            return
        try:
            data = shape_to_viewer_data(shape, _options(raw_options), {"kind": "code", "format": "CADQUERY"})
        except (CadLoadError, TooManyTrianglesError) as e:
            _fail(f"Error: {e}")
            return
        except Exception as e:  # noqa: BLE001 - OCP の C++ 例外は Python の別の型で来る
            _fail(f"Error: could not make the viewer data from the shape ({type(e).__name__}: {e})")
            return
        _done(data, out_path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--options", default="{}")
    args = parser.parse_args(argv)
    raw = json.loads(args.options)
    path = Path(args.file)
    fmt = format_of(path)
    if fmt is None or fmt == "VIEWER_JSON":
        _fail(f"Error: unsupported file type {path.suffix!r}. Supported: {supported_extensions_text()}")
        return
    if fmt in NO_CADQUERY_FORMATS:
        from src.cad.mesh_data import TooManyTrianglesError, stl_to_viewer_data
        from src.cad.stl_reader import StlError

        try:
            data = stl_to_viewer_data(path, max_triangles=int(raw.get("max_triangles", 500_000)), region_angle_deg=float(raw.get("stl_region_angle_deg", 1.0)))
        except (StlError, TooManyTrianglesError, OSError) as e:
            _fail(f"Error: could not read the STL file: {e}")
            return
        _done(data, args.out)
        return
    try:
        from src.cad.export_mesh import CadLoadError, cad_file_to_viewer_data
        from src.cad.mesh_data import TooManyTrianglesError
    except ImportError as e:
        _fail(f"Error: cadquery is not installed ({e}), so {fmt} files cannot be read (STL works without it). Install it with pip install -r requirements.txt.")
        return
    try:
        data = cad_file_to_viewer_data(path, fmt, _options(raw))
    except (CadLoadError, TooManyTrianglesError) as e:
        _fail(f"Error: could not read the {fmt} file: {e}")
        return
    except Exception as e:  # noqa: BLE001 - OCP の C++ 例外は Python の別の型で来る
        _fail(f"Error: could not read the {fmt} file ({type(e).__name__}: {e})")
        return
    _done(data, args.out)


if __name__ == "__main__":
    main()
