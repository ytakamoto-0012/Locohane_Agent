"""推論サーバーごとの reasoning（thinking）方言の差を吸収する純粋関数群。

llama-server と vLLM は、どちらも OpenAI 互換 API だが thinking まわりの
フィールド名・拡張パラメータが異なる。tools/probe_reasoning_dialect.py で
実測した結果（llama-server b10437 / Qwen3.8、vLLM 0.30.0 / Qwen3.5-9B-AWQ、
2026-09-26）に基づく対応は次のとおり。

- 応答の thinking: llama-server は `reasoning_content`、vLLM は `reasoning`
  のみ（vLLM の旧版・互換モードでは両方に同じ内容が入りうる）。受信側は
  provider に関係なく両方読み、内部では additional_kwargs["reasoning_content"]
  に正規化する（extract_reasoning）。
- 履歴へ戻す thinking: llama-server は `reasoning_content` のみ、vLLM は
  `reasoning` のみ反映し、もう一方は無視する。両方送っても重複・エラーは
  無い（両サーバーで実測）。
- 履歴へ戻す範囲: 最後のユーザー発話（mark_user_turn の目印付き）より後の
  assistant メッセージだけ。それより前のユーザーターンの thinking は送らない
  （Qwen3/MiniMax のテンプレート・Claude/OpenAI 旧モデルと同じ切り方）。
- 拡張パラメータ: 対応表は build_extra_body() の docstring 参照。

送信側は接続先ごとの LLMEndpoint.provider で切り替える。"openai_compatible"
は後方互換のため llama-server と同じパラメータを送り、履歴の thinking は
どちらのサーバーでも読まれるよう両方のキーで送る。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from langchain_core.messages import BaseMessage, HumanMessage

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger(__name__)

# 応答の thinking を探すキー（先勝ち）。vLLM の互換モードは両方に同じ内容を
# 入れるため、連結せず先に見つかった方だけを採用する。
_RESPONSE_REASONING_KEYS = ("reasoning", "reasoning_content")

# 履歴の assistant メッセージへ thinking を載せ直す際のキー。
_HISTORY_REASONING_KEYS: dict[str, tuple[str, ...]] = {
    "llama_cpp": ("reasoning_content",),
    "vllm": ("reasoning",),
    "openai_compatible": ("reasoning_content", "reasoning"),
}

# ユーザー自身の入力から作った HumanMessage に付ける目印（additional_kwargs のキー）。
# nudge・画像フォローアップ等アプリが差し込む HumanMessage と区別し、thinking を
# 載せ直す範囲の境界に使う（ChatLlamaCpp._get_request_payload 参照）。
# langchain_openai は HumanMessage の additional_kwargs を（name 以外）送らない
# ため、LLM への入力には影響しない。
USER_TURN_KWARG = "locohane_user_turn"

# vLLM に相当パラメータが無いため送らない config 項目。
_VLLM_UNSUPPORTED_KEYS = (
    "dry_multiplier",
    "dry_base",
    "dry_allowed_length",
    "dry_penalty_last_n",
    "dry_sequence_breakers",
    "reasoning_format",
    "reasoning_budget_message",
)

# 同じ警告は build_model() のたびに出さず1回だけにする（キーは (provider, 項目)）。
_warned_unsupported: set[tuple[str, str]] = set()


def extract_reasoning(fields: dict[str, Any] | None) -> str | None:
    """delta / message / additional_kwargs から thinking テキストを取り出す。

    Args:
        fields: ストリームの delta、非ストリームの message、または
            AIMessage.additional_kwargs に相当する dict。

    Returns:
        `reasoning` → `reasoning_content` の順で最初に見つかった空でない
        文字列。どちらも無ければ None。
    """
    if not fields:
        return None
    for key in _RESPONSE_REASONING_KEYS:
        value = fields.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def history_reasoning_keys(provider: str) -> tuple[str, ...]:
    """履歴の assistant メッセージへ thinking を載せるキーを返す。

    Args:
        provider: LLMEndpoint.provider。

    Returns:
        書き込むキーのタプル。未知の provider は "openai_compatible" 扱い。
    """
    return _HISTORY_REASONING_KEYS.get(provider, _HISTORY_REASONING_KEYS["openai_compatible"])


def mark_user_turn(message: HumanMessage) -> HumanMessage:
    """ユーザー自身の発話である目印（USER_TURN_KWARG）を付けて返す。

    Args:
        message: ユーザー入力から組み立てた HumanMessage（書き換える）。

    Returns:
        同じ message。
    """
    message.additional_kwargs[USER_TURN_KWARG] = True
    return message


def last_user_turn_index(messages: Sequence[BaseMessage]) -> int | None:
    """目印付きの HumanMessage のうち最後のものの位置を返す。

    Args:
        messages: 会話履歴。

    Returns:
        位置。目印付きが1件も無ければ None（目印導入前のスレッド・
        サブエージェント等）。
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, HumanMessage) and m.additional_kwargs.get(USER_TURN_KWARG):
            return i
    return None


def _warn_once(warn_key: tuple[str, str], message: str) -> None:
    if warn_key in _warned_unsupported:
        return
    _warned_unsupported.add(warn_key)
    logger.warning(message)


