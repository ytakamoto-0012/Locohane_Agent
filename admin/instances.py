"""instances/ ディレクトリ配下のインスタンス管理（作成・削除・複製・整合性チェック）。

各インスタンスは Locohane 本体（app.py）を独立した子プロセスとして動かす単位。
instances/<name>/ 配下に instance.json（起動情報）・config_overrides.json
（admin/overrides.py が書く設定差分）・.env（admin/env_files.py が書く
ログイン情報）・backups/ を持つ。データ本体は config.ini 既定の
common_data_dir = ./data/${instance} により data/<name>/ に置かれる（README_DETAIL.md「ディレクトリ構成」参照）。

インスタンスには種類（instance.json の kind）がある:
- app（既定）: Locohane 本体（chainlit run app.py）。
- skill_lab: スキルスキル調整ワーカー（python -m admin.skill_lab.worker）。チャット画面・
  ポートを持たず、研究テーマ（各 app インスタンスの ${common_data_dir}/skill_lab/）を
  そのインスタンスの [llm] 接続先で評価・修正する。
"""

from __future__ import annotations

import json
import re
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

from src.config import PROJECT_ROOT, is_instance_overridable, load_config
from admin.i18n import t as _t

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# 新規作成時のインスタンス名の上限文字数。インスタンス名は data/<name>/ 等のパスに
# そのまま入るため、長いと配下（default_workdir の _tmp_*、スキルの生成物等）で
# Windows のパス長上限（260文字）を超えやすい。既存インスタンス（default 等）の
# 参照には影響させないよう、validate_name() ではなく create_instance() だけで見る。
NEW_NAME_MAX_LEN = 6

DEFAULT_INSTANCE_NAME = "default"
DEFAULT_APP_HOST = "127.0.0.1"
DEFAULT_APP_PORT = 8000

KIND_APP = "app"
KIND_SKILL_LAB = "skill_lab"
INSTANCE_KINDS = (KIND_APP, KIND_SKILL_LAB)

# スキル調整ワーカーが自分の設定として持つもの。それ以外（スキル・ガード・タイムアウト・[llm] の推論パラメータ等）は
# 研究テーマの対象インスタンスの設定を引き継ぐ（admin/skill_lab/evaluation.py・worker.py の _llm_config()）ため、
# ワーカーの設定画面には出さず、保存もさせない。LLM の接続先は evals/instance.py の _LLM_ENDPOINT_KEYS と揃える。
SKILL_LAB_WORKER_KEYS = frozenset(
    {("llm", "main_url"), ("llm", "sub_url"), ("llm", "main_routing_strategy"), ("llm", "sub_routing_strategy")}
)
SKILL_LAB_WORKER_SECTIONS = frozenset({"skill_lab_worker", "log"})


def config_key_applies(kind: str, section: str, key: str) -> bool:
    """config.ini のキーが、その種類のインスタンスの設定として意味を持つか（設定画面に出す・保存を許すか）。

    スキル調整ワーカーは接続先・[skill_lab_worker]・[log] だけ。本体には [skill_lab_worker] を出さない。
    インスタンス別の上書きが効かない（config.ini・管理者設定の値しか使わない）キーはどちらにも出さない。
    """
    if not is_instance_overridable(section, key):
        return False
    if kind == KIND_SKILL_LAB:
        return section in SKILL_LAB_WORKER_SECTIONS or (section, key) in SKILL_LAB_WORKER_KEYS
    return section != "skill_lab_worker"


def filter_overrides_for_kind(kind: str, data: dict) -> dict:
    """config_overrides.json の内容から、その種類のインスタンスで意味を持つキーだけを残す（複製時用）。"""
    result: dict = {}
    for section, kv in data.items():
        if not isinstance(kv, dict):
            continue
        kept = {k: v for k, v in kv.items() if config_key_applies(kind, section, k)}
        if kept:
            result[section] = kept
    return result


class InstanceError(Exception):
    """instances.py の操作失敗。"""


