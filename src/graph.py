"""ReAct ループのグラフ構築（手書き実装／prebuilt 実装を config で切替）。

config.graph_impl（config.ini の [graph].implementation）により
2つの実装を出し分ける。

- "handwritten": agent ノードと tools ノードを自分で明示配線する。ループの流れ
  （モデル呼び出し → ツール実行 → 結果を戻す）がコードから直接追えることを
  最優先する構成。
  - agent ノード: システムプロンプト + 会話履歴を ChatOpenAI に渡してモデルを呼ぶ
  - tools ノード: 直近 AI メッセージの tool_calls を実行する
    （ImageAwareToolNode = ToolNode を継承し、view_image の画像artifactを
    後続の HumanMessage として自動追加するラッパー）
  - should_continue: tool_calls があれば tools へ、無ければ END
- "prebuilt": LangGraph 標準の create_react_agent にそのまま委譲する。配線は
  ライブラリ内部に隠蔽されるが実装量は最小になる。

いずれの実装でも、LLM 接続は llama.cpp server（OpenAI 互換）を langchain-openai
の ChatOpenAI で。Ollama 固有の API・ライブラリは一切使わない。
会話状態は AsyncSqliteSaver で永続化する（保存先は config.checkpoint_db）。
"""

from __future__ import annotations

import logging
import uuid

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import create_react_agent

from .config import Config
from .context_compaction import maybe_append_precompact_note_nudge
from .context_trim import is_trigger_reached, trim_old_ai_messages, trim_old_tool_messages
from .llm import (
    ThinkingLoopDetected,
    ToolCallLoopDetected,
    build_model,
    enable_thinking_control,
    pick_loop_nudge_message,
    tool_loop_nudge_text,
)
from .main_token_guard import maybe_append_token_guard
from .tool_loop_guard import raise_if_tool_call_loop
from .tools import ImageAwareToolNode, filter_main_agent_tools, get_all_tools
from .tools.session_tools import apply_session_tool_overrides

logger = logging.getLogger(__name__)

# 空応答（無言終了）を検知した際に注入する、最終回答を促す短いリマインダー。
EMPTY_RESPONSE_NUDGE = (
    "(Automatic reminder: your last response was empty. If no more tool calls are needed, "
    "write your final answer to the user as text now, based on the work so far. "
    "If the work is not finished, continue with the next tool call.)"
)


