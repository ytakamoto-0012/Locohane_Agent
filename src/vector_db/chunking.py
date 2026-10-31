"""テキストをチャンク（埋め込みの単位）に分割する。"""

from __future__ import annotations

import re

_BLANK_LINES_RE = re.compile(r"\n\s*\n")


def split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """段落（空行）→ 行 → 文字数の順に切れ目を探し、chunk_size 文字以内のチャンクに分ける。

    2つ目以降のチャンクの先頭には、直前のチャンクの末尾 overlap 文字を重ねる
    （切れ目で文脈が途切れて検索に引っかからなくなるのを防ぐ）。

    Args:
        text: 分割する本文。
        chunk_size: 1チャンクの最大文字数（overlap 分を含む）。
        overlap: 隣り合うチャンクで重ねる文字数（0 以上 chunk_size 未満）。

    Returns:
        空白だけのチャンクを除いたチャンクのリスト。
    """
    if not (0 <= overlap < chunk_size):
        raise ValueError(f"overlap must be at least 0 and less than chunk_size: chunk_size={chunk_size}, overlap={overlap}")
    body_size = chunk_size - overlap
    pieces: list[str] = []
    for paragraph in _BLANK_LINES_RE.split(text.replace("\r\n", "\n")):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= body_size:
            pieces.append(paragraph)
            continue
        for line in paragraph.split("\n"):
            line = line.strip()
            for start in range(0, len(line), body_size):
                pieces.append(line[start : start + body_size])

    bodies: list[str] = []
    current = ""
    for piece in pieces:
        joined = f"{current}\n{piece}" if current else piece
        if len(joined) <= body_size:
            current = joined
        else:
            if current:
                bodies.append(current)
            current = piece
    if current:
        bodies.append(current)

    chunks: list[str] = []
    for i, body in enumerate(bodies):
        if i > 0 and overlap > 0:
            body = bodies[i - 1][-overlap:] + "\n" + body
            body = body[-chunk_size:] if len(body) > chunk_size else body
        if body.strip():
            chunks.append(body)
    return chunks
