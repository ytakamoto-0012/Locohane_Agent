"""mcp_server/ 配下で共有する設定値の集約。

configparser 等の外部依存は使わず、Python 定数として完結させる
（`mcp_server/` は Locohane 本体の Chainlit アプリとは独立した
standalone プロセスとして動くため、`config.ini` の読み込み機構
（`src/config.py`）には依存させない）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 対象とする skills/ ディレクトリ（複数指定可）。既定はこのプロジェクト直下の
# 1つのみだが、別の Agent Skills フォルダも合わせて配布/実行対象にしたい場合は
# 環境変数 LOCOHANE_MCP_SKILLS_SRC に os.pathsep（Windowsは`;`）区切りで
# 複数パスを指定できる。`src/tools/_safe_path.py` の `_state._SKILLS_ROOTS`
# （Locohane本体側も複数ルートを保持できる）と同じ考え方。
# 同名スキルが複数ルートに存在する場合は先頭に近いルートを優先する。
_skills_src_env = os.environ.get("LOCOHANE_MCP_SKILLS_SRC")
SKILLS_SRC: tuple[Path, ...] = (
    tuple(Path(p) for p in _skills_src_env.split(os.pathsep) if p)
    if _skills_src_env
    else (PROJECT_ROOT / "skills",)
)

# run_skill_script がスクリプトを実行する際のサブプロセス cwd。
# 既定は None（cwdを指定しない = MCPサーバー自身の起動時cwdをそのまま継承）。
# Locohaneのスキルは生成物の出力先を output_path 等の明示的な引数で受け取る
# 設計（skills/SKILLS_README.md 4-4節）のため、cwdを固定で強制する必要は薄い。
# Claude Desktop等cwdが不定なクライアントから使う場合や、生成物の既定置き場を
# 固定したい場合は環境変数 LOCOHANE_MCP_SKILLS_WORKDIR で上書きできる。
_workdir_env = os.environ.get("LOCOHANE_MCP_SKILLS_WORKDIR")
WORKDIR: Path | None = Path(_workdir_env) if _workdir_env else None

# run_skill_script のスクリプト実行タイムアウト秒数。
# 既定は Locohane 本体の config.ini [scripts].timeout と同値。
# 環境変数 LOCOHANE_MCP_SKILLS_SCRIPT_TIMEOUT_SECONDS（整数）で上書きできる。
# 不正な値（非数値等）が設定されていた場合は既定値へフォールバックする。
_timeout_env = os.environ.get("LOCOHANE_MCP_SKILLS_SCRIPT_TIMEOUT_SECONDS")
try:
    SCRIPT_TIMEOUT_SECONDS = int(_timeout_env) if _timeout_env else 300
except ValueError:
    SCRIPT_TIMEOUT_SECONDS = 300

# run_skill_script がスクリプトの実行に使う Python 実行ファイル。
# 既定は sys.executable（このMCPサーバー自身を起動しているPython）だが、
# それが Locohane 本体の config.ini [scripts].python（スキルが前提とする
# 依存関係が入った仮想環境）と一致しない起動経路（Claude Desktop等）もあるため、
# 環境変数 LOCOHANE_MCP_SKILLS_PYTHON で明示的に指定できるようにする。
_python_env = os.environ.get("LOCOHANE_MCP_SKILLS_PYTHON")
SCRIPT_PYTHON: str = _python_env if _python_env else sys.executable

# MCPサーバー自体の名前（FastMCPインスタンス名）。クライアント側の表示名や
# ツール名衝突回避のため、複数のMCPサーバーを同時接続する場合に区別したいことがある。
# 環境変数 LOCOHANE_MCP_SKILLS_NAME で上書きできる。既定は "locohane-skills"。
_name_env = os.environ.get("LOCOHANE_MCP_SKILLS_NAME")
SERVER_NAME: str = _name_env if _name_env else "locohane-skills"

# Resources 配布（publish.build_publish_dir）から除外するファイル名／ディレクトリ名。
# .env系: APIキー等の機密情報を含みうるもの（新しいスキルに追加した場合はここにも追記すること）。
# __pycache__: 配布に無意味な Python コンパイルキャッシュ。
EXCLUDE_NAMES = (".env", ".env.local", ".env.*", "__pycache__")
