"""設定ダッシュボードの「メモリー」（インスタンスの永続メモリーの閲覧・編集）。

対象はそのインスタンスの実効設定（config.ini＋上書き）の [paths] memory_dir。メモリーはユーザー別
（memory_dir/users/<ユーザー>/）と共通（memory_dir 直下。ユーザー別になる前からあるもの）があり、
管理者は公開範囲（private / shared）に関係なくすべて見られる。読み書きは src/memory.py に任せ
（索引 MEMORY.md の再構築・公開範囲の規則も含む）、ここでは入力の検証と画面向けのエラーメッセージ、
書き換え前のバックアップ、変更履歴への記録だけを行う。

API では所有者をクエリ owner で指定する（省略・空なら共通）。書き換え・削除の前に、元のファイルを
instances/<name>/backups/memory/<所有者（共通は _common）>/<メモリー名>/ へ写す（[admin] backup_keep 世代まで）。
本体インスタンスは会話の開始（グラフの組み立て）ごとに索引を組み立て直すため、稼働中でも再起動せずに
次の会話から反映される。

編集画面の「AIで翻訳」は、説明と本文を LLM にダッシュボードの表示言語（日本語 / English。リクエストの
X-Locohane-Lang）へ訳させて返すだけで、保存はしない（管理者が確かめてから
保存する）。LLM は管理アシスタントと同じく [admin] assistant_llm_instance のインスタンス（空なら対象インスタンス
自身）の [llm] 接続先を使う。

設定画面の [memory] shared_criteria / private_criteria の「AIで清書」も同じ LLM で、管理者の下書きを
ダッシュボードの表示言語の箇条書きへ整えて返すだけ（保存は設定画面の「保存」で行う）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src import memory as mem_store
from src import skill_drafts
from src.config import Config

from . import audit, settings_files
from . import instances as inst
from .assistant import ThinkFilter
from admin.i18n import current_lang
from admin.i18n import t as _t

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# バックアップ先で共通のメモリーを表すフォルダ名（ユーザーのフォルダ名は _ で始まらない。skill_drafts.draft_owner_name）。
_COMMON_BACKUP_DIRNAME = "_common"


class MemoryCreateBody(BaseModel):
    name: str
    description: str
    type: str
    content: str
    owner: str | None = None
    visibility: str | None = None


class MemoryTranslateBody(BaseModel):
    description: str
    content: str


# ダッシュボードの表示言語 → LLM へ伝える言語名。
TRANSLATE_LANGUAGES = {"ja": "Japanese", "en": "English"}

_TRANSLATE_SYSTEM = (
    "You translate a memory note of an AI assistant into {language}. Output only the translated text, with no "
    "preface, explanation or code fence around it. Keep the Markdown structure (headings, lists, tables, bold, "
    "links) as it is. Do not translate code, commands, file paths, URLs, config keys, tool names, memory names "
    "(ASCII slugs) or numbers, and keep the labels **Why:** and **How to apply:** as they are. If the text is "
    "already in {language}, output it unchanged."
)


class CriteriaPolishBody(BaseModel):
    kind: str
    text: str


# [memory] shared_criteria / private_criteria の清書。結果はシステムプロンプトの「- "shared" for:」の下に
# そのまま字下げして入る（src/memory.py の render_visibility_block）。言語はダッシュボードの表示言語。
_POLISH_CRITERIA_SYSTEM = (
    "An AI assistant runs on an in-house system shared by the members of one organization. When it saves a memory "
    "it decides whether the memory is \"shared\" (every member can find and read it) or \"private\" (only the "
    "current user). The administrator wrote a rough note of which memories should be {kind}. Rewrite it in "
    "{language} as clear, concise criteria the assistant can apply to a memory's content. Keep every condition and "
    "example in the note and add none of your own. One condition per line, each starting with \"- \"; put examples "
    "in parentheses. Output only the criteria: no heading, no preface, no explanation, no code fence."
)


def _clean_criteria(text: str) -> str:
    """清書の結果から、LLM が付けがちなコードフェンス・空行・行末の空白を外す（改行は残す）。"""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    lines = [line for line in lines if line.strip() and not line.strip().startswith("```")]
    return "\n".join(lines)


class MemoryRef(BaseModel):
    name: str
    owner: str | None = None


class MemoriesDeleteBody(BaseModel):
    items: list[MemoryRef]


class MemoryEditBody(BaseModel):
    description: str
    type: str
    content: str
    visibility: str | None = None


def _validate_fields(description: str, memory_type: str, content: str, visibility: str | None) -> None:
    if memory_type not in mem_store.MEMORY_TYPES:
        raise HTTPException(status_code=400, detail=_t("種類が不正です: {0!r}", memory_type))
    if visibility is not None and visibility not in mem_store.VISIBILITIES:
        raise HTTPException(status_code=400, detail=_t("公開範囲が不正です: {0!r}", visibility))
    if not description.strip():
        raise HTTPException(status_code=400, detail=_t("説明を入力してください。"))
    if not content.strip():
        raise HTTPException(status_code=400, detail=_t("本文を入力してください。"))


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400, detail=_t("名前は半角英数字・-・_ の64文字以内で指定してください: {0!r}", name)
        )


def _owner_scope(owner: str | None) -> str | None:
    """クエリ・入力の所有者を、メモリーのフォルダ名へ整える（空なら共通 = None）。

    ログインユーザー名のまま渡されてもよいよう、本体と同じ規則（skill_drafts.draft_owner_name）で整える。
    """
    if owner is None or not owner.strip():
        return None
    return skill_drafts.draft_owner_name(owner.strip())


def _summary(m: mem_store.Memory) -> dict:
    return {
        "name": m.name,
        "description": m.description,
        "type": m.memory_type,
        "owner": m.owner,
        "visibility": m.visibility,
        "mtime": m.path.stat().st_mtime,
        "sources": len(m.sources),
    }


def build_router(
    *,
    require_login: Callable,
    require_csrf: Callable,
    instances_root: Path,
    audit_log_path: Path,
    client_ip: Callable[[Request], str],
    config_loader: Callable[[str], Config],
    backup_keep: int,
    llm_instance: Callable[[str], str] = lambda name: name,
    model_factory: Callable[[Config], Any] | None = None,
) -> APIRouter:
    """メモリーの API の router を作る。

    Args:
        llm_instance: 対象インスタンス名 → 翻訳に使う LLM のインスタンス名（翻訳のたびに呼ぶ）。
        model_factory: 設定 → チャットモデル（awaitable 可。テスト用の差し替え口。既定は src.llm.build_model）。
    """
    router = APIRouter()

    async def _translate(config: Config, language: str, text: str) -> str:
        return await _complete(config, _TRANSLATE_SYSTEM.format(language=language), text)

    async def _complete(config: Config, system: str, text: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        if model_factory is not None:
            model = model_factory(config)
        else:
            from src.llm import build_model

            model = build_model(config)
        if hasattr(model, "__await__"):
            model = await model
        reply = await model.ainvoke([SystemMessage(content=system), HumanMessage(content=text)])
        content = reply.content
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        think = ThinkFilter()
        return (think.feed(str(content or "")) + think.flush()).strip()

    def _memory_root(name: str) -> Path:
        try:
            inst.validate_name(name)
            inst.read_instance(instances_root, name)
        except inst.InstanceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            return config_loader(name).memory_dir
        except Exception as exc:  # noqa: BLE001 - 設定の誤りは画面に出す
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _find(root: Path, memory_name: str, owner: str | None) -> mem_store.Memory:
        _validate_name(memory_name)
        try:
            mem = mem_store.find_memory(root, memory_name, owner)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if mem is None:
            raise HTTPException(status_code=404, detail=_t("メモリーが見つかりません: {0}", memory_name))
        return mem

    def _backup(name: str, m: mem_store.Memory) -> None:
        backup_dir = inst.backups_dir(instances_root, name) / "memory" / (m.owner or _COMMON_BACKUP_DIRNAME) / m.name
        settings_files.backup_file(m.path, backup_dir, backup_keep, prefix=m.memory_type)

    def _audit(request: Request, user: str, name: str, action: str, memory_name: str, owner: str | None) -> None:
        audit.append(
            audit_log_path,
            {
                "instance": name,
                "actor": user,
                "remote_addr": client_ip(request),
                "action": action,
                "memory": memory_name,
                "memory_owner": owner,
            },
        )

    @router.get("/api/instances/{name}/memories")
    def list_memories(name: str, user: str = Depends(require_login)):
        root = _memory_root(name)
        return {
            "memory_dir": str(root),
            "types": list(mem_store.MEMORY_TYPES),
            "visibilities": list(mem_store.VISIBILITIES),
            "owners": mem_store.list_owners(root),
            "memories": [_summary(m) for m in mem_store.all_memories(root)],
        }

    @router.get("/api/instances/{name}/memories/{memory_name}")
    def get_memory(name: str, memory_name: str, owner: str | None = None, user: str = Depends(require_login)):
        m = _find(_memory_root(name), memory_name, _owner_scope(owner))
        return {
            **_summary(m),
            "content": m.content,
            "sources": [{"path": c.source.path, "state": c.state} for c in mem_store.check_sources(m)],
        }

    @router.post("/api/instances/{name}/memories/translate")
    async def translate_memory(
        name: str,
        body: MemoryTranslateBody,
        user: str = Depends(require_login),
        _csrf: None = Depends(require_csrf),
    ):
        """説明と本文をダッシュボードの表示言語へ訳して返す（保存はしない）。"""
        _memory_root(name)
        language = TRANSLATE_LANGUAGES[current_lang()]
        if not body.description.strip() and not body.content.strip():
            raise HTTPException(status_code=400, detail=_t("翻訳する説明・本文がありません。"))
        llm_name = llm_instance(name) or name
        try:
            config = config_loader(llm_name)
            description = await _translate(config, language, body.description) if body.description.strip() else ""
            content = await _translate(config, language, body.content) if body.content.strip() else ""
        except Exception as exc:  # noqa: BLE001 - 接続失敗等は画面に出す
            logger.warning("Memory translation failed (llm=%s): %s", llm_name, exc)
            raise HTTPException(
                status_code=502,
                detail=_t("LLM（インスタンス {0!r} の [llm] 接続先）から応答を得られませんでした: {1}", llm_name, exc)
                + _t("（「管理者設定」の「回答に使う LLM」で、LLM に接続できるインスタンスを選べます）"),
            ) from exc
        if (body.description.strip() and not description) or (body.content.strip() and not content):
            raise HTTPException(status_code=502, detail=_t("LLM の応答が空でした。"))
        # 説明は索引に1行で載るため、訳で改行が入っても1行に戻す。
        return {"description": " ".join(description.split()), "content": content, "language": current_lang(), "llm_instance": llm_name}

    @router.post("/api/instances/{name}/memories/polish-criteria")
    async def polish_criteria(
        name: str,
        body: CriteriaPolishBody,
        user: str = Depends(require_login),
        _csrf: None = Depends(require_csrf),
    ):
        """[memory] shared_criteria / private_criteria の下書きを、ダッシュボードの表示言語で清書して返す（保存はしない）。"""
        _memory_root(name)
        if body.kind not in mem_store.VISIBILITIES:
            raise HTTPException(status_code=400, detail=_t("公開範囲が不正です: {0!r}", body.kind))
        if not body.text.strip():
            raise HTTPException(status_code=400, detail=_t("清書する文章がありません。"))
        llm_name = llm_instance(name) or name
        try:
            system = _POLISH_CRITERIA_SYSTEM.format(kind=body.kind, language=TRANSLATE_LANGUAGES[current_lang()])
            text = await _complete(config_loader(llm_name), system, body.text)
        except Exception as exc:  # noqa: BLE001 - 接続失敗等は画面に出す
            logger.warning("Criteria polishing failed (llm=%s): %s", llm_name, exc)
            raise HTTPException(
                status_code=502,
                detail=_t("LLM（インスタンス {0!r} の [llm] 接続先）から応答を得られませんでした: {1}", llm_name, exc)
                + _t("（「管理者設定」の「回答に使う LLM」で、LLM に接続できるインスタンスを選べます）"),
            ) from exc
        polished = _clean_criteria(text)
        if not polished:
            raise HTTPException(status_code=502, detail=_t("LLM の応答が空でした。"))
        return {"text": polished, "language": current_lang(), "llm_instance": llm_name}

    @router.post("/api/instances/{name}/memories")
    def create_memory(
        name: str,
        body: MemoryCreateBody,
        request: Request,
        user: str = Depends(require_login),
        _csrf: None = Depends(require_csrf),
    ):
        root = _memory_root(name)
        owner = _owner_scope(body.owner)
        _validate_name(body.name)
        _validate_fields(body.description, body.type, body.content, body.visibility)
        if mem_store.find_memory(root, body.name, owner) is not None:
            raise HTTPException(status_code=409, detail=_t("同じ名前のメモリーが既にあります: {0}", body.name))
        try:
            mem_store.create_memory(
                root, body.name, body.description, body.type, body.content, owner=owner, visibility=body.visibility
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "memory_create", body.name, owner)
        return {"success": True, "owner": owner, "visibility": mem_store.find_memory(root, body.name, owner).visibility}

    @router.post("/api/instances/{name}/memories/delete")
    def delete_memories(
        name: str,
        body: MemoriesDeleteBody,
        request: Request,
        user: str = Depends(require_login),
        _csrf: None = Depends(require_csrf),
    ):
        """複数のメモリーをまとめて削除する。1件でも見つからなければ何も削除しない。"""
        root = _memory_root(name)
        targets = list(dict.fromkeys((item.name, _owner_scope(item.owner)) for item in body.items))
        if not targets:
            raise HTTPException(status_code=400, detail=_t("削除するメモリーを選択してください。"))
        found: list[mem_store.Memory] = []
        missing: list[str] = []
        for memory_name, owner in targets:
            _validate_name(memory_name)
            try:
                mem = mem_store.find_memory(root, memory_name, owner)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if mem is None:
                missing.append(f"{owner or _t('（共通）')} / {memory_name}")
            else:
                found.append(mem)
        if missing:
            raise HTTPException(status_code=404, detail=_t("メモリーが見つかりません: {0}", ", ".join(missing)))
        for mem in found:
            _backup(name, mem)
            mem_store.delete_memory(root, mem.name, mem.owner)
            _audit(request, user, name, "memory_delete", mem.name, mem.owner)
        return {"deleted": len(found)}

    @router.put("/api/instances/{name}/memories/{memory_name}")
    def edit_memory(
        name: str,
        memory_name: str,
        body: MemoryEditBody,
        request: Request,
        owner: str | None = None,
        user: str = Depends(require_login),
        _csrf: None = Depends(require_csrf),
    ):
        root = _memory_root(name)
        scope = _owner_scope(owner)
        m = _find(root, memory_name, scope)
        _validate_fields(body.description, body.type, body.content, body.visibility)
        _backup(name, m)
        try:
            mem_store.edit_memory(
                root,
                memory_name,
                description=body.description,
                memory_type=body.type,
                content=body.content,
                owner=scope,
                visibility=body.visibility,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _audit(request, user, name, "memory_update", memory_name, scope)
        return {"success": True, "visibility": mem_store.find_memory(root, memory_name, scope).visibility}

    @router.delete("/api/instances/{name}/memories/{memory_name}")
    def delete_memory(
        name: str,
        memory_name: str,
        request: Request,
        owner: str | None = None,
        user: str = Depends(require_login),
        _csrf: None = Depends(require_csrf),
    ):
        root = _memory_root(name)
        scope = _owner_scope(owner)
        m = _find(root, memory_name, scope)
        _backup(name, m)
        mem_store.delete_memory(root, memory_name, scope)
        _audit(request, user, name, "memory_delete", memory_name, scope)
        return {"success": True}

    return router
