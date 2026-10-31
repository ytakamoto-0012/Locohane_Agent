"""管理アシスタント（ダッシュボード右上の「AIに相談」）。

管理者の困りごとに対して、どの画面・どの設定キーを変えればよいかを LLM が助言する。
AI は設定を**読むだけ**で、保存・起動停止などの操作は一切しない（操作は人が画面で行う）。

AI に渡す材料は次のとおりで、毎回組み立て直す。

- 画面ガイド（admin/ASSISTANT_GUIDE.md。毎回読み直すので、編集すれば再起動不要で反映される）
- 相談内容に近い config.ini のキー（[admin] assistant_max_keys 件まで。相談対象インスタンスの
  説明・既定値・現在値。機密値はマスクする）と、README_DETAIL.md・API リファレンスの段落
  （assistant_max_docs 件まで）。選び方は admin/assistant_index.py（埋め込み＋文字2-gram の簡易 RAG）
- 管理者が今開いている画面と、インスタンス一覧（種類・稼働状態）

config.ini の説明文・ドキュメントは全部で数十万字あり、ローカルの小さめのモデルにそのまま渡すと
入力が長すぎるため、検索で絞って渡す。

会話履歴はブラウザ側にだけあり、サーバーには保存しない。回答は NDJSON で
少しずつ返す（{"type":"delta","text":...} … {"type":"done","html":...,"keys":[...]}、
失敗時は {"type":"error","message":...}）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

from src.config import Config

from . import audit
from .assistant_index import AssistantIndex, EmbeddingProvider, Passage
from .ini_catalog import KeyInfo
from admin.i18n import current_lang, response_language_instruction
from admin.i18n import t as _t

logger = logging.getLogger(__name__)

GUIDE_PATH = Path(__file__).resolve().parent / "ASSISTANT_GUIDE.md"

# 1件の説明文・値として AI に渡す最大文字数（長い説明・JSON の値で入力が膨らまないように）。
_MAX_DESCRIPTION_CHARS = 1000
_MAX_VALUE_CHARS = 300
# 相談者・AI の1メッセージとして受け付ける最大文字数。
MAX_MESSAGE_CHARS = 4000
_MASKED_NOTE = "(hidden because it is confidential)"

# html=False: 回答中の生HTMLはエスケープする（innerHTML へ流し込むため）。
_md = MarkdownIt("commonmark", {"html": False}).enable("table")

# 回答・質問中の "[section] key" 形式の参照。
_KEY_REF_RE = re.compile(r"\[([A-Za-z_][\w.]*)\]\s*`?([A-Za-z_][\w]*)")
_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"


# ---------------------------------------------------------------------------
# 関係するキーの選び出し
# ---------------------------------------------------------------------------


def referenced_keys(text: str, keys: list[KeyInfo]) -> list[tuple[str, str]]:
    """本文中で "[section] key" の形で参照されている、実在するキー（出現順・重複なし）。"""
    known = {(k.section, k.key) for k in keys}
    result: list[tuple[str, str]] = []
    for section, key in _KEY_REF_RE.findall(text):
        pair = (section, key)
        if pair in known and pair not in result:
            result.append(pair)
    return result


def search_query(question: str, history: list[tuple[str, str]]) -> str:
    """検索に使う文。直前の相談者の発言も足す（「それはどこ？」のような続きの質問でも話題が残るように）。"""
    previous = [content for role, content in history if role == "user"]
    return f"{question}\n{previous[-1]}" if previous else question


def choose_context(
    index: AssistantIndex,
    keys: list[KeyInfo],
    question: str,
    *,
    history: list[tuple[str, str]],
    max_keys: int,
    max_docs: int,
    embedding: EmbeddingProvider | None,
) -> tuple[list[tuple[str, str]], list[Passage], str]:
    """AI に渡すキー・段落と検索方式（"embedding"/"bigram"）を返す。

    history はそれまでの (role, content)。会話中で "[section] key" と名指しされたキーは必ず先頭に含める。
    keys はその種類のインスタンスで意味を持つキー（これ以外は選ばない）。
    """
    allowed = {(k.section, k.key) for k in keys}
    named = referenced_keys("\n".join([*(c for _, c in history), question]), keys)
    result = index.search(
        search_query(question, history),
        max_keys=max_keys,
        max_docs=max_docs,
        allowed_keys=allowed,
        embedding=embedding,
    )
    chosen = named + [k for k in result.keys if k not in named]
    return chosen[: max(max_keys, len(named))], result.docs, result.mode


# ---------------------------------------------------------------------------
# プロンプトの組み立て
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "… (omitted)"


def _shown_value(section: str, key: str, value: str | None) -> str:
    if value is None:
        return "(none)"
    if audit.is_sensitive_key(key) or audit.is_sensitive_key(f"{section}_{key}"):
        return _MASKED_NOTE
    return _clip(value, _MAX_VALUE_CHARS) if value.strip() else "(empty)"


def format_key_block(key_views: list[dict[str, Any]]) -> str:
    """キーの一覧を AI 向けの文字列にする（key_views は管理ツールの config 画面と同じ形の dict）。"""
    if not key_views:
        return "(no keys that seem related to the question were found)"
    lines: list[str] = []
    for view in key_views:
        section, key = view["section"], view["key"]
        lines.append(f"### [{section}] {key}")
        if view.get("group_heading"):
            lines.append(f"- Group: {view['group_heading']}")
        lines.append(f"- Default (config.ini): {_shown_value(section, key, view.get('default'))}")
        if view.get("override") is not None:
            lines.append(f"- Current value (changed on the screen): {_shown_value(section, key, view['override'])}")
        else:
            lines.append("- Current value: same as the default")
        if view.get("choices"):
            shown = ", ".join(c if c else "(empty)" for c in view["choices"])
            lines.append(f"- Allowed values: {shown} (nothing else can be saved)")
        if view.get("env_override_active"):
            lines.append(f"- Note: overridden by the environment variable {view.get('env_name')}, so saving on the screen has no effect")
        if view.get("description"):
            lines.append("- Description: " + _clip(view["description"], _MAX_DESCRIPTION_CHARS).replace("\n", " "))
        lines.append("")
    return "\n".join(lines).rstrip()


def format_doc_block(docs: list[Passage]) -> str:
    """文書の段落を AI 向けの文字列にする。"""
    if not docs:
        return "(none)"
    return "\n\n".join(f"### {d.source} > {d.title}\n{d.text.strip()}" for d in docs)


def _format_view(view: dict[str, Any] | None) -> str:
    if not view:
        return "(unknown)"
    parts = []
    if view.get("tab"):
        parts.append(f"top tab \"{view['tab']}\"")
    if view.get("instance"):
        parts.append(f"details of instance {view['instance']!r}")
    if view.get("subview"):
        parts.append(f"sub tab \"{view['subview']}\"")
    return " > ".join(parts) or "(unknown)"


def build_system_prompt(
    *,
    guide: str,
    instance: str,
    instance_kind: str,
    instances: list[dict[str, str]],
    view: dict[str, Any] | None,
    key_block: str,
    doc_block: str = "(none)",
) -> str:
    instance_lines = "\n".join(
        f"- {i['name']} ({i.get('display_name') or i['name']}, kind: {i.get('kind')}, state: {i.get('state')})"
        for i in instances
    )
    return f"""You are an assistant that helps with operating the Locohane settings dashboard (admin tool).
