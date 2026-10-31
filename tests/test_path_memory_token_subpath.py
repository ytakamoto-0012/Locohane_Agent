"""`@N/子パス`（パスメモリー参照の後ろにファイル名を続ける写法）の解決の回帰テスト。

モデルはフォルダの `@N` の中のファイルを `@2/photo_01.png` のように書く。以前は
引数全体がちょうど `@N` の場合だけ解決していたため、この写法は解決されずに
analyze_image が `file not found` を繰り返していた（2026-10-25 の evals run で38回）。
以下を固定化する:
- フォルダのエントリに限り、`/` と `\\` のどちらの区切りでも子パスを結合する。
- ファイルのエントリに子パスを続けたらエラーにする（黙って結合しない）。
- エラー文は基となる `@N` の名前で述べる。
"""

from pathlib import Path

import pytest
from PIL import Image

from src import path_memory, tools
from src.tools import _script_job
from src.tools._path_memory_helpers import _resolve_path_memory_token


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {"thread_id": "thread-1"}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


@pytest.fixture
def env(tmp_path, monkeypatch):
    path_memory_dir = tmp_path / "path_memory_data"
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_DIR", path_memory_dir)
    monkeypatch.setattr(tools._state, "_PATH_MEMORY_MAX_ENTRIES", 500)
    monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", None)
    monkeypatch.setattr(tools._state, "_SKILLS_ROOTS", [tmp_path / "skills"])
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())

    def _register(path: Path) -> str:
        return f"@{path_memory.register('thread-1', str(path), path_memory_dir, 500)}"

    return tmp_path, _register


@pytest.fixture
def photos(env):
    tmp_path, register = env
    folder = tmp_path / "photos"
    folder.mkdir()
    image = folder / "photo_01.png"
    Image.new("RGB", (4, 4), "red").save(image)
    return register(folder), folder, image


def test_bare_token_resolves_as_before(photos):
    token, folder, _ = photos
    assert _resolve_path_memory_token(token) == (str(folder), None)


@pytest.mark.parametrize("sep", ["/", "\\"])
def test_subpath_after_folder_entry_is_joined(photos, sep):
    token, _, image = photos
    assert _resolve_path_memory_token(f"{token}{sep}photo_01.png") == (str(image), None)


def test_subpath_after_file_entry_is_an_error(env, photos):
    _, register = env
    _, _, image = photos
    token = register(image)
    value, error = _resolve_path_memory_token(f"{token}/x.png")
    assert value == f"{token}/x.png"
    assert f"Path memory {token} is a file" in error
    assert f"Pass {token} alone" in error


def test_unregistered_token_with_subpath_names_the_token(env):
    value, error = _resolve_path_memory_token("@9/rest")
    assert value == "@9/rest"
    assert error.startswith("Path memory @9 is not registered.")
    assert "@9/rest" not in error


@pytest.mark.parametrize("value", [r"C:\data\a.png", "data/a.png", "a@1/b.png", "@x/b.png"])
def test_non_token_values_pass_through(env, value):
    assert _resolve_path_memory_token(value) == (value, None)


def test_run_script_args_resolve_subpath(photos, monkeypatch):
    token, _, image = photos
    script = Path("C:/fake/skill/scripts/run.bat")
    monkeypatch.setattr(_script_job, "_resolve_script_filename", lambda skill, name: script)
    command = _script_job._resolve_run_script_command("some-skill", "run.bat", [f"{token}/photo_01.png", "--flag"])
    assert command == [str(script), str(image), "--flag"]


def test_analyze_image_accepts_subpath_of_folder_entry(photos):
    token, _, _ = photos
    text, artifact = tools.analyze_image.func(relative_path=f"{token}/photo_01.png")
    assert text == f"Loaded the image: {token}/photo_01.png"
    assert artifact["image_url"].startswith("data:image/")
    # 画像の参照にはフォルダの `@N/子パス` ではなく、画像ファイル自体を登録した `@N` を使う
    assert f"{token}/" not in artifact["ref"]