async def _build_handwritten_graph(config: Config, system_prompt: str, checkpointer, *, wait_when_busy: bool = True):
    """手書きの ReAct グラフをコンパイルして返す。

    agent ノード（call_model）と tools ノード（ImageAwareToolNode(main_tools)）を
    START → agent → (条件分岐) → tools → agent → ... → END という
    ReAct ループとして明示配線する。main_tools は get_all_tools() を
    filter_main_agent_tools() で絞り込んだもの（[main_agent_tool_guard] 有効時、
    呼び出し不可なツールは docstring ごとモデルへ見せない。詳細は
    filter_main_agent_tools のdocstring参照）。

    Args:
        config: アプリ設定（LLM 接続情報）。build_model 経由でモデル構築に使う。
        system_prompt: スキル一覧を注入済みのシステムプロンプト。
            call_model が毎回メッセージ列の先頭に付与する。
        checkpointer: AsyncSqliteSaver など LangGraph のチェックポインタ。
            会話状態（MessagesState）の永続化に使う。
        wait_when_busy: build_model() の同名引数にそのまま渡す。

    Returns:
        コンパイル済みの LangGraph（CompiledStateGraph）。
        astream_events / ainvoke などで実行できる。
    """
    main_tools = filter_main_agent_tools(apply_session_tool_overrides(get_all_tools()), config)
    model = enable_thinking_control(
        await build_model(config, role="main", wait_when_busy=wait_when_busy), config, "main"
    ).bind_tools(main_tools)

    async def call_model(state: MessagesState) -> dict:
        """agent ノード: システムプロンプトを先頭に付けてモデルを呼ぶ。

        Args:
            state: これまでの会話履歴を保持する MessagesState
                （"messages" キーに HumanMessage/AIMessage/ToolMessage の列）。

        Returns:
            {"messages": [response]} の形の dict。LangGraph の
            add_messages リデューサにより既存の履歴へ追記される。
            response は tool_calls を含みうる AIMessage。
        """
        history = state["messages"]
        if config.context_trim_enabled and is_trigger_reached(
            history, config.context_trim_trigger_total_tokens
        ):
            # 古い ToolMessage（サイズ上限のないツール実行結果）が会話履歴に
            # 蓄積し続けるとプロンプトプリフィルが極端に遅くなるため、モデル
            # への入力だけを間引く（state 自体・checkpointer上の永続履歴は
            # 書き換えない。src/context_trim.py 参照）。トリム自体の発動は
            # context_trim_trigger_total_tokens（Claude API の context editing
            # 相当）の閾値到達が条件（is_trigger_reached）。
            history = trim_old_tool_messages(
                history,
                keep_recent_iterations=config.context_trim_keep_recent_tool_iterations,
                max_chars=config.context_trim_truncated_max_chars,
                guarded_tool_max_chars=config.context_trim_duplicate_guard_tool_max_chars,
            )
            if config.context_trim_ai_messages:
                # モデル自身が tool_calls の引数へファイル本文を書き写した場合、
                # ToolMessage 側だけを絞っても入力は膨らみ続けるため AIMessage も
                # 対象にする（src/context_trim.py 参照）。
                history = trim_old_ai_messages(
                    history,
                    keep_recent_iterations=config.context_trim_keep_recent_ai_iterations,
                    max_chars=config.context_trim_truncated_max_chars,
                )
        # 全く同じ応答（ツール呼び出し）を繰り返していれば、LLMを呼ばずに打ち切って
        # 注意メッセージ付きの再試行へ回す（src/tool_loop_guard.py）。下のガード・
        # ナッジが末尾に HumanMessage を足すと連続が途切れたとみなされ、閾値超過中は
        # 検知できなくなるため、それらより前に判定する。
        raise_if_tool_call_loop(history, config)
        # 切り詰めてもなお1リクエストあたりのトークン量が閾値に達している場合は、
        # 引継ぎプロンプトを出させて安全に終えるよう促す（src/main_token_guard.py）。
        before_guard = history
        history = maybe_append_token_guard(history, config)
        if history is before_guard:
            # 引継ぎプロンプト（会話全体を打ち切る）が発動しなかった場合のみ、
            # コンテキスト圧縮が近づいている旨とwrite_thread_noteへの書き出しを
            # 促す（src/context_compaction.py）。両方を同時に差し込むと
            # 「ツールを呼ぶな」と「write_thread_noteを呼べ」が矛盾するため。
            history = maybe_append_precompact_note_nudge(history, config)
        messages = [SystemMessage(content=system_prompt), *history]
        response = await model.ainvoke(messages)
        return {"messages": [response]}

    def should_continue(state: MessagesState) -> str:
        """直近 AI メッセージに tool_calls があれば tools、無ければ終了。

        add_conditional_edges から呼ばれる分岐関数。

        Args:
            state: agent ノード実行後の MessagesState。

        Returns:
            次に遷移するノード名 "tools"、またはループを終了させる
            LangGraph の終端マーカー END。
        """
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return END

    # --- 2 手書きノードを明示配線（ここがループ本体） ---
    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", ImageAwareToolNode(main_tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")  # ツール結果を持って再びモデルへ（ReAct）

    return graph.compile(checkpointer=checkpointer)


