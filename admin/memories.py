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

一覧で選んだメモリー（複数可）から、同じ LLM にドラフトスキル（SKILL.md）を下書きさせ、指定したユーザーの
ドラフト置き場 [skill_creator] draft_dir/<ユーザー>/<スキル名>/ へ保存することもできる（既定のユーザーは
anonymous。skill-creator の scaffold_skill.py と同じ構成・_draft_meta.json で作る）。作ったドラフトは
そのユーザーの次のメッセージから使え、昇格はほかのドラフトと同じくスキル研究室で行う。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml
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


class DraftSkillBody(BaseModel):
    items: list[MemoryRef]
    # 作成先のユーザー（ドラフト置き場のユーザーフォルダ名へ整える）。空なら anonymous。
    owner: str | None = None
    # スキル名（空なら LLM に決めさせる）。
    skill_name: str | None = None
    # LLM への補足（どんなスキルにしたいか等。任意）。
    request: str | None = None


# 1回に選べるメモリーの数と、LLM へ渡すメモリー本文の合計文字数の上限（ローカル LLM の文脈長に収めるため）。
DRAFT_SKILL_MAX_MEMORIES = 20
DRAFT_SKILL_MAX_CHARS = 30000

_DRAFT_SKILL_SYSTEM = """You turn memory notes of a business AI assistant into a skill (SKILL.md) for that assistant.
The memories record facts, corrections and confirmed procedures learned in past conversations. Combine them into one
reusable skill that tells the assistant how to do that kind of work from the start.
A SKILL.md starts with frontmatter (name, description) and the body describes the procedure.
- name: {name_rule}
- description: the only trigger clue. Write concretely "what it does" and "for what kind of instructions it is used",
  on one line, 1024 characters or fewer.
- The body is 500 lines or fewer: purpose, procedure (numbered steps), and notes (what to avoid, how to handle exceptions).
  Keep every concrete fact, value, rule and reason in the memories that the procedure needs; do not invent features,
  commands, file paths or values that are not in the memories. Do not write tool names (run_script, etc.) in the body.
- Do not copy personal information (e-mail addresses, phone numbers, passwords, account numbers) into the skill.
- Write the description and the body in {language}.
Output only the full text of SKILL.md starting with the "---" line (no preamble, no code fences)."""

# スキル名（src/skills.py の _NAME_RE と同じ規則。文字数・区切りの連続は _validate で確かめる）。
_SKILL_NAME_RULE_GIVEN = "use exactly {name!r}."
_SKILL_NAME_RULE_FREE = (
    "choose a short name in English: lowercase letters, digits and single hyphens only (e.g. monthly-report-check), "
    "64 characters or fewer."
)


def _dashboard_editor(user: str) -> str:
    """設定ダッシュボードで作成・編集した人として frontmatter（author / updated_by）に残す名前。

    チャットのユーザー名と区別できるよう、ダッシュボードでの操作であることを添える。
    """
    return f"{user} (dashboard)"


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


def _draft_owner(owner: str | None) -> str:
    """ドラフトの作成先ユーザーを、ドラフト置き場のユーザーフォルダ名へ整える（空なら anonymous）。"""
    return skill_drafts.draft_owner_name((owner or "").strip() or None)


