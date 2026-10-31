"""pptx系スキル（pptx-create/pptx-edit/pptx-inspect/pptx-read/pptx-render）が
共有するヘルパー関数。

実体はこのファイルのみ（`office_shared/`配下、SKILL.mdを持たない非スキル
ディレクトリ）。各スキルのscripts/配下のスクリプトはsys.path経由でこの
ファイルを直接importする（1-B 相互import方式、詳細はOFFICE_SKILLS_README.md
参照）。

run_script からは直接実行されない。
"""

from __future__ import annotations

import hashlib
import json
import locale
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from pptx.enum.dml import MSO_FILL_TYPE
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.enum.text import PP_ALIGN

_ALIGN_NAMES = {
    PP_ALIGN.LEFT: "left",
    PP_ALIGN.CENTER: "center",
    PP_ALIGN.RIGHT: "right",
    PP_ALIGN.JUSTIFY: "justify",
}


def setup_utf8_stdio() -> None:
    """標準出力/標準エラーをUTF-8に固定する。

    Windows環境ではパイプ経由のPython子プロセスの既定エンコーディングが
    システムのANSIコードページ（例: cp932）になり、run_script側の
    encoding="utf-8" デコードと食い違って日本語が文字化けするため、
    各スクリプトの先頭で必ず呼ぶこと。
    """
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def read_text_with_fallback(path: Path) -> str:
    """UTF-8 で読めなければ OS の ANSI コードページ（日本語版 Windows なら cp932、英語版なら cp1252 等）、
    cp932 の順に読む（本体の src/tools/_file_tools_common.py と同じ順）。

    cp1252 の文書の多くは cp932 としてもエラーなく読めて文字化けに気付けないため、ANSI を先に試す。
    """
    data = path.read_bytes()
    error: UnicodeDecodeError | None = None
    for encoding in dict.fromkeys(("utf-8", locale.getencoding(), "cp932")):
        try:
            return data.decode(encoding).replace("\r\n", "\n")
        except UnicodeDecodeError as e:
            error = error or e
        except LookupError:
            continue
    raise error or UnicodeDecodeError("utf-8", data, 0, len(data), "undecodable")


def backup_before_overwrite(path: Path) -> Path | None:
    """path が既に存在する場合、上書き直前に同じフォルダへタイムスタンプ付きで
    コピーしてバックアップを作成する。存在しなければ何もせず None を返す
    （新規作成時はバックアップ不要）。
    """
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_name(f"{path.stem}.bak_{timestamp}{path.suffix}")
    suffix_n = 2
    while backup_path.exists():
        backup_path = path.with_name(f"{path.stem}.bak_{timestamp}_{suffix_n}{path.suffix}")
        suffix_n += 1
    shutil.copy2(path, backup_path)
    return backup_path


def _length_cm(value) -> float | None:
    """python-pptxのLength（EMU）をcm単位のfloatへ変換する（None時はNoneのまま）。"""
    return round(value.cm, 2) if value is not None else None


def check_shape_overflow(shape_info: dict, slide_width_cm: float | None, slide_height_cm: float | None) -> str | None:
    """shapeがスライド境界をはみ出しているかをチェックする。

    はみ出していれば説明文を返す。はみ出していなければNoneを返す。
    微小許容誤差（0.05cm）を加味する。
    """
    if slide_width_cm is None or slide_height_cm is None:
        return None

    left_cm = shape_info.get("left_cm")
    top_cm = shape_info.get("top_cm")
    width_cm = shape_info.get("width_cm")
    height_cm = shape_info.get("height_cm")

    if left_cm is None or top_cm is None or width_cm is None or height_cm is None:
        return None

    tolerance = 0.05
    issues = []

    if left_cm < -tolerance:
        issues.append(f"sticks out {abs(left_cm):.2f}cm past the left edge")
    if top_cm < -tolerance:
        issues.append(f"sticks out {abs(top_cm):.2f}cm past the top edge")
    if left_cm + width_cm > slide_width_cm + tolerance:
        overshoot = left_cm + width_cm - slide_width_cm
        issues.append(f"sticks out {overshoot:.2f}cm past the right edge")
    if top_cm + height_cm > slide_height_cm + tolerance:
        overshoot = top_cm + height_cm - slide_height_cm
        issues.append(f"sticks out {overshoot:.2f}cm past the bottom edge")

    if not issues:
        return None

    shape_name = shape_info.get("name", "unknown")
    shape_idx = shape_info.get("shape_index", "?")
    return f"shape_index {shape_idx}('{shape_name}') {', '.join(issues)}"


