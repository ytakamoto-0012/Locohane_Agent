"""永続的ファイルベースメモリー（ロジック層）。

ClaudeCode 相当の「スレッド／セッションをまたいで共有される永続メモリー」を
実装する。役割はここまで（それだけ）:

1. memory_dir 配下に User / Feedback / Project / Reference の4種類の
   メモリーを YAML frontmatter 付き Markdown ファイルとして保存する。
2. 索引ファイル MEMORY.md を、保存されているメモリー群から都度再構築する
   （索引を差分更新せず常に全再構築することで整合ズレを防ぐ）。
3. システムプロンプトへ差し込むための索引テキストを組み立てる
   （{{memory}} プレースホルダー用、200行超は切り詰め）。
4. ファイルから読んだ内容を記録するメモリーには、元ファイルの sha256 を
   frontmatter の sources に残し、読むときに今の sha256 と照合する
   （一致すれば元ファイルを開き直さずに内容を使える）。

Chainlit には依存しない。ツール層（tools.py）の @tool 関数がこのモジュールの
関数を呼び、例外を "エラー: ..." 形式の文字列へ変換して LLM に返す。

賢い仕掛け（動的import・全文検索エンジン等）は入れない。ファイルを読み書き
して素朴なキーワード一致で検索する、それだけ。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# 4種類のメモリータイプ（サブディレクトリ名でもある）。
MEMORY_TYPES = ("user", "feedback", "project", "reference")

# name 検証ルール: ASCII の英数字・ハイフン・アンダースコアのみ、1〜64文字（ファイル名として安全にするため）。
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_NAME_MAX = 64

_INDEX_FILENAME = "MEMORY.md"
_INDEX_MAX_LINES = 200
_INDEX_ENTRY_MAX_CHARS = 150

# 元ファイル付きメモリーの上限。ファイルの本文をまるごと写すと read_memory のたびに
# コンテキストが膨らむため、関係する部分（値・表・該当ページの要点）だけに絞らせる。
SOURCED_CONTENT_MAX_CHARS = 4000
SOURCE_FILES_MAX = 10
_HASH_CHUNK_BYTES = 1024 * 1024

SourceState = Literal["unchanged", "changed", "missing"]


@dataclass(frozen=True)
class SourceFile:
    """メモリーの内容の元になったファイルの、記録した時点の指紋。

    Attributes:
        path: 元ファイルの絶対パス。
        sha256: 記録した時点の内容の sha256（16進）。
        size: 記録した時点のサイズ（バイト）。照合でサイズが違えばハッシュを計算せずに「変わった」とする。
    """

    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SourceCheck:
    """元ファイル1件の照合結果。

    Attributes:
        source: 記録してある指紋。
        state: "unchanged"（sha256 が一致）| "changed"（内容が違う）| "missing"（読めない・無い）。
    """

    source: SourceFile
    state: SourceState


@dataclass(frozen=True)
class Memory:
    """1件のメモリー（frontmatter のメタデータ＋本文）。

    Attributes:
        name: メモリーの一意な名前（4type横断でユニーク、拡張子を除くファイル名）。
        description: 一行の説明文（索引 MEMORY.md にもそのまま載る）。
        memory_type: "user" | "feedback" | "project" | "reference"。
        content: frontmatter を除いた本文（Markdown）。
        path: このメモリーの実ファイルパス。
        sources: 内容の元になったファイルの指紋（frontmatter の sources。無ければ空）。
    """

    name: str
    description: str
    memory_type: str
    content: str
    path: Path
    sources: tuple[SourceFile, ...] = ()


def ensure_dirs(memory_root: Path) -> None:
    """memory_root と4種類のtypeサブディレクトリを作成する（既存なら何もしない）。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
    """
    memory_root.mkdir(parents=True, exist_ok=True)
    for memory_type in MEMORY_TYPES:
        (memory_root / memory_type).mkdir(parents=True, exist_ok=True)


def _validate_name(name: str) -> None:
    """name がスラッグとして安全かを検証する。不正なら ValueError。"""
    if not name or len(name) > _NAME_MAX or not _NAME_RE.match(name):
        raise ValueError(
            f"name must be an ASCII slug: ASCII letters (a-z, A-Z), digits, hyphens and underscores, "
            f"up to {_NAME_MAX} characters: {name}"
        )


def _validate_type(memory_type: str) -> None:
    """memory_type が既定の4種のいずれかかを検証する。不正なら ValueError。"""
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"memory_type must be one of {MEMORY_TYPES}: {memory_type}")


def _safe_memory_path(memory_root: Path, memory_type: str, name: str) -> Path:
    """memory_root/<type>/<name>.md を返す。境界外・不正値なら ValueError。

    tools.py の _safe_path と同じ二重防御の思想: name は正規表現で
    ディレクトリ区切り文字自体を拒否した上で、resolve() + is_relative_to()
    でも境界を検証する。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        memory_type: "user" | "feedback" | "project" | "reference"。
        name: メモリーの一意な名前。

    Returns:
        検証済みの絶対パス。

    Raises:
        ValueError: memory_type が不正、name が不正、または解決後のパスが
            memory_root 配下に収まらない場合。
    """
    _validate_type(memory_type)
    _validate_name(name)
    root = memory_root.resolve()
    candidate = (root / memory_type / f"{name}.md").resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Access outside the memory directory is not allowed: {name}")
    return candidate


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    """先頭 `---` で囲まれた YAML frontmatter を (dict, 本文) で返す。

    skills._parse_frontmatter と同じ手法（先頭 "---" の有無 → "---" で
    3分割）だが、本文（本体テキスト）も呼び出し元へ返す点が異なる
    （メモリーは content の読み書きが必須のため）。

    Args:
        text: メモリーファイルの全文（UTF-8 でデコード済み）。

    Returns:
        (frontmatter の dict, frontmatter を除いた本文) のタプル。
        frontmatter が無い/壊れている/dict でない場合は (None, text)。
    """
    if not text.lstrip().startswith("---"):
        return None, text
    stripped = text.lstrip()
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return None, text
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        logger.warning("Failed to parse memory frontmatter YAML: %s", e)
        return None, text
    if not isinstance(data, dict):
        return None, text
    return data, parts[2].lstrip("\n")


def _now() -> str:
    """現在時刻を ISO8601（UTC）文字列で返す（frontmatter の created/updated 用）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _serialize(
    name: str,
    description: str,
    memory_type: str,
    content: str,
    *,
    created: str,
    updated: str,
    sources: tuple[SourceFile, ...] = (),
) -> str:
    """メモリー1件を frontmatter 付き Markdown 文字列へ組み立てる。"""
    frontmatter: dict = {
        "name": name,
        "description": description,
        "type": memory_type,
        "created": created,
        "updated": updated,
    }
    if sources:
        frontmatter["sources"] = [{"path": s.path, "sha256": s.sha256, "size": s.size} for s in sources]
    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
    return f"---\n{header}---\n\n{content.rstrip()}\n"


