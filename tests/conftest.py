import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import tempfile

# 開発者の手元の「管理者設定」（instances/admin_settings.json）がテストの設定に混ざらないよう、無いファイルを指す。
os.environ.setdefault("ADMIN_SETTINGS_PATH", str(Path(tempfile.gettempdir()) / "locohane_tests_no_admin_settings.json"))
