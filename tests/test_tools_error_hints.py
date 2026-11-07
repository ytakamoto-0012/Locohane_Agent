"""execute_python_code(_readonly) の失敗に添える案内（src/tools/_error_hints.py）のテスト。"""

from src.tools._error_hints import DECODE_ERROR_HINT, error_hints


def test_cp932_decode_error_gets_the_encoding_hint():
    stderr = "Traceback ...\nUnicodeDecodeError: 'cp932' codec can't decode byte 0x95 in position 18: illegal multibyte sequence"
    assert error_hints(stderr) == [DECODE_ERROR_HINT]


def test_other_errors_get_no_hint():
    assert error_hints("ValueError: boom") == []
    assert error_hints("UnicodeDecodeError: 'utf-8' codec can't decode byte 0x82") == []
    assert error_hints("") == []