def _parse_sources(raw: object, path: Path) -> tuple[SourceFile, ...]:
    """frontmatter の sources を SourceFile のタプルへ変換する。壊れた要素は警告して読み飛ばす。"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        logger.warning("Ignoring the memory sources because it is not a list: %s", path)
        return ()
    sources: list[SourceFile] = []
    for item in raw:
        if (
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("sha256"), str)
            and isinstance(item.get("size"), int)
        ):
            sources.append(SourceFile(item["path"], item["sha256"], item["size"]))
        else:
            logger.warning("Ignoring an invalid memory source entry: %s: %r", path, item)
    return tuple(sources)


def _load(path: Path) -> Memory | None:
    """メモリーファイル1件を読み込み Memory へ変換する。壊れていれば None。"""
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    if frontmatter is None:
        logger.warning("Skipping because the memory frontmatter cannot be read: %s", path)
        return None
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    memory_type = frontmatter.get("type")
    if not isinstance(name, str) or not isinstance(description, str) or memory_type not in MEMORY_TYPES:
        logger.warning("Skipping because the memory frontmatter is invalid: %s", path)
        return None
    return Memory(
        name=name,
        description=description,
        memory_type=memory_type,
        content=body.rstrip("\n"),
        path=path,
        sources=_parse_sources(frontmatter.get("sources"), path),
    )


def _sha256_of(path: Path) -> str:
    """ファイルの sha256 を、全体を読み込まずに少しずつ計算する。"""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_sources(paths: list[Path]) -> tuple[SourceFile, ...]:
    """元ファイルの今の指紋（sha256・サイズ）を取る。

    Args:
        paths: 元ファイルの絶対パス（呼び出し側で @N・相対パスを解決済みのもの）。

    Returns:
        paths の順の SourceFile（同じパスは1件にまとめる）。

    Raises:
        ValueError: 件数が SOURCE_FILES_MAX を超える、絶対パスでない、ファイルでない・読めない場合。
    """
    unique: list[Path] = []
    for p in paths:
        if p not in unique:
            unique.append(p)
    if len(unique) > SOURCE_FILES_MAX:
        raise ValueError(f"source_files can list at most {SOURCE_FILES_MAX} files: {len(unique)}")
    sources: list[SourceFile] = []
    for p in unique:
        if not p.is_absolute():
            raise ValueError(f"source_files must be absolute paths: {p}")
        if not p.is_file():
            raise ValueError(f"source file not found (or not a file): {p}")
        try:
            sources.append(SourceFile(str(p), _sha256_of(p), p.stat().st_size))
        except OSError as e:
            raise ValueError(f"cannot read the source file: {p}: {e}") from e
    return tuple(sources)


def check_sources(mem: Memory) -> list[SourceCheck]:
    """メモリーの元ファイルを今の内容と照合する。

    サイズが違えばハッシュを計算せずに "changed"。サイズが同じなら sha256 を計算して比べる
    （更新日時は使わない。コピーや展開で変わっても内容が同じなら信用してよいため）。

    Args:
        mem: 照合するメモリー。

    Returns:
        mem.sources の順の照合結果（sources が無ければ空リスト）。
    """
    results: list[SourceCheck] = []
    for source in mem.sources:
        p = Path(source.path)
        try:
            if not p.is_file():
                state: SourceState = "missing"
            elif p.stat().st_size != source.size:
                state = "changed"
            else:
                state = "unchanged" if _sha256_of(p) == source.sha256 else "changed"
        except OSError as e:
            logger.warning("Cannot check a memory source file: %s: %s", p, e)
            state = "missing"
        results.append(SourceCheck(source, state))
    return results


def _validate_sourced_content(content: str, sources: tuple[SourceFile, ...]) -> None:
    """元ファイル付きのメモリーの本文が上限を超えていれば ValueError。"""
    if sources and len(content) > SOURCED_CONTENT_MAX_CHARS:
        raise ValueError(
            f"content of a memory with source_files must be at most {SOURCED_CONTENT_MAX_CHARS} characters "
            f"({len(content)} given). Keep only the parts that answer the kind of question "
            "(values, table rows, key points with page numbers), not the whole file."
        )


def _iter_memories(memory_root: Path, memory_type: str | None = None) -> list[Memory]:
    """対象type配下（省略時は全type）の *.md を走査し Memory のリストを返す。"""
    types = (memory_type,) if memory_type else MEMORY_TYPES
    results: list[Memory] = []
    for t in types:
        type_dir = memory_root / t
        if not type_dir.is_dir():
            continue
        for entry in sorted(type_dir.glob("*.md")):
            mem = _load(entry)
            if mem is not None:
                results.append(mem)
    return results


def _find_memory(memory_root: Path, name: str) -> Memory | None:
    """name だけを手がかりに4type横断でメモリーを探す（name は4type横断でユニーク前提）。"""
    _validate_name(name)
    for memory_type in MEMORY_TYPES:
        candidate = _safe_memory_path(memory_root, memory_type, name)
        if candidate.is_file():
            return _load(candidate)
    return None


def rebuild_index(memory_root: Path) -> None:
    """保存されている全メモリーから MEMORY.md を再生成する。

    索引は差分更新せず、create_memory/update_memory/delete_memory の
    末尾で毎回このように全再構築することで、ファイル群と索引の
    整合ズレを構造的に防ぐ。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
    """
    memories = sorted(_iter_memories(memory_root), key=lambda m: (m.memory_type, m.name))
    lines = [
        "# MEMORY.md",
        "",
        "Index of persistent memories. Rebuilt automatically by create_memory/update_memory/delete_memory.",
        "",
    ]
    if not memories:
        lines.append("(no memories saved)")
    else:
        for mem in memories:
            entry = f"- [{mem.memory_type}] {mem.name}: {mem.description}"
            lines.append(entry[:_INDEX_ENTRY_MAX_CHARS])
    (memory_root / _INDEX_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_memory(
    memory_root: Path,
    name: str,
    description: str,
    memory_type: str,
    content: str,
    source_files: list[Path] | None = None,
) -> Path:
    """新しいメモリーを保存し、MEMORY.md 索引を再構築する。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        name: 一意な名前（英数字・ハイフン・アンダースコアのみ）。
        description: 一行の説明文。
        memory_type: "user" | "feedback" | "project" | "reference"。
        content: メモリー本文。
        source_files: 本文の元になったファイルの絶対パス。指定すると今の sha256 を記録する
            （本文は SOURCED_CONTENT_MAX_CHARS 文字まで）。

    Returns:
        作成したファイルの絶対パス。

    Raises:
        ValueError: name/memory_type が不正、description/content が空、
            同名のメモリーが既に存在する、source_files が読めない・多すぎる、
            または元ファイル付きの本文が長すぎる場合。
    """
    if not description.strip():
        raise ValueError("description is empty")
    if not content.strip():
        raise ValueError("content is empty")
    path = _safe_memory_path(memory_root, memory_type, name)
    if _find_memory(memory_root, name) is not None:
        raise ValueError(f"A memory with the same name already exists: {name} (use update_memory to update it)")
    sources = fingerprint_sources(source_files) if source_files else ()
    _validate_sourced_content(content, sources)
    ensure_dirs(memory_root)
    now = _now()
    path.write_text(
        _serialize(name, description.strip(), memory_type, content, created=now, updated=now, sources=sources),
        encoding="utf-8",
    )
    rebuild_index(memory_root)
    logger.info("create_memory: name=%s type=%s", name, memory_type)
    return path


def update_memory(memory_root: Path, name: str, content: str, source_files: list[Path] | None = None) -> Path:
    """既存メモリーの本文を更新し、MEMORY.md 索引を再構築する（name/description/type は不変）。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        name: 更新対象メモリーの名前。
        content: 新しい本文。
        source_files: None なら記録済みの元ファイルの指紋をそのまま残す。リストを渡すと
            その今の sha256 で置き換える（空リストなら元ファイルの記録を外す）。

    Returns:
        更新したファイルの絶対パス。

    Raises:
        ValueError: content が空、name のメモリーが存在しない、source_files が読めない・多すぎる、
            または元ファイル付きの本文が長すぎる場合。
    """
    if not content.strip():
        raise ValueError("content is empty")
    mem = _find_memory(memory_root, name)
    if mem is None:
        raise ValueError(f"Memory not found: {name}")
    sources = mem.sources if source_files is None else fingerprint_sources(source_files)
    _validate_sourced_content(content, sources)
    frontmatter, _ = _split_frontmatter(mem.path.read_text(encoding="utf-8"))
    created = frontmatter.get("created") if frontmatter else None
    mem.path.write_text(
        _serialize(
            mem.name,
            mem.description,
            mem.memory_type,
            content,
            created=created or _now(),
            updated=_now(),
            sources=sources,
        ),
        encoding="utf-8",
    )
    rebuild_index(memory_root)
    logger.info("update_memory: name=%s", name)
    return mem.path


def delete_memory(memory_root: Path, name: str) -> Path:
    """メモリーを削除し、MEMORY.md 索引を再構築する。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        name: 削除対象メモリーの名前。

    Returns:
        削除したファイルの絶対パス（削除後はもう存在しない）。

    Raises:
        ValueError: name のメモリーが存在しない場合。
    """
    mem = _find_memory(memory_root, name)
    if mem is None:
        raise ValueError(f"Memory not found: {name}")
    mem.path.unlink()
    rebuild_index(memory_root)
    logger.info("delete_memory: name=%s", name)
    return mem.path


def read_memory(memory_root: Path, name: str) -> Memory:
    """メモリー1件を全文（本文込み）で読み込む。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        name: 読み込むメモリーの名前。

    Returns:
        該当する Memory。

    Raises:
        ValueError: name のメモリーが存在しない場合。
    """
    mem = _find_memory(memory_root, name)
    if mem is None:
        raise ValueError(f"Memory not found: {name}")
    return mem


def search_memories(memory_root: Path, query: str, memory_type: str | None = None) -> list[Memory]:
    """name/description/content に対する大文字小文字を区別しない部分一致検索。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        query: 検索キーワード。
        memory_type: 指定すればそのtypeのみを検索対象にする。

    Returns:
        ヒットした Memory のリスト（memory_type→name の順）。

    Raises:
        ValueError: query が空、または memory_type が不正な値の場合。
    """
    if not query.strip():
        raise ValueError("query is empty")
    if memory_type is not None:
        _validate_type(memory_type)
    needle = query.lower()
    hits = [
        mem
        for mem in _iter_memories(memory_root, memory_type)
        if needle in f"{mem.name}\n{mem.description}\n{mem.content}".lower()
    ]
    return sorted(hits, key=lambda m: (m.memory_type, m.name))


def list_memories(memory_root: Path, memory_type: str | None = None) -> list[Memory]:
    """保存されている全メモリー（または指定typeのみ）を列挙する。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        memory_type: 指定すればそのtypeのみを列挙する。

    Returns:
        Memory のリスト（memory_type→name の順）。

    Raises:
        ValueError: memory_type が不正な値の場合。
    """
    if memory_type is not None:
        _validate_type(memory_type)
    return sorted(_iter_memories(memory_root, memory_type), key=lambda m: (m.memory_type, m.name))


def render_memory_block(memory_root: Path, max_lines: int = _INDEX_MAX_LINES) -> str:
    """system_prompt.md の {{memory}} プレースホルダーへ差し込むテキストを組み立てる。

    MEMORY.md を読み、max_lines を超える場合は切り詰めて注記を付す
    （「常に会話コンテキストに読み込まれるが200行を超えると切り捨てられる」仕様）。

    Args:
        memory_root: メモリーストアのルートディレクトリ。
        max_lines: 差し込む最大行数。

    Returns:
        差し込み用テキスト。MEMORY.md が未作成/空の場合は
        「（保存されているメモリーはありません）」を返す。
    """
    index_path = memory_root / _INDEX_FILENAME
    if not index_path.is_file():
        return "(no memories saved)"
    lines = index_path.read_text(encoding="utf-8").splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [
            "",
            f"... (truncated after {max_lines} lines; check details with list_memories / search_memory)",
        ]
    text = "\n".join(lines).strip()
    return text or "(no memories saved)"
