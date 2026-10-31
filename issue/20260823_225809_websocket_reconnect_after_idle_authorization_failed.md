# 長時間アイドル後のWebSocket再接続でchainlitの認証に失敗する

- **区分**: 問題点
- **検知日時**: 2026-08-23 22:58:09
- **対象ログファイル**: data/logs/app_20260823_195217.log

## 経緯

魚図鑑PPTX作成タスクが20:36:54に完了・成功応答を返した後、20:50:54に
ユーザー側でWebSocketが切断（`ターン進行中=False`、正常なタブクローズ等と
推測）。その後 **約2時間7分の空白** を経て22:58:09に同じ`thread_id`で
再接続が試行された。

`app.py`側の`_connect_logged`（`src.tools`の`connect`イベントハンドラ）は
`sessionId`から既存セッションを発見し「WebSocket再接続」ログを正常に出力
したが、直後にchainlit本体側の認証処理で失敗している。

```
2026-08-23 22:58:09,642 INFO app.py: WebSocket再接続: sid=RiAuTzx4SuVG5hnfAAAR thread_id=9d5c3480-384b-4ff3-98e9-381b0f9de886 ターン進行中=False（進行中の再接続は、切断中に完了したStep更新がフロントに届かず「実行中」のまま固まる不具合の疑いあり）
2026-08-23 22:58:09,642 ERROR chainlit: Authorization for the session failed.
2026-08-23 22:58:09,645 ERROR engineio.server: 'Session is disconnected' 4BxtNnRQvf8KoT28AAAQ (further occurrences of this error will be logged with level INFO)
```

このログ以降、当該ログファイルへの追記が無く（監視時点でアプリの
アクティビティが停止）、再接続がその後成功したか、ユーザーがどのような
画面状態を見たかは本ログ範囲からは確認できていない。

## 推定原因

未検証。`app.py`の`_connect_logged`（[app.py:1202](../app.py#L1202)付近）が
参照する`sessionId`ベースの自前セッション追跡（`_WebsocketSession`）は
まだ当該セッションを保持していたため「再接続」と判定できたが、chainlit
本体側の認証層（`Authorization for the session failed`、chainlitライブラリ
内部のエラーで本リポジトリのコードではない）は約2時間のアイドル後に
セッションを無効と判断した可能性がある。両者のセッション有効期限・
GCタイミングが食い違っている（自前追跡は生きているのにchainlit側の
認証だけ先に失効する）ことが原因として考えられるが、chainlit側の
セッション有効期限設定や、そもそも2時間という間隔が閾値なのかは未確認。

`app.py`の`_connect_logged`のログメッセージ自体が「進行中の再接続は
Step更新が届かず実行中のまま固まる不具合の疑いあり」と既知のリスクを
警告しているが、今回は`ターン進行中=False`だったため、その不具合とは
別の（より基本的な）認証失敗が発生している。

## 追記（YYYY-MM-DD HH:MM）

## ユーザー回答

ここにはユーザーの回答が記述される
