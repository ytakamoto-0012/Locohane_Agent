"""スレッド切り替え後も応答できるようにする ask 系ツール共通の中継ヘルパー。"""

from __future__ import annotations

import asyncio

from ..ask_relay import clear_pending_ask
from ..ask_relay import dispatch_to_live_viewers
from ..ask_relay import register_pending_ask


async def _ask_with_cross_session_relay(thread_id: str, factory, timeout: int):
    """Ask*Message(...).send() を、スレッド切り替え（フルページリロード）後に
    戻ってきた別セッションからでも回答できるようにして呼び出す。

    左サイドバーでの会話切り替え（frontend/src/components/Sidebar.tsx の
    goToThread）はフルページリロードのため、生成中スレッドから離れて
    ask系ツール（approve_plan/ask_user_question/ask_user_choice）の応答待ちの
    まま戻ってくると、Chainlit的には元セッションとは別物の新セッションになる
    （sessionIdState が毎回新規発行されるため）。素の
    cl.AskActionMessage/AskUserMessage/AskElementMessage.send() だけだと、
    その回答は誰にも届かなくなりボタン・入力フォームも二度と表示されない
    （2026-08-21 ユーザー報告。当初 approve_plan のみで発覚）。

    引き継ぎ先は2種類あり、どちらも同じ pending_asks[thread_id]["future"] を
    解決するだけなので下の asyncio.wait(FIRST_COMPLETED) 側は区別しない:
    (1) 後から戻ってくるセッション — app.py の on_chat_resume が検知して
        今まさに繋がっているセッション側で出し直す（_relay_pending_ask 参照）。
    (2) 既に接続済みのまま留まっているセッション — 下の
        dispatch_to_live_viewers 呼び出しが、登録した直後に該当スレッドを
        見ている全セッションへ即座に出し直す（(1)だけだと「離脱→復帰後、
        画面を見たまま待っている間に別のask系ツールが呼ばれた」場合に
        一切表示されない。2026-08-22 ユーザー実機テストで発覚）。
    元セッション（このコルーチンが実行されているセッション）側の factory()
    自体はブラウザがまだそこに残っていればそのまま機能するので、これらを
    asyncio.wait(FIRST_COMPLETED) で競わせる。ただし元セッション側が
    「本物の回答」で終わった場合のみ即採用し、「無応答タイムアウト
    （None）」で終わった場合は relay_future 側にもう一度 timeout 秒だけ
    チャンスを与える。元セッション側は切断済み（=絶対に誰も応答できない）
    ソケット宛のことが多く、そのtimeoutは「ユーザーが実際に無視した」を
    意味しないため（離脱→戻るまで時間がかかると、死んだソケット側の
    timeoutだけが先に発火し、後から戻ってきたセッションでの正しい回答が
    握りつぶされる不具合があった。2026-08-22 実機テストで発覚。詳細は
    下記実装のコメント参照）。

    Args:
        thread_id: 応答待ちを登録するスレッドID。
        factory: 引数無しの async callable。呼ぶたびに同じ内容の
            Ask*Message(...).send() を新規に実行して結果を返す（呼び出し元の
            セッション文脈で毎回新規に構築されるよう、cl.CustomElement 等を
            使う場合は factory の中で生成すること。中継時は別セッション文脈
            から呼ばれるため、事前に1個だけ作ったオブジェクトを使い回すと
            正しく動作しない）。
        timeout: 応答待ちのタイムアウト秒数（登録のみに使い、実際の
            タイムアウト処理自体は factory が呼ぶ Ask*Message 自身に任せる）。

    登録・解除の状態は app.py 側ではなく src/ask_relay.py（app.py/このモジュール
    どちらにも属さない中立モジュール）に置いてある。以前は app.py 側の状態を
    `from app import ...` で遅延importして参照していたが、chainlit run は
    app.pyを "app" という名前では sys.modules に登録しないため、その import が
    app.py全体を未初期化の別モジュールとして再実行し、稼働中の
    @cl.on_message 等のハンドラを壊してしまう不具合があった
    （src/ask_relay.py のdocstring参照）。
    """
    relay_future = register_pending_ask(thread_id, factory, timeout)
    local_ask_task = asyncio.create_task(factory())
    # on_chat_resume経由の引き継ぎは「後から戻ってくるセッション」にしか
    # 発火しないため、「既に接続済みのまま留まっているセッション」向けに
    # 今すぐ中継する（src/ask_relay.py の dispatch_to_live_viewers docstring
    # 参照。2026-08-22 ユーザー実機テストで発覚: 離脱→復帰後に画面を見たまま
    # 待っている間にask系ツールが呼ばれても入力ボックスが一切出なかった）。
    dispatch_to_live_viewers(thread_id)
    try:
        done, _pending = await asyncio.wait({local_ask_task, relay_future}, return_when=asyncio.FIRST_COMPLETED)
        if relay_future in done:
            if not local_ask_task.done():
                local_ask_task.cancel()
                try:
                    await local_ask_task
                except BaseException:  # noqa: BLE001 - 取消済みタスクの後片付けのみ、例外は握りつぶしてよい
                    pass
            return relay_future.result()
        local_result = local_ask_task.result()
        if local_result is not None:
            # local_ask_task が「本物の回答」で終わった（元セッションのブラウザが
            # まだ生きていてそちらで直接回答された）。relay_future はもう不要。
            relay_future.cancel()
            return local_result
        # local_ask_task はタイムアウト（None）で終わったが、relay_future は
        # まだ未解決。死んだソケット（切断済みの元セッション）宛の送信は
        # 相手が絶対に応答できないため、この timeout は「ユーザーが実際に
        # 無視した」ことを意味しない。離脱→復帰まで時間がかかった場合、
        # 死んだソケット側のtimeoutだけが先に発火して、後から戻ってきた
        # セッションでの正しい回答（relay_future）が握りつぶされてしまう
        # （2026-08-22 実機テストで発覚: switch away → ask発火まで長く待つ →
        # switch back → 質問フォームは正しく表示され回答したのに、LLMには
        # 「応答なし（タイムアウト）」が返っていた）。relay側にも同じ
        # timeout秒数だけ改めてチャンスを与える（resolve_pending_ask が
        # 呼ぶ Ask*Message 自体も同じ timeout を持つため、それ以上長く
        # 待っても新たな回答は来ない）。
        try:
            return await asyncio.wait_for(relay_future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
    finally:
        clear_pending_ask(thread_id, relay_future)