@dataclass
class InstanceMeta:
    """instance.json の内容。"""

    name: str
    display_name: str
    app_host: str = DEFAULT_APP_HOST
    app_port: int = DEFAULT_APP_PORT
    autostart: bool = False
    # chainlit run の -h/--headless（ブラウザタブの自動オープンを抑止）。
    # 既定 True（管理ツール経由の起動でタブが乱立しないように）。
    headless: bool = True
    # chainlit run の -w/--watch（ファイル変更検知での自動リロード）。
    # 既定 False（本番運用では非推奨。開発時の動作確認用にインスタンス単位で
    # 有効化できる）。
    watch: bool = False
    # インスタンスの種類（KIND_APP / KIND_SKILL_LAB）。作成後は変更しない。
    kind: str = KIND_APP

    @property
    def is_skill_lab(self) -> bool:
        return self.kind == KIND_SKILL_LAB

    def to_json(self) -> dict:
        return {
            "display_name": self.display_name,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "autostart": self.autostart,
            "headless": self.headless,
            "watch": self.watch,
            "kind": self.kind,
        }


def validate_name(name: str) -> None:
    """インスタンス名の妥当性を検証する（そのままディレクトリ名に使うため制限する）。"""
    if not _NAME_RE.match(name):
        raise InstanceError(_t("インスタンス名は半角英数字・アンダースコア・ハイフンのみ、1〜32文字にしてください: {0!r}", name))


def validate_new_name(name: str) -> None:
    """新規作成するインスタンス名を検証する（validate_name() に加えて NEW_NAME_MAX_LEN 文字以内）。"""
    validate_name(name)
    if len(name) > NEW_NAME_MAX_LEN:
        raise InstanceError(
            _t("インスタンス名は{0}文字以内にしてください（データ保存先のパスが長くなりすぎるため）: {1!r}", NEW_NAME_MAX_LEN, name)
        )


def instance_dir(instances_root: Path, name: str) -> Path:
    return instances_root / name


def instance_json_path(instances_root: Path, name: str) -> Path:
    return instance_dir(instances_root, name) / "instance.json"


def overrides_path(instances_root: Path, name: str) -> Path:
    return instance_dir(instances_root, name) / "config_overrides.json"


def env_path(instances_root: Path, name: str) -> Path:
    return instance_dir(instances_root, name) / ".env"


def settings_dir(instances_root: Path, name: str) -> Path:
    """インスタンス専用の表示設定（public/settings/ より優先される。app.py 参照）。"""
    return instance_dir(instances_root, name) / "settings"


def backups_dir(instances_root: Path, name: str) -> Path:
    return instance_dir(instances_root, name) / "backups"


def stdout_log_path(instances_root: Path, name: str) -> Path:
    return instance_dir(instances_root, name) / "app_stdout.log"


def list_instance_names(instances_root: Path) -> list[str]:
    """instance.json を持つディレクトリの一覧（名前順）。instances_root が無ければ空リスト。"""
    if not instances_root.is_dir():
        return []
    return sorted(p.name for p in instances_root.iterdir() if p.is_dir() and (p / "instance.json").is_file())


def read_instance(instances_root: Path, name: str) -> InstanceMeta:
    path = instance_json_path(instances_root, name)
    if not path.is_file():
        raise InstanceError(_t("インスタンス {0!r} が見つかりません。", name))
    data = json.loads(path.read_text(encoding="utf-8"))
    return InstanceMeta(
        name=name,
        display_name=data.get("display_name", name),
        app_host=data.get("app_host", DEFAULT_APP_HOST),
        app_port=int(data.get("app_port", DEFAULT_APP_PORT)),
        autostart=bool(data.get("autostart", False)),
        # 既存インスタンス（headless/watch導入前に作成済み）との後方互換のため、
        # キー自体が無い場合は InstanceMeta の既定値（headless=True, watch=False）を使う。
        headless=bool(data.get("headless", True)),
        watch=bool(data.get("watch", False)),
        # kind 導入前に作成済みのインスタンスは本体（app）。
        kind=data.get("kind", KIND_APP) if data.get("kind", KIND_APP) in INSTANCE_KINDS else KIND_APP,
    )


