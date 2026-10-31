# リトライ後の初回チャンク受信が126秒遅延（llama-serverスロット詰まりの疑い）

- **区分**: 問題点
- **検知日時**: 2026-08-02 00:58:12
- **対象ログファイル**: data/logs/app_20260802_00_1.log

## 経緯

`Task-111`が00:54:13に初回リクエストを開始し、その後何らかの理由で
00:56:06に「リトライ2回目開始」となった。このリトライ後、最初の
チャンク（thinkingまたは本文）を受信するまでに126秒を要し、
`app.py`側の遅延検知（閾値10秒）がWARNINGとして
「llama-server スロット詰まりの疑い」を出力した。ログ上、HTTPレスポンス
ヘッダー自体は00:58:10に200 OKで返っており、ボディの最初のチャンクが
届くまでの`receive_response_body`区間で待たされている
（`response_closed`が00:58:15に`GeneratorExit`で終了しており、この
リクエストも最終的には打ち切られたとみられる）。

## ログ引用

```
2026-08-02 00:54:13,074 DEBUG app.py: on_message: 初回リクエスト開始 [name='Task-111' id=1279064425040 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0]
2026-08-02 00:56:06,741 WARNING app.py: on_message: リトライ2回目開始 [name='Task-111' id=1279064425040 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-02 00:58:10,015 DEBUG httpcore.http11: receive_response_headers.complete return_value=(b'HTTP/1.1', 200, b'OK', ...)
2026-08-02 00:58:12,936 WARNING app.py: リトライ後の初回チャンク受信まで126秒（異常遅延） [name='Task-111' id=1279064425040 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
2026-08-02 00:58:15,091 DEBUG httpcore.http11: receive_response_body.failed exception=GeneratorExit()
```

## 推定原因

未検証。`app.py`のログメッセージ自体が「llama-server スロット詰まりの
疑い」と推測しており、`llama-server`の`--parallel`スロット数を超えて
複数リクエストが同時に飛んだ場合や、長いコンテキストのプロンプト
プリフィルに時間がかかっている可能性が考えられる（`config.ini`の
`[llm].stream_chunk_timeout_seconds`コメント参照）。前段の
リトライ2回目自体がなぜ発生したか（何が1回目を失敗させたか）は
このログ範囲からは特定できていない。

## 追記（2026-08-02 09:32）

リトライ後の初回チャンク受信まで24秒の遅延が再発。前回の126秒より大幅に改善したが、閾値（10秒）を超えている。

```
2026-08-02 09:31:51,168 WARNING app.py: on_message: リトライ2回目開始 [name='Task-1892' id=1773157096208 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-02 09:32:14,788 WARNING app.py: リトライ後の初回チャンク受信まで24秒（異常遅延） [name='Task-1892' id=1773157096208 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
```

## 追記（2026-08-02 10:38）

リトライ後の初回チャンク受信まで575秒（約9分35秒）という極端な遅延が
再発。前回の24秒、126秒を大幅に超える異常値。`app.py` 側が
「llama-server スロット詰まりの疑い」としてWARNINGを出力した。

```
2026-08-02 10:29:09,023 WARNING app.py: on_message: リトライ2回目開始 [name='Task-65' id=2511301068112 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-02 10:38:44,092 WARNING app.py: リトライ後の初回チャンク受信まで575秒（異常遅延） [name='Task-65' id=2511301068112 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
```

## 追記（2026-08-02 12:27）

リトライ後の初回チャンク受信まで22秒、16秒の遅延が2回連続で発生。
画像ファイルの一括処理（306件）中にllama-serverのスロットが
圧迫された可能性。

```
2026-08-02 12:27:05,879 WARNING app.py: on_message: リトライ2回目開始 [name='Task-93' id=2933086050384 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-02 12:27:27,397 WARNING app.py: リトライ後の初回チャンク受信まで22秒（異常遅延） [name='Task-93' id=2933086050384 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
2026-08-02 12:29:18,749 WARNING app.py: on_message: リトライ3回目開始 [name='Task-93' id=2933086050384 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-02 12:29:34,836 WARNING app.py: リトライ後の初回チャンク受信まで16秒（異常遅延） [name='Task-93' id=2933086050384 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
```

## 追記（2026-08-23 12:58）

`ThinkingLoopDetected`によるリトライ直後、初回チャンク受信まで62秒
（異常遅延）。VBAマクロ修正タスクの継続中、複数エージェントが短時間に
連続して実行されていたタイミングと重なる。

```
2026-08-23 12:57:50,022 WARNING app.py: on_message: リトライ2回目開始 [name='Task-65828' id=1853693016848 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-23 12:58:51,886 WARNING app.py: リトライ後の初回チャンク受信まで62秒（異常遅延） [name='Task-65828' id=1853693016848 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
```

