"""analyze_image ツール。"""

from __future__ import annotations

from langchain_core.tools import tool
from pathlib import Path
import json
import logging

from ..images import image_followup_message
from ..images import image_ref
from ..images import is_image_file
from ..images import to_data_url
from .. import skill_drafts

from . import _state
from ._duplicate_guard import _record_and_check_duplicate
from ._path_memory_helpers import (
    _PATH_MEMORY_TOKEN_RE,
    _register_path_memory,
    _resolve_path_memory_token,
    suggest_from_path_memory,
)
from ._state import _duplicate_guard_session_key
from ._workdir import _foreign_tmp_dir_error
from ._workdir import _resolve_workdir

logger = logging.getLogger(__name__)


def _resolve_analyze_image_path(raw: str) -> Path:
    """analyze_image 専用のパス解決。読み込み系のためパスの制限は行わない。

    相対パスは従来通り skills ルート基準で解決する（SKILL.md の
    references/assets からの参照や既存の呼び出し規約との後方互換のため）。
    skills ルート配下に見つからない場合は、作業ディレクトリ基準でも
    解決を試みる（廃止した show_image ツールが相対パスを作業ディレクトリ
    基準で解決していたための後方互換）。絶対パスはそのまま解決する
    （Read/Glob/Grep と同じ方針で、ローカルファイルシステム上の任意パスを
    読めることを優先する）。_state._SKILLS_ROOTS が複数ある場合は _safe_path()
    と同じく前方から順に実在確認し、最初に見つかった候補を返す。

    Args:
        raw: analyze_image に渡された relative_path（相対パスまたは絶対パス）。

    Returns:
        解決済みの絶対パス（Path）。

    Raises:
        RuntimeError: init_tools() が未実行の場合。
    """
    if not _state._SKILLS_ROOTS:
        raise RuntimeError("init_tools() has not been run")
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    for root in _state._SKILLS_ROOTS:
        candidate = (root / p).resolve()
        if candidate.exists():
            return candidate
    workdir_candidate = (_resolve_workdir() / p).resolve()
    if workdir_candidate.exists():
        return workdir_candidate
    return (_state._SKILLS_ROOTS[0] / p).resolve()

