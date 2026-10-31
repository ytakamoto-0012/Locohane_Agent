"""インスタンス別 .env（instances/<name>/.env）とプロジェクト直下 .env の読み書き。

管理対象は AUTH_USERS（ログインユーザー）・CHAINLIT_AUTH_SECRET（JWT署名鍵）・
任意の追加環境変数。書き込みには python-dotenv の set_key/unset_key を使う
（管理対象外の行・コメントはそのまま残る。src.config._parse_auth_users と
互換な "[["user","pass"], ...]" 形式で AUTH_USERS を直列化する）。

このモジュール自体はパスワードをログ出力しない。呼び出し元（admin/server.py）
側でも、このモジュールが返す値をAPIレスポンスへそのまま載せないこと
（パスワードは書き込み専用として扱う。README_DETAIL.md「.envとユーザー管理」参照）。
"""

from __future__ import annotations

import json
from pathlib import Path

import dotenv

from src.config import _parse_auth_users
from admin.i18n import t as _t

# .env に書き込む際、値にこれらのいずれかを含むキー名は「機密」として扱う
# （admin/audit.py の is_sensitive_key と同じ基準）。
AUTH_USERS_KEY = "AUTH_USERS"
CHAINLIT_AUTH_SECRET_KEY = "CHAINLIT_AUTH_SECRET"
# .env 上でこれらは admin/server.py の専用エンドポイント経由でのみ操作する
# （env の汎用キー一覧編集エンドポイントからは隠す。誤って二重管理しない）。
MANAGED_KEYS = frozenset({AUTH_USERS_KEY, CHAINLIT_AUTH_SECRET_KEY})
# インスタンス .env に置くと管理ツール側の判定と食い違うため、汎用の環境変数
# 編集からは設定させないキー。インスタンス .env は app.py が起動後に
# override=True で読むが、管理ツール（ポート・データ保存先の重複チェック、
# app.lock による稼働判定、削除時のデータディレクトリ判定）は .env を読まずに
# load_config() するため、これらで保存先やインスタンス名を変えると検知・保護が
# 効かなくなる（2026-09-29 レビューで発見）。データ保存先は設定画面の
# [paths] から変更させる（そちらは重複チェックされる）。
UNTRACKABLE_KEYS = frozenset(
    {"LOCOHANE_INSTANCE", "LOCOHANE_INSTANCE_ENV", "CONFIG_OVERRIDES_PATH", "COMMON_DATA_DIR", "CHECKPOINT_DB"}
)


class EnvFileError(Exception):
    """env_files.py の操作失敗。"""


def read_values(path: Path) -> dict[str, str]:
    """.env を読み、{キー: 値} を返す（値は生文字列。パースはしない）。存在しなければ空dict。"""
    if not path.is_file():
        return {}
    return dict(dotenv.dotenv_values(str(path)))


def read_users(path: Path) -> dict[str, str]:
    """.env の AUTH_USERS を {ユーザー名: パスワード} で返す（無ければ空dict）。"""
    values = read_values(path)
    raw = values.get(AUTH_USERS_KEY, "")
    return _parse_auth_users(raw)


def _serialize_users(users: dict[str, str]) -> str:
    """{ユーザー名: パスワード} を AUTH_USERS の値（1行、JSON形式）へ直列化する。

    JSON形式の二重引用符は ast.literal_eval（_parse_auth_users が使う）でも
    そのまま解釈できるため互換性がある。1行にまとめるのは、python-dotenv の
    複数行クォート形式を書き込み側で再現する複雑さを避けるため
    （手動で見やすい複数行形式に書き換えたい場合は、ユーザーが直接 .env を
    編集すればよい。その場合も読み込み側は問題なく解釈できる）。
    """
    return json.dumps([[name, password] for name, password in users.items()], ensure_ascii=False)


def write_users(path: Path, users: dict[str, str]) -> None:
    """AUTH_USERS を丸ごと書き換える（追加・削除・パスワード変更、全て呼び出し元でdictを組み立てて渡す）。

    users が空の場合は AUTH_USERS キー自体を削除する（空リストのまま残すと
    「意図的に誰もログインできない」のか「未設定」なのか紛らわしいため）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if users:
        # quote_mode="always": "never"（クォート無し）だと、パスワードに " "（空白）や
        # "#"（コメント開始と誤認）を含む場合、python-dotenv の再パース時に値が
        # 途中で切れてしまう（2026-09-29 レビューで発見・実機検証済み。例:
        # "pa #ss" が "pa" までしか読み戻せなかった）。常にクォートすることで
        # 空白・#・改行・引用符を含む値でも安全にラウンドトリップする。
        dotenv.set_key(str(path), AUTH_USERS_KEY, _serialize_users(users), quote_mode="always")
    else:
        if path.is_file():
            dotenv.unset_key(str(path), AUTH_USERS_KEY)


def read_auth_secret(path: Path) -> str:
    """CHAINLIT_AUTH_SECRET の現在値（無ければ空文字列）。"""
    return read_values(path).get(CHAINLIT_AUTH_SECRET_KEY, "")


def write_auth_secret(path: Path, secret: str) -> None:
    """CHAINLIT_AUTH_SECRET を書き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # quote_mode="always" の理由は write_users() 参照（secrets.token_urlsafe()
    # 由来の値のため実害は薄いが、統一しておく）。
    dotenv.set_key(str(path), CHAINLIT_AUTH_SECRET_KEY, secret, quote_mode="always")


def read_extra_vars(path: Path) -> dict[str, str]:
    """AUTH_USERS/CHAINLIT_AUTH_SECRET を除く、その他の環境変数一覧。"""
    values = read_values(path)
    return {k: v for k, v in values.items() if k not in MANAGED_KEYS}


def write_extra_var(path: Path, key: str, value: str) -> None:
    """MANAGED_KEYS 以外の任意の環境変数を1つ設定する。"""
    if key in MANAGED_KEYS:
        raise EnvFileError(_t("{0} は専用のエンドポイントから操作してください。", key))
    if not key or not key.isascii() or not key.replace("_", "").isalnum() or key[0].isdigit():
        raise EnvFileError(_t("環境変数名として不正です: {0!r}", key))
    if key.upper() in UNTRACKABLE_KEYS:
        raise EnvFileError(
            _t("{0} はインスタンスの .env では設定できません（管理ツールの重複チェック・稼働判定と食い違うため）。データ保存先は設定画面の [paths] から変更してください。", key)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # quote_mode="always" の理由は write_users() 参照。
    dotenv.set_key(str(path), key, value, quote_mode="always")


def delete_extra_var(path: Path, key: str) -> None:
    """MANAGED_KEYS 以外の環境変数を1つ削除する。"""
    if key in MANAGED_KEYS:
        raise EnvFileError(_t("{0} は専用のエンドポイントから操作してください。", key))
    if path.is_file():
        dotenv.unset_key(str(path), key)
