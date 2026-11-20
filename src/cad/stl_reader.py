"""STL（ASCII / バイナリ / 色付きバイナリ）の読み込みと、三角形の平面領域へのまとめ。

numpy だけで動く（cadquery 不要）。CadQuery 本体は STL を読めず、OCP の RWStl は
三角形ごとの色を捨てるため自前で読む。

色付き STL はバイナリ STL の各三角形の属性 2 バイトに 5 ビットずつ RGB を入れる方式で、
流儀が 2 つある（どちらも事実上の標準で、仕様書は無い）:

- VisCAM / SolidView 方式: bit15 が 1 なら色あり。bit10-14 赤・bit5-9 緑・bit0-4 青。
- Materialise Magics 方式: 80 バイトのヘッダーに ``COLOR=`` と既定色 RGBA 4 バイトがある。
  bit15 が 0 なら色あり（bit0-4 赤・bit5-9 緑・bit10-14 青）、1 なら既定色。

ASCII STL は色を持たない（``color`` 行等の標準外の拡張は扱わない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# 色の無い三角形（VisCAM 方式で bit15 が 0 のもの）に付ける色。
UNCOLORED_RGB = (180, 180, 180)

_BINARY_DTYPE = np.dtype(
    [
        ("normal", "<f4", (3,)),
        ("v", "<f4", (3, 3)),
        ("attr", "<u2"),
    ]
)
_ASCII_VERTEX_RE = re.compile(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)")


class StlError(ValueError):
    """STL として読めないファイル。"""


@dataclass
class StlMesh:
    """読み込んだ STL。

    Attributes:
        triangles: (n, 3, 3) の float64。三角形ごとの 3 頂点の座標。
        colors: (n, 3) の uint8（RGB）。色付き STL でなければ None。
        colored: (n,) の bool。その三角形に色が指定されていたか（colors が None なら None）。
        color_format: "viscam" / "magics" / None。
        binary: バイナリ STL なら True。
    """

    triangles: np.ndarray
    colors: np.ndarray | None
    colored: np.ndarray | None
    color_format: str | None
    binary: bool


def read_stl(path: str | Path) -> StlMesh:
    """STL ファイルを読む（バイナリかどうかはファイルサイズで判定する）。

    先頭が ``solid`` で始まるバイナリ STL もあるため、先頭の文字ではなく
    「84 + 50 × 三角形数」とファイルサイズが一致するかで判定する。

    Raises:
        StlError: STL として読めない場合。
    """
    data = Path(path).read_bytes()
    if len(data) >= 84:
        count = int(np.frombuffer(data, dtype="<u4", count=1, offset=80)[0])
        if len(data) == 84 + 50 * count:
            return _read_binary(data, count)
    if data.lstrip()[:5].lower() == b"solid":
        return _read_ascii(data)
    raise StlError("Not an STL file (neither a binary STL whose size matches its triangle count nor an ASCII STL).")


def _read_binary(data: bytes, count: int) -> StlMesh:
    if count == 0:
        raise StlError("The STL file has no triangles.")
    records = np.frombuffer(data, dtype=_BINARY_DTYPE, count=count, offset=84)
    triangles = records["v"].astype(np.float64)
    attr = records["attr"].astype(np.uint32)
    colors, colored, color_format = _decode_colors(data[:80], attr)
    return StlMesh(triangles, colors, colored, color_format, binary=True)


def _expand5(v: np.ndarray) -> np.ndarray:
    """5 ビットの色成分を 8 ビットへ広げる（31 → 255）。"""
    return ((v << 3) | (v >> 2)).astype(np.uint8)


def _decode_colors(header: bytes, attr: np.ndarray) -> tuple[np.ndarray | None, np.ndarray | None, str | None]:
    """属性 2 バイトから色を読む。色が 1 つも無ければ (None, None, None)。"""
    low, mid, high = attr & 31, (attr >> 5) & 31, (attr >> 10) & 31
    flag = (attr >> 15) & 1
    pos = header.find(b"COLOR=")
    if pos >= 0 and pos + 10 <= len(header):
        default = np.array(list(header[pos + 6 : pos + 9]), dtype=np.uint8)
        colors = np.stack([_expand5(low), _expand5(mid), _expand5(high)], axis=1)
        use_default = flag == 1
        colors[use_default] = default
        return colors, np.ones(len(attr), dtype=bool), "magics"
    colored = flag == 1
    if not colored.any():
        return None, None, None
    colors = np.stack([_expand5(high), _expand5(mid), _expand5(low)], axis=1)
    colors[~colored] = np.array(UNCOLORED_RGB, dtype=np.uint8)
    return colors, colored, "viscam"


def _read_ascii(data: bytes) -> StlMesh:
    values = _ASCII_VERTEX_RE.findall(data)
    if not values or len(values) % 3:
        raise StlError("Could not read the vertices of the ASCII STL file.")
    try:
        coords = np.array(values, dtype=np.float64)
    except ValueError as e:
        raise StlError(f"The ASCII STL file has a vertex that is not a number: {e}") from e
    return StlMesh(coords.reshape(-1, 3, 3), None, None, None, binary=False)


@dataclass
class PlanarRegions:
    """三角形を平面領域へまとめた結果。

    Attributes:
        region: (n,) の int。三角形ごとの領域番号（0 始まり）。
        vertex_ids: (n, 3) の int。溶接（同じ座標の頂点を 1 つにまとめた）後の頂点番号。
        vertices: (m, 3) の float64。溶接後の頂点座標。
        normals: (n, 3) の float64。三角形ごとの単位法線（面積 0 の三角形は 0 ベクトル）。
        areas: (n,) の float64。三角形の面積。
        boundaries: {(領域a, 領域b): [(頂点番号, 頂点番号), ...]}。a < b、外周（隣が無い）は b = -1 ではなく
            (a, -1) の形で入る。領域の境目の線分。
    """

    region: np.ndarray
    vertex_ids: np.ndarray
    vertices: np.ndarray
    normals: np.ndarray
    areas: np.ndarray
    boundaries: dict[tuple[int, int], list[tuple[int, int]]]


def group_planar_regions(triangles: np.ndarray, colors: np.ndarray | None = None, angle_deg: float = 1.0) -> PlanarRegions:
    """隣り合い、法線の向きが近く、同じ色の三角形を 1 つの平面領域にまとめる。

    領域の最初の三角形（種）の法線との角度が angle_deg 以下の隣接三角形だけを足していく
    （隣どうしだけで比べると、曲面を少しずつ曲がりながら 1 領域に飲み込んでしまうため）。
    面積 0 の三角形は向きを持たないので、隣の領域にそのまま入れる。
    """
    n = len(triangles)
    diag = float(np.linalg.norm(triangles.reshape(-1, 3).max(axis=0) - triangles.reshape(-1, 3).min(axis=0))) or 1.0
    # 同じ座標の頂点を 1 つにまとめる（STL は三角形ごとに頂点を持つため、隣接を知るのに要る）。
    quantum = diag * 1e-7
    keys = np.round(triangles.reshape(-1, 3) / quantum).astype(np.int64)
    uniq, inverse = np.unique(keys, axis=0, return_inverse=True)
    vertex_ids = inverse.reshape(n, 3)
    vertices = np.zeros((len(uniq), 3))
    np.add.at(vertices, inverse.ravel(), triangles.reshape(-1, 3))
    vertices /= np.bincount(inverse.ravel(), minlength=len(uniq))[:, None]

    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    length = np.linalg.norm(cross, axis=1)
    areas = length / 2.0
    normals = np.zeros_like(cross)
    nonzero = length > (diag * diag * 1e-14)
    normals[nonzero] = cross[nonzero] / length[nonzero, None]

    # 辺（頂点番号の組）ごとに、その辺を持つ三角形を集める。
    a = vertex_ids[:, [0, 1, 2]].ravel()
    b = vertex_ids[:, [1, 2, 0]].ravel()
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    edge_key = lo.astype(np.int64) * len(uniq) + hi
    edge_tri = np.repeat(np.arange(n), 3)
    order = np.argsort(edge_key, kind="stable")
    sorted_keys = edge_key[order]
    sorted_tris = edge_tri[order]
    starts = np.flatnonzero(np.r_[True, sorted_keys[1:] != sorted_keys[:-1]])
    ends = np.r_[starts[1:], len(sorted_keys)]

    neighbors: list[list[int]] = [[] for _ in range(n)]
    for s, e in zip(starts.tolist(), ends.tolist()):
        if e - s < 2:
            continue
        tris = sorted_tris[s:e].tolist()
        for i in range(len(tris) - 1):
            neighbors[tris[i]].append(tris[i + 1])
            neighbors[tris[i + 1]].append(tris[i])

    cos_tol = float(np.cos(np.radians(angle_deg)))
    color_key = colors.astype(np.int64) @ np.array([65536, 256, 1]) if colors is not None else np.zeros(n, dtype=np.int64)
    region = np.full(n, -1, dtype=np.int64)
    current = 0
    for seed in range(n):
        if region[seed] >= 0:
            continue
        region[seed] = current
        seed_normal = normals[seed]
        seed_color = color_key[seed]
        has_dir = bool(nonzero[seed])
        stack = [seed]
        while stack:
            t = stack.pop()
            for u in neighbors[t]:
                if region[u] >= 0 or color_key[u] != seed_color:
                    continue
                if nonzero[u]:
                    if not has_dir:
                        # 面積 0 の三角形から始まった領域は、最初に出会った向きを種の向きにする。
                        seed_normal, has_dir = normals[u], True
                    elif float(normals[u] @ seed_normal) < cos_tol:
                        continue
                region[u] = current
                stack.append(u)
        current += 1

    boundaries: dict[tuple[int, int], list[tuple[int, int]]] = {}
    sorted_lo = lo[order]
    sorted_hi = hi[order]
    for s, e in zip(starts.tolist(), ends.tolist()):
        regs = sorted({int(region[t]) for t in sorted_tris[s:e].tolist()})
        if e - s == 1:
            pair = (regs[0], -1)
        elif len(regs) >= 2:
            pair = (regs[0], regs[1])
        else:
            continue
        boundaries.setdefault(pair, []).append((int(sorted_lo[s]), int(sorted_hi[s])))
    return PlanarRegions(region, vertex_ids, vertices, normals, areas, boundaries)
