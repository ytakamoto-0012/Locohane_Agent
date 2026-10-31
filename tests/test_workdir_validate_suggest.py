"""作業フォルダ入力欄のサーバー側検証・パス候補（src/workdir_suggest.py と app.py の
pick_work_dir / suggest_work_dir action_callback）の回帰テスト。

背景（2026-10-23 ユーザー要望）: 以前はサーバーPCから存在しないフォルダを指定しても
受け付けて既定フォルダへ黙ってフォールバックしていたため、ブラウザだけ別PCの構成では
自分のPCのパスを入力してしまったことに気づけなかった。
"""

from types import SimpleNamespace

import pytest

import app
from src import workdir_suggest
from src.tools.check_work_dir_status import WorkDirAccessStatus


class _FakeUserSession:
    def __init__(self):
        self.values: dict = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class _FakeMessage:
    sent: list[str] = []

    def __init__(self, content: str):
        self.content = content

    async def send(self):
        _FakeMessage.sent.append(self.content)


@pytest.fixture
def fake_cl(monkeypatch):
    session = _FakeUserSession()
    _FakeMessage.sent = []
    monkeypatch.setattr(app.cl, "user_session", session)
    monkeypatch.setattr(app.cl, "Message", _FakeMessage)

    async def _noop(resolved):
        return None

    monkeypatch.setattr(app, "_persist_work_dir", _noop)
    return session


def _make_tree(tmp_path):
    for name in ("Project", "projects_old", "my_project", "Other"):
        (tmp_path / name).mkdir()
    (tmp_path / "project.txt").write_text("", encoding="utf-8")


def test_suggest_prefix_first_then_contains_dirs_only(tmp_path) -> None:
    _make_tree(tmp_path)
    result = workdir_suggest.suggest_work_dirs(str(tmp_path) + "/proj")
    names = [p.replace("\\", "/").rsplit("/", 1)[-1] for p in result]
    # 前方一致（大文字小文字無視）が先、部分一致が後。ファイルは含めない。
    assert names == ["Project", "projects_old", "my_project"]


def test_suggest_lists_children_when_input_ends_with_separator(tmp_path) -> None:
    _make_tree(tmp_path)
    result = workdir_suggest.suggest_work_dirs(str(tmp_path) + "/")
    assert len(result) == 4


def test_suggest_caps_results(tmp_path) -> None:
    for i in range(15):
        (tmp_path / f"dir{i:02d}").mkdir()
    assert len(workdir_suggest.suggest_work_dirs(str(tmp_path) + "/dir", limit=10)) == 10
    assert len(workdir_suggest.suggest_work_dirs(str(tmp_path) + "/dir")) == 15


def test_suggest_returns_empty_for_missing_parent(tmp_path) -> None:
    assert workdir_suggest.suggest_work_dirs(str(tmp_path / "nope") + "/a") == []
    assert workdir_suggest.suggest_work_dirs("") == []


def test_error_message_rejects_missing_and_unreadable_but_allows_read_only() -> None:
    missing = WorkDirAccessStatus("X", exists=False, readable=False, writable=False)
    unreadable = WorkDirAccessStatus("X", exists=True, readable=False, writable=False)
    read_only = WorkDirAccessStatus("X", exists=True, readable=True, writable=False)
    assert "存在しない" in workdir_suggest.work_dir_error_message("X", missing)
    assert "読み取れません" in workdir_suggest.work_dir_error_message("X", unreadable)
    assert workdir_suggest.work_dir_error_message("X", read_only) is None


@pytest.mark.asyncio
async def test_pick_work_dir_rejects_missing_dir_without_applying(tmp_path, fake_cl) -> None:
    fake_cl.set("work_dir", "C:/previous")
    res = await app.on_pick_work_dir(SimpleNamespace(payload={"path": str(tmp_path / "missing")}))
    assert res["ok"] is False
    assert "サーバー" in res["error"] and "存在しない" in res["error"]
    # 以前の設定はそのまま、状態メッセージも出さない。
    assert fake_cl.get("work_dir") == "C:/previous"
    assert _FakeMessage.sent == []


