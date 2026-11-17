"""用語集（memory_type="glossary" の永続メモリー）の登録ロジック。

用語は「1語1メモリー（1ファイル）」で、本文は決まった見出しの Markdown にする:

    **Term:** 用語
    **Aliases:**
    - 別名1
    - 別名2
    **Meaning:** 用語の意味（必須。ファイルを指すだけの書き方は拒否する）
    **Usage:** 使われ方・使われる場面（任意）
    **Related terms:**
    - 関連用語1
    **Sources:**
    - 出典1
    - 出典2

別名・関連用語は1行1件の箇条書きにする（カンマ区切りにすると、カンマや読点を含む別名が読み戻すときに割れるため）。
以前のカンマ区切り（`**Aliases:** 別名1, 別名2`）の本文も読める。

LLM が create_memory で辞書ファイルの中身を reference に1件でまとめたり、「意味は○○.xlsx を参照」とだけ
書いたりしていたため、用語の登録は専用ツール（tools/glossary_tools.py の save_glossary_term）に任せ、
ここで機械的に次を守る:

- 1語1ファイル（名前は用語から自動で作る。日本語の用語でも ASCII の名前になる）。
- 同じ意味の用語（用語・別名のどれかが一致するもの）は1件にまとめる（別名に足し、重複した分は消す）。
  まとめるのは同じ所有者の用語どうしだけ（自分の用語へ共通の用語を取り込んで消すと、他の全員から見えなくなるため）。
- 関連用語は互いの本文に載せる（相手が登録済みなら、相手の Related terms にもこの用語を足す）。
  ただし個人用の用語の名前は、他の人から見える用語には書き込まない。
- 意味（Meaning）は必須で、出典（Sources）も1つ以上要る。
- 同じ用語を何度登録し直しても、本文が上限（元ファイル付きは memory.SOURCED_CONTENT_MAX_CHARS）を超えないよう、
  古い出典・Notes から切り詰める。

用語どうしの一致は NFKC・大文字小文字・空白を無視して比べる（素朴な文字列一致だけ。意味の近さは見ない）。

サブエージェントは1応答内の複数のツール呼び出しを並列（同期ツールはスレッド）で実行するため、save_term は
メモリーのルートごとのロックで1件ずつ順に行う（同じ名前を同時に選んで上書きする・重複の削除が重なるのを防ぐ）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
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
_BULLET_RE = re.compile(r"^\s*[-*]\s+")
# 以前のカンマ区切りの一覧（箇条書きでない行）を分ける区切り。
_LIST_SPLIT_RE = re.compile(r"\s*[,、，;；]\s*")
_DESCRIPTION_MEANING_CHARS = 80

# 「意味はファイルを見て」だけの書き方の目印。
# パス・@N（ここから後ろの空白・読点までをまとめて除く）。
_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s、。,]*|\\\\[^\s、。,]+|@\d+")
# ファイル名の拡張子（直後が英数字でなければ一致。日本語が続いても一致させるため \b は使わない）。
_FILE_EXT_RE = re.compile(r"\.(?:xlsx|xlsm|xls|csv|tsv|pdf|docx?|pptx?|txt|md|json)(?![A-Za-z0-9])", re.IGNORECASE)
# 拡張子の前の ASCII のファイル名（あれば、そこまでをファイル名とみなす）。
_ASCII_STEM_RE = re.compile(r"[A-Za-z0-9_\-.~]+$")
# 拡張子の前が日本語などのとき、ファイル名とみなす最大の文字数（区切りがあればそこまで）。
# 文の頭から拡張子までを丸ごとファイル名として除くと、ファイル名の前に書いた正しい定義まで消えるため。
_NON_ASCII_STEM_MAX = 12
_STEM_SEPARATORS = frozenset("、。,，.:：;；()（）「」『』[]【】<>＜＞'\"")
_REFERENCE_PHRASES_RE = re.compile(
    r"を?参照(?:のこと|してください|する|して|)|を見て(?:ください|)|に記載(?:されている|がある|あり|の通り|)|に載って(?:いる|います)"
    r"|のとおり|の通り|詳細は|詳しくは|\bsee\b|refer to|listed in|described in|defined in|in the file|the file",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(r"[\s、。,.:：;；()（）「」『』\[\]【】\-–—・]")
# パス等の目印を除いた残りがこの文字数より短ければ、意味を書いていない（ファイルを指しているだけ）とみなす。
_MEANING_MIN_CHARS_WITH_REFERENCE = 8

# 登録し直すたびに増える部分の上限（古いものから捨てる）。
_SOURCES_MAX = 10
_NOTES_MAX_CHARS = 1000
_RELATED_MAX = 30

_EXCERPT_CHARS = 160

_SAVE_LOCKS: dict[str, threading.RLock] = {}
_SAVE_LOCKS_GUARD = threading.Lock()


def _save_lock(root: Path) -> threading.RLock:
    """メモリーのルートごとの、用語の登録のロック（同じプロセスの中の並列呼び出しを1件ずつにする）。"""
    key = os.path.normcase(str(Path(root).resolve()))
    with _SAVE_LOCKS_GUARD:
        return _SAVE_LOCKS.setdefault(key, threading.RLock())


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
    not_linked_private: list[str] = field(default_factory=list)
    trimmed: bool = False


@dataclass
class _Loaded:
    """読み込んだ用語メモリーと、その本文を読み解いたもの。"""

    mem: memory.Memory
    entry: GlossaryEntry

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.mem.name, self.mem.owner)


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


def _parse_list(lines: list[str]) -> list[str]:
    """一覧の見出しの値: 箇条書きの行は1行1件、そうでない行（以前のカンマ区切り）は区切りで分ける。"""
    items: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        if _BULLET_RE.match(line):
            items.append(_BULLET_RE.sub("", line).strip())
        else:
            items += _LIST_SPLIT_RE.split(line.strip())
    return _clean_list(items)


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

    sources = [_BULLET_RE.sub("", s).strip() for s in fields.get("sources", [])]
    notes = "\n".join([*preamble, text("notes")]).strip()
    return GlossaryEntry(
        term=text("term") or _legacy_term(mem.description, mem.name),
        aliases=_parse_list(fields.get("aliases", [])),
        meaning=text("meaning"),
        usage=text("usage"),
        related=_parse_list(fields.get("related", [])),
        sources=_clean_list([s for s in sources if s]),
        notes=notes,
    )


def render_content(entry: GlossaryEntry) -> str:
    """用語の本文（決まった見出しの Markdown）。"""
    lines = [f"**{_LABELS['term']}:** {entry.term}"]
    if entry.aliases:
        lines.append(f"**{_LABELS['aliases']}:**")
        lines += [f"- {a}" for a in entry.aliases]
    lines.append(f"**{_LABELS['meaning']}:** {entry.meaning.strip()}")
    if entry.usage.strip():
        lines.append(f"**{_LABELS['usage']}:** {entry.usage.strip()}")
    if entry.related:
        lines.append(f"**{_LABELS['related']}:**")
        lines += [f"- {r}" for r in entry.related]
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


def _strip_file_names(text: str) -> tuple[str, bool]:
    """text からファイル名（拡張子とその前のファイル名の部分）を除く。

    拡張子の前が ASCII ならその ASCII の並びだけを、日本語などなら区切り（読点・括弧等）か
    _NON_ASCII_STEM_MAX 文字までをファイル名とみなす（ファイル名の前に書いた定義を消さないため）。

    Returns:
        (除いた後の文, ファイル名があったか)。
    """
    parts: list[str] = []
    last = 0
    found = False
    for m in _FILE_EXT_RE.finditer(text):
        found = True
        head = text[last : m.start()]
        ascii_stem = _ASCII_STEM_RE.search(head)
        if ascii_stem:
            cut = ascii_stem.start()
        else:
            cut = len(head)
            while (
                cut > 0
                and len(head) - cut < _NON_ASCII_STEM_MAX
                and not head[cut - 1].isspace()
                and head[cut - 1] not in _STEM_SEPARATORS
            ):
                cut -= 1
        parts.append(head[:cut])
        last = m.end()
    parts.append(text[last:])
    return "".join(parts), found


def validate_meaning(meaning: str) -> None:
    """意味が書かれているか（ファイルを指すだけの書き方でないか）を確かめる。不足なら ValueError。"""
    if not meaning or not meaning.strip():
        raise ValueError(
            "meaning is empty. Write what the term means in your own words (the definition itself), "
            "and put where it came from in sources."
        )
    without_paths = _PATH_RE.sub("", meaning)
    without_files, has_file = _strip_file_names(without_paths)
    has_reference = has_file or without_paths != meaning or _REFERENCE_PHRASES_RE.search(without_files) is not None
    if not has_reference:
        return
    rest = _FILLER_RE.sub("", _REFERENCE_PHRASES_RE.sub("", without_files))
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


def _load_writable(root: Path, me: str) -> list[_Loaded]:
    """書き換えられる用語（自分 → 共通の順）を読み込む。"""
    result: list[_Loaded] = []
    for owner in (me, None):
        result += [_Loaded(m, parse_entry(m)) for m in memory._iter_memories(root, GLOSSARY_TYPE, owner)]
    return result


def _matching_others(root: Path, keys: set[str], me: str) -> list[memory.Memory]:
    """他のユーザーの用語のうち、keys のどれかと一致し、me から見えるもの。"""
    policy = memory.policy_for(root)
    return [
        m
        for owner in memory.list_owners(root)
        if owner != me
        for m in memory._iter_memories(root, GLOSSARY_TYPE, owner)
        if parse_entry(m).keys() & keys and memory.is_visible_to(m, me, policy)
    ]


def find_matches(root: Path, keys: set[str], me: str) -> tuple[list[memory.Memory], list[memory.Memory]]:
    """用語・別名が keys のどれかと一致する用語メモリー: (書き換えられるもの（自分→共通の順）, 他のユーザーの見えるもの)。"""
    writable = [x.mem for x in _load_writable(root, me) if x.entry.keys() & keys]
    return writable, _matching_others(root, keys, me)


def _merge_text(old: str, new: str) -> str:
    """意味・使われ方の合わせ方: 新しい方を使う。ただし新しい方が古い方に含まれるなら古い方を残す。"""
    old, new = old.strip(), new.strip()
    if not new:
        return old
    if old and normalize(new) in normalize(old):
        return old
    return new


def _cap_lists(entry: GlossaryEntry) -> bool:
    """登録し直すたびに増える部分（出典・関連用語・Notes）を上限まで切り詰める（古いものから）。切ったら True。"""
    trimmed = False
    if len(entry.sources) > _SOURCES_MAX:
        entry.sources = entry.sources[-_SOURCES_MAX:]
        trimmed = True
    if len(entry.related) > _RELATED_MAX:
        entry.related = entry.related[:_RELATED_MAX]
        trimmed = True
    notes = entry.notes.strip()
    if len(notes) > _NOTES_MAX_CHARS:
        entry.notes = "…" + notes[-_NOTES_MAX_CHARS:]
        trimmed = True
    return trimmed


def _fit_content(entry: GlossaryEntry, has_source_files: bool) -> bool:
    """本文が保存できる長さに収まるよう、Notes → 古い出典の順に切り詰める。切ったら True。

    Raises:
        ValueError: 用語・意味・使われ方だけで上限を超える場合（LLM に短くさせる）。
    """
    trimmed = _cap_lists(entry)
    if not has_source_files:
        return trimmed
    limit = memory.SOURCED_CONTENT_MAX_CHARS
    while len(render_content(entry)) > limit and entry.notes:
        over = len(render_content(entry)) - limit
        keep = len(entry.notes) - over - 1
        entry.notes = "…" + entry.notes[-keep:] if keep > 20 else ""
        trimmed = True
    while len(render_content(entry)) > limit and len(entry.sources) > 1:
        entry.sources = entry.sources[1:]
        trimmed = True
    while len(render_content(entry)) > limit and entry.related:
        entry.related = entry.related[:-1]
        trimmed = True
    if len(render_content(entry)) > limit:
        raise ValueError(
            f"the glossary entry is longer than {limit} characters even without its notes. Shorten meaning "
            "and usage (keep the definition and the key points of how it is used)."
        )
    return trimmed


def _merge_fingerprints(target: memory.Memory, source_files: list[Path] | None) -> tuple[memory.SourceFile, ...] | None:
    """まとめ先に記録済みの元ファイルの指紋と、今回の元ファイルの指紋を合わせる（今回が無ければ None = 記録済みのまま）。

    記録済みの指紋は記録した時のまま残す（今の内容で取り直すと、記録後に変わったファイルが「変わっていない」扱いになり、
    無くなったファイルは記録から黙って外れるため）。今回渡したファイルだけ今の指紋で取り直す。
    """
    if not source_files:
        return None
    fresh = memory.fingerprint_sources(source_files)
    fresh_keys = {os.path.normcase(s.path) for s in fresh}
    kept = [s for s in target.sources if os.path.normcase(s.path) not in fresh_keys]
    return (*kept, *fresh)[-memory.SOURCE_FILES_MAX :]


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
        visibility: 公開範囲（memory.create_memory と同じ）。既にある自分の用語を更新するときも反映する
            （None なら、新規は shared、更新は今の値のまま。ただし個人用の重複をまとめたときは private）。
            共通の用語では無視する。

    Raises:
        ValueError: term が空、meaning が無い・ファイルを指すだけ、sources が無い、visibility が不正、保存できない場合。
    """
    term = " ".join((term or "").split())
    if not term:
        raise ValueError("term is empty")
    validate_meaning(meaning)
    if visibility is not None and visibility not in memory.VISIBILITIES:
        raise ValueError(f"visibility must be one of {memory.VISIBILITIES}: {visibility}")
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
    with _save_lock(root):
        return _save_term_locked(root, me, new, source_files, visibility)


