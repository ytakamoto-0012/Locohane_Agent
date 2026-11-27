"""show_cad_viewer ツール（チャット内の 3D ビューアで形を見せ、利用者が選んだ面・エッジ・頂点・部品を返す）。

形は CAD ファイル（src/cad/formats.py の拡張子）か CadQuery コードから作る。どちらもサブプロセス
（src/cad/run_cad.py）でビューア用 JSON にし、そのセッションの `_tmp_` フォルダに置く。
コードの実行は execute_python_code と同じ扱い（[scripts] code_execution_enabled・計画の承認・書き込みガード）。

ビューアは cl.AskElementMessage + CustomElement(name="CadViewer") で出し、
frontend/src/components/CadViewerBar.tsx が {submitted, selected: [{kind, id}]} を返す
（ask_user_choice の multi_select と同じ経路。スレッドを切り替えて戻っても答えられる）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import chainlit as cl
from langchain_core.tools import tool

from ..ask_relay import ask_turn
from ..cad.formats import format_of, supported_extensions_text
from ..cad.features import summarize_features
from ..cad.step_summary import summarize_step
from ..cad.topology import TOPOLOGY_KEYS, to_local_point, to_local_vector
from ..cad.mesh_data import FORMAT as VIEWER_FORMAT
from ..cad.mesh_data import apply_view_edits, write_viewer_json
from ..cad.viewer_package import write_package
from ..cad import viewer_cache
from ..i18n import t
from ..images import image_ref, to_data_url
from . import _state
from ._ask_echo import ASK_TIMEOUT_MESSAGE, _echo_ask_answer
from ._ask_relay_helper import _ask_with_cross_session_relay
from ._path_memory_helpers import _register_path_memory
from ._safe_path import _resolve_file_tools_path
from ._proc_tree import run_with_timeout
from ._python_fs_guard import _exec_guard_roots, _python_fs_guard_preamble, _register_exec_output_files
from ._state import _resolve_ask_timeout
from ._subprocess_env import _subprocess_env
from ._workdir import _resolve_exec_workdir, _resolve_workdir, _tmp_dir_parents

logger = logging.getLogger(__name__)

SelectKind = Literal["face", "edge", "vertex", "part", "hole", "outer"]
_ALL_KINDS: tuple[str, ...] = ("face", "edge", "vertex", "part", "hole", "outer")
# 選んだ要素の種類 → ビューア用データでの一覧のキー。hole は開いた形の面の中の穴、outer はつながった面のまとまりの外周
# （src/cad/topology.py）。
_LIST_KEYS = {"face": "faces", "edge": "edgeList", "vertex": "vertexList", "part": "parts", "hole": "holeList", "outer": "outerList"}
# ローカル座標に直して selection_file・計測に添える項目（点と向き）。
# foot は点とエッジの法線距離（point_edge_distance）の垂線の足。
_LOCAL_POINT_KEYS = ("center", "point", "start", "end", "axisOrigin", "circleCenter", "sphereCenter", "apex", "ellipseCenter", "foot")
_LOCAL_VECTOR_KEYS = ("normal", "normalAtCenter", "direction", "axisDirection", "axis", "delta")

_CANCEL_MESSAGE = "Error: the user closed the 3D viewer without confirming a selection."
_RUN_CAD = Path(__file__).resolve().parent.parent / "cad" / "run_cad.py"
# エラー時に LLM へ返す、サブプロセスの出力の末尾の長さ。
_OUTPUT_TAIL_CHARS = 3000
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
# ビューアの指示欄から受け取る文字数の上限（超えた分は切る）。
_MESSAGE_MAX_CHARS = 4000
# ビューアで測った結果を受け取る数の上限（frontend の CadViewerBar.tsx の MEASUREMENTS_MAX と同じ）。
# 1 件は数十文字なので、戻り値にそのまま入れても大きさは抑えられる。
_MEASUREMENTS_MAX = 50
# ビューア用データに書く「元に戻す」の履歴の上限（frontend の editHistory.ts・CadViewerBar.tsx の上限に余裕を持たせた値）。
_HISTORY_MAX_STEPS = 200
_HISTORY_MAX_CHARS = 4_000_000
# ビューアの答えを受け取った後、重い処理の前にイベントループへ譲る時間（閉じる合図をブラウザへ届けるため）。
_UI_FLUSH_SECONDS = 0.05
# 計測の種類（frontend/src/components/cad/measure.ts の MeasureResult.type）ごとに受け取る項目。
# value は必ず要る値。num = 数、vec = 3 次元の点・向き、text = 短い文字列、points = 点の並び、items = 要素の参照の並び、
# item = 要素の参照 1 つ、flag = 真偽値。
_MEASURE_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "radius": {
        "value": ("radius",),
        "num": ("radius", "diameter", "major_radius", "semi_angle_deg"),
        "vec": ("center", "axis"),
        "text": ("geomType", "radius_at"),
        "item": ("item",),
        # 円錐の半径を表示用の三角形の上の点で測ったとき（frontend の measure.ts measureRadius）。
        "flag": ("approximate",),
    },
    "distance": {"value": ("distance",), "num": ("distance",), "vec": ("delta",), "points": ("points",), "items": ("items",), "flag": ("approximate",)},
    # 点からエッジへの法線距離（垂線の足 foot。直線は延長線、円は円全体の上の足なら on_extension）。
    "point_edge_distance": {
        "value": ("distance",),
        "num": ("distance",),
        "vec": ("foot", "delta"),
        "points": ("points",),
        "items": ("items",),
        "text": ("geomType",),
        "flag": ("approximate", "on_extension"),
    },
    "plane_distance": {"value": ("distance",), "num": ("distance",), "vec": ("direction",), "items": ("items",)},
    "plane_angle": {"value": ("angle_deg",), "num": ("angle_deg",), "items": ("items",)},
    "arc3": {
        "value": ("radius",),
        "num": ("radius", "diameter"),
        "vec": ("center", "normal"),
        "points": ("points",),
        "items": ("items",),
        "flag": ("approximate",),
    },
}


def _setting(name: str, default: Any) -> Any:
    return getattr(_state._LLM_CONFIG, name, default) if _state._LLM_CONFIG is not None else default


def _options(compare_with: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {
        "linear_deflection_ratio": _setting("cad_viewer_linear_deflection_ratio", 0.001),
        "angular_deflection_deg": _setting("cad_viewer_angular_deflection_deg", 20.0),
        "max_triangles": _setting("cad_viewer_max_triangles", 1_000_000),
        "stl_region_angle_deg": _setting("cad_viewer_stl_region_angle_deg", 1.0),
        # 開いた形の穴とローカル AXIS（src/cad/topology.py）。キャッシュのキーにも入る。
        "hole_tolerance": _setting("cad_viewer_hole_topology_tolerance", 0.1),
        # 開いたときに縫合しておくか（しなければ 0。ビューアの「縫合」で後から縫合できる）。
        "sew_tolerance": float(_setting("cad_viewer_sew_tolerance", 0.5)) if _setting("cad_viewer_sew_on_open", False) else 0.0,
        "local_axis_lengths": list(_setting("cad_viewer_local_axis_lengths", (100.0, 200.0, 300.0)) or []) or "off",
        "local_axis_length_tolerance": _setting("cad_viewer_local_axis_length_tolerance", 0.1),
        "local_axis_angle_tolerance_deg": _setting("cad_viewer_local_axis_angle_tolerance_deg", 0.5),
        # ゼブラ・曲率の表示に使う頂点ごとの法線と主曲率（src/cad/analysis.py）。
        "surface_analysis": bool(_setting("cad_viewer_surface_analysis", True)),
    }
    if compare_with:
        out["compare_with"] = compare_with
    return out


# ビューア用データがほかのビューア用データを指す連鎖をたどる上限（src/cad/compare.py の LOAD_CHAIN_MAX と同じ）。
_SOURCE_CHAIN_MAX = 10

# 比べる前の形に使える形式（B-rep を持つもの。ビューア用データは元の形のファイルを読み直す）。
_COMPARABLE_FORMATS = ("STEP", "IGES", "BREP", "BIN", "VIEWER_JSON")


def _resolve_input_path(raw: str) -> tuple[Path | None, str]:
    """file_path・compare_with（絶対パス・作業ディレクトリからの相対パス・@N）を絶対パスにする。

    Read/Glob と同じ _resolve_file_tools_path を通す（相対パスは作業ディレクトリ基準、他のセッションの
    `_tmp_` フォルダ・読めない他のユーザーのドラフトは拒否）。使えなければ (None, エラー文。先頭の "Error: " は無し)。
    """
    path, err = _resolve_file_tools_path(raw)
    if err or path is None:
        return None, (err or "the path could not be resolved").removeprefix("Error: ")
    return path, ""


def _source_path_error(data: dict) -> str:
    """ビューア用データが元の形として指すファイル（保存・比較で読み直す）が、このセッションから読めないものなら理由。

    ビューア用データは LLM がコードで書けるので、中の source.path を他のセッションの `_tmp_` 等へ
    向けて、そのファイルを読ませることができないようにする。比べるとき（src/cad/compare.py の load_previous）は
    参照先がまたビューア用データなら、その先もたどるので、連鎖の先のファイルもすべて確かめる。
    参照先の JSON を読むので、大きいときはスレッドで呼ぶ。
    """
    seen: set[str] = set()
    for _ in range(_SOURCE_CHAIN_MAX):
        source = data.get("source") or {}
        if not isinstance(source, dict):
            return ""
        next_json: Path | None = None
        for key in ("path", "brep"):
            src = source.get(key)
            if not src or not isinstance(src, str):
                continue
            if not Path(src).is_absolute():
                # 保存・比較の子プロセスは中の文字列をそのまま（cwd はこのセッションの `_tmp_`）開くので、
                # 相対パス・@N はここで確かめたファイルと別のファイルを指しうる。ビューアが書くパスは常に絶対パス。
                return f"the model file of this viewer data cannot be used: {key} must be an absolute path (got {src!r})"
            path, err = _resolve_input_path(src)
            if err or path is None:
                return f"the model file of this viewer data cannot be used: {err}"
            if key == "path" and format_of(path) == "VIEWER_JSON" and path.is_file():
                next_json = path
        if next_json is None:
            return ""
        marker = os.path.normcase(str(next_json.resolve()))
        if marker in seen:
            return "the viewer data refers back to itself"
        seen.add(marker)
        nxt = _read_viewer_json(next_json)
        if isinstance(nxt, str):
            # 読めない参照先は比べるときにも使われない（load_previous がエラーにする）
            return ""
        data = nxt
    return f"the viewer data refers to other viewer data more than {_SOURCE_CHAIN_MAX} times"


def _read_viewer_json(path: Path) -> dict | str:
    """ビューア用データを読む（大きいとき数秒かかるので、スレッドで呼ぶ）。読めなければ "Error: ..."。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"Error: could not read the viewer data {path}: {e}"
    if not isinstance(data, dict) or data.get("format") != VIEWER_FORMAT:
        return f"Error: {path} is not viewer data made by show_cad_viewer. Pass a CAD file ({supported_extensions_text()})."
    return data


async def _resolve_compare_with(value: str) -> str:
    """compare_with（絶対パスか @N）を絶対パスにする。使えなければ "Error: ..." を返す。"""
    path, err = _resolve_input_path(value)
    if path is None:
        return f"Error: compare_with: {err}"
    if not path.is_file():
        return f"Error: compare_with: the file does not exist: {path}"
    fmt = format_of(path)
    if fmt not in _COMPARABLE_FORMATS:
        return (
            f"Error: compare_with must be a STEP/IGES/BREP/BIN file or viewer_data of one (got {path.suffix!r}; "
            "STL and DXF cannot be compared)."
        )
    if fmt == "VIEWER_JSON":
        # 比べるときはビューア用データが指す元の形のファイルを読み直すので、それもこのセッションから読めるか確かめる。
        data = await asyncio.to_thread(_read_viewer_json, path)
        if isinstance(data, str):
            return data.replace("Error: ", "Error: compare_with: ", 1)
        bad = await asyncio.to_thread(_source_path_error, data)
        if bad:
            return f"Error: compare_with: {bad}"
    return str(path)


# 基準モデル（reference）に使える形式。ビューア用データはその中の三角形をそのまま使う（元のファイルは読まない）。
_REFERENCE_FORMATS = ("STEP", "IGES", "BREP", "BIN", "STL", "VIEWER_JSON")
AlignMode = Literal["none", "bestfit"]