def _solid_fill_color(fill) -> str | None:
    """shape.fill/cell.fillが単色塗りならRRGGBB文字列を返す（テーマ色・塗りなし等はNone）。"""
    try:
        if fill.type != MSO_FILL_TYPE.SOLID:
            return None
        return str(fill.fore_color.rgb)
    except Exception:
        return None


def _line_color(line) -> str | None:
    """shape.lineがRGB直接指定の枠線色を持つならRRGGBB文字列を返す（テーマ色・枠線なし等はNone）。"""
    try:
        return str(line.color.rgb)
    except Exception:
        return None


def _first_run_text_style(text_frame) -> dict | None:
    """text_frameの最初の非空段落・最初のrunの文字書式を返す（set_shape_styleが
    書き込む値の代表的な読み返し用。run単位で書式が混在していても最初の1つ
    しか見えない点はedit_pptx.pyのop_set_shape_styleが段落全体へ一括適用する
    運用を前提にした簡易版）。runが無ければNone。
    """
    for paragraph in text_frame.paragraphs:
        if not paragraph.runs:
            continue
        run = paragraph.runs[0]
        font = run.font
        try:
            text_color = str(font.color.rgb)
        except Exception:
            text_color = None
        try:
            font_size_pt = font.size.pt if font.size is not None else None
        except Exception:
            font_size_pt = None
        return {
            "text_color": text_color,
            "bold": font.bold,
            "italic": font.italic,
            "underline": font.underline,
            "font_size_pt": font_size_pt,
            "font_name": font.name,
            "align": _ALIGN_NAMES.get(paragraph.alignment),
        }
    return None


def describe_shape(shape, index: int) -> dict:
    """1つのshapeの構造情報をdictにする。

    inspect_pptx.py（一覧表示）が使う。ここで定義する `shape_index` は
    `enumerate(slide.shapes)` の0始まり連番であり、edit_pptx.py の各操作が
    指定する `shape_index` と完全に一致する仕様として両スクリプトで揃える
    （edit_pptx.py自体はこの関数を呼ばないが、shape_indexの数え方はここに合わせる）。

    `left_cm`/`top_cm`/`width_cm`/`height_cm` は edit_pptx.py の
    `set_shape_position` が受け取る単位（cm）と完全に一致させている
    （pptx-inspectで読んだ値をそのままset_shape_positionの引数へ使い回せる
    ようにするため。プレースホルダ等でレイアウト側から位置を継承していて
    実座標が取得できない場合はNoneになる）。

    `fill_color`/`border_color`/`text_style`/`crop` は edit_pptx.py の
    `set_shape_style`/`crop_picture` が書き込める値のうち、単色塗り・RGB直接
    指定・最初のrunの書式・トリミング量として読み返せる範囲を返す（テーマ色
    指定やrunごとに異なる書式などは読み返せず、いずれもNoneになる）。
    """
    info: dict = {
        "shape_index": index,
        "name": shape.name,
        "shape_type": str(shape.shape_type) if shape.shape_type is not None else None,
        "is_placeholder": shape.is_placeholder,
        "placeholder_idx": None,
        "placeholder_type": None,
        "has_text_frame": shape.has_text_frame,
        "text_preview": None,
        "has_table": shape.has_table,
        "table_dims": None,
        "has_picture": shape.shape_type == MSO_SHAPE_TYPE.PICTURE,
        "left_cm": _length_cm(shape.left),
        "top_cm": _length_cm(shape.top),
        "width_cm": _length_cm(shape.width),
        "height_cm": _length_cm(shape.height),
        "fill_color": None,
        "border_color": None,
        "text_style": None,
        "crop": None,
    }
    if shape.is_placeholder:
        info["placeholder_idx"] = shape.placeholder_format.idx
        info["placeholder_type"] = str(shape.placeholder_format.type)
    if shape.has_text_frame:
        text = shape.text_frame.text
        info["text_preview"] = text[:50] if text else ""
        info["text_style"] = _first_run_text_style(shape.text_frame)
    if shape.has_table:
        table = shape.table
        info["table_dims"] = {"rows": len(table.rows), "cols": len(table.columns)}
    else:
        try:
            info["fill_color"] = _solid_fill_color(shape.fill)
        except Exception:
            pass
        try:
            info["border_color"] = _line_color(shape.line)
        except Exception:
            pass
    if info["has_picture"]:
        try:
            crop = {
                "crop_left": shape.crop_left,
                "crop_top": shape.crop_top,
                "crop_right": shape.crop_right,
                "crop_bottom": shape.crop_bottom,
            }
            if any(v not in (0.0, None) for v in crop.values()):
                info["crop"] = crop
        except Exception:
            pass
    return info


