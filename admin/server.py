"""設定ダッシュボード（管理ツール）のFastAPIエントリーポイント。

`python -m admin.server` で起動する（admin.bat 参照）。Locohane本体
（app.py）とは別プロセス・別ポートで動く。

起動時にやること:
1. [admin]セクション（管理ツール自身の設定）を config.ini から読む。
2. プロジェクト直下 .env の ADMIN_USERS を読む（空なら起動を拒否する）。
3. [admin].ssl_certfile / ssl_keyfile があれば HTTPS で待ち受ける（本体インスタンスも同じ証明書で HTTPS 化する）。
4. instances/default/ が無ければ 127.0.0.1:8000 で作る。
5. autostart=true の全インスタンスを子プロセスとして起動する。
"""

from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config import (
    ADDITIVE_OVERRIDE_KEYS,
    ADMIN_SETTING_KEYS,
    PROJECT_ROOT,
    is_instance_overridable,
    load_config,
    merge_additive_override,
    resolve_instances_root,
    resolve_ssl_files,
)
from src.version import __version__

from . import api_docs, assistant, assistant_index, audit, auth, env_files, env_overrides, monitor, overrides, settings_files, supervisor
from . import ini_catalog_en
from . import instances as inst
from . import admin_settings as admin_settings_api
from . import data_backup as data_backup_api
from . import knowledge as knowledge_api
from .skill_lab import api as skill_lab_api
from .skill_lab import sources as skill_lab_sources
from .ini_catalog import parse_file as parse_ini_file
from admin import i18n as admin_i18n
from admin.i18n import t as _t

# コンソールのコードページで表せない文字（英語版 Windows の cp437 で日本語のパス等）を
# ログに出しても UnicodeEncodeError にしない（admin.bat 経由なら PYTHONUTF8=1 で UTF-8）。
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="backslashreplace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

CONFIG_INI_PATH = PROJECT_ROOT / "config.ini"
# [admin].instances_dir（環境変数 INSTANCES_DIR）で変更可能。
INSTANCES_ROOT = resolve_instances_root(CONFIG_INI_PATH)
PROJECT_ENV_PATH = PROJECT_ROOT / ".env"
AUDIT_LOG_PATH = INSTANCES_ROOT / "admin_changes.log"
# 管理ツール自身の動作ログ（admin_*.log。ナレッジの取り込み・削除の経過・エラー等）。
ADMIN_LOG_DIR = INSTANCES_ROOT / "_admin_logs"
SETTINGS_DIR = PROJECT_ROOT / "public" / "settings"
SETTINGS_BACKUP_DIR = INSTANCES_ROOT / "_settings_backups"
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_PY_PATH = PROJECT_ROOT / "src" / "config.py"

# [admin] セクションは、インスタンス別 config_overrides.json の上書き対象外
# （overrides.py の _EXEMPT_SECTIONS）。plain load_config() は安全にこの目的で使える。
_cfg = load_config()

_admin_users = auth.load_admin_users(PROJECT_ENV_PATH)
if not _admin_users:
    raise RuntimeError(
        "ADMIN_USERS is not set. Set "
        'ADMIN_USERS=[["username","password"]] in .env at the project root, then restart the admin tool'
        " (see .env.example)."
    )

_sessions = auth.SessionStore(timeout_minutes=_cfg.admin_session_timeout_minutes)
_throttle = auth.LoginThrottle()
# [admin].ssl_certfile / ssl_keyfile が両方あれば、管理ツールと、ここから起動する
# 本体インスタンスの両方を HTTPS で待ち受ける（同じ証明書を共有する）。
SSL_FILES = resolve_ssl_files(CONFIG_INI_PATH)
_URL_SCHEME = "https" if SSL_FILES else "http"
_supervisor = supervisor.Supervisor(INSTANCES_ROOT, CONFIG_INI_PATH, ssl_files=SSL_FILES)
_env_var_mapping = env_overrides.build_mapping_from_file(CONFIG_PY_PATH)

inst.ensure_default_instance(INSTANCES_ROOT)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    for name in inst.list_instance_names(INSTANCES_ROOT):
        meta = inst.read_instance(INSTANCES_ROOT, name)
        if not meta.autostart:
            continue
        status = _supervisor.status(name)
        if status.state == supervisor.InstanceState.STOPPED:
            try:
                _supervisor.start(name)
                logger.info("autostart: started instance %r.", name)
            except supervisor.SupervisorError as exc:
                logger.warning("autostart: failed to start instance %r: %s", name, exc)
    _knowledge_router.ingest_scheduler.start()
    _data_backup_router.backup_scheduler.start()
    # 設定画面のツール名の候補は別プロセスで求めるため、最初に開くときに待たないよう先に求め始める。
    skill_lab_sources.prefetch_main_agent_tool_names()
    yield
    _data_backup_router.backup_scheduler.stop()
    _knowledge_router.ingest_scheduler.stop()
    if _cfg.admin_stop_apps_on_exit:
        _supervisor.stop_all()


app = FastAPI(title="Locohane Settings Dashboard", version=__version__, lifespan=_lifespan)
# X-Locohane-Lang ヘッダー（admin/static/admin.js の api()）でエラーメッセージ等の言語を決める。
app.add_middleware(admin_i18n.LanguageMiddleware)


@app.get("/ui-config.js", include_in_schema=False)
async def ui_config_js() -> Response:
    """表示言語の既定値（[ui].default_language）を返すスクリプト。

    admin/static/i18n.js が読み込み時に同期的に使うため、JSON ではなく script として返す
    （ログイン画面でも使うため認証なし）。
    """
    body = f"window.LOCOHANE_DEFAULT_LANGUAGE = {json.dumps(_cfg.ui_default_language)};\n"
    return Response(content=body, media_type="application/javascript")


# ---------------------------------------------------------------------------
# 認証・CSRF
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE_NAME,
        token,
        httponly=True,
        # HTTPS 時は Secure を付け、HTTP の経路へセッションクッキーが流れないようにする。
        secure=SSL_FILES is not None,
        samesite="strict",
        max_age=_cfg.admin_session_timeout_minutes * 60,
    )


