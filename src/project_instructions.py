"""プロジェクト固有指示ファイル（ClaudeCode の CLAUDE.md 相当）の読み込み。

.locohane/LOCOHANE.md が存在すれば、その内容をシステムプロンプトの
{{project_instructions}} へ差し込む。ファイルが無くてもエラーにはならず、
プレースホルダーテキストが差し込まれるだけ（CLAUDE.mdと同じ仕様）。

config.ini の project_locohane_dir で複数ディレクトリを指定できる（各ディレクトリ
配下の LOCOHANE.md をそのまま読み込んで連結するだけで、マージ・重複排除などは
行わない）。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

_MAX_LINES = 200


def render_project_instructions_block(paths: Sequence[Path], max_lines: int = _MAX_LINES) -> str:
    """system_prompt.md の {{project_instructions}} へ差し込むテキストを組み立てる。

    Args:
        paths: LOCOHANE.md（複数可）の絶対パスのリスト（config.project_instructions_paths）。
        max_lines: 1ファイルあたりに差し込む最大行数。

    Returns:
        差し込み用テキスト。存在するファイルが1つも無い/すべて空の場合は
        「（プロジェクト固有の指示はありません）」を返す。複数ファイルが
        存在する場合は、それぞれの内容を見出し付きで連結する。
    """
    blocks: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [
                "",
                f"... (truncated after {max_lines} lines. "
                f"Read {path} directly for the full text)",
            ]
        text = "\n".join(lines).strip()
        if not text:
            continue
        if len(paths) > 1:
            text = f"### {path}\n\n{text}"
        blocks.append(text)
    return "\n\n---\n\n".join(blocks) if blocks else "(no project-specific instructions)"
