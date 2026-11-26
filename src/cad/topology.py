"""開いたサーフェース・メッシュの穴（境界のループ）と、ローカル座標系（ローカル AXIS）の検出。numpy だけで動く（cadquery 不要）。

どちらもビューア用データ（src/cad/mesh_data.py）から求めるので、B-rep（STEP・IGES・BREP・DXF・CadQuery コード）と
STL を同じ処理で扱う。run_cad.py が形をビューア用データにした後に ``attach_topology`` を呼ぶ。

穴（``holeList``・``holes``）:

1. 表示用の三角形の頂点を同じ座標で 1 つにまとめ（許容差の 1/1000 の刻み）、辺ごとに両側の三角形を組にする。
   同じ面の三角形どうし（元の頂点の番号が同じ辺）は先に組にし、残りは同じ位置の辺どうしで、三角形が辺の
   反対側にあるもの（「辺から三角形の残りの頂点への向き」〔面の側〕がなす角が 60° を超える）だけを組にする。
   組にならない辺が境界の辺。B-rep は面ごとに三角形を作るが、隣り合う面のエッジの点は同じ位置なので、
   つながった面の境目は境界にならない。同じ側に面がある辺（フィレット前と後の面が重なっている等、ほぼ同じ面が
   2 枚重なっているところ）は組にしないので、重なった面はそれぞれの縁を持つ。同じ位置に辺が 3 本以上あるとき
   （面の境目に別の部品のパッチの縁が重なっている等）は、同じ部品の辺どうしを先に組にする。
2. トポロジートレランス（既定 0.1mm）: 境界の辺のうち、平行に近い別の境界の辺から許容差以内に並び、面が反対側に
   あるものは、隙間が許容差以下の「つながった」境目として除く（縫い合わせていない面どうし等）。
   許容差より細い切れ込み・小さい穴も同じ理由で消える。
3. 縫合（``sew_tolerance``。0 で縫合しない）: 2 と同じ突き合わせを、別の面の境界の辺どうしに限って縫合トレランス
   以内まで広げる（面と面の離れを閉じる。1 つの面の中の小さい穴・細い切れ込みは消さない）。
4. 残った境界の辺をループにつなぐ。部品が分かっていれば同じ部品の辺だけをたどり（部品の縁に別の部品の縁が
   重なっていても乗り移らない）、途切れた端どうしが許容差以内なら橋渡しする（部品をまたぐ縁もここでつながる）。平均の幅（2 × 面積 ÷ 周長）が
   許容差以下のループ、縫合したときは複数の面にまたがり幅が縫合トレランス以下のループは隙間として除く。
5. 面のまとまり（部分）: 1 の組と 2・3 の突き合わせでつながった面を 1 つにまとめる（``faceComponents``。面ごとの番号）。
   B-rep のエッジの共有は使わない（縫い合わせた IGES で、重なった 2 枚の面がエッジを共有していることがあるため）。
6. まとまりごとに、まとまりの外接箱の端（2 か所以上。厚みの無い向きは数えない）に届くループのうち、届く端が多く外接箱の
   大きいものを外周（``outerList``・``outers``）とし、残りを穴とする（両端の開いた筒では片方の口が外周、もう片方が穴になる）。
   外周の線が途切れて閉じていないときに、一番大きい穴を外周にしないため。
7. 穴の法線はループの面積ベクトル（Newell の方法）を、穴の縁の三角形の向き（面の表側）に合わせたもの。
   縫い合わせていない面は面ごとに表裏がそろっていないことがあるので、ビューアは向きの差を符号によらずに比べる。

ローカル AXIS（``localAxes``）: 直線のエッジのうち長さが X・Y・Z の長さ（既定 100・200・300）に許容差以内で合う 3 本が、
1 つの端点を共有して互いに直交するもの。共有する端点を原点、そこから各線の先への向きを X・Y・Z とする
（直交からのずれは Gram-Schmidt で直した正規直交基底にする。左手系なら rightHanded: false）。
"""

from __future__ import annotations

import base64
import math
from typing import Any, Iterable, Sequence

import numpy as np

from .mesh_data import b64, bbox_of, rnd, vec

