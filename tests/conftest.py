import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import tempfile

# 開発者の手元の「管理者設定」（instances/admin_settings.json）がテストの設定に混ざらないよう、無いファイルを指す。
os.environ.setdefault("ADMIN_SETTINGS_PATH", str(Path(tempfile.gettempdir()) / "locohane_tests_no_admin_settings.json"))
# 開発者の手元に内蔵の埋め込みのモデル（models/embedding/…）があるかどうかで、embedding_url が空のときの設定が変わらないよう、
# 既定では内蔵の埋め込みを使わない（使うテストは VECTOR_DB_LOCAL_EMBEDDING_MODEL を自分で設定する）。
os.environ.setdefault("VECTOR_DB_LOCAL_EMBEDDING_MODEL", "")

import pytest  # noqa: E402

_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


@pytest.fixture(autouse=True)
def _keep_embedding_usage_in_temp(monkeypatch):
    """埋め込みの使用量の台帳（src/embedding_usage.py）を、一時フォルダの外（load_config() の実際のスレッドDB等）へ書かせない。

    多くのテストは load_config() の設定を元にするため、[thread_store] db が開発者の手元の本物のDBを指す。
    検索・取り込みのテストがそこへ台帳の行を書き込まないよう、一時フォルダの外への記録は捨てる。
    """
    from src import embedding_usage

    original = embedding_usage.record

    def guarded(db_path, *args, **kwargs):
        if not Path(db_path).resolve().is_relative_to(_TEMP_ROOT):
            return None
        return original(db_path, *args, **kwargs)

    monkeypatch.setattr(embedding_usage, "record", guarded)
