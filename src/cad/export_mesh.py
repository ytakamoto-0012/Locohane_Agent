"""B-rep の形（CadQuery / OCP）→ ビューア用データ。cadquery が要る（サブプロセスで import する）。

- 面・エッジ・頂点の番号は、形全体に対する ``TopExp.MapShapes_s`` の順（0 始まり）。CadQuery の
  ``shape.Faces()`` / ``Workplane.faces().vals()`` と同じ順になる（tests/test_cad_export_mesh.py で確認）。
- 面に属さないエッジ・エッジに属さない頂点は ``"free": true``（ワイヤーやスケッチだけの形、DXF の開いた線・点）。
- STEP / IGES は XCAF で読み、アセンブリの部品（名前・配置・色）を「部品」として残す。DXF はレイヤーを部品とする。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

import cadquery as cq
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.Bnd import Bnd_Box
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_VERTEX
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape

from .features import model_digest, recognize_features
from .mesh_data import TooManyTrianglesError, build_viewer_data, hex_color, rnd, vec

# セレクタ候補を計算する上限（面・エッジ・頂点それぞれの数）。超えたら候補を付けない（時間がかかるため）。
SELECTOR_HINT_MAX_ENTITIES = 2000
# 読み込んだ CAD ファイルで B-rep の正しさの検査（BRepCheck_Analyzer）をする面の数の上限。超えたら省く
# （部品の多いアセンブリでは 1 万面あたり 10 秒ほどかかる。検査は CadQuery のコードで作った形の確認が主な目的なので、
# コードの形は面の数によらず検査する）。
BREP_CHECK_MAX_FILE_FACES = 5000
# 三角形に分ける前に正確な外接箱を求める面の数の上限（超えたら頂点の外接箱で分ける細かさを決める。shapes_to_viewer_data）。
EXACT_BBOX_MAX_FACES = 2000
# ">Z[k]" のような何番目かのセレクタを試す段数の上限（軸ごと）。
_NTH_SELECTOR_LEVELS = 12


@dataclass
class Part:
    """アセンブリの部品（STEP/IGES の XCAF の葉、DXF のレイヤー、コード実行では 1 つ）。"""

    name: str
    path: str
    shape: TopoDS_Shape
    color: tuple[int, int, int] | None = None
    # 部品の中の面ごとの色（XCAF で面に色が付いている場合）。キーは TopoDS_Face（配置後）。
    face_colors: list[tuple[TopoDS_Shape, tuple[int, int, int]]] = field(default_factory=list)
    # DXF のレイヤーの設定（色・線種・非表示・フリーズ・ロック）。ビューアのサイドバーに出し、
    # 非表示・フリーズのレイヤーは最初から隠す。DXF 以外は None。
    layer: dict[str, Any] | None = None
    # STEP / IGES（XCAF）の部品だけ: 形の定義のラベル、アセンブリの中のインスタンスのラベル（無ければ None）、
    # 製品の名前（インスタンスの名前と違うことがある）、配置、ラベルを持つ XCAF の文書（ラベルを使う間は生かしておく）。
    label: Any = None
    instance: Any = None
    product: str = ""
    loc: Any = None
    doc: Any = None


@dataclass
class Options:
    linear_deflection_ratio: float = 0.001
    angular_deflection_deg: float = 20.0
    max_triangles: int = 1_000_000
    # CadQuery のセレクタの候補を求めるか（比べる前の形のように、選ばせない形では求めない）。
    selector_hints: bool = True


class CadLoadError(ValueError):
    """CAD ファイルが読めない・形が空。"""


# ---------------------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------------------


def load_cad_file(path: str | Path, fmt: str) -> tuple[list[Part], str]:
    """B-rep 系の CAD ファイルを部品の一覧として読む。戻り値は (部品, 単位)。"""
    path = str(path)
    if fmt in ("STEP", "IGES"):
        return _load_xcaf(path, fmt), "mm"
    if fmt == "BREP":
        return [Part("model", "model", cq.importers.importBrep(path).val().wrapped)], "unknown"
    if fmt == "BIN":
        return [Part("model", "model", cq.importers.importBin(path).val().wrapped)], "unknown"
    if fmt == "DXF":
        return _load_dxf(path), "unknown"
    raise CadLoadError(f"Unsupported format: {fmt}")


def _label_name(label) -> str:
    from OCP.TDataStd import TDataStd_Name

    # 属性が無いラベルに FindAttribute を呼ぶと OCP がプロセスごと落ちることがあるので、先に有無を見る。
    if not label.IsAttribute(TDataStd_Name.GetID_s()):
        return ""
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        from .step_p21 import repair_mojibake

        # CadQuery が書いた STEP の日本語名は二重の UTF-8 になっている（step_p21.repair_mojibake）。
        return repair_mojibake(attr.Get().ToExtString())
    return ""


def _label_color(color_tool, label) -> tuple[int, int, int] | None:
    from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
    from OCP.XCAFDoc import XCAFDoc_ColorType

    color = Quantity_Color()
    for kind in (XCAFDoc_ColorType.XCAFDoc_ColorSurf, XCAFDoc_ColorType.XCAFDoc_ColorGen):
        if color_tool.GetColor_s(label, kind, color):
            r, g, b = color.Values(Quantity_TOC_sRGB)
            return (round(r * 255), round(g * 255), round(b * 255))
    return None


def _load_xcaf(path: str, fmt: str) -> list[Part]:
    """STEP / IGES を XCAF で読み、アセンブリの葉（部品）を配置済みの形で返す。"""
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    if fmt == "STEP":
        from OCP.STEPCAFControl import STEPCAFControl_Reader as Reader
    else:
        from OCP.IGESCAFControl import IGESCAFControl_Reader as Reader

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    reader = Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    reader.SetLayerMode(True)
    if fmt == "STEP":
        # GD&T（寸法・幾何公差・データム）と検証プロパティ（体積・面積・重心）も読む。
        # ユーザー定義属性は Python から一覧できず、材料は OCCT が製品に付いた文字列の属性を名前によらず材料と
        # みなしてしまうので、どちらもここでは読まず src/cad/step_p21.py で P21 から読む（保存時もそちらで書く）。
        reader.SetGDTMode(True)
        reader.SetMatMode(False)
        reader.SetPropsMode(True)
    if reader.ReadFile(path) != IFSelect_RetDone:
        raise CadLoadError(f"Could not read the {fmt} file: {path}")
    if not reader.Transfer(doc):
        raise CadLoadError(f"Could not convert the {fmt} file to shapes: {path}")
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    parts: list[Part] = []

    def walk(label, loc: TopLoc_Location, names: list[str], inherited: tuple[int, int, int] | None) -> None:
        name = _label_name(label)
        own_color = _label_color(color_tool, label) or inherited
        instance = None
        if shape_tool.IsReference_s(label):
            instance = label
            ref = TDF_Label()
            shape_tool.GetReferredShape_s(label, ref)
            loc = loc.Multiplied(shape_tool.GetLocation_s(label))
            ref_name = _label_name(ref)
            label = ref
            name = name or ref_name
            # インスタンス（配置）に付いた色は、定義の色より優先する（同じ部品を何回も使うとき、配置ごとに色を変えられる。
            # ビューアの「保存」も、何回も使う部品の色はインスタンスに付ける。step_xcaf.export_step_xcaf）。
            own_color = _label_color(color_tool, instance) or _label_color(color_tool, ref) or inherited
        path_names = [*names, name or f"part{len(parts) + 1}"]
        if shape_tool.IsAssembly_s(label):
            comps = TDF_LabelSequence()
            shape_tool.GetComponents_s(label, comps, False)
            for i in range(1, comps.Length() + 1):
                walk(comps.Value(i), loc, path_names, own_color)
            return
        shape = shape_tool.GetShape_s(label)
        if shape.IsNull():
            return
        located = shape.Moved(loc) if not loc.IsIdentity() else shape
        part = Part(
            path_names[-1], "/".join(path_names), located, own_color,
            label=label, instance=instance, product=_label_name(label) or path_names[-1], loc=loc, doc=doc,
        )
        # 面（サブシェイプ）単位の色。
        subs = TDF_LabelSequence()
        shape_tool.GetSubShapes_s(label, subs)
        for i in range(1, subs.Length() + 1):
            sub_label = subs.Value(i)
            sub_color = _label_color(color_tool, sub_label)
            if sub_color is None:
                continue
            sub_shape = shape_tool.GetShape_s(sub_label)
            sub_located = sub_shape.Moved(loc) if not loc.IsIdentity() else sub_shape
            part.face_colors.append((sub_located, sub_color))
        parts.append(part)

    roots = TDF_LabelSequence()
    shape_tool.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), TopLoc_Location(), [], None)
    if not parts:
        raise CadLoadError(f"The {fmt} file has no shapes: {path}")
    if fmt == "IGES":
        for part in parts:
            _sew(part)
    return parts


def _sew(part: Part) -> None:
    """IGES の面を縫い合わせる（IGES は面どうしのつながりを持たないことが多く、そのままでは
    隣り合う面がエッジ・頂点を共有せず、同じ位置に別々のエッジが重なって選びにくいため）。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    sewing = BRepBuilderAPI_Sewing()
    sewing.Add(part.shape)
    sewing.Perform()
    sewed = sewing.SewedShape()
    if sewed.IsNull():
        return
    part.face_colors = [(sewing.Modified(s) if sewing.IsModified(s) else s, c) for s, c in part.face_colors]
    part.shape = sewed


