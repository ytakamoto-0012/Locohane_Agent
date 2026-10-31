"""app.py の貼り付けテキスト添付・最大入力文字数ヘルパーの回帰テスト。

フロントエンド（Composer.tsx）は [ui].paste_as_attachment_threshold_chars 以上の
貼り付けを pasted-text-<時刻>-<連番>.txt の添付として送る。バックエンドは
これを保存先パスではなく本文としてLLMへ渡し（_build_human_message）、
本文＋貼り付けテキストの合計が [ui].max_input_chars を超える送信を拒否する
（_check_input_length）。
"""

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import app
from app import _build_human_message, _check_input_length, _is_pasted_text_file


def _with_max_input_chars(monkeypatch, limit: int) -> None:
    monkeypatch.setattr(app, "_config", dataclasses.replace(app._config, ui_max_input_chars=limit))


def _pasted_file(tmp_path, text: str, name: str = "pasted-text-1759123456789-1.txt"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_is_pasted_text_file_matches_frontend_naming() -> None:
    assert _is_pasted_text_file(r"C:\uploads\user\pasted-text-1759123456789-1.txt")
    assert _is_pasted_text_file("pasted-text-1759123456789.txt")
    assert not _is_pasted_text_file("pasted-text-notes.txt")
    assert not _is_pasted_text_file("report.txt")


def test_build_human_message_inlines_pasted_text_instead_of_path(tmp_path) -> None:
    pasted = _pasted_file(tmp_path, "# 深夜三時の洗濯機\n本文")
    other = tmp_path / "data.csv"
    other.write_text("a,b", encoding="utf-8")

    msg = _build_human_message("要約して", [str(pasted), str(other)])

    assert "# 深夜三時の洗濯機\n本文" in msg.content
    assert "[Pasted text 1" in msg.content
    # 通常のファイルは従来通りパスのみを列挙し、貼り付けテキストはそこに含めない。
    upload_section = msg.content.split("[Saved locations of uploaded files")[1]
    assert str(other) in upload_section
    assert str(pasted) not in upload_section


def test_check_input_length_counts_content_and_pasted_text(monkeypatch, tmp_path) -> None:
    _with_max_input_chars(monkeypatch, 10)
    pasted = _pasted_file(tmp_path, "x" * 6)
    element = SimpleNamespace(path=str(pasted), name=pasted.name)

    ok = SimpleNamespace(content="abcd", elements=[element])
    too_long = SimpleNamespace(content="abcde", elements=[element])

    assert _check_input_length(ok) is None
    assert "上限10文字" in _check_input_length(too_long)


def test_check_input_length_ignores_regular_attachments(monkeypatch, tmp_path) -> None:
    _with_max_input_chars(monkeypatch, 10)
    regular = tmp_path / "big.txt"
    regular.write_text("x" * 100, encoding="utf-8")
    message = SimpleNamespace(content="abc", elements=[SimpleNamespace(path=str(regular), name=regular.name)])

    assert _check_input_length(message) is None


def test_check_input_length_unlimited_when_zero(monkeypatch) -> None:
    _with_max_input_chars(monkeypatch, 0)

    assert _check_input_length(SimpleNamespace(content="x" * 1_000_000, elements=[])) is None


def test_build_human_message_passes_image_as_path_not_data_url(tmp_path) -> None:
    # 大量の画像でメインエージェントのコンテキストを圧迫しないよう、画像も
    # data URL を積まずに他のファイルと同じく保存先パスだけを本文へ書く。
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    msg = _build_human_message("この写真は何？", [str(image)])

    assert isinstance(msg.content, str)
    assert "data:image" not in msg.content
    upload_section = msg.content.split("[Saved locations of uploaded files")[1]
    assert str(image) in upload_section


def test_build_human_message_adds_path_memory_tokens_and_folder(tmp_path, monkeypatch) -> None:
    # ツールのパス引数へ書き写さず渡せるよう `@N` を添え、2件以上なら
    # dispatch_agent_batch の path に渡せる送信フォルダも載せる。
    registered = {}

    def _fake_register(paths, description=None):
        result = {}
        for p in paths:
            registered.setdefault(p, f"@{len(registered) + 1}")
            result[registered[p]] = p
        return result

    monkeypatch.setattr(app, "_register_path_memory", _fake_register)
    a, b = tmp_path / "a.png", tmp_path / "b.png"

    msg = _build_human_message("見て", [str(a), str(b)])

    assert f"- @1: {a}" in msg.content
    assert f"- @2: {b}" in msg.content
    assert f"(folder: @3: {tmp_path})" in msg.content


def test_save_uploads_uses_per_message_folder_in_thread_workdir(tmp_path, monkeypatch) -> None:
    # 同名ファイルで以前のアップロード（LLM が後で開くパス）を上書きしないよう、
    # スレッド専用フォルダの uploads/<送信日時>/ へ送信ごとに分けて保存する。
    monkeypatch.setattr(app, "_resolve_exec_workdir", lambda: tmp_path)
    src1, src2 = tmp_path / "s1", tmp_path / "s2"
    src1.write_bytes(b"1")
    src2.write_bytes(b"2")

    def _message(*elements):
        return SimpleNamespace(elements=[SimpleNamespace(path=str(p), name=n) for p, n in elements])

    first = app._save_uploads(_message((src1, "photo.jpg"), (src2, "photo.jpg")))
    second = app._save_uploads(_message((src2, "../../photo.jpg")))

    assert [Path(p).name for p in first] == ["photo.jpg", "photo (2).jpg"]
    assert Path(first[0]).parent.parent == tmp_path / "uploads"
    assert Path(second[0]).parent != Path(first[0]).parent
    assert Path(second[0]).parent.parent == tmp_path / "uploads"
    assert Path(first[0]).read_bytes() == b"1"
    assert app._save_uploads(_message()) == []


def test_save_uploads_sanitizes_unsafe_names(tmp_path, monkeypatch) -> None:
    # `..`・Windows で使えない文字・予約デバイス名でも送信フォルダの中へ保存できる
    # （以前は `..` で送信フォルダの親を指して copyfile が落ちていた）。
    monkeypatch.setattr(app, "_resolve_exec_workdir", lambda: tmp_path)
    src = tmp_path / "s"
    src.write_bytes(b"x")
    elements = [SimpleNamespace(path=str(src), name=n) for n in ("..", "a?b*.png", "CON.txt")]

    saved = app._save_uploads(SimpleNamespace(elements=elements))

    assert len(saved) == 3
    for p in saved:
        assert Path(p).parent.parent == tmp_path / "uploads"
        assert Path(p).read_bytes() == b"x"
    assert Path(saved[1]).name == "a_b_.png"
    assert Path(saved[2]).name == "_CON.txt"
