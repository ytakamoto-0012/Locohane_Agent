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
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import chainlit as cl
from langchain_core.tools import tool

from ..ask_relay import ask_turn
from ..cad.formats import format_of, supported_extensions_text
from ..cad.mesh_data import FORMAT as VIEWER_FORMAT
from ..cad.mesh_data import apply_view_edits, write_viewer_json
from ..i18n import t
from . import _state
from ._ask_echo import ASK_TIMEOUT_MESSAGE, _echo_ask_answer
from ._ask_relay_helper import _ask_with_cross_session_relay
from ._path_memory_helpers import _register_path_memory, _resolve_path_memory_token
from ._proc_tree import run_with_timeout
from ._python_fs_guard import _exec_guard_roots, _python_fs_guard_preamble, _register_exec_output_files
from ._state import _resolve_ask_timeout
from ._subprocess_env import _subprocess_env
from ._workdir import _resolve_exec_workdir, _resolve_workdir, _tmp_dir_parents

logger = logging.getLogger(__name__)

SelectKind = Literal["face", "edge", "vertex", "part"]
_ALL_KINDS: tuple[str, ...] = ("face", "edge", "vertex", "part")
# 選んだ要素の種類 → ビューア用データでの一覧のキー。
_LIST_KEYS = {"face": "faces", "edge": "edgeList", "vertex": "vertexList", "part": "parts"}

_CANCEL_MESSAGE = "Error: the user closed the 3D viewer without confirming a selection."
_RUN_CAD = Path(__file__).resolve().parent.parent / "cad" / "run_cad.py"
# エラー時に LLM へ返す、サブプロセスの出力の末尾の長さ。
_OUTPUT_TAIL_CHARS = 3000
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
# ビューアの指示欄から受け取る文字数の上限（超えた分は切る）。
_MESSAGE_MAX_CHARS = 4000


def _setting(name: str, default: Any) -> Any:
    return getattr(_state._LLM_CONFIG, name, default) if _state._LLM_CONFIG is not None else default


def _options() -> dict[str, Any]:
    return {
        "linear_deflection_ratio": _setting("cad_viewer_linear_deflection_ratio", 0.001),
        "angular_deflection_deg": _setting("cad_viewer_angular_deflection_deg", 20.0),
        "max_triangles": _setting("cad_viewer_max_triangles", 500_000),
        "stl_region_angle_deg": _setting("cad_viewer_stl_region_angle_deg", 1.0),
    }


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _OUTPUT_TAIL_CHARS else "...\n" + text[-_OUTPUT_TAIL_CHARS:]