def _load_dxf(path: str) -> list[Part]:
    """DXF をレイヤーごとの部品として読む。閉じた輪郭は面、開いた線は単独カーブ、POINT は単独の点。"""
    import ezdxf
    from cadquery.occ_impl.importers.dxf import _dxf_convert
    from cadquery.occ_impl.shapes import sortWiresByBuildOrder

    doc = ezdxf.readfile(path)
    parts: list[Part] = []
    for layer, entities in doc.modelspace().groupby(dxfattrib="layer").items():
        shapes: list[cq.Shape] = []
        wires = _dxf_convert(entities, 1e-6)
        closed = [w for w in wires if w.IsClosed()]
        shapes.extend(w for w in wires if not w.IsClosed())
        if closed:
            for wire_set in sortWiresByBuildOrder(closed):
                try:
                    shapes.append(cq.Face.makeFromWires(wire_set[0], wire_set[1:]))
                except Exception:  # noqa: BLE001 - 面を張れない輪郭は線のまま見せる
                    shapes.extend(wire_set)
        for el in entities:
            if el.dxftype() == "POINT":
                shapes.append(cq.Vertex.makeVertex(*el.dxf.location.xyz))
        if shapes:
            settings, color = _dxf_layer_settings(doc, layer)
            parts.append(Part(layer, layer, cq.Compound.makeCompound(shapes).wrapped, color, layer=settings))
    if not parts:
        raise CadLoadError(f"The DXF file has no lines, curves or points that can be shown: {path}")
    return parts


