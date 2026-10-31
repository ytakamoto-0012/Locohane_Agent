"""excel-vba-editスキル（skills/excel-vba-edit/scripts/_vba_ops.py）の
`Continue Do`/`Continue For`/`Continue While`検出の回帰テスト。

背景（2026-08-23）: LLMが生成したVBAコード（ImportCSVプロシージャ）に
`Continue Do`が含まれていた。これはVB.NET等には存在するがVBAには存在しない
構文で、`_lint_vba_syntax`のブロック対応チェックをすり抜けてそのまま
`set_code`で書き込まれてしまい、後になってマクロ実行時に問題として発覚した
（analyze-docsが原因調査し、`GoTo`+ラベルへの置換を計画した）。低パラメータ
モデルが他言語のContinue文と混同しやすい既知のハマりどころのため、書き込み
前に機械的に検出してエラーにするようにした。
"""

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "excel-vba-edit" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _vba_ops import _lint_vba_syntax  # noqa: E402


class TestLintDetectsInvalidContinueStatement:
    def test_continue_do_raises(self):
        code = (
            "Sub Foo()\r\n"
            "    Do While True\r\n"
            "        If x = 1 Then\r\n"
            "            Continue Do\r\n"
            "        End If\r\n"
            "    Loop\r\n"
            "End Sub"
        )
        with pytest.raises(ValueError, match="Continue Do/For/While"):
            _lint_vba_syntax(code)

    def test_continue_for_raises(self):
        code = "Sub Foo()\r\n    For i = 1 To 10\r\n        Continue For\r\n    Next i\r\nEnd Sub"
        with pytest.raises(ValueError, match="Continue Do/For/While"):
            _lint_vba_syntax(code)

    def test_continue_while_raises(self):
        code = "Sub Foo()\r\n    While True\r\n        Continue While\r\n    Wend\r\nEnd Sub"
        with pytest.raises(ValueError, match="Continue Do/For/While"):
            _lint_vba_syntax(code)

    def test_continue_inside_string_literal_not_flagged(self):
        code = 'Sub Foo()\r\n    Debug.Print "Continue Do"\r\nEnd Sub'
        _lint_vba_syntax(code)  # should not raise

    def test_continue_inside_comment_not_flagged(self):
        code = "Sub Foo()\r\n    ' Continue Do is not valid VBA\r\n    Debug.Print 1\r\nEnd Sub"
        _lint_vba_syntax(code)  # should not raise

    def test_normal_code_without_continue_does_not_raise(self):
        code = "Sub Foo()\r\n    Do While True\r\n        Exit Do\r\n    Loop\r\nEnd Sub"
        _lint_vba_syntax(code)  # should not raise
