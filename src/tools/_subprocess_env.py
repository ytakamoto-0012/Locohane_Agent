"""run_script/execute_python_code のサブプロセス環境変数・書き込みガード注入。"""

from __future__ import annotations

from pathlib import Path
import chainlit as cl
import logging
import os
import tempfile

from .. import skill_drafts
from . import _state
from ._safe_path import _safe_path
from ._python_fs_guard import _python_fs_guard_preamble
from ._workdir import _exec_tmp_name, _resolve_exec_workdir, _tmp_dir_parents

logger = logging.getLogger(__name__)


def _subprocess_env() -> dict[str, str]:
    """run_script/execute_python_code の子プロセスへ渡す環境変数を組み立てる。

    既存の PYTHONIOENCODING（日本語文字化け対策）に加え、パスメモリー
    （src/path_memory.py）用の AGENT_THREAD_ID/AGENT_PATH_MEMORY_DIR/
    AGENT_PATH_MEMORY_MAX_ENTRIES を注入する。run_script 経由で実行される
    スキルのスクリプトは、これらを `path_memory.env_params()` で読み、
    自分が出力するパスをレジストリへ登録できる。
    AGENT_SRC_DIR は execute_python_code のサブプロセスが
    `src/path_memory.py` をインポートするために使う。
    AGENT_DEFAULT_WORKDIR は `_resolve_exec_workdir()`/`path_memory.exec_tmp_dir()`
    が中間生成物置き場 `_tmp_<name>/` の基準ディレクトリを決めるために使う
    （run_script の cwd が指すユーザー指定 work_dir は保持日数ベースの自動削除
    対象外のため、cwd 基準にすると中間生成物が消えずに溜まり続ける。常に
    default_workdir 基準に固定することで自動削除の対象に含める。
    `_resolve_exec_workdir` 参照）。
    AGENT_EXEC_TMP_NAME は `_tmp_<name>/` の `<name>` 部分（`_exec_tmp_name()`
    が生成する、作成時刻プレフィックス付きthread_id）。サブプロセス側
    （skills配下の各スクリプト・`path_memory.exec_tmp_dir()`）はこの値を使って
    メインプロセスの `_resolve_exec_workdir()` と同じディレクトリ名を組み立てる。
    未設定時は AGENT_THREAD_ID（生のthread_id、時刻プレフィックス無し）へ
    フォールバックする。

    config.ini `[paths].bin_path`（既定は空）に列挙されたディレクトリを PATH の
    先頭へ追加する。コマンド名を素の状態で叩く外部バイナリのスキルは、事前に
    ユーザーがOS側のPATH環境変数へ手動登録していないと「コマンドが見つからない」
    で失敗する。config.ini に配置先を明示しておけば、evals・app.py実行時の
    どちらでも追加の手動設定なしで呼び出せる。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env["AGENT_THREAD_ID"] = cl.user_session.get("thread_id") or "_no_session"
    env["AGENT_EXEC_TMP_NAME"] = _exec_tmp_name()
    if _state._PATH_MEMORY_DIR is not None:
        env["AGENT_PATH_MEMORY_DIR"] = str(_state._PATH_MEMORY_DIR)
    env["AGENT_PATH_MEMORY_MAX_ENTRIES"] = str(_state._PATH_MEMORY_MAX_ENTRIES)
    env["AGENT_SRC_DIR"] = str(_state._SRC_DIR)
    if _state._DEFAULT_WORKDIR is not None:
        env["AGENT_DEFAULT_WORKDIR"] = str(_state._DEFAULT_WORKDIR)
    cfg = _state._LLM_CONFIG
    # ユーザー別ドラフトスキル（src/skill_drafts.py）。skill-creator のスクリプトが
    # 自分のドラフト置き場を求めるのに使う。会話のユーザーが分からない文脈
    # （evals 等）では渡さない（skill-creator 側はドラフト操作をエラーにする）。
    draft_user = skill_drafts.current_draft_user()
    if cfg is not None and draft_user is not None and getattr(cfg, "skill_draft_dir", None) is not None:
        env["AGENT_SKILL_DRAFT_DIR"] = str(cfg.skill_draft_dir)
        env["AGENT_USER"] = draft_user
        env["AGENT_OTHER_USERS_DRAFTS"] = cfg.skill_other_users_drafts
    else:
        for key in ("AGENT_SKILL_DRAFT_DIR", "AGENT_USER", "AGENT_OTHER_USERS_DRAFTS"):
            env.pop(key, None)
    # 正式スキルの走査ルート（優先順、ドラフト置き場を除く）。skill-creator が
    # Locohane 本体の Python 環境に依存せずに正式スキル名との重複を確かめるのに使う。
    draft_root = getattr(cfg, "skill_draft_dir", None)
    official_roots = [r for r in (_state._SKILLS_ROOTS or []) if draft_root is None or r != draft_root.resolve()]
    env["AGENT_SKILL_ROOTS"] = os.pathsep.join(str(r) for r in official_roots)
    if cfg is not None:
        bin_dirs = [d for d in cfg.bin_path if d.is_dir()]
        if bin_dirs:
            env["PATH"] = os.pathsep.join([*(str(d) for d in bin_dirs), env.get("PATH", "")])
    return env

def _allowed_sandbox_dirs_for(skill_name: str, script_filename: str) -> list[Path]:
    """config.ini [default_workdir].allow_sandbox_dir のうち、指定した
    (skill_name, script_filename) からの書き込みが許可されているディレクトリ
    だけを抽出する。

    _state._ALLOW_SANDBOX_DIRS の各エントリ（SandboxDirEntry）は
    dir（許可ディレクトリ）と allow_entries（そのディレクトリへの書き込みを
    許可する対象の集合）を持つ。allow_entries には
    [main_agent_tool_guard].allow_entries と同じ形式で、ビルトインツール名
    そのもの（文字列）、または (スキル名, スクリプトファイル名) のタプルが
    入りうる。run_script/run_script_background は常に特定のスキル/スクリプト
    を実行するため、ここでは skill_name（スキル名単位の許可）と
    (skill_name, script_filename)（スクリプト単位の許可）のどちらかに
    一致するエントリのみを対象にする。allow_entries が空集合のエントリは
    無制限扱いとし、対象を問わず常に許可する。

    Args:
        skill_name: 実行しようとしているスキルのフォルダ名。
        script_filename: 実行しようとしているスクリプトのファイル名。

    Returns:
        許可されたディレクトリのうち実在するもの（is_dir()）のリスト。
        存在しないディレクトリは無視する。
    """
    result: list[Path] = []
    for entry in _state._ALLOW_SANDBOX_DIRS:
        if (
            not entry.allow_entries
            or skill_name in entry.allow_entries
            or (skill_name, script_filename) in entry.allow_entries
        ):
            if entry.dir.is_dir():
                result.append(entry.dir)
    return result


def _skill_creator_draft_roots(skill_name: str) -> list[Path]:
    """skill-creator 本体が書き込んでよいドラフト置き場のディレクトリを返す。

    skills_dir 配下の skill-creator（実体の場所で判定。同名の .locohane 側
    スキルは対象外）のときだけ、会話のユーザー自身のフォルダ
    `<draft_dir>/<ユーザー名>/`（other_users_drafts=full なら draft_dir 全体）を
    返す。それ以外のスキル・ユーザー不明の文脈では空リスト。
    """
    cfg = _state._LLM_CONFIG
    draft_dir = getattr(cfg, "skill_draft_dir", None)
    user = skill_drafts.current_draft_user()
    if skill_name != skill_drafts.SKILL_CREATOR_NAME or draft_dir is None or user is None:
        return []
    try:
        scripts_dir = _safe_path(f"{skill_name}/scripts", op="exec")
    except (ValueError, RuntimeError):
        return []
    if not skill_drafts.is_builtin_skill_creator(scripts_dir, cfg):
        return []
    own_dir = draft_dir / user
    own_dir.mkdir(parents=True, exist_ok=True)
    return [draft_dir.resolve()] if cfg.skill_other_users_drafts == "full" else [own_dir.resolve()]


def _run_script_guard_env(workdir: Path, skill_name: str, script_filename: str) -> tuple[dict[str, str], Path | None]:
    """run_script/run_script_background が起動するサブプロセスへ、書き込み
    サンドボックスガードを注入した環境変数を組み立てる。

    run_script はスキル作者が書いた既存の scripts/ 配下のファイルをそのまま
    実行するため、execute_python_code のようにコード文字列の先頭へガードの
    ソースを連結する方法が使えない（対象ファイルを書き換えるのは事故の元）。
    代わりに Python がサブプロセス起動時に sys.path 上の sitecustomize
    モジュールを自動 import する仕組みを利用し、_python_fs_guard_preamble が
    生成するのと同じモンキーパッチコードを一時ディレクトリへ sitecustomize.py
    として書き出し、PYTHONPATH の先頭に追加することで対象スクリプトのソースを
    一切変更せずに書き込み・削除系呼び出しへガードを差し込む。

    許可されるのは workdir（run_script の cwd = 作業ディレクトリ。呼び出し元の
    _prepare_script_execution が _resolve_workdir() を使うため、
    work_dir未設定等の場合はここで既に `_tmp_<thread_id>` になっている）・
    `_tmp_<thread_id>`（念のため
    ここでも明示的に加える。default_workdirはサーバー側の共有ディレクトリの
    ため、直下やその他のサブディレクトリへの書き込みは許可せず、常に
    自セッション専用の`_tmp_<thread_id>`のみに限定する。他セッションが
    生成物を誤参照する事故の防止）・config.ini [default_workdir].allow_sandbox_dir
    のうち今回実行する (skill_name, script_filename) が許可対象になっている
    追加ディレクトリ（_allowed_sandbox_dirs_for()、既定は空。セッション分離は
    効かず全セッションから常時書き込み可能）・path_memory_dir（register_output_path() が
    ロックファイルを書き込むLocohane内部の状態ディレクトリ。ユーザー成果物の
    保存先ではないためexecute_python_code側のallowed_rootsとは異なりここにのみ追加）
    配下のみ。それ以外の場所（他ドライブ・Locohaneプロジェクト本体・
    default_workdir直下の他ディレクトリを含む）への書き込み・削除は
    PermissionError でブロックされる。読み取りは対象外だが、
    `_tmp_<thread_id>`（`workdir`直下に全セッション共通で並ぶ一時フォルダ）に
    限っては、自セッション以外への読み取り・書き込み・削除を allowed_roots の
    内外を問わず追加でブロックする（`_python_fs_guard_preamble` の
    `tmp_dir_roots` 参照）。

    Args:
        workdir: このスクリプト実行の cwd（_resolve_workdir の解決結果）。
        skill_name: 実行しようとしているスキルのフォルダ名（allow_sandbox_dir
            の allow_entries 照合に使う）。
        script_filename: 実行しようとしているスクリプトのファイル名
            （同上）。

    Returns:
        (env, guard_dir) のタプル。guard_dir は呼び出し側がサブプロセス
        終了後に削除する一時ディレクトリ。一時ファイル作成に失敗した場合は
        guard_dir が None になり、env にはガード無しの _subprocess_env() の
        結果のみが入る（ガード注入の失敗自体でスクリプト実行を止めない。
        書き込み制限が効かなくなるだけで、通常の権限エラー等は従来通り
        OS側で発生する）。
    """
    env = _subprocess_env()
    allowed_roots = [workdir]
    if _state._DEFAULT_WORKDIR is not None:
        allowed_roots.append(_resolve_exec_workdir())
    allowed_roots.extend(_allowed_sandbox_dirs_for(skill_name, script_filename))
    allowed_roots.extend(_skill_creator_draft_roots(skill_name))
    display_roots = list(allowed_roots)
    if _state._PATH_MEMORY_DIR is not None:
        allowed_roots.append(_state._PATH_MEMORY_DIR)
    try:
        guard_dir = Path(tempfile.mkdtemp(prefix="agent_fs_guard_"))
        guard_src = _python_fs_guard_preamble(
            allowed_roots, tmp_dir_roots=_tmp_dir_parents(workdir), display_roots=display_roots
        )
        (guard_dir / "sitecustomize.py").write_text(guard_src, encoding="utf-8")
    except OSError:
        logger.warning("run_script: failed to create the temporary file for the write guard, running without the guard.")
        return env, None
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(guard_dir) + (os.pathsep + existing_path if existing_path else "")
    return env, guard_dir
