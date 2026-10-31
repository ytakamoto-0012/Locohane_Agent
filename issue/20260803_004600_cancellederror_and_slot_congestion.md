# CancelledError と llama-server スロット詰まりの疑い

- **区分**: 問題点
- **検知日時**: 2026-08-03 00:36:00
- **対象ログファイル**: data/logs/app_20260803_00_1.log, app_20260803_00_2.log

## 経緯

画像レシピ抽出バッチタスク中に、複数のタスクが `CancelledError` で
中止された。また、リトライ後の初回チャンク受信までに17秒〜61秒かかって
おり、「llama-server スロット詰まりの疑い」が警告されている。

特に:
- 00:36:05 に `CancelledError` 検知（Task-226564, Task-216526）
- 00:36:05 に孤立した tool_calls(1件) にプレースホルダの ToolMessage
  を補完してチェックポイントを修復
- 00:38:51 にリトライ後の初回チャンク受信まで17秒（異常遅延）
- 00:29:56 にリトライ後の初回チャンク受信まで61秒（異常遅延）

## ログ引用

```
2026-08-03 00:29:56,819 WARNING app.py: リトライ後の初回チャンク受信まで61秒（異常遅延） [name='Task-95' id=1790221320656 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
2026-08-03 00:36:05,107 WARNING src.subagent: subagent: asyncio.CancelledError を検知 [name='Task-226564' id=1790349137040 cancelling=2 cancelled=False must_cancel=False elapsed_ms=0, cause='None']
2026-08-03 00:36:05,107 WARNING app.py: on_message: CancelledErrorを検知 [name='Task-216526' id=1790364860368 cancelling=1 cancelled=False must_cancel=False elapsed_ms=0, cause='None', context='None']
2026-08-03 00:36:05,115 WARNING app.py: on_message: CancelledErrorを検知し、孤立したtool_calls(1件)にプレースホルダのToolMessageを補完してチェックポイントを修復しました
2026-08-03 00:38:51,197 WARNING app.py: リトライ後の初回チャンク受信まで17秒（異常遅延） [name='Task-227872' id=1790346249808 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
2026-08-03 00:38:56,017 WARNING app.py: on_message: CancelledErrorを検知 [name='Task-227872' id=1790346249808 cancelling=1 cancelled=False must_cancel=False elapsed_ms=0, cause='None', context='None']
2026-08-03 00:46:15,925 WARNING app.py: on_message: CancelledErrorを検知 [name='Task-262298' id=1790339348816 cancelling=1 cancelled=False must_cancel=False elapsed_ms=0, cause='None', context='None']
```

## 推定原因

1. **llama-server スロットの枯渇**: 61秒、17秒という初回チャンクまでの
   遅延は、llama-server が同時に処理できるリクエスト数（--parallel）を
   超えるリクエストが送られ、スロットが空くまで待機している可能性が高い。
2. **ThinkingLoopDetected 後のリトライ集中**: ループ検知後にリトライする
   際、旧クライアントが壊れたまま次のリクエストを送信し、応答ヘッダー
   待ちでハングする現象が確認されている。
3. **CancelledError の連鎖**: 1つのタスクが中止されると、関連する
   サブエージェントや tool_calls も連鎖的に中止される。

## 追記（2026-08-03 02:02）

同一夜間のセッションで、最終的に画像レシピ抽出バッチがステップ11
（画像111〜120件目）でThinkingLoopGuard発火により未完遂のまま停止した
（詳細は`issue.md`の**ISSUE-004**参照）。

併せて、コンソールに以下のRuntimeErrorが確認された:
```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```
`langchain_openai`の`_astream_with_chunk_timeout`（`_client_utils.py:650`）が
`asyncio.wait_for()`でストリーム取得を別タスク化しており、httpx/httpcore/anyioの
CancelScope制約（開いたタスクでしか閉じられない）に抵触している。
`config.ini`に記載済みの`stream_chunk_timeout_seconds=0`実験がまだ本番反映
されていないため、これが未実施であることも今回の一因。詳細・修正案は
`issue.md`の**ISSUE-004**に集約。

## ユーザー回答

ここにはユーザーの回答が記述される
