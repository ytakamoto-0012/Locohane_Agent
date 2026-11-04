"""read_tool/glob_tool/grep_tool/json_query が共有する、ローカルファイルシステム
読込・検索の純粋ヘルパー。

src/memory.py と同じ契約: Chainlit・パスメモリー・作業ディレクトリ解決には
一切依存しない。呼び出し元（各ツールファイル）が解決済みの絶対パスを渡す
前提で、ツール本体側は ValueError を "エラー: ..." 形式の文字列へ変換する
薄いラッパーに徹する。
"""

from __future__ import annotations

import codecs
import difflib
import locale
import os
import stat
from collections.abc import Iterator
from pathlib import Path

# 巨大ファイルの逐次読みで、1行がこれより長ければ残りを読み捨てる（改行の無い
# GB 級のファイルで1行をまるごとメモリへ載せないため）。
STREAM_MAX_LINE_CHARS = 10000
_STREAM_ENCODING_PROBE_BYTES = 1024 * 1024


def _candidate_encodings() -> tuple[str, ...]:
    """read_text_with_fallback と同じ順序のエンコーディング候補。"""
    return tuple(dict.fromkeys(("utf-8", locale.getencoding(), "cp932")))


def format_size(size_bytes: int) -> str:
    """バイト数を "1.2 GB" のような短い表記にする（ツール結果の文言用）。"""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def detect_stream_encoding(path: Path) -> str | None:
    """巨大ファイルを逐次読みするときのエンコーディングを、先頭部分だけで決める。

    read_text_with_fallback と同じ候補・順序で、先頭 1MB をエラーなく
    デコードできた最初のものを返す（末尾で切れたマルチバイト文字はエラーに
    しない）。どれでも読めなければ None。
    """
    with open(path, "rb") as f:
        head = f.read(_STREAM_ENCODING_PROBE_BYTES)
    for encoding in _candidate_encodings():
        try:
            codecs.getincrementaldecoder(encoding)().decode(head, final=False)
            return encoding
        except UnicodeDecodeError:
            continue
        except LookupError:
            continue
    return None


def iter_text_lines(path: Path, encoding: str, max_line_chars: int = STREAM_MAX_LINE_CHARS) -> Iterator[str]:
    """テキストファイルを1行ずつ逐次読みする（改行は含めない）。

    改行の扱いは read_text_with_fallback と同じく \\r\\n・\\r・\\n を行区切りとする。
    先頭部分で決めたエンコーディングで後半が読めない場合に止まらないよう、
    デコードできないバイトは置換文字にする。max_line_chars を超える行は
    そこで切り、末尾に "…" を付けて残りを読み捨てる。
    """
    with open(path, encoding=encoding, errors="replace", newline=None) as f:
        while True:
            line = f.readline(max_line_chars)
            if not line:
                return
            if line.endswith("\n"):
                yield line[:-1]
                continue
            # 行が長すぎて途中で切れた（またはファイル末尾で改行が無い）。
            rest = f.readline(max_line_chars)
            if not rest:
                yield line
                return
            while rest and not rest.endswith("\n"):
                rest = f.readline(max_line_chars)
            yield line + "…"


_LINK_REPARSE_TAGS = frozenset(
    {getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C), getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)}
)


def is_link_dir(entry: os.DirEntry) -> bool:
    """ディレクトリのエントリがシンボリックリンクかジャンクションか。

    Python 3.11 の os.walk・pathlib の "**" はジャンクションを普通のフォルダとして
    たどるため、循環するジャンクションがあると延々と潜り続ける。再帰走査では
    これらに入らない。OneDrive のオンデマンドフォルダ等もリパースポイントだが、
    中身は実体のあるフォルダなので、タグで見分けてリンクだけを除く。
    """
    try:
        if entry.is_symlink():
            return True
        return getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0) in _LINK_REPARSE_TAGS
    except OSError:
        return True


def read_text_with_fallback(path: Path) -> str:
    """UTF-8 で読めなければ OS の ANSI コードページ（日本語版 Windows なら cp932、英語版なら cp1252 等）、
    CP932（Shift-JIS系）の順にフォールバックして読む。どれでも読めなければ UnicodeDecodeError を送出する。

    ANSI コードページを CP932 より先に試すのは、cp1252 の文書（"Größe" 等）の多くが CP932 としても
    エラーなく読めてしまい、文字化けに気付けないため。逆に Shift-JIS の日本語は句読点・括弧の先頭
    バイト 0x81 が cp1252 に無いため、英語版 Windows でも多くは CP932 へ進んで正しく読める。"""
    data = path.read_bytes()
    error: UnicodeDecodeError | None = None
    for encoding in dict.fromkeys(("utf-8", locale.getencoding(), "cp932")):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError as e:
            error = error or e
        except LookupError:
            continue
    else:
        raise error or UnicodeDecodeError("utf-8", data, 0, len(data), "undecodable")
    # Path.read_text と同じく改行を \n にそろえる（universal newlines）。
    return text.replace("\r\n", "\n").replace("\r", "\n")


def looks_binary(path: Path) -> bool:
    """先頭1024バイトに NUL バイトが含まれるかでバイナリらしさを判定する。"""
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
        return b"\x00" in chunk
    except OSError:
        return False


def suggest_similar_dir(path: Path) -> str:
    """存在しないディレクトリパスに対し、実在する近い候補をヒント文字列として返す。

    ローカルLLMがパスを記憶から手打ちで組み立て直した際にスペルミス・余分な
    空白・区切り文字の欠落を起こし、同じ失敗を繰り返すケースへの対策。
    実在する最も近い祖先ディレクトリまで遡り、そこから先の欠けている
    ディレクトリ名に似たものが兄弟ディレクトリの中にないか探す。

    Args:
        path: 存在しなかったディレクトリパス（解決済みの絶対パスを渡すこと）。

    Returns:
        候補が見つかればエラーメッセージに追記できる短いヒント文字列
        （先頭に半角スペース付き）。見つからなければ空文字列。
    """
    target = path
    ancestor = target
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or ancestor == target:
        return ""

    remainder = target.relative_to(ancestor)
    if not remainder.parts:
        return ""
    missing_name = remainder.parts[0]

    try:
        siblings = [child.name for child in ancestor.iterdir() if child.is_dir()]
    except OSError:
        return ""

    close = difflib.get_close_matches(missing_name, siblings, n=1, cutoff=0.5)
    if not close:
        return ""

    suggested = ancestor / close[0]
    return (
        f" Did you mean {suggested}?"
        " Do not rebuild paths from memory or guesswork; copy the string in the previous tool result "
        " or the path_memory @N as is."
    )