def require_login(
    request: Request, response: Response, locohane_admin_session: str | None = Cookie(default=None)
) -> str:
    username = _sessions.validate(locohane_admin_session)
    if username is None:
        raise HTTPException(status_code=401, detail=_t("ログインが必要です。"))
    # サーバー側のスライディングタイムアウト（SessionStore.validate）に合わせて
    # Cookie の max_age も毎回延ばす（2026-09-29 レビューで発見：ログイン時に
    # 1回設定するだけだと、操作中でもログインから一定時間でCookieが失効していた）。
    _set_session_cookie(response, locohane_admin_session)
    return username


def require_csrf(request: Request) -> None:
    if not auth.has_csrf_header(request.headers):
        raise HTTPException(status_code=403, detail=_t("CSRFヘッダ（X-Locohane-Admin: 1）がありません。"))


def _require_instance(name: str) -> inst.InstanceMeta:
    try:
        inst.validate_name(name)
        return inst.read_instance(INSTANCES_ROOT, name)
    except inst.InstanceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    ip = _client_ip(request)
    if _throttle.is_locked_out(ip):
        raise HTTPException(status_code=429, detail=_t("ログイン試行回数が上限を超えました。しばらく待ってから再試行してください。"))
    if not auth.verify_password(_admin_users, body.username, body.password):
        _throttle.record_failure(ip)
        raise HTTPException(status_code=401, detail=_t("ユーザー名またはパスワードが違います。"))
    _throttle.record_success(ip)
    token = _sessions.create(body.username)
    _set_session_cookie(response, token)
    return {"username": body.username, "version": __version__}


@app.post("/api/logout")
def logout(response: Response, locohane_admin_session: str | None = Cookie(default=None)):
    if locohane_admin_session:
        _sessions.revoke(locohane_admin_session)
    response.delete_cookie(auth.SESSION_COOKIE_NAME)
    return {"success": True}


@app.get("/api/me")
def me(user: str = Depends(require_login)):
    return {"username": user, "version": __version__}


# ---------------------------------------------------------------------------
# インスタンス管理
# ---------------------------------------------------------------------------


def _instance_summary(name: str) -> dict[str, Any]:
    meta = inst.read_instance(INSTANCES_ROOT, name)
    status = _supervisor.status(name)
    return {
        "name": name,
        "display_name": meta.display_name,
        "app_host": meta.app_host,
        "app_port": meta.app_port,
        "autostart": meta.autostart,
        "headless": meta.headless,
        "watch": meta.watch,
        "state": status.state.value,
        "pid": status.pid,
        "exit_code": status.exit_code,
        # 「開く」リンク用。app_host が 0.0.0.0（全インターフェースで待受）だと
        # ブラウザから http://0.0.0.0:port は開けないため、ホストは 127.0.0.1 固定。
        "url": f"{_URL_SCHEME}://127.0.0.1:{meta.app_port}",
        "is_default": name == inst.DEFAULT_INSTANCE_NAME,
        "kind": meta.kind,
    }


@app.get("/api/instances")
def list_instances(user: str = Depends(require_login)):
    return {"instances": [_instance_summary(name) for name in inst.list_instance_names(INSTANCES_ROOT)]}


class CreateInstanceBody(BaseModel):
    name: str
    display_name: str | None = None
    app_host: str = "127.0.0.1"
    app_port: int | None = None
    autostart: bool = False
    headless: bool = True
    watch: bool = False
    copy_from: str | None = None
    # app（Locohane 本体）/ skill_lab（スキル研究室。チャット画面・ポートを持たない）
    kind: str = inst.KIND_APP
    # copy_from のインスタンス専用の拡張ディレクトリ（昇格済みのスキル・サブエージェント）も写すか
    copy_extensions: bool = False


@app.post("/api/instances")
def create_instance(
    body: CreateInstanceBody, request: Request, user: str = Depends(require_login), _csrf: None = Depends(require_csrf)
):
    try:
        meta = inst.create_instance(
            INSTANCES_ROOT,
            CONFIG_INI_PATH,
            name=body.name,
            display_name=body.display_name,
            app_host=body.app_host,
            app_port=body.app_port,
            autostart=body.autostart,
            headless=body.headless,
            watch=body.watch,
            copy_from=body.copy_from,
            kind=body.kind,
            copy_extensions=body.copy_extensions,
        )
    except inst.InstanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.append(
        AUDIT_LOG_PATH,
        {
            "instance": meta.name,
            "actor": user,
            "remote_addr": _client_ip(request),
            "action": "instance_create",
            "copy_from": body.copy_from,
            "kind": body.kind,
            "copy_extensions": body.copy_extensions,
        },
    )
    return _instance_summary(meta.name)


class UpdateInstanceBody(BaseModel):
    display_name: str | None = None
    app_host: str | None = None
    app_port: int | None = None
    autostart: bool | None = None
    headless: bool | None = None
    watch: bool | None = None


@app.put("/api/instances/{name}")
def update_instance(
    name: str,
    body: UpdateInstanceBody,
    request: Request,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    _require_instance(name)
    try:
        inst.update_instance_meta(
            INSTANCES_ROOT,
            CONFIG_INI_PATH,
            name=name,
            display_name=body.display_name,
            app_host=body.app_host,
            app_port=body.app_port,
            autostart=body.autostart,
            headless=body.headless,
            watch=body.watch,
        )
    except inst.InstanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.append(
        AUDIT_LOG_PATH,
        {"instance": name, "actor": user, "remote_addr": _client_ip(request), "action": "instance_update"},
    )
    return _instance_summary(name)


@app.get("/api/instances/{name}/data-paths")
def get_instance_data_paths(name: str, user: str = Depends(require_login)):
    """インスタンス削除時に一緒に削除できる永続データの候補一覧。"""
    _require_instance(name)
    try:
        entries = inst.list_data_paths(INSTANCES_ROOT, CONFIG_INI_PATH, name)
    except inst.InstanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "entries": [e.to_json() for e in entries],
        # インスタンス専用の拡張ディレクトリ（昇格済みのスキル・サブエージェント）。削除ダイアログで警告する。
        "extensions": inst.instance_extensions_summary(INSTANCES_ROOT, CONFIG_INI_PATH, name),
    }


class DeleteInstanceBody(BaseModel):
    # 一緒に削除する永続データのキー（GET /api/instances/{name}/data-paths の key）。
    # 省略・空ならデータは削除しない。
    delete_data: list[str] = []