def _resolve_reference(value: str) -> str:
    """reference（絶対パスか @N）を絶対パスにする。使えなければ "Error: ..." を返す。"""
    path, err = _resolve_input_path(value)
    if path is None:
        return f"Error: reference: {err}"
    if not path.is_file():
        return f"Error: reference: the file does not exist: {path}"
    if format_of(path) not in _REFERENCE_FORMATS:
        return f"Error: reference must be a STEP/IGES/BREP/BIN/STL file or viewer_data (got {path.suffix!r}; a DXF is 2D)."
    return str(path)


def _deviation_tolerance() -> float:
    try:
        tol = float(_setting("cad_viewer_deviation_tolerance", 0.1))
    except (TypeError, ValueError):
        return 0.1
    return tol if tol > 0 else 0.1


async def _attach_reference(data: dict, data_path: Path, reference: str, align: str) -> tuple[dict, Path] | str:
    """ビューア用データに基準モデルとの位置合わせ・偏差を足したもの（サブプロセス run_cad.py --reference）と、その JSON。"""
    workdir = _resolve_exec_workdir()
    out = _new_data_path(workdir, prefix="cad_view_reference")
    options = {
        **_options(),
        "reference": reference,
        "align": align,
        "reference_deflection_ratio": _setting("cad_viewer_reference_deflection_ratio", 0.0002),
        "deviation_tolerance": _deviation_tolerance(),
    }
    cmd = [_state._SCRIPT_PYTHON, str(_RUN_CAD), "--reference", str(data_path), "--out", str(out), "--options", json.dumps(options)]
    failed = await _run(cmd, workdir)
    if failed:
        return failed
    made = await asyncio.to_thread(_load_data, out)
    if isinstance(made, str):
        return made
    return made[0], made[1]


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _OUTPUT_TAIL_CHARS else "...\n" + text[-_OUTPUT_TAIL_CHARS:]


@tool(response_format="content_and_artifact")
async def show_cad_viewer(
    question: str,
    file_path: str = "",
    code: str = "",
    select: list[SelectKind] | None = None,
    compare_with: str = "",
    reference: str = "",
    align: AlignMode = "none",
) -> tuple[str, dict | None]:
    """Show a 3D model in an interactive viewer in the chat, and return the elements the user selects.

    The user can rotate, zoom and pan the model, select faces / edges / vertices (or whole parts of an
    assembly, or holes and outer boundaries of an open surface / mesh) by clicking, box selection, "select all" or "same color" / "same part / layer", hide parts / layers, change face and part
    colors, type an instruction for you, and press "Confirm". This tool waits for that, writes the geometry of
    each selected element (type such as PLANE/CYLINDER/LINE/CIRCLE, area or length, center, normal,
    radius, bounding box, neighbouring elements, and CadQuery selectors that pick exactly that
    element) to a JSON file, and returns a short summary with that file as @N. Use it when the user
    wants to point at part of a model ("make a hole in this face", "round this edge"), or just wants
    to look at a model.

    Give exactly one of file_path or code.

    file_path: a CAD file (absolute path or @N). Supported extensions:
      .step/.stp (STEP, keeps assembly parts, names and colors), .iges/.igs (IGES, same),
      .brep/.brp (BREP), .bin (binary BREP), .dxf (2D: closed outlines become faces, open lines
      are free curves, POINTs are free points, layers are parts),
      .stl (ASCII or binary, including colored binary STL; STL has no B-rep, so faces are planar
      regions of triangles and edges are their borders, with no vertices. However many triangles
      it has, an STL can always be read: when the request needs a value the result does not have,
      delegate to the explore sub-agent as id_note in the result says, instead of assuming it
      cannot be read),
      .json (viewer data this tool returned before; shows the same model again without rebuilding).
      Reading a file needs no plan approval.

    code: CadQuery Python code. Put the shape in a variable named `result`, or call
      `show_object(shape)` (several calls are shown together). Workplane, Shape, Sketch, Assembly
      and Vector (a point) are accepted. Like execute_python_code, it runs only after the plan has
      been approved with create_plan/approve_plan, its cwd is this session's temporary folder, and
      writes are limited to the working directory. Files it writes (e.g. an exported .step) are
      registered as @N.

    compare_with: the previous version of the model (a STEP/IGES/BREP/BIN file, or viewer_data this
      tool returned, as an absolute path or @N). The result then has "diff": volumes before / after,
      common, added and removed material (exact B-rep booleans), holes and bosses added or removed,
      solid counts and size, and warnings. The viewer shows added material in green and removed
      material in red.

    reference: a reference (nominal) model to measure the shown model against, e.g. the CAD model
      for a scanned STL, or another version of a part (a STEP/IGES/BREP/BIN/STL file or viewer_data,
      absolute path or @N). Unlike compare_with, it works for STL too and gives a deviation map: the
      signed distance from every point of the shown model to the reference surface (positive = outside
      the reference = extra material). The viewer overlays the reference and can align the models:
      best fit, best fit on the selected faces, or datums A/B/C (3-2-1, like a checking fixture).
      The result then has "comparison" (alignment method, transform, RMS, deviation statistics with
      the tolerance, and deviation per selected face).
    align: how to place the model on the reference before measuring when the viewer opens:
      "none" (keep the coordinates; use this when both are in the same coordinate system) or
      "bestfit" (least-squares best fit, for a scan in its own coordinates). The user can re-align
      in the viewer.

    The viewer can also show the model with zebra stripes (reflection lines from a dome, or from
    horizontal / vertical tube lights on a cylinder or a flat panel) and a curvature color map (mean, Gaussian, max / min principal
    curvature) to check surface quality; no parameter is needed for these.

    Element IDs are 0-based in CadQuery order: face i is
    `cq.Workplane().add(shape).faces().vals()[i]` (same for edges() / vertices()) for the same shape.
    They are valid only for the shape whose model.digest they came from: after the shape changes,
    find elements again by geometry (center, normal, radius) or selectors, not by the old IDs.
    For STL, IDs are mesh regions and have no CadQuery equivalent.

    How to edit a model reliably:
      1. If a size needed for the change was not given by the user, ask the user (ask_user_choice)
         instead of guessing it.
      2. After changing a model with code, call this tool with compare_with set to the previous version
         and check the numbers in model.check and diff (volume added / removed, holes before / after,
         warnings) before saying the edit is done. Do not rely on the picture alone.
      3. If the numbers show an unintended change, fix it at most twice; if it is still wrong, tell the
         user what is wrong and ask how to proceed.

    Args:
        question: What you want the user to select, shown above the viewer
            (e.g. "Select the face to put the hole in").
        file_path: The CAD file to show (see above). Leave empty when using code.
        code: CadQuery code that makes the shape (see above). Leave empty when using file_path.
        select: Which kinds of elements the user may select: any of "face", "edge", "vertex",
            "part", "hole", "outer". Default: all kinds the model has ("part" only for an assembly with 2 or more
            parts, "vertex" not for STL, "hole" only for an open surface or mesh with holes in it, "outer"
            only for an open surface or mesh).
        compare_with: The previous version of the model to compare with (see above). Leave empty
            for no comparison.
        reference: The reference model for the deviation map and alignment (see above). Leave empty
            for none.
        align: "none" or "bestfit" (see above). Used only with reference.

    Returns:
        A short JSON summary whose size does not depend on how many elements were selected:
        selected_count, selected_by_kind, selection_file (@N of a JSON with every selected element
        and its geometry under "selected", and the color changes under "color_changes"; it can be
        very large, so read only the fields you need with json_query), model (format, units, size,
        element counts, digest, check = solids / valid / open edges / volume / warnings, features =
        holes and bosses grouped by diameter and depth, holes = holes inside an open surface or mesh
        grouped by diameter and normal, and local_axes = local coordinate systems found as three
        perpendicular lines), diff when compare_with was given, comparison when reference was given,
        local_axis when the user viewed the
        model in a local coordinate system (then selected elements and measurements also have
        'local' values), and
        viewer_data (@N of the viewer data). If the user changed face / part
        colors in the viewer, also color_change_count (the original model file itself is not changed).
        If the user measured in the viewer (R / diameter, point distance, point-to-edge normal distance,
        plane distance or angle, 3-point arc), also measurements: one small entry per measurement with its values.
        If the user typed an instruction in the viewer, it comes first as user_instruction: treat it
        as the user's next request about the selected elements. Then a small screenshot of the viewer
        as the user saw it when confirming (selected elements highlighted) is also shown to you as an
        image, and screenshot is its @N. If not, it has next_step instead: unless
        the user's earlier request said what to do with the selection, just report the selection briefly
        and stop (do not read selection_file or dispatch agents).
        When the user pressed "Save" in the viewer, the 3D model with the viewer's colors and hidden
        parts applied was exported (STEP for STEP/IGES/BREP/CadQuery, DXF for DXF, colored STL for STL),
        and its download button has already been shown in the chat (no need for provide_download).
        If the user changed colors or the display, viewer_data is viewer data with those changes
        (saved automatically).
        It returns an "Error: ..." string (never raises) if the input is wrong, the code or file
        fails, the plan is not approved for code, the user closes the viewer
        ("Error: the user closed the 3D viewer ..."), or there is no answer within the timeout.
    """
    shot: dict[str, Any] = {}
    text = await _show_cad_viewer(question, file_path, code, select, shot, compare_with, reference, align)
    return text, shot.get("artifact")


async def _show_cad_viewer(
    question: str,
    file_path: str,
    code: str,
    select: list[str] | None,
    shot: dict[str, Any],
    compare_with: str = "",
    reference: str = "",
    align: str = "none",
) -> str:
    """show_cad_viewer の本体。指示付きの確定で画面を撮っていれば、LLM へ見せる画像を shot["artifact"] に入れる。"""
    if not _setting("cad_viewer_enabled", True):
        return "Error: the 3D viewer is disabled in config.ini ([cad_viewer] enabled=false)."
    file_path, code = (file_path or "").strip(), code or ""
    if bool(file_path) == bool(code.strip()):
        return "Error: give exactly one of file_path (a CAD file) or code (CadQuery code)."
    bad = [k for k in (select or []) if k not in _ALL_KINDS]
    if bad:
        return f"Error: unknown kinds in select: {bad}. Use any of {list(_ALL_KINDS)}."

    previous = ""
    if (compare_with or "").strip():
        previous = await _resolve_compare_with(compare_with.strip())
        if previous.startswith("Error:"):
            return previous
    reference_path = ""
    if (reference or "").strip():
        reference_path = _resolve_reference(reference.strip())
        if reference_path.startswith("Error:"):
            return reference_path
    if align not in ("none", "bestfit"):
        return f"Error: align must be 'none' or 'bestfit' (got {align!r})."
    # 大きな CAD ファイルは形を読んでビューア用データにするまで 1 分以上かかることがあるので、その間は準備中と出しておく
    # （ビューアが出るまで何も表示されず、止まったように見えないように）。
    notice = await _show_preparing_notice()
    try:
        if code.strip():
            made = await _viewer_data_from_code(code, previous)
        else:
            made = await _viewer_data_from_file(file_path, previous)
        if not isinstance(made, str) and reference_path:
            # 基準モデルとの位置合わせ・偏差は、できたビューア用データに後から足す（キャッシュには入れない）。
            with_ref = await _attach_reference(made[0], made[1], reference_path, align)
            if isinstance(with_ref, str):
                made = with_ref
            else:
                made = (with_ref[0], with_ref[1], made[2])
    finally:
        await _remove_notice(notice)
    if isinstance(made, str):
        return made
    data, data_path, notes = made

    kinds = _available_kinds(data, select)
    if not kinds:
        return f"Error: the model has none of the requested kinds {select} to select."
    data_token = _register_file(data_path, "3D viewer data (show_cad_viewer)")
    save_id = uuid.uuid4().hex
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    # ブラウザへ送るパッケージ（ビューア用データは props に入れず、ブラウザが HTTP で取りに来る）。
    package_path = _new_data_path(_resolve_exec_workdir(), prefix="cad_package").with_suffix(".lcvd")
    try:
        await asyncio.to_thread(write_package, data, package_path)
    except (OSError, ValueError) as e:
        logger.warning("show_cad_viewer: could not write the viewer package", exc_info=True)
        package_path.unlink(missing_ok=True)
        return f"Error: could not prepare the 3D data for the viewer ({type(e).__name__}: {e})."
    data_id = uuid.uuid4().hex
    _VIEWER_PACKAGES[data_id] = ViewerPackage(path=package_path, thread_id=thread_id)
    _SAVE_TARGETS[save_id] = _SaveTarget(data=data, out_dir=_resolve_workdir(need_write=True), thread_id=thread_id, data_path=data_path)
    align_state = _new_align_state(data, thread_id)
    if align_state is not None:
        _ALIGN_STATES[save_id] = align_state
    try:
        answer = await _ask_selection(question, data, kinds, save_id, data_id, align_state is not None)
    finally:
        target = _SAVE_TARGETS.pop(save_id, None)
        _ALIGN_STATES.pop(save_id, None)
        if align_state is not None:
            # 位置合わせの計算の途中で確定・閉じたときは、終わるのを待つ（終わった結果を LLM へ返す）。
            async with align_state.lock:
                _drop_align_state(align_state)
        _VIEWER_PACKAGES.pop(data_id, None)
        # ビューアは答え（時間切れ・閉じたを含む）を受け取った時点で消えるので、パッケージも消す（大きいため）。
        try:
            package_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("show_cad_viewer: could not remove the viewer package %s", package_path, exc_info=True)
    if target is not None:
        # 「保存」の書き出しの途中で確定・閉じたときは、書き終わるのを待つ（待たないと書き出したことを LLM へ伝えられず、
        # 後からダウンロードボタンだけがチャットに出る）。もう新しい保存は受け付けない（_SAVE_TARGETS から外した）。
        async with target.lock:
            pass
    # 閉じた・時間切れでも LLM へ伝える注意（書き出したモデル・自動で保存したビューア用データ）。
    extra: list[str] = []
    if target is not None and target.saved is not None:
        extra.append(
            f"The user exported the 3D model with the viewer's colors and hidden parts applied to {target.saved_token} "
            "(its download button is already in the chat; no need for provide_download)."
        )
        if target.notes:
            extra.append("Notes on the exported file (tell the user if they matter): " + " / ".join(target.notes))
    if isinstance(answer, str):
        return "\n".join([answer, *extra])
    # ビューアを閉じる合図（clear_ask・remove_element）は送信の待ち行列に入っただけで、イベントループに戻るまで
    # ブラウザへ届かない。この後の書き出し（大きな CAD ではビューア用データが数十 MB）を同期で行うと、その間
    # ビューアが閉じずに固まって見えるので、一度ループに譲ってから重い処理をスレッドで行う。
    await asyncio.sleep(_UI_FLUSH_SECONDS)
    return await asyncio.to_thread(_finish_answer, data, answer, data_token, notes, extra, shot, align_state)


