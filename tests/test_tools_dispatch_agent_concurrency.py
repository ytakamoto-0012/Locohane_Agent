"""dispatch_agent の並列実行数をガードするセマフォの回帰テスト。

ToolNode は同一AIMessage内の複数tool_callsを並列実行するため、モデルが
dispatch_agent を1ターンで複数回呼ぶと、単一インスタンスのllama-server
へ複数リクエストが同時に飛ぶ。本番で、この並列実行がLangGraphチェック
ポイントの破損（AIMessageのtool_callsに対応するToolMessageが欠落し、
次のモデル呼び出しでValueErrorが発生する）につながる事象が確認された
ため、実際のLLM呼び出し（run_subagent）の同時実行数が
_DISPATCH_AGENT_SEMAPHORES の設定値（config.ini の [subagent].max_parallel
由来）どおりに制御されることを検証する。

_DISPATCH_AGENT_SEMAPHORES はセッション（llm.get_current_session()）ごとに
独立した Semaphore を遅延生成する辞書であるため、各テストは monkeypatch で
辞書を空に差し替えてから実行し、他テストが同じセッションキー（未設定時は
None）で生成した Semaphore を再利用してしまわないようにする。
"""

import asyncio
import json

import pytest

from src import llm, tools


class _FakeUserSession:
    """dispatch_agent の finally が main_agent_tool_guard カウンタをリセットする際に
    触れる cl.user_session を、Chainlit実行コンテキスト無しでも動くよう差し替える。
    """

    def __init__(self):
        self._data: dict = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _FakeMessage:
    """tools.cl.Message の差し替え。dispatch_agent が内部で起動する進捗push
    タスク（_push_dispatch_agent_progress）が cl.Message(...).send() を呼ぶため、
    Chainlit実行コンテキスト無しでも動くよう差し替える
    （test_tools_dispatch_agent.py の _FakeMessage と同じパターン）。
    """

    def __init__(self, content: str = "", **kwargs) -> None:
        self.content = content

    async def send(self) -> None:
        pass

    async def update(self) -> None:
        pass


def _setup(monkeypatch, tmp_path=None) -> None:
    monkeypatch.setattr(tools._state, "_LLM_CONFIG", object())
    monkeypatch.setattr(
        tools._state,
        "_AGENT_TYPES",
        {"explore": tools._state.ResolvedAgentType(description="", system_prompt="", tools=[])},
    )
    monkeypatch.setattr(tools.cl, "user_session", _FakeUserSession())
    monkeypatch.setattr(tools.cl, "Message", _FakeMessage)
    # 他テストが同じセッションキー（未設定時は None）で生成した Semaphore を
    # 再利用しないよう、辞書を空の状態から始める。
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_SEMAPHORES", {})
    # ジョブレジストリも他テストの残留状態を引き継がないよう空にしておく。
    monkeypatch.setattr(tools._dispatch_agent_job, "_DISPATCH_AGENT_JOBS", {})
    if tmp_path is not None:
        workdir = tmp_path / "workdir"
        workdir.mkdir(exist_ok=True)
        monkeypatch.setattr(tools._state, "_DEFAULT_WORKDIR", workdir)


_TC_ID_COUNTER = 0


async def _invoke_dispatch_agent(**kwargs):
    """dispatch_agent は InjectedToolCallId を持つため ToolCall 形式で呼ぶ
    必要がある（test_tools_dispatch_agent.py の _invoke_dispatch_agent と
    同じ理由づけ。ToolCall形式では戻り値が ToolMessage にラップされるため
    .content を取り出して従来通り文字列を返す）。"""
    global _TC_ID_COUNTER
    _TC_ID_COUNTER += 1
    result = await tools.dispatch_agent.ainvoke(
        {"name": "dispatch_agent", "args": kwargs, "id": f"test-tc-{_TC_ID_COUNTER}", "type": "tool_call"}
    )
    return result.content if hasattr(result, "content") else result


async def _dispatch_three(monkeypatch) -> tuple[list[str], int]:
    concurrent = 0
    max_concurrent = 0

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return f"done:{task.splitlines()[-1]}"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    results = await asyncio.gather(
        _invoke_dispatch_agent(task="a", agent_type="explore"),
        _invoke_dispatch_agent(task="b", agent_type="explore"),
        _invoke_dispatch_agent(task="c", agent_type="explore"),
    )
    return results, max_concurrent