## ユーザー回答

2026-08-02 修正実施済み。`ThinkingLoopDetected` 発生時は常に
`aclose_active_llm_clients()` で旧接続を強制クローズし、
`_rebuild_graph()` でグラフ再構築するフローに統一した。
動作テスト中。

## 追記（2026-08-05 23:36）

栄養情報追加バッチ処理中にスロット詰まりが再発。前回の575秒に次ぐ
極端な遅延。

```
2026-08-05 23:36:16,548 WARNING app.py: on_message: リトライ2回目開始 [name='Task-101' id=1987090799120 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-05 23:36:20,278 WARNING app.py: on_message: リトライ3回目開始 [name='Task-101' id=1987090799120 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-05 23:36:32,251 WARNING app.py: リトライ後の初回チャンク受信まで12秒（異常遅延） [name='Task-101' id=1987090799120 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
```

## 追記（2026-08-05 23:42）

栄養情報追加バッチ処理中のループ検知リトライでもスロット詰まりが発生。
11秒、130秒の遅延。

```
2026-08-05 23:42:18,398 WARNING app.py: on_message: リトライ2回目開始 [name='Task-5855' id=1987091661840 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-05 23:42:29,389 WARNING app.py: リトライ後の初回チャンク受信まで11秒（異常遅延） [name='Task-5855' id=1987091661840 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
```

```
2026-08-05 23:43:47,222 WARNING app.py: on_message: リトライ2回目開始 [name='Task-5855' id=1987091661840 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-05 23:45:57,279 WARNING app.py: リトライ後の初回チャンク受信まで130秒（異常遅延） [name='Task-5855' id=1987091661840 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
```

130秒は過去最悪の575秒には及ばないものの、依然として極めて長い遅延。
大量ファイル処理タスク中に頻発する傾向がある。

## 追記（2026-08-09 00:50）

00:25:00以降の `app_20260809_002425.log` でもスロット詰まりが再発。
00:27:14 にリトライ後の初回チャンク受信まで101秒の遅延、
00:44:11 には366秒（6分6秒）という過去最悪クラスの遅延を記録した
（対象ログ: `data/logs/app_20260809_002425.log`）。

```
2026-08-09 00:27:14,971 WARNING app.py: リトライ後の初回チャンク受信まで101秒（異常遅延） [name='Task-118' id=2453094929424 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
2026-08-09 00:44:11,101 WARNING app.py: リトライ後の初回チャンク受信まで366秒（異常遅延） [name='Task-118' id=2453094929424 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
```

366秒は過去2番目の遅延。同一タスク `Task-118` が ThinkingLoopDetected
のリトライを繰り返す間に llama-server のスロットが長時間占有され、
次のリクエストの応答が極めて遅くなった。

## 追記（2026-08-23 00:34）

excel-vbaマクロブック作成タスクの再起動直後、`Task-239`でLLM応答の
ループ検知（CSV読み込み〜Excel作成の手順を長々と自己解説していた）から
グラフ再構築・リトライ2回目に入り、その後初回チャンク受信まで33秒の
遅延（過去最短クラスではあるが依然閾値10秒超）を記録した。

```
2026-08-23 00:33:38,098 WARNING src.llm: LLM応答のループを検知したため生成を打ち切ります（直近テキスト: '0544_01.csv）の内容を読み取り、全20件の明細データを取得\n2. openpyxlでExcelファイル（収支計算表.xlsx）を作...'）
2026-08-23 00:33:38,513 WARNING app.py: on_message: リトライ2回目開始 [name='Task-239' id=2055320019344 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-23 00:34:11,776 WARNING app.py: リトライ後の初回チャンク受信まで33秒（異常遅延） [name='Task-239' id=2055320019344 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
```

引き続きThinkingLoopDetectedのリトライ発生後に遅延する既存パターンと一致。

## 追記（2026-08-23 10:37）

excel-vbaマクロブック作成タスク（再々開後）で再発。リトライ2回目開始
から初回チャンク受信まで120秒の遅延（過去最短クラスではあるが依然
閾値10秒を大幅超過）。

```
2026-08-23 10:35:08,144 WARNING app.py: on_message: リトライ2回目開始 [name='Task-151' id=2223834966992 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-23 10:37:08,324 WARNING app.py: リトライ後の初回チャンク受信まで120秒（異常遅延） [name='Task-151' id=2223834966992 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0] — llama-server スロット詰まりの疑い
```

引き続き既知パターン（リトライ発生後の遅延）と一致し、新規の原因は
確認できていない。