def register_output_path(path, description: str | None = None) -> dict[str, str] | None:
    """生成/更新したファイルをパスメモリーへ登録し、{"@N": 絶対パス} を返す。

    run_script が子プロセスへ注入する AGENT_SRC_DIR 経由で src/path_memory.py
    を import する。AGENT_SRC_DIR未設定やimport失敗時はNone（run_script以外
    から直接実行された場合でもスクリプト自体は失敗させないためのフォールバック）。
    """
    src_dir = os.environ.get("AGENT_SRC_DIR")
    if not src_dir:
        return None
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    try:
        import path_memory
    except ImportError:
        return None
    thread_id, pm_dir, max_entries = path_memory.env_params()
    abs_path = str(Path(path).resolve())
    idx = path_memory.register(thread_id, abs_path, pm_dir, max_entries, description=description)
    if idx is None:
        return None
    return {f"@{idx}": abs_path}


def write_json_result(result: dict, category: str, source_path) -> dict:
    """result全体をJSONファイルへ書き出し、要約に足す {"result_path", "path_memory"} を返す。

    保存先は execute_python_code の中間生成物と同じ `_tmp_<thread_id>/<category>/`
    規約（pdf-tools の render_pdf_pages.py 参照）。基準は run_script の cwd
    ではなく常に default_workdir（AGENT_DEFAULT_WORKDIR）。cwd はユーザー指定
    work_dir になりうり、その場合は自動削除対象外のため、cwd基準だと消えずに
    溜まり続ける。
    """
    thread_id = os.environ.get("AGENT_EXEC_TMP_NAME") or os.environ.get("AGENT_THREAD_ID") or "_no_session"
    base_dir = Path(os.environ.get("AGENT_DEFAULT_WORKDIR") or "./data/temp")
    out_dir = base_dir / f"_tmp_{thread_id}" / category
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(Path(source_path).resolve()).encode("utf-8")).hexdigest()[:8]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_path = out_dir / f"{digest}_{timestamp}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    info: dict = {"result_path": str(out_path)}
    pm = register_output_path(out_path, description=f"full JSON of the {category} read result")
    if pm:
        info["path_memory"] = pm
    return info


def summarize_result(result: dict, omit_keys: list[str]) -> dict:
    """result から omit_keys で指定したキーを除いた要約辞書を返す。

    除いたキーがlist型なら "<key>_count"、str型なら "<key>_length" を
    件数/文字数として残す（本文全体はファイル側にのみ残す）。
    """
    summary = dict(result)
    for key in omit_keys:
        if key not in summary:
            continue
        value = summary.pop(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, str):
            summary[f"{key}_length"] = len(value)
    return summary
