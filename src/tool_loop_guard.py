"""同じ応答（ツール呼び出し）を連続で繰り返すループの検知（[tool_loop_guard]）。

小型ローカルモデルは、ツールの結果（特にエラー）を読んでも、全く同じ応答を
繰り返すことがある。eval 008（2026-10-04）では、本文とツール呼び出し（名前・
引数）が完全に同じ応答を30回連続で出し、トークン上限まで消費して成果物を
作れずに終わった。

ここでは、どのツールのどんな結果かを問わず、モデルの応答（本文と
ツール呼び出しの名前・引数）が連続で全く同じなら、それをループとみなす。
思考（reasoning）は毎回揺れるため比べない。回復は、ストリーム中の反復ループ
（ThinkingLoopDetected）と同じ仕組み（注意メッセージを入れて再試行し、上限で
停止を通知する）に任せる。Qwen3.6 では tool_choice によるツール呼び出しの
強制が効かないため、モデルの強制機能には頼らない。

再試行の前に、繰り返した応答（AIMessage とその結果）を会話履歴から取り除く。
残したままだと、モデルは注意メッセージを読んで「別のツールを呼ぶ」と思考しても、
履歴の同じ tool_call をトークン単位で書き写してしまい、何度注意しても止まらない
（2026-10-04 本番ログ: 引数に tool_call の XML が漏れた write_thread_note を
7周繰り返した）。何を繰り返したかは注意メッセージ（ToolLoop.detail）で伝える。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from .config import Config
from .images import is_image_followup_message
from .llm import ToolCallLoopDetected

# 注意メッセージに添える結果の抜粋の最大文字数。
_EXCERPT_CHARS = 200


@dataclass(frozen=True)
class ToolLoop:
    """検知したループの内容。

    Attributes:
        repeats: 同じ応答が連続した回数。
        tool_names: 繰り返された応答で呼ばれたツール名（重複なし、呼び出し順）。
        result_excerpt: 直近の応答に対するツール結果の抜粋（最初のツール結果）。
        start_index: 繰り返しの最初の AIMessage の、渡された messages 内の位置
            （messages[start_index:] が繰り返した区間）。
        message_ids: 繰り返した区間の全メッセージの id。id の無いメッセージが
            1件でもあれば空（一部だけ消すと tool_call と結果の対応が崩れるため）。
    """

    repeats: int
    tool_names: tuple[str, ...]
    result_excerpt: str
    start_index: int = 0
    message_ids: tuple[str, ...] = ()

    def detail(self, removed: bool = False) -> str:
        """注意メッセージに添える説明を返す。

        Args:
            removed: 繰り返した応答を会話履歴から取り除いた場合は True。
        """
        names = ", ".join(self.tool_names)
        note = "The repeated calls were removed from the conversation history. " if removed else ""
        return (
            f"(Auto-detected: you repeated the exact same tool call ({names}) in the last {self.repeats} attempts. "
            f"{note}Its result: {self.result_excerpt})"
        )


def _content_text(message: BaseMessage) -> str:
    return message.content if isinstance(message.content, str) else str(message.content)


def _response_signature(ai: AIMessage) -> tuple:
    """応答の署名（本文とツール呼び出しの名前・引数）。思考は含めない。"""
    calls = sorted(
        (call.get("name") or "", json.dumps(call.get("args"), ensure_ascii=False, sort_keys=True, default=str))
        for call in ai.tool_calls
    )
    return (_content_text(ai).strip(), tuple(calls))


def detect_tool_call_loop(messages: list[BaseMessage], config: Config) -> ToolLoop | None:
    """末尾から連続して全く同じ応答（ツール呼び出し）が max_repeats 回続いていれば返す。

    ツールの結果は比べない（エラーかどうかも問わない）。末尾が ToolMessage
    （画像のフォローアップは読み飛ばす）でなければ、連続は途切れているとみなす
    （注意メッセージ・ナッジ等の HumanMessage を挟んだ後は、また max_repeats 回
    繰り返すまで検知しない）。exclude_tools だけを呼ぶ応答（状態確認の
    ポーリング）でも連続は途切れる。

    Args:
        messages: モデルへ渡す直前の会話履歴。
        config: tool_loop_guard_* を含むアプリ設定。

    Returns:
        検知したループ。無ければ（無効化されている場合も）None。
    """
    if not config.tool_loop_guard_enabled or config.tool_loop_guard_max_repeats <= 1:
        return None
    exclude = set(config.tool_loop_guard_exclude_tools)

    first_signature: tuple | None = None
    first_results: list[ToolMessage] = []
    repeats = 0
    results: list[ToolMessage] = []
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if is_image_followup_message(message):
            continue
        if isinstance(message, ToolMessage):
            results.append(message)
            continue
        if not isinstance(message, AIMessage) or not results:
            break
        results.reverse()
        if all((m.name or "") in exclude for m in results):
            break
        signature = _response_signature(message)
        if first_signature is None:
            first_signature, first_results = signature, results
        elif signature != first_signature:
            break
        repeats += 1
        if repeats >= config.tool_loop_guard_max_repeats:
            names = tuple(dict.fromkeys(m.name or "" for m in first_results))
            excerpt = _content_text(first_results[0]).strip().replace("\n", " ")[:_EXCERPT_CHARS]
            tail = messages[index:]
            ids = tuple(m.id for m in tail) if all(m.id for m in tail) else ()
            return ToolLoop(
                repeats=repeats,
                tool_names=names,
                result_excerpt=excerpt,
                start_index=index,
                message_ids=ids,
            )
        results = []
    return None


def raise_if_tool_call_loop(messages: list[BaseMessage], config: Config) -> None:
    """ループを検知したら ToolCallLoopDetected を送出する（メインエージェント用）。

    繰り返した区間のメッセージ id を例外に載せ、再試行する側
    （app.py・ainvoke_ensuring_final_text）がチェックポイントから取り除く。

    Args:
        messages: モデルへ渡す直前の会話履歴。
        config: tool_loop_guard_* を含むアプリ設定。

    Raises:
        ToolCallLoopDetected: ループを検知した場合。
    """
    loop = detect_tool_call_loop(messages, config)
    if loop is not None:
        raise ToolCallLoopDetected(loop.detail(removed=bool(loop.message_ids)), remove_ids=loop.message_ids)


# 引数へ漏れた tool_call の XML（Qwen系の <function=..><parameter=..>..</parameter></function> 書式）。
# 閉じタグの直後に次の閉じタグ・開きタグが続く形に限り、書式を説明する文章などの誤検知を避ける。
_LEAKED_TOOL_MARKUP_RE = re.compile(r"</parameter>\s*(?:</function>|<parameter=)")


def _has_unmatched_close(value: str) -> bool:
    """値の中に、対応する <parameter= の無い </parameter>（漏れの形）があるか。

    漏れた値は引数の途中から始まるため、最初の閉じタグの前に開きタグが無い。
    書式の正しい例（<parameter=..>..</parameter></function>）を含む文書・
    テストコードの書き込みは開きと閉じが対応するので、ここでは弾かない。
    """
    for match in _LEAKED_TOOL_MARKUP_RE.finditer(value):
        closes = value.count("</parameter>", 0, match.start()) + 1
        if value.count("<parameter=", 0, match.start()) < closes:
            return True
    return False


def find_leaked_tool_markup(args) -> str | None:
    """tool_call の引数に tool_call の XML が漏れていれば、その引数名を返す。

    llama-server がモデル出力の tool_call を取り違えると、次の引数や次の
    tool_call の XML が引数の値に混ざる。そのまま実行すると壊れた値で
    副作用（書き込み等）が起き、履歴に残った壊れた tool_call をモデルが
    書き写して同じ呼び出しを繰り返す（2026-10-04 本番ログ）。

    Args:
        args: tool_call の args（dict。入れ子の list/dict も調べる）。

    Returns:
        漏れが見つかった引数名。無ければ None。
    """
    if not isinstance(args, dict):
        return None

    def _has_markup(value) -> bool:
        if isinstance(value, str):
            return _has_unmatched_close(value)
        if isinstance(value, dict):
            return any(_has_markup(v) for v in value.values())
        if isinstance(value, list):
            return any(_has_markup(v) for v in value)
        return False

    for key, value in args.items():
        if _has_markup(value):
            return str(key)
    return None


def leaked_tool_markup_error(name: str, key: str) -> str:
    """find_leaked_tool_markup が見つけた場合に返すエラー文。"""
    return (
        f"Error: the argument {key} of {name} contains tool call syntax (such as </parameter>). "
        "The tool was not run. Split the arguments correctly one by one and call it again."
    )
