"""ビューアで変えた色・隠した部品を反映した 3D モデルを CAD ファイルに書き出す（3D ビューアの「保存」ボタン）。

入力は、変更を反映したビューア用データ（mesh_data.apply_view_edits の結果）。元の形式ごとに書き出す形式を決める:

- STEP / IGES / BREP / BIN / CadQuery コード → STEP（部品の名前・部品の色・面の色。隠した部品は含めない）。
  形は元のファイル（コードのときはビューア用データを作ったときに書いた BREP）から読み直す。面の番号は
  shapes_to_viewer_data と同じ順になるので、ビューア用データの面の色をそのまま当てられる。cadquery が要る。
  元が STEP なら、読み直した XCAF の文書に変更を当てて書くので、PMI（GD&T）・レイヤー・材料・検証プロパティ・
  ユーザー定義属性・ヘッダー（作成者・組織・作成システム）も残る（src/cad/step_xcaf.py）。スキーマは
  ``data["exportOptions"]["step_write_schema"]``（[cad_viewer] step_write_schema。auto は元と同じ、元が STEP でなければ AP242）。
- DXF → DXF（元のファイルの写しのレイヤーの色を変え、隠したレイヤーを非表示にする。面ごとの色は書けない）。
- STL → 色付きバイナリ STL（VisCAM / SolidView 方式。面＝平面領域ごとの色）。numpy だけで動く。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import numpy as np

# 元の形式 → 書き出す形式の拡張子。
OUTPUT_SUFFIX = {"STEP": ".step", "IGES": ".step", "BREP": ".step", "BIN": ".step", "CADQUERY": ".step", "DXF": ".dxf", "STL": ".stl"}


class ExportError(ValueError):
    """書き出せない（元のファイルが無い・形が変わった等）。"""


def output_suffix(data: dict[str, Any]) -> str | None:
    """このビューア用データから書き出すファイルの拡張子（書き出せない形式なら None）。"""
    return OUTPUT_SUFFIX.get(str((data.get("source") or {}).get("format") or ""))


def export_model(data: dict[str, Any], out_path: str | Path) -> list[str]:
    """変更を反映したビューア用データ data から 3D モデルを out_path に書く。戻り値は LLM・利用者へ伝える注意。

    Raises:
        ExportError: 書き出せない場合。
    """
    fmt = str((data.get("source") or {}).get("format") or "")
    if fmt == "STL":
        return _export_stl(data, Path(out_path))
    if fmt == "DXF":
        return _export_dxf(data, Path(out_path))
    if fmt in OUTPUT_SUFFIX:
        return _export_step(data, Path(out_path))
    raise ExportError(f"Models from {fmt or 'this source'} cannot be exported.")


def _hidden_parts(data: dict[str, Any]) -> set[int]:
    return {p for p in ((data.get("viewState") or {}).get("hiddenParts") or []) if isinstance(p, int) and not isinstance(p, bool)}


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[k : k + 2], 16) for k in (1, 3, 5))  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# STL
# ---------------------------------------------------------------------------


def _export_stl(data: dict[str, Any], out: Path) -> list[str]:
    mesh = data.get("mesh") or {}
    positions = np.frombuffer(base64.b64decode(mesh.get("positions") or ""), dtype="<f4").reshape(-1, 3)
    indices = np.frombuffer(base64.b64decode(mesh.get("indices") or ""), dtype="<u4").reshape(-1, 3)
    if not len(indices):
        raise ExportError("The model has no triangles to write.")
    tris = positions[indices].astype(np.float32)
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 0)
    attr = np.zeros(len(tris), dtype="<u2")
    colored = False
    for fi, (start, count) in enumerate(mesh.get("faceRanges") or []):
        color = (data["faces"][fi] or {}).get("color") if fi < len(data.get("faces") or []) else None
        if not color or not count:
            continue
        r, g, b = _rgb(color)
        attr[start : start + count] = 0x8000 | ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)
        colored = True
    rec = np.zeros(len(tris), dtype=[("normal", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    rec["normal"], rec["v"], rec["attr"] = normals, tris, attr
    header = b"Locohane 3D viewer export".ljust(80, b" ")
    with out.open("wb") as f:
        f.write(header)
        f.write(np.array([len(tris)], dtype="<u4").tobytes())
        f.write(rec.tobytes())
    return [] if colored else ["The model has no colors, so the STL has none."]


# ---------------------------------------------------------------------------
# DXF
# ---------------------------------------------------------------------------


def _export_dxf(data: dict[str, Any], out: Path) -> list[str]:
    import ezdxf

    src = Path((data.get("source") or {}).get("path") or "")
    if not src.is_file():
        raise ExportError(f"The original DXF file is not found: {src}")
    doc = ezdxf.readfile(str(src))
    hidden = _hidden_parts(data)
    notes: list[str] = []
    for index, part in enumerate(data.get("parts") or []):
        if not isinstance(part, dict):
            continue
        name = part.get("name")
        if not name or not doc.layers.has_entry(name):
            continue
        layer = doc.layers.get(name)
        before = (part.get("layer") or {}).get("color")
        color = part.get("color")
        if color and (before or "").lower() != color.lower():
            layer.rgb = _rgb(color)
        elif not color and before:
            # 色を外した（「色なし」に戻した）レイヤーは、元の色を残さず既定の色（ACI 7）にする。
            layer.dxf.discard("true_color")
            layer.color = 7
            notes.append(f"Layer {name!r} had its color removed, so it was set to the default color (ACI 7).")
        pid = part.get("id")
        if (pid if isinstance(pid, int) and not isinstance(pid, bool) else index) in hidden:
            layer.off()
        else:
            layer.on()
            layer.thaw()
    doc.saveas(str(out))
    changes = (data.get("edits") or {}).get("color_changes") or []
    if any(c.get("kind") == "face" for c in changes):
        notes.append("DXF has no per-face colors, so only layer colors were written (face color changes are not in the file).")
    return notes


# ---------------------------------------------------------------------------
# STEP
# ---------------------------------------------------------------------------


def _load_parts(data: dict[str, Any]):
    from .export_mesh import Part, load_cad_file

    src = data.get("source") or {}
    fmt = str(src.get("format") or "")
    if fmt == "CADQUERY":
        brep = Path(src.get("brep") or "")
        if not brep.is_file():
            raise ExportError("The shape made by the CadQuery code is no longer available. Show the code again and save.")
        import cadquery as cq

        return [Part("result", "result", cq.importers.importBrep(str(brep)).val().wrapped)]
    path = Path(src.get("path") or "")
    if not path.is_file():
        raise ExportError(f"The original {fmt} file is not found: {path}")
    parts, _units = load_cad_file(path, fmt)
    return parts


def _export_step(data: dict[str, Any], out: Path) -> list[str]:
    import cadquery as cq
    from OCP.TopAbs import TopAbs_FACE

    from .export_mesh import _ids_in, _index_map

    parts = _load_parts(data)
    compound = cq.Compound.makeCompound([cq.Shape.cast(p.shape) for p in parts]).wrapped
    face_map = _index_map(compound, TopAbs_FACE)
    faces = data.get("faces") or []
    if face_map.Extent() != len(faces):
        raise ExportError("The original file has changed since it was shown (the number of faces differs). Show it again and save.")
    part_info = data.get("parts") or []
    hidden = _hidden_parts(data)
    if part_info and len(part_info) != len(parts):
        raise ExportError("The original file has changed since it was shown (the number of parts differs). Show it again and save.")
    schema = str((data.get("exportOptions") or {}).get("step_write_schema") or "auto")
    if (data.get("source") or {}).get("format") == "STEP" and parts and parts[0].label is not None:
        # 元が STEP なら、読み直した XCAF の文書に変更を当てて書く（PMI・レイヤー・材料・属性・ヘッダーを残す）。
        from .step_xcaf import export_step_xcaf

        try:
            return export_step_xcaf(data, parts, face_map, out, schema)
        except ValueError as e:
            raise ExportError(str(e)) from e

    stem = Path((data.get("source") or {}).get("path") or "model").stem or "model"
    assy = cq.Assembly(name=stem)
    used: set[str] = {stem}
    written = 0
    for pi, part in enumerate(parts):
        if pi in hidden:
            continue
        info = part_info[pi] if part_info else {}
        name = str(info.get("name") or (stem if len(parts) == 1 else part.name) or f"part{pi + 1}")
        unique, n = name, 2
        while unique in used:
            unique, n = f"{name}_{n}", n + 1
        used.add(unique)
        part_color = info.get("color")
        color = cq.Color(*(c / 255 for c in _rgb(part_color)), 1) if part_color else None
        assy.add(cq.Shape.cast(part.shape), name=unique, color=color)
        child = assy.objects[unique]
        for fi in _ids_in(part.shape, TopAbs_FACE, face_map):
            fc = faces[fi].get("color")
            if fc and (part_color or "").lower() != fc.lower():
                child.addSubshape(cq.Shape.cast(face_map.FindKey(fi + 1)), color=cq.Color(*(c / 255 for c in _rgb(fc)), 1))
        written += 1
    if not written:
        raise ExportError("Every part is hidden, so there is nothing to write.")
    from cadquery.occ_impl.assembly import toCAF

    from . import step_p21
    from .step_xcaf import fix_names, write_schema_id, write_xcaf

    schema_id = write_schema_id(schema, None)
    _top, doc = toCAF(assy, coloredSTEP=True)
    fix_names(doc)
    try:
        write_xcaf(doc, out, schema_id)
    except ValueError as e:
        raise ExportError(str(e)) from e
    text, _enc = step_p21.read_text(out)
    out.write_bytes(step_p21.escape_non_ascii(text).encode("ascii", "replace"))
    notes = [f"Written as STEP {({3: 'AP203', 4: 'AP214', 5: 'AP242'})[schema_id]}."]
    if hidden:
        notes.append(f"{len(hidden)} hidden part(s) are not in the file.")
    return notes