async def _build_prebuilt_graph(config: Config, system_prompt: str, checkpointer, *, wait_when_busy: bool = True):
    """LangGraph 標準の create_react_agent にそのまま委譲してグラフを作る。

    ノード配線・tool_calls の分岐はすべて create_react_agent 内部に隠蔽される。
    モデルは bind_tools せずに渡す（create_react_agent が内部で
    ImageAwareToolNode に渡した tools を bind する）。tools は get_all_tools() を
    filter_main_agent_tools() で絞り込んだもの（[main_agent_tool_guard] 有効時、
    呼び出し不可なツールは docstring ごとモデルへ見せない）。

    Args:
        config: アプリ設定（LLM 接続情報）。build_model 経由でモデル構築に使う。
        system_prompt: スキル一覧を注入済みのシステムプロンプト。
            create_react_agent の prompt 引数にそのまま渡す。
        checkpointer: AsyncSqliteSaver など LangGraph のチェックポインタ。
        wait_when_busy: build_model() の同名引数にそのまま渡す。

    Returns:
        コンパイル済みの LangGraph（CompiledStateGraph）。
        astream_events / ainvoke などで実行できる。
    """
    model = enable_thinking_control(await build_model(config, role="main", wait_when_busy=wait_when_busy), config, "main")
    main_tools = filter_main_agent_tools(apply_session_tool_overrides(get_all_tools()), config)

    def pre_model_hook(state: MessagesState) -> dict:
        """モデル呼び出し直前に、入力を絞り、必要なら引継ぎ促しを差し込む。

        create_react_agent 標準のフック。戻り値に "llm_input_messages" を
        含めると、今回のモデル呼び出しの入力だけが差し替わり、state 自体・
        checkpointer上の永続履歴はそのまま残る（src/context_trim.py 参照）。
        """
        trimmed = list(state["messages"])
        should_trim = config.context_trim_enabled and is_trigger_reached(
            trimmed, config.context_trim_trigger_total_tokens
        )
        if should_trim:
            trimmed = trim_old_tool_messages(
                trimmed,
                keep_recent_iterations=config.context_trim_keep_recent_tool_iterations,
                max_chars=config.context_trim_truncated_max_chars,
                guarded_tool_max_chars=config.context_trim_duplicate_guard_tool_max_chars,
            )
        if should_trim and config.context_trim_ai_messages:
            # モデル自身が tool_calls の引数へファイル本文を書き写した場合、
            # ToolMessage 側だけを絞っても入力は膨らみ続けるため AIMessage も
            # 対象にする（src/context_trim.py 参照）。
            trimmed = trim_old_ai_messages(
                trimmed,
                keep_recent_iterations=config.context_trim_keep_recent_ai_iterations,
                max_chars=config.context_trim_truncated_max_chars,
            )
        # 全く同じ応答（ツール呼び出し）を繰り返していれば、LLMを呼ばずに打ち切って
        # 注意メッセージ付きの再試行へ回す（src/tool_loop_guard.py）。下のガード・
        # ナッジが末尾に HumanMessage を足すと連続が途切れたとみなされ、閾値超過中は
        # 検知できなくなるため、それらより前に判定する。
        raise_if_tool_call_loop(trimmed, config)
        # 切り詰めてもなお1リクエストあたりのトークン量が閾値に達している場合は、
        # 引継ぎプロンプトを出させて安全に終えるよう促す（src/main_token_guard.py）。
        before_guard = trimmed
        trimmed = maybe_append_token_guard(trimmed, config)
        if trimmed is before_guard:
            # 引継ぎプロンプト（会話全体を打ち切る）が発動しなかった場合のみ、
            # コンテキスト圧縮が近づいている旨とwrite_thread_noteへの書き出しを
            # 促す（src/context_compaction.py）。両方を同時に差し込むと
            # 「ツールを呼ぶな」と「write_thread_noteを呼べ」が矛盾するため。
            trimmed = maybe_append_precompact_note_nudge(trimmed, config)
        return {"llm_input_messages": trimmed}

    return create_react_agent(
        model,
        ImageAwareToolNode(main_tools),
        prompt=system_prompt,
        # トリミング・トークンガード・圧縮予告ナッジ・ツール呼び出しループ検知の
        # いずれかが有効なら
        # フックが必要（一部だけを見て None にすると、残りが黙って効かなく
        # なる）。
        pre_model_hook=(
            pre_model_hook
            if (
                config.context_trim_enabled
                or config.graph_token_guard_enabled
                or (config.context_compaction_enabled and config.context_compaction_pre_note_threshold > 0)
                or config.tool_loop_guard_enabled
            )
            else None
        ),
        checkpointer=checkpointer,
    )


async def build_graph(config: Config, system_prompt: str, checkpointer, *, wait_when_busy: bool = True):
    """config.graph_impl に応じて手書き／prebuilt のグラフを構築する。

    Args:
        config: アプリ設定。graph_impl（"handwritten" または "prebuilt"）で
            どちらの実装を使うか決める。
        system_prompt: スキル一覧を注入済みのシステムプロンプト。
        checkpointer: AsyncSqliteSaver など LangGraph のチェックポインタ。
            会話状態（MessagesState）の永続化に使う。
        wait_when_busy: build_model() の同名引数にそのまま渡す。既定True
            （空きが出るまで待つ）。app.py の on_chat_start/on_chat_resume
            （直後に生成するとは限らない操作）はFalseを渡す。

    Returns:
        コンパイル済みの LangGraph（CompiledStateGraph）。
        astream_events / ainvoke などで実行できる。

    Raises:
        ValueError: config.graph_impl が "handwritten" / "prebuilt" 以外の場合。
    """
    if config.graph_impl == "handwritten":
        return await _build_handwritten_graph(config, system_prompt, checkpointer, wait_when_busy=wait_when_busy)
    if config.graph_impl == "prebuilt":
        return await _build_prebuilt_graph(config, system_prompt, checkpointer, wait_when_busy=wait_when_busy)
    raise ValueError(f"unknown graph_impl: {config.graph_impl!r}")