# 既定のトポロジートレランス（モデルの単位。mm）。
DEFAULT_HOLE_TOLERANCE = 0.1
# 既定のローカル AXIS の X・Y・Z の線の長さと、長さ・端点の位置の許容差、直交の角度の許容差（度）。
DEFAULT_AXIS_LENGTHS = (100.0, 200.0, 300.0)
DEFAULT_AXIS_LENGTH_TOLERANCE = 0.1
DEFAULT_AXIS_ANGLE_TOLERANCE_DEG = 0.5
# 見つけるローカル AXIS の数の上限。
LOCAL_AXES_MAX = 20
# 突き合わせで調べる、辺の上の点の位置（辺の長さに対する割合）。
_SAMPLES = (0.5, 0.1, 0.9, 0.3, 0.7)
# 突き合わせる辺どうしが平行とみなす向きの差。
_PARALLEL_COS = math.cos(math.radians(45))
# 辺の両側の三角形が「反対側にある」とみなす、面の側の向き（辺から三角形の残りの頂点への向き）の内積の上限
# （なす角が 60° を超える。ほぼ同じ面が重なっていると 0°、平らにつながると 180°、直角に折れると 90°）。
_OPPOSITE_COS = 0.5
# 外周に選ぶループが届いていなければならない、まとまりの外接箱の端の数（厚みのある向きが 2 つ以上あるとき）。
_OUTER_MIN_SIDES = 2


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def attach_topology(data: dict[str, Any], options: dict[str, Any] | None = None) -> None:
    """ビューア用データに穴（holeList・holes）とローカル AXIS（localAxes）を足す（data を書き換える）。

    見つからなければ何も足さない。失敗しても形の表示はできるので、例外は外へ出さず data["topologyError"] に残す。
    """
    options = options or {}
    try:
        apply_holes(
            data,
            float(options.get("hole_tolerance", DEFAULT_HOLE_TOLERANCE)),
            float(options.get("sew_tolerance", 0.0) or 0.0),
        )
        lengths = _axis_lengths(options.get("local_axis_lengths"))
        if lengths:
            axes = find_local_axes(
                data.get("edgeList") or [],
                lengths,
                float(options.get("local_axis_length_tolerance", DEFAULT_AXIS_LENGTH_TOLERANCE)),
                float(options.get("local_axis_angle_tolerance_deg", DEFAULT_AXIS_ANGLE_TOLERANCE_DEG)),
            )
            if axes:
                data["localAxes"] = axes
    except Exception as e:  # noqa: BLE001 - 穴・ローカル AXIS が求まらなくても形は見せる
        data["topologyError"] = f"{type(e).__name__}: {e}"


def _axis_lengths(raw: Any) -> tuple[float, float, float] | None:
    if raw is None:
        return DEFAULT_AXIS_LENGTHS
    if isinstance(raw, str):
        raw = [x for x in raw.replace(";", ",").split(",") if x.strip()]
    try:
        vals = tuple(float(x) for x in raw)
    except (TypeError, ValueError):
        return None
    if len(vals) != 3 or any(v <= 0 for v in vals):
        return None
    return vals  # type: ignore[return-value]


def _decode(value: Any, dtype: str, width: int) -> np.ndarray:
    if not value:
        return np.zeros((0, width), dtype=np.dtype(dtype))
    raw = base64.b64decode(value) if isinstance(value, str) else bytes(value)
    return np.frombuffer(raw, dtype=np.dtype(dtype).newbyteorder("<")).reshape(-1, width)


# ---------------------------------------------------------------------------
# 穴
# ---------------------------------------------------------------------------


# apply_holes が書き換えるビューア用データのキー（縫合をやり直すとき、前の結果を消してから入れる）。
TOPOLOGY_KEYS = ("holeList", "holes", "outerList", "outers", "faceComponents")


def apply_holes(data: dict[str, Any], tolerance: float, sew_tolerance: float = 0.0) -> dict[str, Any] | None:
    """data の穴・外周・面のまとまりを求め直して入れる（前の結果は消す）。tolerance が 0 以下なら穴は探さない。

    ビューアの「縫合」（src/tools/cad_viewer.py の sew_viewer_data）は、縫合トレランスを変えてこれを呼び直す。
    戻り値は find_boundary_holes の結果（三角形が無ければ None）。
    """
    for key in TOPOLOGY_KEYS:
        data.pop(key, None)
    stats = data.get("stats")
    if isinstance(stats, dict):
        stats.pop("holes", None)
        stats.pop("outers", None)
    found = find_boundary_holes(data, tolerance, sew_tolerance)
    if found is None:
        return None
    # 面のまとまり（「連続する面」の選択に使う）は、閉じた形でも入れる。
    if found.get("faceComponents") is not None:
        data["faceComponents"] = found["faceComponents"]
    if tolerance > 0 and found.get("holes") is not None:
        data["holeList"] = found["holeList"]
        data["holes"] = found["holes"]
        data["outerList"] = found["outerList"]
        data["outers"] = found["outers"]
        data.setdefault("stats", {}).update(holes=len(found["holeList"]), outers=len(found["outerList"]))
    return found