def write_instance(instances_root: Path, meta: InstanceMeta) -> None:
    path = instance_json_path(instances_root, meta.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_default_instance(instances_root: Path) -> InstanceMeta:
    """default インスタンスの instance.json が無ければ、app.bat と同じ 127.0.0.1:8000 で作る。"""
    if instance_json_path(instances_root, DEFAULT_INSTANCE_NAME).is_file():
        return read_instance(instances_root, DEFAULT_INSTANCE_NAME)
    meta = InstanceMeta(
        name=DEFAULT_INSTANCE_NAME,
        display_name="default",
        app_host=DEFAULT_APP_HOST,
        app_port=DEFAULT_APP_PORT,
        autostart=True,
    )
    write_instance(instances_root, meta)
    return meta


def is_port_available(host: str, port: int) -> bool:
    """host:port へ実際に bind してみて、空いているかを確認する。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
        return True


def suggest_port(instances_root: Path, host: str, exclude_ports: set[int], start: int = 8000) -> int:
    """8000以上で、既存インスタンス・除外ポート・実際のbind確認のいずれとも重ならない最小のポートを提案する。"""
    used = {read_instance(instances_root, n).app_port for n in list_instance_names(instances_root)} - {0}
    used |= exclude_ports
    port = start
    while port in used or not is_port_available(host, port):
        port += 1
        if port > 65535:
            raise InstanceError(_t("空いているポートが見つかりませんでした。"))
    return port


def _effective_checkpoint_db(config_ini_path: Path, ov_path: Path, name: str) -> Path:
    """そのインスタンスの実効設定を解決し、checkpoint_db の絶対パスを返す。

    common_data_dir（延いてはデータ保存先全体）が重複していないかの比較に
    使う代表値。checkpoint_db は必ず存在する設定であり、common_data_dir の
    直下にある（config.ini既定）ため、これが一致すれば実質的に同じデータ
    ディレクトリを指しているとみなせる。
    """
    cfg = load_config(config_path=config_ini_path, overrides_path=ov_path, instance_name=name)
    return cfg.checkpoint_db


def _check_data_dir_conflict(instances_root: Path, config_ini_path: Path, *, name: str, own_checkpoint_db: Path) -> None:
    """他インスタンスとのデータ保存先（checkpoint_db）の重複だけを検出する（ポートは見ない）。

    check_conflicts() と check_data_dir_conflict_for_overrides() の共通処理。

    Raises:
        InstanceError: 重複が見つかった場合。
    """
    for other_name in list_instance_names(instances_root):
        if other_name == name:
            continue
        other_checkpoint_db = _effective_checkpoint_db(config_ini_path, overrides_path(instances_root, other_name), other_name)
        if other_checkpoint_db == own_checkpoint_db:
            raise InstanceError(
                _t("データ保存先がインスタンス {0!r} と重複しています（{1}）。[paths].common_data_dir を分けてください。", other_name, own_checkpoint_db)
            )


def check_conflicts(
    instances_root: Path,
    config_ini_path: Path,
    *,
    name: str,
    app_host: str,
    app_port: int,
    kind: str = KIND_APP,
) -> None:
    """他インスタンスとのポート・データ保存先の重複を検出する。

    作成時・設定変更時（ポート変更）・起動時に呼ぶ。スキル調整ワーカー（kind=skill_lab）は
    ポートを使わないため、ポートの重複は本体（app）同士だけで確かめる。

    Raises:
        InstanceError: 重複が見つかった場合。
    """
    own_checkpoint_db = _effective_checkpoint_db(config_ini_path, overrides_path(instances_root, name), name)
    for other_name in list_instance_names(instances_root):
        if other_name == name or kind == KIND_SKILL_LAB:
            continue
        other_meta = read_instance(instances_root, other_name)
        if other_meta.is_skill_lab:
            continue
        if other_meta.app_host == app_host and other_meta.app_port == app_port:
            raise InstanceError(_t("ポート {0}:{1} は既にインスタンス {2!r} が使用しています。", app_host, app_port, other_name))
    _check_data_dir_conflict(instances_root, config_ini_path, name=name, own_checkpoint_db=own_checkpoint_db)


def check_data_dir_conflict_for_overrides(
    instances_root: Path, config_ini_path: Path, *, name: str, overrides_tmp_path: Path
) -> None:
    """config_overrides.json 保存前の一時ファイルを反映した checkpoint_db が、
    他インスタンスと重複していないかを確認する。

    admin/overrides.py の preview()/save()/restore() から、
    load_config() 自体の検証が成功した直後に呼ぶ（2026-09-29 レビューで発見：
    設定ダッシュボードで [paths].common_data_dir を書き換えて他インスタンスと
    同じデータディレクトリを指す状態のまま保存でき、後にそのインスタンスを
    削除すると相手のデータまで消えてしまう事故があった。保存時点でここを
    塞ぐことで根本的に防ぐ）。

    Raises:
        InstanceError: 重複が見つかった場合。
    """
    cfg = load_config(config_path=config_ini_path, overrides_path=overrides_tmp_path, instance_name=name)
    _check_data_dir_conflict(instances_root, config_ini_path, name=name, own_checkpoint_db=cfg.checkpoint_db)


def create_instance(
    instances_root: Path,
    config_ini_path: Path,
    *,
    name: str,
    display_name: str | None = None,
    app_host: str = DEFAULT_APP_HOST,
    app_port: int | None = None,
    autostart: bool = False,
    headless: bool = True,
    watch: bool = False,
    copy_from: str | None = None,
    kind: str = KIND_APP,
    copy_extensions: bool = False,
) -> InstanceMeta:
    """新しいインスタンスを作成する。

    kind は KIND_APP（Locohane 本体）か KIND_SKILL_LAB（スキル研究室）。
    copy_extensions=True なら、複製元のインスタンス専用の拡張ディレクトリ
    （[skill_lab].instance_locohane_dir の skills/・agents/・evals/・LOCOHANE.md。研究室で昇格したスキル・サブエージェント）も写す。

    データの分離は config.ini 既定の common_data_dir = ./data/${instance} に
    任せる（上書きは書かない）。copy_from を指定した場合はそのインスタンスの
    config_overrides.json/.env の内容をコピーするが、common_data_dir の上書き
    だけは除く（同じデータディレクトリを2インスタンスが指す事故を防ぐ）。

    整合性チェック（ポート・データ保存先の重複）に失敗した場合は、作成した
    ディレクトリを削除してロールバックする。

    Raises:
        InstanceError: 名前が不正（NEW_NAME_MAX_LEN 文字超を含む）、既に存在する、または整合性チェック失敗。
    """
    validate_new_name(name)
    if kind not in INSTANCE_KINDS:
        raise InstanceError(_t("インスタンスの種類が不正です: {0!r}（{1} のいずれか）", kind, ', '.join(INSTANCE_KINDS)))
    if instance_dir(instances_root, name).exists():
        raise InstanceError(_t("インスタンス {0!r} は既に存在します。", name))
    if copy_from:
        # 未検証のまま instances_root / copy_from に使うと ".." 等で instances/ の
        # 外（プロジェクト直下の .env 等）をコピーできてしまうため、名前として
        # 検証し、実在するインスタンスに限る。
        validate_name(copy_from)
        if not instance_json_path(instances_root, copy_from).is_file():
            raise InstanceError(_t("複製元のインスタンス {0!r} が見つかりません。", copy_from))
    if kind == KIND_SKILL_LAB:
        # 研究室はチャット画面を持たず、待ち受けもしない。
        app_port = 0
    elif app_port is None:
        app_port = suggest_port(instances_root, app_host, exclude_ports=set())

    try:
        base_overrides: dict = {}
        if copy_from:
            src_ov = overrides_path(instances_root, copy_from)
            if src_ov.is_file():
                # 種類が違うインスタンスから複製しても、効かない設定（本体の設定をワーカーへ等）は持ち込まない。
                base_overrides = filter_overrides_for_kind(kind, json.loads(src_ov.read_text(encoding="utf-8")))

        paths_ov = base_overrides.get("paths")
        if isinstance(paths_ov, dict):
            paths_ov.pop("common_data_dir", None)
            if not paths_ov:
                del base_overrides["paths"]

        target_ov_path = overrides_path(instances_root, name)
        target_ov_path.parent.mkdir(parents=True, exist_ok=True)
        if base_overrides:
            target_ov_path.write_text(
                json.dumps(base_overrides, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
            )

        if copy_from:
            src_env = env_path(instances_root, copy_from)
            if src_env.is_file():
                shutil.copy2(src_env, env_path(instances_root, name))

        meta = InstanceMeta(
            name=name,
            display_name=display_name or name,
            app_host=app_host,
            app_port=app_port,
            autostart=autostart,
            headless=headless,
            watch=watch,
            kind=kind,
        )
        write_instance(instances_root, meta)

        check_conflicts(instances_root, config_ini_path, name=name, app_host=app_host, app_port=app_port, kind=kind)
        if copy_from and copy_extensions:
            _copy_instance_extensions(instances_root, config_ini_path, copy_from, name)
    except Exception:
        shutil.rmtree(instance_dir(instances_root, name), ignore_errors=True)
        raise
    return meta


def _instance_locohane_dir(instances_root: Path, config_ini_path: Path, name: str) -> Path:
    cfg = load_config(config_path=config_ini_path, overrides_path=overrides_path(instances_root, name), instance_name=name)
    return cfg.instance_locohane_dir


# インスタンス専用の拡張ディレクトリのうち、複製で写すもの。既定ではインスタンスの設定フォルダそのものなので、
# config_overrides.json・.env・ログ等まで写さないよう、走査対象と昇格先だけに絞る。
_EXTENSION_ENTRIES = ("skills", "agents", "evals", "LOCOHANE.md")


def _copy_instance_extensions(instances_root: Path, config_ini_path: Path, src_name: str, dst_name: str) -> None:
    """複製元のインスタンス専用の拡張ディレクトリの skills/・agents/・evals/・LOCOHANE.md を写す。"""
    src = _instance_locohane_dir(instances_root, config_ini_path, src_name)
    dst = _instance_locohane_dir(instances_root, config_ini_path, dst_name)
    if not src.is_dir() or src.resolve() == dst.resolve():
        return
    entries = [n for n in _EXTENSION_ENTRIES if (src / n).exists()]
    taken = [str(dst / n) for n in entries if (dst / n).exists()]
    if taken:
        raise InstanceError(_t("複製先のインスタンス専用ディレクトリに既にあります: {0}", ', '.join(taken)))
    dst.mkdir(parents=True, exist_ok=True)
    for n in entries:
        if (src / n).is_dir():
            shutil.copytree(src / n, dst / n, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src / n, dst / n)


def instance_extensions_summary(instances_root: Path, config_ini_path: Path, name: str) -> dict:
    """インスタンス専用の拡張ディレクトリにあるスキル・サブエージェントの一覧（削除ダイアログの警告用）。

    Returns:
        {"dir", "deleted_with_instance"（インスタンス削除で一緒に消えるか）, "skills", "agents"}。
        設定が読めない場合は dir を None にする。
    """
    try:
        ext = _instance_locohane_dir(instances_root, config_ini_path, name)
    except Exception:  # noqa: BLE001 - 一覧表示用。設定エラーは別途 list_data_paths が伝える
        return {"dir": None, "deleted_with_instance": False, "skills": [], "agents": []}
    skills_dir, agents_dir = ext / "skills", ext / "agents"
    skills = sorted(p.name for p in skills_dir.iterdir() if (p / "SKILL.md").is_file()) if skills_dir.is_dir() else []
    agents = sorted(p.stem for p in agents_dir.glob("*.md")) if agents_dir.is_dir() else []
    return {
        "dir": str(ext),
        "deleted_with_instance": ext.resolve().is_relative_to(instance_dir(instances_root, name).resolve()),
        "skills": skills,
        "agents": agents,
    }


def update_instance_meta(
    instances_root: Path,
    config_ini_path: Path,
    *,
    name: str,
    display_name: str | None = None,
    app_host: str | None = None,
    app_port: int | None = None,
    autostart: bool | None = None,
    headless: bool | None = None,
    watch: bool | None = None,
) -> InstanceMeta:
    """instance.json の一部フィールドを更新する（ポート変更時は整合性チェックを行う）。"""
    current = read_instance(instances_root, name)
    new_meta = InstanceMeta(
        name=name,
        display_name=display_name if display_name is not None else current.display_name,
        app_host=app_host if app_host is not None else current.app_host,
        app_port=app_port if app_port is not None else current.app_port,
        autostart=autostart if autostart is not None else current.autostart,
        headless=headless if headless is not None else current.headless,
        watch=watch if watch is not None else current.watch,
        kind=current.kind,
    )
    if not current.is_skill_lab and (new_meta.app_host, new_meta.app_port) != (current.app_host, current.app_port):
        check_conflicts(instances_root, config_ini_path, name=name, app_host=new_meta.app_host, app_port=new_meta.app_port)
    write_instance(instances_root, new_meta)
    return new_meta


# インスタンス削除時に一緒に削除するか選択できる永続データ（Config のフィールド名, 表示名）。
# common_data_dir を先頭に置くのは、選択されていればその配下の項目は
# まとめて消えるため（delete_instance() は存在しなくなった項目を飛ばす）。
DATA_PATH_FIELDS: tuple[tuple[str, str], ...] = (
    ("common_data_dir", "[paths] common_data_dir（データディレクトリ全体）"),
    ("checkpoint_db", "[paths] checkpoint_db（会話状態）"),
    ("thread_store_db", "[thread_store] db（スレッド一覧）"),
    ("memory_dir", "[paths] memory_dir（永続メモリー）"),
    ("plans_dir", "[paths] plans_dir（実行計画）"),
    ("log_dir", "[log] dir（アプリログ）"),
    ("chat_log_dir", "[chat_log] dir（会話ログ）"),
    ("elements_dir", "[elements] dir（添付・埋め込み画像）"),
    ("path_memory_dir", "[path_memory] dir（パスメモリー）"),
    ("vector_db_dir", "[vector_db] dir（ベクターDB。Qdrant 上のベクターは別途 Qdrant 側で削除）"),
    ("default_workdir", "[default_workdir] dir（既定の作業ディレクトリ）"),
)

# SQLite ファイル本体と一緒に消す付随ファイル（WALモードのジャーナル等）。
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass
class DataPathEntry:
    """インスタンス削除時のデータ削除候補1件。"""

    key: str
    label: str
    path: Path
    exists: bool
    # False の場合は reason に理由を入れる（UIでは選択不可として表示）。
    deletable: bool
    reason: str = ""
    # UIの初期チェック状態。<プロジェクト>/data/<name>/ 配下（既定の保存先）のみ True。
    default_selected: bool = False

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "path": str(self.path),
            "exists": self.exists,
            "deletable": self.deletable,
            "reason": self.reason,
            "default_selected": self.default_selected,
        }


def _overlaps(a: Path, b: Path) -> bool:
    """a と b が同一、またはどちらかがもう一方の配下にあるか（Windowsでは大文字小文字を区別しない）。"""
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _protected_paths(cfg) -> list[Path]:
    """そのインスタンスが使う、データ以外の（共有されうる）パス。"""
    paths = [cfg.skills_dir, cfg.agents_dir, *cfg.project_locohane_dirs, cfg.instance_locohane_dir, *cfg.bin_path]
    paths += [entry.dir for entry in cfg.allow_sandbox_dirs]
    return [p.resolve() for p in paths]


def list_data_paths(instances_root: Path, config_ini_path: Path, name: str) -> list[DataPathEntry]:
    """インスタンス削除時に一緒に削除できる永続データの候補一覧を返す。

    各候補は、次のいずれかに当たる場合は削除不可（deletable=False）にする。
    他インスタンスのデータを巻き込んで消す事故を防ぐため（2026-09-29 レビューで
    発見：common_data_dir を誤って ./data/default に向けた状態で b を削除すると
    default のデータが消えた）。

    - ドライブのルート・ホームディレクトリ・プロジェクトルート・instances_root
      自体か、その上位ディレクトリ
    - プロジェクト内にあるが <プロジェクト>/data/<name>/ の配下ではない（skills/
      等の本体ファイルや、data/ 自体・data/<他インスタンス名>/ を消さないため）
    - 他インスタンスのデータパスや、自他の skills_dir/agents_dir/
      project_locohane_dir/bin_path/allow_sandbox_dir と同一・包含関係にある
    - 他インスタンスの設定が読めず、重複を確認できない

    Raises:
        InstanceError: そのインスタンス自身の設定が読めない。
    """
    try:
        cfg = load_config(config_path=config_ini_path, overrides_path=overrides_path(instances_root, name), instance_name=name)
    except Exception as exc:  # noqa: BLE001
        raise InstanceError(_t("インスタンス {0!r} の設定を読み込めないため、データの保存先を特定できません: {1}", name, exc)) from exc

    project_root = PROJECT_ROOT.resolve()
    own_default_data = project_root / "data" / name
    guard_roots = [project_root, Path.home().resolve(), instances_root.resolve()]

    # 他インスタンスのデータパス・共有パスと、自インスタンスの共有パス（重複検出用）。
    others: list[tuple[str, Path]] = [(_t("インスタンス {0!r} 自身の共有パス", name), p) for p in _protected_paths(cfg)]
    unreadable: list[str] = []
    for other_name in list_instance_names(instances_root):
        if other_name == name:
            continue
        try:
            other_cfg = load_config(
                config_path=config_ini_path, overrides_path=overrides_path(instances_root, other_name), instance_name=other_name
            )
        except Exception:  # noqa: BLE001
            unreadable.append(other_name)
            continue
        for key, _ in DATA_PATH_FIELDS:
            others.append((_t("インスタンス {0!r} の {1}", other_name, key), getattr(other_cfg, key).resolve()))
        others += [(_t("インスタンス {0!r} の共有パス", other_name), p) for p in _protected_paths(other_cfg)]

    entries: list[DataPathEntry] = []
    for key, label in DATA_PATH_FIELDS:
        path = getattr(cfg, key).resolve()
        entry = DataPathEntry(key=key, label=_t(label), path=path, exists=path.exists(), deletable=False)
        entries.append(entry)
        if not entry.exists:
            entry.reason = _t("存在しません")
            continue
        if path == Path(path.anchor) or any(root.is_relative_to(path) for root in guard_roots):
            entry.reason = _t("ドライブのルート・ホーム・プロジェクト等の上位ディレクトリのため削除できません")
            continue
        if path.is_relative_to(project_root) and not path.is_relative_to(own_default_data):
            entry.reason = _t("プロジェクト内の data/{0}/ 配下ではないため削除できません", name)
            continue
        if unreadable:
            entry.reason = _t("インスタンス {0} の設定を読み込めず、重複を確認できないため削除できません", ", ".join(unreadable))
            continue
        conflict = next((desc for desc, other in others if _overlaps(path, other)), None)
        if conflict is not None:
            entry.reason = _t("{0} と重複しているため削除できません", conflict)
            continue
        entry.deletable = True
        entry.default_selected = path.is_relative_to(own_default_data)
    return entries


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def delete_instance(
    instances_root: Path,
    name: str,
    config_ini_path: Path | None = None,
    delete_data_keys: list[str] | tuple[str, ...] = (),
) -> list[Path]:
    """インスタンスのディレクトリ（config_overrides.json/.env/backups等）を丸ごと削除する。

    delete_data_keys に DATA_PATH_FIELDS のキーを指定すると、その永続データも
    削除する（config_ini_path 必須。削除可否は list_data_paths() で判定する）。
    データの削除に失敗した場合は、やり直せるようインスタンスのディレクトリは残す。

    呼び出し元（admin/server.py）は、事前に supervisor で稼働中でないことを
    確認してから呼ぶこと（このモジュール自体はプロセス管理を知らない）。

    Returns:
        実際に削除したデータのパス一覧。

    Raises:
        InstanceError: default インスタンスを削除しようとした、存在しない、
            削除できないデータを指定した、またはデータの削除に失敗した。
    """
    if name == DEFAULT_INSTANCE_NAME:
        raise InstanceError(_t("既定インスタンス(default)は削除できません。"))
    path = instance_dir(instances_root, name)
    if not path.is_dir():
        raise InstanceError(_t("インスタンス {0!r} が見つかりません。", name))

    targets: list[Path] = []
    if delete_data_keys:
        if config_ini_path is None:
            raise InstanceError(_t("データを削除するには config.ini のパスが必要です。"))
        entries = {e.key: e for e in list_data_paths(instances_root, config_ini_path, name)}
        for key in dict.fromkeys(delete_data_keys):
            entry = entries.get(key)
            if entry is None:
                raise InstanceError(_t("不明なデータ項目です: {0!r}", key))
            if not entry.deletable:
                raise InstanceError(_t("{0} は削除できません（{1}）: {2}", entry.label, entry.reason, entry.path))
            targets.append(entry.path)

    deleted: list[Path] = []
    # 上位ディレクトリから消し、配下の項目は存在しなくなっていれば飛ばす。
    for target in sorted(targets, key=lambda p: len(p.parts)):
        if not target.exists():
            continue
        try:
            _remove_path(target)
        except OSError as exc:
            raise InstanceError(
                _t("データの削除に失敗しました（インスタンス自体は削除していません）: {0}: {1}", target, exc)
            ) from exc
        deleted.append(target)
    shutil.rmtree(path)
    return deleted
