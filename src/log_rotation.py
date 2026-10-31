"""行数ベースで app_*.log をローテーションする logging.Handler。

標準の logging.handlers.RotatingFileHandler はバイトサイズ基準でしか
ローテーションできないため、行数基準（config.ini の [log].max_lines）が
要件のこのアプリでは独自実装が必要。

ファイル名は起動時固定ではなく、ローテーションのたびに「今」の日時から
<log_dir>/app_YYYYMMDD_HHMMSS.log の形式で作り直す（接頭辞 app は prefix で変えられる。
設定ダッシュボード（管理ツール）は admin_*.log に書く）。
同一秒に複数回ローテーションする場合は _1, _2... の連番を付けて衝突を
避ける。

1レコード=1行とは限らない（例外トレースバックで複数行になるレコードが
ある）ため、行数は「レコード数」ではなく「実際に書き込んだテキスト中の
改行数」でカウントする（app_*.log というファイル自体の行数を指標にする
ため）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

_PREFIX = "app"
_STAMP_FMT = "%Y%m%d_%H%M%S"


class LineCountRotatingFileHandler(logging.Handler):
    """行数が max_lines を超えたら日時つきファイル名で新規ローテーションする Handler。

    Attributes:
        log_dir: ログファイルの出力先ディレクトリ（既に存在する前提。
            app.py 側で config.log_dir.mkdir() 済み）。
        max_lines: 1ファイルが保持する最大行数。これを超えたら次の
            emit() 前にローテーションする。0以下の場合はローテーションを
            行わない（無効化）。
    """

    def __init__(
        self,
        log_dir: Path,
        max_lines: int,
        clear_on_startup: bool,
        encoding: str = "utf-8",
        prefix: str = _PREFIX,
    ) -> None:
        """Handler を構築し、書き込み先ファイルを1つ確定させる。

        Args:
            log_dir: ログ出力先ディレクトリ。
            max_lines: ローテーション閾値（行数）。0以下なら無効化。
            clear_on_startup: True なら起動のたびに必ず新規ファイルを
                作成する。False なら直近の既存 app_*.log への追記を試み、
                既に max_lines 以上であればその場でローテーションする。
            encoding: ファイルのエンコーディング。
            prefix: ファイル名の接頭辞（<prefix>_YYYYMMDD_HHMMSS.log）。
        """
        super().__init__()
        self.log_dir = log_dir
        self.prefix = prefix
        self.max_lines = max_lines
        self.encoding = encoding
        self._stream = None
        self._line_count = 0
        self._current_path: Path | None = None
        self._resolve_initial_file(clear_on_startup)

    def _resolve_initial_file(self, clear_on_startup: bool) -> None:
        if clear_on_startup:
            self._rotate()
            return
        latest = self._latest_existing_file()
        if latest is None:
            self._rotate()
            return
        line_count = self._count_lines(latest)
        if self.max_lines > 0 and line_count >= self.max_lines:
            self._rotate()
        else:
            self._current_path = latest
            self._line_count = line_count
            self._stream = latest.open("a", encoding=self.encoding)

    def _latest_existing_file(self) -> Path | None:
        candidates = list(self.log_dir.glob(f"{self.prefix}_*.log"))
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    @staticmethod
    def _count_lines(path: Path) -> int:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)

    def _build_new_path(self) -> Path:
        stamp = datetime.now().strftime(_STAMP_FMT)
        base = self.log_dir / f"{self.prefix}_{stamp}.log"
        if not base.exists():
            return base
        n = 1
        while True:
            candidate = self.log_dir / f"{self.prefix}_{stamp}_{n}.log"
            if not candidate.exists():
                return candidate
            n += 1

    def _rotate(self) -> None:
        if self._stream is not None:
            self._stream.close()
        self._current_path = self._build_new_path()
        self._stream = self._current_path.open("w", encoding=self.encoding)
        self._line_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record) + "\n"
            if self._stream is None:
                # close() の後にも書けるよう、同じファイルを開き直す（標準の FileHandler と同じ）。uvicorn は起動時に
                # logging の設定をやり直し、それまでのハンドラーを閉じるため（管理ツールの admin_*.log。admin/server.py）。
                self._stream = self._current_path.open("a", encoding=self.encoding)
            self._stream.write(text)
            self._stream.flush()
            self._line_count += text.count("\n")
            if self.max_lines > 0 and self._line_count >= self.max_lines:
                self._rotate()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        super().close()