def _save_term_locked(
    root: Path, me: str, new: GlossaryEntry, source_files: list[Path] | None, visibility: str | None
) -> SaveResult:
    policy = memory.policy_for(root)
    loaded = _load_writable(root, me)
    keys = new.keys()
    writable = [x for x in loaded if x.entry.keys() & keys]
    # まとめるのは、まとめ先と同じ所有者の用語だけ（自分の用語に共通の用語を取り込んで消すと、他の全員から消えるため）。
    target = writable[0] if writable else None
    dupes = [x for x in writable[1:] if target is not None and x.mem.owner == target.mem.owner]
    untouched = [x for x in writable[1:] if x not in dupes]

    previous_meaning: str | None = None
    merged: list[str] = []
    if target is not None:
        entry = target.entry
        previous_meaning = entry.meaning or None
        for d in dupes:
            de = d.entry
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
        entry = new
    entry.aliases = [a for a in entry.aliases if normalize(a) != normalize(entry.term)]
    entry.related = [r for r in entry.related if normalize(r) not in entry.keys()]

    if target is None:
        fingerprints = None
        has_source_files = bool(source_files)
    else:
        fingerprints = _merge_fingerprints(target.mem, source_files)
        has_source_files = bool(fingerprints if fingerprints is not None else target.mem.sources)
    trimmed = _fit_content(entry, has_source_files)
    content = render_content(entry)
    description = render_description(entry)

    if target is None:
        name = _unused_name(root, _slug_name(entry.term), me)
        path = memory.create_memory(
            root, name, description, GLOSSARY_TYPE, content, source_files, owner=me, visibility=visibility, author=me
        )
        owner: str | None = me
    else:
        name, owner = target.mem.name, target.mem.owner
        final_visibility = visibility
        if owner is not None and final_visibility is None and any(
            x.mem.visibility == "private" for x in [target, *dupes]
        ):
            # 個人用の重複を取り込んだら、まとめた後も個人用にする（他の人へ見せないため）。
            final_visibility = "private"
        path = memory.edit_memory(
            root,
            name,
            description=description,
            memory_type=GLOSSARY_TYPE,
            content=content,
            owner=owner,
            visibility=final_visibility if owner is not None else None,
            updated_by=me,
            fingerprints=fingerprints,
        )
        for d in dupes:
            try:
                memory.delete_memory(root, d.mem.name, d.mem.owner)
            except ValueError:
                # 別のプロセス（設定ダッシュボード等）が先に消した。
                logger.info("save_glossary_term: the duplicate %s was already deleted", d.mem.name)
            merged.append(d.mem.name)

    saved = memory.find_memory(root, name, owner)
    private = saved is not None and memory.is_private_to_owner(saved, policy)
    done = {(name, owner), *(d.key for d in dupes)}
    candidates = [x for x in loaded if x.key not in done]
    linked, unregistered, not_linked_private = _link_related(root, me, entry, candidates, private=private, policy=policy)
    others = [f"{x.mem.name} (common)" for x in untouched]
    others += [f"{m.name} (user: {m.owner})" for m in _matching_others(root, entry.keys(), me)]
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
        others=others,
        not_linked_private=not_linked_private,
        trimmed=trimmed,
    )


