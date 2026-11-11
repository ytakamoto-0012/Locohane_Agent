"""ベクターDB（社内文書の意味検索、src/vector_db/）のツール群（読み取り専用）。"""

from __future__ import annotations

from langchain_core.tools import tool
import logging

from .. import chat_log, embedding_usage
from ..vector_db import search as vector_search
from ..vector_db.catalog import CatalogError
from ..vector_db.embeddings import EmbeddingError
from ..vector_db.store import StoreError

from . import _state
from ._path_memory_helpers import _register_path_memory

logger = logging.getLogger(__name__)

_DISABLED_MESSAGE = "The vector DB feature is disabled ([vector_db] enabled = false in config.ini). Internal documents cannot be searched."
# Hit.matched（一致した埋め込み）の表示名。
_MATCHED_LABELS = {"text": "text", "keyword": "keyword", "image": "image"}
# search_document_paths の Hit.matched の表示名。
_PATH_MATCHED_LABELS = {"path": "meaning", "path_keyword": "keyword"}


def _unexpected_error(tool_name: str, e: Exception) -> str:
    """想定外の例外を「エラー: ...」の文字列にする。

    langgraph の ToolNode は既定で ToolInvocationError 以外の例外を投げ直し、会話のターンごと止めるため、
    ベクターDB の一時的な不具合（取り込みの「再構築」と同時の検索で表が消えた等）は文字列で返す。
    """
    logger.exception("%s: unexpected error", tool_name)
    return f"Error: an unexpected error occurred while searching internal documents ({type(e).__name__}: {e}). Try again later."


def _chat_owner() -> str:
    """会話のユーザー（生成の台帳 token_usage_monthly と同じ thread_store.resolve_owner の名前）。セッション外は匿名。"""
    try:
        from chainlit.context import context as cl_context  # noqa: PLC0415 - evals・テストでは文脈が無い

        user = cl_context.session.user
    except Exception:  # noqa: BLE001 - ChainlitContextException を含むセッション文脈外
        user = None
    return chat_log.resolve_log_username(user.identifier if user else None)


def _record_search_usage(cfg, meter: embedding_usage.EmbeddingMeter) -> None:
    """検索語の埋め込みのトークン数を、スレッドDBの埋め込みの台帳へ記録する（「利用量・料金」用）。"""
    if cfg.thread_store_enabled:
        embedding_usage.flush_meter(meter, cfg.thread_store_db, embedding_usage.SOURCE_SEARCH, _chat_owner())


def _enabled_config():
    cfg = _state._LLM_CONFIG
    if cfg is None or not cfg.vector_db_enabled:
        return None
    return cfg


# render_document_collections_block() に載せるコレクションの上限（超えた分は list_document_collections で見る）。
_PROMPT_COLLECTIONS_MAX = 30


def render_document_collections_block(cfg) -> str:
    """system_prompt.md の {{document_collections}} へ差し込むテキストを組み立てる。

    取り込みで一覧が変わるため、起動時ではなくグラフを組み立てるたびに呼ぶ（app.py の _rebuild_graph）。
    取り込み日時・チャンク数は載せない（取り込みのたびにプロンプトが変わるのを減らすため）。
    使えない参照（参照元が無い等）は載せない。
    """
    if cfg is None or not cfg.vector_db_enabled:
        return "(none: the knowledge feature is disabled. Do not use search_documents etc.)"
    try:
        collections = vector_search.list_collections(cfg)
        references = vector_search.list_references(cfg)
    except Exception:  # noqa: BLE001 - 一覧が読めなくても会話は始められるようにする
        logger.exception("render_document_collections_block: failed to list collections")
        return "(the list could not be loaded; check it with list_document_collections)"
    entries = [(c.name, c.description, c.file_count) for c in collections]
    entries += [(r.reference.name, r.collection.description, r.collection.file_count) for r in references if r.collection is not None]
    if not entries:
        return "(none: no documents have been ingested yet)"
    lines = [
        f"- {name}{f' - {description}' if description else ''} ({count} documents)"
        for name, description, count in entries[:_PROMPT_COLLECTIONS_MAX]
    ]
    if len(entries) > _PROMPT_COLLECTIONS_MAX:
        lines.append(f"- ... and {len(entries) - _PROMPT_COLLECTIONS_MAX} more (see list_document_collections)")
    return "\n".join(lines)


