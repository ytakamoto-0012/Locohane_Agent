"""設定ダッシュボードの「管理者設定」（全インスタンス共通の設定）。

インスタンス別の config_overrides.json では変えられない、管理ツール全体に効く設定を画面で変える。
値は <instances_dir>/admin_settings.json（{"<section>": {"<key>": "<値>"}}）に持ち、load_config() が
config.ini の後・インスタンス別の上書きの前に重ねる（src/config.py の ADMIN_SETTING_KEYS・_apply_admin_settings）。
環境変数が設定されていればそちらが優先する（画面には「環境変数で上書き中」と出して編集させない）。

- [vector_db] ingest_workers・ingest_processes・ingest_embed_requests: ナレッジの取り込みの並列数（全インスタンス・全コレクションの合計。
  admin/knowledge.py は取り込みを始めるたびに読み直す）。
- [vector_db] qdrant_max_retries: Qdrant へ接続できない・接続を切られたときに送り直す回数（-1 は無制限。取り込み・コピー・削除を
  始めるたびに読み直す。検索は最大1回）。qdrant_retry_interval_seconds はその間隔。
- [vector_db] scan_workers・max_files_per_collection・office_timeout_seconds・max_file_mb: 取り込み元の走査の並列数・
  1コレクションの最大ファイル数・Office の PDF 書き出しの上限秒数・1ファイルの上限サイズ（取り込みを始めるたびに読み直す）。
- [vector_db] ingest_temp_dir: 取り込みの間だけ使う一時ファイル（ページ画像・Office から書き出した PDF）を置くフォルダ
  （空ならシステムの一時フォルダ。取り込みを始めるたびに読み直す）。
- [admin] assistant_llm_instance・assistant_embedding_instance: 「AIに相談」が回答・検索に使うインスタンス
  （admin/assistant.py は相談のたびに読み直す）。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.config import ADMIN_SETTING_KEYS, PROJECT_ROOT, Config, admin_settings_path, load_config

from . import audit
from . import instances as inst
from .ini_catalog import parse_file
from admin.i18n import t as _t

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingSpec:
    section: str
    key: str
    env: str
    # positive_int / non_negative_int / retry_count（-1 は無制限）/ positive_float（0 より大きい数）/ instance（空はなし）/
    # dir（フォルダのパス。空はなし）
    kind: str


# 整数の項目の下限。
_INT_MINIMUMS = {"positive_int": 1, "non_negative_int": 0, "retry_count": -1}

SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("vector_db", "ingest_workers", "VECTOR_DB_INGEST_WORKERS", "positive_int"),
    SettingSpec("vector_db", "ingest_processes", "VECTOR_DB_INGEST_PROCESSES", "non_negative_int"),
    SettingSpec("vector_db", "ingest_embed_requests", "VECTOR_DB_INGEST_EMBED_REQUESTS", "non_negative_int"),
    SettingSpec("vector_db", "scan_workers", "VECTOR_DB_SCAN_WORKERS", "positive_int"),
    SettingSpec("vector_db", "max_files_per_collection", "VECTOR_DB_MAX_FILES_PER_COLLECTION", "positive_int"),
    SettingSpec("vector_db", "max_file_mb", "VECTOR_DB_MAX_FILE_MB", "positive_float"),
    SettingSpec("vector_db", "office_timeout_seconds", "VECTOR_DB_OFFICE_TIMEOUT_SECONDS", "positive_float"),
    SettingSpec("vector_db", "ingest_temp_dir", "VECTOR_DB_INGEST_TEMP_DIR", "dir"),
    SettingSpec("vector_db", "qdrant_max_retries", "VECTOR_DB_QDRANT_MAX_RETRIES", "retry_count"),
    SettingSpec("vector_db", "qdrant_retry_interval_seconds", "VECTOR_DB_QDRANT_RETRY_INTERVAL_SECONDS", "positive_float"),
    SettingSpec("admin", "assistant_llm_instance", "ADMIN_ASSISTANT_LLM_INSTANCE", "instance"),
    SettingSpec("admin", "assistant_embedding_instance", "ADMIN_ASSISTANT_EMBEDDING_INSTANCE", "instance"),
)
assert {(s.section, s.key) for s in SPECS} == set(ADMIN_SETTING_KEYS)
_SPEC_BY_KEY = {(s.section, s.key): s for s in SPECS}


def current_admin_config(config_ini_path: Path) -> Config:
    """管理者設定を重ねた、インスタンス別の上書きの無い設定（管理ツール全体に効く値を読む用）。"""
    return load_config(config_path=config_ini_path, overrides_path=Path(os.devnull))


def read_saved(path: Path) -> dict[str, dict[str, str]]:
    """画面で保存した値（読めない・形の違うファイルは空とみなす。src/config.py の _apply_admin_settings と同じく
    config.ini の値で動き、画面で保存すれば正しい内容で上書きされる）。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Admin settings: ignoring %s because it cannot be read: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {section: values for section, values in data.items() if isinstance(values, dict)}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".admin_settings_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _format_float(value: float) -> str:
    """整数なら "10.0" ではなく "10" と書く（config.ini の既定値と同じなら上書きとして持たないため）。"""
    return str(int(value)) if value.is_integer() else str(value)