def _link_related(
    root: Path,
    me: str,
    entry: GlossaryEntry,
    candidates: list[_Loaded],
    *,
    private: bool,
    policy: memory.MemoryPolicy,
) -> tuple[list[str], list[str], list[str]]:
    """entry の関連用語が登録済み（自分・共通）なら、その Related terms に entry の用語を足す。

    entry が個人用なら、自分の個人用の用語にだけ書き込む（個人用の用語の名前を、他の人から見える用語に載せないため）。

    Args:
        candidates: 書き換えられる用語（保存した用語・まとめて消した用語を除く）。書き込んだものはここで差し替える。

    Returns:
        (相手にも載せた・載っていた関連用語, まだ登録されていない関連用語, 相手が他の人から見えるので載せなかった関連用語)。
    """
    linked: list[str] = []
    unregistered: list[str] = []
    not_linked_private: list[str] = []
    for rel in entry.related:
        matches = [i for i, x in enumerate(candidates) if normalize(rel) in x.entry.keys()]
        if not matches:
            unregistered.append(rel)
            continue
        if private:
            matches = [i for i in matches if candidates[i].mem.owner == me and memory.is_private_to_owner(candidates[i].mem, policy)]
            if not matches:
                not_linked_private.append(rel)
                continue
        i = matches[0]
        other = candidates[i]
        oe = other.entry
        if not ({normalize(r) for r in oe.related} & entry.keys()):
            oe.related.append(entry.term)
            if not oe.meaning:
                # 見出しの無い古い用語メモリー: 本文を意味として引き継いで見出し付きに書き直す。
                oe.meaning, oe.notes = oe.notes or oe.term, ""
            try:
                _fit_content(oe, bool(other.mem.sources))
                memory.edit_memory(
                    root,
                    other.mem.name,
                    description=render_description(oe),
                    memory_type=GLOSSARY_TYPE,
                    content=render_content(oe),
                    owner=other.mem.owner,
                    updated_by=me,
                )
            except ValueError as e:
                # 相手が消された・長すぎる等。こちらの用語の保存は済んでいるので、リンクだけ諦める。
                logger.warning("save_glossary_term: cannot link back %s: %s", other.mem.name, e)
                unregistered.append(rel)
                continue
            reloaded = memory.find_memory(root, other.mem.name, other.mem.owner)
            if reloaded is not None:
                candidates[i] = _Loaded(reloaded, parse_entry(reloaded))
            if not ({normalize(r) for r in candidates[i].entry.related} & entry.keys()):
                # 相手の関連用語が上限に達していて載らなかった。
                unregistered.append(rel)
                continue
        linked.append(rel)
    return linked, unregistered, not_linked_private


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
