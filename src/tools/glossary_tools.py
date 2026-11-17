"""用語集の登録ツール（save_glossary_term）。ロジックは src/glossary.py。

create_memory で用語をまとめて reference に入れたり、意味を書かずに辞書ファイルを指すだけの記録に
なったりしたため、用語は専用のツールで1語1ファイル・決まった見出しで保存させる。保存した後、
ナレッジ（ベクターDB）からその用語が出てくる箇所を少しだけ引いて返し、意味・使われ方を確かめさせる。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from langchain_core.tools import tool

from .. import embedding_usage, glossary, memory
from ..vector_db import search as vector_search
from ._safe_path import _resolve_file_tools_path
from .memory_tools import _current_owner, _require_memory_root, _visibility_note
from .vector_db_tools import _enabled_config, _record_search_usage

logger = logging.getLogger(__name__)

# 出典の文のうち、ファイルとして解決を試みる部分（"<パス> (Sheet1, row 3)" のパスの部分）。
_PATH_LIKE_RE = re.compile(r"^(@\d+|[A-Za-z]:[\\/].*?|\\\\.*?)(\s*[(（].*)?$")
_KNOWLEDGE_TOP_K = 8
_KNOWLEDGE_SHOW = 3


def _resolve_sources(sources: list[str] | None) -> tuple[list[str], list[Path]]:
    """出典の文を整え、実在するファイルを指すもの（@N・絶対パス）はパスへ解決する。

    Returns:
        (本文に載せる出典の文（@N は絶対パスに置き換える）, sha256 を記録するファイル)。
    """
    texts: list[str] = []
    files: list[Path] = []
    for raw in sources or []:
        text = " ".join(str(raw).split())
        if not text:
            continue
        m = _PATH_LIKE_RE.match(text)
        if m:
            try:
                p, error = _resolve_file_tools_path(m.group(1).strip())
            except Exception as e:  # noqa: BLE001 - 解決できない出典も、文として本文に残せばよい
                logger.warning("save_glossary_term: cannot resolve the source %r: %s", text, e)
                p, error = None, str(e)
            if not error and p is not None and p.is_file():
                text = f"{p}{m.group(2) or ''}"
                if p not in files:
                    files.append(p)
        texts.append(text)
    return texts, files[: memory.SOURCE_FILES_MAX]


def _knowledge_excerpts(entry: glossary.GlossaryEntry) -> str:
    """ナレッジでその用語（別名を含む）が出てくる箇所を数件引く（使えなければ空文字）。"""
    cfg = _enabled_config()
    if cfg is None:
        return ""
    keys = entry.keys()
    meter = embedding_usage.EmbeddingMeter()
    try:
        result = vector_search.search(cfg, " ".join([entry.term, *entry.aliases]), top_k=_KNOWLEDGE_TOP_K, meter=meter)
    except Exception as e:  # noqa: BLE001 - ナレッジが引けなくても用語の保存は済んでいる
        logger.warning("save_glossary_term: knowledge search failed: %s", e)
        return "\n[Knowledge] The knowledge could not be searched this time; check how the term is used later if needed."
    finally:
        _record_search_usage(cfg, meter)
    lines: list[str] = []
    for hit in result.hits:
        excerpt = glossary.excerpt_around(hit.text, keys)
        if excerpt is None:
            continue
        locator = f" {hit.locator}" if hit.locator else ""
        lines.append(f"- {hit.source_path}{locator}: {excerpt}")
        if len(lines) >= _KNOWLEDGE_SHOW:
            break
    if not lines:
        return "\n[Knowledge] No ingested document mentions this term."
    return (
        "\n[Knowledge] Passages in the ingested documents that mention this term:\n"
        + "\n".join(lines)
        + "\nCheck them against the saved meaning. If they show a different or more precise meaning, or how the "
        "term is used, call save_glossary_term again with the same term, the corrected meaning/usage and that file "
        "(with page) added to sources; it updates this entry."
    )


@tool
def save_glossary_term(
    term: str,
    meaning: str,
    sources: list[str],
    aliases: list[str] | None = None,
    usage: str = "",
    related_terms: list[str] | None = None,
    visibility: str | None = None,
) -> str:
    """Register one company-specific term in the glossary (one term = one memory file).

    Always use this tool (not create_memory/update_memory) to learn terms: jargon, abbreviations,
    internal product/system/department names, including every term of a dictionary file (xlsx/csv)
    the user asks you to learn. Call it once per term. The memory name is made automatically.
    If a saved term or alias already matches term or one of aliases, that entry is updated instead
    of creating a duplicate (terms with the same meaning are merged into one entry, and the
    duplicates are deleted). Related terms are written into both entries.

    A glossary entry is knowledge: write the definition itself. "See terms.xlsx" or "described in
    the file" is rejected; read the file first and write what the term means.

    Args:
        term: The term as it is written (e.g. "受注残" or "ABC").
        meaning: What the term means, in your own words: the definition itself (required). Write it
            in the language of the conversation. When the entry exists, this replaces the stored
            meaning, so write the complete meaning (the previous one is shown in the result).
        sources: Where the meaning came from, as precisely as you can (at least one): the file with
            sheet/row/page (an absolute path or `@N`, e.g. "D:\\docs\\terms.xlsx (Sheet1, row 12)"),
            the knowledge document and page, or "explained by the user (YYYY-MM-DD)". Files given by
            path are fingerprinted (sha256) like source_files of create_memory.
        aliases: Other names with the same meaning: abbreviations, full forms, readings, spelling
            variants (e.g. ["じゅちゅうざん", "受注残高"]).
        usage: How and where the term is used (in which documents, tasks or departments; an example
            sentence). Write what the documents show.
        related_terms: Terms related to this one but with a different meaning (e.g. a broader term,
            the opposite term, a term used in the same process).
        visibility: "shared" or "private", as for create_memory (if omitted, shared).

    Returns:
        A text telling the saved entry (new or updated), merged duplicates, related terms linked
        back and those not registered yet, and passages of the ingested documents (knowledge) that
        mention the term, to check the meaning against. On invalid input (empty term, a meaning
        that only points to a file, no sources) it returns an "Error: ..." string instead of raising.
    """
    try:
        root = _require_memory_root()
        me = _current_owner()
        source_texts, source_files = _resolve_sources(sources)
        result = glossary.save_term(
            root,
            me=me,
            term=term,
            meaning=meaning,
            aliases=aliases,
            usage=usage,
            related_terms=related_terms,
            sources=source_texts,
            source_files=source_files or None,
            visibility=visibility,
        )
        mem = memory.find_memory(root, result.name, me) or memory.find_memory(root, result.name, None)
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"
    entry = result.entry
    if result.created:
        lines = [f'Saved the glossary term "{entry.term}" as a new entry ({result.name}): {result.path}']
    else:
        lines = [f'Updated the existing glossary entry "{entry.term}" ({result.name}): {result.path}']
        if glossary.normalize(term) != glossary.normalize(entry.term):
            lines.append(f'"{" ".join(term.split())}" was added to its aliases (same meaning).')
    if mem is not None and mem.owner is not None:
        lines.append(_visibility_note(root, mem, visibility))
    if entry.aliases:
        lines.append(f"Aliases: {', '.join(entry.aliases)}")
    if result.merged:
        lines.append(f"Merged entries with the same meaning into this one and deleted them: {', '.join(result.merged)}")
    if result.previous_meaning:
        lines.append(
            f'The stored meaning was replaced. Previous meaning: "{result.previous_meaning}". If part of it was still '
            "right, call again with the complete meaning."
        )
    if result.linked:
        lines.append(f"Linked both ways with the related terms: {', '.join(result.linked)}")
    if result.unregistered_related:
        lines.append(
            f"Related terms not registered yet: {', '.join(result.unregistered_related)} (if they are "
            "company-specific terms, register them too; they will be linked both ways)."
        )
    if result.others:
        lines.append(f"Other users also have an entry for this term: {', '.join(result.others)}")
    return "\n".join(lines) + _knowledge_excerpts(entry)

