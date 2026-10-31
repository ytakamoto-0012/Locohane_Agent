"""ダッシュボードの「APIリファレンス」メニューに表示する Markdown の読み込み・HTML化。

原本は admin/API_REFERENCE.md（英語表示では英訳の admin/API_REFERENCE.en.md）。リクエストのたびにファイルを読み直すため、
md を編集すれば管理ツールを再起動しなくても次の表示で反映される。
"""

from __future__ import annotations

from pathlib import Path

from markdown_it import MarkdownIt

API_REFERENCE_PATH = Path(__file__).resolve().parent / "API_REFERENCE.md"
API_REFERENCE_EN_PATH = API_REFERENCE_PATH.with_name("API_REFERENCE.en.md")


def reference_path(lang: str) -> Path:
    """表示言語の APIリファレンス（英訳が無ければ日本語の原本）。"""
    if lang == "en" and API_REFERENCE_EN_PATH.is_file():
        return API_REFERENCE_EN_PATH
    return API_REFERENCE_PATH


# html=False: md 内の生HTMLはエスケープして表示する（innerHTML へ流し込むため）。
_md = MarkdownIt("commonmark", {"html": False}).enable("table")


def render(path: Path = API_REFERENCE_PATH) -> dict[str, str]:
    """md を読み、{"markdown": 原文, "html": 変換後HTML} を返す。

    Raises:
        FileNotFoundError: md が存在しない。
    """
    text = path.read_text(encoding="utf-8")
    return {"markdown": text, "html": _md.render(text)}
