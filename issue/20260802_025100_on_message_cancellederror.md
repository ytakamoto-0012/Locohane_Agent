# on_message で CancelledError が検知

- **区分**: 問題点
- **検知日時**: 2026-08-02 02:51:00
- **対象ログファイル**: data/logs/app_20260802_02_2.log

## 経緯

`app.py` の `on_message` で `CancelledError` が検知された。
直前に `httpcore` 側の `receive_response_body.failed` で
`CancelledError()` が発生しており、LLMサーバーからのストリーミング応答が
キャンセルされた状態。

タスクID `Task-183` (id=1416360801360) で発生。
`cancelling=1` となっており、何らかの理由でタスクがキャンセルされ、
その後のストリーム処理で `CancelledError` が発生した。

## ログ引用

```
2026-08-02 02:47:41,802 DEBUG httpcore.http11: receive_response_body.failed exception=CancelledError()
2026-08-02 02:47:41,818 WARNING app.py: on_message: CancelledErrorを検知 [name='Task-183' id=1416360801360 cancelling=1 cancelled=False must_cancel=False elapsed_ms=0, cause='None', context='None']
```

## 推定原因

未検証。`cause='None'` となっており、なぜキャンセルされたかの直接原因は
ログからは読み取れない。ユーザーによるチャット終了、タイムアウト、
または内部のキャンセルロジックによるものなどが考えられる。

## 追記（YYYY-MM-DD HH:MM）

（同一原因の問題が再検知されるたびに、ここに追記を積み重ねていく）

## ユーザー回答

ここにはユーザーの回答が記述される