@tool
async def show_cad_viewer(
    question: str,
    file_path: str = "",
    code: str = "",
    select: list[SelectKind] | None = None,
) -> str:
    """Show a 3D model in an interactive viewer in the chat, and return the elements the user selects.

    The user can rotate, zoom and pan the model, select faces / edges / vertices (or whole parts of an
    assembly) by clicking, box selection or "select all", hide parts / layers, change face and part
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
      regions of triangles and edges are their borders, with no vertices),
      .json (viewer data this tool returned before; shows the same model again without rebuilding).
      Reading a file needs no plan approval.

    code: CadQuery Python code. Put the shape in a variable named `result`, or call
      `show_object(shape)` (several calls are shown together). Workplane, Shape, Sketch, Assembly
      and Vector (a point) are accepted. Like execute_python_code, it runs only after the plan has
      been approved with create_plan/approve_plan, its cwd is this session's temporary folder, and
      writes are limited to the working directory. Files it writes (e.g. an exported .step) are
      registered as @N.

    Element IDs are 0-based in CadQuery order: face i is
    `cq.Workplane().add(shape).faces().vals()[i]` (same for edges() / vertices()) for the same shape.
    For STL, IDs are mesh regions and have no CadQuery equivalent.

    Args:
        question: What you want the user to select, shown above the viewer
            (e.g. "Select the face to put the hole in").
        file_path: The CAD file to show (see above). Leave empty when using code.
        code: CadQuery code that makes the shape (see above). Leave empty when using file_path.
        select: Which kinds of elements the user may select: any of "face", "edge", "vertex",
            "part". Default: all kinds the model has ("part" only for an assembly with 2 or more
            parts, "vertex" not for STL).

    Returns:
        A short JSON summary whose size does not depend on how many elements were selected:
        selected_count, selected_by_kind, selection_file (@N of a JSON with every selected element
        and its geometry under "selected", and the color changes under "color_changes"; it can be
        very large, so read only the fields you need with json_query), model (format, units, size,
        element counts), and viewer_data (@N of the viewer data). If the user changed face / part
        colors in the viewer, also color_change_count (the model file itself is not changed).
        If the user typed an instruction in the viewer, it comes first as user_instruction: treat it
        as the user's next request about the selected elements.
        It returns an "Error: ..." string (never raises) if the input is wrong, the code or file
        fails, the plan is not approved for code, the user closes the viewer
        ("Error: the user closed the 3D viewer ..."), or there is no answer within the timeout.
    """
    if not _setting("cad_viewer_enabled", True):
        return "Error: the 3D viewer is disabled in config.ini ([cad_viewer] enabled=false)."
    file_path, code = (file_path or "").strip(), code or ""
    if bool(file_path) == bool(code.strip()):
        return "Error: give exactly one of file_path (a CAD file) or code (CadQuery code)."
    bad = [k for k in (select or []) if k not in _ALL_KINDS]
    if bad:
        return f"Error: unknown kinds in select: {bad}. Use any of {list(_ALL_KINDS)}."

    if code.strip():
        made = await _viewer_data_from_code(code)
    else:
        made = await _viewer_data_from_file(file_path)
    if isinstance(made, str):
        return made
    data, data_path, notes = made

    kinds = _available_kinds(data, select)
    if not kinds:
        return f"Error: the model has none of the requested kinds {select} to select."
    data_token = _register_file(data_path, "3D viewer data (show_cad_viewer)")
    save_id = uuid.uuid4().hex
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    _SAVE_TARGETS[save_id] = _SaveTarget(data=data, out_dir=_resolve_workdir(need_write=True), thread_id=thread_id)
    try:
        answer = await _ask_selection(question, data, kinds, save_id)
    finally:
        target = _SAVE_TARGETS.pop(save_id, None)
    if target is not None and target.saved is not None:
        notes = [*notes, f"The user saved the edited view (colors and hidden parts applied) to {target.saved_token}. Pass it as file_path to show it again."]
    if isinstance(answer, str):
        return answer + (f"\n{notes[-1]}" if target is not None and target.saved is not None else "")
    picked, colors, message = answer
    return _format_result(data, picked, data_token, notes, colors, message)


@dataclass
class _SaveTarget:
    """ビューアの「保存」ボタンの保存先（ビューアを出している間だけ _SAVE_TARGETS に置く）。"""

    data: dict
    out_dir: Path
    thread_id: str
    saved: Path | None = None
    saved_token: str = ""


# ビューアに渡した save_id → 保存先。ブラウザから来る save_id はこれに無ければ受け付けない。
_SAVE_TARGETS: dict[str, _SaveTarget] = {}


def _saved_path(target: _SaveTarget) -> Path:
    src = target.data.get("source") or {}
    stem = Path(src["path"]).stem if src.get("path") else "cadquery"
    return target.out_dir / f"{stem}_edited_{time.strftime('%Y%m%d_%H%M%S')}.json"