def validate(values: dict[str, Any], instance_names: list[str]) -> dict[tuple[str, str], str]:
    """画面の値（"<section>.<key>" → 値）を検証して {(section, key): config.ini に書く形の文字列} にする。"""
    result: dict[tuple[str, str], str] = {}
    for spec in SPECS:
        name = f"{spec.section}.{spec.key}"
        if name not in values:
            continue
        raw = values[name]
        if spec.kind in _INT_MINIMUMS:
            try:
                number = int(str(raw).strip())
            except ValueError as exc:
                raise ValueError(_t("[{0}].{1} は整数にしてください。", spec.section, spec.key)) from exc
            minimum = _INT_MINIMUMS[spec.kind]
            if number < minimum:
                raise ValueError(_t("[{0}].{1} は {2} 以上にしてください。", spec.section, spec.key, minimum))
            result[(spec.section, spec.key)] = str(number)
        elif spec.kind == "positive_float":
            try:
                value = float(str(raw).strip())
            except ValueError as exc:
                raise ValueError(_t("[{0}].{1} は数値にしてください。", spec.section, spec.key)) from exc
            if not math.isfinite(value) or value <= 0:
                raise ValueError(_t("[{0}].{1} は 0 より大きい数にしてください。", spec.section, spec.key))
            result[(spec.section, spec.key)] = _format_float(value)
        elif spec.kind == "dir":
            text = str(raw or "").strip()
            if "${" in text:
                raise ValueError(_t("[{0}].{1} には ${{common_data_dir}}・${{instance}} を使えません（全インスタンス共通の設定のため）。", spec.section, spec.key))
            # 相対パスは config.ini と同じくプロジェクトルート基準（src/config.py の _as_optional_dir）。
            if text and (PROJECT_ROOT / text).exists() and not (PROJECT_ROOT / text).is_dir():
                raise ValueError(_t("[{0}].{1} の {2} はフォルダではありません。", spec.section, spec.key, text))
            result[(spec.section, spec.key)] = text
        else:
            text = str(raw or "").strip()
            if text and text not in instance_names:
                raise ValueError(_t("インスタンス {0} がありません。", text))
            result[(spec.section, spec.key)] = text
    return result


def _endpoints(cfg: Config) -> list[dict[str, str]]:
    return [{"base_url": e.base_url, "model": e.model} for e in cfg.main_endpoints]