@pytest.mark.asyncio
async def test_dispatch_agent_default_max_parallel_serializes_calls(monkeypatch) -> None:
    _setup(monkeypatch)
    # モジュールデフォルト（init_tools() 未実行時）は max_parallel=1 ＝ 完全直列化。
    results, max_concurrent = await _dispatch_three(monkeypatch)

    assert max_concurrent == 1
    assert sorted(results) == ["done:a", "done:b", "done:c"]


@pytest.mark.asyncio
async def test_dispatch_agent_max_parallel_two_caps_concurrency(monkeypatch) -> None:
    _setup(monkeypatch)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_MAX_PARALLEL", 2)
    results, max_concurrent = await _dispatch_three(monkeypatch)

    assert max_concurrent == 2
    assert sorted(results) == ["done:a", "done:b", "done:c"]


@pytest.mark.asyncio
async def test_dispatch_agent_max_parallel_disabled_allows_full_concurrency(monkeypatch) -> None:
    _setup(monkeypatch)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_MAX_PARALLEL", 0)
    results, max_concurrent = await _dispatch_three(monkeypatch)

    assert max_concurrent == 3
    assert sorted(results) == ["done:a", "done:b", "done:c"]


@pytest.mark.asyncio
async def test_dispatch_agent_max_parallel_is_independent_per_session(monkeypatch) -> None:
    """max_parallel=1 でも、別セッション（thread_id）同士は互いに待ち合わない。

    _DISPATCH_AGENT_SEMAPHORES はセッションごとに独立した Semaphore を持つため、
    セッションAでdispatch_agentが直列化されていても、セッションBの
    dispatch_agent呼び出しをブロックしないことを確認する。
    """
    _setup(monkeypatch)
    monkeypatch.setattr(tools._state, "_DISPATCH_AGENT_MAX_PARALLEL", 1)

    concurrent = 0
    max_concurrent = 0

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return f"done:{task.splitlines()[-1]}"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    async def run_in_session(thread_id: str, task: str) -> str:
        llm.set_current_session(thread_id)
        return await _invoke_dispatch_agent(task=task, agent_type="explore")

    loop = asyncio.get_event_loop()
    start = loop.time()
    results = await asyncio.gather(
        run_in_session("session-a", "a"),
        run_in_session("session-b", "b"),
    )
    elapsed = loop.time() - start

    assert max_concurrent == 2
    assert sorted(results) == ["done:a", "done:b"]
    # セッション間で待ち合っていなければ、2回分の asyncio.sleep(0.05) が
    # 並行して走るため約0.05秒。待ち合っていれば約0.1秒になるはず。
    assert elapsed < 0.09


@pytest.mark.asyncio
async def test_dispatch_agent_injects_work_dir_hint_into_task(monkeypatch, tmp_path) -> None:
    """メインエージェントが推測したパスが誤っていても、サブエージェントが
    照合できる本当の作業ディレクトリを、taskの先頭に事実として与える
    （issue: 存在しないパスをtaskに書かれたサブエージェントが、work_dir外の
    似た名前のフォルダを黙って採用してしまった件の再発防止）。
    """
    _setup(monkeypatch, tmp_path=tmp_path)
    captured: dict = {}

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        captured["task"] = task
        return "ok"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    await _invoke_dispatch_agent(task="investigate", agent_type="explore")

    # work_dir未設定時、_resolve_workdir()はdefault_workdir自体ではなく
    # スレッド専用フォルダ（_resolve_exec_workdir()）を返す（2026-08-29修正、
    # 別スレッドのデータを拾わないための分離）。
    expected_work_dir = tools._workdir._resolve_exec_workdir()
    expected_info = json.dumps({"absolute_path": str(expected_work_dir)}, ensure_ascii=False)
    assert captured["task"] == f"[Working directory]\n{expected_info}\n\ninvestigate"


@pytest.mark.asyncio
async def test_dispatch_agent_hint_injection_is_swallowed_when_default_workdir_unset(monkeypatch) -> None:
    """_DEFAULT_WORKDIR 未設定（init_tools() 未実行相当）でも、ヒント注入の
    RuntimeError で dispatch_agent 自体を失敗させず、taskはヒント無しで渡る。
    """
    _setup(monkeypatch)  # tmp_path を渡さない = _DEFAULT_WORKDIR は None のまま
    captured: dict = {}

    async def fake_run_subagent(task, tools_list, system_prompt, llm_config, max_iterations, **kwargs):
        captured["task"] = task
        return "ok"

    monkeypatch.setattr(tools._dispatch_agent_job.subagent, "run_subagent", fake_run_subagent)

    result = await _invoke_dispatch_agent(task="investigate", agent_type="explore")

    assert result == "ok"
    assert captured["task"] == "investigate"