def _warn_unsupported_once(provider: str, key: str) -> None:
    _warn_once((provider, key), f"[llm].{key} is not sent to endpoints with provider={provider} because they have no matching parameter")


def build_extra_body(config: Config, provider: str) -> dict[str, Any]:
    """OpenAI 標準 API に無い拡張パラメータ（extra_body）を provider 別に組み立てる。

    None（config.ini で空欄）の項目は送らずサーバー既定に委ねる。

    | config.ini [llm]          | llama_cpp / openai_compatible | vllm                    |
    |---------------------------|-------------------------------|-------------------------|
    | top_k                     | top_k                         | top_k                   |
    | repeat_penalty            | repeat_penalty                | repetition_penalty      |
    | dry_*                     | dry_*                         | 送らない（警告）        |
    | enable_thinking           | chat_template_kwargs          | chat_template_kwargs    |
    | reasoning_preserve        | chat_template_kwargs の preserve_reasoning / preserve_thinking（両方）|
    | reasoning_effort          | reasoning_effort（default は送らない。none なら enable_thinking=false も送る）| 同左 |
    | reasoning_format          | reasoning_format              | 送らない（警告）        |
    | reasoning_budget          | reasoning_budget_tokens       | thinking_token_budget（-1 は送らない）|
    | reasoning_budget_message  | reasoning_budget_message      | 送らない（警告）        |

    llama-server はリクエストの `reasoning_budget` を黙って無視し、
    `reasoning_budget_tokens` のみ反映する（実測）。`reasoning_effort=default`
    はテンプレートへ文字列 "default" がそのまま渡り、対応していない
    テンプレートでは HTTP 500 になる（実測）ため、どの provider でも送らない。

    Args:
        config: アプリ設定。
        provider: 送信先の LLMEndpoint.provider。

    Returns:
        ChatOpenAI の extra_body に渡す dict（空の場合もある）。
    """
    is_vllm = provider == "vllm"
    extra_body: dict[str, Any] = {}

    if config.top_k is not None:
        extra_body["top_k"] = config.top_k
    if config.repeat_penalty is not None:
        extra_body["repetition_penalty" if is_vllm else "repeat_penalty"] = config.repeat_penalty

    llama_only: dict[str, Any] = {
        "dry_multiplier": config.dry_multiplier,
        "dry_base": config.dry_base,
        "dry_allowed_length": config.dry_allowed_length,
        "dry_penalty_last_n": config.dry_penalty_last_n,
        "dry_sequence_breakers": config.dry_sequence_breakers,
        "reasoning_format": config.reasoning_format,
        "reasoning_budget_message": config.reasoning_budget_message,
    }
    for key, value in llama_only.items():
        if value is None:
            continue
        if is_vllm:
            _warn_unsupported_once(provider, key)
        else:
            extra_body[key] = value

    chat_template_kwargs: dict[str, Any] = {}
    if config.reasoning_effort == "none":
        # vLLM は chat_template_kwargs.enable_thinking=true を top-level の
        # reasoning_effort=none より優先し、thinking が止まらない（実測）。
        # enable_thinking だけを読むテンプレートでも止まるよう false で送る。
        if config.enable_thinking:
            _warn_once(
                ("*", "reasoning_effort=none"),
                "[llm].reasoning_effort=none, so [llm].enable_thinking=true is ignored and enable_thinking=false is sent",
            )
        chat_template_kwargs["enable_thinking"] = False
    elif config.enable_thinking is not None:
        chat_template_kwargs["enable_thinking"] = config.enable_thinking
    if config.reasoning_preserve is not None:
        # テンプレートにより参照する変数名が異なる（llama.cpp標準は
        # preserve_reasoning、Qwen/Gemma系は preserve_thinking）ため両方送る。
        chat_template_kwargs["preserve_reasoning"] = config.reasoning_preserve
        chat_template_kwargs["preserve_thinking"] = config.reasoning_preserve
    if chat_template_kwargs:
        extra_body["chat_template_kwargs"] = chat_template_kwargs

    if config.reasoning_effort is not None and config.reasoning_effort != "default":
        extra_body["reasoning_effort"] = config.reasoning_effort

    extra_body.update(budget_params(provider, config.reasoning_budget))

    return extra_body


def budget_params(provider: str, budget: int | None) -> dict[str, Any]:
    """思考予算を provider ごとのパラメータ名へ変換する。

    build_extra_body() と thinking_control.apply_level()（ステップごとの
    予算の差し替え）の両方から使う。

    Args:
        provider: 送信先の LLMEndpoint.provider。
        budget: 思考予算のトークン数。-1 は無制限。None なら送らない。

    Returns:
        llama_cpp / openai_compatible なら {"reasoning_budget_tokens": budget}、
        vllm なら {"thinking_token_budget": budget}（-1 は送らない）。
        送らない場合は空の dict。
    """
    if budget is None:
        return {}
    if provider != "vllm":
        return {"reasoning_budget_tokens": budget}
    if budget >= 0:
        return {"thinking_token_budget": budget}
    return {}
