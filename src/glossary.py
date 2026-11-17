"""用語集（memory_type="glossary" の永続メモリー）の登録ロジック。

用語は「1語1メモリー（1ファイル）」で、本文は決まった見出しの Markdown にする:

    **Term:** 用語
    **Aliases:** 別名1, 別名2
    **Meaning:** 用語の意味（必須。ファイルを指すだけの書き方は拒否する）
    **Usage:** 使われ方・使われる場面（任意）
    **Related terms:** 関連用語1, 関連用語2
    **Sources:**
    - 出典1
    - 出典2

LLM が create_memory で辞書ファイルの中身を reference に1件でまとめたり、「意味は○○.xlsx を参照」とだけ
書いたりしていたため、用語の登録は専用ツール（tools/glossary_tools.py の save_glossary_term）に任せ、
ここで機械的に次を守る:

- 1語1ファイル（名前は用語から自動で作る。日本語の用語でも ASCII の名前になる）。
- 同じ意味の用語（用語・別名のどれかが一致するもの）は1件にまとめる（別名に足し、重複した分は消す）。
- 関連用語は互いの本文に載せる（相手が登録済みなら、相手の Related terms にもこの用語を足す）。
- 意味（Meaning）は必須で、出典（Sources）も1つ以上要る。

用語どうしの一致は NFKC・大文字小文字・空白を無視して比べる（素朴な文字列一致だけ。意味の近さは見ない）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import memory

logger = logging.getLogger(__name__)

GLOSSARY_TYPE = "glossary"
_NAME_PREFIX = "glossary-"

# 本文の見出し（英語に固定して機械的に読み書きする。値は会話の表示言語のまま）。
_LABELS = {
    "term": "Term",
    "aliases": "Aliases",
    "meaning": "Meaning",
    "usage": "Usage",
    "related": "Related terms",
    "sources": "Sources",
    "notes": "Notes",
}
_LABEL_TO_KEY = {v: k for k, v in _LABELS.items()}
_LABEL_RE = re.compile(r"^\*\*(" + "|".join(re.escape(v) for v in _LABELS.values()) + r"):\*\*\s?(.*)$")
_LIST_SPLIT_RE = re.compile(r"\s*[,、，;；]\s*")
_DESCRIPTION_MEANING_CHARS = 80

# 「意味はファイルを見て」だけの書き方の目印（パス・@N・ファイル名・参照の言い回し）。
_REFERENCE_TOKENS_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s、。,]*|\\\\[^\s、。,]+|@\d+|[^\s、。,（）()「」]+\.(?:xlsx|xlsm|xls|csv|tsv|pdf|docx?|pptx?|txt|md|json)\b"
    r"|を?参照(?:のこと|してください|する|して|)|を見て(?:ください|)|に記載(?:されている|がある|あり|の通り|)|に載って(?:いる|います)"
    r"|のとおり|の通り|詳細は|詳しくは|see\b|refer to|listed in|described in|defined in|in the file|the file",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(r"[\s、。,.:：;；()（）「」『』\[\]【】\-–—・]")
# パス等の目印を除いた残りがこの文字数より短ければ、意味を書いていない（ファイルを指しているだけ）とみなす。
_MEANING_MIN_CHARS_WITH_REFERENCE = 8

_EXCERPT_CHARS = 160


@dataclass
class GlossaryEntry:
    """用語集の1語（本文を読み解いたもの）。"""

    term: str
    aliases: list[str] = field(default_factory=list)
    meaning: str = ""
    usage: str = ""
    related: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    notes: str = ""

    def keys(self) -> set[str]:
        """一致の判定に使う、用語と別名の正規化した形。"""
        return {k for k in (normalize(t) for t in [self.term, *self.aliases]) if k}


@dataclass(frozen=True)
class SaveResult:
    """save_term の結果（ツールの返す文の材料）。"""

    name: str
    path: Path
    entry: GlossaryEntry
    created: bool
    previous_meaning: str | None
    merged: list[str]
    linked: list[str]
    unregistered_related: list[str]
    others: list[str]


def normalize(text: str) -> str:
    """用語の比較用の形（NFKC・小文字・空白なし）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).casefold()