def _dxf_layer_settings(doc, name: str) -> tuple[dict[str, Any], tuple[int, int, int] | None]:
    """DXF のレイヤー表の設定と、形に付ける色（ACI 7 = 白/黒は背景で変わる色なので付けない）。"""
    from ezdxf.colors import aci2rgb

    settings: dict[str, Any] = {"color": None, "aci": None, "linetype": None, "off": False, "frozen": False, "locked": False}
    if not doc.layers.has_entry(name):
        return settings, None
    entry = doc.layers.get(name)
    rgb = entry.rgb
    aci = entry.color
    if rgb is None and aci not in (None, 0, 7, 256):
        rgb = tuple(aci2rgb(aci))
    color = tuple(int(c) for c in rgb) if rgb is not None else None
    settings.update(
        color=hex_color(color) if color else None,
        aci=aci,
        linetype=entry.dxf.get("linetype"),
        off=bool(entry.is_off()),
        frozen=bool(entry.is_frozen()),
        locked=bool(entry.is_locked()),
    )
    return settings, color


def to_shape(obj: Any) -> cq.Shape | None:
    """CadQuery コードの結果（Workplane・Shape・Sketch・Assembly・Vector・Location・それらのリスト）を形にする。"""
    if obj is None:
        return None
    if isinstance(obj, cq.Shape):
        return obj
    if isinstance(obj, TopoDS_Shape):
        return cq.Shape.cast(obj)
    if isinstance(obj, cq.Assembly):
        return obj.toCompound()
    if isinstance(obj, cq.Sketch):
        faces = getattr(obj, "_faces", None)
        return cq.Shape.cast(faces.wrapped) if faces is not None else None
    if isinstance(obj, cq.Vector):
        return cq.Vertex.makeVertex(obj.x, obj.y, obj.z)
    if isinstance(obj, cq.Location):
        return to_shape(cq.Vector(obj.toTuple()[0]))
    if isinstance(obj, cq.Workplane):
        return to_shape(list(obj.vals()))
    if isinstance(obj, (list, tuple)):
        shapes = [s for s in (to_shape(o) for o in obj) if s is not None]
        if not shapes:
            return None
        return shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes)
    return None


# ---------------------------------------------------------------------------
# ビューア用データ
# ---------------------------------------------------------------------------


def _index_map(shape: TopoDS_Shape, kind) -> TopTools_IndexedMapOfShape:
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m


def _adjacency(
    parent_map: TopTools_IndexedMapOfShape, kind, child_map: TopTools_IndexedMapOfShape, seam_of: bool = False
) -> tuple[list[list[int]], list[bool]]:
    """子（エッジ・頂点）ごとの、それを含む親（面・エッジ）の ID の一覧（昇順）。

    親ごとに子の小さな索引を作って逆に引く。``TopExp.MapShapesAndAncestors_s`` の結果の形のリストを Python で回すと
    1 件ごとに遅く（部品の多い STEP で数十秒かかった）、こちらは同じ結果を数十分の一の時間で出す。
    seam_of=True（親が面）のときは、その面の継ぎ目（円筒・球を閉じるため同じ面に 2 回属するエッジ）かも返す。
    """
    children: list[list[int]] = [[] for _ in range(child_map.Extent())]
    seam = [False] * child_map.Extent()
    for pi in range(1, parent_map.Extent() + 1):
        parent = parent_map.FindKey(pi)
        face = TopoDS.Face_s(parent) if seam_of else None
        sub = _index_map(parent, kind)
        for j in range(1, sub.Extent() + 1):
            child = sub.FindKey(j)
            ci = child_map.FindIndex(child) - 1
            if ci < 0:
                continue
            children[ci].append(pi - 1)
            if face is not None and BRep_Tool.IsClosed_s(TopoDS.Edge_s(child), face):
                seam[ci] = True
    return children, seam


