"""作業ディレクトリ（cwd）解決の共有ヘルパー。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
import hashlib
import shutil

import chainlit as cl

from . import _state

if TYPE_CHECKING:
    # 型注釈でのみ使用（check_work_dir_status.py との循環importを避けるため実行時はimportしない）。
    from .check_work_dir_status import WorkDirAccessStatus


def _foreign_tmp_dir_error(path: Path) -> str | None:
    """path が自セッション以外の `_tmp_<thread_id>` 配下なら拒否メッセージを返す。

    `_tmp_<thread_id>`（execute_python_code系の中間生成物置き場、
    `_resolve_exec_workdir()`）は作業ディレクトリ・default_workdir直下に
    全セッション共通の兄弟フォルダとして並ぶため、素朴にパスを許可すると
    他セッションの一時ファイルまで読めてしまう（LLMが他セッションの
    残留ファイルを自セッションの生成物と誤認する事故の原因）。

    誤検知を避けるため、「作業ディレクトリ/default_workdir 直下の
    最初の階層が `_tmp_` で始まる名前かどうか」だけを見る（パスの
    どこかに `_tmp_` を含む文字列があるかではない）。無関係な場所に
    たまたま `_tmp_` で始まる名前のフォルダがあっても影響しない。

    さらに、この階層が実際にディレクトリである場合のみ拒否する
    （`_foreign_tmp_dir_names()` と同じ条件）。`_resolve_exec_workdir()`
    が作るセッション作業フォルダは常にディレクトリであり、ファイルには
    なりえないため、LLMが作業ディレクトリ直下に `_tmp_ops.json` のような
    `_tmp_` で始まる名前の**ファイル**を自分で作成した場合まで誤って
    「他セッションの一時ディレクトリ」として読み取り拒否してしまう回帰が
    あった（2026-08-22 発見・修正）。

    `_tmp_<thread_id>` は常に `_state._DEFAULT_WORKDIR` 直下にのみ作られる
    （`_resolve_exec_workdir()` docstring参照）ため、ここでは `primary` に
    `_resolve_workdir()` を使わない（work_dir未設定時、`_resolve_workdir()`
    自体が `_resolve_exec_workdir()` を返すようになった＝呼ぶだけで自分の
    `_tmp_<thread_id>` を新規作成・シードする副作用があり、単なる除外判定の
    ためだけに無関係な場所でこの副作用を起こしてしまうため。2026-08-29発見・
    修正）。

    Args:
        path: 解決済みの絶対パス（未解決でも内部で resolve() する）。

    Returns:
        他セッションの一時ディレクトリ配下と判定できればエラー文字列、
        そうでなければ None。
    """
    own_name = f"_tmp_{_exec_tmp_name()}"
    try:
        resolved = path.resolve()
    except OSError:
        return None
    for parent in _tmp_dir_parents(_state._DEFAULT_WORKDIR):
        try:
            rel = resolved.relative_to(parent)
        except ValueError:
            continue
        if not rel.parts:
            continue
        first = rel.parts[0]
        if first.startswith("_tmp_") and first != own_name:
            try:
                is_dir = (parent / first).is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                return f"Error: the temporary directory of another session ({first}) cannot be read."
        return None
    return None

def _foreign_tmp_dir_names() -> frozenset[str]:
    """自セッション以外の `_tmp_<thread_id>` ディレクトリ名の集合を返す。

    Glob/Grep が作業ディレクトリ本体などの祖先から再帰検索する際、
    他セッションの `_tmp_<X>` サブツリーだけを走査・結果から除外するために
    glob_tool.py の `glob_search`/grep_tool.py の `grep_search` の
    `exclude_names` へ渡す。

    `_foreign_tmp_dir_error()` と同じ理由で、`primary` に `_resolve_workdir()`
    ではなく `_state._DEFAULT_WORKDIR` を直接使う（副作用のあるフォルダ
    新規作成を、単なる除外判定のためだけに起こさないため）。
    """
    own_name = f"_tmp_{_exec_tmp_name()}"
    names: set[str] = set()
    for parent in _tmp_dir_parents(_state._DEFAULT_WORKDIR):
        try:
            entries = list(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith("_tmp_") and entry.name != own_name and entry.is_dir():
                names.add(entry.name)
    return frozenset(names)

def _resolve_workdir(need_write: bool = False) -> Path:
    """run_script/Glob/Read等が使う「現在の作業ディレクトリ」を決定する。

    Chainlit の ChatSettings（独自フロントエンドには表示されないが
    on_settings_update の経路自体は残っている）でユーザーがセッションに
    作業ディレクトリを設定していればそれを使う（app.py の
    on_settings_update が cl.user_session["work_dir"] に絶対パス文字列を
    保存する）。

    **未設定、またはユーザー指定のwork_dirが使えない（存在しない・読めない・
    ［need_write時は］書き込めない）場合は、常にこのスレッド専用フォルダ
    `_resolve_exec_workdir()`（`_tmp_<thread_id>`）へフォールバックする。**
    以前はここで config.ini の [default_workdir].dir（_state._DEFAULT_WORKDIR、
    全スレッド共通の共有フォルダ）を直接返していたが、これだと無関係な
    別スレッド（や外部から直接置かれたファイル）を、パス省略時の既定検索先
    としてそのまま拾ってしまい、意図しないデータでタスクが壊れる事故が
    起こりうる。`_tmp_<thread_id>` はスレッドごとに独立しており、初回作成時に
    その時点の default_workdir 直下の中身がコピーされる（`_resolve_exec_workdir()`
    docstring参照）ため、ツールバーで作業フォルダを指定しない素朴な使い方
    （default_workdir 直下に直接ファイルを置いて質問する）も引き続き成立する。

    default_workdir 直下そのものへ明示的に絶対パスを指定してのアクセス
    （Read/Glob等の path 引数に直接渡す）は制限しない。ここで変えるのは
    「パス省略時に自動的にどこを見るか」という既定値だけである。

    read_skill / read_skill_file / スクリプト本体の場所解決には影響しない
    （それらは常に _safe_path 経由で skills ルート配下に固定される）。

    Args:
        need_write: True の場合、書き込み可否（status.writable）も見て
            フォールバック判定する。既存ファイルの読み取りのみが目的の
            呼び出し元は False のままでよい。

    Returns:
        呼び出し元が使う絶対パス。work_dir が未設定、またはアクセス不可・
        （need_write時は）書き込み不可と判定されていれば
        `_resolve_exec_workdir()`（`_tmp_<thread_id>`）。

    Raises:
        RuntimeError: init_tools() が未実行で _state._DEFAULT_WORKDIR が None の場合。
    """
    if _state._DEFAULT_WORKDIR is None:
        raise RuntimeError("init_tools() has not been run")
    work_dir = cl.user_session.get("work_dir")
    if not work_dir:
        return _resolve_exec_workdir()
    status: WorkDirAccessStatus | None = cl.user_session.get("work_dir_access")
    if status is not None:
        if not status.exists or not status.readable:
            return _resolve_exec_workdir()
        if need_write and not status.writable:
            return _resolve_exec_workdir()
    return Path(work_dir)


def _build_workdir_status_info(work_dir: str | None, status: "WorkDirAccessStatus | None") -> dict:
    """作業ディレクトリの状態を表す辞書を組み立てる（LLMへ状態を伝える共通の元データ）。

    `check_work_dir_status`ツールと、会話スレッド開始時に自動注入される
    `app.py`の`_build_work_dir_notice()`の両方がこの関数を呼ぶ。以前は
    この2箇所が独自にほぼ同じ判定ロジックを別々に重複実装しており、
    食い違った（かつ両方とも`state`の意味が矛盾する誤った）情報をLLMへ
    伝えていた（2026-08-29 発見）。ロジックを1箇所に集約することで、
    今後どちらか一方だけを直して食い違う事故を防ぐ。

    `state`は常に「`absolute_path`へ直接読み書きできるか」だけを表す
    （`"read_write"`ならできる、それ以外（`"read_only"`/`"unreadable"`/
    `"not_found"`）はできない）。`write_dir`は常に存在し、直接書き込み
    できる場合は`absolute_path`と同じ値、できない場合はスレッド専用の
    `_resolve_exec_workdir()`の絶対パスになる。書き込み判断には常に
    `write_dir`だけを見ればよく、`state`の値を書き込み可否の判断に使う
    必要はない。

    Args:
        work_dir: `cl.user_session.get("work_dir")`の値（未設定なら
            `None`または空文字）。
        status: `work_dir`設定済みの場合の実測アクセス結果
            （`probe_workdir_access()`の戻り値）。未設定、または実測が
            まだ済んでいない場合は`None`（`None`の場合は楽観的に
            `read_write`とみなす。既存の呼び出し元の挙動を維持）。

    Returns:
        `absolute_path`/`state`/`source`/`write_dir`/`description`を持つ
        辞書。`work_dir`設定済みでアクセス不可（`not_found`/`unreadable`）
        の場合のみ、読み取りフォールバック先を示す`read_dir`も追加する。
    """
    if not work_dir:
        resolved = str(_resolve_exec_workdir())
        return {
            "absolute_path": resolved,
            "state": "read_write",
            "source": "default",
            "write_dir": resolved,
            "description": "You can read and write absolute_path directly. write_dir is the same as absolute_path.",
        }
    if status is None or (status.exists and status.readable and status.writable):
        return {
            "absolute_path": work_dir,
            "state": "read_write",
            "source": "user_changed",
            "write_dir": work_dir,
            "description": "You can read and write absolute_path directly. write_dir is the same as absolute_path.",
        }
    write_dir = str(_resolve_exec_workdir())
    info = {"absolute_path": work_dir, "source": "user_changed", "write_dir": write_dir}
    if not status.exists:
        info["state"] = "not_found"
        info["read_dir"] = str(_state._DEFAULT_WORKDIR)
        info["description"] = "absolute_path does not exist. Use read_dir for reading and write_dir for writing."
    elif not status.readable:
        info["state"] = "unreadable"
        info["read_dir"] = str(_state._DEFAULT_WORKDIR)
        info["description"] = "absolute_path is not accessible. Use read_dir for reading and write_dir for writing."
    else:
        info["state"] = "read_only"
        info["description"] = (
            "absolute_path is read-only. Write to write_dir. Keep reading from absolute_path. "
            "When editing an existing file, output to write_dir and report its absolute path."
        )
    return info


# `_tmp_<name>` に使う thread_id の短縮ID（SHA-1 16進）の桁数。10桁（40bit）あれば
# default_workdir 直下に並ぶスレッド数で衝突する心配はまず無い。
_EXEC_TMP_ID_LEN = 10


def _short_thread_id(thread_id: str) -> str:
    """thread_id を `_tmp_<name>` 用の短い固定長IDにする（同じ thread_id なら常に同じ値）。"""
    return hashlib.sha1(thread_id.encode("utf-8")).hexdigest()[:_EXEC_TMP_ID_LEN]


def _exec_tmp_name() -> str:
    """`_tmp_<name>` の `<name>` 部分を返す（`<作成時刻>_<thread_id の短縮ID>`）。

    以前は thread_id（UUID等）をそのまま使っていたため、default_workdir
    直下に並ぶ `_tmp_*` フォルダをファイラーで見ても作成順に並ばず、
    調査時にどれが最新か分かりにくいという問題があった。作成時刻を
    先頭に付けることで、名前順ソート＝作成順になるようにする。

    同一スレッド（同一プロセス内）では `_state._EXEC_TMP_NAME_CACHE` に
    一度決めた値をキャッシュし、以後は同じ値を返す
    （セッション終了時は `forget_session_tool_semaphores()` が片付ける）。
    キャッシュしない場合、
    `_subprocess_env()` が環境変数へ渡す値を計算する呼び出しと、
    `_resolve_exec_workdir()` がディレクトリ実体を作る呼び出しが、いずれも
    このタイムスタンプ生成を経由するため、ごく僅かなタイミング差で
    互いに異なる名前を計算してしまい、サブプロセス側の書き込みガード
    （`_python_fs_guard.py` の `_GUARD_OWN_TMP_NAME`）と実際に作られる
    ディレクトリ名が食い違って自分のディレクトリへの書き込みがブロック
    される事故があった（2026-08-26 発見・修正）。

    プロセス再起動やスレッド再開でキャッシュが空でも、default_workdir
    直下に既に `_tmp_<何か>_<短縮ID>` が存在すればその名前を探して
    再利用する（毎回時刻を変えると、スレッド再開のたびに別の
    ディレクトリへ分裂してしまうため）。旧バージョンが作った
    `_tmp_<何か>_<thread_id>`・プレフィックス無しの `_tmp_<thread_id>` も
    `endswith(f"_{thread_id}")` で拾い、そのまま追記先として使う（後方互換）。

    以前は `<YYYYMMDD_HHMMSS_ミリ秒>_<thread_id（UUID 36文字）>` と長く、
    `_tmp_` を含めフォルダ名だけで約65文字あったため、default_workdir が
    深い場所にあったり配下でスキルがさらに入れ子のフォルダ・長いファイル名を
    作ったりすると、Windows のパス長上限（260文字）を超えて書き込みに
    失敗しやすかった。時刻は秒まで（`YYMMDDHHMMSS`）に、thread_id は
    SHA-1 の先頭 `_EXEC_TMP_ID_LEN` 桁に縮める（フォルダ名は28文字）。

    Returns:
        `_tmp_` の直後に続く名前。キャッシュにも既存ディレクトリにも
        見つからなければ `<YYMMDDHHMMSS>_<短縮ID>` を新規に生成して
        キャッシュする（ディレクトリ自体はここでは作成しない）。
    """
    thread_id = cl.user_session.get("thread_id") or "_no_session"
    cached = _state._EXEC_TMP_NAME_CACHE.get(thread_id)
    if cached is not None:
        return cached
    short_id = _short_thread_id(thread_id)
    name = None
    if _state._DEFAULT_WORKDIR is not None:
        try:
            for entry in _state._DEFAULT_WORKDIR.iterdir():
                if (
                    entry.name.startswith("_tmp_")
                    and entry.name.endswith((f"_{short_id}", f"_{thread_id}"))
                    and entry.is_dir()
                ):
                    name = entry.name[len("_tmp_"):]
                    break
        except OSError:
            pass
    if name is None:
        stamp = datetime.now().strftime("%y%m%d%H%M%S")
        name = f"{stamp}_{short_id}"
    _state._EXEC_TMP_NAME_CACHE[thread_id] = name
    return name


def _seed_exec_workdir(dest: Path) -> None:
    """新規作成したスレッド専用フォルダへ、default_workdir直下の既存の中身をコピーする。

    ツールバーで作業フォルダを指定していないユーザーが、default_workdir
    直下に直接置いた既存ファイル（アプリを経由しない外部からの配置、
    以前のセッションでの生成物等）を、最初のメッセージから参照できる
    ようにするため。コピーは `_resolve_exec_workdir()` がこのフォルダを
    初めて作成する瞬間にのみ行われ、以降このフォルダ内で完結するため、
    他スレッドの後続の変更が漏れてくることも、このスレッドの変更が他
    スレッドへ漏れることもない（コピー元・コピー先のどちらも他スレッドの
    `_tmp_*` フォルダは対象外）。

    Args:
        dest: コピー先（作成直後の自スレッド専用フォルダ）。
    """
    if _state._DEFAULT_WORKDIR is None:
        return
    try:
        entries = list(_state._DEFAULT_WORKDIR.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("_tmp_"):
            continue
        target = dest / entry.name
        try:
            if entry.is_dir():
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)
        except OSError:
            continue


def _resolve_exec_workdir() -> Path:
    """execute_python_code / run_script が中間生成物を書く実行用ディレクトリ。

    常に default_workdir 配下に `_tmp_<thread_id>` を作って返す（無ければ
    作成する）。`_resolve_workdir()` がwork_dir未設定・アクセス不可時の
    フォールバック先としても使う（このディレクトリが「今の作業ディレクトリ」
    そのものになる）ため、LLMがコード内で相対パスで書き出すファイル
    （ops.json 等の中間生成物）も、Glob/Read等の既定検索先も、ここに
    一本化される。`_tmp/<thread_id>` のような親子階層ではなく
    `_tmp_<thread_id>` という単一のディレクトリ名にしているのは、
    日数ベースの自動削除（cleanup_old_dirs、app.py）が丸ごと rmtree した際、
    親ディレクトリ（`_tmp`）が空のまま残り続ける問題を避けるため。

    このスレッドで初めてこのフォルダを作成する場合（`d.exists()`が
    `False`だった場合）、`_seed_exec_workdir()` で default_workdir 直下の
    既存の中身をコピーする。スレッド再開時に既存フォルダを再利用する
    場合は再コピーしない（そのスレッドがそれまでに行った変更を上書き
    しないため）。

    以前はユーザー指定の work_dir 直下に作っていたが、生成中に別スレッドへ
    切り替えるとソケット切断（on_chat_end）で即座に rmtree され、裏で
    継続中の処理と競合する問題があった。default_workdir 固定にすることで
    on_chat_end での即時削除自体を廃止し、default_workdir_retention_days
    による日数ベースの自動削除（app.py）に一本化した（work_dir は元々
    サーバー/クライアントでファイルシステムが分離しうるため書き込み不可の
    こともあり得るが、default_workdir はサーバー側の設定のため常に
    書き込み可能という前提が置ける）。

    Returns:
        `_tmp_<thread_id>` ディレクトリの絶対パス。
    """
    d = _state._DEFAULT_WORKDIR / f"_tmp_{_exec_tmp_name()}"
    is_new = not d.exists()
    d.mkdir(parents=True, exist_ok=True)
    if is_new:
        _seed_exec_workdir(d)
    return d

def _tmp_dir_parents(primary: Path) -> list[Path]:
    """`_tmp_<thread_id>` が実際に作られうる親ディレクトリの一覧を返す。

    `_resolve_exec_workdir()` は通常 `primary`（呼び出し元が渡す実行用
    ディレクトリの親、または run_script の cwd そのもの）配下に
    `_tmp_<thread_id>` を作るが、mkdir失敗時は `_state._DEFAULT_WORKDIR` 配下へ
    フォールバックする（1402-1408行目）。他セッションの `_tmp_<X>` 検出
    （`_foreign_tmp_dir_error`/`_foreign_tmp_dir_names`/ガードプリアンブルの
    `tmp_dir_roots`）は、このフォールバック先も含めて両方を見る必要がある。

    Args:
        primary: 実行用ディレクトリの親（execute_python_code系）、または
            run_script の cwd（run_script系。この場合それ自体が親）。

    Returns:
        重複・非存在を除いた既存ディレクトリのリスト。
    """
    candidates = [primary]
    if _state._DEFAULT_WORKDIR is not None:
        candidates.append(_state._DEFAULT_WORKDIR)
    seen: set[Path] = set()
    result: list[Path] = []
    for c in candidates:
        try:
            resolved = c.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        result.append(resolved)
    return result
