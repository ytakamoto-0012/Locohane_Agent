"""コンテキスト圧縮後、重複呼び出しガードの履歴がリセットされることの回帰テスト。

要約後のモデルは要約に含まれなかった個々のツール呼び出し（どのファイルを
どの引数で読んだか等）を覚えていないが、_check_file_tools_duplicate /
analyze_image の重複ガードは cl.user_session に会話全体を通じた呼び出し
履歴を保持し続ける。記憶が無いのにガードだけが「既に呼び出し済み」として
拒否し続けると、モデルはエラーの理由を理解できないまま同じような呼び出しを
繰り返し、抜け出せないループに陥る（ユーザー報告）。

app.py はコンテキスト圧縮成功パスで reset_call_history_guards_after_compaction()
を呼び、token_usage_cumulative_main のリセットと同様にこれらの履歴を
クリアする想定。ここではその関数単体の振る舞いを検証する。
"""

from src import tools


class _FakeUserSession:
    def __init__(self):
        self._data: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def test_reset_clears_file_tools_and_image_call_signatures(monkeypatch) -> None:
    fake_session = _FakeUserSession()
    monkeypatch.setattr(tools.cl, "user_session", fake_session)

    fake_session.set("file_tools_call_signatures", {"Read\x00foo.txt\x000\x00None": 1})
    fake_session.set("analyze_image_call_signatures", {"C:\\img.png": 1})

    tools.reset_call_history_guards_after_compaction()

    assert fake_session.get("file_tools_call_signatures") is None
    assert fake_session.get("analyze_image_call_signatures") is None


def test_reset_inside_subagent_clears_subagent_scoped_keys(monkeypatch) -> None:
    # carry_over_to_main=false ではサブエージェントごとに記録先キーが分かれる。
    # サブエージェントの圧縮成功パス（src/subagent.py）から呼んだ場合に、その
    # サブエージェントの記録が消えること（漏れていたため、圧縮で消えた画像の
    # 読み直しが重複ガードに拒否され処理が欠落した。2026-10-01、006）。
    fake_session = _FakeUserSession()
    monkeypatch.setattr(tools.cl, "user_session", fake_session)
    monkeypatch.setattr(
        tools._state, "_LLM_CONFIG", type("Cfg", (), {"file_tools_duplicate_guard_carry_over_to_main": False})()
    )
    fake_session.set("analyze_image_call_signatures", {"main.png": 1})
    fake_session.set("analyze_image_call_signatures_subagent_run-1", {"sub.png": 1})

    token = tools._state._SUBAGENT_RUN_ID.set("run-1")
    try:
        tools.reset_call_history_guards_after_compaction()
    finally:
        tools._state._SUBAGENT_RUN_ID.reset(token)

    assert fake_session.get("analyze_image_call_signatures_subagent_run-1") is None
    # メインエージェント側の記録には触れない
    assert fake_session.get("analyze_image_call_signatures") == {"main.png": 1}


def test_duplicate_guard_allows_previously_blocked_call_after_reset(monkeypatch) -> None:
    fake_session = _FakeUserSession()
    monkeypatch.setattr(tools.cl, "user_session", fake_session)

    signature = "Read\x00foo.txt\x000\x00None"
    # 上限(1回)まで消費させ、2回目が拒否される状態を作る。
    assert tools._duplicate_guard._record_and_check_duplicate("file_tools_call_signatures", signature, 1) is False
    assert tools._duplicate_guard._record_and_check_duplicate("file_tools_call_signatures", signature, 1) is True

    tools.reset_call_history_guards_after_compaction()

    # リセット後は同じシグネチャでも「初回」として扱われる。
    assert tools._duplicate_guard._record_and_check_duplicate("file_tools_call_signatures", signature, 1) is False