def find_boundary_holes(data: dict[str, Any], tolerance: float, sew_tolerance: float = 0.0) -> dict[str, Any] | None:
    """ビューア用データの三角形から、開いた形の穴と外周、面のまとまりを求める。三角形が無ければ None。

    戻り値は {"faceComponents": 面ごとのまとまりの番号} と、境界があれば（tolerance > 0 のとき）
    {"holeList": 穴の情報, "holes": 穴の縁の線分, "outerList": 外周の情報, "outers": 外周の線分}。
    sew_tolerance（0 以下で縫合しない）は、別の面の境界の辺どうしを突き合わせる距離（「縫合」）。
    """
    mesh = data.get("mesh") or {}
    positions = _decode(mesh.get("positions"), "f4", 3).astype(np.float64)
    indices = _decode(mesh.get("indices"), "u4", 3).astype(np.int64)
    if not len(indices):
        return None
    faces = data.get("faces") or []
    tri_face = _triangle_faces(mesh.get("faceRanges") or [], len(indices))
    find_holes = tolerance > 0
    tol = tolerance if find_holes else DEFAULT_HOLE_TOLERANCE
    sew = max(float(sew_tolerance), 0.0) if find_holes else 0.0
    face_part = np.array([f.get("part") if isinstance(f.get("part"), int) else -1 for f in faces], dtype=np.int64)
    tris = _Boundary(positions, indices, tol, sew, tri_face, face_part)
    if find_holes:
        tris.match_within_tolerance()
    comp = _face_components(len(faces), tris.face_pairs(tri_face))
    out: dict[str, Any] = {"faceComponents": _renumber(comp).tolist()}
    if not find_holes or not len(tris.a):
        return out
    loops, open_chains = tris.loops()
    edges = data.get("edgeList") or []
    is_brep = (data.get("source") or {}).get("format") != "STL"

    tri_normals = _triangle_normals(positions, indices)
    candidates: list[dict[str, Any]] = []
    gaps = 0
    for pts, loop_tris in loops:
        info = _loop_info(pts, loop_tris, tri_normals, tri_face)
        if info is None:
            continue
        # 許容差以下の幅のループ、縫合したときは面と面の間に残った縫合トレランス以下の幅のループは隙間。
        if info["_width"] <= tol or (sew > tol and len(info["_faces"]) > 1 and info["_width"] <= sew):
            gaps += 1
            continue
        candidates.append(info)
    if not candidates and not open_chains and not gaps:
        return out

    face_boxes = _face_boxes(positions, indices, mesh.get("faceRanges") or [], len(faces))
    outer_ids = _choose_outers(candidates, comp, face_boxes, max(tol, sew))
    holes = [c for i, c in enumerate(candidates) if i not in outer_ids]
    outers = [candidates[i] for i in sorted(outer_ids)]

    if is_brep:
        _attach_brep_edges(holes + outers, faces, edges, data.get("edges") or {})

    hole_items, hole_geometry = _loop_items(holes, "hole", faces)
    outer_items, outer_geometry = _loop_items(outers, "outer", faces)
    hole_geometry.update(
        tolerance=rnd(tol),
        sewTolerance=rnd(sew),
        sewnEdges=int(tris.sewn_count),
        outerLoops=len(outer_ids),
        openChains=open_chains,
        gapLoops=gaps,
    )
    out.update(holeList=hole_items, holes=hole_geometry, outerList=outer_items, outers=outer_geometry)
    return out


def _renumber(comp: np.ndarray) -> np.ndarray:
    """まとまりの番号を、最初に出てきた順の 0, 1, 2, … にする。"""
    if not len(comp):
        return comp
    _, first, inverse = np.unique(comp, return_index=True, return_inverse=True)
    order = np.argsort(np.argsort(first))
    return order[inverse.reshape(-1)].astype(np.int64)


def _face_boxes(positions: np.ndarray, indices: np.ndarray, face_ranges: Sequence[Sequence[int]], n_faces: int) -> np.ndarray:
    """面ごとの三角形の外接箱 (n_faces, 2, 3)。三角形の無い面は NaN。"""
    out = np.full((n_faces, 2, 3), np.nan)
    if not len(indices):
        return out
    p = positions[indices]
    lo, hi = p.min(axis=1), p.max(axis=1)
    for fid, (start, count) in enumerate(face_ranges[:n_faces]):
        s, c = int(start), int(count)
        if c > 0:
            out[fid, 0] = lo[s : s + c].min(axis=0)
            out[fid, 1] = hi[s : s + c].max(axis=0)
    return out


def _choose_outers(candidates: list[dict[str, Any]], comp: np.ndarray, face_boxes: np.ndarray, tol: float) -> set[int]:
    """まとまりごとの外周のループ（candidates の番号）。

    まとまりの外接箱の端（各軸の最小・最大。厚みの無い軸は数えない）に届く数が多く、外接箱の大きいループを外周にする。
    外周は形の端を回るので端に届く。届く端が _OUTER_MIN_SIDES（厚みのある軸が 1 つなら 1）に満たないループは外周に
    しない（外周の線が途切れて閉じなかったとき、穴を外周にしてしまわないように）。
    """
    by_comp: dict[int, list[int]] = {}
    for i, c in enumerate(candidates):
        key = int(comp[c["_faces"][0]]) if c["_faces"] and len(comp) else -1
        c["_component"] = key
        by_comp.setdefault(key, []).append(i)
    out: set[int] = set()
    for key, ids in by_comp.items():
        if key >= 0 and len(face_boxes):
            members = face_boxes[comp == key]
            members = members[~np.isnan(members[:, 0, 0])]
        else:
            members = np.zeros((0, 2, 3))
        if not len(members):
            best = max(ids, key=lambda i: (candidates[i]["_diag"], candidates[i]["perimeter"]))
            out.add(best)
            continue
        lo, hi = members[:, 0].min(axis=0), members[:, 1].max(axis=0)
        eps = max(2.0 * tol, 1e-4 * float(np.linalg.norm(hi - lo)))
        thick = [k for k in range(3) if hi[k] - lo[k] > 2 * eps]
        if not thick:
            best = max(ids, key=lambda i: (candidates[i]["_diag"], candidates[i]["perimeter"]))
            out.add(best)
            continue
        need = min(_OUTER_MIN_SIDES, len(thick))
        scored = []
        for i in ids:
            pts = candidates[i]["_points"]
            pmin, pmax = pts.min(axis=0), pts.max(axis=0)
            touch = sum(int(pmin[k] <= lo[k] + eps) + int(pmax[k] >= hi[k] - eps) for k in thick)
            if touch >= need:
                scored.append(((touch, candidates[i]["_diag"], candidates[i]["perimeter"]), i))
        if scored:
            out.add(max(scored)[1])
    return out