def check_llm(cfg: Config, *, timeout: float = 5.0, transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    """インスタンスの [llm] main_url の各接続先へ GET /models を送り、つながるかを返す（モデルは動かさない）。"""
    results = []
    for e in cfg.main_endpoints:
        url = e.base_url.rstrip("/") + "/models"
        try:
            with httpx.Client(timeout=timeout, transport=transport) as client:
                res = client.get(url, headers={"Authorization": f"Bearer {e.api_key}"} if e.api_key else {})
            ok = res.status_code == 200
            detail = "" if ok else f"HTTP {res.status_code}"
        except httpx.HTTPError as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append({"base_url": e.base_url, "model": e.model, "ok": ok, "detail": detail})
    return results


class SettingsBody(BaseModel):
    values: dict[str, Any]


class CheckLlmBody(BaseModel):
    instance: str


def build_router(
    *,
    require_login: Callable,
    require_csrf: Callable,
    instances_root: Path,
    config_ini_path: Path,
    audit_log_path: Path,
    client_ip: Callable[[Request], str],
    config_loader: Callable[[str], Config],
    list_instances: Callable[[], list[dict[str, str]]],
    llm_transport: httpx.BaseTransport | None = None,
) -> APIRouter:
    """管理者設定の API の router を作る（llm_transport は接続確認のテスト用の差し替え口）。"""
    router = APIRouter()

    def _path() -> Path:
        return admin_settings_path(config_ini_path)

    def _ini_defaults() -> dict[tuple[str, str], str]:
        return {(k.section, k.key): k.default_value for k in parse_file(config_ini_path).keys if (k.section, k.key) in ADMIN_SETTING_KEYS}

    def _instances() -> list[dict[str, Any]]:
        result = []
        for item in list_instances():
            entry = dict(item)
            try:
                entry["llm"] = _endpoints(config_loader(item["name"]))
            except Exception as exc:  # noqa: BLE001 - 設定の読めないインスタンスも一覧には出す
                entry["llm"], entry["error"] = [], str(exc)
            result.append(entry)
        return result

    @router.get("/api/admin-settings")
    def get_settings(user: str = Depends(require_login)):
        saved = read_saved(_path())
        defaults = _ini_defaults()
        try:
            effective = current_admin_config(config_ini_path)
        except Exception as exc:  # noqa: BLE001 - 設定の誤りも画面に出す
            raise HTTPException(status_code=500, detail=_t("設定を読めません: {0}", exc)) from exc
        current = {
            ("vector_db", "ingest_workers"): str(effective.vector_db_ingest_workers),
            ("vector_db", "ingest_processes"): str(effective.vector_db_ingest_processes),
            ("vector_db", "ingest_embed_requests"): str(effective.vector_db_ingest_embed_requests),
            ("vector_db", "scan_workers"): str(effective.vector_db_scan_workers),
            ("vector_db", "max_files_per_collection"): str(effective.vector_db_max_files_per_collection),
            ("vector_db", "max_file_mb"): _format_float(effective.vector_db_max_file_mb),
            ("vector_db", "office_timeout_seconds"): _format_float(effective.vector_db_office_timeout_seconds),
            # 解決した絶対パスではなく、書いたとおりの値を出す（相対パスのまま保存し直せるように）。
            ("vector_db", "ingest_temp_dir"): os.environ.get(
                _SPEC_BY_KEY[("vector_db", "ingest_temp_dir")].env,
                saved.get("vector_db", {}).get("ingest_temp_dir", defaults.get(("vector_db", "ingest_temp_dir"), "")),
            ).strip(),
            ("vector_db", "qdrant_max_retries"): str(effective.vector_db_qdrant_max_retries),
            ("vector_db", "qdrant_retry_interval_seconds"): _format_float(effective.vector_db_qdrant_retry_interval_seconds),
            ("admin", "assistant_llm_instance"): effective.admin_assistant_llm_instance,
            ("admin", "assistant_embedding_instance"): effective.admin_assistant_embedding_instance,
        }
        settings = []
        for spec in SPECS:
            pair = (spec.section, spec.key)
            env_value = os.environ.get(spec.env)
            source = "env" if env_value is not None else "dashboard" if spec.key in saved.get(spec.section, {}) else "config.ini"
            settings.append(
                {
                    "section": spec.section,
                    "key": spec.key,
                    "env": spec.env,
                    "kind": spec.kind,
                    "value": current[pair],
                    "default": defaults.get(pair, ""),
                    "source": source,
                }
            )
        return {"settings": settings, "instances": _instances(), "cpu_count": os.cpu_count(), "path": str(_path())}

    @router.put("/api/admin-settings")
    def put_settings(body: SettingsBody, request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        try:
            values = validate(body.values, inst.list_instance_names(instances_root))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        defaults = _ini_defaults()
        saved = read_saved(_path())
        changes = []
        for (section, key), value in values.items():
            before = saved.get(section, {}).get(key, defaults.get((section, key), ""))
            if value == defaults.get((section, key), ""):
                saved.get(section, {}).pop(key, None)  # 既定値と同じなら上書きとして持たない
            else:
                saved.setdefault(section, {})[key] = value
            if before != value:
                changes.append({"section": section, "key": key, "before": before, "after": value})
        _write(_path(), {s: kv for s, kv in saved.items() if kv})
        audit.append(
            audit_log_path,
            {"instance": "-", "actor": user, "remote_addr": client_ip(request), "action": "admin_settings_save", "changes": changes},
        )
        logger.info("Admin settings saved by %s: %s", user, changes)
        return get_settings(user)

    @router.delete("/api/admin-settings")
    def reset_settings(request: Request, user: str = Depends(require_login), _c=Depends(require_csrf)):
        """画面で保存した値を消し、config.ini の値に戻す。"""
        _path().unlink(missing_ok=True)
        audit.append(audit_log_path, {"instance": "-", "actor": user, "remote_addr": client_ip(request), "action": "admin_settings_reset"})
        logger.info("Admin settings reset to config.ini by %s.", user)
        return get_settings(user)

    @router.post("/api/admin-settings/check-llm")
    def post_check_llm(body: CheckLlmBody, user: str = Depends(require_login), _c=Depends(require_csrf)):
        """インスタンスの [llm] main_url へ接続できるかを確かめる（「AIに相談」の回答に使う LLM を選ぶとき用）。"""
        if body.instance not in inst.list_instance_names(instances_root):
            raise HTTPException(status_code=404, detail=_t("インスタンス {0} がありません。", body.instance))
        try:
            cfg = config_loader(body.instance)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=_t("インスタンス {0} の設定を読めません: {1}", body.instance, exc)) from exc
        results = check_llm(cfg, transport=llm_transport)
        return {"instance": body.instance, "ok": any(r["ok"] for r in results), "endpoints": results}

    return router