Listen to the administrator's problem and advise which screen to open, where on it, and which setting to change and how.

# Rules
- You cannot change settings. The administrator performs the operations. Do not write as if you operated something yourself, such as "I changed it" or "I will restart it".
- Always write config keys in the form `[section] key` (e.g. `[llm] main_url`). Do not invent key names that are not in "Possibly related config keys", "Possibly related documentation" or the screen guide below.
- "Possibly related config keys" is only a part selected by searching with the question, not all keys of config.ini. Do not assert that "no such setting exists" just because it is not in the list. When no matching key is found, say so and suggest words to search for in the search box of the config.ini screen.
- Check with the descriptions that the meaning of a listed key matches the question (e.g. file "size" vs "count"), and do not answer with a different key that is merely similar.
- Show the steps in the order "which screen -> which tab -> which key -> what kind of value" as a short numbered list. Propose concrete values based on the current value and the description, and say so if a restart is needed for the change to take effect. For how a change takes effect (which process needs a restart, etc.), write only what the screen guide and the descriptions say; if they do not say it, do not assert it by guesswork.
- When the cause cannot be narrowed down to one, first show the order in which to check (monitor, logs, etc.), and if needed ask the administrator one question for additional information.
- Do not guess values marked as hidden because they are confidential.
- Keep answers concise. No preamble and no repeating the same content.

# Instance being discussed
{instance} (kind: {instance_kind})

# Screen the administrator has open now
{_format_view(view)}

# Instances
{instance_lines or "(none)"}

# Screen guide
{guide.strip()}

# Possibly related config keys (a part found by searching with the question; values are the effective values of the instance being discussed)
{key_block}