@app.delete("/api/instances/{name}")
def delete_instance(
    name: str,
    request: Request,
    body: DeleteInstanceBody | None = None,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    _require_instance(name)
    status = _supervisor.status(name)
    if status.state in (supervisor.InstanceState.RUNNING, supervisor.InstanceState.EXTERNAL):
        raise HTTPException(status_code=409, detail=_t("稼働中のインスタンスは削除できません。先に停止してください。"))
    delete_data = body.delete_data if body else []
    try:
        deleted = inst.delete_instance(INSTANCES_ROOT, name, CONFIG_INI_PATH, delete_data)
    except inst.InstanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.append(
        AUDIT_LOG_PATH,
        {
            "instance": name,
            "actor": user,
            "remote_addr": _client_ip(request),
            "action": "instance_delete",
            "deleted_data": [str(p) for p in deleted],
        },
    )
    return {"success": True, "deleted_data": [str(p) for p in deleted]}


@app.post("/api/instances/{name}/start")
def start_instance(
    name: str, request: Request, user: str = Depends(require_login), _csrf: None = Depends(require_csrf)
):
    _require_instance(name)
    try:
        status = _supervisor.start(name)
    except supervisor.SupervisorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.append(
        AUDIT_LOG_PATH, {"instance": name, "actor": user, "remote_addr": _client_ip(request), "action": "app_start"}
    )
    return {"state": status.state.value, "pid": status.pid}


@app.post("/api/instances/{name}/stop")
def stop_instance(
    name: str, request: Request, user: str = Depends(require_login), _csrf: None = Depends(require_csrf)
):
    _require_instance(name)
    _supervisor.stop(name)
    audit.append(
        AUDIT_LOG_PATH, {"instance": name, "actor": user, "remote_addr": _client_ip(request), "action": "app_stop"}
    )
    return {"state": _supervisor.status(name).state.value}


@app.post("/api/instances/{name}/restart")
def restart_instance(
    name: str, request: Request, user: str = Depends(require_login), _csrf: None = Depends(require_csrf)
):
    _require_instance(name)
    try:
        status = _supervisor.restart(name)
    except supervisor.SupervisorError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit.append(
        AUDIT_LOG_PATH, {"instance": name, "actor": user, "remote_addr": _client_ip(request), "action": "app_restart"}
    )
    return {"state": status.state.value, "pid": status.pid}


# ---------------------------------------------------------------------------
# config.ini（キー単位の閲覧・変更）
# ---------------------------------------------------------------------------


# スキル調整ワーカーの設定画面の先頭に出す説明（ワーカーに出すキーは inst.config_key_applies() 参照）。
_SKILL_LAB_SCOPE_NOTE = (
    "スキル調整ワーカーの設定は、LLM の接続先（[llm] main_url・sub_url・routing_strategy）、"
    "ループの上限（[skill_lab_worker]）、ワーカー自身のログ（[log]）だけです。"
    "評価・AI 修正・judge・下書きは、研究テーマの対象インスタンスの設定をそのまま使います。"
)


def _localize_schema(schema: Any) -> Any:
    """リスト編集用スキーマ（admin/ini_catalog.py の _KEY_SCHEMAS）の表示ラベル・入力例を表示言語に訳す。"""
    if isinstance(schema, list):
        return [_localize_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return {
        k: _t(v) if k in ("label", "placeholder", "group_placeholder") and isinstance(v, str) else _localize_schema(v)
        for k, v in schema.items()
    }


def _config_key_view(name: str, kind: str) -> dict[str, Any]:
    catalog = parse_ini_file(CONFIG_INI_PATH)
    ov_path = inst.overrides_path(INSTANCES_ROOT, name)
    ov = overrides.read(ov_path)
    # 起動時の実際の優先度（app.py 参照）と同じ順で合成する:
    # インスタンス .env（override=True）> OS環境変数 > プロジェクト直下 .env。
    # 管理ツール自身の os.environ だけを見ると、.env 由来の上書きにバッジが
    # 付かず「保存したのに反映されない」原因が分からなかった。
    effective_env = {
        **env_files.read_values(PROJECT_ENV_PATH),
        **os.environ,
        **env_files.read_values(inst.env_path(INSTANCES_ROOT, name)),
    }
    active_env = env_overrides.active_overrides(_env_var_mapping, effective_env)
    keys = []
    # 英語表示では説明・見出しを英訳カタログ（admin/ini_catalog_en.py）から返す（日本語の正本は config.ini のコメント）。
    english = admin_i18n.current_lang() == "en"
    for info in catalog.keys:
        override_value = ov.get(info.section, {}).get(info.key)
        # 全インスタンス共通の設定（管理者設定）へ移したキーに、以前インスタンス別に保存した値が残っていれば、
        # 効かないことを示して消せるように出す（[admin] は従来からインスタンス別に保存できないため除く）。
        ignored = override_value is not None and (info.section, info.key) in ADMIN_SETTING_KEYS and info.section != "admin"
        if not ignored and not inst.config_key_applies(kind, info.section, info.key):
            continue
        effective = override_value if override_value is not None else info.default_value
        if override_value is not None and (info.section, info.key) in ADDITIVE_OVERRIDE_KEYS:
            # 上書きは既定値に足す分だけを持つため、実際に効く値（既定値＋足す分）を見せ、それを編集させる。
            effective = merge_additive_override(info.section, info.key, info.default_value, override_value)[0]
        keys.append(
            {
                "section": info.section,
                "key": info.key,
                "default": info.default_value,
                "override": override_value,
                "effective": effective,
                "description": ini_catalog_en.DESCRIPTIONS.get(f"{info.section}.{info.key}", info.description) if english else info.description,
                "group_heading": ini_catalog_en.HEADINGS.get(info.group_heading or "", info.group_heading) if english else info.group_heading,
                "ui_kind": info.ui_kind,
                "choices": list(info.choices),
                "schema": _localize_schema(info.schema),
                "env_name": _env_var_mapping.get((info.section, info.key)),
                "env_override_active": (info.section, info.key) in active_env,
                "ignored": ignored,
            }
        )
    return {
        "keys": keys,
        "mtime": overrides.mtime_or_none(ov_path),
        "scope_note": _t(_SKILL_LAB_SCOPE_NOTE) if kind == inst.KIND_SKILL_LAB else None,
    }


@app.get("/api/instances/{name}/config")
def get_instance_config(name: str, user: str = Depends(require_login)):
    meta = _require_instance(name)
    return _config_key_view(name, meta.kind)


# 設定画面の候補（get_instance_config_candidates）で、メインエージェントのツール名を求め終わるのを待つ上限（秒）。
_TOOL_NAMES_WAIT_SECONDS = 3.0


@app.get("/api/instances/{name}/config/candidates")
def get_instance_config_candidates(name: str, user: str = Depends(require_login)):
    """スキル名・スクリプト名・agent_type・ツール名を選ぶ欄の候補（保存済みの設定でこのインスタンスが認識するもの）。

    同梱の skills/・agents/ に加え、[paths] project_locohane_dir とインスタンス専用の拡張ディレクトリを走査する。
    ツール名はメインエージェントのビルトインツール（MCP の動的ツールは接続しないと分からないため含まない）。
    設定が読めない場合は error を返し、画面は手入力の欄にする。
    ツール名は別プロセスで求める（管理ツールの起動時に求め始める）。_TOOL_NAMES_WAIT_SECONDS 秒で求め終わらなければ
    ツール名は空にして返す（設定画面の表示を待たせないため。ツール名の欄は手入力になり、次に開いたときに出る）。
    """
    _require_instance(name)
    try:
        tools = list(skill_lab_sources.main_agent_tool_names(timeout=_TOOL_NAMES_WAIT_SECONDS) or ())
    except Exception as exc:  # noqa: BLE001 - ツール名が取れなくてもスキル等の候補は出す（ツール名の欄は手入力になる）
        logger.warning("Cannot list built-in tool names: %s", exc)
        tools = []
    try:
        cfg = monitor.instance_config(INSTANCES_ROOT, CONFIG_INI_PATH, name)
        return {**skill_lab_sources.config_candidates(cfg), "tools": tools, "error": None}
    except Exception as exc:  # noqa: BLE001 - 候補が出せなくても設定画面自体は使えるようにする
        logger.warning("Cannot list skill/agent candidates for %s: %s", name, exc)
        return {"skills": [], "agent_types": [], "tools": tools, "error": str(exc)}


class ConfigUpdateBody(BaseModel):
    updates: dict[str, dict[str, str]] = {}
    resets: list[list[str]] = []
    base_mtime: float | None = None


def _flatten_updates(updates: dict[str, dict[str, str]], kind: str) -> dict[tuple[str, str], str]:
    flat = {(section, key): value for section, kv in updates.items() for key, value in kv.items()}
    # 画面に出していないキー（その種類のインスタンスでは効かない設定）は、API を直接叩いても保存させない。
    # インスタンス別に上書きできないキーは、overrides.save() が変更先の画面を案内するエラーで拒否する。
    rejected = [
        f"[{section}].{key}"
        for section, key in flat
        if is_instance_overridable(section, key) and not inst.config_key_applies(kind, section, key)
    ]
    if rejected:
        target = _t("スキル調整ワーカー") if kind == inst.KIND_SKILL_LAB else _t("本体のインスタンス")
        raise HTTPException(status_code=400, detail=_t("{0}では効かない設定のため変更できません: {1}", target, ', '.join(rejected)))
    return flat


def _flatten_resets(resets: list[list[str]]) -> list[tuple[str, str]]:
    result = []
    for pair in resets:
        if len(pair) != 2:
            raise HTTPException(status_code=400, detail=_t("resets の要素は [section, key] の2要素にしてください: {0!r}", pair))
        result.append((pair[0], pair[1]))
    return result


@app.post("/api/instances/{name}/config/preview")
def preview_instance_config(name: str, body: ConfigUpdateBody, user: str = Depends(require_login)):
    meta = _require_instance(name)
    catalog = parse_ini_file(CONFIG_INI_PATH)
    try:
        _new_data, changes = overrides.preview(
            path=inst.overrides_path(INSTANCES_ROOT, name),
            updates=_flatten_updates(body.updates, meta.kind),
            resets=_flatten_resets(body.resets),
            ini_catalog=catalog,
            config_ini_path=CONFIG_INI_PATH,
            instance_name=name,
            instances_root=INSTANCES_ROOT,
        )
    except overrides.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"changes": changes}


@app.put("/api/instances/{name}/config")
def put_instance_config(
    name: str,
    body: ConfigUpdateBody,
    request: Request,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    meta = _require_instance(name)
    catalog = parse_ini_file(CONFIG_INI_PATH)
    try:
        result = overrides.save(
            path=inst.overrides_path(INSTANCES_ROOT, name),
            updates=_flatten_updates(body.updates, meta.kind),
            resets=_flatten_resets(body.resets),
            base_mtime=body.base_mtime,
            ini_catalog=catalog,
            config_ini_path=CONFIG_INI_PATH,
            backup_dir=inst.backups_dir(INSTANCES_ROOT, name),
            backup_keep=_cfg.admin_backup_keep,
            audit_log_path=AUDIT_LOG_PATH,
            instance_name=name,
            actor=user,
            remote_addr=_client_ip(request),
            instances_root=INSTANCES_ROOT,
        )
    except overrides.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except overrides.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    needs_restart = _supervisor.status(name).state == supervisor.InstanceState.RUNNING
    return {"overrides": result, "needs_restart": needs_restart, "mtime": overrides.mtime_or_none(inst.overrides_path(INSTANCES_ROOT, name))}


@app.get("/api/instances/{name}/backups")
def list_backups(name: str, user: str = Depends(require_login)):
    _require_instance(name)
    backup_dir = inst.backups_dir(INSTANCES_ROOT, name)
    if not backup_dir.is_dir():
        return {"backups": []}
    files = sorted(backup_dir.glob("config_overrides_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {"backups": [{"name": f.name, "mtime": f.stat().st_mtime} for f in files]}


@app.post("/api/instances/{name}/backups/{filename}/restore")
def restore_backup(
    name: str,
    filename: str,
    request: Request,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    _require_instance(name)
    backup_dir = inst.backups_dir(INSTANCES_ROOT, name)
    src = backup_dir / filename
    if "/" in filename or "\\" in filename or ".." in filename or src.parent != backup_dir:
        raise HTTPException(status_code=400, detail=_t("不正なファイル名です。"))
    try:
        result = overrides.restore(
            path=inst.overrides_path(INSTANCES_ROOT, name),
            backup_file=src,
            config_ini_path=CONFIG_INI_PATH,
            backup_dir=backup_dir,
            backup_keep=_cfg.admin_backup_keep,
            audit_log_path=AUDIT_LOG_PATH,
            instance_name=name,
            actor=user,
            remote_addr=_client_ip(request),
            instances_root=INSTANCES_ROOT,
        )
    except overrides.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    needs_restart = _supervisor.status(name).state == supervisor.InstanceState.RUNNING
    return {"overrides": result, "needs_restart": needs_restart}


# ---------------------------------------------------------------------------
# ユーザー管理（instances/<name>/.env）
# ---------------------------------------------------------------------------


@app.get("/api/instances/{name}/users")
def list_users(name: str, user: str = Depends(require_login)):
    _require_instance(name)
    own_env_path = inst.env_path(INSTANCES_ROOT, name)
    own_values = env_files.read_values(own_env_path)
    if env_files.AUTH_USERS_KEY in own_values:
        usernames = sorted(env_files.read_users(own_env_path).keys())
        inherited = False
    else:
        usernames = sorted(env_files.read_users(PROJECT_ENV_PATH).keys())
        inherited = True
    return {
        "usernames": usernames,
        "inherited_from_project_env": inherited,
        "auth_secret_set": bool(env_files.read_auth_secret(own_env_path)),
    }


class UserAddBody(BaseModel):
    username: str
    password: str


@app.post("/api/instances/{name}/users")
def add_user(
    name: str,
    body: UserAddBody,
    request: Request,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    _require_instance(name)
    env_path = inst.env_path(INSTANCES_ROOT, name)
    users = env_files.read_users(env_path)
    if body.username in users:
        raise HTTPException(status_code=400, detail=_t("既に存在するユーザー名です。"))
    users[body.username] = body.password
    env_files.write_users(env_path, users)
    audit.append(
        AUDIT_LOG_PATH,
        {
            "instance": name,
            "actor": user,
            "remote_addr": _client_ip(request),
            "action": "user_add",
            "target_user": body.username,
        },
    )
    return {"usernames": sorted(users.keys())}


def _reject_if_project_env_user(name: str, target_username: str) -> None:
    """継承中のプロジェクト直下 .env のユーザーは管理者設定なので、画面からの変更・削除を拒否する。"""
    own_values = env_files.read_values(inst.env_path(INSTANCES_ROOT, name))
    if env_files.AUTH_USERS_KEY in own_values:
        return
    if target_username in env_files.read_users(PROJECT_ENV_PATH):
        raise HTTPException(
            status_code=403,
            detail=_t("プロジェクト直下 .env のユーザー（管理者設定）はこの画面から変更・削除できません。"),
        )


class UserPasswordBody(BaseModel):
    password: str


@app.put("/api/instances/{name}/users/{target_username}")
def set_user_password(
    name: str,
    target_username: str,
    body: UserPasswordBody,
    request: Request,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    _require_instance(name)
    _reject_if_project_env_user(name, target_username)
    env_path = inst.env_path(INSTANCES_ROOT, name)
    users = env_files.read_users(env_path)
    if target_username not in users:
        raise HTTPException(status_code=404, detail=_t("ユーザーが見つかりません（継承中の場合は先にユーザーを追加してください）。"))
    users[target_username] = body.password
    env_files.write_users(env_path, users)
    audit.append(
        AUDIT_LOG_PATH,
        {
            "instance": name,
            "actor": user,
            "remote_addr": _client_ip(request),
            "action": "user_password_reset",
            "target_user": target_username,
        },
    )
    return {"success": True}


@app.delete("/api/instances/{name}/users/{target_username}")
def delete_user(
    name: str,
    target_username: str,
    request: Request,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    _require_instance(name)
    _reject_if_project_env_user(name, target_username)
    env_path = inst.env_path(INSTANCES_ROOT, name)
    users = env_files.read_users(env_path)
    if target_username not in users:
        raise HTTPException(status_code=404, detail=_t("ユーザーが見つかりません。"))
    del users[target_username]
    env_files.write_users(env_path, users)
    audit.append(
        AUDIT_LOG_PATH,
        {
            "instance": name,
            "actor": user,
            "remote_addr": _client_ip(request),
            "action": "user_delete",
            "target_user": target_username,
        },
    )
    return {"usernames": sorted(users.keys())}


@app.post("/api/instances/{name}/auth-secret")
def generate_auth_secret(
    name: str, request: Request, user: str = Depends(require_login), _csrf: None = Depends(require_csrf)
):
    _require_instance(name)
    secret = _secrets.token_urlsafe(64)
    env_files.write_auth_secret(inst.env_path(INSTANCES_ROOT, name), secret)
    audit.append(
        AUDIT_LOG_PATH,
        {"instance": name, "actor": user, "remote_addr": _client_ip(request), "action": "auth_secret_generate"},
    )
    return {"success": True}


@app.get("/api/instances/{name}/env")
def get_extra_env(name: str, user: str = Depends(require_login)):
    _require_instance(name)
    values = env_files.read_extra_vars(inst.env_path(INSTANCES_ROOT, name))
    return {"vars": {k: audit.mask(k, v) for k, v in values.items()}}


class EnvVarBody(BaseModel):
    key: str
    value: str


@app.put("/api/instances/{name}/env")
def set_extra_env(
    name: str,
    body: EnvVarBody,
    request: Request,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    _require_instance(name)
    try:
        env_files.write_extra_var(inst.env_path(INSTANCES_ROOT, name), body.key, body.value)
    except env_files.EnvFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.append(
        AUDIT_LOG_PATH,
        {
            "instance": name,
            "actor": user,
            "remote_addr": _client_ip(request),
            "action": "env_var_set",
            "key": body.key,
            "value": audit.mask(body.key, body.value),
        },
    )
    return {"success": True}


@app.delete("/api/instances/{name}/env/{key}")
def delete_extra_env(
    name: str, key: str, request: Request, user: str = Depends(require_login), _csrf: None = Depends(require_csrf)
):
    _require_instance(name)
    try:
        env_files.delete_extra_var(inst.env_path(INSTANCES_ROOT, name), key)
    except env_files.EnvFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit.append(
        AUDIT_LOG_PATH,
        {"instance": name, "actor": user, "remote_addr": _client_ip(request), "action": "env_var_delete", "key": key},
    )
    return {"success": True}


# ---------------------------------------------------------------------------
# 表示設定（instance 未指定 = public/settings/ の共通設定、
# 指定 = instances/<name>/settings/ のインスタンス専用設定）
# ---------------------------------------------------------------------------


def _settings_target(instance: str | None) -> tuple[Path, Path]:
    """(書き込み先ディレクトリ, バックアップ先) を返す。"""
    if not instance:
        return SETTINGS_DIR, SETTINGS_BACKUP_DIR
    _require_instance(instance)
    return inst.settings_dir(INSTANCES_ROOT, instance), inst.backups_dir(INSTANCES_ROOT, instance)


@app.get("/api/settings")
def get_settings_overview(instance: str | None = None, user: str = Depends(require_login)):
    """各表示設定の実効値と、インスタンス専用に上書きされているかを返す。"""
    target_dir, _ = _settings_target(instance)
    texts = {}
    for name in sorted(settings_files.TEXT_FILE_NAMES):
        overridden = bool(instance) and settings_files.has_text_file(target_dir, name)
        source_dir = target_dir if overridden else SETTINGS_DIR
        texts[name] = {"content": settings_files.read_text_file(source_dir, name), "overridden": overridden}
    images = {}
    for kind in ("icon", "favicon"):
        own = settings_files.image_file(target_dir, kind) if instance else None
        images[kind] = {
            "overridden": own is not None,
            "filename": own or settings_files.image_file(SETTINGS_DIR, kind),
        }
    return {"instance": instance or None, "texts": texts, "images": images}


class TextSettingBody(BaseModel):
    content: str


class ImageUploadBody(BaseModel):
    filename: str
    content_base64: str


def _audit_settings(request: Request, user: str, instance: str | None, action: str, file: str) -> None:
    audit.append(
        AUDIT_LOG_PATH,
        {"instance": instance or None, "actor": user, "remote_addr": _client_ip(request), "action": action, "file": file},
    )


# 画像ルートは /api/settings/{filename} と衝突しないよう images/ 配下に分ける。
@app.put("/api/settings/images/{kind}")
def put_setting_image(
    kind: str,
    body: ImageUploadBody,
    request: Request,
    instance: str | None = None,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    import base64
    import binascii

    target_dir, backup_dir = _settings_target(instance)
    try:
        content = base64.b64decode(body.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=_t("base64のデコードに失敗しました: {0}", exc)) from exc
    try:
        saved_name = settings_files.write_image(target_dir, kind, body.filename, content, backup_dir, _cfg.admin_backup_keep)
    except settings_files.SettingsFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_settings(request, user, instance, "settings_update", saved_name)
    return {"filename": saved_name}


@app.delete("/api/settings/images/{kind}")
def delete_setting_image(
    kind: str,
    request: Request,
    instance: str,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    """インスタンス専用の画像を削除し、共通設定に戻す。"""
    target_dir, backup_dir = _settings_target(instance)
    if kind not in ("icon", "favicon"):
        raise HTTPException(status_code=400, detail=_t("不正な種別です: {0}", kind))
    settings_files.delete_file(target_dir, kind, backup_dir, _cfg.admin_backup_keep)
    _audit_settings(request, user, instance, "settings_reset_to_shared", kind)
    return {"success": True}


@app.put("/api/settings/{filename}")
def put_setting_text(
    filename: str,
    body: TextSettingBody,
    request: Request,
    instance: str | None = None,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    target_dir, backup_dir = _settings_target(instance)
    try:
        settings_files.write_text_file(target_dir, filename, body.content, backup_dir, _cfg.admin_backup_keep)
    except settings_files.SettingsFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_settings(request, user, instance, "settings_update", filename)
    return {"success": True}


@app.delete("/api/settings/{filename}")
def delete_setting_text(
    filename: str,
    request: Request,
    instance: str,
    user: str = Depends(require_login),
    _csrf: None = Depends(require_csrf),
):
    """インスタンス専用のテキストを削除し、共通設定に戻す。"""
    target_dir, backup_dir = _settings_target(instance)
    try:
        settings_files.delete_file(target_dir, filename, backup_dir, _cfg.admin_backup_keep)
    except settings_files.SettingsFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit_settings(request, user, instance, "settings_reset_to_shared", filename)
    return {"success": True}


# ---------------------------------------------------------------------------
# モニター（稼働状況・会話閲覧・トークン推移・ログ・LLM接続先。読み取り専用）
# ---------------------------------------------------------------------------


def _monitor_config(name: str):
    _require_instance(name)
    try:
        return monitor.instance_config(INSTANCES_ROOT, CONFIG_INI_PATH, name)
    except monitor.MonitorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _runtime_view(name: str, cfg) -> dict[str, Any]:
    status = _supervisor.status(name)
    is_live = status.state in (supervisor.InstanceState.RUNNING, supervisor.InstanceState.EXTERNAL)
    raw = monitor.read_live_runtime_status(cfg.common_data_dir, is_live=is_live)
    ids = [s.get("thread_id") for s in (raw or {}).get("sessions", [])]
    ids += [g.get("thread_id") for g in (raw or {}).get("generating", [])]
    try:
        names = monitor.thread_names(cfg.thread_store_db, ids)
    except (monitor.MonitorError, sqlite3.Error):
        names = {}
    return {"state": status.state.value, **monitor.summarize_runtime(raw, names)}


@app.get("/api/monitor/overview")
def get_monitor_overview(user: str = Depends(require_login)):
    """全インスタンスの接続中ユーザー数・生成中スレッド数・ナレッジの取り込み状況（インスタンス一覧のカード用）。"""
    result = []
    ingesting = _knowledge_router.ingest_jobs.running()
    workers = _knowledge_router.ingest_slots.snapshot()
    for name in inst.list_instance_names(INSTANCES_ROOT):
        try:
            cfg = monitor.instance_config(INSTANCES_ROOT, CONFIG_INI_PATH, name)
        except monitor.MonitorError:
            continue
        view = _runtime_view(name, cfg)
        generating = {g.get("thread_id"): g for g in view["generating"]}
        try:
            series = monitor.live_context_series(cfg.log_dir, view["generating"])
        except Exception:  # noqa: BLE001 - グラフ用の付帯情報のため、失敗してもカードの稼働状況は返す
            logger.warning("Could not get the input token history of instance %s", name, exc_info=True)
            series = []
        context_series = [
            {**s, "thread_name": generating[s["thread_id"]].get("thread_name"), "owner": generating[s["thread_id"]].get("owner")}
            for s in series
        ]
        result.append(
            {
                "name": name,
                "available": view["available"],
                # 接続中（セッションあり）のユーザー。生成だけ裏で続いているユーザーは含めない。
                "users": [u["user"] for u in view["users"] if u["sessions"] > 0],
                "sessions": len(view["sessions"]),
                "generating": len(view["generating"]),
                "waiting": sum(1 for g in view["generating"] if g.get("waiting_for_user")),
                "log_counts_24h": monitor.recent_level_counts(cfg.log_dir),
                # 生成中スレッドごとの LLM リクエストの入力トークン（メイン・サブの最大値）。
                "context_series": context_series,
                # ナレッジで取り込み中（順番待ち・削除を含む）のコレクション（管理ツールのプロセス内のジョブ）と、
                # そのジョブが使っている取り込みの枠（knowledge.job_workers。カードのワーカーのアイコン）。
                "ingesting": [
                    {
                        **{
                            k: getattr(j, k)
                            for k in ("collection", "kind", "state", "trigger", "started_at", "done", "total", "current", "stop_requested")
                        },
                        "workers": knowledge_api.job_workers(workers, name, j.collection),
                    }
                    for j in ingesting.get(name, [])
                ],
            }
        )
    return {"instances": result}


@app.get("/api/instances/{name}/monitor/runtime")
def get_monitor_runtime(name: str, user: str = Depends(require_login)):
    cfg = _monitor_config(name)
    view = _runtime_view(name, cfg)
    view["log_counts_24h"] = monitor.recent_level_counts(cfg.log_dir)
    return view


@app.get("/api/instances/{name}/monitor/users")
def get_monitor_users(name: str, user: str = Depends(require_login)):
    cfg = _monitor_config(name)
    try:
        return {"users": monitor.user_summary(cfg.thread_store_db)}
    except (monitor.MonitorError, sqlite3.Error) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/instances/{name}/monitor/threads")
def get_monitor_threads(
    name: str,
    owner: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: str = Depends(require_login),
):
    cfg = _monitor_config(name)
    try:
        return monitor.list_threads(cfg.thread_store_db, owner=owner, query=q, limit=min(max(limit, 1), 500), offset=max(offset, 0))
    except (monitor.MonitorError, sqlite3.Error) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/instances/{name}/monitor/threads/{thread_id}")
def get_monitor_thread(
    name: str, thread_id: str, request: Request, internal: bool = False, user: str = Depends(require_login)
):
    """会話内容。利用者の会話を管理者が閲覧した事実を変更履歴へ残す。"""
    cfg = _monitor_config(name)
    try:
        detail = monitor.thread_detail(cfg.thread_store_db, thread_id, include_internal=internal)
    except (monitor.MonitorError, sqlite3.Error) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail=_t("スレッドが見つかりません。"))
    audit.append(
        AUDIT_LOG_PATH,
        {
            "instance": name,
            "actor": user,
            "remote_addr": _client_ip(request),
            "action": "conversation_view",
            "thread_id": thread_id,
            "thread_owner": detail["owner"],
        },
    )
    return detail


@app.get("/api/instances/{name}/monitor/threads/{thread_id}/tokens")
def get_monitor_thread_tokens(name: str, thread_id: str, user: str = Depends(require_login)):
    cfg = _monitor_config(name)
    return {"points": monitor.token_history(cfg.log_dir, thread_id)}


@app.get("/api/instances/{name}/monitor/logs")
def get_monitor_logs(
    name: str,
    level: str = "WARNING",
    limit: int = 200,
    q: str | None = None,
    thread_id: str | None = None,
    user: str = Depends(require_login),
):
    cfg = _monitor_config(name)
    entries = monitor.tail_log(cfg.log_dir, min_level=level, limit=min(max(limit, 1), 2000), query=q, thread_id=thread_id)
    return {"entries": entries}


@app.get("/api/instances/{name}/monitor/endpoints")
def get_monitor_endpoints(name: str, user: str = Depends(require_login)):
    cfg = _monitor_config(name)
    return {"endpoints": monitor.probe_endpoints(cfg)}


# ---------------------------------------------------------------------------
# 変更履歴
# ---------------------------------------------------------------------------


@app.get("/api/audit")
def get_audit(instance: str | None = None, limit: int = 200, user: str = Depends(require_login)):
    return {"entries": audit.read_recent(AUDIT_LOG_PATH, limit=limit, instance=instance)}


# ---------------------------------------------------------------------------
# APIリファレンス（admin/API_REFERENCE.md をリクエストごとに読み直して返す）
# ---------------------------------------------------------------------------


@app.get("/api/docs/api-reference")
def get_api_reference(user: str = Depends(require_login)):
    path = api_docs.reference_path(admin_i18n.current_lang())
    try:
        return api_docs.render(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_t("{0} が見つかりません。", path.name)) from exc


# ---------------------------------------------------------------------------
# スキル研究室（admin/skill_lab/api.py。テーマはインスタンスごとの /api/instances/{name}/skill-lab/ 配下）
# ---------------------------------------------------------------------------

app.include_router(
    skill_lab_api.build_router(
        require_login=require_login,
        require_csrf=require_csrf,
        instances_root=INSTANCES_ROOT,
        supervisor=_supervisor,
        audit_log_path=AUDIT_LOG_PATH,
        client_ip=_client_ip,
        backup_keep=_cfg.admin_backup_keep,
    )
)


# ---------------------------------------------------------------------------
# ナレッジ（ベクターDB のコレクションと取り込み。admin/knowledge.py）
# ---------------------------------------------------------------------------

# 時間指定の自動取り込み（_knowledge_router.ingest_scheduler）は _lifespan で起動・停止する。
_knowledge_router = knowledge_api.build_router(
    require_login=require_login,
    require_csrf=require_csrf,
    instances_root=INSTANCES_ROOT,
    audit_log_path=AUDIT_LOG_PATH,
    client_ip=_client_ip,
)
app.include_router(_knowledge_router)


# ---------------------------------------------------------------------------
# データバックアップ（instances_dir と全インスタンスの common_data_dir の zip。admin/data_backup.py）
# ---------------------------------------------------------------------------

# 時間指定の自動バックアップ（_data_backup_router.backup_scheduler）は _lifespan で起動・停止する。
_data_backup_router = data_backup_api.build_router(
    require_login=require_login,
    require_csrf=require_csrf,
    instances_root=INSTANCES_ROOT,
    config_ini_path=CONFIG_INI_PATH,
    audit_log_path=AUDIT_LOG_PATH,
    client_ip=_client_ip,
    config_loader=lambda name: monitor.instance_config(INSTANCES_ROOT, CONFIG_INI_PATH, name),
)
app.include_router(_data_backup_router)


# ---------------------------------------------------------------------------
# 管理者設定（全インスタンス共通の設定。admin/admin_settings.py）
# ---------------------------------------------------------------------------

app.include_router(
    admin_settings_api.build_router(
        require_login=require_login,
        require_csrf=require_csrf,
        instances_root=INSTANCES_ROOT,
        config_ini_path=CONFIG_INI_PATH,
        audit_log_path=AUDIT_LOG_PATH,
        client_ip=_client_ip,
        config_loader=lambda name: monitor.instance_config(INSTANCES_ROOT, CONFIG_INI_PATH, name),
        list_instances=lambda: _assistant_instances(),
    )
)


# ---------------------------------------------------------------------------
# 管理アシスタント（右上の「AIに相談」。admin/assistant.py。設定は読むだけで変更しない）
# ---------------------------------------------------------------------------


def _assistant_catalog_keys(name: str):
    kind = _require_instance(name).kind
    return [k for k in parse_ini_file(CONFIG_INI_PATH).keys if inst.config_key_applies(kind, k.section, k.key)]


def _assistant_config_view(name: str) -> dict[str, Any]:
    return _config_key_view(name, _require_instance(name).kind)


def _assistant_instances() -> list[dict[str, str]]:
    return [
        {k: str(v) for k, v in _instance_summary(name).items() if k in ("name", "display_name", "kind", "state")}
        for name in inst.list_instance_names(INSTANCES_ROOT)
    ]


# config.ini・README_DETAIL.md・API_REFERENCE.md の索引（元ファイルが変われば次の相談で作り直す）。
# 文書側の埋め込みは instances/_assistant_index/ にキャッシュする。
# 英語表示の相談は英訳の文書・説明で作った索引で探す（日本語の索引では英語の質問と文字が重ならない）。
_assistant_index = {
    "ja": assistant_index.AssistantIndex(
        config_ini_path=CONFIG_INI_PATH,
        readme_path=PROJECT_ROOT / "README_DETAIL.md",
        api_reference_path=api_docs.API_REFERENCE_PATH,
        cache_dir=INSTANCES_ROOT / "_assistant_index",
    ),
    "en": assistant_index.AssistantIndex(
        config_ini_path=CONFIG_INI_PATH,
        readme_path=PROJECT_ROOT / "README_DETAIL.en.md",
        api_reference_path=api_docs.API_REFERENCE_EN_PATH,
        cache_dir=INSTANCES_ROOT / "_assistant_index",
        language="en",
    ),
}


def _assistant_embedding(target: str, llm_instance: str):
    """検索に使う埋め込み（[admin] assistant_use_embedding・assistant_embedding_instance）。無ければ None。"""
    if not _cfg.admin_assistant_use_embedding:
        return None
    # 「管理者設定」で変えた値を再起動なしで効かせるため、相談のたびに読み直す。
    fixed = admin_settings_api.current_admin_config(CONFIG_INI_PATH).admin_assistant_embedding_instance
    others = sorted(inst.list_instance_names(INSTANCES_ROOT))
    for name in dict.fromkeys([fixed] if fixed else [llm_instance, target, *others]):
        try:
            cfg = monitor.instance_config(INSTANCES_ROOT, CONFIG_INI_PATH, name)
        except monitor.MonitorError as exc:
            logger.warning("Admin assistant: cannot read the settings of instance %r, excluding it from embedding endpoint candidates: %s", name, exc)
            continue
        provider = assistant_index.provider_from_config(cfg, name)
        if provider is not None:
            return provider
    return None


app.include_router(
    assistant.build_router(
        require_login=require_login,
        require_csrf=require_csrf,
        admin_config=_cfg,
        catalog_keys=_assistant_catalog_keys,
        config_view=_assistant_config_view,
        list_instances=_assistant_instances,
        instance_kind=lambda name: _require_instance(name).kind,
        config_loader=lambda name: monitor.instance_config(INSTANCES_ROOT, CONFIG_INI_PATH, name),
        index=_assistant_index,
        llm_instance_setting=lambda: admin_settings_api.current_admin_config(CONFIG_INI_PATH).admin_assistant_llm_instance,
        embedding_provider=_assistant_embedding,
    )
)


# ---------------------------------------------------------------------------
# 静的ファイル（ログイン画面・ダッシュボードUI）
# ---------------------------------------------------------------------------

if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def _setup_file_logging() -> None:
    """管理ツールのログ（ナレッジの取り込み・削除等）を ADMIN_LOG_DIR/admin_*.log にも書く。

    行数でのローテーションは [log].max_lines（本体の app_*.log と同じ）。[log].level = none なら書かない。
    """
    if _cfg.log_level == "none":
        return
    from src.log_rotation import LineCountRotatingFileHandler

    ADMIN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = LineCountRotatingFileHandler(ADMIN_LOG_DIR, _cfg.log_max_lines, clear_on_startup=False, prefix="admin")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logger.info("Admin tool log: %s", ADMIN_LOG_DIR)


def main() -> None:
    import uvicorn

    _setup_file_logging()
    ssl_kwargs = {"ssl_certfile": str(SSL_FILES[0]), "ssl_keyfile": str(SSL_FILES[1])} if SSL_FILES else {}
    uvicorn.run(app, host=_cfg.admin_host, port=_cfg.admin_port, log_level="info", **ssl_kwargs)


if __name__ == "__main__":
    main()
