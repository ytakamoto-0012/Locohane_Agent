"""STEP の PMI（GD&T）・レイヤー・属性の読み込みと、それらを残した STEP の書き出し。cadquery（OCP）が要る。

読み込み（``attach_step_info``）: XCAF の文書（src/cad/export_mesh.py の _load_xcaf が部品に残したラベル）から
寸法・幾何公差・データム・レイヤーを読み、それが指す面・エッジ・部品をビューアの ID に直して ``data["step"]`` に入れる。
ヘッダー（AP203 / AP214 / AP242 と版、作成システム）と製品の属性は src/cad/step_p21.py で P21 から読む。

書き出し（``export_step_xcaf``）: 元の STEP を同じ設定で読み直した XCAF の文書に、ビューアで変えた色・隠した部品・
変えた属性を当てて書く。PMI・レイヤー・材料・検証プロパティは OCCT がそのまま書き、OCCT が書かない
ユーザー定義属性と元のヘッダー（作成者・組織・作成システム）は step_p21 で足す。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.XCAFDoc import XCAFDoc_Datum, XCAFDoc_Dimension, XCAFDoc_DocumentTool, XCAFDoc_GeomTolerance

from . import step_p21
from .mesh_data import rnd

# 書き出す STEP のスキーマ（[cad_viewer] step_write_schema）→ OCCT の write.step.schema の番号。
_SCHEMA_IDS = {"AP214": 4, "AP203": 3, "AP242": 5}


def _enum_name(value: Any, prefix: str) -> str:
    name = getattr(value, "name", None) or str(value).split(".")[-1]
    return name.split(prefix, 1)[-1] if prefix in name else name


def _hstr(h: Any) -> str:
    try:
        return h.ToCString() if h is not None else ""
    except Exception:  # noqa: BLE001 - 空の文字列ハンドル
        return ""


class _ShapeIndex:
    """XCAF のラベルの形（部品の定義の中の面・エッジ・部品そのもの）→ ビューアの ID。"""

    def __init__(self, parts: list, face_map: TopTools_IndexedMapOfShape, edge_map: TopTools_IndexedMapOfShape) -> None:
        from .export_mesh import _index_map

        self.parts = parts
        self.shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(parts[0].label) if parts and parts[0].label is not None else None
        self.maps = []
        for part in parts:
            if part.label is None or self.shape_tool is None:
                self.maps.append(None)
                continue
            definition = self.shape_tool.GetShape_s(part.label)
            entry = {}
            for kind, gmap in ((TopAbs_FACE, face_map), (TopAbs_EDGE, edge_map)):
                dmap = _index_map(definition, kind)
                lmap = _index_map(part.shape, kind)  # 配置しても列挙の順は同じ
                ids = [gmap.FindIndex(lmap.FindKey(j)) - 1 if j <= lmap.Extent() else -1 for j in range(1, dmap.Extent() + 1)]
                entry[kind] = (dmap, ids)
            entry["shape"] = definition
            self.maps.append(entry)

    def lookup(self, label: TDF_Label) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {"faces": [], "edges": [], "parts": []}
        if self.shape_tool is None:
            return out
        shape = self.shape_tool.GetShape_s(label)
        if shape.IsNull():
            return out
        # 面の集まり（シェル・コンパウンド）を指すこともあるので、指す形の中の面・エッジの一覧も使う。
        # 一覧は部品ごとに作り直さず 1 回だけ作る（部品の多い STEP では、作り直しで十秒ほどかかった）。
        subs: dict[Any, list[TopoDS_Shape] | None] = {}
        for pi, entry in enumerate(self.maps):
            if entry is None:
                continue
            if entry["shape"].IsSame(shape):
                out["parts"].append(pi)
                continue
            for kind, key in ((TopAbs_FACE, "faces"), (TopAbs_EDGE, "edges")):
                dmap, ids = entry[kind]
                self._collect(shape, kind, dmap, ids, out[key], subs)
        return {k: sorted(set(v)) for k, v in out.items() if v}

    @staticmethod
    def _collect(
        shape: TopoDS_Shape, kind, dmap: TopTools_IndexedMapOfShape, ids: list[int], into: list[int], subs: dict[Any, list[TopoDS_Shape] | None]
    ) -> None:
        from .export_mesh import _index_map

        idx = dmap.FindIndex(shape)
        if idx > 0:
            if ids[idx - 1] >= 0:
                into.append(ids[idx - 1])
            return
        if kind not in subs:
            sub = _index_map(shape, kind)
            # 形そのものが面（エッジ）なら、中の一覧は自分だけ。上の FindIndex で見つからなかったので調べることは無い。
            subs[kind] = None if sub.Extent() == 1 and sub.FindKey(1).IsSame(shape) else [sub.FindKey(j) for j in range(1, sub.Extent() + 1)]
        items = subs[kind]
        if not items:
            return
        for item in items:
            k = dmap.FindIndex(item)
            if k > 0 and ids[k - 1] >= 0:
                into.append(ids[k - 1])


def _refs(dt, label: TDF_Label, index: _ShapeIndex) -> dict[str, list[int]]:
    first, second = TDF_LabelSequence(), TDF_LabelSequence()
    try:
        dt.GetRefShapeLabel_s(label, first, second)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list[int]] = {}
    for seq, suffix in ((first, ""), (second, "_second")):
        for i in range(1, seq.Length() + 1):
            for key, ids in index.lookup(seq.Value(i)).items():
                out.setdefault(key + suffix, []).extend(ids)
    return {k: sorted(set(v)) for k, v in out.items()}


_DIM_SYMBOL = {"Size_Diameter": "⌀", "Size_SphericalDiameter": "S⌀", "Size_Radius": "R", "Size_SphericalRadius": "SR"}


def _dimension(obj) -> dict[str, Any]:
    dtype = _enum_name(obj.GetType(), "DimensionType_")
    item: dict[str, Any] = {"type": dtype}
    try:
        item["value"] = rnd(obj.GetValue(), 6)
    except Exception:  # noqa: BLE001 - 範囲で書かれた寸法は値が 1 つでない
        pass
    try:
        if obj.IsDimWithRange():
            item["lower"] = rnd(obj.GetLowerBound(), 6)
            item["upper"] = rnd(obj.GetUpperBound(), 6)
        if obj.IsDimWithPlusMinusTolerance():
            item["upperTol"] = rnd(obj.GetUpperTolValue(), 6)
            item["lowerTol"] = rnd(obj.GetLowerTolValue(), 6)
        if obj.IsDimWithClassOfTolerance():
            res = obj.GetClassOfTolerance()
            if isinstance(res, tuple) and len(res) >= 4 and res[0]:
                item["toleranceClass"] = f"{_enum_name(res[2], 'FormVariance_')}{_enum_name(res[3], 'Grade_').replace('IT', '')}"
                item["isHole"] = bool(res[1])
        q = _enum_name(obj.GetQualifier(), "DimensionQualifier_")
        if q and q != "None":
            item["qualifier"] = q
        name = _hstr(obj.GetSemanticName())
        if name:
            item["name"] = name
    except Exception:  # noqa: BLE001
        pass
    item["text"] = _dimension_text(item)
    return item


def _dimension_text(item: dict[str, Any]) -> str:
    sym = _DIM_SYMBOL.get(item["type"], "")
    if "value" in item:
        text = f"{sym}{item['value']:g}"
    elif "lower" in item:
        text = f"{sym}{item['lower']:g}..{item['upper']:g}"
    else:
        text = sym or item["type"]
    if "Angular" in item["type"]:
        text += "°"
    if "toleranceClass" in item:
        text += f" {item['toleranceClass']}"
    if "upperTol" in item:
        up, lo = item["upperTol"], item["lowerTol"]
        text += f" +{up:g}/-{abs(lo):g}" if abs(up) != abs(lo) else f" ±{abs(up):g}"
    if item["type"] in ("None", "") or (item.get("value") == 0 and "upperTol" not in item and "lower" not in item):
        return "Dimension (presentation only, no value)"
    label = item["type"].replace("_", " ")
    return f"{label}: {text}" if not sym else text


def _tolerance(obj, datums: list[str]) -> dict[str, Any]:
    ttype = _enum_name(obj.GetType(), "GeomToleranceType_")
    item: dict[str, Any] = {"type": ttype}
    try:
        item["value"] = rnd(obj.GetValue(), 6)
        mat = _enum_name(obj.GetMaterialRequirementModifier(), "GeomToleranceMatReqModif_")
        if mat and mat != "None":
            item["materialModifier"] = mat
        zone = _enum_name(obj.GetZoneModifier(), "GeomToleranceZoneModif_")
        if zone and zone != "None":
            item["zoneModifier"] = zone
        if _enum_name(obj.GetTypeOfValue(), "GeomToleranceTypeValue_") == "Diameter":
            item["diametral"] = True
    except Exception:  # noqa: BLE001
        pass
    if datums:
        item["datums"] = datums
    value = f"{'⌀' if item.get('diametral') else ''}{item.get('value', 0):g}"
    mod = {"M": "Ⓜ", "L": "Ⓛ"}.get(item.get("materialModifier", ""), "")
    item["text"] = " | ".join([ttype, value + mod, *datums])
    return item


def read_pmi(parts: list, face_map, edge_map) -> dict[str, Any]:
    """寸法・幾何公差・データムを読み、指す面・エッジ・部品をビューアの ID にする。"""
    if not parts or parts[0].label is None:
        return {}
    dt = XCAFDoc_DocumentTool.DimTolTool_s(parts[0].label)
    index = _ShapeIndex(parts, face_map, edge_map)
    out: dict[str, list[dict[str, Any]]] = {"dimensions": [], "tolerances": [], "datums": []}

    labels = TDF_LabelSequence()
    dt.GetDatumLabels(labels)
    for i in range(1, labels.Length() + 1):
        lab = labels.Value(i)
        if not lab.IsAttribute(XCAFDoc_Datum.GetID_s()):
            continue
        obj = XCAFDoc_Datum.Set_s(lab).GetObject()
        name = _hstr(obj.GetName()) if obj is not None else ""
        out["datums"].append({"id": f"datum:{len(out['datums'])}", "kind": "datum", "name": name, "text": f"Datum {name}", **_refs(dt, lab, index)})

    labels = TDF_LabelSequence()
    dt.GetDimensionLabels(labels)
    for i in range(1, labels.Length() + 1):
        lab = labels.Value(i)
        if not lab.IsAttribute(XCAFDoc_Dimension.GetID_s()):
            continue
        obj = XCAFDoc_Dimension.Set_s(lab).GetObject()
        if obj is None:
            continue
        out["dimensions"].append({"id": f"dim:{len(out['dimensions'])}", "kind": "dimension", **_dimension(obj), **_refs(dt, lab, index)})

    labels = TDF_LabelSequence()
    dt.GetGeomToleranceLabels(labels)
    for i in range(1, labels.Length() + 1):
        lab = labels.Value(i)
        if not lab.IsAttribute(XCAFDoc_GeomTolerance.GetID_s()):
            continue
        obj = XCAFDoc_GeomTolerance.Set_s(lab).GetObject()
        if obj is None:
            continue
        dseq = TDF_LabelSequence()
        try:
            dt.GetDatumWithObjectOfTolerLabels_s(lab, dseq)
        except Exception:  # noqa: BLE001
            pass
        names = []
        for j in range(1, dseq.Length() + 1):
            d = dseq.Value(j)
            if d.IsAttribute(XCAFDoc_Datum.GetID_s()):
                dobj = XCAFDoc_Datum.Set_s(d).GetObject()
                if dobj is not None and _hstr(dobj.GetName()):
                    names.append(_hstr(dobj.GetName()))
        out["tolerances"].append({"id": f"tol:{len(out['tolerances'])}", "kind": "tolerance", **_tolerance(obj, names), **_refs(dt, lab, index)})
    return {k: v for k, v in out.items() if v}


def _calibrate_dimension_units(dims: list[dict[str, Any]], faces: list[dict[str, Any]]) -> str:
    """寸法の値を、形と同じ単位（mm）にそろえる。

    OCCT 7.9 は、長さの単位がインチ等の STEP で、寸法の値（value・範囲）を換算せずに返す（公差・幾何公差は換算する。
    NIST の FTC-06 で確認）。円柱・球を指す直径・半径の寸法で、形から測った大きさとの比を求め、比がそろっていれば
    長さの寸法すべてに掛ける。確かめられる寸法が無ければ換算しない（推測で数値を変えない）。
    """
    ratios = []
    for d in dims:
        if d.get("type") not in ("Size_Diameter", "Size_Radius") or not d.get("value") or not d.get("faces"):
            continue
        face = faces[d["faces"][0]]
        r = face.get("radius")
        if face.get("geomType") not in ("CYLINDER", "SPHERE") or not r:
            continue
        measured = 2 * r if d["type"] == "Size_Diameter" else r
        ratios.append(measured / d["value"])
    if not ratios:
        return ""
    ratios.sort()
    k = ratios[len(ratios) // 2]
    agree = sum(1 for x in ratios if abs(x / k - 1) < 1e-3)
    if abs(k - 1) < 1e-3 or agree < max(1, len(ratios) * 0.8):
        return ""
    for d in dims:
        if "Angular" in str(d.get("type", "")):
            continue
        for key in ("value", "lower", "upper"):
            if isinstance(d.get(key), (int, float)):
                d[key] = rnd(d[key] * k, 6)
        d["text"] = _dimension_text(d)
    return f"Dimension values were converted to mm (x{k:g}) to match the geometry; the file stores them in another length unit."


def read_layers(parts: list, face_map, edge_map) -> list[dict[str, Any]]:
    """レイヤーの名前と、それに属する部品・面・エッジ。"""
    from OCP.TCollection import TCollection_ExtendedString

    if not parts or parts[0].label is None:
        return []
    lt = XCAFDoc_DocumentTool.LayerTool_s(parts[0].label)
    index = _ShapeIndex(parts, face_map, edge_map)
    labels = TDF_LabelSequence()
    lt.GetLayerLabels(labels)
    out = []
    for i in range(1, labels.Length() + 1):
        name = TCollection_ExtendedString()
        lt.GetLayer(labels.Value(i), name)
        shapes = TDF_LabelSequence()
        lt.GetShapesOfLayer_s(labels.Value(i), shapes)
        refs: dict[str, list[int]] = {}
        for j in range(1, shapes.Length() + 1):
            lab = shapes.Value(j)
            # インスタンスのラベルなら、その部品。
            hit = [pi for pi, p in enumerate(parts) if p.instance is not None and p.instance.IsEqual(lab)]
            if hit:
                refs.setdefault("parts", []).extend(hit)
                continue
            for key, ids in index.lookup(lab).items():
                refs.setdefault(key, []).extend(ids)
        out.append({"name": name.ToExtString(), **{k: sorted(set(v)) for k, v in refs.items()}})
    return out


def attach_step_info(data: dict[str, Any], parts: list, face_map, edge_map, path: str | Path) -> None:
    """STEP のヘッダー・製品の属性・PMI・レイヤーを data["step"] に入れ、面・部品の情報に印を付ける。"""
    info = step_p21.read_step_info(path)
    step: dict[str, Any] = {"header": info.get("header") or {}, "products": info.get("products") or []}
    try:
        pmi = read_pmi(parts, face_map, edge_map)
    except Exception as e:  # noqa: BLE001 - PMI が読めなくても形は見せる
        pmi, step["pmi_error"] = {}, f"{type(e).__name__}: {e}"
    if pmi:
        note = _calibrate_dimension_units(pmi.get("dimensions") or [], data["faces"])
        if note:
            step["pmi_units_note"] = note
        step["pmi"] = pmi
        for items in pmi.values():
            for item in items:
                for fid in item.get("faces", []) + item.get("faces_second", []):
                    data["faces"][fid].setdefault("pmi", []).append(item["id"])
                for eid in item.get("edges", []) + item.get("edges_second", []):
                    data["edgeList"][eid].setdefault("pmi", []).append(item["id"])
    try:
        layers = read_layers(parts, face_map, edge_map)
    except Exception:  # noqa: BLE001
        layers = []
    if layers:
        step["layers"] = layers
    # 部品（アセンブリの葉）に、その製品の品番・説明・属性・材料・レイヤーを付ける。
    by_name = {p.get("name"): p for p in step["products"]}
    for pi, part in enumerate(data.get("parts") or []):
        prod = by_name.get(parts[pi].product) if pi < len(parts) else None
        if prod:
            part["product"] = parts[pi].product
            for key in ("attributes", "material", "validation"):
                if prod.get(key):
                    part[key] = prod[key]
        names = [lay["name"] for lay in layers if pi in lay.get("parts", [])]
        if names:
            part["layers"] = names
    step["warnings"] = _validation_warnings(data, step["products"], parts)
    if not step["warnings"]:
        del step["warnings"]
    data["step"] = step


def _validation_warnings(data: dict[str, Any], products: list[dict[str, Any]], parts: list) -> list[str]:
    """STEP に書かれた検証プロパティ（体積・面積）と、読んだ形から求めた値が違えば知らせる（変換の誤りの目印）。"""
    out = []
    vols: dict[str, float] = {}
    for pi, part in enumerate(data.get("parts") or []):
        if pi < len(parts) and part.get("volume") is not None:
            vols.setdefault(parts[pi].product, float(part["volume"]))
    if not data.get("parts") and parts and (data.get("check") or {}).get("volume") is not None:
        vols[parts[0].product] = float(data["check"]["volume"])
    for prod in products:
        val = prod.get("validation") or {}
        given = next((v for k, v in val.items() if "volume" in str(k).lower() and isinstance(v, (int, float))), None)
        got = vols.get(prod.get("name"))
        if given is None or got is None or got == 0:
            continue
        ratio = float(given) / got
        if abs(ratio - 1) > 1e-3 and abs(ratio * 16387.064 - 1) > 1e-3:  # インチで書かれた値は無視
            out.append(f"Product {prod.get('name')!r}: the STEP file says volume {given:g}, but the geometry read has {got:g} (the file may have been converted wrongly).")
    return out


# ---------------------------------------------------------------------------
# 書き出し
# ---------------------------------------------------------------------------


def write_schema_id(setting: str, source_ap: str | None) -> int:
    """書き出すスキーマ（[cad_viewer] step_write_schema。auto は元と同じ、元が STEP でなければ AP242）の番号。"""
    setting = (setting or "auto").upper()
    if setting in _SCHEMA_IDS:
        return _SCHEMA_IDS[setting]
    if source_ap and source_ap.startswith("AP203"):
        return _SCHEMA_IDS["AP203"]
    if source_ap == "AP214":
        return _SCHEMA_IDS["AP214"]
    return _SCHEMA_IDS["AP242"]


def fix_names(doc) -> None:
    """CadQuery（toCAF）が付けた名前の二重の UTF-8 を直す（日本語の部品名を正しく書くため）。"""
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    from .export_mesh import _label_name

    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    labels = TDF_LabelSequence()
    st.GetShapes(labels)
    todo = [labels.Value(i) for i in range(1, labels.Length() + 1)]
    seen = 0
    while todo and seen < 100_000:
        lab = todo.pop()
        seen += 1
        if lab.IsAttribute(TDataStd_Name.GetID_s()):
            attr = TDataStd_Name()
            lab.FindAttribute(TDataStd_Name.GetID_s(), attr)
            raw = attr.Get().ToExtString()
            fixed = _label_name(lab)
            if fixed != raw:
                TDataStd_Name.Set_s(lab, TCollection_ExtendedString(fixed, True))
        comps = TDF_LabelSequence()
        if st.IsAssembly_s(lab):
            st.GetComponents_s(lab, comps, False)
            todo.extend(comps.Value(i) for i in range(1, comps.Length() + 1))


def write_xcaf(doc, out: Path, schema_id: int) -> bool:
    """XCAF の文書を STEP に書く。戻り値は GD&T（PMI）も書けたか。

    OCCT の GD&T の書き出しは、実際の CAD が書いた PMI の一部（NIST の CTC-01 等）で失敗する。そのときは
    PMI を除いて書き直す（形・名前・色・レイヤー・検証プロパティは残る）。
    """
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Controller

    for dimtol in (True, False):
        STEPControl_Controller.Init_s()
        writer = STEPCAFControl_Writer()
        Interface_Static.SetIVal_s("write.step.schema", schema_id)
        for mode in ("SetColorMode", "SetNameMode", "SetLayerMode", "SetPropsMode"):
            getattr(writer, mode)(True)
        writer.SetDimTolMode(dimtol)
        # 材料は step_p21 で書く（読み込みで OCCT に読ませていないため）。
        writer.SetMaterialMode(False)
        if not writer.Transfer(doc, STEPControl_AsIs):
            raise ValueError("Could not convert the model to STEP.")
        if writer.Write(str(out)) == IFSelect_RetDone:
            return dimtol
    raise ValueError(f"Could not write the STEP file: {out}")


def export_step_xcaf(data: dict[str, Any], parts: list, face_map, out: Path, schema_setting: str) -> list[str]:
    """元の STEP の XCAF の文書に、ビューアの色・隠した部品を当てて書き、属性とヘッダーを足す。"""
    from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
    from OCP.XCAFDoc import XCAFDoc_ColorType

    from .export_mesh import _index_map, _label_color

    doc = parts[0].doc
    st = XCAFDoc_DocumentTool.ShapeTool_s(parts[0].label)
    ct = XCAFDoc_DocumentTool.ColorTool_s(parts[0].label)
    hidden = {int(p) for p in ((data.get("viewState") or {}).get("hiddenParts") or [])}
    part_info = data.get("parts") or []
    n_faces = len(data.get("faces") or [])
    notes: list[str] = []

    def color(hex_color: str):
        r, g, b = (int(hex_color[k : k + 2], 16) / 255 for k in (1, 3, 5))
        return Quantity_Color(r, g, b, Quantity_TOC_sRGB)

    # ビューアで色を変えた面（to が None は色を外した面。元のファイルの面の色は XCAF の文書にもう入っている）。
    changed_faces: dict[int, str | None] = {
        int(c["id"]): (c.get("to") or None) for c in (data.get("edits") or {}).get("color_changes") or [] if c.get("kind") == "face"
    }
    faces_info = data.get("faces") or []
    shared_defs: set[int] = set()
    kept_face_colors: set[int] = set()

    def uses(part) -> int:
        return sum(1 for q in parts if q.label is not None and q.label.IsEqual(part.label))

    def set_or_unset(lab, hex_color: str | None) -> None:
        if hex_color:
            ct.SetColor(lab, color(hex_color), XCAFDoc_ColorType.XCAFDoc_ColorSurf)
        else:
            for kind in (XCAFDoc_ColorType.XCAFDoc_ColorSurf, XCAFDoc_ColorType.XCAFDoc_ColorGen):
                ct.UnSetColor(lab, kind)

    def hex_of(rgb) -> str | None:
        return "#{:02x}{:02x}{:02x}".format(*rgb) if rgb else None

    for pi, part in enumerate(parts):
        info = part_info[pi] if pi < len(part_info) else {}
        if pi in hidden:
            continue
        pc = info.get("color") or None
        before = hex_of(part.color)
        part_changed = (before or "").lower() != (pc or "").lower()
        n_uses = uses(part)
        if part_changed:
            # 読み込みではインスタンスの色が定義の色より優先される（_load_xcaf）。定義を 1 回しか使わない部品は
            # 定義にも付け、何回も使う部品はそのインスタンスにだけ付ける（ほかの配置の色を変えないため）。
            targets = [part.instance] if part.instance is not None else []
            if n_uses == 1 or part.instance is None:
                targets.append(part.label)
            for lab in targets:
                set_or_unset(lab, pc)
        # 面の色。ビューアは部品の色を変えるとその部品の面をすべてその色で描くので、元のファイルで面に個別の色が
        # 付いていた面も合わせる（合わせないと、書き出したファイルではその面だけ前の色のまま残る）。
        # 何回も使う部品の面の色は定義の面に付く（すべての配置に効く）。
        definition = st.GetShape_s(part.label)
        dmap = _index_map(definition, TopAbs_FACE)
        lmap = _index_map(part.shape, TopAbs_FACE)
        for j in range(1, min(dmap.Extent(), lmap.Extent()) + 1):
            fid = face_map.FindIndex(lmap.FindKey(j)) - 1
            if fid < 0 or fid >= n_faces:
                continue
            explicit = fid in changed_faces
            if not explicit and not part_changed:
                continue
            sub = TDF_Label()
            found = st.FindSubShape(part.label, dmap.FindKey(j), sub)
            if explicit:
                want = changed_faces[fid]
            else:
                # 部品の色だけを変えた面: 面に個別の色が付いていれば、ビューアに見えていた色（edited の faces[i].color）にする。
                if not found or sub.IsNull() or _label_color(ct, sub) is None:
                    continue
                if n_uses > 1:
                    # 定義の面の色を変えると、色を変えていない他の配置の面まで変わるので変えない。
                    kept_face_colors.add(pi)
                    continue
                want = (faces_info[fid].get("color") if fid < len(faces_info) else None) or pc
            if not found:
                if not want:
                    continue  # 色の無い面から色を外すことは無い
                sub = st.AddSubShape(part.label, dmap.FindKey(j))
            if sub.IsNull():
                continue
            if want and hex_of(_label_color(ct, sub)) == want.lower():
                continue
            set_or_unset(sub, want)
            if explicit and n_uses > 1:
                shared_defs.add(pi)
    for pi in sorted(hidden):
        part = parts[pi]
        if part.instance is not None:
            st.RemoveComponent(part.instance)
    st.UpdateAssemblies()
    # インスタンスを外して、どこからも使われなくなった定義は消す（残すと単独の形として書き出される）。
    removed: set[int] = set()
    for pi in sorted(hidden):
        part = parts[pi]
        if all(q.label is None or not q.label.IsEqual(part.label) or qi in hidden for qi, q in enumerate(parts)):
            key = id(part.label)
            if key not in removed and st.IsFree_s(part.label):
                st.RemoveShape(part.label, True)
                removed.add(key)
    st.UpdateAssemblies()
    if len(hidden) >= len(parts):
        raise ValueError("Every part is hidden, so there is nothing to write.")

    header = ((data.get("step") or {}).get("header")) or {}
    schema_id = write_schema_id(schema_setting, header.get("ap"))
    tmp = out.with_name(out.stem + ".occt" + out.suffix)
    try:
        pmi_written = write_xcaf(doc, tmp, schema_id)
        text, _encoding = step_p21.read_text(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    products = [p for p in (data.get("step") or {}).get("products") or [] if p.get("name")]
    # 書き出したファイルに残る製品の属性を足す。parts は葉（部品）だけなので、アセンブリの製品も含めるよう、
    # 隠した部品にしか使われていない製品だけを外す（見つからない製品名は add_attributes が飛ばす）。
    visible_products = {parts[pi].product for pi in range(len(parts)) if pi not in hidden}
    dropped = {parts[pi].product for pi in hidden if pi < len(parts)} - visible_products
    attrs = {p["name"]: step_p21.material_rows(p.get("material") or []) + list(p.get("attributes") or []) for p in products if p["name"] not in dropped}
    text, n_attrs = step_p21.add_attributes(text, attrs)
    text = step_p21.set_header(text, header, "Locohane 3D viewer (Open CASCADE)")
    # ASCII だけの本文にする（OCCT が UTF-8 のまま書いた名前・属性を \X2\ にする）。
    out.write_bytes(step_p21.escape_non_ascii(text).encode("ascii", "replace"))

    ap = {3: "AP203", 4: "AP214", 5: "AP242"}[schema_id]
    notes.append(f"Written as STEP {ap}.")
    pmi = (data.get("step") or {}).get("pmi") or {}
    if pmi:
        if schema_id == 5 and not pmi_written:
            notes.append(
                "PMI (GD&T) could not be written for this model (Open CASCADE failed on it), so the file has no PMI. "
                "The original file still has it."
            )
        elif schema_id == 5:
            notes.append("PMI kept: " + ", ".join(f"{len(v)} {k}" for k, v in pmi.items()) + ".")
        else:
            notes.append(f"PMI (GD&T) is not written in {ap}; set [cad_viewer] step_write_schema = AP242 to keep it.")
    if n_attrs:
        notes.append(f"{n_attrs} user-defined attribute(s) written.")
    if hidden:
        notes.append(f"{len(hidden)} hidden part(s) are not in the file.")
    if shared_defs:
        notes.append("Some face colors were set on a part used several times, so they apply to every copy of it.")
    if kept_face_colors:
        notes.append(
            "Some recolored parts are used several times and have faces with their own color; those faces keep their "
            "original color in the file (changing them would recolor every copy)."
        )
    return notes

