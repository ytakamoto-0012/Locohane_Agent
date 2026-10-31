"""別のインスタンスのコレクションの参照（catalog.Reference）を、参照元の設定・コレクションに解決する。

参照は複製せずに参照元のインスタンスの保存先（[vector_db].dir・backend・Qdrant の接頭辞）を直接読む。
取り込み・更新は参照元のインスタンスで行い、参照する側は読むだけ。参照元の設定は
instances/<参照元>/config_overrides.json を重ねた load_config() で読み、ファイルの更新日時が
変わるまでプロセス内で使い回す。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import threading

from dotenv import dotenv_values

from ..config import DEFAULT_CONFIG_PATH, Config, load_config
from .catalog import Collection, Reference, catalog_exists, open_catalog

# instance.json の kind（admin/instances.py の KIND_SKILL_LAB と同じ値）。スキル研究室はナレッジを持たない。
_KIND_SKILL_LAB = "skill_lab"

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[tuple, Config]] = {}


@dataclass(frozen=True)
class ResolvedReference:
    """解決した参照。参照元を使えないときは config・collection が None で reason に理由が入る。"""

    reference: Reference
    config: Config | None = None
    collection: Collection | None = None
    reason: str | None = None


def own_instance_name(cfg: Config) -> str:
    return cfg.instance_dir.name


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def load_instance_config(cfg: Config, instance: str) -> Config:
    """インスタンス instance の設定（config.ini＋instances/<instance>/config_overrides.json）。

    参照元の Qdrant の API キーは参照元の .env（instances/<instance>/.env）の QDRANT_API_KEY があればそれを使う
    （環境変数はこのプロセスのもののため、それ以外の環境変数による上書きは参照する側のものが効く）。
    """
    root = cfg.instance_dir.parent
    overrides = root / instance / "config_overrides.json"
    env_file = root / instance / ".env"
    stamp = (_mtime(DEFAULT_CONFIG_PATH), _mtime(overrides), _mtime(env_file))
    key = (str(root), instance)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    loaded = load_config(config_path=DEFAULT_CONFIG_PATH, overrides_path=overrides, instance_name=instance)
    api_key = dotenv_values(env_file).get("QDRANT_API_KEY") if env_file.is_file() else None
    if api_key:
        loaded = replace(loaded, vector_db_qdrant_api_key=api_key)
    with _cache_lock:
        _cache[key] = (stamp, loaded)
    return loaded


def check_source_instance(cfg: Config, instance: str) -> str | None:
    """instance を参照元にできなければ理由（英語。LLM・ログ向け）を返す。"""
    if instance == own_instance_name(cfg):
        return "it is this instance itself"
    meta_path = cfg.instance_dir.parent / instance / "instance.json"
    if not meta_path.is_file():
        return f"instance {instance} does not exist"
    try:
        kind = json.loads(meta_path.read_text(encoding="utf-8")).get("kind")
    except (OSError, ValueError):
        return f"cannot read instance.json of instance {instance}"
    if kind == _KIND_SKILL_LAB:
        return f"instance {instance} is a skill lab instance"
    return None


def resolve(cfg: Config, reference: Reference) -> ResolvedReference:
    """参照を参照元の設定・コレクションに解決する（参照の参照はしない。参照元の参照は catalog の collection_refs にあり対象外）。"""
    reason = check_source_instance(cfg, reference.source_instance)
    if reason:
        return ResolvedReference(reference, reason=reason)
    try:
        source_cfg = load_instance_config(cfg, reference.source_instance)
    except Exception as e:  # noqa: BLE001 - 参照元の設定の誤りで参照する側の検索全体を止めない
        return ResolvedReference(reference, reason=f"cannot read the settings of instance {reference.source_instance}: {e}")
    if not source_cfg.vector_db_enabled:
        return ResolvedReference(reference, reason=f"[vector_db] enabled is false in instance {reference.source_instance}")
    collection = None
    if catalog_exists(source_cfg.vector_db_dir):
        with open_catalog(source_cfg.vector_db_dir) as catalog:
            collection = next((c for c in catalog.list_collections() if c.name == reference.source_collection), None)
    if collection is None:
        return ResolvedReference(
            reference, reason=f"collection {reference.source_collection} does not exist in instance {reference.source_instance}"
        )
    return ResolvedReference(reference, config=source_cfg, collection=collection)


def list_references(cfg: Config) -> list[Reference]:
    if not catalog_exists(cfg.vector_db_dir):
        return []
    with open_catalog(cfg.vector_db_dir) as catalog:
        return catalog.list_references()


__all__ = ["ResolvedReference", "check_source_instance", "list_references", "load_instance_config", "own_instance_name", "resolve"]