@pytest.mark.asyncio
async def test_settings_update_rejected_resends_current_work_dir(tmp_path, fake_cl, monkeypatch) -> None:
    # 歯車アイコンの設定で拒否したパスが入力欄・chat_settings に残らないよう、
    # 現在の作業ディレクトリで ChatSettings を送り直す。
    resent: list[str] = []

    async def _fake_send(initial):
        resent.append(initial)

    monkeypatch.setattr(app, "_send_work_dir_settings", _fake_send)
    fake_cl.set("work_dir", "C:/previous")
    await app.on_settings_update({"work_dir": str(tmp_path / "missing")})
    assert resent == ["C:/previous"]
    assert fake_cl.get("work_dir") == "C:/previous"
    assert len(_FakeMessage.sent) == 1 and "変更できませんでした" in _FakeMessage.sent[0]

    # 受け付けた場合は送り直さない（入力欄の値がそのまま実際の値になる）。
    await app.on_settings_update({"work_dir": str(tmp_path)})
    assert resent == ["C:/previous"]


@pytest.mark.asyncio
async def test_pick_work_dir_accepts_existing_dir(tmp_path, fake_cl) -> None:
    res = await app.on_pick_work_dir(SimpleNamespace(payload={"path": f'"{tmp_path}"'}))
    assert res == {"ok": True, "error": None}
    assert fake_cl.get("work_dir") == str(tmp_path.resolve())
    assert len(_FakeMessage.sent) == 1


@pytest.mark.asyncio
async def test_suggest_work_dir_action_returns_candidates(tmp_path) -> None:
    _make_tree(tmp_path)
    res = await app.on_suggest_work_dir(SimpleNamespace(payload={"text": str(tmp_path) + "/Oth"}))
    assert [p.replace("\\", "/").rsplit("/", 1)[-1] for p in res["suggestions"]] == ["Other"]


def test_suggest_action_does_not_create_thread() -> None:
    # 候補問い合わせ（入力のたびに呼ばれる）で会話が作られないこと。
    assert "suggest_work_dir" in app._NON_CHAT_ACTION_NAMES


@pytest.mark.asyncio
async def test_work_dir_server_info_returns_hostname(monkeypatch) -> None:
    monkeypatch.setattr(workdir_suggest.socket, "gethostname", lambda: "SERVER-PC")
    assert await app.on_work_dir_server_info(SimpleNamespace(payload={})) == {"hostname": "SERVER-PC"}
    assert "work_dir_server_info" in app._NON_CHAT_ACTION_NAMES


def test_suggest_excludes_hidden_and_system_dirs(tmp_path) -> None:
    _make_tree(tmp_path)
    (tmp_path / ".git").mkdir()
    if workdir_suggest._is_windows():
        import ctypes

        hidden = tmp_path / "$RECYCLE.BIN"
        hidden.mkdir()
        ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2 | 0x4)  # HIDDEN | SYSTEM
    names = [p.replace("\\", "/").rsplit("/", 1)[-1] for p in workdir_suggest.suggest_work_dirs(str(tmp_path) + "/")]
    assert sorted(names, key=str.casefold) == ["my_project", "Other", "Project", "projects_old"]


@pytest.mark.asyncio
async def test_suggest_action_uses_config_limit(tmp_path, monkeypatch) -> None:
    import dataclasses

    for i in range(8):
        (tmp_path / f"dir{i}").mkdir()
    monkeypatch.setattr(app, "_config", dataclasses.replace(app._config, ui_work_dir_max_suggestions=3))
    res = await app.on_suggest_work_dir(SimpleNamespace(payload={"text": str(tmp_path) + "/dir"}))
    assert len(res["suggestions"]) == 3
    monkeypatch.setattr(app, "_config", dataclasses.replace(app._config, ui_work_dir_max_suggestions=0))
    res = await app.on_suggest_work_dir(SimpleNamespace(payload={"text": str(tmp_path) + "/dir"}))
    assert res["suggestions"] == []


def test_config_ini_work_dir_max_suggestions_loaded() -> None:
    from src.config import load_config

    assert load_config().ui_work_dir_max_suggestions == 100