def _diag(shape: TopoDS_Shape, *, exact: bool) -> float:
    """外接箱の対角の長さ。

    exact=True: 正確な外接箱（BRepBndLib.AddOptimal）。三角形に分けた後なら三角形を使うのですぐ終わる。
    exact=False: 頂点の点の外接箱（速い）。ふつうの形は角が頂点なので正確な値とほぼ同じになる。曲面が頂点の外へ大きく
    張り出す形（球・トーラス・継ぎ目だけの円筒など）で頂点の箱が形の定義からの粗い箱の半分に満たないときは、粗い箱を使う。
    """
    if exact:
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(shape, box)
        return _box_diag(box)
    points = Bnd_Box()
    vertices = _index_map(shape, TopAbs_VERTEX)
    for i in range(1, vertices.Extent() + 1):
        points.Add(BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertices.FindKey(i))))
    rough = Bnd_Box()
    BRepBndLib.Add_s(shape, rough, False)
    by_points, by_geometry = _box_diag(points, 0.0), _box_diag(rough)
    return by_points if by_points >= by_geometry * 0.5 else by_geometry


def _box_diag(box: Bnd_Box, empty: float = 1.0) -> float:
    if box.IsVoid():
        return empty
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) or empty


def _surface_props(f: TopoDS_Shape) -> tuple[float, tuple[float, float, float]]:
    """面の (面積, 重心)。CadQuery の Face.Area() と Face.Center() は同じ積分を別々にするので、1 回で両方を取る。"""
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(f, props)
    c = props.CentreOfMass()
    return props.Mass(), (c.X(), c.Y(), c.Z())


def _pnt(p) -> list[float]:
    return vec((p.X(), p.Y(), p.Z()))


def _dir(d) -> list[float]:
    return vec((d.X(), d.Y(), d.Z()))


_SURFACE_NAMES = {
    GeomAbs_SurfaceType.GeomAbs_Plane: "PLANE",
    GeomAbs_SurfaceType.GeomAbs_Cylinder: "CYLINDER",
    GeomAbs_SurfaceType.GeomAbs_Cone: "CONE",
    GeomAbs_SurfaceType.GeomAbs_Sphere: "SPHERE",
    GeomAbs_SurfaceType.GeomAbs_Torus: "TORUS",
    GeomAbs_SurfaceType.GeomAbs_BezierSurface: "BEZIER",
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "BSPLINE",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution: "REVOLUTION",
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion: "EXTRUSION",
    GeomAbs_SurfaceType.GeomAbs_OffsetSurface: "OFFSET",
}
_CURVE_NAMES = {
    GeomAbs_CurveType.GeomAbs_Line: "LINE",
    GeomAbs_CurveType.GeomAbs_Circle: "CIRCLE",
    GeomAbs_CurveType.GeomAbs_Ellipse: "ELLIPSE",
    GeomAbs_CurveType.GeomAbs_Hyperbola: "HYPERBOLA",
    GeomAbs_CurveType.GeomAbs_Parabola: "PARABOLA",
    GeomAbs_CurveType.GeomAbs_BezierCurve: "BEZIER",
    GeomAbs_CurveType.GeomAbs_BSplineCurve: "BSPLINE",
    GeomAbs_CurveType.GeomAbs_OffsetCurve: "OFFSET",
}


def _face_info(face_id: int, f: TopoDS_Shape) -> tuple[dict[str, Any], float]:
    """面の情報と、丸める前の面積（形全体の表面積の合計に使う）。"""
    face = cq.Face(TopoDS.Face_s(f))
    adaptor = BRepAdaptor_Surface(face.wrapped)
    stype = adaptor.GetType()
    info: dict[str, Any] = {"id": face_id, "kind": "face", "geomType": _SURFACE_NAMES.get(stype, "OTHER")}
    area, center_xyz = _surface_props(face.wrapped)
    info["area"] = rnd(area)
    center = cq.Vector(*center_xyz)
    info["center"] = vec(center_xyz)
    try:
        info["normal" if stype == GeomAbs_SurfaceType.GeomAbs_Plane else "normalAtCenter"] = vec(face.normalAt(center).toTuple())
    except Exception:  # noqa: BLE001 - 退化した面は法線を出さない
        pass
    if stype == GeomAbs_SurfaceType.GeomAbs_Plane:
        info["planeOffset"] = rnd(float(np.dot(info["normal"], info["center"]))) if "normal" in info else None
    elif stype == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        cyl = adaptor.Cylinder()
        info.update(radius=rnd(cyl.Radius()), axisOrigin=_pnt(cyl.Location()), axisDirection=_dir(cyl.Axis().Direction()))
        concave = _cylinder_concave(face.wrapped, adaptor, cyl)
        if concave is not None:
            # true = 内向き（穴の内側の面）、false = 外向き（丸棒・ボスの外側の面）。
            info["concave"] = concave
    elif stype == GeomAbs_SurfaceType.GeomAbs_Cone:
        cone = adaptor.Cone()
        info.update(
            refRadius=rnd(cone.RefRadius()),
            semiAngleDeg=rnd(math.degrees(cone.SemiAngle())),
            apex=_pnt(cone.Apex()),
            axisDirection=_dir(cone.Axis().Direction()),
        )
    elif stype == GeomAbs_SurfaceType.GeomAbs_Sphere:
        sph = adaptor.Sphere()
        info.update(radius=rnd(sph.Radius()), sphereCenter=_pnt(sph.Location()))
    elif stype == GeomAbs_SurfaceType.GeomAbs_Torus:
        tor = adaptor.Torus()
        info.update(
            majorRadius=rnd(tor.MajorRadius()),
            minorRadius=rnd(tor.MinorRadius()),
            axisOrigin=_pnt(tor.Location()),
            axisDirection=_dir(tor.Axis().Direction()),
        )
    bb = face.BoundingBox()
    info["bbox"] = {"min": vec((bb.xmin, bb.ymin, bb.zmin)), "max": vec((bb.xmax, bb.ymax, bb.zmax)), "size": vec((bb.xlen, bb.ylen, bb.zlen))}
    return info, area


