"""ask系ツール（approve_plan/AskUserQuestion/ask_user_choice）の応答待ちを、
スレッド切り替え後の別セッションへ引き継ぐための状態。app.py（Chainlit
エントリスクリプト）と src/tools.py の双方から参照されるため、どちらにも
属さないこの中立モジュールに置く。

背景（2026-08-21 発覚、元は src/plan_relay.py として approve_plan 専用に実装）:
以前はこの状態を app.py 側に置き、src/tools.py から `from app import ...` で
遅延importしていた。しかし `chainlit run app.py` は
`importlib.util.spec_from_file_location(target, target)`（target=app.pyのフルパス）
でapp.pyを読み込み、`sys.modules[target]` に登録する（config.py の load_module 参照）。
つまり実行中のapp.pyは `sys.modules["app"]` という名前では登録されていない。
そのため `from app import ...` を実行すると sys.modules に "app" が見つからず、
（load_module がapp.pyのディレクトリを sys.path へ追加済みのため）sys.path 経由で
app.py がまっさらな別モジュールとして最初から再実行されてしまう。
この再実行は `@cl.on_message`/`@cl.on_chat_start`/`@cl.on_chat_resume` を含む
トップレベルコードを丸ごとやり直し、これらのデコレータはプロセス全体で1つの
シングルトンである `chainlit.config.config.code.on_message` 等を書き換えるため、
稼働中の本物のハンドラが「未初期化の別モジュールのハンドラ」に上書きされて
アプリ全体が壊れる（approve_plan呼び出し直後に接続がおかしくなる、として観測された）。
このモジュールを介することで app.py→src.tools への逆import自体を無くす。

2026-08-22 追記1: approve_plan だけが `AskActionMessage` 専用の引き継ぎを持ち、
`ask_user_question`（AskUserMessage/AskElementMessage）と `ask_user_choice`
（AskActionMessage/AskElementMessage/AskUserMessage）には同じ引き継ぎが無く、
離脱→復帰で質問フォーム・選択肢ボタンが二度と表示されない不具合があった。
Ask*Messageの型ごとにコンストラクタ引数の形が違う（action_specsのような
tuple列でシリアライズすると型が増えるたびに専用の再構築ロジックが要る）ため、
状態としては「呼べば同じAsk*Message().send()をもう一度実行する、引数無しの
async factory」を丸ごと保持する形に一般化した。これによりAsk*Messageの型に
依存しない共通の引き継ぎ機構になっている。

2026-08-22 追記2（ユーザー実機テストで発覚）: 上記の引き継ぎは
`@cl.on_chat_resume`（＝新しいセッションが接続してきた瞬間）からしか
発火しない。そのため「離脱→復帰済みで、既にそのスレッドを見たまま
留まっているセッション」に対して、その後に新しいask系ツールが呼ばれても
一切表示されない（on_chat_resumeが再度発火する契機が無いため）。
dispatch_to_live_viewers はこのギャップを埋める。ask系ツールが応答待ちを
登録した直後に同期的に呼び、その時点で同じスレッドを見ている全セッションへ
即座に同じAsk*Messageを出し直す。on_chat_resume経由の引き継ぎ（後から
戻ってくるセッション向け）とは別の入口だが、どちらも同じ
pending_asks[thread_id]["future"] を解決するだけなので、
`_ask_with_cross_session_relay`（src/tools.py）側の
`asyncio.wait({local_ask_task, relay_future}, FIRST_COMPLETED)` は変更不要。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import chainlit as cl

# thread_id -> 応答待ち（approve_plan/ask_user_question/ask_user_choice の
# Ask*Message）の引き継ぎ用状態。
# 値: {"factory": Callable[[], Awaitable], "timeout": int, "future": asyncio.Future}
# factory は「呼べば同じ内容のAsk*Message(...).send()をもう一度実行する」
# 引数無しの非同期callable（呼び出し元が実行中のセッション文脈で呼ぶ想定のため、
# 中で使う cl.CustomElement 等は factory の中で毎回新規生成すること）。
pending_asks: dict[str, dict[str, Any]] = {}


def register_pending_ask(thread_id: str, factory: Callable[[], Awaitable[Any]], timeout: int) -> asyncio.Future:
    """ask系ツールがAsk*Message送信前に呼ぶ。他セッションから引き継げるよう
    再実行用のfactoryを登録し、引き継ぎ側が回答を書き込むための Future を返す。
    """
    fut = asyncio.get_event_loop().create_future()
    pending_asks[thread_id] = {"factory": factory, "timeout": timeout, "future": fut}
    return fut


def clear_pending_ask(thread_id: str, fut: asyncio.Future) -> None:
    """呼び出し元ツールの finally節から呼ぶ。同じ thread_id へ後続の
    ask系呼び出しが既に新しい応答待ちを登録している場合は誤って消さないよう、
    渡された Future が現在の登録と一致する場合のみ消す。
    """
    entry = pending_asks.get(thread_id)
    if entry is not None and entry["future"] is fut:
        pending_asks.pop(thread_id, None)


async def resolve_pending_ask(thread_id: str) -> None:
    """指定スレッドに未解決の応答待ちがあれば、今の実行コンテキスト
    （cl.context、＝現在このコルーチンに束縛されているセッション）で
    同じAsk*Messageを出し直し、回答を pending_asks の Future 経由で書き込む。

    呼び出し元は、引き継ぎ先セッションの文脈で実行されるようにしてから
    呼ぶこと。on_chat_resume から呼ぶ場合は、Chainlit自体が
    ハンドラ実行前に cl.context を新セッションへ束縛済みなのでそのままでよい。
    既に接続済みの別セッションへ中継する場合は、事前に
    chainlit.context.init_ws_context(target) で束縛してから呼ぶ
    （dispatch_to_live_viewers 参照）。
    """
    entry = pending_asks.get(thread_id)
    if entry is None or entry["future"].done():
        return
    try:
        res = await entry["factory"]()
    except Exception:  # noqa: BLE001 - 引き継ぎ表示自体の失敗で元セッション側の待機を巻き込まない
        logging.getLogger(__name__).warning("Failed to show the handover of a pending question [thread_id=%s]", thread_id, exc_info=True)
        return
    if not entry["future"].done():
        entry["future"].set_result(res)


def dispatch_to_live_viewers(thread_id: str) -> None:
    """ask系ツールが register_pending_ask 直後に呼ぶ。今まさに同じスレッドを
    見ている（離脱→復帰済みで、まだそこに留まっている）全セッションへ、
    直ちに同じAsk*Messageを出し直す。

    on_chat_resume経由の引き継ぎ（_relay_pending_ask/resolve_pending_ask の
    on_chat_resume呼び出し側）は「新しいセッションが接続してきた瞬間」にしか
    発火しないため、「既に接続済みのまま留まっているセッションに対して、
    その後にask系ツールが呼ばれた」場合はカバーできない
    （2026-08-22 ユーザー実機テストで発覚: 離脱→復帰後、画面を見たまま
    待っている間にAskUserQuestionが呼ばれても入力ボックスが一切出なかった）。

    各対象セッションごとに新しい asyncio.Task として fire-and-forget で
    起動する。asyncio.create_task は生成時点の contextvars を丸ごと
    コピーするため、タスクの中で chainlit.context.init_ws_context により
    そのタスク自身のコンテキストだけを束縛でき、他のタスク・呼び出し元の
    cl.context には影響しない。

    呼び出し元（cl.context が無いテスト環境等）で cl.context.session への
    アクセス自体が失敗しても、この関数の失敗で本来のask系ツール呼び出しを
    巻き込まないよう、ここは丸ごとベストエフォートにする
    （src/thread_store.py の _current_owner と同じ方針）。
    """
    try:
        from chainlit.context import init_ws_context
        from chainlit.session import ws_sessions_sid

        current = cl.context.session
        others = [
            other for other in list(ws_sessions_sid.values()) if other is not current and other.thread_id == thread_id
        ]
    except Exception:  # noqa: BLE001 - cl.context が無い環境（テスト等）では何もしないだけでよい
        return

    async def _relay_as(target) -> None:
        init_ws_context(target)
        await resolve_pending_ask(thread_id)

    for other in others:
        asyncio.create_task(_relay_as(other))