def _finish_answer(
    data: dict,
    answer: tuple[dict[str, Any], list[tuple[str, int]], list[dict[str, Any]], str],
    data_token: str,
    notes: list[str],
    extra: list[str],
    shot: dict[str, Any],
    align_state: _AlignState | None = None,
) -> str:
    """答え後の処理。壊れたビューア用データ（LLM が書いた JSON 等）でも Error: 文字列を返す（例外をもらさない）。"""
    try:
        return _finish_answer_body(data, answer, data_token, notes, extra, shot, align_state)
    except Exception as e:  # noqa: BLE001 - LLM が書いた・手で編集した JSON で落とさない（Error: 文字列で返す）
        logger.warning("show_cad_viewer: could not process the viewer's answer", exc_info=True)
        return f"Error: could not process the viewer's answer ({type(e).__name__}: {e}). The selection was lost."


def _finish_answer_body(
    data: dict,
    answer: tuple[dict[str, Any], list[tuple[str, int]], list[dict[str, Any]], str],
    data_token: str,
    notes: list[str],
    extra: list[str],
    shot: dict[str, Any],
    align_state: _AlignState | None = None,
) -> str:
    """ビューアの答えを受け取った後の処理（自動保存・画面の保存・選択結果の書き出し）。同期処理なのでスレッドで呼ぶ。"""
    res, picked, colors, message = answer
    if align_state is not None and align_state.changed and align_state.values is not None:
        # ビューアで合わせ直した位置と偏差を、自動保存するビューア用データにも残す（開き直すとその位置から始まる）。
        _store_alignment(data, align_state)
        res = {**res, "_alignment_changed": True}
    # ブラウザがビューア用データを読み込めなかった・読み込み中に閉じた（CadViewerBar.tsx の CadViewerLoader）。
    # 表示の状態が無いので、自動保存はしない（しないと、隠していた部品を戻したと受け取ってしまう）。
    load_error = res.get("load_error")
    if isinstance(load_error, str) and load_error.strip():
        return "\n".join(
            [
                f"Error: the user's browser could not load the 3D data for the viewer ({load_error.strip()[:300]}). "
                "Tell the user. If the model is very large, a lower [cad_viewer] max_triangles (fewer triangles) may help.",
                *extra,
            ]
        )
    if res.get("load_cancelled"):
        return "\n".join([_CANCEL_MESSAGE, *extra])
    attr_changes = _attribute_changes(data, _valid_attributes(data, res.get("attributes")))
    # 色・表示・属性を変えていれば、変更を反映したビューア用データを自動で保存し、それを viewer_data として返す。
    edited_token = _auto_save_view(data, res, colors)
    if not res.get("submitted", True):
        if edited_token:
            extra.append(f"The viewer data with the user's color and display changes was saved to {edited_token} (pass it as file_path to show it again).")
        return "\n".join([_CANCEL_MESSAGE, *extra])
    if edited_token:
        data_token = edited_token
        extra.append("viewer_data has the user's color and display changes (saved automatically).")
    screenshot_token = ""
    if message:
        saved = _save_screenshot(res.get("screenshot"))
        if saved is not None:
            screenshot_token = _register_file(saved, "3D viewer screenshot (show_cad_viewer)")
            shot["artifact"] = {
                "image_url": to_data_url(saved),
                "ref": image_ref(saved, screenshot_token if screenshot_token != str(saved) else None),
            }
    measurements = _valid_measurements(data, res.get("measurements"))
    local_axis = _valid_local_axis(data, res.get("local_axis"))
    try:
        comparison = _comparison_summary(data, align_state, picked, res.get("deviation_tolerance"))
    except Exception as e:  # noqa: BLE001 - 比較の要約を作れなくても選択は返す
        logger.warning("show_cad_viewer: could not summarize the comparison", exc_info=True)
        comparison = {"error": f"could not summarize the comparison with the reference model ({type(e).__name__}: {e})"}
    return _format_result(
        data,
        picked,
        data_token,
        [*notes, *extra],
        colors,
        message,
        screenshot_token,
        attr_changes,
        measurements,
        local_axis,
        comparison,
    )


def _screenshot_long_side() -> int:
    """指示付きで確定したときに撮る画面の長辺。[cad_viewer] screenshot_enabled=false か 0 以下なら 0（撮らない）。"""
    if not _setting("cad_viewer_screenshot_enabled", True):
        return 0
    return max(0, int(_setting("cad_viewer_screenshot_long_side_pixels", 512)))


_PNG_DATA_URL_PREFIX = "data:image/png;base64,"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# ブラウザから受け取る画面の画像の大きさの上限（低解像度で撮るので、これを超えるものは受け付けない）。
_SCREENSHOT_MAX_BYTES = 4 * 1024 * 1024


def _save_screenshot(raw: Any) -> Path | None:
    """ビューアが撮った画面（PNG の data URL）を、そのセッションの `_tmp_` フォルダに PNG で保存する。

    撮っていない・形がおかしい・大きすぎる・書けないときは None（選択と指示はそのまま返す）。
    """
    if _screenshot_long_side() <= 0:
        return None
    if not isinstance(raw, str) or not raw.startswith(_PNG_DATA_URL_PREFIX):
        return None
    b64 = raw[len(_PNG_DATA_URL_PREFIX) :]
    if len(b64) > _SCREENSHOT_MAX_BYTES * 4 // 3 + 4:
        logger.warning("show_cad_viewer: the screenshot is too large (%d chars); ignored", len(b64))
        return None
    try:
        png = base64.b64decode(b64, validate=True)
    except ValueError:
        logger.warning("show_cad_viewer: the screenshot is not valid base64; ignored")
        return None
    if not png.startswith(_PNG_SIGNATURE):
        logger.warning("show_cad_viewer: the screenshot is not a PNG; ignored")
        return None
    path = _new_data_path(_resolve_exec_workdir(), prefix="cad_screenshot").with_suffix(".png")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
    except OSError:
        logger.warning("show_cad_viewer: could not save the screenshot", exc_info=True)
        return None
    return path


@dataclass
class _SaveTarget:
    """ビューアの「保存」ボタンの保存先（ビューアを出している間だけ _SAVE_TARGETS に置く）。"""

    data: dict
    out_dir: Path
    thread_id: str
    # 開いたときのビューア用データの JSON（「縫合」のサブプロセスが読む。穴・外周は色・表示の変更によらない）。
    data_path: Path | None = None
    saved: Path | None = None
    saved_token: str = ""
    # 最後に保存したときの書き出しの注意書き（確定したときに LLM にも伝える）。
    notes: list[str] = field(default_factory=list)
    # 書き出しの最中に持つ（同じビューアの保存を 1 つずつにし、確定したときに書き出しの終わりを待つため）。
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# ビューアに渡した save_id → 保存先。ブラウザから来る save_id はこれに無ければ受け付けない。
_SAVE_TARGETS: dict[str, _SaveTarget] = {}


@dataclass(frozen=True)
class ViewerPackage:
    """ブラウザへ送るビューア用データのパッケージ（src/cad/viewer_package.py）。ビューアを出している間だけ置く。"""

    path: Path
    thread_id: str


# ビューアに渡した data_id → パッケージ。app.py の GET /locohane/cad/data/{id} が viewer_package で引く。
# ビューア用データは props に入れない（曲面の多い STL では数百 MB になり、1 通のメッセージ・1 本の文字列として
# ブラウザへ送ると、ブラウザが扱える文字列の長さを超える。会話の保存・ほかの画面への中継でも重い）。
_VIEWER_PACKAGES: dict[str, ViewerPackage] = {}


def viewer_package(data_id: str) -> ViewerPackage | None:
    """ビューアを出している data_id のパッケージ（無ければ None）。"""
    return _VIEWER_PACKAGES.get(data_id)


def _model_path(target: _SaveTarget, suffix: str) -> Path:
    src = target.data.get("source") or {}
    stem = Path(src["path"]).stem if src.get("path") else "cadquery"
    # 同じ秒に別のビューアが同じモデルを保存しても上書きし合わないよう、短い乱数を付ける。
    return target.out_dir / f"{stem}_edited_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{suffix}"


def _initial_hidden(data: dict) -> set[int]:
    """ビューアを開いたときに隠れている部品（CadViewerBar.tsx の initialHiddenParts と同じ）。"""
    view = data.get("viewState") if isinstance(data.get("viewState"), dict) else {}
    if view.get("hiddenParts") is not None and isinstance(view["hiddenParts"], list):
        # LLM が書いたり手で編集したりした JSON で数ではない項目があっても、例外にしない（壊れた項目は捨てる）
        out: set[int] = set()
        for p in view["hiddenParts"]:
            try:
                out.add(int(p))
            except (TypeError, ValueError):
                continue
        return out
    # 部品の id は parts の並び順と同じ（export_mesh._part_info）。手で書いた JSON で id が無い・数でなくても
    # 例外にしない（並び順で補う）。
    out = set()
    for i, p in enumerate(data.get("parts") or []):
        if not isinstance(p, dict):
            continue
        layer = p.get("layer") or {}
        if isinstance(layer, dict) and (layer.get("off") or layer.get("frozen")):
            out.add(_part_id(p, i))
    return out


def _part_id(part: dict, index: int) -> int:
    """部品の id（無い・数でなければ並び順）。"""
    pid = part.get("id")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else index


def _view_edits(data: dict, payload: dict[str, Any], colors: list[dict[str, Any]]) -> tuple[dict, bool]:
    """ブラウザから来た色・隠した部品・種類ごとの表示を適用したビューア用データと、開いたときから変わったか。"""
    parts = data.get("parts") or []
    hidden = payload.get("hidden_parts")
    hidden_parts = [p for p in (hidden if isinstance(hidden, list) else []) if isinstance(p, int) and not isinstance(p, bool) and 0 <= p < len(parts)]
    vis = payload.get("visible")
    visible = {k: bool(vis[k]) for k in ("face", "edge", "vertex") if isinstance(vis, dict) and isinstance(vis.get(k), bool)}
    # LLM が書いた・手で編集した JSON で viewState・visible が辞書でなくても例外にしない（_initial_hidden と同じ）。
    view_state = data.get("viewState") if isinstance(data.get("viewState"), dict) else {}
    saved_visible = view_state.get("visible") if isinstance(view_state.get("visible"), dict) else {}
    before_visible = {"face": True, "edge": True, "vertex": True, **saved_visible}
    changed = bool(colors) or set(hidden_parts) != _initial_hidden(data) or any(before_visible.get(k) != v for k, v in visible.items())
    # ビューアで基準モデルとの位置を合わせ直した（data["deviation"] は _store_alignment で書き換えてある）。
    changed = changed or bool(payload.get("_alignment_changed"))
    attrs = _valid_attributes(data, payload.get("attributes"))
    # ビューアで「縫合」をした（data の穴・外周・面のまとまりは sew_viewer_data が求め直してある）。
    changed = changed or bool(attrs) or payload.get("sewn") is True
    edited = apply_view_edits(
        data,
        face_colors={c["id"]: c["to"] for c in colors if c["kind"] == "face"},
        part_colors={c["id"]: c["to"] for c in colors if c["kind"] == "part"},
        hidden_parts=hidden_parts,
        visible=visible,
        color_changes=colors,
        saved_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        history=_valid_history(data, payload.get("history")),
    )
    if attrs:
        # 製品の属性を置き換え、その製品の部品の情報にも写す（保存・開き直しで編集後の属性になる）。
        edited["edits"]["attribute_changes"] = _attribute_changes(data, attrs)
        for prod in (edited.get("step") or {}).get("products") or []:
            if prod.get("name") in attrs:
                prod["attributes"] = attrs[prod["name"]]
        for part in edited.get("parts") or []:
            if part.get("product") in attrs:
                part["attributes"] = attrs[part["product"]]
    return edited, changed


