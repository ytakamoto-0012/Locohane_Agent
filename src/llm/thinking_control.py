"""ReActループの各ステップで思考（thinking）レベルを機械的に切り替える。

ローカルLLMでは思考トークンの生成が応答の遅さの大きな原因になる。一方で
「Read/Grep が成功した直後に次のツールを呼ぶだけ」のようなルーチン的な
ステップまで、計画を立てるときと同じだけ思考させる必要はない。そこで、
送信直前のメッセージ列（直前の履歴の形）からルールでレベルを選び、
リクエストの extra_body を差し替える（ChatLlamaCpp._get_request_payload から
呼ばれる。[thinking_control] 参照）。

レベルの実現方法はプロンプトの先頭部分を変えないものに限る（KVキャッシュを
外さないため）:
- off   : chat_template_kwargs.enable_thinking=false
- それ以外: 思考予算（llama-server: reasoning_budget_tokens /
  vLLM: thinking_token_budget）の大小
reasoning_effort は使わない。Qwen3.6 の公式テンプレートは読まず、対応させる
コミュニティ版テンプレートはシステムプロンプトへ指示文を足すため、ステップ
ごとに切り替えるとキャッシュが外れる。

判定は履歴だけから決まる純粋関数で、状態を保存しない。「直前のステップの
レベル」は、最後のユーザー発言から順に判定をやり直して求める（_replay）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from ..config import THINKING_LEVELS, Config
from ..images import is_image_followup_message
from .dialect import budget_params, extract_reasoning

# レベルを比較するための順位（off が最小、xhigh が最大）。
_RANK = {level: i for i, level in enumerate(THINKING_LEVELS)}

# リクエストの extra_body に入りうる思考予算のキー（provider ごとに名前が違う）。
_BUDGET_KEYS = ("reasoning_budget_tokens", "thinking_token_budget")


@dataclass(frozen=True)
class ThinkingControlSettings:
    """select_level / apply_level が使う設定（Config から作る）。

    Attributes:
        rule_user_turn: ルール1（末尾がユーザー発言）で使うレベル。None なら無効。
        rule_tool_error: ルール2（直前のツールがエラー）で使うレベル。None なら無効。
        rule_consecutive_cap: ルール3（このレベル未満が続いた）で使うレベル。
            None なら無効。
        rule_after_tools: ルール4（直前のツールが全て after_tools にある）で
            使うレベル。None なら無効。
        default_level: どのルールにも当たらなかったときのレベル。
        budgets: レベルごとの思考予算。None なら予算を送らない（サーバー既定）。
            off は予算を使わないため含めない。
        after_tools: ルール4の対象ツール名。
        error_prefixes: ツールの結果がこの文字列で始まればエラーとみなす。
        max_consecutive_reduced: ルール3が働くまでの連続回数。0以下なら働かない。
        budget_message: [llm].reasoning_budget_message。予算での打ち切り・本文への
            漏れの検出（ログ用）に使う。None なら検出しない。
    """

    rule_user_turn: str | None
    rule_tool_error: str | None
    rule_consecutive_cap: str | None
    rule_after_tools: str | None
    default_level: str
    budgets: dict[str, int | None]
    after_tools: frozenset[str]
    error_prefixes: tuple[str, ...]
    max_consecutive_reduced: int
    budget_message: str | None


def settings_from_config(config: Config) -> ThinkingControlSettings:
    """Config の thinking_control_* から ThinkingControlSettings を作る。

    予算が空欄のレベルは [llm].reasoning_budget を使う（それも空欄なら
    予算を送らず、サーバー既定に委ねる）。

    Args:
        config: アプリ設定。

    Returns:
        ThinkingControlSettings。
    """
    budgets = {
        "low": config.thinking_control_budget_low,
        "medium": config.thinking_control_budget_medium,
        "high": config.thinking_control_budget_high,
        "xhigh": config.thinking_control_budget_xhigh,
    }
    return ThinkingControlSettings(
        rule_user_turn=config.thinking_control_rule_user_turn,
        rule_tool_error=config.thinking_control_rule_tool_error,
        rule_consecutive_cap=config.thinking_control_rule_consecutive_cap,
        rule_after_tools=config.thinking_control_rule_after_tools,
        default_level=config.thinking_control_default_level,
        budgets={level: config.reasoning_budget if budget is None else budget for level, budget in budgets.items()},
        after_tools=frozenset(config.thinking_control_after_tools),
        error_prefixes=tuple(config.thinking_control_error_prefixes),
        max_consecutive_reduced=config.thinking_control_max_consecutive_reduced,
        budget_message=config.reasoning_budget_message,
    )


def _is_tool_error(message: ToolMessage, error_prefixes: tuple[str, ...]) -> bool:
    if getattr(message, "status", None) == "error":
        return True
    content = message.content if isinstance(message.content, str) else str(message.content)
    return content.lstrip().startswith(error_prefixes) if error_prefixes else False


def _first_step(settings: ThinkingControlSettings) -> tuple[str, str]:
    if settings.rule_user_turn is not None:
        return settings.rule_user_turn, "user_turn"
    return settings.default_level, "default"


def _next_step(
    prev_ai: AIMessage,
    tool_messages: list[ToolMessage],
    history: list[str],
    settings: ThinkingControlSettings,
) -> tuple[str, str]:
    """直前のAIMessageとそのツール結果から、次のリクエストのレベルを決める。"""
    if settings.rule_tool_error is not None and any(_is_tool_error(m, settings.error_prefixes) for m in tool_messages):
        return settings.rule_tool_error, "tool_error"

    n = settings.max_consecutive_reduced
    cap = settings.rule_consecutive_cap
    if cap is not None and n > 0 and len(history) >= n and all(_RANK[level] < _RANK[cap] for level in history[-n:]):
        return cap, "consecutive_cap"

    if settings.rule_after_tools is not None:
        names = [call.get("name") for call in prev_ai.tool_calls]
        # 一覧にないツールが1つでも混ざっていれば default_level に任せる。
        if names and all(name in settings.after_tools for name in names):
            return settings.rule_after_tools, "after_tools"

    return settings.default_level, "default"


def _replay(messages: list[BaseMessage], settings: ThinkingControlSettings) -> list[tuple[str, str]]:
    """最後のユーザー発言以降の各リクエストについて (レベル, 理由) を順に求める。

    返り値の末尾が「これから送るリクエスト」のレベル。それより前は、同じ
    ルールで判定し直した過去のステップのレベル（ルール3が参照する）。
    """
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage) and not is_image_followup_message(messages[i]):
            start = i + 1
            break

    steps = [_first_step(settings)]
    prev_ai: AIMessage | None = None
    tool_messages: list[ToolMessage] = []
    for message in messages[start:]:
        if isinstance(message, AIMessage):
            if prev_ai is not None:
                steps.append(_next_step(prev_ai, tool_messages, [level for level, _ in steps], settings))
            prev_ai = message
            tool_messages = []
        elif isinstance(message, ToolMessage):
            tool_messages.append(message)
    if prev_ai is not None:
        steps.append(_next_step(prev_ai, tool_messages, [level for level, _ in steps], settings))
    return steps


def select_level(messages: list[BaseMessage], settings: ThinkingControlSettings) -> tuple[str, str, str | None]:
    """これから送るリクエストの思考レベルを選ぶ。

    最後の HumanMessage（画像のフォローアップは除く）以降を対象に、次の順で最初に当てはまったものを使う
    （無効化されたルールは飛ばす）。

    1. 末尾が HumanMessage（ユーザー発言・ナッジ等）→ rule_user_turn
    2. 直前のツール結果にエラーがある → rule_tool_error
    3. 直前 max_consecutive_reduced 回が全て rule_consecutive_cap 未満
       → rule_consecutive_cap
    4. 直前に呼んだツールが全て after_tools にある → rule_after_tools
    5. それ以外 → default_level

    Args:
        messages: 送信しようとしているメッセージ列（SystemMessage を含んでよい）。
        settings: ThinkingControlSettings。

    Returns:
        (レベル, 判定理由のルール名, 直前のステップのレベル)。直前のステップが
        無ければ3つ目は None。
    """
    steps = _replay(messages, settings)
    level, reason = steps[-1]
    prev_level = steps[-2][0] if len(steps) >= 2 else None
    return level, reason, prev_level


def apply_level(
    extra_body: dict[str, Any] | None, level: str, settings: ThinkingControlSettings, provider: str
) -> dict[str, Any]:
    """extra_body のコピーへ、レベルに対応する思考パラメータを反映して返す。

    Args:
        extra_body: build_extra_body() が組み立てた元の extra_body（変更しない）。
        level: select_level() が選んだレベル。
        settings: ThinkingControlSettings。
        provider: 送信先の LLMEndpoint.provider。

    Returns:
        新しい extra_body。off なら enable_thinking=false にして予算のキーを
        外す。それ以外は enable_thinking を元の値のままにし、予算のキーだけを
        レベルの値に置き換える。
    """
    result = dict(extra_body or {})
    for key in _BUDGET_KEYS:
        result.pop(key, None)
    if level == "off":
        chat_template_kwargs = dict(result.get("chat_template_kwargs") or {})
        chat_template_kwargs["enable_thinking"] = False
        result["chat_template_kwargs"] = chat_template_kwargs
    else:
        result.update(budget_params(provider, settings.budgets[level]))
    return result


def previous_step_stats(messages: list[BaseMessage], settings: ThinkingControlSettings) -> dict[str, Any] | None:
    """直前のAIMessageの思考量と、予算での打ち切り・本文への漏れを調べる（ログ用）。

    予算を使い切ると、サーバーが思考の末尾に budget_message を挿入する。
    これが reasoning_content にあれば打ち切られた、content にあれば思考が
    本文へ漏れたとみなす。

    Args:
        messages: 送信しようとしているメッセージ列。
        settings: ThinkingControlSettings。

    Returns:
        {"reasoning_chars", "truncated", "leaked"} の dict。最後のユーザー
        発言より後に AIMessage が無ければ None。budget_message が未設定なら
        truncated/leaked は None。
    """
    for message in reversed(messages):
        if isinstance(message, HumanMessage) and not is_image_followup_message(message):
            return None
        if isinstance(message, AIMessage):
            reasoning = extract_reasoning(message.additional_kwargs) or ""
            content = message.content if isinstance(message.content, str) else str(message.content)
            marker = settings.budget_message
            return {
                "reasoning_chars": len(reasoning),
                "truncated": (marker in reasoning) if marker else None,
                "leaked": (marker in content) if marker else None,
            }
    return None


def unknown_tool_names(config: Config, known_names: set[str]) -> list[str]:
    """[thinking_control].after_tools のうち、実在しないツール名を返す（起動時の警告用）。

    Args:
        config: アプリ設定。
        known_names: 実在するツール名（get_all_tools() の name）。

    Returns:
        known_names に無いツール名（書かれた順、重複なし）。
    """
    return [name for name in dict.fromkeys(config.thinking_control_after_tools) if name not in known_names]
