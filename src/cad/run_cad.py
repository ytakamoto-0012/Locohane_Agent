"""show_cad_viewer のサブプロセス側の入口。形を読み（または CadQuery コードの結果を受け取り）ビューア用 JSON を書く。

ファイルを読む場合（src/tools/cad_viewer.py が起動する）::

    python run_cad.py --file <CADファイル> --out <JSON> --options '<JSON>'

基準モデル（options の reference・align）との位置合わせと偏差を、できたビューア用データに足す場合::

    python run_cad.py --reference <ビューア用データの JSON> --out <JSON> --options '<JSON>'

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
        max_triangles=int(raw.get("max_triangles", 1_000_000)),
        surface_analysis=bool(raw.get("surface_analysis", True)),
    )


def _attach_diff(data: dict[str, Any], shape: Any, raw: dict[str, Any]) -> None:
    """options の compare_with（前の版の CAD ファイルかビューア用データ）があれば、比べた結果を data["diff"] に入れる。

    比べられなくても形の表示はできるので、失敗は data["diff"] = {"error": ...} にして続ける。
    """
    prev = raw.get("compare_with")
    if not prev:
        return
    try:
        from src.cad.compare import CompareError, compare, load_previous

        old, label = load_previous(prev)
        diff = compare(old, shape, data, _options(raw), int(raw.get("max_triangles", 1_000_000)))
        diff["previous"] = label
    except ImportError as e:
        diff = {"error": f"cadquery is not installed ({e})"}
    except CompareError as e:
        diff = {"error": str(e)}
    except Exception as e:  # noqa: BLE001 - OCP の C++ 例外は Python の別の型で来る
        diff = {"error": f"could not compare with the previous version ({type(e).__name__}: {e})"}
    data["diff"] = diff


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(EXIT_FAILED)


def _done(data: dict[str, Any], out_path: str, raw: dict[str, Any] | None = None) -> None:
    raw = raw or {}
    from src.cad.topology import attach_topology

    # 開いた形の穴（トポロジートレランス）とローカル AXIS（100・200・300 の直交する 3 本の線）を足す。
    attach_topology(data, raw)
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
        # 「保存」ボタンで 3D モデルを書き出すときに形を読み直せるよう、ビューア用データの隣に BREP で残す。
        brep = str(Path(out_path).with_suffix(".brep"))
        source: dict[str, Any] = {"kind": "code", "format": "CADQUERY"}
        try:
            if shape.exportBrep(brep):
                source["brep"] = brep
        except Exception:  # noqa: BLE001 - 残せなくても表示はできる（書き出しだけができなくなる）
            pass
        try:
            data = shape_to_viewer_data(shape, _options(raw_options), source)
        except (CadLoadError, TooManyTrianglesError) as e:
            _fail(f"Error: {e}")
            return
        except Exception as e:  # noqa: BLE001 - OCP の C++ 例外は Python の別の型で来る
            _fail(f"Error: could not make the viewer data from the shape ({type(e).__name__}: {e})")
            return
        _attach_diff(data, shape, raw_options)
        _done(data, out_path, raw_options)


def _export(data_path: str, out_path: str) -> None:
    """3D ビューアの「保存」: ビューア用データから 3D モデルを書き、標準出力に ``CAD_VIEWER_OK {"notes": [...]}``。"""
    from src.cad.export_model import ExportError, export_model

    try:
        data = json.loads(Path(data_path).read_text(encoding="utf-8"))
        notes = export_model(data, out_path)
    except ImportError as e:
        _fail(f"Error: cadquery / ezdxf is not installed ({e}), so this model cannot be exported. Install them with pip install -r requirements.txt.")
        return
    except (ExportError, OSError, ValueError) as e:
        _fail(f"Error: {e}")
        return
    except Exception as e:  # noqa: BLE001 - OCP の C++ 例外は Python の別の型で来る
        _fail(f"Error: could not export the model ({type(e).__name__}: {e})")
        return
    print(f"{OK_MARKER} {json.dumps({'notes': notes}, ensure_ascii=False)}")


def _reference(data_path: str, out_path: str, raw: dict[str, Any]) -> None:
    """ビューア用データに基準モデル（options の reference）との位置合わせ・偏差を足して --out に書く（src/cad/reference.py）。

    基準モデルが読めなくても形は表示するので、失敗は deviation.error に入れて終了コード 0 で終える。
    """
    from src.cad.reference import attach_reference

    try:
        data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _fail(f"Error: could not read the viewer data {data_path}: {e}")
        return
    attach_reference(data, raw)
    write_viewer_json(data, out_path)
    dev = data.get("deviation") or {}
    print(f"{OK_MARKER} {json.dumps({'error': dev.get('error')} if dev.get('error') else {'stats': dev.get('stats')}, ensure_ascii=False)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file")
    group.add_argument("--export", help="変更を反映したビューア用データの JSON。--out に 3D モデルを書き出す。")
    group.add_argument("--reference", help="ビューア用データの JSON。options の reference（基準モデル）との偏差を足して --out に書く。")
    parser.add_argument("--out", required=True)
    parser.add_argument("--options", default="{}")
    args = parser.parse_args(argv)
    if args.export:
        _export(args.export, args.out)
        return
    raw = json.loads(args.options)
    if args.reference:
        _reference(args.reference, args.out, raw)
        return
    path = Path(args.file)
    fmt = format_of(path)
    if fmt is None or fmt == "VIEWER_JSON":
        _fail(f"Error: unsupported file type {path.suffix!r}. Supported: {supported_extensions_text()}")
        return
    if fmt in NO_CADQUERY_FORMATS:
        from src.cad.mesh_data import TooManyTrianglesError, stl_to_viewer_data
        from src.cad.stl_reader import StlError

        try:
            data = stl_to_viewer_data(
                path,
                max_triangles=int(raw.get("max_triangles", 1_000_000)),
                region_angle_deg=float(raw.get("stl_region_angle_deg", 1.0)),
                analysis=bool(raw.get("surface_analysis", True)),
            )
        except (StlError, TooManyTrianglesError, OSError) as e:
            _fail(f"Error: could not read the STL file: {e}")
            return
        _done(data, args.out, raw)
        return
    try:
        import cadquery as cq

        from src.cad.export_mesh import CadLoadError, load_cad_file, shapes_to_viewer_data
        from src.cad.mesh_data import TooManyTrianglesError
    except ImportError as e:
        _fail(f"Error: cadquery is not installed ({e}), so {fmt} files cannot be read (STL works without it). Install it with pip install -r requirements.txt.")
        return
    try:
        parts, units = load_cad_file(path, fmt)
        data = shapes_to_viewer_data(parts, source={"kind": "file", "format": fmt, "path": str(path)}, units=units, options=_options(raw))
        if raw.get("compare_with"):
            shapes = [cq.Shape.cast(p.shape) for p in parts]
            _attach_diff(data, shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes), raw)
    except (CadLoadError, TooManyTrianglesError) as e:
        _fail(f"Error: could not read the {fmt} file: {e}")
        return
    except Exception as e:  # noqa: BLE001 - OCP の C++ 例外は Python の別の型で来る
        _fail(f"Error: could not read the {fmt} file ({type(e).__name__}: {e})")
        return
    _done(data, args.out, raw)


if __name__ == "__main__":
    main()