def _auto_save_view(data: dict, res: dict[str, Any], colors: list[dict[str, Any]]) -> str:
    """確定・閉じたときに、色・表示を変えていれば変更を反映したビューア用データを `_tmp_` に書き、@N を返す。"""
    edited, changed = _view_edits(data, res, colors)
    if not changed:
        return ""
    path = _new_data_path(_resolve_exec_workdir(), prefix="cad_view_edited")
    try:
        _write_atomic(edited, path)
    except OSError:
        logger.warning("show_cad_viewer: could not save the edited viewer data to %s", path, exc_info=True)
        return ""
    return _register_file(path, "3D viewer data with the user's edits (show_cad_viewer)")


# 縫合トレランスの上限（モデルの対角線の長さに対する割合。これより大きいと形全体がつながってしまう）。
_SEW_MAX_RATIO = 0.1


async def sew_viewer_data(payload: dict[str, Any]) -> dict[str, Any]:
    """ビューアの「縫合」（app.py の action_callback "cad_viewer_sew"）。

    payload の tolerance（縫合トレランス。0 で縫合をやめる）で、ビューア用データの穴・外周・面のまとまりを
    サブプロセス（src/cad/run_cad.py --topology）で求め直し、ビューアを出している間のデータ（target.data）を
    書き換える。穴・外周の ID は変わる。形のファイルは変えない。

    Returns:
        {"ok": True, "holeList", "holes", "outerList", "outers", "faceComponents", "stats"} か {"ok": False, "error"}。
    """
    save_id = str(payload.get("save_id") or "")
    target = _SAVE_TARGETS.get(save_id)
    if target is None:
        return {"ok": False, "error": "This viewer is no longer waiting for an answer."}
    if (cl.user_session.get("thread_id") or "_no_session") != target.thread_id:
        return {"ok": False, "error": "This viewer belongs to another conversation."}
    raw = payload.get("tolerance")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw < 0:
        return {"ok": False, "error": "The sewing tolerance must be 0 or a positive number."}
    diag = float(math.hypot(*((target.data.get("bbox") or {}).get("size") or [0.0, 0.0, 0.0])))
    if diag > 0 and raw > diag * _SEW_MAX_RATIO:
        return {"ok": False, "error": f"The sewing tolerance is too large for this model (at most {diag * _SEW_MAX_RATIO:.4g})."}
    hole_tol = float(_setting("cad_viewer_hole_topology_tolerance", 0.1))
    if hole_tol <= 0:
        return {"ok": False, "error": "Holes are not searched ([cad_viewer] hole_topology_tolerance is 0 or less)."}
    if target.data_path is None or not Path(target.data_path).is_file():
        return {"ok": False, "error": "The viewer data is no longer available."}
    if target.lock.locked():
        return {"ok": False, "error": "The model is being saved or sewn. Wait until it finishes."}
    async with target.lock:
        workdir = _resolve_exec_workdir()
        out = _new_data_path(workdir, prefix="cad_topology")
        try:
            cmd = [
                _state._SCRIPT_PYTHON,
                str(_RUN_CAD),
                "--topology",
                str(target.data_path),
                "--out",
                str(out),
                "--options",
                json.dumps({"hole_tolerance": hole_tol, "sew_tolerance": float(raw)}),
            ]
            failed = await _run(cmd, workdir)
            if failed:
                logger.warning("show_cad_viewer: could not sew the faces: %s", failed)
                reason = failed.split("[stderr]\n", 1)[-1].split("\n[stdout]", 1)[0].strip()
                return {"ok": False, "error": reason.removeprefix("Error: ")}
            topo = await asyncio.to_thread(lambda: json.loads(out.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            logger.warning("show_cad_viewer: could not sew the faces", exc_info=True)
            return {"ok": False, "error": str(e)}
        finally:
            out.unlink(missing_ok=True)
        data = target.data
        for key in TOPOLOGY_KEYS:
            data.pop(key, None)
        stats = data.setdefault("stats", {})
        stats.pop("holes", None)
        stats.pop("outers", None)
        for key in TOPOLOGY_KEYS:
            if key in topo:
                data[key] = topo[key]
        stats.update(topo.get("stats") or {})
    return {"ok": True, **{k: topo[k] for k in TOPOLOGY_KEYS if k in topo}, "stats": topo.get("stats") or {}}


async def save_viewer_edits(payload: dict[str, Any]) -> dict[str, Any]:
    """ビューアの「保存」ボタン（app.py の action_callback "cad_viewer_save"）。

    変えた色・隠した部品を反映した 3D モデルを CAD ファイルに書き出し（src/cad/export_model.py。STEP 系 → STEP、
    DXF → DXF、STL → 色付き STL）、@N に登録してチャットにダウンロードボタンを出す。同じビューアで何度保存しても
    同じファイルを上書きする。ビューアは閉じない。

    Returns:
        {"ok": True, "path", "token", "color_changes", "notes"} か {"ok": False, "error"}。
    """
    save_id = str(payload.get("save_id") or "")
    target = _SAVE_TARGETS.get(save_id)
    if target is None:
        return {"ok": False, "error": "This viewer is no longer waiting for an answer."}
    if (cl.user_session.get("thread_id") or "_no_session") != target.thread_id:
        return {"ok": False, "error": "This viewer belongs to another conversation."}
    if target.lock.locked():
        return {"ok": False, "error": "The model is still being saved. Wait until it finishes."}
    async with target.lock:
        return await _save_locked(target, payload)


async def _save_locked(target: _SaveTarget, payload: dict[str, Any]) -> dict[str, Any]:
    """save_viewer_edits の本体（target.lock を持って呼ぶ）。"""
    from ..cad.export_model import output_suffix

    data = target.data
    suffix = output_suffix(data)
    if suffix is None:
        return {"ok": False, "error": "This model cannot be exported."}
    # 色を当てたビューア用データを作るのは、大きなモデルでは数秒かかる（JSON の写し・三角形の色の書き換え）。
    try:
        colors = _valid_colors(data, payload.get("colors"))
        edited, _changed = await asyncio.to_thread(_view_edits, data, payload, colors)
    except Exception as e:  # noqa: BLE001 - 壊れたビューア用データ（LLM が書いた JSON 等）で落とさない
        logger.warning("show_cad_viewer: could not apply the viewer edits", exc_info=True)
        return {"ok": False, "error": f"could not apply the viewer's edits: {e}"}
    edited["exportOptions"] = {"step_write_schema": str(_setting("cad_viewer_step_write_schema", "auto"))}
    out = target.saved or _model_path(target, suffix)
    workdir = _resolve_exec_workdir()
    spec = _new_data_path(workdir, prefix="cad_export")
    tmp_out = out.with_name(f".{out.stem}.writing{out.suffix}")
    try:
        await asyncio.to_thread(_write_atomic, edited, spec)
        cmd = [_state._SCRIPT_PYTHON, str(_RUN_CAD), "--export", str(spec), "--out", str(tmp_out)]
        stdout: list[str] = []
        failed = await _run(cmd, workdir, stdout_sink=stdout)
        if failed:
            logger.warning("show_cad_viewer: could not export the model: %s", failed)
            # 画面に出すのはサブプロセスが標準エラーに書いた理由だけにする。
            reason = failed.split("[stderr]\n", 1)[-1].split("\n[stdout]", 1)[0].strip()
            return {"ok": False, "error": reason.removeprefix("Error: ")}
        os.replace(tmp_out, out)
    except OSError as e:
        logger.warning("show_cad_viewer: could not export the model to %s", out, exc_info=True)
        return {"ok": False, "error": str(e)}
    finally:
        spec.unlink(missing_ok=True)
        # 書き出しに失敗した・置き換えられなかった（保存先を別のプログラムが開いている等）ときの書きかけを残さない。
        # 置き換えた後は tmp_out はもう無い。
        try:
            tmp_out.unlink(missing_ok=True)
        except OSError:
            logger.warning("show_cad_viewer: could not remove %s", tmp_out, exc_info=True)
    if target.saved is None:
        target.saved = out
        target.saved_token = next(
            iter(_register_path_memory([str(out)], description="3D model exported from the 3D viewer (show_cad_viewer)", thread_id=target.thread_id)),
            str(out),
        )
    notes = _export_notes(stdout[0] if stdout else "")
    target.notes = notes
    logger.info("show_cad_viewer: exported the model (%d color changes) to %s; notes: %s", len(colors), out, notes)
    await _offer_download(out)
    return {"ok": True, "path": str(out), "token": target.saved_token, "color_changes": len(colors), "notes": notes}


@dataclass
class _AlignState:
    """基準モデルとの位置合わせ（ビューアを出している間だけ _ALIGN_STATES に置く。キーは save_id）。

    ビューアの「位置合わせ」（app.py の action_callback "cad_viewer_align"）は、ここで覚えた今の変換から
    詰め直して偏差を求め直す。偏差の値はブラウザが GET /locohane/cad/data/{values_id} で取りに来る
    （float32 の並び。_VIEWER_PACKAGES に置く）。
    """

    data: dict
    thread_id: str
    workdir: Path
    transform: Any  # np.ndarray (4,4)
    initial_transform: Any
    alignment: dict[str, Any]
    tolerance: float
    values: Any = None  # np.ndarray（対象の頂点ごとの偏差）。None なら data["deviation"]["values"] のまま
    changed: bool = False
    values_id: str = ""
    values_path: Path | None = None
    index: Any = None  # src.cad.align.SurfaceIndex（初めて合わせ直すときに作る）
    target: Any = None  # src.cad.align.TargetMesh
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_ALIGN_STATES: dict[str, _AlignState] = {}
# ブラウザから受け取る面の ID の数の上限（データム・部分ベストフィット）。
_ALIGN_MAX_FACES = 200_000


def _new_align_state(data: dict, thread_id: str) -> _AlignState | None:
    """基準モデルとの偏差があるビューア用データなら、位置合わせの状態を作る。"""
    dev = data.get("deviation") or {}
    if not data.get("reference") or not dev.get("values") or dev.get("error"):
        return None
    import numpy as np

    from ..cad.align import as_matrix

    T = as_matrix(dev.get("transform"))
    if T is None:
        T = np.eye(4)
    tol = dev.get("tolerance")
    return _AlignState(
        data=data,
        thread_id=thread_id,
        workdir=_resolve_exec_workdir(),
        transform=T,
        initial_transform=T.copy(),
        alignment=dict(dev.get("alignment") or {"method": "none"}),
        tolerance=float(tol) if isinstance(tol, (int, float)) and tol > 0 else _deviation_tolerance(),
    )


def _face_ids(raw: Any, n_faces: int) -> list[int]:
    if not isinstance(raw, list):
        return []
    out = [f for f in raw[:_ALIGN_MAX_FACES] if isinstance(f, int) and not isinstance(f, bool) and 0 <= f < n_faces]
    return sorted(set(out))


def _align_compute(state: _AlignState, method: str, faces: list[int], datums: dict[str, list[int]], dof: dict[str, int]) -> dict[str, Any]:
    """位置合わせと偏差の計算（重いのでスレッドで呼ぶ）。"""
    import numpy as np

    from ..cad.align import SurfaceIndex, align, deviation_stats, deviations, describe_transform
    from ..cad.reference import mesh_arrays, target_mesh

    if state.index is None:
        ref = state.data["reference"]
        pos, idx = mesh_arrays({"mesh": {"positions": ref["positions"], "indices": ref["indices"]}})
        state.index = SurfaceIndex(pos, idx)
        state.target = target_mesh(state.data)
    if method == "reset":
        T, info = np.eye(4), {"method": "none"}
    elif method == "initial":
        T, info = state.initial_transform.copy(), dict((state.data.get("deviation") or {}).get("alignment") or {"method": "none"})
    else:
        T, info = align(state.target, state.index, method, start=state.transform, faces=faces, datums=datums, datum_dof=dof)
    values = deviations(state.target, state.index, T)
    stats = deviation_stats(values, state.target.vertex_weights(), state.tolerance)
    path = state.workdir / f"cad_deviation_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.f32"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(np.ascontiguousarray(values, dtype="<f4").tobytes())
    os.replace(tmp, path)
    state.transform, state.alignment, state.values, state.changed = T, info, values, True
    return {"path": path, "transform": describe_transform(T), "alignment": info, "stats": stats}


async def align_viewer_model(payload: dict[str, Any]) -> dict[str, Any]:
    """ビューアの「位置合わせ」（app.py の action_callback "cad_viewer_align"）。

    payload: {save_id, method: "bestfit" | "faces" | "datum" | "reset" | "initial", faces: [面の ID],
    datums: {A: [...], B: [...], C: [...]}, dof: {A, B, C}, tolerance}。

    Returns:
        {"ok": True, "values_id", "transform", "alignment", "stats"} か {"ok": False, "error"}。
    """
    save_id = str(payload.get("save_id") or "")
    state = _ALIGN_STATES.get(save_id)
    if state is None:
        return {"ok": False, "error": "This viewer has no reference model, or is no longer waiting for an answer."}
    if (cl.user_session.get("thread_id") or "_no_session") != state.thread_id:
        return {"ok": False, "error": "This viewer belongs to another conversation."}
    method = str(payload.get("method") or "")
    if method not in ("bestfit", "faces", "datum", "reset", "initial"):
        return {"ok": False, "error": f"unknown alignment method {method!r}"}
    n_faces = len(state.data.get("faces") or [])
    faces = _face_ids(payload.get("faces"), n_faces)
    raw_datums = payload.get("datums") if isinstance(payload.get("datums"), dict) else {}
    datums = {k: _face_ids(raw_datums.get(k), n_faces) for k in ("A", "B", "C")}
    raw_dof = payload.get("dof") if isinstance(payload.get("dof"), dict) else {}
    dof = {k: int(v) for k, v in raw_dof.items() if k in ("A", "B", "C") and isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 6}
    tol = payload.get("tolerance")
    if isinstance(tol, (int, float)) and not isinstance(tol, bool) and math.isfinite(tol) and tol > 0:
        state.tolerance = float(tol)
    if state.lock.locked():
        return {"ok": False, "error": "The alignment is still running. Wait until it finishes."}
    async with state.lock:
        try:
            out = await asyncio.to_thread(_align_compute, state, method, faces, datums, dof)
        except Exception as e:  # noqa: BLE001 - 点が足りない等は AlignError、壊れたデータでも落とさない
            logger.info("show_cad_viewer: alignment failed: %s", e)
            return {"ok": False, "error": str(e)}
        old_id, old_path = state.values_id, state.values_path
        state.values_id = uuid.uuid4().hex
        state.values_path = out["path"]
        _VIEWER_PACKAGES[state.values_id] = ViewerPackage(path=out["path"], thread_id=state.thread_id)
        if old_id:
            _VIEWER_PACKAGES.pop(old_id, None)
        if old_path is not None:
            old_path.unlink(missing_ok=True)
    logger.info("show_cad_viewer: aligned by %s: %s", method, out["alignment"])
    return {"ok": True, "values_id": state.values_id, "transform": out["transform"], "alignment": out["alignment"], "stats": out["stats"]}


def _store_alignment(data: dict, state: _AlignState) -> None:
    """合わせ直した変換・偏差を data["deviation"] に書く（自動保存するビューア用データに残すため）。"""
    import numpy as np

    from ..cad.align import describe_transform
    from ..cad.mesh_data import b64

    dev = dict(data.get("deviation") or {})
    dev.update(
        values=b64(np.asarray(state.values, dtype=np.float32), "f4"),
        transform=describe_transform(state.transform)["matrix"],
        alignment=state.alignment,
        tolerance=state.tolerance,
    )
    data["deviation"] = dev


def _drop_align_state(state: _AlignState | None) -> None:
    """ビューアを閉じたら、偏差の値のファイルを消す。"""
    if state is None:
        return
    if state.values_id:
        _VIEWER_PACKAGES.pop(state.values_id, None)
    if state.values_path is not None:
        try:
            state.values_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("show_cad_viewer: could not remove %s", state.values_path, exc_info=True)


def _comparison_summary(data: dict, state: _AlignState | None, picked: list[tuple[str, int]], tolerance: Any) -> dict[str, Any] | None:
    """LLM へ返す基準モデルとの比較の概要（位置合わせ・偏差の統計・選んだ面ごとの偏差）。"""
    dev = data.get("deviation") or {}
    ref = data.get("reference") or {}
    if dev.get("error"):
        return {"error": dev["error"]}
    if not ref or not dev:
        return None
    import numpy as np

    from ..cad.align import as_matrix, deviation_stats, describe_transform, face_deviation_stats
    from ..cad.reference import _decode, target_mesh

    tol = float(tolerance) if isinstance(tolerance, (int, float)) and not isinstance(tolerance, bool) and tolerance > 0 else None
    if tol is None:
        tol = state.tolerance if state is not None else float(dev.get("tolerance") or _deviation_tolerance())
    if state is not None and state.values is not None:
        values, T, alignment = state.values, state.transform, state.alignment
    else:
        values = _decode(dev["values"], "f4").astype(np.float64)
        T = as_matrix(dev.get("transform"))
        T = np.eye(4) if T is None else T
        alignment = dev.get("alignment") or {"method": "none"}
    target = state.target if state is not None and state.target is not None else target_mesh(data)
    tf = describe_transform(T)
    out: dict[str, Any] = {
        "reference": (ref.get("source") or {}),
        "alignment": {**alignment, "translation": tf["translation"], "rotation_deg": tf["rotation_deg"], "matrix": tf["matrix"]},
        "deviation": deviation_stats(values, target.vertex_weights(), tol),
        "units": data.get("units", "unknown"),
        "note": (
            "Deviation = signed distance from each point of the shown model (moved by alignment.matrix, which maps the shown "
            "model's coordinates to the reference's) to the reference surface; positive = outside the reference (extra material), "
            "negative = inside (missing material). Statistics are area-weighted. within_tolerance_ratio is the area ratio within "
            "±tolerance."
        ),
    }
    if ref.get("chordTolerance"):
        out["reference_chord_tolerance"] = ref["chordTolerance"]
        out["note"] += " The CAD reference was split into triangles with chords up to reference_chord_tolerance, which bounds the accuracy."
    faces = [eid for kind, eid in picked if kind == "face"][:50]
    if faces:
        out["selected_faces"] = face_deviation_stats(target, np.asarray(values, dtype=np.float64), faces, tol)
    return out


def _export_notes(stdout: str) -> list[str]:
    """書き出しのサブプロセス（run_cad.py --export）が最後の行に出した ``CAD_VIEWER_OK {"notes": [...]}`` の注意書き。"""
    from ..cad.run_cad import OK_MARKER

    for line in reversed(stdout.splitlines()):
        if not line.startswith(OK_MARKER):
            continue
        try:
            notes = json.loads(line[len(OK_MARKER) :].strip()).get("notes")
        except (ValueError, AttributeError):
            return []
        return [str(n) for n in notes if n] if isinstance(notes, list) else []
    return []


async def _show_preparing_notice() -> Any:
    """「3D データを準備しています」の一時的なメッセージを出す（出せなければ None）。ビューアを出す前に _remove_notice で消す。"""
    try:
        return await cl.Message(content=t("notice.cad_viewer_preparing"), type="system_message").send()
    except Exception:  # noqa: BLE001 - 出せなくてもビューアは出せる
        logger.debug("show_cad_viewer: could not show the preparing notice", exc_info=True)
        return None


async def _remove_notice(notice: Any) -> None:
    if notice is None:
        return
    try:
        await notice.remove()
    except Exception:  # noqa: BLE001 - 消せなくても残るのは案内の文だけ
        logger.debug("show_cad_viewer: could not remove the preparing notice", exc_info=True)


async def _offer_download(path: Path) -> None:
    """保存したファイルのダウンロードボタンをチャットに出す（LLM が provide_download を呼ぶのを待たない）。

    保存するたびに出す（cl.File は送った時点の中身を持つので、上書き後は新しいボタンが要る）。
    出せなくても保存自体は成功なので、失敗はログだけにする。
    """
    from .provide_download import download_access_error

    if download_access_error(path):
        logger.warning("show_cad_viewer: did not offer the saved file for download: %s", path)
        return
    try:
        await cl.Message(
            content=t("notice.generated_files", names=path.name),
            elements=[cl.File(name=path.name, path=str(path))],
            type="system_message",
        ).send()
    except Exception:  # noqa: BLE001 - ボタンを出せなくても保存の結果は返す
        logger.warning("show_cad_viewer: could not show the download button for %s", path, exc_info=True)


def _write_atomic(data: dict, out: Path) -> None:
    """書いている途中で壊れたファイルを残さないよう、同じフォルダの一時ファイルに書いて置き換える。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cad_view_", suffix=".tmp", dir=str(out.parent))
    os.close(fd)
    try:
        write_viewer_json(data, tmp)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _register_file(path: Path, description: str) -> str:
    """path を @N に登録し、その @N（登録できなければパスそのもの）を返す。"""
    return next(iter(_register_path_memory([str(path)], description=description)), str(path))


async def _viewer_data_from_file(file_path: str, compare_with: str = "") -> tuple[dict, Path, list[str]] | str:
    path, err = _resolve_input_path(file_path)
    if path is None:
        return f"Error: {err}"
    if not path.is_file():
        return f"Error: the file does not exist: {path}"
    fmt = format_of(path)
    if fmt is None:
        return f"Error: unsupported file type {path.suffix!r}. Supported: {supported_extensions_text()}"
    if fmt == "VIEWER_JSON":
        # 数十 MB になりうるので、イベントループを止めないようスレッドで読む。
        data = await asyncio.to_thread(_read_viewer_json, path)
        if isinstance(data, str):
            return data
        bad = await asyncio.to_thread(_source_path_error, data)
        if bad:
            # 「保存」は元の形のファイルを読み直して書き出すので、読めないファイルを指すデータは開かない。
            return f"Error: {bad}"
        if compare_with:
            # ビューア用データは形を作り直さないので比べられない。元の形のファイルを file_path に渡させる。
            src = (data.get("source") or {}).get("path")
            hint = f" (file_path={src})" if src else ""
            return f"Error: compare_with needs the model itself: pass the model file{hint} or the code, not viewer data, as the new version."
        # 前に比べたときの結果は出し直さない（今回は比べていない）。
        data.pop("diff", None)
        return data, path, []
    if compare_with and fmt not in _COMPARABLE_FORMATS:
        # STL・DXF は B-rep が無く比べられない（黙って diff の無い結果を返すと、比べたと思われる）。
        return f"Error: compare_with needs a STEP/IGES/BREP/BIN model as the new version (got {fmt}; STL and DXF cannot be compared)."

    workdir = _resolve_exec_workdir()
    out = _new_data_path(workdir)
    options = _options(compare_with)
    cache_dir, cache_bytes = _cache_settings()
    # キーは compare_with のビューア用データ（数十 MB になりうる）を読んで作るので、イベントループを止めないようスレッドで。
    key = await asyncio.to_thread(viewer_cache.cache_key, path, options, _state._SCRIPT_PYTHON) if cache_bytes > 0 else None
    cached = await asyncio.to_thread(viewer_cache.lookup, cache_dir, key) if key else None
    if cached is not None:
        # 前に同じファイルを同じ設定で作ったビューア用データを写して使う（会話ごとに別のファイルにして、
        # 色・表示の変更の自動保存がキャッシュや他の会話のデータを書き換えないようにする）。
        try:
            await asyncio.to_thread(shutil.copyfile, cached, out)
            made = await asyncio.to_thread(_load_data, out)
            if not isinstance(made, str):
                logger.info("show_cad_viewer: used the cached viewer data for %s", path.name)
                return made
        except OSError:
            logger.warning("show_cad_viewer: could not use the cached viewer data %s", cached, exc_info=True)
        out.unlink(missing_ok=True)
    cmd = [_state._SCRIPT_PYTHON, str(_RUN_CAD), "--file", str(path), "--out", str(out), "--options", json.dumps(options)]
    failed = await _run(cmd, workdir)
    if failed:
        return failed
    made = await asyncio.to_thread(_load_data, out)
    if key and not isinstance(made, str):
        await asyncio.to_thread(viewer_cache.store, cache_dir, key, out, cache_bytes)
    return made


def _cache_settings() -> tuple[Path, int]:
    """ビューア用データのキャッシュの置き場と上限（バイト。0 以下なら使わない）。"""
    configured = _setting("cad_viewer_cache_dir", None)
    if configured:
        cache_dir = Path(configured)
    else:
        # 設定が無いとき（テスト等）は default_workdir の親（既定の common_data_dir）の下。
        cache_dir = _state._DEFAULT_WORKDIR.parent / "cad_cache"
    return cache_dir, int(_setting("cad_viewer_cache_max_mb", 2048)) * 1024 * 1024


async def _viewer_data_from_code(code: str, compare_with: str = "") -> tuple[dict, Path, list[str]] | str:
    if not _state._CODE_EXEC_ENABLED:
        return "Error: running Python code generated by the LLM is disabled in config.ini ([scripts] code_execution_enabled=false). Pass a CAD file as file_path instead."
    if not cl.user_session.get("plan_approved"):
        logger.info("show_cad_viewer: code blocked because the plan is not approved")
        return (
            "Error: cannot run the CadQuery code because the plan is not approved. "
            "Create a plan with create_plan, get the user's approval with approve_plan, and then call this tool again. "
            "(Showing a CAD file with file_path needs no approval.)"
        )
    from ..cad.run_cad import CODE_EPILOGUE, CODE_PROLOGUE

    workdir = _resolve_exec_workdir()
    out = _new_data_path(workdir)
    try:
        before = {p: p.stat().st_mtime for p in workdir.iterdir() if p.is_file()}
    except OSError:
        before = {}
    try:
        roots, display_roots = _exec_guard_roots()
        guard = _python_fs_guard_preamble(roots, tmp_dir_roots=_tmp_dir_parents(workdir.parent), display_roots=display_roots)
        script = (
            guard
            + CODE_PROLOGUE.format(src_root=str(_state._SRC_DIR.parent))
            + code
            + "\n"
            + CODE_EPILOGUE.format(out_path=str(out), options=_options(compare_with))
        )
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", dir=str(workdir), delete=False, encoding="utf-8")
        tmp.write(script)
        tmp.close()
        script_path = Path(tmp.name)
    except OSError as e:
        return f"Error: could not create a temporary file: {e}"
    try:
        failed = await _run([_state._SCRIPT_PYTHON, str(script_path)], workdir)
    finally:
        script_path.unlink(missing_ok=True)
    # ビューア用データ自体は「コードが作ったファイル」に数えない（今の更新時刻を実行前の値として渡す）。
    # 「保存」で書き出すために残す形の BREP（run_cad.py）も同じ。
    for own in (out, out.with_suffix(".brep")):
        if own.exists():
            before[own] = own.stat().st_mtime
    note = _register_exec_output_files(workdir, before, cl.user_session.get("thread_id") or "_no_session")
    if failed:
        return failed + (f"\n{note}" if note else "")
    made = await asyncio.to_thread(_load_data, out)
    if isinstance(made, str):
        return made
    data, data_path, notes = made
    if note:
        notes.append(note)
    return data, data_path, notes


def _new_data_path(workdir: Path, prefix: str = "cad_view") -> Path:
    return workdir / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.json"


async def _run(cmd: list[str], workdir: Path, stdout_sink: list[str] | None = None) -> str | None:
    """サブプロセスを動かす。失敗したら LLM へ返すエラー文、成功したら None。

    stdout_sink を渡すと、成功したときの標準出力をそこへ足す（書き出しの注意書きを読むため）。
    """
    env = _subprocess_env()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    logger.info("show_cad_viewer: running %s", cmd[1])
    try:
        proc = await asyncio.to_thread(run_with_timeout, cmd, cwd=str(workdir), env=env, timeout=_state._SCRIPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return (
            f"Error: making the 3D model timed out after {_state._SCRIPT_TIMEOUT} seconds "
            "(for large models, raise [scripts] timeout in config.ini, or lower [cad_viewer] max_triangles)."
        )
    except OSError as e:
        return f"Error: could not start making the 3D model: {e}"
    if proc.returncode == 0:
        if stdout_sink is not None:
            stdout_sink.append(proc.stdout or "")
        return None
    parts = [f"Error: making the 3D model failed (exit code {proc.returncode})."]
    if proc.stderr and proc.stderr.strip():
        parts.append(f"[stderr]\n{_tail(proc.stderr)}")
    if proc.stdout and proc.stdout.strip():
        parts.append(f"[stdout]\n{_tail(proc.stdout)}")
    return "\n".join(parts)


def _load_data(out: Path) -> tuple[dict, Path, list[str]] | str:
    try:
        return json.loads(out.read_text(encoding="utf-8")), out, []
    except (OSError, ValueError) as e:
        return f"Error: the 3D model data was not written: {e}"


def _available_kinds(data: dict, select: list[str] | None) -> list[str]:
    """モデルが持っていて、select で許された種類（ビューアに出す順）。"""
    wanted = set(select) if select else set(_ALL_KINDS)
    has = {
        "face": bool(data.get("faces")),
        "edge": bool(data.get("edgeList")),
        "vertex": bool(data.get("vertexList")),
        "part": len(data.get("parts") or []) >= 2,
        "hole": bool(data.get("holeList")),
        "outer": bool(data.get("outerList")),
    }
    return [k for k in _ALL_KINDS if k in wanted and has[k]]


def _valid_selection(data: dict, kinds: list[str], selected: Any) -> list[tuple[str, int]]:
    """ブラウザから来た選択のうち、正しいものだけを (種類, ID) で返す（重複は除く）。"""
    out: list[tuple[str, int]] = []
    # 全選択では数十万件になりうるので、重複は集合で見る（リストを探すと件数の 2 乗の時間がかかり、
    # イベントループ上で呼ぶのでサーバー全体が止まる）。
    seen: set[tuple[str, int]] = set()
    counts = {k: len(data.get(_LIST_KEYS[k]) or []) for k in kinds if k in _LIST_KEYS}
    for s in selected if isinstance(selected, list) else []:
        if not isinstance(s, dict):
            continue
        kind, eid = s.get("kind"), s.get("id")
        if kind not in counts or not isinstance(eid, int) or isinstance(eid, bool):
            continue
        key = (kind, eid)
        if 0 <= eid < counts[kind] and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _valid_color_map(raw: Any, count: int) -> dict[str, str | None] | None:
    """{ID の文字列: "#rrggbb" か null}（範囲外の ID・形の違う値は捨てる）。dict でなければ None。"""
    if not isinstance(raw, dict):
        return None
    out: dict[str, str | None] = {}
    for k, v in raw.items():
        try:
            eid = int(k)
        except (TypeError, ValueError):
            continue
        if 0 <= eid < count and (v is None or (isinstance(v, str) and _HEX_COLOR.fullmatch(v))):
            out[str(eid)] = v.lower() if v else None
    return out


def _valid_history(data: dict, raw: Any) -> dict[str, Any] | None:
    """ビューアの「元に戻す」「やり直す」の履歴（frontend の editHistory.ts の CadSavedHistory）。

    中身はビューアだけが使うが、ビューア用データに書くので形と大きさを確かめる。おかしければ履歴を捨てる
    （開き直したときに前の操作まで戻れなくなるだけで、今の状態は変わらない）。
    """
    # ensure_ascii=False: frontend は JSON.stringify の文字数で上限を守るので、日本語を \uXXXX（6 文字）で数えない。
    if not isinstance(raw, dict) or len(json.dumps(raw, ensure_ascii=False, separators=(",", ":"))) > _HISTORY_MAX_CHARS:
        return None
    n_faces = len(data.get("faces") or [])
    n_parts = len(data.get("parts") or [])

    def edit(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict) or not isinstance(item.get("colors"), dict):
            return None
        faces = _valid_color_map(item["colors"].get("faces"), n_faces)
        parts = _valid_color_map(item["colors"].get("parts"), n_parts)
        hidden = item.get("hiddenParts")
        vis = item.get("visible")
        if faces is None or parts is None or not isinstance(hidden, list) or not isinstance(vis, dict):
            return None
        return {
            "colors": {"faces": faces, "parts": parts},
            "hiddenParts": sorted({p for p in hidden if isinstance(p, int) and not isinstance(p, bool) and 0 <= p < n_parts}),
            "visible": {k: vis.get(k) is not False for k in ("face", "edge", "vertex")},
            "attributes": _valid_attributes(data, item.get("attributes")),
        }

    past, future, base = raw.get("past"), raw.get("future"), raw.get("base")
    if not isinstance(past, list) or not isinstance(future, list) or not isinstance(base, dict):
        return None
    if len(past) > _HISTORY_MAX_STEPS or len(future) > _HISTORY_MAX_STEPS:
        return None
    past_edits = [edit(i) for i in past]
    future_edits = [edit(i) for i in future]
    base_faces = _valid_color_map(base.get("faces"), n_faces)
    base_parts = _valid_color_map(base.get("parts"), n_parts)
    if None in past_edits or None in future_edits or base_faces is None or base_parts is None or not (past_edits or future_edits):
        return None
    return {
        "past": past_edits,
        "future": future_edits,
        "base": {"faces": base_faces, "parts": base_parts, "attributes": _valid_attributes(data, base.get("attributes"), keep_unchanged=True)},
    }


def _valid_colors(data: dict, colors: Any) -> list[dict[str, Any]]:
    """ブラウザから来た色の変更（{"faces": {ID: "#rrggbb" か null}, "parts": {...}}）のうち、正しいものを
    [{"kind", "id", "from", "to"}] で返す。from は元の色（無ければ None）。to が None は「色なしに戻す」
    （自動保存したデータを開き直して「元に戻す」で、色の無かった元の状態へ戻したとき）。"""
    out: list[dict[str, Any]] = []
    if not isinstance(colors, dict):
        return out
    # 色を変えた部品。その部品の面で元と同じ色の上書きは捨てない（ビューアは面の上書きを部品の色より優先して
    # 描くので、捨てると保存した面が部品の色になり、画面と食い違う）。
    part_list = data.get("parts") or []
    raw_parts = colors.get("parts") if isinstance(colors.get("parts"), dict) else {}
    recolored_parts: set[int] = set()
    for sid, hex_color in raw_parts.items():
        try:
            pid = int(sid)
        except (TypeError, ValueError):
            continue
        valid = hex_color is None or (isinstance(hex_color, str) and _HEX_COLOR.fullmatch(hex_color))
        if valid and 0 <= pid < len(part_list) and (part_list[pid].get("color") or "").lower() != (hex_color or "").lower():
            recolored_parts.add(pid)
    for key, kind in (("faces", "face"), ("parts", "part")):
        items = data.get(_LIST_KEYS[kind]) or []
        changes = colors.get(key)
        for sid, hex_color in (changes.items() if isinstance(changes, dict) else []):
            try:
                eid = int(sid)
            except (TypeError, ValueError):
                continue
            if not (0 <= eid < len(items)) or not (hex_color is None or (isinstance(hex_color, str) and _HEX_COLOR.fullmatch(hex_color))):
                continue
            before = items[eid].get("color")
            if (before or "").lower() == (hex_color or "").lower():
                part = items[eid].get("part") if kind == "face" else None
                if not (isinstance(part, int) and part in recolored_parts):
                    continue
            out.append({"kind": kind, "id": eid, "from": before, "to": hex_color.lower() if hex_color else None})
    out.sort(key=lambda c: (c["kind"], c["id"]))
    return out


# ビューアから受け取る属性の編集の上限（1 製品の行数・名前と値の文字数）。
_ATTR_MAX_ROWS = 200
_ATTR_MAX_NAME = 200
_ATTR_MAX_VALUE = 2000
# 数の属性の型（length_measure 等。STEP の本文にそのまま書くので英字と _ だけ）。
_ATTR_TYPE = re.compile(r"[A-Za-z][A-Za-z_]{0,59}")


def _valid_attributes(data: dict, raw: Any, keep_unchanged: bool = False) -> dict[str, list[dict[str, Any]]]:
    """ブラウザから来た STEP の属性の編集（{製品名: [{name, value, type?}]}）のうち、正しいもので、元と違うもの
    （keep_unchanged なら元と同じものも。「元に戻す」の履歴の base は元の属性そのものなので）。"""
    products = {p.get("name"): p for p in (data.get("step") or {}).get("products") or [] if p.get("name")}
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(raw, dict):
        return out
    for name, rows in raw.items():
        if name not in products or not isinstance(rows, list):
            continue
        clean: list[dict[str, Any]] = []
        for row in rows[:_ATTR_MAX_ROWS]:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip():
                continue
            value = row.get("value")
            if isinstance(value, bool) or not isinstance(value, (str, int, float)) and value is not None:
                continue
            if isinstance(value, str):
                value = value[:_ATTR_MAX_VALUE]
            item: dict[str, Any] = {"name": row["name"].strip()[:_ATTR_MAX_NAME], "value": value}
            if isinstance(row.get("type"), str) and isinstance(value, (int, float)) and _ATTR_TYPE.fullmatch(row["type"]):
                item["type"] = row["type"]
            if isinstance(row.get("group"), str) and row["group"]:
                item["group"] = row["group"][:_ATTR_MAX_NAME]
            clean.append(item)
        if keep_unchanged or clean != list(products[name].get("attributes") or []):
            out[name] = clean
    return out


def _attribute_changes(data: dict, edits: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """属性の編集を、名前ごとの {product, name, from, to}（追加は from null、削除は to null）にする。"""
    products = {p.get("name"): p for p in (data.get("step") or {}).get("products") or []}
    changes: list[dict[str, Any]] = []
    for prod, rows in edits.items():
        before = {a.get("name"): a.get("value") for a in products.get(prod, {}).get("attributes") or []}
        after = {a["name"]: a.get("value") for a in rows}
        for key in [*before, *[k for k in after if k not in before]]:
            if before.get(key, None) != after.get(key, None) or (key in before) != (key in after):
                changes.append({"product": prod, "name": key, "from": before.get(key), "to": after.get(key) if key in after else None})
    return changes


def _valid_message(message: Any) -> str:
    """ビューアの指示欄の文字列（無ければ空、長すぎれば切る）。"""
    return message.strip()[:_MESSAGE_MAX_CHARS] if isinstance(message, str) else ""


def _measure_number(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    r = round(f, 6)
    return 0.0 if r == 0 else r


def _measure_vec(v: Any) -> list[float] | None:
    if not isinstance(v, list) or len(v) != 3:
        return None
    out = [_measure_number(x) for x in v]
    return None if any(x is None for x in out) else out  # type: ignore[return-value]


def _measure_item(data: dict, v: Any) -> dict[str, Any] | None:
    """計測に使った要素の参照 {kind, id}（モデルに無い要素なら None）。"""
    if not isinstance(v, dict):
        return None
    kind, eid = v.get("kind"), v.get("id")
    if kind not in _LIST_KEYS or isinstance(eid, bool) or not isinstance(eid, int):
        return None
    return {"kind": kind, "id": eid} if 0 <= eid < len(data.get(_LIST_KEYS[kind]) or []) else None


def _valid_measurements(data: dict, raw: Any) -> list[dict[str, Any]]:
    """ビューアで測った結果（R・距離・平面どうし・3 点円弧）。種類ごとに決めた項目だけを、形を確かめて受け取る。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for m in raw[:_MEASUREMENTS_MAX]:
        if not isinstance(m, dict) or m.get("type") not in _MEASURE_FIELDS:
            continue
        fields = _MEASURE_FIELDS[m["type"]]
        item: dict[str, Any] = {"type": m["type"]}
        for k in fields.get("num", ()):
            if (n := _measure_number(m.get(k))) is not None:
                item[k] = n
        if any(k not in item for k in fields["value"]):
            continue
        for k in fields.get("vec", ()):
            if (vec := _measure_vec(m.get(k))) is not None:
                item[k] = vec
        for k in fields.get("text", ()):
            if isinstance(m.get(k), str) and m[k]:
                item[k] = m[k][:40]
        for k in fields.get("points", ()):
            pts = [_measure_vec(p) for p in m.get(k)] if isinstance(m.get(k), list) else []
            if pts and len(pts) <= 3 and all(p is not None for p in pts):
                item[k] = pts
        for k in fields.get("items", ()):
            if isinstance(m.get(k), list) and len(m[k]) <= 3:
                item[k] = [_measure_item(data, s) for s in m[k]]
        for k in fields.get("item", ()):
            if (ref := _measure_item(data, m.get(k))) is not None:
                item[k] = ref
        for k in fields.get("flag", ()):
            if isinstance(m.get(k), bool):
                item[k] = m[k]
        out.append(item)
    return out


def _valid_local_axis(data: dict, raw: Any) -> dict[str, Any] | None:
    """ビューアで「座標系」に選んでいたローカル AXIS（ビューア用データの localAxes の ID）。選んでいなければ None。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    axes = data.get("localAxes") or []
    axis = axes[raw] if 0 <= raw < len(axes) else None
    if not isinstance(axis, dict) or not all(isinstance(axis.get(k), list) and len(axis[k]) == 3 for k in ("origin", "x", "y", "z")):
        return None
    return axis


def _with_local(item: dict[str, Any], axis: dict[str, Any] | None) -> dict[str, Any]:
    """item の点・向き（_LOCAL_POINT_KEYS・_LOCAL_VECTOR_KEYS）をローカル座標にしたものを item["local"] に添える。"""
    if axis is None:
        return item

    def is_vec(v: Any) -> bool:
        return isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)

    local: dict[str, Any] = {}
    for k in _LOCAL_POINT_KEYS:
        if is_vec(item.get(k)):
            local[k] = to_local_point(axis, item[k])
    for k in _LOCAL_VECTOR_KEYS:
        if is_vec(item.get(k)):
            local[k] = to_local_vector(axis, item[k])
    pts = item.get("points")
    if isinstance(pts, list) and pts and all(is_vec(p) for p in pts):
        local["points"] = [to_local_point(axis, p) for p in pts]
    return {**item, "local": local} if local else item


def _local_axis_summary(axis: dict[str, Any]) -> dict[str, Any]:
    out = {k: axis.get(k) for k in ("id", "origin", "x", "y", "z", "rightHanded", "orthogonalityErrorDeg", "edges")}
    out["note"] = (
        "The user viewed the model in this local coordinate system (local AXIS: three perpendicular lines found in the model; "
        "origin and the unit vectors x, y, z are in world coordinates). local = [x·(p - origin), y·(p - origin), z·(p - origin)] "
        "for a point p, and [x·v, y·v, z·v] for a direction v. Selected elements and measurements also carry 'local' with "
        "their points and directions in this system. Use the local values when the user speaks in local coordinates; CadQuery "
        "code still works in world coordinates."
    )
    if axis.get("rightHanded") is False:
        out["warning"] = "The three lines form a left-handed system (z = -(x × y)). Check the line lengths with the user."
    return out


def _echo_text(
    data: dict,
    picked: list[tuple[str, int]],
    colors: list[dict[str, Any]],
    message: str = "",
    attr_count: int = 0,
    measure_count: int = 0,
) -> str:
    """チャットに残す、利用者の答えの短い文。指示は省かずに後ろへ付ける。

    この文はユーザー発言として会話に保存され、スレッドを再開すると LLM の文脈にも入るので、選んだ要素は
    種類ごとの件数だけにする（一つずつの ID・情報は選択結果の JSON にある）。
    """
    counts = [
        t("cad.count", kind=t(f"cad.kind.{k}"), count=n) for k in _ALL_KINDS if (n := sum(1 for kind, _ in picked if kind == k))
    ]
    text = t("cad.selected_summary", items=t("list.separator").join(counts)) if counts else t("ask.none_selected")
    if colors:
        text += f" / {t('cad.color_changes', count=len(colors))}"
    if attr_count:
        text += f" / {t('cad.attribute_changes', count=attr_count)}"
    if measure_count:
        text += f" / {t('cad.measurements', count=measure_count)}"
    if message:
        text += f"\n\n{message}"
    return text


async def _ask_selection(
    question: str, data: dict, kinds: list[str], save_id: str = "", data_id: str = "", can_align: bool = False
) -> tuple[dict[str, Any], list[tuple[str, int]], list[dict[str, Any]], str] | str:
    # 3D ビューア専用のタイムアウト（[cad_viewer] timeout_seconds）。ビューアを閉じずにブラウザを閉じると、
    # 会話の生成がこの秒数まで待ち続けるので、既定は有限にしてある。
    seconds = int(_setting("show_cad_viewer_timeout_seconds", 3600))
    timeout = _resolve_ask_timeout(seconds)
    thread_id = cl.user_session.get("thread_id") or "_no_session"

    async def _factory():
        # CustomElement はセッション文脈に依存するため、中継時に作り直せるよう毎回ここで作る。
        element = cl.CustomElement(
            name="CadViewer",
            props={
                "question": question,
                "select": kinds,
                # ビューア用データの本体はブラウザが GET /locohane/cad/data/{data_id} で取りに来る（_VIEWER_PACKAGES）。
                "data_id": data_id,
                "save_id": save_id,
                # 指示付きで確定したときに撮る画面の長辺（0 以下なら撮らない）。
                "screenshot_long_side": _screenshot_long_side(),
                # 穴にマウスを乗せたとき、同じ向きとしてまとめる法線の角度差の既定値（度。ビューアで変えられる）。
                "hole_normal_angle_deg": float(_setting("cad_viewer_hole_normal_angle_deg", 3.0)),
                # 穴をまとめるときの穴サイズ誤差の既定値（モデルの単位。0 以下なら初めは条件を外す）。
                "hole_size_tolerance": float(_setting("cad_viewer_hole_size_tolerance", 0.3)),
                # 「縫合」の欄の初めの値（モデルの単位）。
                "sew_tolerance": float(_setting("cad_viewer_sew_tolerance", 0.5)),
                # 待つ秒数（0 = 無期限）。ビューアは残りが少なくなったら残り時間を出す（出し直したときはそこから数える）。
                "timeout_seconds": max(0, seconds),
                # 基準モデルとの位置合わせをビューアからやり直せるか（app.py の cad_viewer_align）と、偏差の公差の既定値。
                "can_align": can_align,
                "deviation_tolerance": float((data.get("deviation") or {}).get("tolerance") or _deviation_tolerance()),
            },
        )
        ask_message = cl.AskElementMessage(content=question, element=element, timeout=timeout)
        res = await ask_message.send()
        # ビューアは回答待ちの間しか描かれず、後から見返す用途も無い（データ ID もこの後すぐ使えなくなる）ので、
        # 答え（時間切れを含む）を受け取ったら消す。
        try:
            await element.remove()
        except Exception:  # noqa: BLE001 - 後片付けの失敗でツールの結果を失わせない
            logger.warning("Could not remove the 3D viewer element", exc_info=True)
        if res is not None:
            if res.get("submitted", True):
                try:
                    answer = _echo_text(
                        data,
                        _valid_selection(data, kinds, res.get("selected")),
                        _valid_colors(data, res.get("colors")),
                        _valid_message(res.get("message")),
                        len(_attribute_changes(data, _valid_attributes(data, res.get("attributes")))),
                        len(_valid_measurements(data, res.get("measurements"))),
                    )
                except Exception:  # noqa: BLE001 - LLM が書いた・手で編集した JSON で落とさない（答えの表示だけ簡単にする）
                    logger.warning("show_cad_viewer: could not summarize the viewer's answer", exc_info=True)
                    answer = t("ask.confirmed")
            else:
                answer = t("ask.cancel")
            await _echo_ask_answer(ask_message, question, answer)
        return res

    async with ask_turn(thread_id):
        res = await _ask_with_cross_session_relay(thread_id, _factory, timeout)
    if res is None:
        return ASK_TIMEOUT_MESSAGE
    if not isinstance(res, dict):
        return "Error: the viewer returned an answer in an unexpected form. The selection was lost."
    # 閉じた（submitted: false）ときも返す（色・表示の変更を自動で保存するため。選択は使わない）。
    try:
        return (
            res,
            _valid_selection(data, kinds, res.get("selected")) if res.get("submitted", True) else [],
            _valid_colors(data, res.get("colors")),
            _valid_message(res.get("message")),
        )
    except Exception as e:  # noqa: BLE001 - 壊れたビューア用データ（LLM が書いた JSON 等）でも Error: 文字列で返す
        logger.warning("show_cad_viewer: could not read the viewer's answer", exc_info=True)
        return f"Error: could not process the viewer's answer ({type(e).__name__}: {e}). The selection was lost."


def _holes_summary(data: dict) -> dict[str, Any]:
    """開いた形の面の中の穴（src/cad/topology.py）の要約（大きさは穴の数によらず一定）。"""
    holes = data.get("holeList") or []
    geom = data.get("holes") or {}
    groups: dict[tuple, int] = {}
    for h in holes:
        key = (round(float(h.get("diameter") or 0), 2), tuple(round(float(c), 2) + 0.0 for c in h.get("normal") or []))
        groups[key] = groups.get(key, 0) + 1
    rows = [{"diameter": d, "normal": list(n), "count": c} for (d, n), c in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0][0]))[:12]]
    out: dict[str, Any] = {
        "count": len(holes),
        "topology_tolerance": geom.get("tolerance"),
        "outer_boundaries": len(data.get("outerList") or []) or geom.get("outerLoops"),
        "groups": rows,
        "note": (
            "Holes inside the open surface / mesh: closed borders other than the outer boundary of each connected group of faces "
            "(gaps up to topology_tolerance count as connected). Each hole (kind 'hole' in the selection) has center, normal "
            "(facing the same way as the surface; faces that are not sewn may disagree on which side is the front, so treat "
            "opposite normals as the same direction), diameter (mean), area, perimeter, the faces around it and, for B-rep, its border edges. "
            "The outer boundaries (kind 'outer', one per connected group of faces) can be selected too and have the same fields."
        ),
    }
    if geom.get("sewTolerance"):
        # ビューアの「縫合」（または [cad_viewer] sew_on_open）で、別の面どうしの離れを縫合トレランスまで閉じた。
        out["sew_tolerance"] = geom["sewTolerance"]
        out["sew_note"] = (
            "Gaps between different faces up to sew_tolerance were treated as sewn when finding holes and outer boundaries "
            "(the model file itself is not changed or sewn)."
        )
    if len(groups) > len(rows):
        out["more_groups"] = len(groups) - len(rows)
    if geom.get("openChains"):
        out["open_borders"] = geom["openChains"]
    return out


def _model_summary(data: dict) -> dict[str, Any]:
    src = data.get("source") or {}
    stats = data.get("stats") or {}
    out: dict[str, Any] = {
        "format": src.get("format", "?"),
        "source": src.get("path") or "your CadQuery code",
        "units": data.get("units", "unknown"),
        "size": (data.get("bbox") or {}).get("size", []),
        "counts": {k: v for k, v in stats.items() if k != "triangles"},
    }
    if data.get("modelDigest"):
        out["digest"] = data["modelDigest"]
    if data.get("check"):
        out["check"] = data["check"]
    if data.get("features"):
        out["features"] = summarize_features(data["features"])
    if data.get("step"):
        out["step"] = summarize_step(data["step"])
    if data.get("holeList") or data.get("outerList"):
        out["holes"] = _holes_summary(data)
    analysis = data.get("analysis") or {}
    if analysis.get("summary"):
        # ゼブラ・曲率の表示に使う主曲率の概要（凸 = 正。method は exact = B-rep の正確な値、mesh = 三角形から推定）。
        out["curvature"] = {"method": analysis.get("method"), **analysis["summary"]}
    if data.get("localAxes"):
        out["local_axes"] = [{k: a.get(k) for k in ("id", "origin", "x", "y", "z", "rightHanded")} for a in (data.get("localAxes") or [])[:5]]
        if len(data["localAxes"]) > 5:
            out["more_local_axes"] = len(data["localAxes"]) - 5
    return out


def _diff_summary(diff: dict) -> dict[str, Any]:
    """前の版との比較の概要（重ね表示用のメッシュは除く）。"""
    if diff.get("error"):
        return {"error": diff["error"]}
    out = {k: v for k, v in diff.items() if k not in ("added", "removed")}
    shown = "added" in diff or "removed" in diff
    out["overlay"] = "shown in the viewer: green = added material, red = removed material" if shown else "none"
    return out


def _id_note(data: dict) -> str:
    if (data.get("source") or {}).get("format") == "STL":
        # メッシュの数が多い（数十万〜数百万）STL で、LLM が「データにアクセスできない」「数が多くて読めない」と
        # 思い込んで断ることがあった。実際には explore サブエージェントが selection_file・STL をコードで読んで
        # 計算できるので、断らずに委譲させる。委譲は依頼が戻り値に無い値を要るときだけ（指示欄が空のまま確定した
        # ときの next_step「何もしない」と食い違わないように）。viewer_data は数百 MB になり、json_query は呼ぶたびに
        # ファイル全体を読み直すので、多くの要素を引くときは execute_python_code_readonly で 1 回だけ読ませる。
        return (
            "IDs are planar regions of the STL mesh (no CadQuery equivalent); 'meshHints' only tell which region is outermost on an axis. "
            "However many triangles the STL has (even millions), there is always a way to read it and compute from it: never assume "
            "otherwise, and never tell the user that the STL or its data cannot be read or accessed. Only when the user's request needs "
            "a value or information that is not in this result, delegate it with dispatch_agent (agent_type explore), passing "
            "selection_file, viewer_data, the STL path (model.source) and what the user asked. Tell it: selection_file is small and has "
            "the selected elements under 'selected' (read it with json_query); viewer_data has every face under 'faces' and every edge "
            "under 'edgeList' (center, normal, area / length, bbox and neighbouring elements) but can be hundreds of MB, and json_query "
            "reads the whole file again on every call, so to look up many elements, read viewer_data (or the STL triangles) once in one "
            "execute_python_code_readonly run and compute everything there."
        )
    return (
        "IDs are 0-based in CadQuery order (face i = cq.Workplane().add(shape).faces().vals()[i]; same for edges/vertices), "
        "valid only for the shape with this model digest; after the shape changes, find elements again by geometry or selectors. "
        "'selectors' are CadQuery selectors verified to pick exactly that one element. "
        "'feature' on a face names the hole or boss it belongs to (details under 'features'); "
        "'pmi' on a face or edge lists the STEP dimensions / tolerances / datums on it (details under 'pmi')."
    )



def _write_selection(
    data: dict,
    picked: list[tuple[str, int]],
    data_token: str,
    colors: list[dict[str, Any]],
    attr_changes: list[dict[str, Any]] | None = None,
    local_axis: dict[str, Any] | None = None,
) -> Path:
    """選んだ要素の詳しい情報（ジオメトリ全部）と色の変更を、そのセッションの `_tmp_` フォルダの JSON に書く。"""
    path = _new_data_path(_resolve_exec_workdir(), prefix="cad_selection")
    body = {
        "model": _model_summary(data),
        "viewer_data": data_token,
        "id_note": _id_note(data),
        "selected": [_with_local({"kind": kind, "id": eid, **data[_LIST_KEYS[kind]][eid]}, local_axis) for kind, eid in picked],
        "color_changes": colors,
    }
    if local_axis is not None:
        body["local_axis"] = _local_axis_summary(local_axis)
    if attr_changes:
        body["attribute_changes"] = attr_changes
    # 選んだ面が属する穴・ボス（直径・深さ・通しかどうか・軸）。
    used = {data["faces"][eid].get("feature") for kind, eid in picked if kind == "face"} - {None}
    if used:
        body["features"] = [f for f in data.get("features") or [] if f.get("id") in used]
    # 選んだ面・エッジに付いた STEP の PMI（寸法・幾何公差・データム）。
    refs = {r for kind, eid in picked if kind in ("face", "edge") for r in data[_LIST_KEYS[kind]][eid].get("pmi") or []}
    if refs:
        pmi = (data.get("step") or {}).get("pmi") or {}
        body["pmi"] = [item for items in pmi.values() for item in items if item.get("id") in refs]
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _format_result(
    data: dict,
    picked: list[tuple[str, int]],
    data_token: str,
    notes: list[str],
    colors: list[dict[str, Any]] | None = None,
    message: str = "",
    screenshot_token: str = "",
    attr_changes: list[dict[str, Any]] | None = None,
    measurements: list[dict[str, Any]] | None = None,
    local_axis: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
) -> str:
    """LLM へ返す概要（JSON 文字列）。

    大きな CAD データでは面が数千・三角形が数十万になり、全選択もできるので、選んだ要素の一覧
    （ID だけでも）や色の変更の一覧は戻り値に入れない。すべて選択結果のファイルに書き、戻り値は
    件数とファイルの @N だけにする（要素数によらず大きさが一定）。
    """
    colors = colors or []
    attr_changes = attr_changes or []
    result: dict[str, Any] = {"status": "confirmed"}
    if message:
        # 利用者がビューアの指示欄に書いた指示。選択（や色の変更）はこの指示の対象。
        result["user_instruction"] = message
        result["user_instruction_note"] = (
            "The user wrote this instruction in the 3D viewer together with the selection below. "
            "Treat it as the user's next request about the selected elements."
        )
        if screenshot_token:
            # 確定したときの画面（選んだ要素は強調表示）。画像はこの結果の直後に見せる。
            result["screenshot"] = screenshot_token
            result["screenshot_note"] = (
                "A small screenshot of the viewer as the user saw it when confirming (selected elements highlighted) "
                "is attached as an image. Use it to understand which part of the model the instruction is about."
            )
    else:
        # 指示欄が空のまま確定した。選択の詳細を読みにサブエージェントを出す等の無駄な作業をさせない。
        result["next_step"] = (
            "The user confirmed without typing an instruction. If the user's earlier request already said what to do "
            "with the selection (e.g. 'make a hole in the face I pick'), do that. Otherwise do nothing more: do not read "
            "selection_file, do not call json_query or any other tool, and do not dispatch agents. Just reply in one or "
            "two short sentences with what was selected (selected_by_kind) and the measured values (measurements), if any, "
            "and wait for the user's next request."
        )
    result |= {
        "selected_count": len(picked),
        "selected_by_kind": {k: n for k in _ALL_KINDS if (n := sum(1 for kind, _ in picked if kind == k))},
    }
    if measurements:
        # 利用者がビューアで測った結果（最大 _MEASUREMENTS_MAX 件、1 件は小さいので戻り値に入れる）。
        result["measurements"] = [_with_local(m, local_axis) for m in measurements]
        units = data.get("units", "unknown")
        result["measurement_note"] = (
            f"The user measured these in the viewer (lengths in the model units: {units}). "
            "radius = R of a cylinder / sphere / cone (at the clicked point) / fillet torus face or circle edge; "
            "distance = between 2 points (delta = x, y, z components); point_edge_distance = perpendicular (normal) distance "
            "from a clicked point to an edge (foot = the foot of the perpendicular on the edge; on_extension: true means the foot "
            "is on the extension of a line or on the full circle of an arc, outside the edge itself); plane_distance / plane_angle = between 2 planar faces; "
            "arc3 = circle through 3 clicked points. items are the elements the points were on (null = none). "
            "approximate: true means a point was on the display triangles of a curved surface, so the value can be off "
            "by the tessellation tolerance (this includes a cone radius taken at a clicked point). The other radii and "
            "plane distances come from the exact model geometry."
        )
    if colors:
        result["color_change_count"] = len(colors)
    if attr_changes:
        result["attribute_change_count"] = len(attr_changes)
        result["attribute_note"] = (
            "The user edited STEP attributes in the viewer (details under 'attribute_changes' in selection_file). "
            "The original file is not changed; the viewer's Save button writes them to the exported STEP."
        )
    if picked or colors or attr_changes:
        try:
            sel_token = _register_file(
                _write_selection(data, picked, data_token, colors, attr_changes, local_axis), "3D viewer selection (show_cad_viewer)"
            )
        except OSError as e:
            logger.warning("show_cad_viewer: could not write the selection file", exc_info=True)
            result["selection_file_error"] = f"could not write the selection details: {e}. Ask the user to select again."
        else:
            result["selection_file"] = sel_token
            result["how_to_read"] = (
                f"Only when the task needs the details: {sel_token} has one entry per selected element under 'selected' (kind, id, geomType, area/length, center, normal, "
                f"radius, bbox, neighbouring faces/edges, selectors)"
                + (", and the color changes under 'color_changes' ({kind, id, from, to} as #rrggbb; from null = no color in the model, to null = color removed)" if colors else "")
                + ". It can be very large, so never read it whole: query only the fields you need with json_query, "
                f"e.g. file_path={sel_token}, query=\"selected[].id\" or \"selected[?kind=='face'].{{id: id, normal: normal, selectors: selectors}}\"."
            )
    if colors:
        result["color_note"] = (
            "The user changed colors in the viewer. The original model file is not changed; the user can export a model "
            "with the colors applied with the viewer's Save button."
        )
    if local_axis is not None:
        result["local_axis"] = _local_axis_summary(local_axis)
    result["model"] = _model_summary(data)
    if data.get("diff"):
        result["diff"] = _diff_summary(data["diff"])
    if comparison:
        result["comparison"] = comparison
    result["viewer_data"] = data_token
    result["viewer_data_note"] = "Pass viewer_data as file_path to show the same model again without rebuilding."
    result["id_note"] = _id_note(data)
    if notes:
        result["notes"] = notes
    return json.dumps(result, ensure_ascii=False)
