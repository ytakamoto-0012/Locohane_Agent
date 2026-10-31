# サブエージェント実行中、停止ボタン・思考ループガードが実際にはLLM生成を止めない

- **区分**: バグ（修正済み）
- **検知日時**: 2026-08-26 22:00頃
- **対象ログファイル**: data/logs/app_20260826_220253.log

## 経緯

ユーザーから2件のバグ報告を受けた。

1. dispatch_agent（サブエージェント）実行中に停止ボタンを押すと、UIバッジは
   「停止」状態になるが、LLMの生成とバックグラウンド進捗表示が止まらない。
2. dispatch_agent実行中に思考ループガード（ThinkingLoopDetected）が発火すると、
   接続は切れるがLLMの生成は止まらない（停止ボタンとは無関係に発生）。

いずれも実装調査の結果、原因が異なる2箇所のバグであることが判明した。

## 原因1: 停止ボタンが dispatch_agent の job.runner_task に届かない

`dispatch_agent`ツール（src/tools/dispatch_agent.py）は`job.runner_task`を
`asyncio.shield()`で保護した上で完了を待つ。これは安全上限
（[subagent].background_inline_wait_max_seconds）超過時にジョブを裏側で
生かし続けるための意図的な設計だが、停止ボタン（`@cl.on_stop`）・
cross-session停止（`_stop_thread_generating`）は`session.current_task.cancel()`
相当（メイングラフのタスクのみ）と`aclose_active_llm_clients()`（自セッションの
LLM接続の強制クローズ）しか行わず、shieldされた`job.runner_task`自体は
一切キャンセルされない。

さらに`run_subagent`内の`_invoke_with_timeout_retry`は接続エラー
（LLM_CONNECTION_ERRORS）を「一時的な障害」として検知し、モデルを
再構築して自動リトライする設計のため、`aclose_active_llm_clients()`による
強制切断すら「リトライすべき接続エラー」として吸収され、ジョブが
完走してしまう。

### ログ実証（app_20260826_220253.log）

```
22:04:47,595 dispatch_agent: LLM呼び出しがタイムアウトしたため再試行します(1/3回目)
22:04:47,928 on_message: CancelledErrorを検知（停止ボタン押下によるメイングラフの中断）
22:04:48,251 on_stop: セッションのLLMクライアントを強制クローズし、グラフを再構築しました
...（この後もsubagent iter=1〜11が進行）...
22:06:34,909 dispatch_agent 正常終了: 11回で完了
```

停止ボタン押下（22:04:47台）から約107秒後、ジョブは何事もなかったかのように
正常完了している。

### 修正

`src/tools/_dispatch_agent_job.py`に`cancel_dispatch_agent_jobs_for_thread(thread_id)`
を追加。対象thread_idの実行中ジョブ全件に対し、`stop_dispatch_agent_job`ツールと
同じ`job.status="killed"` + `job.runner_task.cancel()`を適用する。
`app.py`の`on_stop()`・`_stop_thread_generating()`の両方から、
`aclose_active_llm_clients()`と併せて呼ぶよう変更した。

## 原因2: ThinkingLoopDetected発生時、旧クライアントの接続が強制クローズされない

`src/context_compaction.py`の`maybe_compact`（要約専用LLM呼び出し）は、
ThinkingLoopDetected発生時に`aclose_model_client(current_model)`で
そのモデル専用のhttpx.AsyncClientを無条件で強制クローズしてから
リトライ・打ち切りを行う（過去のユーザー報告への対応として実装済み）。

しかし`src/subagent.py`の`_invoke_with_loop_retry`（dispatch_agentの
メインReActループが使う、同種のリトライロジック）には同じ処理が
欠けていた。`agen.aclose()`（ChatLlamaCpp._astream_guarded内、5秒
タイムアウト）が失敗・タイムアウトした場合、`build_model()`で新しい
クライアントを生成して差し替えるだけで、壊れた可能性のある旧
クライアント（＝llama-server側の旧ストリーム）は単に参照を手放す
だけだった。参照を手放しても実際のTCP接続・llama-server側の生成が
即座に終わる保証はなく、接続が生きたまま生成が続き得る。

同じ関数内、リトライ予算を使い切って`raise`する分岐（`if attempt >=
max_retries: raise`）が閉じ処理より前にあったため、最後の1回は
そもそも閉じ処理自体が呼ばれない経路でもあった。

### 修正

`src/subagent.py`の`_invoke_with_loop_retry`に、`aclose_model_client`の
importを追加し、`except ThinkingLoopDetected`ブロックの先頭
（`if attempt >= max_retries: raise`より前）で`await
aclose_model_client(current_model)`を呼ぶよう変更した。
`context_compaction.py`と同一パターン。`aclose_active_llm_clients()`
（セッション内全クライアント一括クローズ）ではなく`aclose_model_client()`
（このモデルインスタンス1つだけ）を使うのは、dispatch_agentの並列
サブエージェント実行機能により、同一セッション内で並行実行中の別
サブエージェント・メイングラフのクライアントまで巻き添えで閉じて
しまう実害（2026-07-21に実際発生・確認済み）を避けるため。

## 検証

- 既存テスト（tests/test_subagent_timeout_retry.py 等、dispatch_agent/
  subagent/context_compaction関連）+ 全テストスイート475件、修正後も
  全件成功を確認。
- 原因1はログで再現条件・修正効果を確認済み（上記ログ抜粋）。
- 原因2はログ実証は取れていない（現存ログに該当イベントの記録なし、
  ユーザー申告ベース）。ただし同型のバグが`context_compaction.py`側で
  過去に実際発生・修正されている前例があり、`subagent.py`側にのみ
  同じ修正が漏れていたことをコードレベルで確認済み。

## ユーザー回答

ここにはユーザーの回答が記述される
