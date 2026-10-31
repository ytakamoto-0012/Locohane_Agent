"""execute_python_code系の書き込みサンドボックスガード生成。"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

from .. import path_memory
from .. import skill_drafts

from . import _state
from ._workdir import _resolve_exec_workdir, _resolve_workdir


def _register_exec_output_files(workdir: Path, before_snapshot: dict[Path, float], thread_id: str) -> str:
    """execute_python_code の実行前後で workdir 直下のファイル差分を検知し、
    新規作成/更新されたファイルを path_memory へ自動登録する。

    LLMが execute_python_code のコード内で相対パス書き込みしたファイル
    （中間生成物）を、後続の run_script 等へ渡す際にLLMが絶対パスを手で
    組み立て直す必要が無いようにするため（cwdが `_tmp_<thread_id>` に
    切り替わったことをLLMが意識しなくて済む）。

    Args:
        workdir: execute_python_code が使った実行用ディレクトリ
            （_resolve_exec_workdir() の戻り値）。
        before_snapshot: 実行前に取得した {ファイルパス: mtime} のスナップショット。
        thread_id: path_memory への登録に使うセッションID。
            execute_python_code_background 経由の呼び出しでは
            check_script_job 呼び出し時の cl.user_session とジョブ起動時の
            セッションが一致する保証に頼らず、job.thread_id を明示的に渡す。

    Returns:
        新規作成/更新ファイルがあれば「[生成/更新ファイル]」見出し付きの
        文字列（1行1ファイル、`@N ファイル名（新規作成|更新）` 形式）。
        対象ファイルが無い場合は空文字列。パスメモリーへ登録できなかった
        ファイルは絶対パスをそのまま表示する。
    """
    try:
        after_files = [p for p in workdir.iterdir() if p.is_file()]
    except OSError:
        return ""

    changed: list[tuple[Path, str]] = []
    for p in after_files:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        prev = before_snapshot.get(p)
        if prev is None:
            changed.append((p, "created"))
        elif mtime != prev:
            changed.append((p, "updated"))
    if not changed:
        return ""

    lines = []
    for p, kind in changed:
        index = None
        if _state._PATH_MEMORY_DIR is not None:
            index = path_memory.register(
                thread_id,
                str(p),
                _state._PATH_MEMORY_DIR,
                _state._PATH_MEMORY_MAX_ENTRIES,
                description=f"{kind} by execute_python_code",
            )
        if index is not None:
            lines.append(f"@{index} {p.name} ({kind})")
        else:
            lines.append(f"{p} ({kind}; absolute path used as is because path memory registration failed)")
    return "[Created/updated files]\n" + "\n".join(lines)


def _exec_guard_roots() -> tuple[list[Path], list[Path]]:
    """execute_python_code系（execute_python_code/execute_python_code_background）
    のガードに渡す (allowed_roots, display_roots) を組み立てる。

    実際の作業ディレクトリ（_resolve_workdir(need_write=True)。work_dir未設定・
    書き込み不可等の場合は自動的に `_tmp_<thread_id>` へ倒れる。
    _resolve_workdir()自体がこのフォールバックを行うため、ここで二重に
    縮小する必要はない）・`_tmp_<thread_id>`（_resolve_exec_workdir()。
    work_dir が有効な場合でも常に念のため加える）に加え、
    path_memory_dir（LLM生成コードが path_memory.register()/resolve() を
    直接呼ぶ場合にロックファイル書き込みが必要になるLocohane内部の
    状態ディレクトリ）を allowed_roots に含める。

    config.ini [default_workdir].allow_sandbox_dir（_state._ALLOW_SANDBOX_DIRS）
    はここでは対象外（execute_python_code系は対象外。run_script/
    run_script_background 経由のみ、_subprocess_env.py の
    _run_script_guard_env() が扱う）。default_workdir自体
    （`_tmp_<thread_id>` 以外のサブディレクトリや直下）は意図的に含めない
    （default_workdirはサーバー側の共有ディレクトリのため、他セッションが
    誤参照する事故を避けるため常に自セッション専用の`_tmp_<thread_id>`
    だけに書き込みを限定する。_resolve_workdir()参照）。
    4箇所の呼び出し元での重複を避けるための共通ヘルパー。

    display_roots は allowed_roots から path_memory_dir を除いたもの。
    書き込みガード違反時にLLMへ「ここに書き直せ」と提示する候補は
    path_memory_dir（ロックファイル専用でユーザー成果物の置き場ではない）を
    含めるべきではないため分離している。

    以前は実行用ディレクトリ（_tmp_<thread_id>）の親から作業ディレクトリを
    逆算していたが、_tmp_<thread_id> が常に default_workdir 配下に固定
    されたため（_resolve_exec_workdir() 参照）、ここで独立に解決する
    必要がある（そうしないと実際の work_dir が allowed_roots から漏れ、
    execute_python_code がユーザー指定の作業ディレクトリへ書き込めなく
    なってしまう）。
    """
    roots = [_resolve_workdir(need_write=True), _resolve_exec_workdir()]
    display_roots = list(roots)
    if _state._PATH_MEMORY_DIR is not None:
        roots.append(_state._PATH_MEMORY_DIR)
    return roots, display_roots


def _library_cache_roots() -> list[Path]:
    """ガードの許可先・禁止先に関わらず、常に書き込みを許可するライブラリ内部のキャッシュ。

    pywin32 の COM 型情報キャッシュ（`%TEMP%\\gen_py`、win32com/__init__.py の
    __gen_path__）。`import win32com.client` 時にキャッシュ（dicts.dat）が
    無いと再構築のため書き込むため、Temp の掃除等でキャッシュが消えていると
    excel-render 等の COM を使うスキルが import 時点で必ずガードに阻まれる
    （2026-10-01 発見）。ユーザー成果物の置き場ではないため、LLM へ案内する
    display_roots には含めない。
    """
    return [Path(tempfile.gettempdir()) / "gen_py"]


def _python_fs_guard_preamble(
    allowed_roots: Sequence[Path],
    tmp_dir_roots: Sequence[Path] = (),
    display_roots: Sequence[Path] | None = None,
    deny_roots: Sequence[Path] | None = None,
    write_deny_roots: Sequence[Path] | None = None,
    list_deny_roots: Sequence[Path] | None = None,
) -> str:
    """execute_python_code / run_script が実行するコードの先頭（または
    サブプロセスの sitecustomize.py）に連結する、書き込みサンドボックス用の
    ガードコードを生成する。

    LLMが生成したコードや、スキルの scripts/ 配下のスクリプトは絶対パスや
    `..` で任意の場所へ書き込めてしまい、cwd を作業用ディレクトリに絞る
    だけでは他ドライブやプロジェクト本体を誤って書き換える事故を防げない。
    ここで生成するコードは、サブプロセス内で `open`/`os`/`shutil` の
    書き込み・削除・改名系関数をモンキーパッチし、allowed_roots（作業
    ディレクトリと default_workdir）配下以外への操作を場所を問わず
    ブロックする（原則「書き込みは常にサンドボックス配下限定」。以前は
    Locohaneのプロジェクトフォルダだけを保護し、それ以外のドライブ・
    フォルダへの書き込みは無制限に許可していたが、それでは
    「作業ディレクトリの外に書き込まれる」事故を防げないため、常に
    allowed_roots 配下限定へ強化した）。悪意ある回避（ctypes直叩き等）
    までは防げないベストエフォートのガードであり、あくまで「LLMが
    悪気なく意図しない場所に書き込んでしまう」事故防止が目的。

    加えて、`subprocess.Popen`（`run`/`call`/`check_call`/`check_output`
    もこれを経由する）・`os.system`・`os.popen` をモンキーパッチし、
    コマンド名（basename、拡張子は無視）が git / npm / pip / pip3 、
    および copy / xcopy / move / robocopy / del / erase / ren / rename /
    rd / rmdir 等のファイル操作コマンド、cmd / cmd.exe / powershell / pwsh
    等のシェルラッパーのいずれかに一致する場合は場所を問わず
    PermissionError にする。生成・実行させたコードが誤ってリポジトリ操作や
    パッケージインストールを行う事故を防ぐことに加え、`open`/`os`/`shutil`
    への書き込みガードを `os.system("copy ...")` 等のシェル経由で回避
    されるのを防ぐ（tune-prompt iter1でsystem_prompt_scale/002実行中、
    LLMが実際にこの手口で allowed_roots 外へファイルをコピーすることに
    成功した事例を確認したため追加。`cmd /c copy ...` のような多段の
    シェルラッパー経由でのコマンド名偽装までは防げないベストエフォートの
    対策）。こちらは allowed_roots による除外はない（常に全面禁止）。

    さらに tmp_dir_roots 配下（`_tmp_<thread_id>`。execute_python_code系の
    中間生成物置き場、`_resolve_exec_workdir()`）については、自セッション
    以外の `_tmp_<X>` への読み取り（`open()`）・書き込み・削除・改名を
    allowed_roots の内外を問わず一律ブロックする（他セッションの一時
    ファイルをLLM生成コードが誤って読む・書き換える事故の防止。
    ディレクトリ一覧取得 `os.listdir`/`os.scandir`/`Path.iterdir`/`os.walk`
    はv1では対象外）。自セッション自身の `_tmp_<own>` はこれまで通り
    無制限に読み書きできる。

    Args:
        allowed_roots: 書き込み・削除を許可するディレクトリの一覧
            （実行用ディレクトリと `_tmp_<thread_id>`。呼び出し元
            （_exec_guard_roots/_run_script_guard_env）が使う
            _resolve_workdir() 自体が、work_dir未設定・書き込み不可等の
            場合にdefault_workdir自体ではなく`_tmp_<thread_id>`を返すため、
            ここに素のdefault_workdirが渡ることはない）。
        tmp_dir_roots: `_tmp_<thread_id>` が実際に作られうる親ディレクトリの
            一覧（`_tmp_dir_parents()` の戻り値）。他セッション判定にのみ
            使い、allowed_roots とは独立（execute_python_code_readonly は
            allowed_roots=[] で全面書き込み禁止だが、他セッションtmp判定
            自体はここに渡す tmp_dir_roots で別途機能する）。
        display_roots: 書き込みガード違反時にLLMへ「ここに書き直せ」と
            提示する候補の一覧。省略時は allowed_roots をそのまま使う。
            allowed_roots には path_memory_dir 等、内部用でユーザー
            成果物の置き場として案内すべきでないパスが混じることがあるため
            分離できるようにしている（_exec_guard_roots/
            _run_script_guard_env 参照）。重複は自動的に除去される。
        deny_roots: 読み取り（`open()`・コピー元）も含めて一律ブロックする
            ディレクトリの一覧。会話のユーザーが読めない他ユーザーのドラフト
            スキル（src/skill_drafts.py の unreadable_dirs()）に使う。
            省略時（None）はその場で unreadable_dirs() を求めるため、
            呼び出し元が個別に渡す必要は無い。`open()` に加えて
            `os.listdir`/`os.scandir`（`os.walk`・`glob`・`Path.iterdir` も
            これを経由する）と低水準の `os.open` も対象にする。
        write_deny_roots: allowed_roots 配下であっても書き込み・削除を
            ブロックするディレクトリの一覧。会話のユーザーが書き込めない
            他ユーザーのドラフトスキル（src/skill_drafts.py の
            unwritable_dirs()）に使う（作業ディレクトリが draft_dir を含む
            場合でも readable モード等で他人のドラフトを書き換えさせない）。
            省略時（None）はその場で unwritable_dirs() を求める。
        list_deny_roots: deny_roots のうち、ルート自体の一覧取得（直下の名前）も
            ブロックするもの（src/skill_drafts.py の unlistable_dirs()）。
            deny_roots の中でもここに無いルート（listed モードの他ユーザーの
            フォルダ）は、直下のスキル名の一覧だけ取得でき、その中は取得できない。
            省略時（None）はその場で unlistable_dirs() を求める。

    Returns:
        コード文字列の先頭に連結する、あるいは sitecustomize.py として
        そのまま配置できるモンキーパッチ処理のPythonソース。呼び出し元の
        コード自体には何も変更を加えない。
    """
    # tuple の repr をそのまま埋め込む（旧実装は ", ".join(...) を "(...,)" で
    # 囲んでいたため、allowed_roots が空リストのとき ("",) という「空文字列
    # 1件を含むタプル」になってしまい、os.path.realpath("") がカレント
    # ディレクトリを指す結果、書き込みガードが実質無効化される不具合が
    # あった。repr(tuple(...)) なら空リストは正しく "()" になる。
    allowed_repr = repr(tuple(str(p) for p in allowed_roots))
    tmp_roots_repr = repr(tuple(str(p) for p in tmp_dir_roots))
    display_repr = repr(tuple(str(p) for p in (display_roots if display_roots is not None else allowed_roots)))
    lib_cache_repr = repr(tuple(str(p) for p in _library_cache_roots()))
    if deny_roots is None:
        deny_roots = skill_drafts.unreadable_dirs(_state._LLM_CONFIG, skill_drafts.current_draft_user())
    deny_repr = repr(tuple(str(p) for p in deny_roots))
    if write_deny_roots is None:
        write_deny_roots = skill_drafts.unwritable_dirs(_state._LLM_CONFIG, skill_drafts.current_draft_user())
    write_deny_repr = repr(tuple(str(p) for p in write_deny_roots))
    if list_deny_roots is None:
        list_deny_roots = skill_drafts.unlistable_dirs(_state._LLM_CONFIG, skill_drafts.current_draft_user())
    list_deny_repr = repr(tuple(str(p) for p in list_deny_roots))
    return f'''\
import builtins as _guard_builtins
import io as _guard_io
import os as _guard_os
import shutil as _guard_shutil

_GUARD_ALLOWED = [_guard_os.path.realpath(_p) for _p in {allowed_repr}]
_GUARD_LIB_CACHE = [_guard_os.path.realpath(_p) for _p in {lib_cache_repr}]
_GUARD_DISPLAY = list(dict.fromkeys(_guard_os.path.realpath(_p) for _p in {display_repr}))
_GUARD_TMP_ROOTS = [_guard_os.path.realpath(_p) for _p in {tmp_roots_repr}]
_GUARD_DENY_ROOTS = [_guard_os.path.realpath(_p) for _p in {deny_repr}]
_GUARD_WRITE_DENY_ROOTS = [_guard_os.path.realpath(_p) for _p in {write_deny_repr}]
_GUARD_LIST_DENY_ROOTS = [_guard_os.path.realpath(_p) for _p in {list_deny_repr}]
_GUARD_OWN_TMP_NAME = "_tmp_" + (_guard_os.environ.get("AGENT_EXEC_TMP_NAME") or _guard_os.environ.get("AGENT_THREAD_ID", "_no_session"))


def _guard_under(_target, _roots):
    return any(_target == _root or _target.startswith(_root + _guard_os.sep) for _root in _roots)


def _guard_check_deny(_path):
    try:
        _target = _guard_os.path.realpath(_guard_os.fspath(_path))
    except TypeError:
        return
    if _guard_under(_target, _GUARD_DENY_ROOTS):
        raise PermissionError(f"[draft skill guard] Another user's draft skills cannot be accessed: {{_path}}")


def _guard_check_list(_path):
    try:
        _target = _guard_os.path.realpath(_guard_os.fspath(_path))
    except TypeError:
        return
    if _guard_under(_target, _GUARD_LIST_DENY_ROOTS) or any(
        _target.startswith(_root + _guard_os.sep) for _root in _GUARD_DENY_ROOTS
    ):
        raise PermissionError(f"[draft skill guard] Another user's draft skills cannot be accessed: {{_path}}")


def _guard_check_foreign_tmp(_path):
    _guard_check_deny(_path)
    try:
        _target = _guard_os.path.realpath(_guard_os.fspath(_path))
    except TypeError:
        return
    for _root in _GUARD_TMP_ROOTS:
        if _target == _root:
            return
        if _target.startswith(_root + _guard_os.sep):
            _first = _target[len(_root) + 1 :].split(_guard_os.sep, 1)[0]
            if _first.startswith("_tmp_") and _first != _GUARD_OWN_TMP_NAME:
                _own_dir = _guard_os.path.join(_root, _GUARD_OWN_TMP_NAME)
                raise PermissionError(
                    f"[temporary directory guard] The temporary directory of another session "
                    f"cannot be accessed: {{_path}}\\n"
                    f"Use your own temporary folder and try again: {{_own_dir}}"
                )
            return


def _guard_check(_path, _op):
    _guard_check_foreign_tmp(_path)
    try:
        _target = _guard_os.path.realpath(_guard_os.fspath(_path))
    except TypeError:
        return
    if _guard_under(_target, _GUARD_WRITE_DENY_ROOTS):
        raise PermissionError(f"[draft skill guard] Another user's draft skills do not allow {{_op}}: {{_path}}")
    for _root in _GUARD_ALLOWED + _GUARD_LIB_CACHE:
        if _target == _root or _target.startswith(_root + _guard_os.sep):
            return
    if _GUARD_ALLOWED:
        _allowed_list = ", ".join(_GUARD_DISPLAY or _GUARD_ALLOWED)
        raise PermissionError(
            f"[write sandbox guard] {{_op}} is allowed only under the working directory: {{_path}}\\n"
            f"Change the destination to somewhere under one of the following and try again: {{_allowed_list}}\\n"
            "If you do not know the path or are unsure whether it is right, check it with the check_work_dir_status tool."
        )
    raise PermissionError(
        f"[write sandbox guard] This tool cannot write or delete anything: {{_path}}\\n"
        "If you need to write, use the execute_python_code tool instead."
    )


_guard_orig_open = _guard_builtins.open


def _guard_open(_file, _mode="r", *_args, **_kwargs):
    if any(_c in _mode for _c in ("w", "a", "x", "+")):
        _guard_check(_file, "write")
    else:
        _guard_check_foreign_tmp(_file)
    return _guard_orig_open(_file, _mode, *_args, **_kwargs)


_guard_builtins.open = _guard_open
_guard_io.open = _guard_open

for _guard_name in ("remove", "unlink", "rename", "replace", "rmdir", "removedirs", "mkdir", "makedirs", "truncate"):
    def _guard_make_os(_orig, _name):
        def _fn(_path, *_args, **_kwargs):
            _guard_check(_path, _name)
            if _name in ("rename", "replace") and _args:
                _guard_check(_args[0], _name)
            return _orig(_path, *_args, **_kwargs)

        return _fn

    _guard_orig = getattr(_guard_os, _guard_name, None)
    if _guard_orig is not None:
        setattr(_guard_os, _guard_name, _guard_make_os(_guard_orig, _guard_name))

for _guard_name in ("rmtree", "move"):
    def _guard_make_shutil(_orig, _name):
        def _fn(_src, *_args, **_kwargs):
            _guard_check(_src, _name)
            if _args:
                _guard_check(_args[0], _name)
            return _orig(_src, *_args, **_kwargs)

        return _fn

    _guard_orig = getattr(_guard_shutil, _guard_name, None)
    if _guard_orig is not None:
        setattr(_guard_shutil, _guard_name, _guard_make_shutil(_guard_orig, _guard_name))

for _guard_name in ("copy", "copy2", "copyfile", "copytree"):
    def _guard_make_shutil_copy(_orig, _name):
        def _fn(_src, *_args, **_kwargs):
            # コピー元は読み取りのみ（open()の読み取りモードと同じ扱い）。
            # 他セッションの一時ディレクトリからの読み取りだけは引き続きブロックする。
            _guard_check_foreign_tmp(_src)
            if _args:
                _guard_check(_args[0], _name)
            return _orig(_src, *_args, **_kwargs)

        return _fn

    _guard_orig = getattr(_guard_shutil, _guard_name, None)
    if _guard_orig is not None:
        setattr(_guard_shutil, _guard_name, _guard_make_shutil_copy(_guard_orig, _guard_name))

for _guard_name in ("listdir", "scandir", "open"):
    # 一覧取得と低水準 open は読み取りだけを見る（書き込み判定は open()/os.* 側）。
    # 読めない他ユーザーのドラフトの中身・ファイル名を漏らさないため。
    def _guard_make_read(_orig, _check):
        def _fn(*_args, **_kwargs):
            _target = _args[0] if _args else _kwargs.get("path", ".")
            if _target is not None:
                _check(_target)
            return _orig(*_args, **_kwargs)

        return _fn

    _guard_orig = getattr(_guard_os, _guard_name, None)
    if _guard_orig is not None:
        _check = _guard_check_deny if _guard_name == "open" else _guard_check_list
        setattr(_guard_os, _guard_name, _guard_make_read(_guard_orig, _check))

del _guard_name, _guard_orig, _check

import subprocess as _guard_subprocess

_GUARD_BLOCKED_CMDS = {{
    "git", "npm", "pip", "pip3",
    "copy", "xcopy", "move", "robocopy", "del", "erase",
    "ren", "rename", "rd", "rmdir",
    "cmd", "cmd.exe", "powershell", "pwsh",
}}


def _guard_cmd_basename(_arg):
    try:
        _s = _guard_os.fspath(_arg)
    except TypeError:
        _s = _arg
    _base = _guard_os.path.basename(str(_s)).lower()
    for _ext in (".exe", ".cmd", ".bat"):
        if _base.endswith(_ext):
            _base = _base[: -len(_ext)]
            break
    return _base


def _guard_check_cmd(_args):
    if isinstance(_args, (str, bytes)):
        _tokens = str(_args).strip().split()
        _first = _tokens[0] if _tokens else ""
    elif isinstance(_args, _guard_os.PathLike):
        _first = _args
    elif _args:
        _first = _args[0]
    else:
        _first = ""
    if _guard_cmd_basename(_first) in _GUARD_BLOCKED_CMDS:
        raise PermissionError(
            f"[execute_python_code guard] Running git/npm/pip commands is forbidden: {{_args}}"
            ". This is a blanket ban regardless of whether the package is installed. "
            "Existing libraries are usually installed already, so first try importing them or "
            "running the script. Only if that still fails, report to the user that a new library "
            "is needed (you cannot install it yourself with this tool)."
        )


_guard_orig_popen_init = _guard_subprocess.Popen.__init__


def _guard_popen_init(self, args, *_a, **_kw):
    _guard_check_cmd(args)
    _guard_orig_popen_init(self, args, *_a, **_kw)


_guard_subprocess.Popen.__init__ = _guard_popen_init

_guard_orig_system = _guard_os.system


def _guard_os_system(_cmd):
    _guard_check_cmd(_cmd)
    return _guard_orig_system(_cmd)


_guard_os.system = _guard_os_system

_guard_orig_os_popen = _guard_os.popen


def _guard_os_popen(_cmd, *_a, **_kw):
    _guard_check_cmd(_cmd)
    return _guard_orig_os_popen(_cmd, *_a, **_kw)


_guard_os.popen = _guard_os_popen
'''