def _cylinder_concave(face: TopoDS_Shape, adaptor: BRepAdaptor_Surface, cyl) -> bool | None:
    """円柱面の外向きの法線（面の向きを考える）が軸の方を向いていれば True（穴）、外を向いていれば False。"""
    from OCP.BRepLProp import BRepLProp_SLProps

    try:
        u = (adaptor.FirstUParameter() + adaptor.LastUParameter()) / 2
        v = (adaptor.FirstVParameter() + adaptor.LastVParameter()) / 2
        props = BRepLProp_SLProps(adaptor, u, v, 1, 1e-9)
        if not props.IsNormalDefined():
            return None
        n = np.array(_xyz(props.Normal()))
        if face.Orientation() == TopAbs_REVERSED:
            n = -n
        p = np.array(_xyz(props.Value()))
        o = np.array(_xyz(cyl.Location()))
        d = np.array(_xyz(cyl.Axis().Direction()))
        radial = (p - o) - d * float((p - o) @ d)
        return bool(float(n @ radial) < 0)
    except Exception:  # noqa: BLE001 - 求められなければ付けない
        return None


def _edge_info(edge_id: int, e: TopoDS_Shape) -> dict[str, Any]:
    edge = cq.Edge(TopoDS.Edge_s(e))
    info: dict[str, Any] = {"id": edge_id, "kind": "edge"}
    if BRep_Tool.Degenerated_s(edge.wrapped):
        info.update(geomType="DEGENERATED", length=0.0)
        return info
    adaptor = BRepAdaptor_Curve(edge.wrapped)
    ctype = adaptor.GetType()
    info["geomType"] = _CURVE_NAMES.get(ctype, "OTHER")
    info["length"] = rnd(edge.Length())
    info["start"] = vec(edge.startPoint().toTuple())
    info["end"] = vec(edge.endPoint().toTuple())
    info["closed"] = bool(edge.IsClosed())
    if ctype == GeomAbs_CurveType.GeomAbs_Line:
        d = np.subtract(info["end"], info["start"])
        n = float(np.linalg.norm(d))
        if n > 0:
            info["direction"] = vec(d / n)
    elif ctype == GeomAbs_CurveType.GeomAbs_Circle:
        circ = adaptor.Circle()
        info.update(radius=rnd(circ.Radius()), circleCenter=_pnt(circ.Location()), axisDirection=_dir(circ.Axis().Direction()))
    elif ctype == GeomAbs_CurveType.GeomAbs_Ellipse:
        el = adaptor.Ellipse()
        info.update(majorRadius=rnd(el.MajorRadius()), minorRadius=rnd(el.MinorRadius()), ellipseCenter=_pnt(el.Location()))
    return info


def _triangulate_face(f: TopoDS_Shape) -> tuple[np.ndarray, np.ndarray]:
    """メッシュ済みの面の (頂点 (k,3), 三角形 (t,3)) を返す（三角形が無ければ空）。"""
    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(TopoDS.Face_s(f), loc)
    if tri is None:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    trsf = loc.Transformation()
    nodes = np.array([_xyz(tri.Node(i).Transformed(trsf)) for i in range(1, tri.NbNodes() + 1)])
    tris = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)], dtype=np.int64) - 1
    if f.Orientation() == TopAbs_REVERSED:
        tris = tris[:, [0, 2, 1]]
    return nodes, tris


def _xyz(p) -> tuple[float, float, float]:
    return (p.X(), p.Y(), p.Z())


def _polyline(e: TopoDS_Shape, lin: float, ang: float) -> np.ndarray:
    """エッジを折れ線の点列にする（退化したエッジは空）。"""
    edge = TopoDS.Edge_s(e)
    if BRep_Tool.Degenerated_s(edge):
        return np.zeros((0, 3))
    adaptor = BRepAdaptor_Curve(edge)
    disc = GCPnts_TangentialDeflection(adaptor, ang, lin)
    pts = [_xyz(disc.Value(i)) for i in range(1, disc.NbPoints() + 1)]
    return np.array(pts) if len(pts) >= 2 else np.zeros((0, 3))


