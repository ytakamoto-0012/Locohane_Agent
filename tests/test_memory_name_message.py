"""永続メモリーの name が ASCII スラッグに限られることを、エラー文で明示していることの回帰テスト。

以前のエラー文の "letters" は、日本語で書くモデルには日本語の文字も含むように読め、
日本語の name で create_memory / update_memory が拒否されてから ASCII に書き直す
無駄な呼び出しが起きていた（2026-10-25 の evals run で4回）。挙動（ASCII のみ許可）は変えない。
"""

import pytest

from src import memory


def test_name_message_says_ascii_slug(tmp_path):
    with pytest.raises(ValueError) as e:
        memory.create_memory(tmp_path, "子供会行事予定表の作成", "d", "project", "body")
    msg = str(e.value)
    assert "ASCII slug" in msg
    assert "子供会行事予定表の作成" in msg
    assert memory.create_memory(tmp_path, "kids-festival-schedule", "d", "project", "body").is_file()
