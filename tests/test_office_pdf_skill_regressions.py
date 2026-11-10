"""office/pdf 系スキルのコード点検（2026-11-11）で直した不具合の回帰テスト。"""

import importlib
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pypdfium2 as pdfium
import pytest

_SKILLS = Path(__file__).resolve().parent.parent / "skills"
_PDF = _SKILLS / "pdf-tools" / "scripts"


def _run(script: Path, *args, cwd=None, env_extra=None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    env.pop("AGENT_SRC_DIR", None)
    return subprocess.run(
        [sys.executable, str(script), *map(str, args)], capture_output=True, text=True, encoding="utf-8", cwd=cwd, env=env
    )


def _pdf_text(path: Path) -> str:
    doc = pdfium.PdfDocument(str(path))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return "\n".join(doc[i].get_textpage().get_text_bounded() for i in range(len(doc)))
    finally:
        doc.close()


def _create_pdf(tmp_path, html: str, name: str = "o.pdf") -> tuple[Path, dict]:
    out = tmp_path / name
    proc = _run(_PDF / "create_pdf.py", out, "--html", html, env_extra={"AGENT_DEFAULT_WORKDIR": str(tmp_path / "work")})
    assert proc.returncode == 0, proc.stderr
    return out, json.loads(proc.stdout)


def test_table_rows_inside_thead_and_tbody_are_kept(tmp_path):
    out, _ = _create_pdf(
        tmp_path,
        "<h1>Report</h1><table><thead><tr><th>Month</th><th>Sales</th></tr></thead>"
        "<tbody><tr><td>April</td><td>120</td></tr></tbody><tfoot><tr><td>Total</td><td>120</td></tr></tfoot></table>",
    )
    text = _pdf_text(out)
    for expected in ("Month", "Sales", "April", "Total"):
        assert expected in text  # 以前は <thead>/<tbody> の中の行を探さず、表が丸ごと消えていた


def test_colspan_and_rowspan_are_laid_out(tmp_path):
    out, _ = _create_pdf(
        tmp_path,
        '<table><tr><th colspan="2">Heading</th></tr><tr><th>Row head</th><td rowspan="2">Merged</td></tr>'
        "<tr><td>Below</td></tr></table>",
    )
    text = _pdf_text(out)
    for expected in ("Heading", "Row head", "Merged", "Below"):
        assert expected in text


def test_tall_image_is_shrunk_to_the_page(tmp_path):
    from PIL import Image

    image = tmp_path / "tall.png"
    Image.new("RGB", (400, 3000), "red").save(image)
    _create_pdf(tmp_path, f'<p>a</p><img src="{image}">')  # 以前は「too large」で PDF 全体の生成が失敗した


def test_callout_keeps_paragraphs_and_lists_apart(tmp_path):
    out, _ = _create_pdf(tmp_path, '<div class="callout"><p>Note one</p><ul><li>Item A</li><li>Item B</li></ul></div>')
    text = _pdf_text(out)
    between = text[text.index("Note one") + len("Note one") : text.index("Item A")]
    assert "\n" in between  # 以前は「Note oneItem AItem B」と1つの段落に連結されていた


def test_create_pdf_backs_up_an_existing_file(tmp_path):
    _create_pdf(tmp_path, "<p>first</p>")
    _, result = _create_pdf(tmp_path, "<p>second</p>")
    backup = Path(result["backup_path"])
    assert backup.exists() and "first" in _pdf_text(backup)


@pytest.fixture
def isolated_script_modules():
    """スキルのスクリプトを import したテストの後に、sys.modules を元へ戻す。

    スキルのスクリプトは同じ名前の `_common` を各自の scripts/ から import する。ここで読んだ pdf-tools の
    `_common` が sys.modules に残ると、後のテスト（skill-creator 等）が別スキルの `_common` をつかんで
    ImportError になる（全体実行でだけ落ちていた）。
    """
    names = ("_common", "render_pdf_pages")
    saved = {name: sys.modules[name] for name in names if name in sys.modules}
    yield
    for name in names:
        sys.modules.pop(name, None)
    sys.modules.update(saved)


def test_render_pdf_pages_survives_a_page_that_fails(tmp_path, monkeypatch, capsys, isolated_script_modules):
    source = tmp_path / "in.pdf"
    source.write_bytes(b"%PDF-1.4")
    monkeypatch.syspath_prepend(str(_PDF))
    monkeypatch.setenv("AGENT_DEFAULT_WORKDIR", str(tmp_path / "work"))
    render = importlib.import_module("render_pdf_pages")

    class _Page:
        def render(self, **_kwargs):
            raise RuntimeError("boom")

    class _Doc:
        def __init__(self, _path):
            self.closed = False

        def __len__(self):
            return 2

        def __getitem__(self, _i):
            return _Page()

        def close(self):
            self.closed = True

    monkeypatch.setattr(render, "pdfium", type("FakePdfium", (), {"PdfDocument": _Doc}))
    monkeypatch.setattr(sys, "argv", ["render_pdf_pages.py", str(source)])
    assert render.main() == 0  # 以前は失敗したページに image_path が無く KeyError で落ちた
    images = json.loads(capsys.readouterr().out)["images"]
    assert [sorted(i) for i in images] == [["error", "page"], ["error", "page"]]


def test_pptx_read_includes_text_inside_groups(tmp_path):
    import pptx
    from pptx.util import Inches

    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(0, 0, Inches(1), Inches(1)).text_frame.text = "in group"
    inner = group.shapes.add_group_shape()
    inner.shapes.add_textbox(0, 0, Inches(1), Inches(1)).text_frame.text = "in nested group"
    path = tmp_path / "g.pptx"
    prs.save(path)
    proc = _run(_SKILLS / "pptx-read" / "scripts" / "read_pptx.py", path, env_extra={"AGENT_DEFAULT_WORKDIR": str(tmp_path / "work")})
    assert proc.returncode == 0, proc.stderr
    result = json.loads(Path(json.loads(proc.stdout)["result_path"]).read_text(encoding="utf-8"))
    assert result["slides"][0]["texts"] == ["in group", "in nested group"]


def test_docx_read_includes_nested_table_text(tmp_path):
    import docx

    document = docx.Document()
    table = document.add_table(rows=1, cols=2)
    cell = table.cell(0, 0)
    cell.text = "outer"
    nested = cell.add_table(rows=1, cols=2)
    nested.cell(0, 0).text = "inner A"
    nested.cell(0, 1).text = "inner B"
    table.cell(0, 1).text = "right"
    path = tmp_path / "n.docx"
    document.save(path)
    proc = _run(_SKILLS / "docx-read" / "scripts" / "read_docx.py", path, env_extra={"AGENT_DEFAULT_WORKDIR": str(tmp_path / "work")})
    assert proc.returncode == 0, proc.stderr
    result = json.loads(Path(json.loads(proc.stdout)["result_path"]).read_text(encoding="utf-8"))
    first = result["tables"][0][0]
    assert "inner A | inner B" in first[0] and first[0].startswith("outer") and first[1] == "right"


@pytest.fixture
def excel_common(monkeypatch):
    monkeypatch.syspath_prepend(str(_SKILLS / "office_shared"))
    return importlib.import_module("excel_common")


def test_password_protected_office_file_is_detected_before_starting_office(tmp_path, excel_common):
    encrypted = tmp_path / "p.xlsx"
    encrypted.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 100)
    plain = tmp_path / "q.xlsx"
    plain.write_bytes(b"PK\x03\x04" + b"\0" * 100)
    legacy = tmp_path / "r.xls"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0" + b"\0" * 100)
    assert "password-protected" in excel_common.password_protected_error(encrypted)
    assert excel_common.password_protected_error(plain) is None
    assert excel_common.password_protected_error(legacy) is None  # 旧形式は常に OLE のため判定しない


def test_reused_pid_is_not_terminated(excel_common, monkeypatch):
    identity = {"image": "excel.exe", "created": 100}
    monkeypatch.setattr(excel_common, "process_identity", lambda pid: dict(identity))
    assert excel_common._is_same_tracked_process({"pid": 1, "image": "excel.exe", "created": 100})
    # 起動時刻が違う＝同じ番号の別プロセス
    assert not excel_common._is_same_tracked_process({"pid": 1, "image": "excel.exe", "created": 99})
    # 古い記録（起動時刻なし）は実行ファイル名だけ確かめる
    assert excel_common._is_same_tracked_process({"pid": 1})
    identity["image"] = "notepad.exe"
    assert not excel_common._is_same_tracked_process({"pid": 1})
    monkeypatch.setattr(excel_common, "process_identity", lambda pid: None)
    assert not excel_common._is_same_tracked_process({"pid": 1, "image": "excel.exe", "created": 100})