def is_empty_final_message(messages: list) -> bool:
    """直近メッセージが「無言終了」（tool_calls も、意味のある本文も無い AIMessage）かを判定する。

    小型ローカルモデルは、長い会話の末に thinking が長引いた・コンテキストが
    逼迫した等の理由で、ツール呼び出しも最終回答テキストも無いまま応答を
    終えることがある（tune-prompt調査で annual_schedule_xlsx_end_to_end 等の
    ケースで実際に再現した）。system_prompt.md の文言強化だけでは解消しない
    ため、コード側でも検知してリトライできるようにする。

    高負荷時には content が完全な空文字列ではなく「…」のような記号のみの
    退化応答になることもある（2026-08-10 issue: サーバー高負荷下で
    `content='…'` を返しターンがそのまま終了し、ユーザーには「…」としか
    表示されなかった）。そのため単純な空文字列判定ではなく、英数字・各言語
    の文字（`str.isalnum()` は日本語の漢字・ひらがな・カタカナも True を
    返す）を一切含まない場合も「空」とみなす。

    Args:
        messages: MessagesState の "messages" リスト。

    Returns:
        直近メッセージが AIMessage かつ tool_calls が無く、content に
        意味のある文字（英数字・各言語の文字）が一つも含まれない場合に True。
    """
    if not messages:
        return False
    last = messages[-1]
    if not isinstance(last, AIMessage):
        return False
    if getattr(last, "tool_calls", None):
        return False
    content = last.content if isinstance(last.content, str) else str(last.content)
    stripped = content.strip()
    if not stripped:
        return True
    return not any(ch.isalnum() for ch in stripped)


async def _remove_message_ids_if_present(graph, run_config: dict, ids: list[str]) -> None:
    """指定した message id のうち、現在のグラフ状態に実在するものだけを RemoveMessage で取り除く。

    app.py の同名ヘルパーと同じ理由づけ（蓄積したnudge idが、呼び出し元による
    状態変更で既に無効化されていてもValueErrorを送出しないようにする）。
    このモジュールは app.py 由来の _CheckpointerTimeout に依存しないため、
    checkpointer操作の例外は個別に捕捉せずそのまま呼び出し元へ伝播させる。
    """
    if not ids:
        return
    state = await graph.aget_state(run_config)
    live_ids = {m.id for m in (state.values.get("messages", []) if state else [])}
    present = [i for i in ids if i in live_ids]
    if present:
        await graph.aupdate_state(run_config, {"messages": [RemoveMessage(id=i) for i in present]})