def _selector_hints(compound: cq.Shape, kind: str, count: int) -> dict[int, list[str]]:
    """CadQuery のセレクタを実際に当て、要素 1 つだけに一致したものを {要素ID: [セレクタ, ...]} で返す。"""
    if count == 0 or count > SELECTOR_HINT_MAX_ENTITIES:
        return {}
    wp = cq.Workplane("XY").add(compound)
    pick = {"face": wp.faces, "edge": wp.edges, "vertex": wp.vertices}[kind]
    base_map = _index_map(compound.wrapped, {"face": TopAbs_FACE, "edge": TopAbs_EDGE, "vertex": TopAbs_VERTEX}[kind])
    candidates: list[str] = []
    for axis in "XYZ":
        candidates += [f">{axis}", f"<{axis}"]
        if kind != "vertex":
            candidates += [f"|{axis}"]
        if kind == "face":
            candidates += [f"#{axis}"]
    if kind == "face":
        candidates += [f"%{n}" for n in ("PLANE", "CYLINDER", "CONE", "SPHERE", "TORUS", "BSPLINE")]
    elif kind == "edge":
        candidates += [f"%{n}" for n in ("LINE", "CIRCLE", "ELLIPSE", "BSPLINE")]
    hints: dict[int, list[str]] = {}

    def try_selector(sel: str) -> int:
        try:
            vals = pick(sel).vals()
        except Exception:  # noqa: BLE001 - その形に当てられないセレクタは候補にしない
            return -1
        if len(vals) == 1 and isinstance(vals[0], cq.Shape):
            idx = base_map.FindIndex(vals[0].wrapped) - 1
            if idx >= 0:
                hints.setdefault(idx, []).append(sel)
        return len(vals)

    for sel in candidates:
        try_selector(sel)
    # ">Z[1]" のような「上から何番目」のセレクタ（面が同じ高さに並ぶ箱等でよく使う）。
    for axis in "XYZ":
        for k in range(_NTH_SELECTOR_LEVELS):
            if try_selector(f">{axis}[{k}]") <= 0:
                break
    return hints