def _loop_items(loops: list[dict[str, Any]], kind: str, faces: list[dict]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """ループ（穴・外周）の情報の一覧と、ビューアに描く縁の線分（線分ごとに 2 点。ループ i は ranges[i]）。"""
    items: list[dict[str, Any]] = []
    seg_chunks: list[np.ndarray] = []
    ranges: list[list[int]] = []
    seg_total = 0
    for i, c in enumerate(loops):
        pts = c["_points"]
        seg_chunks.append(np.stack([pts, np.roll(pts, -1, axis=0)], axis=1).reshape(-1, 3))
        ranges.append([seg_total, len(pts)])
        seg_total += len(pts)
        item = {"id": i, "kind": kind, "geomType": "BOUNDARY_LOOP"}
        item.update({k: v for k, v in c.items() if not k.startswith("_")})
        item["faces"] = c["_faces"]
        if "_edges" in c:
            item["edges"] = c["_edges"]
        parts = sorted({int(faces[f]["part"]) for f in c["_faces"] if f < len(faces) and isinstance(faces[f].get("part"), int)})
        if len(parts) == 1:
            item["part"] = parts[0]
        items.append(item)
    geometry = {"positions": b64(np.concatenate(seg_chunks) if seg_chunks else np.zeros((0, 3)), "f4"), "ranges": ranges}
    return items, geometry


def _triangle_faces(face_ranges: Sequence[Sequence[int]], n_tris: int) -> np.ndarray:
    out = np.full(n_tris, -1, dtype=np.int64)
    for fid, (start, count) in enumerate(face_ranges):
        out[int(start) : int(start) + int(count)] = fid
    return out


def _triangle_normals(positions: np.ndarray, indices: np.ndarray) -> np.ndarray:
    """三角形ごとの面積ベクトル（向きは三角形の回り方。大きさは面積の 2 倍）。"""
    p = positions[indices]
    return np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])