@tool(response_format="content_and_artifact")
def analyze_image(relative_path: str, show_in_chat: bool = False) -> tuple[str, dict | None]:
    """Show an image file to the LLM as visual information so that you (the LLM) can analyze, describe or judge its content.
    Use it to analyze images referenced by SKILL.md, generated artifacts (run_script output, etc.) and images
    provided by the user. As a read tool it accepts absolute paths (only other sessions'
    `_tmp_<created time>_<short ID>` folders are an exception and cannot be analyzed).

    With `show_in_chat=True`, the image is also shown in the chat at the same time as you (the LLM)
    understand its content (use this when the user wants to see the image itself, e.g. "show me" or
    "display it"). **When showing an image, you always understand its content too**, so it cannot be
    called to "only show it without looking". The default `show_in_chat=False` only analyzes without
    showing (use this when checking many images mechanically, so the chat does not get cluttered).
    The return value is only a confirmation text; the image itself becomes visible to the LLM on the
    next model call.

    Args:
        relative_path: A relative path is resolved relative to the skills root
            (e.g. excel-vba-read/references/example.png). To look at an image anywhere else,
            specify an absolute path (e.g.
            C:\\Users\\foo\\data\\2019\\img1.png).
            You can also pass a path memory reference (`@N` form) attached to
            Glob/Grep/Read results as is.
        show_in_chat: If True, the image is also shown in the chat
            (the default False only analyzes for the LLM without showing it).

    Returns:
        A tuple (confirmation text, artifact). artifact is a dict with
        {"image_url": "data:<mime>;base64,<...>"} only if the image could be loaded, otherwise None.
        With `show_in_chat=True`, the confirmation text is a JSON string of the form
        `{"output_path": "..."}`, which triggers automatic display in the chat UI (cl.Image attachment).
        If the file does not exist, the extension is not supported
        (png/jpg/jpeg/gif/webp/bmp), or `@N` is not registered,
        it returns an "Error: ..." text and None instead of raising.
    """
    resolved_path, error = _resolve_path_memory_token(relative_path)
    if error:
        return f"Error: {error}", None
    path = _resolve_analyze_image_path(resolved_path)
    tmp_error = _foreign_tmp_dir_error(path) or skill_drafts.access_error(
        path, "read", _state._LLM_CONFIG, skill_drafts.current_draft_user()
    )
    if tmp_error:
        return tmp_error, None
    if not path.is_file():
        hint = suggest_from_path_memory(str(path)) if not path.exists() else ""
        return f"Error: file not found: {relative_path}{hint}", None
    if not is_image_file(path):
        return (
            f"Error: unsupported image format (png/jpg/jpeg/gif/webp/bmp only): {relative_path}",
            None,
        )
    # 同一画像の重複解析を検知する（tune-prompt調査で同じ画像を14回重複して
    # 呼ぶ実例あり。画像artifactはトークン消費が大きく、繰り返し会話へ積むと
    # コンテキストを大きく圧迫するため、2回目以降はartifactを積まない）。
    # 解決済みの絶対パスをキーにするため、`@N`や相対/絶対パスなど表記が違っても
    # 同一ファイルなら重複として検知できる。ただし show_in_chat=True による
    # チャットUIへの表示要求はartifact再生成とは独立した操作のため、重複判定
    # 済みでも表示自体は必ず行う（表示だけ拒否すると、既に一度analyze_imageで
    # 内容を確認した画像を後から「表示して」と頼まれた際に何も表示されなくなる）。
    is_duplicate = _record_and_check_duplicate(
        _duplicate_guard_session_key("analyze_image_call_signatures"), str(path)
    )
    if is_duplicate and not show_in_chat:
        return (
            f"Error: this image has already been checked once: {relative_path}. "
            "Showing the same image again was skipped. Refer to the previous description in the conversation history, "
            "or move on to other images or approaches.",
            None,
        )
    logger.info("analyze_image: %s show_in_chat=%s duplicate=%s", relative_path, show_in_chat, is_duplicate)
    if is_duplicate:
        # 表示のみ行い、トークン消費の大きいartifact（画像データURL）は
        # 再生成しない。
        return json.dumps({"output_path": str(path)}, ensure_ascii=False), None
    # 4032x3024 のような高解像度写真をそのまま渡すと数枚でトークン上限に達するため、
    # config.ini [images] の設定に従って縮小してから渡す（既定は縮小なし）。
    cfg = _state._LLM_CONFIG
    # 要約等で画像をテキスト化する際の参照（images.IMAGE_REFS_KEY）。`@N` が
    # 無ければパスメモリーへ登録して得る（登録済みなら同じ `@N` が返る）。
    # `@N/子パス` はフォルダの `@N` なので、画像ファイル自体を登録した `@N` を使う。
    token_match = _PATH_MEMORY_TOKEN_RE.match(relative_path)
    token = relative_path if token_match and not token_match.group(2) else None
    if token is None:
        token = next(iter(_register_path_memory([str(path)], description="analyze_image")), None)
    artifact = {
        "image_url": to_data_url(
            path,
            max_long_side=cfg.image_max_long_side_pixels if cfg else 0,
            jpeg_quality=cfg.image_jpeg_quality if cfg else 85,
        ),
        "ref": image_ref(path, token),
    }
    if show_in_chat:
        # このJSON形式は src/files.py の extract_generated_files() が汎用的に
        # 検知する「生成ファイル」規約に乗せるためのもの。app.py の on_tool_end
        # ハンドラはメインエージェント由来かサブエージェント（dispatch_agent）
        # 内部由来かを区別せずこの規約を処理するため、サブエージェントから
        # 呼んだ場合もその場でチャットに画像が表示される。
        return json.dumps({"output_path": str(path)}, ensure_ascii=False), artifact
    return f"Loaded the image: {relative_path}", artifact


def _with_image_followups(result: dict) -> dict:
    """ToolMessage.artifact に画像があれば、直後に画像付き HumanMessage を追加する。

    analyze_image が {"image_url": ...} という artifact 付きの ToolMessage を
    返した場合にのみ発火する。それ以外のツール結果には触れない。

    Args:
        result: ToolNode.invoke/ainvoke の戻り値（{"messages": [ToolMessage, ...]}）。

    Returns:
        画像artifactがあれば末尾に HumanMessage を追加した新しい dict、
        無ければ result をそのまま返す。
    """
    messages = result.get("messages", [])
    extra = [followup for msg in messages if (followup := image_followup_message(getattr(msg, "artifact", None))) is not None]
    if not extra:
        return result
    return {**result, "messages": [*messages, *extra]}
