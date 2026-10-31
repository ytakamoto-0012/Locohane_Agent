"""app.py のトークン使用量集計ヘルパーの回帰テスト。

_is_subagent_call() は、event["parent_ids"] にこれまで観測した
dispatch_agent の run_id（dispatch_agent_run_ids集合、ターン終了まで
保持しon_tool_endでもpopしない）が含まれるかどうかで、dispatch_agent
内部（サブエージェント）由来のイベントかを判定する。
"""

import dataclasses
import json

import app
from app import TOKEN_USAGE_PREFIX, _format_token_usage, _is_subagent_call, _token_usage_level
from src import i18n

# セッション文脈の外では既定言語（日本語）の表示名になる。
MAIN_CALL_USAGE_LABEL = i18n.t("usage.main")


def _with_thresholds(monkeypatch, warn: int, alert: int) -> None:
    """app._config（frozen dataclass）の閾値だけを差し替えたコピーに一時的に入れ替える。"""
    monkeypatch.setattr(
        app,
        "_config",
        dataclasses.replace(
            app._config,
            ui_token_usage_warn_threshold=warn,
            ui_token_usage_alert_threshold=alert,
        ),
    )


def test_is_subagent_call_true_when_parent_is_known_dispatch_agent() -> None:
    dispatch_agent_run_ids = {"tool-run-1"}
    event = {"parent_ids": ["some-other-run", "tool-run-1"]}

    assert _is_subagent_call(event, dispatch_agent_run_ids) is True


def test_is_subagent_call_false_for_top_level_event() -> None:
    dispatch_agent_run_ids = {"tool-run-1"}
    event = {"parent_ids": ["unrelated-run"]}

    assert _is_subagent_call(event, dispatch_agent_run_ids) is False


def test_is_subagent_call_false_when_no_dispatch_agent_seen() -> None:
    event = {"parent_ids": ["some-run"]}

    assert _is_subagent_call(event, set()) is False


def test_is_subagent_call_true_after_dispatch_agent_tool_step_already_closed() -> None:
    """[subagent].background_inline_wait_max_seconds 超過時の回帰テスト。

    dispatch_agent 自身は安全上限超過で早期リターンし on_tool_end が
    発火済み（＝steps辞書からは既にpopされている）だが、asyncio.shield で
    温存されたバックグラウンドジョブ（job.runner_task）はその後も動き続け、
    内部LLM呼び出しのイベントをこのターンの astream_events へ流し続ける
    （src/subagent.py参照）。dispatch_agent_run_ids は steps と異なり
    on_tool_end で pop しないため、この遅延イベントも引き続き
    サブエージェント由来と判定できる必要がある。
    """
    dispatch_agent_run_ids = {"tool-run-1"}
    # steps 相当のものは既に空（pop済み）でも、dispatch_agent_run_ids には残る。
    event = {"parent_ids": ["tool-run-1"]}

    assert _is_subagent_call(event, dispatch_agent_run_ids) is True


def test_format_token_usage_includes_call_and_conversation_total() -> None:
    call = {"input": 1, "output": 2, "total": 3}
    cumulative = {"input": 100, "output": 200, "total": 300}

    text = _format_token_usage({None: {"label": MAIN_CALL_USAGE_LABEL, **call}}, cumulative)

    assert text.startswith(TOKEN_USAGE_PREFIX)
    payload = json.loads(text[len(TOKEN_USAGE_PREFIX) :])
    rows = {row["label"]: row for row in payload["rows"]}

    assert set(rows) == {MAIN_CALL_USAGE_LABEL, "会話累計"}
    assert rows[MAIN_CALL_USAGE_LABEL] == {"label": MAIN_CALL_USAGE_LABEL, **call, "level": None, "group": "call"}
    assert rows["会話累計"] == {
        "label": "会話累計",
        **cumulative,
        "group": "total",
    }


def test_format_token_usage_one_row_per_owner_with_main_first(monkeypatch) -> None:
    # 並列実行中のサブエージェントは出力元ごとに1行ずつ並び、警告色も行ごとに判定する。
    _with_thresholds(monkeypatch, warn=48000, alert=64000)
    totals = {"input": 0, "output": 0, "total": 0}
    call_usage = {
        "g1": {"label": "SUB: worker（グループ1/16・15件）", "input": 50000, "output": 0, "total": 50000},
        None: {"label": MAIN_CALL_USAGE_LABEL, "input": 1, "output": 2, "total": 3},
        "g2": {"label": "SUB: worker（グループ2/16・15件）", "input": 1, "output": 2, "total": 3},
    }

    payload = json.loads(_format_token_usage(call_usage, totals)[len(TOKEN_USAGE_PREFIX) :])
    call_rows = [row for row in payload["rows"] if row["group"] == "call"]

    assert [row["label"] for row in call_rows] == [
        MAIN_CALL_USAGE_LABEL,
        "SUB: worker（グループ1/16・15件）",
        "SUB: worker（グループ2/16・15件）",
    ]
    assert [row["level"] for row in call_rows] == [None, "warn", None]


def test_token_usage_level_none_below_thresholds(monkeypatch) -> None:
    _with_thresholds(monkeypatch, warn=48000, alert=64000)

    assert _token_usage_level(1000) is None


def test_token_usage_level_warn_between_thresholds(monkeypatch) -> None:
    _with_thresholds(monkeypatch, warn=48000, alert=64000)

    assert _token_usage_level(48000) == "warn"
    assert _token_usage_level(63999) == "warn"


def test_token_usage_level_alert_at_or_above_alert_threshold(monkeypatch) -> None:
    _with_thresholds(monkeypatch, warn=48000, alert=64000)

    assert _token_usage_level(64000) == "alert"
    assert _token_usage_level(100000) == "alert"


def test_token_usage_level_disabled_when_thresholds_zero(monkeypatch) -> None:
    _with_thresholds(monkeypatch, warn=0, alert=0)

    assert _token_usage_level(1_000_000) is None