async def ainvoke_ensuring_final_text(
    graph,
    inputs: dict,
    run_config: dict,
    max_retries: int = 2,
    nudge_messages: list[str] | None = None,
    loop_max_retries: int = 2,
    tool_loop_nudge_messages: list[str] | None = None,
) -> dict:
    """graph.ainvoke() を呼び、無言終了（空応答）や反復ループだった場合は再試行する。

    2種類の異常系を、単一のリトライループ内でどちらのフェーズでも検知して
    自動リトライする（旧実装は「ループ検知の while ループ」と「無言終了の
    for ループ」が分離しており、無言終了リトライ中に発生した
    ThinkingLoopDetected が捕捉されずそのまま送出されてしまう欠陥があった。
    020ケース（tune-prompt iter27）で実際に発生・確認済み。以降は統合する）:
    - 無言終了（空応答）: is_empty_final_message() が True を返す限り、
      EMPTY_RESPONSE_NUDGE を追加の HumanMessage としてグラフへ渡し、最大
      max_retries 回まで再試行する（それでも空応答なら諦めて最後の結果を
      そのまま返す＝呼び出し元は従来通り空の final_answer を受け取りうる）。
    - 反復ループ: ChatLlamaCpp（src/llm.py）がストリーム中に検知して
      ThinkingLoopDetected を送出した場合、pick_loop_nudge_message() で選んだ
      注意メッセージを HumanMessage として注入し、最大 loop_max_retries 回まで
      再試行する。注入した nudge は機械的なものであり、最終的に成功したら
      graph.aupdate_state() の RemoveMessage で会話履歴から取り除く
      （全て失敗した場合も、raise する前に同じ除去処理を必ず経由する。
      旧実装は raise 経路でこの除去が漏れていた）。

    2種類の予算（`max_retries` / `loop_max_retries`）は**互いに独立**に数える。
    全体の最大試行回数（初回 + リトライ）は for ループ自体が
    `total_budget + 1` に制限するため、無限リトライにはならない。
    かつて両フェーズが `attempt >= total_budget` という共有条件も併用していたが、
    これは「序盤にループ検知で予算を使い切ると、終盤の無言終了リトライが
    1回も行われない」という相互侵食を生んでいた（レシピ画像297枚ケースで、
    序盤のループ検知2回により、終盤にツール呼び出しXMLを本文へ書いてしまった
    空応答が1度も再試行されず、35枚処理した時点で無言終了した実例あり）。
    1ターンの中でLLM呼び出しが数十〜数百回に及ぶ長時間タスクでは、序盤と終盤の
    失敗は独立した事象なので、予算も独立させる。

    app.py（Chainlit UI）・evals/run_case.py の両方から共通で使うことを想定する。

    Args:
        graph: build_graph() が返すコンパイル済みグラフ。
        inputs: 最初の ainvoke に渡す入力（例: {"messages": [HumanMessage(...)]})。
        run_config: ainvoke に渡す config（thread_id・recursion_limit 等）。
        max_retries: 空応答時に再試行する最大回数。
        nudge_messages: ループ検知時に注入する注意メッセージの候補
            （config.ini の [thinking_loop_guard].nudge_messages 由来）。
            省略時は空リスト（pick_loop_nudge_message の既定文言を使う）。
        loop_max_retries: ループ検知時に再試行する最大回数
            （config.ini の [thinking_loop_guard].max_retries 由来）。
        tool_loop_nudge_messages: ツール呼び出しのループ（ToolCallLoopDetected）を
            検知したときに注入する注意メッセージの候補（config.ini の
            [tool_loop_guard].nudge_messages 由来）。省略時は組み込みの既定文言。

    Returns:
        graph.ainvoke() と同じ形式の結果 dict（{"messages": [...]})。

    Raises:
        ThinkingLoopDetected: ループ検知後、loop_max_retries 回再試行しても
            なお反復ループが解消しなかった場合（無言終了リトライ中に発生した
            場合も含む）。
    """
    nudge_messages = nudge_messages or []
    remove_ids: list[str] = []
    loop_attempt = 0
    tool_loop_attempt = 0
    empty_attempt = 0
    total_budget = max_retries + loop_max_retries
    current_inputs = inputs
    result: dict | None = None

    for attempt in range(total_budget + 1):
        try:
            result = await graph.ainvoke(current_inputs, config=run_config)
        except ThinkingLoopDetected as exc:
            if isinstance(exc, ToolCallLoopDetected):
                # 繰り返した応答を残すと、注意しても同じ tool_call を書き写し続ける
                # （src/tool_loop_guard.py 冒頭参照）。上限で打ち切る場合も、
                # 次のターンで書き写させないよう、また exc.detail の
                # 「取り除きました」と食い違わないよう取り除く。
                await _remove_message_ids_if_present(graph, run_config, list(exc.remove_ids))
            if loop_attempt >= loop_max_retries:
                await _remove_message_ids_if_present(graph, run_config, remove_ids)
                raise
            logger.warning(
                "Detected an LLM response loop (retry %d): recent text=%r",
                loop_attempt + 1,
                exc.snippet,
            )
            nudge_id = str(uuid.uuid4())
            remove_ids.append(nudge_id)
            if isinstance(exc, ToolCallLoopDetected):
                text = tool_loop_nudge_text(tool_loop_nudge_messages or [], tool_loop_attempt, exc)
                tool_loop_attempt += 1
            else:
                text = pick_loop_nudge_message(nudge_messages, loop_attempt)
            current_inputs = {"messages": [HumanMessage(content=text, id=nudge_id)]}
            loop_attempt += 1
            continue

        if not is_empty_final_message(result.get("messages") or []):
            break
        if empty_attempt >= max_retries:
            break
        nudge_id = str(uuid.uuid4())
        remove_ids.append(nudge_id)
        current_inputs = {"messages": [HumanMessage(content=EMPTY_RESPONSE_NUDGE, id=nudge_id)]}
        empty_attempt += 1

    # ループ検知・無言終了いずれの注意メッセージも機械的な注入であり、
    # 会話履歴に残すとリトライのたびに全量再送されて長い会話ほど
    # コンテキストを圧迫する（かつ「過去に失敗した」痕跡がモデル自身の
    # 目に触れ続けることが、同種の失敗を誘発する自己参照的な悪循環に
    # つながりうる）。thinking_loopのnudgeは元々ここで除去していたが、
    # 無言終了のnudgeは対象外になっていた非対称性を解消する
    # （tune-prompt iter22の長時間会話コンテキスト肥大化調査で発見）。
    await _remove_message_ids_if_present(graph, run_config, remove_ids)
    return result