def shapes_to_viewer_data(parts: list[Part], *, source: dict[str, Any], units: str, options: Options) -> dict[str, Any]:
    """部品の一覧をビューア用データにする。"""
    compound = cq.Compound.makeCompound([cq.Shape.cast(p.shape) for p in parts])
    root = compound.wrapped
    face_map = _index_map(root, TopAbs_FACE)
    edge_map = _index_map(root, TopAbs_EDGE)
    vertex_map = _index_map(root, TopAbs_VERTEX)
    n_faces, n_edges, n_vertices = face_map.Extent(), edge_map.Extent(), vertex_map.Extent()
    if n_faces + n_edges + n_vertices == 0:
        raise CadLoadError("The shape is empty (no faces, edges or vertices).")

    # 三角形に分ける細かさの目安にする大きさ。分ける前の正確な外接箱（AddOptimal）は形全体の積分になり、
    # 部品の多いアセンブリでは十数秒かかるので、面が多いときは頂点から求める（_diag の exact=False）。
    diag = _diag(root, exact=n_faces <= EXACT_BBOX_MAX_FACES)
    ang = math.radians(options.angular_deflection_deg)
    lin = diag * options.linear_deflection_ratio

    # 三角形が上限を超えたら粗くして作り直す（3 回まで）。
    for _attempt in range(4):
        BRepMesh_IncrementalMesh(root, lin, False, ang, True)
        face_meshes = [_triangulate_face(face_map.FindKey(i)) for i in range(1, n_faces + 1)]
        total = sum(len(t) for _, t in face_meshes)
        if options.max_triangles <= 0 or total <= options.max_triangles:
            break
        lin *= 4
        ang = min(ang * 2, math.radians(60))
        from OCP.BRepTools import BRepTools

        BRepTools.Clean_s(root)
    else:
        raise TooManyTrianglesError(
            f"The shape needs {total} triangles even with a coarse tessellation, more than [cad_viewer] max_triangles ({options.max_triangles})."
        )

    # 部品と色（面ごと）。DXF はレイヤーが 1 つでも部品として残す（サイドバーにレイヤーの設定を出すため）。
    with_parts = len(parts) > 1 or any(p.layer is not None for p in parts)
    face_part = np.full(n_faces, -1, dtype=np.int64)
    edge_part = np.full(n_edges, -1, dtype=np.int64)
    vertex_part = np.full(n_vertices, -1, dtype=np.int64)
    face_color: list[tuple[int, int, int] | None] = [None] * n_faces
    for pi, part in enumerate(parts):
        for i in _ids_in(part.shape, TopAbs_FACE, face_map):
            face_part[i] = pi
            face_color[i] = part.color
        for i in _ids_in(part.shape, TopAbs_EDGE, edge_map):
            edge_part[i] = pi
        for i in _ids_in(part.shape, TopAbs_VERTEX, vertex_map):
            vertex_part[i] = pi
        for sub_shape, color in part.face_colors:
            for i in _ids_in(sub_shape, TopAbs_FACE, face_map):
                face_color[i] = color
    any_color = any(c is not None for c in face_color)

    positions: list[np.ndarray] = []
    indices: list[np.ndarray] = []
    colors: list[np.ndarray] = []
    face_ranges: list[list[int]] = []
    tri_offset = 0
    node_offset = 0
    default_rgb = np.array([180, 180, 180], dtype=np.uint8)
    for i, (nodes, tris) in enumerate(face_meshes):
        face_ranges.append([tri_offset, len(tris)])
        if len(tris):
            positions.append(nodes)
            indices.append(tris + node_offset)
            if any_color:
                rgb = np.array(face_color[i], dtype=np.uint8) if face_color[i] is not None else default_rgb
                colors.append(np.tile(rgb, (len(nodes), 1)))
            node_offset += len(nodes)
            tri_offset += len(tris)

    edge_faces, edge_seam = _adjacency(face_map, TopAbs_EDGE, edge_map, seam_of=True)
    vertex_edges, _ = _adjacency(edge_map, TopAbs_VERTEX, vertex_map)

    seg_points: list[np.ndarray] = []
    edge_ranges: list[list[int]] = []
    edges: list[dict] = []
    face_edges: list[set[int]] = [set() for _ in range(n_faces)]
    seg_offset = 0
    for i in range(1, n_edges + 1):
        e = edge_map.FindKey(i)
        pts = _polyline(e, lin, ang)
        segs = np.stack([pts[:-1], pts[1:]], axis=1).reshape(-1, 3) if len(pts) >= 2 else np.zeros((0, 3))
        nseg = len(segs) // 2
        edge_ranges.append([seg_offset, nseg])
        seg_offset += nseg
        if nseg:
            seg_points.append(segs)
        info = _edge_info(i - 1, e)
        adj = edge_faces[i - 1]
        info["faces"] = adj
        for f in adj:
            face_edges[f].add(i - 1)
        if not adj:
            info["free"] = True
        elif len(adj) == 1 and edge_seam[i - 1]:
            # 同じ面に 2 回属するエッジ（円筒・球の継ぎ目）。形の角ではなく、面を閉じるための線。
            info["seam"] = True
        if edge_part[i - 1] >= 0 and with_parts:
            info["part"] = int(edge_part[i - 1])
        edges.append(info)

    faces: list[dict] = []
    surface_area = 0.0
    for i in range(1, n_faces + 1):
        info, area = _face_info(i - 1, face_map.FindKey(i))
        surface_area += area
        info["edges"] = sorted(face_edges[i - 1])
        if face_color[i - 1] is not None:
            info["color"] = hex_color(face_color[i - 1])
        if with_parts and face_part[i - 1] >= 0:
            info["part"] = int(face_part[i - 1])
        faces.append(info)

    vertices: list[dict] = []
    vertex_points = []
    for i in range(1, n_vertices + 1):
        v = vertex_map.FindKey(i)
        p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(v))
        vertex_points.append(_xyz(p))
        adj = vertex_edges[i - 1]
        info = {"id": i - 1, "kind": "vertex", "point": vec(_xyz(p)), "edges": adj}
        if not adj:
            info["free"] = True
        if with_parts and vertex_part[i - 1] >= 0:
            info["part"] = int(vertex_part[i - 1])
        vertices.append(info)

    if options.selector_hints:
        for kind, items in (("face", faces), ("edge", edges), ("vertex", vertices)):
            for idx, sels in _selector_hints(compound, kind, len(items)).items():
                items[idx]["selectors"] = sels
    # 穴・ボスの判定の許容差には、三角形に分けた後の正確な大きさを使う（三角形があるのですぐ求まる）。
    features = recognize_features(faces, edges, _diag(root, exact=True))

    data = build_viewer_data(
        source=source,
        units=units,
        positions=np.concatenate(positions).astype(np.float32) if positions else np.zeros((0, 3), np.float32),
        indices=np.concatenate(indices).astype(np.uint32) if indices else np.zeros((0, 3), np.uint32),
        face_ranges=face_ranges,
        colors=np.concatenate(colors) if colors else None,
        edge_positions=np.concatenate(seg_points).astype(np.float32) if seg_points else np.zeros((0, 3), np.float32),
        edge_ranges=edge_ranges,
        vertex_positions=np.array(vertex_points, dtype=np.float32).reshape(-1, 3),
        faces=faces,
        edges=edges,
        vertices=vertices,
    )
    if with_parts:
        data["parts"] = [_part_info(pi, part, face_part, edge_part) for pi, part in enumerate(parts)]
        data["stats"]["parts"] = len(parts)
    data["check"] = check_shape(
        compound,
        edges,
        n_parts=len(parts),
        surface_area=surface_area,
        validate=source.get("kind") != "file" or n_faces <= BREP_CHECK_MAX_FILE_FACES,
    )
    data["features"] = features
    data["modelDigest"] = model_digest(faces, edges, vertices)
    if source.get("format") == "STEP" and source.get("path") and parts and parts[0].label is not None:
        from .step_xcaf import attach_step_info

        attach_step_info(data, parts, face_map, edge_map, source["path"])
    return data