def _strip_fence(text: str) -> str:
    """LLM が付けがちな前後のコードフェンスを外す。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _official_skill_dir(cfg: Any, skill_name: str) -> Path | None:
    """そのインスタンスが走査する正式スキルに同じ名前があればそのフォルダ（同梱・共有・専用の置き場）。"""
    skills_dir = getattr(cfg, "skills_dir", None)
    roots = ([skills_dir] if skills_dir is not None else []) + list(getattr(cfg, "locohane_skills_dirs", None) or [])
    for root in roots:
        if (Path(root) / skill_name / "SKILL.md").is_file():
            return Path(root) / skill_name
    return None


def _memories_material(memories: list[mem_store.Memory]) -> str:
    """LLM へ渡すメモリーの一覧（所有者・種類・名前・説明・本文）。"""
    blocks = []
    for m in memories:
        blocks.append(
            f"## Memory: {m.name}\n- owner: {m.owner or 'common'}\n- type: {m.memory_type}\n"
            f"- description: {m.description}\n\n{m.content.strip()}"
        )
    return "\n\n".join(blocks)


def _build_skill_md(text: str, skill_name: str | None) -> tuple[str, str, str]:
    """LLM の出力から SKILL.md を組み立て直す。(名前, 説明, 全文) を返す。

    frontmatter は name / description だけにして書き直す（LLM の YAML の崩れ・余計なキーを残さない）。
    skill_name を指定していればその名前に揃える。形式に合わなければ ValueError。
    """
    from src.skills import _parse_frontmatter, strip_frontmatter

    text = _strip_fence(text)
    # 前置きの文（とその後のコードフェンス）が付いていたら、最初の "---" 行から使い、閉じのフェンスも外す。
    start = text.find("---")
    if start > 0:
        text = text[start:].rstrip()
        if text.endswith("```"):
            text = text[:-3]
    fm = _parse_frontmatter(text)
    if fm is None:
        raise ValueError("the response has no frontmatter (--- block at the top)")
    name = skill_name or str(fm.get("name") or "").strip()
    description = " ".join(str(fm.get("description") or "").split())
    body = strip_frontmatter(text)
    if not body:
        raise ValueError("the response has no body")
    frontmatter = yaml.safe_dump(
        {"name": name, "description": description}, allow_unicode=True, sort_keys=False, width=100000
    )
    return name, description, f"---\n{frontmatter}---\n\n{body}\n"


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
    user_candidates: Callable[[str], list[str]] = lambda name: [],
) -> APIRouter:
    """メモリーの API の router を作る。

    Args:
        llm_instance: 対象インスタンス名 → 翻訳に使う LLM のインスタンス名（翻訳のたびに呼ぶ）。
        model_factory: 設定 → チャットモデル（awaitable 可。テスト用の差し替え口。既定は src.llm.build_model）。
        user_candidates: 対象インスタンス名 → ログインユーザー名の一覧（ドラフトスキルの作成先の候補）。
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

    def _config(name: str) -> Config:
        try:
            inst.validate_name(name)
            inst.read_instance(instances_root, name)
        except inst.InstanceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            return config_loader(name)
        except Exception as exc:  # noqa: BLE001 - 設定の誤りは画面に出す
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _memory_root(name: str) -> Path:
        return _config(name).memory_dir

    def _draft_dir(cfg: Config) -> Path:
        draft_dir = getattr(cfg, "skill_draft_dir", None)
        if draft_dir is None:
            raise HTTPException(status_code=400, detail=_t("このインスタンスにはドラフトスキルの置き場（[skill_creator] draft_dir）がありません。"))
        return Path(draft_dir)

    def _llm_error(llm_name: str, exc: Exception) -> HTTPException:
        return HTTPException(
            status_code=502,
            detail=_t("LLM（インスタンス {0!r} の [llm] 接続先）から応答を得られませんでした: {1}", llm_name, exc)
            + _t("（「管理者設定」の「回答に使う LLM」で、LLM に接続できるインスタンスを選べます）"),
        )

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

    # /memories/{memory_name} より前に登録する（同じ形のパスとして取られないように）。
    @router.get("/api/instances/{name}/memories/draft-skill-owners")
    def draft_skill_owners(name: str, user: str = Depends(require_login)):
        """ドラフトスキルの作成先ユーザーの候補（anonymous・ログインユーザー・既存のドラフト／メモリーの所有者）。"""
        cfg = _config(name)
        draft_dir = _draft_dir(cfg)
        default = _draft_owner(None)
        owners: list[str] = [default]
        try:
            owners += [_draft_owner(u) for u in user_candidates(name)]
        except Exception as exc:  # noqa: BLE001 - .env が読めなくても既存の所有者からは選べるようにする
            logger.warning("Failed to list the users of %s: %s", name, exc)
        if draft_dir.is_dir():
            owners += sorted(p.name for p in draft_dir.iterdir() if p.is_dir() and not p.name.startswith("_"))
        owners += mem_store.list_owners(cfg.memory_dir)
        return {
            "owners": list(dict.fromkeys(owners)),
            "default": default,
            "draft_dir": str(draft_dir),
            "other_users_drafts": getattr(cfg, "skill_other_users_drafts", None),
        }

    @router.post("/api/instances/{name}/memories/draft-skill")
    async def create_draft_skill(
        name: str,
        body: DraftSkillBody,
        request: Request,
        user: str = Depends(require_login),
        _csrf: None = Depends(require_csrf),
    ):
        """選んだメモリーから LLM にドラフトスキルを下書きさせ、指定ユーザーのドラフト置き場へ保存する。"""
        from src.skills import _validate as validate_skill

        cfg = _config(name)
        root = cfg.memory_dir
        draft_dir = _draft_dir(cfg)
        targets = list(dict.fromkeys((item.name, _owner_scope(item.owner)) for item in body.items))
        if not targets:
            raise HTTPException(status_code=400, detail=_t("ドラフトスキルにするメモリーを選択してください。"))
        if len(targets) > DRAFT_SKILL_MAX_MEMORIES:
            raise HTTPException(
                status_code=400, detail=_t("一度に選べるメモリーは {0} 件までです（{1} 件選択されています）。", DRAFT_SKILL_MAX_MEMORIES, len(targets))
            )
        found = [_find(root, memory_name, owner) for memory_name, owner in targets]
        owner = _draft_owner(body.owner)
        skill_name = (body.skill_name or "").strip() or None

        def _check_name(candidate: str) -> Path:
            error = validate_skill(candidate, "-", candidate)
            if error:
                raise HTTPException(status_code=400, detail=_t("スキル名が不正です（{0}）: {1!r}", error, candidate))
            if _official_skill_dir(cfg, candidate) is not None:
                raise HTTPException(
                    status_code=409, detail=_t("正式スキルと同じ名前は使えません: {0}（別の名前を指定してください）", candidate)
                )
            target = draft_dir / owner / candidate
            if target.exists():
                raise HTTPException(status_code=409, detail=_t("同じ名前のドラフトスキルが既にあります: {0}/{1}", owner, candidate))
            return target

        if skill_name:
            _check_name(skill_name)  # LLM を呼ぶ前に分かる誤りは先に返す
        material = _memories_material(found)
        if len(material) > DRAFT_SKILL_MAX_CHARS:
            raise HTTPException(
                status_code=400,
                detail=_t("選択したメモリーの本文が長すぎます（{0} 文字。上限 {1} 文字）。選ぶメモリーを減らしてください。", len(material), DRAFT_SKILL_MAX_CHARS),
            )
        system = _DRAFT_SKILL_SYSTEM.format(
            name_rule=_SKILL_NAME_RULE_GIVEN.format(name=skill_name) if skill_name else _SKILL_NAME_RULE_FREE,
            language=TRANSLATE_LANGUAGES[current_lang()],
        )
        prompt = f"# Memories\n\n{material}"
        if (body.request or "").strip():
            prompt += f"\n\n# Note from the administrator\n{body.request.strip()}"
        llm_name = llm_instance(name) or name
        try:
            text = await _complete(config_loader(llm_name), system, prompt)
        except Exception as exc:  # noqa: BLE001 - 接続失敗等は画面に出す
            logger.warning("Draft skill generation failed (llm=%s): %s", llm_name, exc)
            raise _llm_error(llm_name, exc) from exc
        try:
            final_name, description, skill_md = _build_skill_md(text, skill_name)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=_t("LLM の応答を SKILL.md として読めませんでした: {0}", exc)) from exc
        target = _check_name(final_name)
        error = validate_skill(final_name, description, final_name)
        if error:
            raise HTTPException(status_code=502, detail=_t("LLM の応答を SKILL.md として読めませんでした: {0}", error))
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(skill_md, encoding="utf-8")
        (target / "references").mkdir()
        (target / "evals").mkdir()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        editor = _dashboard_editor(user)
        meta = {
            "kind": "new",
            "author": editor,
            "last_editor": editor,
            "thread_id": None,
            "created_at": now,
            "updated_at": now,
            "status": "draft",
            "tryouts": [],
            "source_memories": [{"name": m.name, "owner": m.owner} for m in found],
        }
        (target / skill_drafts.DRAFT_META_FILENAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        audit.append(
            audit_log_path,
            {
                "instance": name,
                "actor": user,
                "remote_addr": client_ip(request),
                "action": "memory_draft_skill",
                "draft": f"{owner}/{final_name}",
                "memories": [f"{m.owner or _COMMON_BACKUP_DIRNAME}/{m.name}" for m in found],
            },
        )
        # 作成先ユーザーが見られない個人用のメモリー（他のユーザーの private）を材料にしたか。
        private_others = [f"{m.owner}/{m.name}" for m in found if m.owner and m.owner != owner and m.visibility == "private"]
        return {
            "success": True,
            "owner": owner,
            "skill_name": final_name,
            "description": description,
            "path": str(target),
            "content": skill_md,
            "private_memories_of_others": private_others,
            "llm_instance": llm_name,
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
                root,
                body.name,
                body.description,
                body.type,
                body.content,
                owner=owner,
                visibility=body.visibility,
                author=_dashboard_editor(user),
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
                updated_by=_dashboard_editor(user),
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