@tool
def list_document_collections() -> str:
    """Return the list of searchable collections (groups of documents) of internal documents.

    Use it to look up the names to pass to the collection argument of search_documents and
    search_document_paths. It is read-only, so you may call it any time regardless of the plan.

    Returns:
        A text with, for each collection, its name, description, number of documents, number of
        chunks and last ingestion time. If the feature is disabled or there are no collections,
        a text saying so.
    """
    cfg = _enabled_config()
    if cfg is None:
        return _DISABLED_MESSAGE
    try:
        collections = vector_search.list_collections(cfg)
        references = vector_search.list_references(cfg)
    except (CatalogError, OSError) as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 - sqlite3.Error 等。ツールの例外は会話のターンごと止めるため文字列で返す
        return _unexpected_error("list_document_collections", e)
    if not collections and not references:
        return "There are no collections yet (an administrator needs to ingest documents in the \"Knowledge\" tab of the settings dashboard)."
    lines = [f"There are {len(collections) + len(references)} collections."]
    for c in collections:
        description = f" - {c.description}" if c.description else ""
        ingested = c.last_ingested_at or "not ingested"
        lines.append(f"- {c.name}{description} ({c.file_count} documents / {c.chunk_count} chunks / last ingested {ingested})")
    # 別のインスタンスのコレクションの参照（取り込みは参照元のインスタンスで行う）。
    for r in references:
        source = f"references collection {r.reference.source_collection} of instance {r.reference.source_instance}"
        c = r.collection
        if c is None:
            lines.append(f"- {r.reference.name} ({source}; cannot be used: {r.reason})")
            continue
        description = f" - {c.description}" if c.description else ""
        ingested = c.last_ingested_at or "not ingested"
        lines.append(f"- {r.reference.name}{description} ({source} / {c.file_count} documents / {c.chunk_count} chunks / last ingested {ingested})")
    return "\n".join(lines)


@tool
def search_documents(query: str, collection: str = "", top_k: int = 0) -> str:
    """Search ingested internal documents for passages close in meaning to query.

    Use it when asked about facts, figures, records or document contents that may be in the
    ingested documents (internal rules, procedures, specifications, fees, costs, past results,
    etc.), before investigating the working directory or searching the web. It
    searches by meaning rather than exact keywords, so write query as a question or a short
    sentence with the key points.
    The knowledge is an index for quickly finding which files to read; it is not a source by
    itself. Results are excerpts (chunks) of the files, so use them to pick the files and pages,
    then open the original (pass the `@N` of the source to Read, or to a read/render skill for
    PDF/Office) and take the facts and figures from it (a main agent without Read delegates to
    explore).
    Exception: if the task is a "simple lookup" (only finding information, no analysis), answer
    from the excerpts when they already show the asked items, citing their sources, and open a
    file only for an asked item the excerpts do not show.
    Results that include "matched: image" match the look of the page (figures, tables, photos,
    etc.). To check the content, pass the `@N` to analyze_image if the source is an image file; for
    PDF/Office, render that page as an image with a render skill (pdf-tools / docx-render /
    pptx-render / excel-render) and then look at it with analyze_image. A location ending in
    "image (<part name>)" (e.g. "Slide 3 image (ppt/media/image2.png)") is a picture embedded in
    the Office file: the file is a ZIP, so take out that part and look at it with analyze_image,
    or render that slide/sheet with a render skill.
    Files that cannot be found from their text, such as photos and drawings, can be found by file
    and folder name with search_document_paths.
    Cite the original files as the sources (full path and page, etc.). Never cite the knowledge or
    a collection name (e.g. "common_data") as the source.
    It is read-only, so you may call it any time regardless of the plan.

    Args:
        query: What to look for (e.g. "inspection criteria for weld bead width").
        collection: The collection to search. If empty, all collections are searched.
            The names can be checked with list_document_collections.
        top_k: The maximum number of results. 0 means the default ([vector_db] top_k in config.ini).

    Returns:
        A text listing, most relevant first, "similarity, match (text / keyword / image), source
        (@N and file path, page, etc.), excerpt". Chunks of the same file and page are merged into
        one, with the best-matching chunk as the excerpt. The total length is truncated at a limit.
        If there are no matches, the feature is disabled, or an error occurs, a text saying so
        (errors as "Error: ...").
    """
    cfg = _enabled_config()
    if cfg is None:
        return _DISABLED_MESSAGE
    meter = embedding_usage.EmbeddingMeter()
    try:
        result = vector_search.search(cfg, query, collection=collection.strip() or None, top_k=top_k or None, meter=meter)
    except (vector_search.SearchError, EmbeddingError, StoreError, CatalogError) as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 - sqlite3.Error（再構築中の検索等）・応答の JSON の不正等
        return _unexpected_error("search", e)
    finally:
        _record_search_usage(cfg, meter)
    logger.info("search_documents: query=%r collection=%r hits=%d", query, collection, len(result.hits))
    notes = [f"({name} is not searched: {reason})" for name, reason in result.skipped]
    if not result.hits:
        return "\n".join(["No matching passages found. Search again with a rephrased query, or check the collection.", *notes])

    path_refs = _register_path_memory(sorted({h.source_path for h in result.hits}), description="sources of search_documents")
    ref_of = {path: ref for ref, path in path_refs.items()}
    budget = cfg.vector_db_max_result_chars
    lines = [f"{len(result.hits)} found."]
    for i, hit in enumerate(result.hits, start=1):
        ref = ref_of.get(hit.source_path)
        source = f"{ref} {hit.source_path}" if ref else hit.source_path
        locator = f" {hit.locator}" if hit.locator else ""
        matched = "/".join(_MATCHED_LABELS[m] for m in hit.matched) or _MATCHED_LABELS["text"]
        # キーワードだけの一致には類似度が無い（BM25 の値はコサイン類似度と比べられない）。
        score = f"similarity {hit.score:.2f}" if set(hit.matched) - {"keyword"} else "similarity -"
        # コレクション名は載せない。出典はファイルであり、名前を載せるとモデルが「出典: <コレクション名>」と
        # 書き写す（2026-10-31 本番）。
        header = f"[{i}] {score} (matched: {matched}) / {source}{locator}"
        if hit.other_chunks:
            header += f" ({hit.other_chunks} more chunks matched at the same location)"
        text = hit.text.strip()
        remaining = budget - len(header)
        if remaining <= 0:
            lines.append(f"(the remaining {len(result.hits) - i + 1} were omitted because of the character limit)")
            break
        if len(text) > remaining:
            text = text[:remaining] + "... (rest omitted; check the full text with Read)"
        lines.append(f"{header}\n{text}")
        budget -= len(header) + len(text)
    lines.extend(notes)
    return "\n\n".join(lines)