async def save_viewer_edits(payload: dict[str, Any]) -> dict[str, Any]:
    """ビューアの「保存」ボタン（app.py の action_callback "cad_viewer_save"）。

    変えた色・隠した部品・種類ごとの表示を適用したビューア用データを JSON に書き、@N に登録する。
    同じビューアで何度保存しても同じファイルを上書きする。ビューアは閉じない。

    Returns:
        {"ok": True, "path", "token", "color_changes"} か {"ok": False, "error"}。
    """
    save_id = str(payload.get("save_id") or "")
    target = _SAVE_TARGETS.get(save_id)
    if target is None:
        return {"ok": False, "error": "This viewer is no longer waiting for an answer."}
    if (cl.user_session.get("thread_id") or "_no_session") != target.thread_id:
        return {"ok": False, "error": "This viewer belongs to another conversation."}
    data = target.data
    colors = _valid_colors(data, payload.get("colors"))
    parts = data.get("parts") or []
    hidden = payload.get("hidden_parts")
    hidden_parts = [p for p in (hidden if isinstance(hidden, list) else []) if isinstance(p, int) and not isinstance(p, bool) and 0 <= p < len(parts)]
    vis = payload.get("visible")
    visible = {k: bool(vis[k]) for k in ("face", "edge", "vertex") if isinstance(vis, dict) and isinstance(vis.get(k), bool)}
    edited = apply_view_edits(
        data,
        face_colors={c["id"]: c["to"] for c in colors if c["kind"] == "face"},
        part_colors={c["id"]: c["to"] for c in colors if c["kind"] == "part"},
        hidden_parts=hidden_parts,
        visible=visible,
        color_changes=colors,
        saved_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    out = target.saved or _saved_path(target)
    try:
        await asyncio.to_thread(_write_atomic, edited, out)
    except OSError as e:
        logger.warning("show_cad_viewer: could not save the edited view to %s", out, exc_info=True)
        return {"ok": False, "error": str(e)}
    if target.saved is None:
        target.saved = out
        target.saved_token = next(
            iter(_register_path_memory([str(out)], description="3D viewer data with edits (show_cad_viewer)", thread_id=target.thread_id)),
            str(out),
        )
    logger.info("show_cad_viewer: saved the edited view (%d color changes) to %s", len(colors), out)
    return {"ok": True, "path": str(out), "token": target.saved_token, "color_changes": len(colors)}


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


async def _viewer_data_from_file(file_path: str) -> tuple[dict, Path, list[str]] | str:
    resolved, err = _resolve_path_memory_token(file_path)
    if err:
        return f"Error: {err}"
    path = Path(resolved)
    if not path.is_absolute():
        path = _resolve_exec_workdir() / path
    if not path.is_file():
        return f"Error: the file does not exist: {path}"
    fmt = format_of(path)
    if fmt is None:
        return f"Error: unsupported file type {path.suffix!r}. Supported: {supported_extensions_text()}"
    if fmt == "VIEWER_JSON":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return f"Error: could not read the viewer data {path}: {e}"
        if not isinstance(data, dict) or data.get("format") != VIEWER_FORMAT:
            return f"Error: {path} is not viewer data made by show_cad_viewer. Pass a CAD file ({supported_extensions_text()})."
        return data, path, []

    workdir = _resolve_exec_workdir()
    out = _new_data_path(workdir)
    cmd = [_state._SCRIPT_PYTHON, str(_RUN_CAD), "--file", str(path), "--out", str(out), "--options", json.dumps(_options())]
    failed = await _run(cmd, workdir)
    if failed:
        return failed
    return _load_data(out)


async def _viewer_data_from_code(code: str) -> tuple[dict, Path, list[str]] | str:
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
            + CODE_EPILOGUE.format(out_path=str(out), options=_options())
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
    if out.exists():
        before[out] = out.stat().st_mtime
    note = _register_exec_output_files(workdir, before, cl.user_session.get("thread_id") or "_no_session")
    if failed:
        return failed + (f"\n{note}" if note else "")
    made = _load_data(out)
    if isinstance(made, str):
        return made
    data, data_path, notes = made
    if note:
        notes.append(note)
    return data, data_path, notes


def _new_data_path(workdir: Path, prefix: str = "cad_view") -> Path:
    return workdir / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.json"


async def _run(cmd: list[str], workdir: Path) -> str | None:
    """サブプロセスを動かす。失敗したら LLM へ返すエラー文、成功したら None。"""
    env = _subprocess_env()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    logger.info("show_cad_viewer: running %s", cmd[1])
    try:
        proc = await asyncio.to_thread(run_with_timeout, cmd, cwd=str(workdir), env=env, timeout=_state._SCRIPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"Error: making the 3D model timed out after {_state._SCRIPT_TIMEOUT} seconds."
    except OSError as e:
        return f"Error: could not start making the 3D model: {e}"
    if proc.returncode == 0:
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
    }
    return [k for k in _ALL_KINDS if k in wanted and has[k]]


def _valid_selection(data: dict, kinds: list[str], selected: Any) -> list[tuple[str, int]]:
    """ブラウザから来た選択のうち、正しいものだけを (種類, ID) で返す（重複は除く）。"""
    out: list[tuple[str, int]] = []
    for s in selected if isinstance(selected, list) else []:
        if not isinstance(s, dict):
            continue
        kind, eid = s.get("kind"), s.get("id")
        if kind not in kinds or not isinstance(eid, int) or isinstance(eid, bool):
            continue
        if 0 <= eid < len(data.get(_LIST_KEYS[kind]) or []) and (kind, eid) not in out:
            out.append((kind, eid))
    return out


def _valid_colors(data: dict, colors: Any) -> list[dict[str, Any]]:
    """ブラウザから来た色の変更（{"faces": {ID: "#rrggbb"}, "parts": {...}}）のうち、正しいものを
    [{"kind", "id", "from", "to"}] で返す。from は元の色（無ければ None）。"""
    out: list[dict[str, Any]] = []
    if not isinstance(colors, dict):
        return out
    for key, kind in (("faces", "face"), ("parts", "part")):
        items = data.get(_LIST_KEYS[kind]) or []
        changes = colors.get(key)
        for sid, hex_color in (changes.items() if isinstance(changes, dict) else []):
            try:
                eid = int(sid)
            except (TypeError, ValueError):
                continue
            if not (0 <= eid < len(items)) or not isinstance(hex_color, str) or not _HEX_COLOR.fullmatch(hex_color):
                continue
            before = items[eid].get("color")
            if (before or "").lower() == hex_color.lower():
                continue
            out.append({"kind": kind, "id": eid, "from": before, "to": hex_color.lower()})
    out.sort(key=lambda c: (c["kind"], c["id"]))
    return out


def _valid_message(message: Any) -> str:
    """ビューアの指示欄の文字列（無ければ空、長すぎれば切る）。"""
    return message.strip()[:_MESSAGE_MAX_CHARS] if isinstance(message, str) else ""


def _echo_text(data: dict, picked: list[tuple[str, int]], colors: list[dict[str, Any]], message: str = "") -> str:
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
    if message:
        text += f"\n\n{message}"
    return text


async def _ask_selection(
    question: str, data: dict, kinds: list[str], save_id: str = ""
) -> tuple[list[tuple[str, int]], list[dict[str, Any]], str] | str:
    timeout = _resolve_ask_timeout(_setting("show_cad_viewer_timeout_seconds", 0))
    thread_id = cl.user_session.get("thread_id") or "_no_session"

    async def _factory():
        # CustomElement はセッション文脈に依存するため、中継時に作り直せるよう毎回ここで作る。
        element = cl.CustomElement(name="CadViewer", props={"question": question, "select": kinds, "data": data, "save_id": save_id})
        ask_message = cl.AskElementMessage(content=question, element=element, timeout=timeout)
        res = await ask_message.send()
        # Chainlit は要素（props にビューア用データ全体、数 MB になりうる）を会話の保存にも書く。ビューアは
        # 回答待ちの間しか描かれず、後から見返す用途も無いので、答え（時間切れを含む）を受け取ったら消す。
        try:
            await element.remove()
        except Exception:  # noqa: BLE001 - 後片付けの失敗でツールの結果を失わせない
            logger.warning("Could not remove the 3D viewer element", exc_info=True)
        if res is not None:
            if res.get("submitted", True):
                answer = _echo_text(
                    data,
                    _valid_selection(data, kinds, res.get("selected")),
                    _valid_colors(data, res.get("colors")),
                    _valid_message(res.get("message")),
                )
            else:
                answer = t("ask.cancel")
            await _echo_ask_answer(ask_message, question, answer)
        return res

    async with ask_turn(thread_id):
        res = await _ask_with_cross_session_relay(thread_id, _factory, timeout)
    if res is None:
        return ASK_TIMEOUT_MESSAGE
    if not res.get("submitted", True):
        return _CANCEL_MESSAGE
    return (
        _valid_selection(data, kinds, res.get("selected")),
        _valid_colors(data, res.get("colors")),
        _valid_message(res.get("message")),
    )


def _model_summary(data: dict) -> dict[str, Any]:
    src = data.get("source") or {}
    stats = data.get("stats") or {}
    return {
        "format": src.get("format", "?"),
        "source": src.get("path") or "your CadQuery code",
        "units": data.get("units", "unknown"),
        "size": (data.get("bbox") or {}).get("size", []),
        "counts": {k: v for k, v in stats.items() if k != "triangles"},
    }


def _id_note(data: dict) -> str:
    if (data.get("source") or {}).get("format") == "STL":
        return "IDs are planar regions of the STL mesh (no CadQuery equivalent); 'meshHints' only tell which region is outermost on an axis."
    return (
        "IDs are 0-based in CadQuery order (face i = cq.Workplane().add(shape).faces().vals()[i]; same for edges/vertices). "
        "'selectors' are CadQuery selectors verified to pick exactly that one element."
    )



def _write_selection(data: dict, picked: list[tuple[str, int]], data_token: str, colors: list[dict[str, Any]]) -> Path:
    """選んだ要素の詳しい情報（ジオメトリ全部）と色の変更を、そのセッションの `_tmp_` フォルダの JSON に書く。"""
    path = _new_data_path(_resolve_exec_workdir(), prefix="cad_selection")
    body = {
        "model": _model_summary(data),
        "viewer_data": data_token,
        "id_note": _id_note(data),
        "selected": [{"kind": kind, "id": eid, **data[_LIST_KEYS[kind]][eid]} for kind, eid in picked],
        "color_changes": colors,
    }
    path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _format_result(
    data: dict,
    picked: list[tuple[str, int]],
    data_token: str,
    notes: list[str],
    colors: list[dict[str, Any]] | None = None,
    message: str = "",
) -> str:
    """LLM へ返す概要（JSON 文字列）。

    大きな CAD データでは面が数千・三角形が数十万になり、全選択もできるので、選んだ要素の一覧
    （ID だけでも）や色の変更の一覧は戻り値に入れない。すべて選択結果のファイルに書き、戻り値は
    件数とファイルの @N だけにする（要素数によらず大きさが一定）。
    """
    colors = colors or []
    result: dict[str, Any] = {"status": "confirmed"}
    if message:
        # 利用者がビューアの指示欄に書いた指示。選択（や色の変更）はこの指示の対象。
        result["user_instruction"] = message
        result["user_instruction_note"] = (
            "The user wrote this instruction in the 3D viewer together with the selection below. "
            "Treat it as the user's next request about the selected elements."
        )
    result |= {
        "selected_count": len(picked),
        "selected_by_kind": {k: n for k in _ALL_KINDS if (n := sum(1 for kind, _ in picked if kind == k))},
    }
    if colors:
        result["color_change_count"] = len(colors)
    if picked or colors:
        try:
            sel_token = _register_file(_write_selection(data, picked, data_token, colors), "3D viewer selection (show_cad_viewer)")
        except OSError as e:
            logger.warning("show_cad_viewer: could not write the selection file", exc_info=True)
            result["selection_file_error"] = f"could not write the selection details: {e}. Ask the user to select again."
        else:
            result["selection_file"] = sel_token
            result["how_to_read"] = (
                f"{sel_token} has one entry per selected element under 'selected' (kind, id, geomType, area/length, center, normal, "
                f"radius, bbox, neighbouring faces/edges, selectors)"
                + (", and the color changes under 'color_changes' ({kind, id, from, to} as #rrggbb; from null = no color in the model)" if colors else "")
                + ". It can be very large, so never read it whole: query only the fields you need with json_query, "
                f"e.g. file_path={sel_token}, query=\"selected[].id\" or \"selected[?kind=='face'].{{id: id, normal: normal, selectors: selectors}}\"."
            )
    if colors:
        result["color_note"] = (
            "The user changed colors in the viewer. The viewer does not change the model file; apply them yourself if the user wants the colors saved."
        )
    result["model"] = _model_summary(data)
    result["viewer_data"] = data_token
    result["viewer_data_note"] = "Pass viewer_data as file_path to show the same model again without rebuilding."
    result["id_note"] = _id_note(data)
    if notes:
        result["notes"] = notes
    return json.dumps(result, ensure_ascii=False)
