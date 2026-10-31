"""管理アシスタントの検索（簡易 RAG）。

config.ini の全キーと、README_DETAIL.md・admin/API_REFERENCE.md の段落を索引にし、
相談内容に近いものを選んで AI に渡す（admin/assistant.py）。英語表示の相談には、英訳
（README_DETAIL.en.md・API_REFERENCE.en.md・admin/ini_catalog_en.py）で作った別の索引を使う。

類似度は2つを組み合わせる。

- 文字2-gram のコサイン類似度（src/path_memory.py の bigram_vector / cosine_similarity と同じ仕組みに、
  多くの段落に出る2-gramほど軽くする TF-IDF の重みを掛けたもの）。埋め込みサーバーが無くても使える。
- 埋め込み（ナレッジと同じ [vector_db] embedding_url。src/vector_db/embeddings.py）のコサイン類似度。
  「ナレッジのファイル容量」と「1ファイルあたりの最大サイズ（max_file_mb）」のように、
  言い回しが違って文字が重ならない相談にも当たる。

埋め込みが使えるときの点数は「埋め込みの類似度 + 0.2 × 2-gram の類似度」、使えないときは2-gram だけ。
（困りごと→正解キーの評価で、2-gram だけより埋め込みの方が大きく良く、2-gram を少し足すとさらに安定した。）

文書側の埋め込みは cache_dir に「段落の本文の SHA-1 → ベクター」で保存し、変わった段落だけを埋め込み直す
（接続先・モデル・prefix が変わればファイルごと別になる）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from src.config import Config
from src.path_memory import bigram_vector, cosine_similarity

from . import ini_catalog_en
from .ini_catalog import KeyInfo, parse_file

logger = logging.getLogger(__name__)

# 埋め込みと併用するときに足す2-gram の類似度の重み。
BIGRAM_WEIGHT_WITH_EMBEDDING = 0.2
# 文書の1段落の最大文字数（これより長い節は段落の区切りで分ける）。
MAX_CHUNK_CHARS = 1200

# README_DETAIL.md のうち、管理の相談には関係しない節（見出しの完全一致。配下の小見出しも除く）。
# 「設定リファレンス」の表は行ごとに config.ini のキーの索引へ取り込むため、節としては入れない。
README_EXCLUDED_HEADINGS = frozenset(
    {
        "ファイル構成",
        "開発環境",
        "設定リファレンス（config.ini）",
        "バージョン管理とリリース手順",
        "ライセンスと商用利用",
        "免責事項",
        "README_DETAIL.md更新時の注意",
    }
)
# 英語表示の索引（README_DETAIL.en.md）で同じ節を除く見出し。
README_EXCLUDED_HEADINGS_EN = frozenset(
    {
        "File layout",
        "Development environment",
        "Settings reference (config.ini)",
        "Versioning and release procedure",
        "License and commercial use",
        "Disclaimer",
        "Notes when updating this document",
    }
)

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# README_DETAIL.md「設定リファレンス」の表の行: | `[llm]` | `max_tokens` | 説明 | 環境変数 |
_REFERENCE_ROW_RE = re.compile(r"^\|\s*`\[([\w.]+)\]`\s*\|\s*((?:`\w+`\s*/?\s*)+)\|\s*(.+?)\s*\|", re.MULTILINE)

Embedder = Callable[[list[str], Literal["document", "query"]], list[list[float]]]


@dataclass(frozen=True)
class Passage:
    """索引の1件。config.ini のキー（kind="config"）か文書の段落（kind="doc"）。"""

    kind: Literal["config", "doc"]
    source: str
    title: str
    text: str
    key: tuple[str, str] | None = None


@dataclass(frozen=True)
class EmbeddingProvider:
    """埋め込みの計算方法と、キャッシュを分けるための識別子（接続先・モデル・prefix）。"""

    embed: Embedder
    cache_id: str
    label: str


@dataclass
class SearchResult:
    keys: list[tuple[str, str]]
    docs: list[Passage]
    # "embedding"（埋め込み＋2-gram）か "bigram"（2-gram のみ）
    mode: str


def provider_from_config(cfg: Config, instance: str) -> EmbeddingProvider | None:
    """インスタンスの [vector_db] embedding_url から埋め込みの計算方法を作る（未設定なら None）。

    [vector_db] enabled が false でも、embedding_url があれば使う（ナレッジを使わないインスタンスでも
    埋め込みサーバーだけ指定しておけば、管理アシスタントの検索に使える）。
    """
    if not cfg.vector_db_embedding_endpoints:
        return None
    from src.vector_db.embeddings import embed_texts, embedding_model

    cache_id = json.dumps(
        {
            "urls": [e.base_url for e in cfg.vector_db_embedding_endpoints],
            "model": embedding_model(cfg),
            "query_prefix": cfg.vector_db_query_prefix,
            "document_prefix": cfg.vector_db_document_prefix,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return EmbeddingProvider(
        # 相談の回答を待たせ、止める手段も無いため、埋め込みサーバーが埋まっていても空きを待たずに送る
        # （段落の埋め込みは kind="document" でも、取り込みと違って待たない）。
        embed=lambda texts, kind: embed_texts(cfg, texts, kind=kind, wait_when_busy=False),
        cache_id=cache_id,
        label=f"[vector_db] embedding_url of instance {instance}",
    )


# ---------------------------------------------------------------------------
# 索引にする文書の読み込み
# ---------------------------------------------------------------------------


def split_markdown(text: str, source: str, *, excluded_headings: frozenset[str] = frozenset()) -> list[Passage]:
    """Markdown を見出し（#〜####）ごとの段落に分ける（コードブロック内の # は見出しとみなさない）。

    1つの節が MAX_CHUNK_CHARS を超える場合は、空行の位置で分ける。title は「見出し > 小見出し」。
    """
    passages: list[Passage] = []
    titles: list[str] = []
    body: list[str] = []
    in_fence = False

    def flush() -> None:
        if any(t in excluded_headings for t in titles):
            return
        content = "\n".join(body).strip()
        if not content:
            return
        title = " > ".join(titles[1:] if len(titles) > 1 else titles)
        for part in _split_long(content):
            passages.append(Passage(kind="doc", source=source, title=title, text=part))

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            flush()
            body = []
            level = len(m.group(1))
            titles = titles[: level - 1] + [""] * max(0, level - 1 - len(titles)) + [m.group(2)]
            continue
        body.append(line)
    flush()
    return passages


def _split_long(content: str) -> list[str]:
    if len(content) <= MAX_CHUNK_CHARS:
        return [content]
    parts: list[str] = []
    current = ""
    for block in re.split(r"\n\s*\n", content):
        while len(block) > MAX_CHUNK_CHARS:
            if current:
                parts.append(current)
                current = ""
            parts.append(block[:MAX_CHUNK_CHARS])
            block = block[MAX_CHUNK_CHARS:]
        if current and len(current) + len(block) + 2 > MAX_CHUNK_CHARS:
            parts.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current.strip():
        parts.append(current)
    return parts


def reference_rows(readme_text: str) -> dict[tuple[str, str], str]:
    """README_DETAIL.md「設定リファレンス」の表から、キー → 説明（利用者向けの言い回し）。"""
    rows: dict[tuple[str, str], str] = {}
    for section, key_cells, description in _REFERENCE_ROW_RE.findall(readme_text):
        for key in re.findall(r"`(\w+)`", key_cells):
            rows[(section, key)] = description
    return rows


def english_keys(keys: list[KeyInfo]) -> list[KeyInfo]:
    """キーの説明・グループ見出しを英訳カタログ（admin/ini_catalog_en.py）の訳に置き換える（訳が無ければ原文）。"""
    return [
        replace(
            info,
            description=ini_catalog_en.DESCRIPTIONS.get(f"{info.section}.{info.key}", info.description),
            group_heading=ini_catalog_en.HEADINGS.get(info.group_heading or "", info.group_heading),
        )
        for info in keys
    ]


def config_passages(keys: list[KeyInfo], reference: dict[tuple[str, str], str]) -> list[Passage]:
    """config.ini のキーごとの索引。

    config.ini ではセクション冒頭の説明（例:「会話スレッド一覧…の有効/無効」）が最初のキーの説明に
    含まれ、ほかのキーには付かない。「会話の履歴を何日残す」のような相談で [thread_store] retention_days に
    当たるよう、最初のキーの説明の1行目をセクションの説明として全キーに付ける。
    """
    section_intro: dict[str, str] = {}
    for info in keys:
        section_intro.setdefault(info.section, info.description.split("\n", 1)[0])
    passages = []
    for info in keys:
        lines = [
            f"[{info.section}] {info.key}",
            info.group_heading or "",
            section_intro[info.section],
            reference.get((info.section, info.key), ""),
            info.description,
        ]
        passages.append(
            Passage(
                kind="config",
                source="config.ini",
                title=f"[{info.section}] {info.key}",
                text="\n".join(line for line in lines if line.strip()),
                key=(info.section, info.key),
            )
        )
    return passages


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------


def _normalized(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class _BigramIndex:
    """文字2-gram（TF-IDF 重み付き）のコサイン類似度。"""

    def __init__(self, texts: list[str]) -> None:
        grams = [bigram_vector(self._prepare(t)) for t in texts]
        df: Counter[str] = Counter()
        for g in grams:
            df.update(g.keys())
        n = len(texts)
        self._idf = {gram: math.log((n + 1) / (count + 1)) + 1 for gram, count in df.items()}
        self._default_idf = math.log(n + 1) + 1
        self._vectors = [self._weight(g) for g in grams]

    @staticmethod
    def _prepare(text: str) -> str:
        # "max_file_mb" を "max file mb" と同じに扱う（質問に "file" とだけ書かれても当たるように）。
        return text.replace("_", " ")

    def _weight(self, grams: Counter[str]) -> Counter[str]:
        return Counter({g: (1 + math.log(c)) * self._idf.get(g, self._default_idf) for g, c in grams.items()})

    def scores(self, query: str) -> list[float]:
        q = self._weight(bigram_vector(self._prepare(query)))
        return [cosine_similarity(q, v) for v in self._vectors]


class AssistantIndex:
    """config.ini・README_DETAIL.md・API_REFERENCE.md の索引（元ファイルが変われば作り直す）。

    language="en" のときは英語の質問に当たるよう、英訳の文書（readme_path・api_reference_path に
    README_DETAIL.en.md・API_REFERENCE.en.md を渡す）と、キーの説明の英訳カタログで索引を作る。
    """

    def __init__(
        self,
        *,
        config_ini_path: Path,
        readme_path: Path,
        api_reference_path: Path,
        cache_dir: Path | None = None,
        language: Literal["ja", "en"] = "ja",
    ) -> None:
        self._config_ini_path = config_ini_path
        self._readme_path = readme_path
        self._api_reference_path = api_reference_path
        self._cache_dir = cache_dir
        self._language = language
        self._lock = threading.Lock()
        self._signature: tuple | None = None
        self._passages: list[Passage] = []
        self._bigram: _BigramIndex | None = None
        # cache_id → 段落ごとの正規化済みベクター（self._passages と同じ順）
        self._embedded: dict[str, list[list[float]]] = {}

    # --- 構築 ---

    def _source_signature(self) -> tuple:
        sig = []
        for path in (self._config_ini_path, self._readme_path, self._api_reference_path):
            try:
                stat = path.stat()
                sig.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                sig.append((str(path), None, None))
        return tuple(sig)

    def _ensure_built(self) -> None:
        signature = self._source_signature()
        if signature == self._signature:
            return
        readme = self._read(self._readme_path)
        keys = parse_file(self._config_ini_path).keys
        english = self._language == "en"
        passages = config_passages(english_keys(keys) if english else keys, reference_rows(readme))
        passages += split_markdown(
            readme,
            self._readme_path.name,
            excluded_headings=README_EXCLUDED_HEADINGS_EN if english else README_EXCLUDED_HEADINGS,
        )
        passages += split_markdown(self._read(self._api_reference_path), self._api_reference_path.name)
        self._passages = passages
        self._bigram = _BigramIndex([f"{p.title}\n{p.text}" for p in passages])
        self._embedded = {}
        self._signature = signature
        logger.info("Built the admin assistant index (%s, %d entries).", self._language, len(passages))

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Cannot read %s for the admin assistant index.", path)
            return ""

    def _cache_path(self, cache_id: str) -> Path | None:
        if self._cache_dir is None:
            return None
        # 言語ごとに段落が違うため別ファイルにする（同じファイルだと、保存時に他方の段落の分を捨ててしまう）。
        suffix = "" if self._language == "ja" else f"_{self._language}"
        return self._cache_dir / f"embeddings_{_sha1(cache_id)[:16]}{suffix}.json"

    def _passage_embeddings(self, provider: EmbeddingProvider) -> list[list[float]]:
        if provider.cache_id in self._embedded:
            return self._embedded[provider.cache_id]
        cache_path = self._cache_path(provider.cache_id)
        cached: dict[str, list[float]] = {}
        if cache_path is not None:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = {}
        texts = [f"{p.title}\n{p.text}" for p in self._passages]
        hashes = [_sha1(t) for t in texts]
        missing = sorted({h: t for h, t in zip(hashes, texts) if h not in cached}.items())
        if missing:
            logger.info("Admin assistant: embedding %d paragraphs (%s).", len(missing), provider.label)
            vectors = provider.embed([t for _, t in missing], "document")
            cached.update({h: v for (h, _), v in zip(missing, vectors)})
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                # 今の索引に無い段落の分は捨てる（文書を書き換えるたびにファイルが膨らまないように）。
                live = set(hashes)
                cache_path.write_text(json.dumps({h: v for h, v in cached.items() if h in live}), encoding="utf-8")
        matrix = [_normalized(cached[h]) for h in hashes]
        self._embedded[provider.cache_id] = matrix
        return matrix

    # --- 検索 ---

    def search(
        self,
        query: str,
        *,
        max_keys: int,
        max_docs: int,
        allowed_keys: set[tuple[str, str]] | None = None,
        embedding: EmbeddingProvider | None = None,
    ) -> SearchResult:
        """相談内容に近いキー（最大 max_keys 件）と文書の段落（最大 max_docs 件）を近い順に返す。

        埋め込みに失敗した場合は2-gram だけで検索する（mode="bigram"）。
        """
        with self._lock:
            self._ensure_built()
            passages = self._passages
            assert self._bigram is not None
            scores = self._bigram.scores(query)
            mode = "bigram"
            if embedding is not None and passages:
                try:
                    matrix = self._passage_embeddings(embedding)
                    q = _normalized(embedding.embed([query], "query")[0])
                    scores = [_dot(q, v) + BIGRAM_WEIGHT_WITH_EMBEDDING * s for v, s in zip(matrix, scores)]
                    mode = "embedding"
                except Exception as exc:  # noqa: BLE001 - 埋め込みが使えなくても2-gram で答える
                    logger.warning("Admin assistant: embeddings unavailable, searching with character 2-grams only (%s): %s", embedding.label, exc)
        order = sorted(range(len(passages)), key=lambda i: -scores[i])
        keys: list[tuple[str, str]] = []
        docs: list[Passage] = []
        for i in order:
            if scores[i] <= 0:
                break
            p = passages[i]
            if p.kind == "config":
                if len(keys) < max_keys and (allowed_keys is None or p.key in allowed_keys):
                    keys.append(p.key)  # type: ignore[arg-type]
            elif len(docs) < max_docs:
                docs.append(p)
            if len(keys) >= max_keys and len(docs) >= max_docs:
                break
        return SearchResult(keys=keys, docs=docs, mode=mode)
