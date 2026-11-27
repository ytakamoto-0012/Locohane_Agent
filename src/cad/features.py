"""穴・ボス（円柱の特徴）の認識と、モデルのダイジェスト。numpy だけで動く（cadquery 不要）。

丸以外の穴（四角・角 R の四角・正多角形）と、閉じたメッシュ（STL）の穴は src/cad/profile_holes.py が足す
（``shape`` と形ごとの大きさを持つ。ここで認識する丸穴は ``shape`` を持たず ``diameter`` だけ）。

ビューア用データの面・エッジの情報（src/cad/export_mesh.py の _face_info / _edge_info）から求める。
小さなローカル LLM に面の生の一覧（数百〜数千件）を読ませる代わりに、「φ5 の通し穴 ×4」のような
特徴の一覧を渡すためのもの（build123d-mcp の inspect_features と同じ考え方）。

- 穴（hole）: 内向きの円柱面（``concave: true``）。同じ軸・同じ半径で、エッジを共有してつながる面は 1 つの穴にまとめる
  （円柱は継ぎ目で 2 枚に分かれることがある）。周り 1 周を覆うもの（被覆率 ≥ 0.98）だけを数える（フィレットは除く）。
- ボス（boss）: 外向きの円柱面で、同じく 1 周を覆うもの（丸棒・ピン）。
- 穴の両端の円のエッジの先の面が、その円の中に収まる（底面・ドリルの先の円錐）なら、その端は「ふさがっている」。
  両端とも開いていれば通し穴（through）、片方がふさがっていれば止まり穴（blind）。座ぐりの大きい方の穴は、
  段の面（円環）が大きい方の円の中に収まるので、深さ＝座ぐりの深さの止まり穴として出る。

ID は ``hole:<n>`` / ``boss:<n>``（このモデルの中だけで有効。ダイジェストとセットで使う）。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import numpy as np

from .mesh_data import rnd, vec
from .profile_holes import profile_size_fields

# 1 周を覆うとみなす被覆率（面積 ÷ (2πr × 軸方向の長さ)）。
_FULL_COVERAGE = 0.98
# LLM へ返す特徴の要約のグループ数の上限（それ以上は件数だけ）。
SUMMARY_MAX_GROUPS = 12


def _canonical_axis(direction: list[float]) -> np.ndarray:
    d = np.asarray(direction, float)
    n = float(np.linalg.norm(d))
    if n == 0:
        return d
    d = d / n
    # 向きの符号をそろえる（最初の 0 でない成分を正にする）。
    for c in d:
        if abs(c) > 1e-9:
            return d if c > 0 else -d
    return d


def _axis_key(face: dict, tol: float) -> tuple | None:
    """同じ軸・同じ半径の円柱面を同じキーにする（位置と半径は tol で丸める）。"""
    if face.get("geomType") != "CYLINDER" or "radius" not in face or "axisDirection" not in face or "axisOrigin" not in face:
        return None
    d = _canonical_axis(face["axisDirection"])
    o = np.asarray(face["axisOrigin"], float)
    # 軸の上で原点に最も近い点（軸のどこを origin にしても同じになる）。
    foot = o - d * float(o @ d)
    # 丸めの境目で同じ軸が別のキーになるのを避けるため、位置と半径は許容差の 1000 倍の刻みで丸める。
    step = tol * 1000
    return (
        tuple(round(float(x), 4) + 0.0 for x in d),
        tuple(int(round(float(x) / step)) for x in foot),
        int(round(float(face["radius"]) / step)),
    )


def recognize_features(faces: list[dict], edges: list[dict], diag: float) -> list[dict]:
    """穴とボスの一覧を返し、属する面の情報に ``feature`` を付ける（faces を書き換える）。"""
    tol = max(diag * 1e-6, 1e-9)
    groups: dict[tuple, list[int]] = {}
    for f in faces:
        key = _axis_key(f, tol)
        if key is not None and "concave" in f:
            groups.setdefault((key, bool(f["concave"])), []).append(int(f["id"]))

    features: list[dict] = []
    counters = {"hole": 0, "boss": 0}
    for (_key, concave), ids in groups.items():
        for comp in _connected(ids, faces):
            feat = _make_feature(comp, concave, faces, edges, tol)
            if feat is None:
                continue
            kind = feat["type"]
            feat["id"] = f"{kind}:{counters[kind]}"
            counters[kind] += 1
            for fid in comp:
                faces[fid]["feature"] = feat["id"]
            features.append(feat)
    return features


def _connected(ids: list[int], faces: list[dict]) -> list[list[int]]:
    """同じキーの面を、エッジを共有するもの同士でまとめる。"""
    idset = set(ids)
    seen: set[int] = set()
    out: list[list[int]] = []
    edge_faces: dict[int, list[int]] = {}
    for fid in ids:
        for e in faces[fid].get("edges") or []:
            edge_faces.setdefault(e, []).append(fid)
    for start in ids:
        if start in seen:
            continue
        comp, stack = [], [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for e in faces[cur].get("edges") or []:
                for nb in edge_faces.get(e, []):
                    if nb in idset and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        out.append(sorted(comp))
    return out


def _make_feature(comp: list[int], concave: bool, faces: list[dict], edges: list[dict], tol: float) -> dict | None:
    first = faces[comp[0]]
    r = float(first["radius"])
    d = _canonical_axis(first["axisDirection"])
    o = np.asarray(first["axisOrigin"], float)
    # 軸方向の範囲（面のエッジの端点を軸に射影）。
    ts: list[float] = []
    comp_edges: set[int] = set()
    for fid in comp:
        for e in faces[fid].get("edges") or []:
            comp_edges.add(e)
            info = edges[e]
            for key in ("start", "end", "circleCenter"):
                if key in info:
                    ts.append(float((np.asarray(info[key], float) - o) @ d))
    if len(ts) < 2:
        return None
    t0, t1 = min(ts), max(ts)
    length = t1 - t0
    if length <= tol:
        return None
    area = sum(float(faces[f].get("area") or 0.0) for f in comp)
    coverage = area / (2 * math.pi * r * length) if r > 0 else 0.0
    if coverage < _FULL_COVERAGE:
        return None  # フィレット等の部分的な円柱
    kind = "hole" if concave else "boss"
    feat: dict[str, Any] = {
        "type": kind,
        "diameter": rnd(2 * r, 4),
        "length" if kind == "boss" else "depth": rnd(length, 4),
        "axisDirection": vec(d),
        "axisStart": vec(o + d * t0),
        "axisEnd": vec(o + d * t1),
        "faces": comp,
    }
    if kind == "hole":
        caps = _cap_faces(comp, comp_edges, r, faces, edges, tol)
        feat["through"] = not caps
        if caps:
            feat["bottomFaces"] = sorted(caps)
    return feat


def _end_edges(comp: list[int], comp_edges: set[int], r: float, edges: list[dict], tol: float) -> list[int]:
    """穴の端の円（半径 r の CIRCLE で、穴の面のどれか 1 つだけに属し、継ぎ目でないもの）。"""
    compset = set(comp)
    out = []
    for e in comp_edges:
        info = edges[e]
        if info.get("seam") or info.get("geomType") != "CIRCLE":
            continue
        if abs(float(info.get("radius", -1)) - r) > max(tol * 10, r * 1e-6):
            continue
        if any(f not in compset for f in info.get("faces") or []):
            out.append(e)
    return out


def _cap_faces(comp: list[int], comp_edges: set[int], r: float, faces: list[dict], edges: list[dict], tol: float) -> set[int]:
    compset = set(comp)
    caps: set[int] = set()
    limit = 2 * r * (1 + 1e-4) + tol * 10
    for e in _end_edges(comp, comp_edges, r, edges, tol):
        for f in edges[e].get("faces") or []:
            if f in compset:
                continue
            size = (faces[f].get("bbox") or {}).get("size") or [math.inf]
            if max(size) <= limit:
                caps.add(f)
    return caps


def feature_group_key(feature: dict) -> tuple:
    """要約でまとめるキー: 種類・形と大きさ（丸は直径。src/cad/profile_holes.py の profile_size_fields）・深さ・通しかどうか。
    ((項目, 値), ...) の並びなので、dict(key) で要約の 1 行の項目に戻せる。"""
    kind = feature["type"]
    items: list[tuple[str, Any]] = [("type", kind)]
    items += list(profile_size_fields(feature).items())
    items.append(("depth" if kind == "hole" else "length", feature.get("depth", feature.get("length"))))
    if kind == "hole":
        items.append(("through", feature.get("through")))
    return tuple(items)


def summarize_features(features: list[dict]) -> dict[str, Any]:
    """LLM へ返す特徴の要約（大きさが件数によらず一定）。同じ種類・形と大きさ・深さ・通しかどうかでまとめる。"""
    groups: dict[tuple, int] = {}
    for f in features:
        key = feature_group_key(f)
        groups[key] = groups.get(key, 0) + 1
    rows = []
    for key, n in sorted(groups.items(), key=lambda kv: (kv[0][0][1], -kv[1], str(kv[0]))):
        rows.append({**dict(key), "count": n})
    out: dict[str, Any] = {
        "holes": sum(1 for f in features if f["type"] == "hole"),
        "bosses": sum(1 for f in features if f["type"] == "boss"),
        "groups": rows[:SUMMARY_MAX_GROUPS],
    }
    if len(rows) > SUMMARY_MAX_GROUPS:
        out["more_groups"] = len(rows) - SUMMARY_MAX_GROUPS
    return out


def model_digest(faces: list[dict], edges: list[dict], vertices: list[dict]) -> str:
    """面・エッジ・頂点の並びと形の要約から作る短いダイジェスト。

    要素の ID は並び順なので、形が変わると同じ ID が別の要素を指す。選択結果とコードの対象の形が
    同じかを確かめるために使う（同じ形を読み直せば同じ値になる）。
    """
    sig = {
        "f": [[f.get("geomType"), _r(f.get("area")), _rv(f.get("center"))] for f in faces],
        "e": [[e.get("geomType"), _r(e.get("length")), _rv(e.get("start")), _rv(e.get("end"))] for e in edges],
        "v": [_rv(v.get("point")) for v in vertices],
    }
    return hashlib.sha256(json.dumps(sig, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def _r(x: Any) -> float | None:
    return round(float(x), 3) if isinstance(x, (int, float)) else None


def _rv(v: Any) -> list[float] | None:
    return [round(float(c), 3) + 0.0 for c in v] if isinstance(v, list) else None