def _clean_list(values: list[str] | None) -> list[str]:
    """空・重複（正規化して同じもの）を除いた並び。"""
    result: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        value = " ".join(str(raw).split())
        key = normalize(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _split_list(text: str) -> list[str]:
    return _clean_list(_LIST_SPLIT_RE.split(text.strip())) if text.strip() else []


def _legacy_term(description: str, name: str) -> str:
    """見出しの無い（このモジュールより前の）用語メモリーの用語: 説明の先頭（括弧・コロンの前）。"""
    head = re.split(r"\s*[(（:：]|\s+[-–—]\s+", description, maxsplit=1)[0].strip()
    return head or name


def parse_entry(mem: memory.Memory) -> GlossaryEntry:
    """用語メモリーの本文を読み解く。見出しが無ければ本文全体を Notes とし、用語は説明の先頭から取る。"""
    fields: dict[str, list[str]] = {}
    current: str | None = None
    preamble: list[str] = []
    for line in mem.content.splitlines():
        m = _LABEL_RE.match(line)
        if m:
            current = _LABEL_TO_KEY[m.group(1)]
            fields.setdefault(current, [])
            if m.group(2).strip():
                fields[current].append(m.group(2))
        elif current is None:
            preamble.append(line)
        else:
            fields[current].append(line)
    if "term" not in fields:
        return GlossaryEntry(term=_legacy_term(mem.description, mem.name), notes=mem.content.strip())

    def text(key: str) -> str:
        return "\n".join(fields.get(key, [])).strip()

    sources = [re.sub(r"^\s*[-*]\s+", "", s).strip() for s in fields.get("sources", [])]
    notes = "\n".join([*preamble, text("notes")]).strip()
    return GlossaryEntry(
        term=text("term") or _legacy_term(mem.description, mem.name),
        aliases=_split_list(text("aliases")),
        meaning=text("meaning"),
        usage=text("usage"),
        related=_split_list(text("related")),
        sources=_clean_list([s for s in sources if s]),
        notes=notes,
    )


def render_content(entry: GlossaryEntry) -> str:
    """用語の本文（決まった見出しの Markdown）。"""
    lines = [f"**{_LABELS['term']}:** {entry.term}"]
    if entry.aliases:
        lines.append(f"**{_LABELS['aliases']}:** {', '.join(entry.aliases)}")
    lines.append(f"**{_LABELS['meaning']}:** {entry.meaning.strip()}")
    if entry.usage.strip():
        lines.append(f"**{_LABELS['usage']}:** {entry.usage.strip()}")
    if entry.related:
        lines.append(f"**{_LABELS['related']}:** {', '.join(entry.related)}")
    if entry.sources:
        lines.append(f"**{_LABELS['sources']}:**")
        lines += [f"- {s}" for s in entry.sources]
    if entry.notes.strip():
        lines.append(f"**{_LABELS['notes']}:** {entry.notes.strip()}")
    return "\n".join(lines)


def render_description(entry: GlossaryEntry) -> str:
    """索引に載る一行: 「用語 (別名): 意味の先頭」。"""
    head = entry.term + (f" ({', '.join(entry.aliases)})" if entry.aliases else "")
    meaning = " ".join(entry.meaning.split())
    if len(meaning) > _DESCRIPTION_MEANING_CHARS:
        meaning = meaning[:_DESCRIPTION_MEANING_CHARS] + "…"
    return f"{head}: {meaning}"


def validate_meaning(meaning: str) -> None:
    """意味が書かれているか（ファイルを指すだけの書き方でないか）を確かめる。不足なら ValueError。"""
    if not meaning or not meaning.strip():
        raise ValueError(
            "meaning is empty. Write what the term means in your own words (the definition itself), "
            "and put where it came from in sources."
        )
    if _REFERENCE_TOKENS_RE.search(meaning):
        rest = _FILLER_RE.sub("", _REFERENCE_TOKENS_RE.sub("", meaning))
        if len(rest) < _MEANING_MIN_CHARS_WITH_REFERENCE:
            raise ValueError(
                "meaning only points to a file or another place. A glossary entry is knowledge: write the "
                "definition of the term itself in meaning (read it from the file first), and put the file "
                "(with sheet/row/page) in sources."
            )


def _slug_name(term: str) -> str:
    """用語から作るメモリーの名前。ASCII だけの用語は読める形、それ以外はハッシュ。"""
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKC", term).lower()).strip("-")
    if ascii_slug and term.isascii():
        return _NAME_PREFIX + ascii_slug[:48].strip("-")
    return _NAME_PREFIX + hashlib.sha1(normalize(term).encode("utf-8")).hexdigest()[:12]


def _unused_name(root: Path, base: str, owner: str | None) -> str:
    name, n = base, 2
    while memory.find_memory(root, name, owner) is not None:
        name = f"{base[:60]}-{n}"
        n += 1
    return name


def _glossary_memories(root: Path, owners: list[str | None]) -> list[memory.Memory]:
    result: list[memory.Memory] = []
    for owner in owners:
        result += [m for m in memory._iter_memories(root, GLOSSARY_TYPE, owner)]
    return result


def find_matches(root: Path, keys: set[str], me: str) -> tuple[list[memory.Memory], list[memory.Memory]]:
    """用語・別名が keys のどれかと一致する用語メモリー: (書き換えられるもの（自分→共通の順）, 他のユーザーの見えるもの)。"""
    writable = [m for m in _glossary_memories(root, [me, None]) if parse_entry(m).keys() & keys]
    policy = memory.policy_for(root)
    others = [
        m
        for owner in memory.list_owners(root)
        if owner != me
        for m in memory._iter_memories(root, GLOSSARY_TYPE, owner)
        if memory.is_visible_to(m, me, policy) and parse_entry(m).keys() & keys
    ]
    return writable, others


def _merge_text(old: str, new: str) -> str:
    """意味・使われ方の合わせ方: 新しい方を使う。ただし新しい方が古い方に含まれるなら古い方を残す。"""
    old, new = old.strip(), new.strip()
    if not new:
        return old
    if old and normalize(new) in normalize(old):
        return old
    return new


def save_term(
    root: Path,
    *,
    me: str,
    term: str,
    meaning: str,
    aliases: list[str] | None = None,
    usage: str = "",
    related_terms: list[str] | None = None,
    sources: list[str] | None = None,
    source_files: list[Path] | None = None,
    visibility: str | None = None,
) -> SaveResult:
    """用語を1件登録する（同じ用語・別名の登録があればそれへまとめる）。関連用語は相手にも載せる。

    Args:
        root: メモリーストアのルート。
        me: 会話のユーザー（新しく作るときの所有者）。
        term: 用語。
        meaning: 意味（必須。ファイルを指すだけの書き方は ValueError）。
        aliases: 同じ意味の別名・略語・読み。
        usage: 使われ方・使われる場面。
        related_terms: 関連する（意味は違う）用語。
        sources: 出典（本文の Sources に載せる文。1つ以上）。
        source_files: 出典のうち実在するファイル（sha256 を記録する。None なら記録済みのまま）。
        visibility: 新しく作るときの公開範囲（memory.create_memory と同じ）。

    Raises:
        ValueError: term が空、meaning が無い・ファイルを指すだけ、sources が無い、保存できない場合。
    """
    term = " ".join((term or "").split())
    if not term:
        raise ValueError("term is empty")
    validate_meaning(meaning)
    new = GlossaryEntry(
        term=term,
        aliases=[a for a in _clean_list(aliases) if normalize(a) != normalize(term)],
        meaning=meaning.strip(),
        usage=(usage or "").strip(),
        related=_clean_list(related_terms),
        sources=_clean_list(sources),
    )
    if not new.sources:
        raise ValueError(
            "sources is empty. Pass where the meaning came from, as precisely as you can: the file with "
            "sheet/row/page (e.g. 'D:\\docs\\terms.xlsx (Sheet1, row 12)'), the knowledge document and page, "
            "or 'explained by the user (YYYY-MM-DD)'."
        )
    writable, others = find_matches(root, new.keys(), me)
    previous_meaning: str | None = None
    merged: list[str] = []
    if writable:
        target = writable[0]
        entry = parse_entry(target)
        previous_meaning = entry.meaning or None
        dupes = writable[1:]
        for d in dupes:
            de = parse_entry(d)
            entry.aliases += [de.term, *de.aliases]
            entry.related += de.related
            entry.sources += de.sources
            if de.meaning and normalize(de.meaning) not in normalize(new.meaning):
                entry.notes = "\n".join(x for x in [entry.notes, f"({de.term}) {de.meaning}"] if x)
            if de.notes:
                entry.notes = "\n".join(x for x in [entry.notes, de.notes] if x)
        entry.aliases = _clean_list([*entry.aliases, new.term, *new.aliases])
        entry.meaning = _merge_text(entry.meaning, new.meaning)
        entry.usage = _merge_text(entry.usage, new.usage)
        entry.related = _clean_list([*entry.related, *new.related])
        entry.sources = _clean_list([*entry.sources, *new.sources])
    else:
        target = None
        entry = new
    entry.aliases = [a for a in entry.aliases if normalize(a) != normalize(entry.term)]
    entry.related = [r for r in entry.related if normalize(r) not in entry.keys()]

    content = render_content(entry)
    description = render_description(entry)
    if target is None:
        name = _unused_name(root, _slug_name(entry.term), me)
        path = memory.create_memory(
            root, name, description, GLOSSARY_TYPE, content, source_files, owner=me, visibility=visibility, author=me
        )
        owner: str | None = me
    else:
        name, owner = target.name, target.owner
        path = memory.edit_memory(
            root,
            name,
            description=description,
            memory_type=GLOSSARY_TYPE,
            content=content,
            owner=owner,
            updated_by=me,
            source_files=_merge_source_files(target, source_files),
        )
        for d in writable[1:]:
            memory.delete_memory(root, d.name, d.owner)
            merged.append(d.name)

    linked, unregistered = _link_related(root, me, entry, exclude=(name, owner))
    logger.info(
        "save_glossary_term: term=%r name=%s owner=%s created=%s merged=%s linked=%s",
        entry.term, name, owner, target is None, merged, linked,
    )
    return SaveResult(
        name=name,
        path=path,
        entry=entry,
        created=target is None,
        previous_meaning=previous_meaning if previous_meaning and normalize(previous_meaning) != normalize(entry.meaning) else None,
        merged=merged,
        linked=linked,
        unregistered_related=unregistered,
        others=[f"{m.name} (user: {m.owner})" for m in others],
    )


def _merge_source_files(target: memory.Memory, source_files: list[Path] | None) -> list[Path] | None:
    """まとめ先に記録済みの元ファイルと今回の元ファイルを合わせる（今回が無ければ記録済みのまま）。"""
    if not source_files:
        return None
    paths = [Path(s.path) for s in target.sources if Path(s.path).is_file()]
    for p in source_files:
        if p not in paths:
            paths.append(p)
    return paths[-memory.SOURCE_FILES_MAX :]


def _link_related(root: Path, me: str, entry: GlossaryEntry, *, exclude: tuple[str, str | None]) -> tuple[list[str], list[str]]:
    """entry の関連用語が登録済み（自分・共通）なら、その Related terms に entry の用語を足す。

    Returns:
        (相手にも載せた・載っていた関連用語, まだ登録されていない関連用語)。
    """
    linked: list[str] = []
    unregistered: list[str] = []
    for rel in entry.related:
        matches, _ = find_matches(root, {normalize(rel)}, me)
        matches = [m for m in matches if (m.name, m.owner) != exclude]
        if not matches:
            unregistered.append(rel)
            continue
        other = matches[0]
        oe = parse_entry(other)
        if not ({normalize(r) for r in oe.related} & entry.keys()):
            oe.related.append(entry.term)
            if not oe.meaning:
                # 見出しの無い古い用語メモリー: 本文を意味として引き継いで見出し付きに書き直す。
                oe.meaning, oe.notes = oe.notes or oe.term, ""
            memory.edit_memory(
                root,
                other.name,
                description=render_description(oe),
                memory_type=GLOSSARY_TYPE,
                content=render_content(oe),
                owner=other.owner,
                updated_by=me,
            )
        linked.append(rel)
    return linked, unregistered


def excerpt_around(text: str, keys: set[str]) -> str | None:
    """text の中で用語（keys のどれか）が出てくるあたりの抜粋。出てこなければ None。"""
    flat = " ".join(text.split())
    norm_chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(flat):
        for c in unicodedata.normalize("NFKC", ch).casefold():
            if not c.isspace():
                norm_chars.append(c)
                index_map.append(i)
    norm = "".join(norm_chars)
    for key in sorted(keys, key=len, reverse=True):
        pos = norm.find(key)
        if pos >= 0:
            center = index_map[pos]
            start = max(0, center - _EXCERPT_CHARS // 2)
            end = min(len(flat), start + _EXCERPT_CHARS)
            return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")
    return None
