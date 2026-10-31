"""excel-vba-editスキル（skills/excel-vba-edit/scripts/_vba_ops.py）の
set_code/add_moduleで`Attribute VB_Name`行が二重化するバグの回帰テスト。

背景（2026-08-23）: `read_vba.py`（oletools）はモジュール冒頭の
`Attribute VB_Name = "..."`行を`code`にそのまま含めて返す。この出力を
そのまま`set_code`へ渡すと、`_replace_code`が`CodeModule.AddFromString`で
生テキストとして挿入するため、VBEが`comp.Name`から自動生成するAttribute行と
重複し、モジュール冒頭に`Attribute VB_Name = "X"`が2行並んでしまう
（実機ログで確認: read→set_codeの往復後、read_vba.pyの再読み込み結果に
`Attribute VB_Name = \"CSVImporter\"\r\nAttribute VB_Name = \"CSVImporter\"\r\n...`
という二重行が現れた）。`_replace_code`が書き込み前に冒頭のAttribute行を
除去するよう修正した。
"""

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "excel-vba-edit" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _vba_ops import _replace_code, _strip_leading_attribute_lines  # noqa: E402


class _FakeCodeModule:
    def __init__(self, initial_lines: int = 0):
        self.CountOfLines = initial_lines
        self.deleted = None
        self.added = None

    def DeleteLines(self, start, count):
        self.deleted = (start, count)
        self.CountOfLines = 0

    def AddFromString(self, code):
        self.added = code
        self.CountOfLines = code.count("\n") + 1


class TestStripLeadingAttributeLines:
    def test_strips_single_attribute_line(self):
        code = 'Attribute VB_Name = "CSVImporter"\r\nOption Explicit\r\nSub Foo()\r\nEnd Sub'
        result = _strip_leading_attribute_lines(code)
        assert result == "Option Explicit\r\nSub Foo()\r\nEnd Sub"

    def test_strips_multiple_leading_attribute_lines(self):
        code = (
            'Attribute VB_Name = "ThisWorkbook"\r\n'
            "Attribute VB_Base = \"0{00020819-0000-0000-C000-000000000046}\"\r\n"
            "Attribute VB_GlobalNameSpace = False\r\n"
            "Option Explicit\r\nSub Foo()\r\nEnd Sub"
        )
        result = _strip_leading_attribute_lines(code)
        assert result == "Option Explicit\r\nSub Foo()\r\nEnd Sub"

    def test_no_attribute_line_unchanged(self):
        code = "Option Explicit\r\nSub Foo()\r\nEnd Sub"
        assert _strip_leading_attribute_lines(code) == code

    def test_attribute_like_text_inside_body_not_stripped(self):
        code = 'Option Explicit\r\nSub Foo()\r\n    Debug.Print "Attribute VB_Name = x"\r\nEnd Sub'
        assert _strip_leading_attribute_lines(code) == code


class TestReplaceCodeDoesNotDuplicateAttributeLine:
    def test_replace_code_strips_attribute_before_addfromstring(self):
        code_module = _FakeCodeModule(initial_lines=5)
        new_code = 'Attribute VB_Name = "CSVImporter"\r\nOption Explicit\r\nSub Foo()\r\nEnd Sub'

        _replace_code(code_module, new_code)

        assert code_module.deleted == (1, 5)
        assert "Attribute VB_Name" not in code_module.added
        assert code_module.added == "Option Explicit\r\nSub Foo()\r\nEnd Sub"