class _Boundary:
    """三角形の辺の組（両側の三角形）と境界の辺（組にならない辺）、境界の辺の突き合わせ・縫合・つなぎ。"""

    def __init__(
        self,
        positions: np.ndarray,
        indices: np.ndarray,
        tolerance: float,
        sew_tolerance: float = 0.0,
        tri_face: np.ndarray | None = None,
        face_part: np.ndarray | None = None,
    ) -> None:
        self.tol = tolerance
        self.sew = sew_tolerance if sew_tolerance > tolerance else 0.0
        self.tri_face = tri_face if tri_face is not None else np.full(len(indices), -1, dtype=np.int64)
        # 三角形ごとの部品の番号（分からなければ -1）。
        self.tri_part = np.full(len(indices), -1, dtype=np.int64)
        if face_part is not None and len(face_part):
            known = (self.tri_face >= 0) & (self.tri_face < len(face_part))
            self.tri_part[known] = face_part[self.tri_face[known]]
        quantum = tolerance * 1e-3
        keys = np.floor(positions / quantum + 0.5).astype(np.int64)
        _, inverse = np.unique(keys, axis=0, return_inverse=True)
        inverse = inverse.reshape(-1)
        n_vertices = int(inverse.max()) + 1 if len(inverse) else 0
        counts = np.bincount(inverse, minlength=n_vertices).astype(np.float64)
        self.points = np.stack([np.bincount(inverse, weights=positions[:, k], minlength=n_vertices) for k in range(3)], axis=1) / counts[:, None]
        tri = inverse[indices]
        ok = (tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])
        tri_ids = np.flatnonzero(ok)
        orig = indices[ok]
        tri = tri[ok]
        a = tri[:, [0, 1, 2]].ravel()
        b = tri[:, [1, 2, 0]].ravel()
        c = tri[:, [2, 0, 1]].ravel()
        oa = orig[:, [0, 1, 2]].ravel()
        ob = orig[:, [1, 2, 0]].ravel()
        owner = np.repeat(tri_ids, 3)
        side = self._sides(a, b, c)
        paired = np.zeros(len(a), dtype=bool)
        # 面をまたいでつながる辺の両側の三角形の組（面のまとまりに使う）。
        self.cross_pairs: list[tuple[int, int]] = []

        # 1. 同じ面の三角形どうし: 元の頂点の番号が同じ辺（面ごとに三角形を作るので、面の中の辺は頂点を共有する）。
        if len(a):
            olo, ohi = np.minimum(oa, ob), np.maximum(oa, ob)
            okey = olo * (int(orig.max()) + 1) + ohi
            _, oinv, ocnt = np.unique(okey, return_inverse=True, return_counts=True)
            paired |= ocnt[oinv.reshape(-1)] >= 2

        # 2. 残りは同じ位置の辺どうしで、三角形が辺の反対側にあるものだけを組にする（同じ側にあるのは重なった面）。
        rest = np.flatnonzero(~paired)
        if len(rest):
            lo, hi = np.minimum(a[rest], b[rest]), np.maximum(a[rest], b[rest])
            key = lo * max(n_vertices, 1) + hi
            order = np.argsort(key, kind="stable")
            sorted_key = key[order]
            starts = np.flatnonzero(np.r_[True, sorted_key[1:] != sorted_key[:-1]])
            sizes = np.diff(np.r_[starts, len(sorted_key)])
            # 2 本ずつの組は一度に調べる。
            two = starts[sizes == 2]
            if len(two):
                i, j = rest[order[two]], rest[order[two + 1]]
                opposite = np.einsum("ij,ij->i", side[i], side[j]) < _OPPOSITE_COS
                paired[i[opposite]] = True
                paired[j[opposite]] = True
                self.cross_pairs += list(zip(owner[i[opposite]].tolist(), owner[j[opposite]].tolist()))
            # 3 本以上（重なった面の上でさらに面がつながる等）は、反対側にあるものどうしを、同じ部品どうしを先に、
            # 近い順に組にする（別の部品のパッチが面の境目に重なっていても、部品の面どうしの境目を組にする）。
            for s, n in zip(starts[sizes > 2].tolist(), sizes[sizes > 2].tolist()):
                group = rest[order[s : s + n]].tolist()
                dots = sorted(
                    (int(self._part_of(owner[x]) != self._part_of(owner[y])), float(side[x] @ side[y]), x, y)
                    for k, x in enumerate(group)
                    for y in group[k + 1 :]
                )
                for _other, d, x, y in dots:
                    if d >= _OPPOSITE_COS:
                        continue
                    if paired[x] or paired[y]:
                        continue
                    paired[x] = paired[y] = True
                    self.cross_pairs.append((int(owner[x]), int(owner[y])))

        boundary = ~paired
        self.a = a[boundary]
        self.b = b[boundary]
        self.side = side[boundary]
        self.tri = owner[boundary]
        self.alive = np.ones(len(self.a), dtype=bool)
        self.partner = np.full(len(self.a), -1, dtype=np.int64)
        self.sewn_count = 0

    def _part_of(self, tri: int) -> int:
        return int(self.tri_part[tri])

    def _sides(self, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
        """辺 (a, b) から三角形の残りの頂点 c への、辺に垂直な単位ベクトル（三角形が辺のどちら側にあるか）。"""
        pa = self.points[a]
        e = self.points[b] - pa
        w = self.points[c] - pa
        ee = np.einsum("ij,ij->i", e, e)
        t = np.divide(np.einsum("ij,ij->i", w, e), ee, out=np.zeros(len(e)), where=ee > 0)
        s = w - e * t[:, None]
        n = np.linalg.norm(s, axis=1)
        return np.divide(s, n[:, None], out=np.zeros_like(s), where=n[:, None] > 0)

    # --- 突き合わせ（トポロジートレランス）と縫合 ---

    def match_within_tolerance(self) -> None:
        """別の境界の辺から許容差以内に並ぶ境界の辺を除く（隙間が許容差以下の境目）。縫合するときは、別の面の
        境界の辺どうしに限って縫合トレランス以内まで除く。

        辺の上の数点（_SAMPLES）のどれもが、平行に近く、三角形が反対側にある別の境界の辺へ、その辺の内側で垂線を
        下ろせて、その長さが許容差以内なら除く。同じ縁で続く隣の辺は、点から下ろした垂線がその辺の外に出るので相手に
        ならない（細かく分けた小さな円の縁でも）。ほぼ同じ面が重なっている縁は三角形が同じ側にあるので除かない。
        """
        n = len(self.a)
        if n < 2:
            return
        tol = self.tol
        reach = max(tol, self.sew)
        pa, pb = self.points[self.a], self.points[self.b]
        seg = pb - pa
        length = np.linalg.norm(seg, axis=1)
        direction = np.divide(seg, length[:, None], out=np.zeros_like(seg), where=length[:, None] > 0)
        face = self.tri_face[self.tri]
        cell = max(reach * 4, float(np.median(length)))
        grid: dict[tuple[int, int, int], list[int]] = {}
        for i in range(n):
            # 辺に沿って升目の大きさごとに点を取り、各点の許容差の範囲に掛かる升目に入れる（升目に比べて長い辺
            # 〔大きな平面の縁など〕でも、外接箱の升目すべてではなく辺の近くの升目だけになる）。
            steps = max(1, int(math.ceil(float(length[i]) / cell)))
            cells: set[tuple[int, int, int]] = set()
            for t in np.linspace(0.0, 1.0, steps + 1):
                p = pa[i] + seg[i] * t
                x0, y0, z0 = np.floor((p - reach) / cell).astype(np.int64).tolist()
                x1, y1, z1 = np.floor((p + reach) / cell).astype(np.int64).tolist()
                for x in range(x0, x1 + 1):
                    for y in range(y0, y1 + 1):
                        for z in range(z0, z1 + 1):
                            cells.add((x, y, z))
            for c in cells:
                grid.setdefault(c, []).append(i)
        tol2 = tol * tol
        sew2 = self.sew * self.sew

        def covering(i: int, p: np.ndarray) -> tuple[int, bool]:
            """p から許容差（別の面なら縫合トレランス）以内で、内側に垂線を下ろせる平行に近い別の辺と、縫合で見つけたか。"""
            found, by_sewing = -1, False
            for j in grid.get(tuple(np.floor(p / cell).astype(np.int64).tolist()), ()):  # type: ignore[arg-type]
                if j == i or abs(float(direction[i] @ direction[j])) < _PARALLEL_COS:
                    continue
                if float(self.side[i] @ self.side[j]) >= _OPPOSITE_COS:
                    continue
                d = seg[j]
                ll = float(d @ d)
                if ll <= 0:
                    continue
                t = float((p - pa[j]) @ d) / ll
                if t < 0.0 or t > 1.0:
                    continue
                q = pa[j] + d * t - p
                dist2 = float(q @ q)
                if dist2 <= tol2:
                    return j, False
                if sew2 and found < 0 and dist2 <= sew2 and face[i] != face[j] and face[i] >= 0 and face[j] >= 0:
                    found, by_sewing = j, True
            return found, by_sewing

        for i in range(n):
            if length[i] <= 0:
                continue
            partner = -1
            sewn = False
            for k in _SAMPLES:
                j, by_sewing = covering(i, pa[i] + seg[i] * k)
                if j < 0:
                    partner = -1
                    break
                sewn = sewn or by_sewing
                if partner < 0:
                    partner = j
            if partner >= 0:
                self.alive[i] = False
                self.partner[i] = partner
                self.sewn_count += int(sewn)

    def face_pairs(self, tri_face: np.ndarray) -> list[tuple[int, int]]:
        """つながった面の組: 面をまたぐ辺の両側の三角形の面と、突き合わせ・縫合で除いた辺の両側の面。"""
        out = []
        for ta, tb in self.cross_pairs:
            fa, fb = int(tri_face[ta]), int(tri_face[tb])
            if fa >= 0 and fb >= 0 and fa != fb:
                out.append((fa, fb))
        for i in np.flatnonzero(self.partner >= 0).tolist():
            fa, fb = int(tri_face[self.tri[i]]), int(tri_face[self.tri[self.partner[i]]])
            if fa >= 0 and fb >= 0 and fa != fb:
                out.append((fa, fb))
        return out

    # --- ループ ---

    def loops(self) -> tuple[list[tuple[np.ndarray, list[int]]], int]:
        """残った境界の辺をループにつなぐ。戻り値は ([(点の並び, 縁の三角形), ...], 閉じなかった線の数)。"""
        live = np.flatnonzero(self.alive).tolist()
        out_edges: dict[int, list[int]] = {}
        in_edges: dict[int, list[int]] = {}
        for i in live:
            out_edges.setdefault(int(self.a[i]), []).append(i)
            in_edges.setdefault(int(self.b[i]), []).append(i)
        used: set[int] = set()
        chains: list[tuple[list[int], list[int]]] = []  # (頂点の並び, 辺の並び)
        face = self.tri_face[self.tri] if len(self.tri) else np.zeros(0, dtype=np.int64)
        part = self.tri_part[self.tri] if len(self.tri) else np.zeros(0, dtype=np.int64)

        def pick(cands: Iterable[int], prev: int) -> int | None:
            # 同じ点から境界の辺が何本も出る（ほぼ同じ面が重なっている等）ときは、今の辺と同じ面の辺を先に選ぶ
            # （重なった別の面の縁へ乗り移らないように）。部品が分かっていれば同じ部品の辺だけをたどる（別の部品の
            # パッチの縁が部品の縁に重なっていても、それぞれの縁を回る。部品をまたぐ縁は _bridge でつなぐ）。
            free = [e for e in cands if e not in used and (part[prev] < 0 or part[e] == part[prev])]
            if not free:
                return None
            return next((e for e in free if face[e] == face[prev]), free[0])

        for start in live:
            if start in used:
                continue
            used.add(start)
            verts = [int(self.a[start]), int(self.b[start])]
            path = [start]
            while verts[-1] != verts[0]:
                cur = verts[-1]
                nxt = pick(out_edges.get(cur, ()), path[-1])
                flip = False
                if nxt is None:
                    # 三角形の回り方がそろっていないメッシュでは、逆向きの辺でつなぐ。
                    nxt = pick(in_edges.get(cur, ()), path[-1])
                    flip = True
                if nxt is None:
                    break
                used.add(nxt)
                path.append(nxt)
                verts.append(int(self.a[nxt]) if flip else int(self.b[nxt]))
            chains.append((verts, path))
        closed = [c for c in chains if len(c[0]) > 3 and c[0][-1] == c[0][0]]
        opened = [c for c in chains if not (len(c[0]) > 1 and c[0][-1] == c[0][0])]
        closed += self._bridge(opened)
        open_count = sum(1 for c in opened if c[0])
        loops = [(self.points[np.array(v[:-1])], [int(self.tri[e]) for e in p]) for v, p in closed if len(v) > 3]
        return loops, open_count

    def _bridge(self, opened: list[tuple[list[int], list[int]]]) -> list[tuple[list[int], list[int]]]:
        """途切れた線の端どうしが許容差以内なら橋渡しして、閉じたループにする（opened から外す）。"""
        if not opened:
            return []
        # 縫合したときは、縫い合わせた境目の端に縫合トレランスまでの途切れが残ることがある。
        tol = max(self.tol, self.sew)
        closed: list[tuple[list[int], list[int]]] = []
        changed = True
        while changed and opened:
            changed = False
            for i, (vi, pi) in enumerate(opened):
                if not vi:
                    continue
                head, tail = self.points[vi[0]], self.points[vi[-1]]
                if len(vi) > 3 and float(np.linalg.norm(head - tail)) <= tol:
                    closed.append((vi + [vi[0]], pi))
                    opened[i] = ([], [])
                    changed = True
                    continue
                best = None
                for j, (vj, _pj) in enumerate(opened):
                    if j == i or not vj:
                        continue
                    for rev in (False, True):
                        start = self.points[vj[-1] if rev else vj[0]]
                        d = float(np.linalg.norm(tail - start))
                        if d <= tol and (best is None or d < best[0]):
                            best = (d, j, rev)
                if best is not None:
                    _, j, rev = best
                    vj, pj = opened[j]
                    opened[i] = (vi + (vj[::-1] if rev else vj), pi + pj)
                    opened[j] = ([], [])
                    changed = True
        opened[:] = [c for c in opened if c[0]]
        return closed


def _loop_info(pts: np.ndarray, loop_tris: list[int], tri_normals: np.ndarray, tri_face: np.ndarray) -> dict[str, Any] | None:
    """ループ 1 つの形の情報（中心・法線・面積・周長・直径・平面からのずれ・外接箱・縁の面）。"""
    if len(pts) < 3:
        return None
    nxt = np.roll(pts, -1, axis=0)
    seg_len = np.linalg.norm(nxt - pts, axis=1)
    perimeter = float(seg_len.sum())
    if perimeter <= 0:
        return None
    center = (((pts + nxt) / 2) * seg_len[:, None]).sum(axis=0) / perimeter
    area_vec = 0.5 * np.cross(pts - center, nxt - center).sum(axis=0)
    area = float(np.linalg.norm(area_vec))
    surf = tri_normals[np.array(loop_tris)].sum(axis=0) if loop_tris else np.zeros(3)
    surf_norm = float(np.linalg.norm(surf))
    if area > 1e-12 * perimeter * perimeter:
        normal = area_vec / area
        if surf_norm > 0 and float(normal @ surf) < 0:
            normal = -normal
    elif surf_norm > 0:
        normal = surf / surf_norm
    else:
        return None
    rel = pts - center
    off = rel @ normal
    in_plane = rel - np.outer(off, normal)
    radii = np.linalg.norm(in_plane, axis=1)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    faces = sorted({int(tri_face[t]) for t in loop_tris if tri_face[t] >= 0})
    return {
        "center": vec(center),
        "normal": vec(normal),
        "area": rnd(area),
        "perimeter": rnd(perimeter),
        "diameter": rnd(2 * float(radii.mean())),
        "circularity": rnd(4 * math.pi * area / (perimeter * perimeter), 4),
        "planarity": rnd(float(np.abs(off).max())),
        "bbox": bbox_of(pts),
        "segments": len(pts),
        "_points": pts,
        "_faces": faces,
        "_width": 2 * area / perimeter,
        "_diag": float(np.linalg.norm(hi - lo)),
    }


def _face_components(n_faces: int, pairs: list[tuple[int, int]]) -> np.ndarray:
    """面どうしのつながり（三角形の辺でつながる・許容差以内で突き合わせた・縫合した）でまとめた、面ごとのまとまりの番号。"""
    parent = list(range(n_faces))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        if 0 <= x < n_faces and 0 <= y < n_faces:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[ry] = rx

    for x, y in pairs:
        union(x, y)
    return np.array([find(i) for i in range(n_faces)], dtype=np.int64)


def _attach_brep_edges(holes: list[dict], faces: list[dict], edges: list[dict], edge_geom: dict[str, Any]) -> None:
    """B-rep の穴に、その縁になっているエッジ（面 1 つにしか属さないエッジ）の ID を付ける。"""
    if not holes or not edges:
        return
    seg = _decode(edge_geom.get("positions"), "f4", 3).astype(np.float64)
    ranges = edge_geom.get("ranges") or []
    by_face: dict[int, list[int]] = {}
    for hi, h in enumerate(holes):
        for f in h["_faces"]:
            by_face.setdefault(f, []).append(hi)
    found: dict[int, list[int]] = {}
    for eid, e in enumerate(edges):
        fs = e.get("faces") or []
        if len(fs) != 1 or e.get("seam") or e.get("free") or eid >= len(ranges):
            continue
        cand = by_face.get(int(fs[0]))
        if not cand:
            continue
        start, count = ranges[eid]
        if not count:
            continue
        pts = seg[int(start) * 2 : (int(start) + int(count)) * 2]
        sample = pts[len(pts) // 2]
        best = None
        for hi in cand:
            d = float(np.min(np.linalg.norm(holes[hi]["_points"] - sample, axis=1)))
            if best is None or d < best[0]:
                best = (d, hi)
        if best is not None:
            found.setdefault(best[1], []).append(eid)
    for hi, ids in found.items():
        holes[hi]["_edges"] = sorted(ids)


# ---------------------------------------------------------------------------
# ローカル AXIS
# ---------------------------------------------------------------------------


def find_local_axes(
    edges: list[dict],
    lengths: Sequence[float] = DEFAULT_AXIS_LENGTHS,
    length_tolerance: float = DEFAULT_AXIS_LENGTH_TOLERANCE,
    angle_tolerance_deg: float = DEFAULT_AXIS_ANGLE_TOLERANCE_DEG,
) -> list[dict[str, Any]]:
    """長さ lengths（X・Y・Z の順）の直線のエッジ 3 本が 1 つの端点を共有して直交するものを、ローカル座標系として返す。"""
    tol = max(float(length_tolerance), 1e-9)
    sin_tol = math.sin(math.radians(max(float(angle_tolerance_deg), 0.0)))
    cands: list[list[tuple[int, np.ndarray, np.ndarray]]] = [[], [], []]
    for e in edges:
        if e.get("geomType") != "LINE" or "start" not in e or "end" not in e:
            continue
        s, t = np.asarray(e["start"], float), np.asarray(e["end"], float)
        length = float(np.linalg.norm(t - s))
        for k, target in enumerate(lengths):
            if abs(length - float(target)) <= tol:
                cands[k].append((int(e["id"]), s, t))
    if not all(cands):
        return []

    def ends(items: list[tuple[int, np.ndarray, np.ndarray]]) -> dict[tuple[int, int, int], list[tuple[int, np.ndarray, np.ndarray]]]:
        grid: dict[tuple[int, int, int], list[tuple[int, np.ndarray, np.ndarray]]] = {}
        for eid, s, t in items:
            for p, q in ((s, t), (t, s)):
                key = tuple(np.floor(p / tol).astype(np.int64).tolist())
                grid.setdefault(key, []).append((eid, p, q))  # type: ignore[arg-type]
        return grid

    y_ends, z_ends = ends(cands[1]), ends(cands[2])

    def near(grid, p: np.ndarray) -> Iterable[tuple[int, np.ndarray, np.ndarray]]:
        c = np.floor(p / tol).astype(np.int64)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for item in grid.get((int(c[0] + dx), int(c[1] + dy), int(c[2] + dz)), ()):
                        if float(np.linalg.norm(item[1] - p)) <= tol:
                            yield item

    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int]] = set()
    for ex, s, t in cands[0]:
        for p, q in ((s, t), (t, s)):
            dx = _unit(q - p)
            for ey, py, qy in near(y_ends, p):
                if ey == ex:
                    continue
                dy = _unit(qy - py)
                if abs(float(dx @ dy)) > sin_tol:
                    continue
                for ez, pz, qz in near(z_ends, p):
                    if ez in (ex, ey):
                        continue
                    dz = _unit(qz - pz)
                    if abs(float(dx @ dz)) > sin_tol or abs(float(dy @ dz)) > sin_tol:
                        continue
                    key = (ex, ey, ez)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(_axis_info(len(out), (p + py + pz) / 3, dx, dy, dz, key))
                    if len(out) >= LOCAL_AXES_MAX:
                        return out
    return out


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _axis_info(idx: int, origin: np.ndarray, dx: np.ndarray, dy: np.ndarray, dz: np.ndarray, edge_ids: tuple[int, int, int]) -> dict[str, Any]:
    right = float(np.cross(dx, dy) @ dz) > 0
    x = dx
    y = _unit(dy - x * float(dy @ x))
    z = np.cross(x, y) if right else -np.cross(x, y)
    worst = max(
        abs(90 - math.degrees(math.acos(max(-1.0, min(1.0, float(a @ b))))))
        for a, b in ((dx, dy), (dx, dz), (dy, dz))
    )
    info: dict[str, Any] = {
        "id": idx,
        # 座標の変換に使うので、ほかの値（6 桁）より細かく持つ（100mm 先で 1e-5 の誤差にならないように）。
        "origin": vec(origin, 9),
        "x": vec(x, 9),
        "y": vec(y, 9),
        "z": vec(z, 9),
        "edges": list(edge_ids),
        "rightHanded": right,
        "orthogonalityErrorDeg": rnd(worst, 4),
    }
    return info


def to_local_point(axis: dict[str, Any], p: Sequence[float]) -> list[float]:
    """ワールド座標の点をローカル座標にする（local = [x·(p−o), y·(p−o), z·(p−o)]）。"""
    d = np.asarray(p, float) - np.asarray(axis["origin"], float)
    return vec([float(np.asarray(axis[k], float) @ d) for k in ("x", "y", "z")])


def to_local_vector(axis: dict[str, Any], v: Sequence[float]) -> list[float]:
    """ワールド座標の向き（法線・軸・差）をローカル座標にする。"""
    w = np.asarray(v, float)
    return vec([float(np.asarray(axis[k], float) @ w) for k in ("x", "y", "z")])