# Possibly related documentation (excerpts of README_DETAIL.md and the API reference found by searching with the question)
{doc_block}
"""


# ---------------------------------------------------------------------------
# 応答の整形
# ---------------------------------------------------------------------------


class ThinkFilter:
    """ストリームから <think>…</think> の部分を取り除く（タグがチャンクをまたいでもよい）。"""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False

    def feed(self, text: str) -> str:
        self._buffer += text
        out: list[str] = []
        while self._buffer:
            tag = _THINK_CLOSE if self._in_think else _THINK_OPEN
            pos = self._buffer.find(tag)
            if pos != -1:
                if not self._in_think:
                    out.append(self._buffer[:pos])
                self._buffer = self._buffer[pos + len(tag) :]
                self._in_think = not self._in_think
                continue
            # タグの途中で切れているかもしれない末尾は次のチャンクまで持ち越す。
            keep = next((n for n in range(len(tag) - 1, 0, -1) if self._buffer.endswith(tag[:n])), 0)
            if not self._in_think:
                out.append(self._buffer[: len(self._buffer) - keep])
            self._buffer = self._buffer[len(self._buffer) - keep :]
            break
        return "".join(out)

    def flush(self) -> str:
        rest, self._buffer = ("" if self._in_think else self._buffer), ""
        return rest


def _chunk_text(chunk: Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=MAX_MESSAGE_CHARS)


class ChatBody(BaseModel):
    instance: str
    messages: list[ChatMessage]
    # 管理者が今開いている画面（{"tab": ..., "instance": ..., "subview": ...}。表示名で構わない）。
    view: dict[str, str | None] | None = None


def _ndjson(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def build_router(
    *,
    require_login: Callable,
    require_csrf: Callable,
    admin_config: Config,
    catalog_keys: Callable[[str], list[KeyInfo]],
    config_view: Callable[[str], dict[str, Any]],
    list_instances: Callable[[], list[dict[str, str]]],
    instance_kind: Callable[[str], str],
    config_loader: Callable[[str], Config],
    index: AssistantIndex | dict[str, AssistantIndex],
    embedding_provider: Callable[[str, str], EmbeddingProvider | None] = lambda _target, _llm: None,
    model_factory: Callable[[Config], Any] | None = None,
    guide_path: Path = GUIDE_PATH,
    llm_instance_setting: Callable[[], str] | None = None,
) -> APIRouter:
    """管理アシスタント API の router を作る。

    Args:
        admin_config: 管理ツール自身の設定（[admin] assistant_* を読む）。
        catalog_keys: インスタンス名 → その種類のインスタンスで意味を持つ config.ini のキー一覧。
        config_view: インスタンス名 → config 画面と同じ形の {"keys": [...]}（現在値・環境変数の上書き有無）。
        list_instances: インスタンス一覧（name・display_name・kind・state の dict）。
        instance_kind: インスタンス名 → 種類（存在しなければ HTTPException(404)）。
        config_loader: インスタンス名 → そのインスタンスの実効設定（LLM 接続先の取得用）。
        index: config.ini・ドキュメントの索引（admin/assistant_index.py）。言語 → 索引の dict なら、
            ダッシュボードの表示言語の索引を使う（無ければ "ja"）。
        embedding_provider: (相談対象, 回答に使う LLM のインスタンス) → 検索に使う埋め込み（無ければ None）。
        model_factory: 設定 → チャットモデル（awaitable 可。テスト用の差し替え口。既定は src.llm.build_model）。
        guide_path: 画面ガイドの md（毎回読み直す）。
        llm_instance_setting: 回答に使う LLM のインスタンス（[admin] assistant_llm_instance）を返す関数。相談のたびに
            呼ぶ（設定ダッシュボードの「管理者設定」で変えたら再起動なしで効かせるため）。省略時は admin_config の値。
    """
    router = APIRouter()
    enabled = admin_config.admin_assistant_enabled
    max_keys = admin_config.admin_assistant_max_keys
    max_docs = admin_config.admin_assistant_max_docs
    max_history = admin_config.admin_assistant_max_history_messages

    def _fixed_llm_instance() -> str:
        return llm_instance_setting() if llm_instance_setting is not None else admin_config.admin_assistant_llm_instance

    def _require_enabled() -> None:
        if not enabled:
            raise HTTPException(status_code=404, detail=_t("管理アシスタントは無効です（[admin] assistant_enabled）。"))

    async def _build_model(config: Config):
        if model_factory is not None:
            model = model_factory(config)
        else:
            from src.llm import build_model

            model = build_model(config)
        if hasattr(model, "__await__"):
            model = await model
        return model

    @router.get("/api/assistant/status")
    def assistant_status(user: str = Depends(require_login)):
        return {"enabled": enabled, "llm_instance": _fixed_llm_instance() or None}

    @router.post("/api/assistant/chat")
    async def assistant_chat(body: ChatBody, user: str = Depends(require_login), _csrf: None = Depends(require_csrf)):
        _require_enabled()
        kind = instance_kind(body.instance)
        messages = [m for m in body.messages if m.content.strip()][-max_history:]
        if not messages or messages[-1].role != "user":
            raise HTTPException(status_code=400, detail=_t("最後のメッセージは相談内容（role=user）にしてください。"))
        llm_instance = _fixed_llm_instance() or body.instance
        if llm_instance != body.instance:
            instance_kind(llm_instance)

        question = messages[-1].content
        history = [(m.role, m.content) for m in messages[:-1]]
        keys = catalog_keys(body.instance)
        views = {(v["section"], v["key"]): v for v in config_view(body.instance)["keys"]}
        try:
            guide = guide_path.read_text(encoding="utf-8")
        except OSError:
            guide = "(could not load the screen guide)"
        instances = list_instances()
        lang_index = index.get(current_lang(), index["ja"]) if isinstance(index, dict) else index

        def build_context() -> tuple[str, list[Passage], str, int]:
            # 埋め込みの HTTP 呼び出し（初回は全段落の埋め込み）を含むため、スレッドで実行する。
            chosen, docs, mode = choose_context(
                lang_index,
                keys,
                question,
                history=history,
                max_keys=max_keys,
                max_docs=max_docs,
                embedding=embedding_provider(body.instance, llm_instance),
            )
            system = build_system_prompt(
                guide=guide,
                instance=body.instance,
                instance_kind=kind,
                instances=instances,
                view=body.view,
                key_block=format_key_block([views[k] for k in chosen if k in views]),
                doc_block=format_doc_block(docs),
            ) + response_language_instruction()
            return system, docs, mode, len(chosen)

        async def stream() -> AsyncIterator[bytes]:
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

            yield _ndjson({"type": "status", "text": _t("関係する設定・説明を探しています…")})
            try:
                system, docs, mode, n_keys = await asyncio.to_thread(build_context)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Admin assistant search failed.")
                yield _ndjson({"type": "error", "message": _t("関係する設定・説明を検索できませんでした: {0}", exc)})
                return
            logger.info(
                "Admin assistant: user=%s instance=%s llm=%s messages=%d keys=%d docs=%d search=%s",
                user, body.instance, llm_instance, len(messages), n_keys, len(docs), mode,
            )
            yield _ndjson({"type": "status", "text": _t("回答を考えています…")})
            lc_messages = [SystemMessage(content=system)] + [
                HumanMessage(content=m.content) if m.role == "user" else AIMessage(content=m.content) for m in messages
            ]
            think = ThinkFilter()
            answer: list[str] = []
            try:
                model = await _build_model(config_loader(llm_instance))
                async for chunk in model.astream(lc_messages):
                    text = think.feed(_chunk_text(chunk))
                    if text:
                        answer.append(text)
                        yield _ndjson({"type": "delta", "text": text})
                rest = think.flush()
                if rest:
                    answer.append(rest)
                    yield _ndjson({"type": "delta", "text": rest})
            except Exception as exc:  # noqa: BLE001 - 接続失敗等は画面に出して終える
                logger.warning("Admin assistant response failed (llm=%s): %s", llm_instance, exc)
                yield _ndjson(
                    {
                        "type": "error",
                        "message": _t("LLM（インスタンス {0!r} の [llm] 接続先）から応答を得られませんでした: {1}", llm_instance, exc)
                        + _t("（「管理者設定」の「回答に使う LLM」で、LLM に接続できるインスタンスを選べます）"),
                    }
                )
                return
            full = "".join(answer).strip()
            if not full:
                yield _ndjson({"type": "error", "message": _t("LLM の応答が空でした。")})
                return
            yield _ndjson(
                {
                    "type": "done",
                    "text": full,
                    "html": _md.render(full),
                    "keys": [{"section": s, "key": k} for s, k in referenced_keys(full, keys)],
                    # AI に渡した説明の出どころ（画面に「参考にした説明」として出す）と検索方式。
                    "sources": [{"source": d.source, "title": d.title} for d in docs],
                    "search": mode,
                }
            )

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    return router
