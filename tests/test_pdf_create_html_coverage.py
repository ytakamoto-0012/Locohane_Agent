"""create_pdf.py が HTML の本文を黙って捨てないこと（2026-11-10 のコード点検）。"""

import json
import subprocess
import sys
import warnings
from pathlib import Path

import pypdfium2 as pdfium

_CREATE = Path(__file__).resolve().parent.parent / "skills" / "pdf-tools" / "scripts" / "create_pdf.py"


def _text(tmp_path, html: str) -> str:
    out = tmp_path / "o.pdf"
    proc = subprocess.run([sys.executable, str(_CREATE), str(out), "--html", html], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["output_path"]
    doc = pdfium.PdfDocument(str(out))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return " ".join(doc[i].get_textpage().get_text_bounded() for i in range(len(doc)))
    finally:
        doc.close()


def test_plain_text_and_inline_tags_outside_paragraphs_are_kept(tmp_path):
    text = _text(tmp_path, "plain words <b>bold words</b><div>inside div</div><h5>small heading</h5><pre>keep\nlines</pre>")
    for expected in ("plain words", "bold words", "inside div", "small heading", "keep", "lines"):
        assert expected in text


def test_nested_list_items_are_separate_lines(tmp_path):
    text = _text(tmp_path, "<ul><li>parent<ul><li>child</li></ul></li></ul>")
    between = text[text.index("parent") + len("parent") : text.index("child")]
    assert "\n" in between  # 以前は親の項目と同じ行に「parent child」と連結されていた