def check_shape(
    compound: cq.Shape, edges: list[dict], n_parts: int = 1, *, surface_area: float | None = None, validate: bool = True
) -> dict[str, Any]:
    """形の検査（ソリッドの数・正しい B-rep か・閉じているか・体積・表面積）と、気づいた点。

    CadQuery の union が部品の一部を落としても valid のまま、のように「形が違うのに成功する」ことがあるため、
    数値を LLM へ必ず返す（絵より先に数値で確かめさせる）。
    surface_area: 面ごとに求めた面積の合計（あれば形全体をもう一度積分しない）。
    validate=False: B-rep の正しさの検査を省く（面の多い CAD ファイル。valid は None、validSkipped に理由）。
    """
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID

    root = compound.wrapped
    solids = _index_map(root, TopAbs_SOLID).Extent()
    shells = _index_map(root, TopAbs_SHELL).Extent()
    out: dict[str, Any] = {"solids": solids, "shells": shells}
    if not validate:
        out["valid"] = None
        out["validSkipped"] = f"not checked: the file has more than {BREP_CHECK_MAX_FILE_FACES} faces (the check takes long on large models)"
    else:
        try:
            out["valid"] = bool(BRepCheck_Analyzer(root).IsValid())
        except Exception:  # noqa: BLE001
            out["valid"] = None
    # 面 1 つにしか属さないエッジ（継ぎ目・単独の線を除く）。ソリッドなら 0 のはず。
    open_edges = sum(
        1 for e in edges if len(e.get("faces") or []) == 1 and not e.get("seam") and not e.get("free") and e.get("geomType") != "DEGENERATED"
    )
    out["openEdges"] = open_edges
    if solids:
        try:
            out["volume"] = rnd(sum(cq.Solid(s).Volume() for s in _solids(root)), 4)
        except Exception:  # noqa: BLE001
            pass
    try:
        out["surfaceArea"] = rnd(compound.Area() if surface_area is None else surface_area, 4)
    except Exception:  # noqa: BLE001
        pass
    warnings: list[str] = []
    if out["valid"] is False:
        warnings.append("The shape is not a valid B-rep (BRepCheck failed): booleans, fillets and export may give wrong results.")
    if solids and open_edges:
        warnings.append(f"{open_edges} edges belong to only one face: the solid is not closed (a face may be missing).")
    if solids > 1 and n_parts == 1:
        warnings.append(f"The shape has {solids} separate solids. If it should be one part, a union did not fuse them (check that they touch or overlap).")
    if warnings:
        out["warnings"] = warnings
    return out


def _solids(root: TopoDS_Shape) -> Iterable[TopoDS_Shape]:
    from OCP.TopAbs import TopAbs_SOLID

    m = _index_map(root, TopAbs_SOLID)
    for i in range(1, m.Extent() + 1):
        yield TopoDS.Solid_s(m.FindKey(i))


def _ids_in(shape: TopoDS_Shape, kind, index_map: TopTools_IndexedMapOfShape) -> Iterable[int]:
    sub = _index_map(shape, kind)
    for j in range(1, sub.Extent() + 1):
        idx = index_map.FindIndex(sub.FindKey(j)) - 1
        if idx >= 0:
            yield idx


def _part_info(pi: int, part: Part, face_part: np.ndarray, edge_part: np.ndarray) -> dict[str, Any]:
    shape = cq.Shape.cast(part.shape)
    bb = shape.BoundingBox()
    info: dict[str, Any] = {
        "id": pi,
        "kind": "part",
        "name": part.name,
        "path": part.path,
        "faces": [int(i) for i in np.flatnonzero(face_part == pi)],
        "edges": [int(i) for i in np.flatnonzero(edge_part == pi)],
        "bbox": {"min": vec((bb.xmin, bb.ymin, bb.zmin)), "max": vec((bb.xmax, bb.ymax, bb.zmax)), "size": vec((bb.xlen, bb.ylen, bb.zlen))},
    }
    if part.color is not None:
        info["color"] = hex_color(part.color)
    if part.layer is not None:
        info["layer"] = part.layer
    try:
        info["volume"] = rnd(shape.Volume())
    except Exception:  # noqa: BLE001
        pass
    return info


def cad_file_to_viewer_data(path: str | Path, fmt: str, options: Options) -> dict[str, Any]:
    parts, units = load_cad_file(path, fmt)
    return shapes_to_viewer_data(parts, source={"kind": "file", "format": fmt, "path": str(path)}, units=units, options=options)


def shape_to_viewer_data(shape: cq.Shape, options: Options, source: dict[str, Any]) -> dict[str, Any]:
    """CadQuery コードの結果（1 つの形）をビューア用データにする。"""
    return shapes_to_viewer_data([Part("result", "result", shape.wrapped)], source=source, units="mm", options=options)