@tool
def search_document_paths(query: str, collection: str = "", top_k: int = 0) -> str:
    """Search ingested internal documents for files whose file/folder name (path) is close in meaning to query.

    Files that cannot be found from their text, such as photos, drawings and scanned images, often
    have their content described in the file or folder name. Use it when search_documents finds
    nothing, or when looking for the files themselves, such as "photos of ..." or "drawings of ...".
    It compares the path strings both by meaning and by keywords (exact match of model numbers,
    dates, etc.), so write query as a short phrase describing what you are looking for.
    Check the content of image files found by passing their `@N` to analyze_image.
    Read the content of PDF/Office files with Read (a main agent delegates to explore).
    It is read-only, so you may call it any time regardless of the plan.

    Args:
        query: What to look for (e.g. "exterior photos of the weld").
        collection: The collection to search. If empty, all collections are searched.
            The names can be checked with list_document_collections.
        top_k: The maximum number of results. 0 means the default ([vector_db] top_k in config.ini).

    Returns:
        A text listing, most relevant first, "similarity, match (meaning / keyword), collection,
        source (@N and file path), path relative to the source folder" (one entry per file). The
        total length is truncated at a limit. If there are no matches, the feature is disabled, or
        an error occurs, a text saying so (errors as "Error: ...").
    """
    cfg = _enabled_config()
    if cfg is None:
        return _DISABLED_MESSAGE
    meter = embedding_usage.EmbeddingMeter()
    try:
        result = vector_search.search_paths(cfg, query, collection=collection.strip() or None, top_k=top_k or None, meter=meter)
    except (vector_search.SearchError, EmbeddingError, StoreError, CatalogError) as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001 - sqlite3.Error（再構築中の検索等）・応答の JSON の不正等
        return _unexpected_error("search", e)
    finally:
        _record_search_usage(cfg, meter)
    logger.info("search_document_paths: query=%r collection=%r hits=%d", query, collection, len(result.hits))
    notes = [f"({name} is not searched: {reason})" for name, reason in result.skipped]
    if not result.hits:
        return "\n".join(
            [
                "No files with a matching name were found. Search again with a rephrased query, or "
                "search the document contents with search_documents.",
                *notes,
            ]
        )

    path_refs = _register_path_memory(sorted({h.source_path for h in result.hits}), description="sources of search_document_paths")
    ref_of = {path: ref for ref, path in path_refs.items()}
    budget = cfg.vector_db_max_result_chars
    lines = [f"{len(result.hits)} found."]
    for i, hit in enumerate(result.hits, start=1):
        ref = ref_of.get(hit.source_path)
        source = f"{ref} {hit.source_path}" if ref else hit.source_path
        matched = "/".join(_PATH_MATCHED_LABELS[m] for m in hit.matched)
        # キーワードだけの一致には類似度が無い（BM25 の値はコサイン類似度と比べられない）。
        score = f"similarity {hit.score:.2f}" if "path" in hit.matched else "similarity -"
        line = f"[{i}] {score} (matched: {matched}) / {source}\nrelative path: {hit.text}"
        if len(line) > budget:
            lines.append(f"(the remaining {len(result.hits) - i + 1} were omitted because of the character limit)")
            break
        lines.append(line)
        budget -= len(line)
    lines.extend(notes)
    return "\n\n".join(lines)
