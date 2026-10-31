# 孤立tool_callによるセッション連続失敗 → グローバルセマフォ競合でプロセスハング

- **区分**: 問題点
- **検知日時**: 2026-08-04 23:49:28
- **対象ログファイル**: data/logs/app_20260804_23.log

## 経緯

2026-08-04 23:44〜23:49、`data/logs/app_20260804_23.log` で2つの同時セッションが揃って復帰不能になった。原因連鎖は以下の通り:

1. `[graph] max_parallel=1`（config.ini）により全セッション共有の `_TOOL_CALL_SEMAPHORE`（[src/tools.py:139](src/tools.py#L139)）が1本しかなく、一方のセッションの `dispatch_agent`（画像OCR大量処理）が長時間そのスロットを占有。
2. もう一方のセッションの `update_task_progress` ツール呼び出し（tool_call id=`CtpwAjOKXuhObroiGoaA2LZrgbYMEcsc`）がスロット待ちのまま、対応する `ToolMessage` が一度も書き込まれずにターンが終了扱いとなり、チェックポイントに「孤立tool_call」（`AIMessage.tool_calls`はあるが対応する `ToolMessage` が無い状態）がコミットされた。
3. 次回ターン開始時に必ず
   ```
   ValueError: Found AIMessages with tool_calls that do not have a corresponding ToolMessage
   ```
   が発生。`app.py` の例外ハンドラは `_rebuild_checkpointer()` / `_rebuild_graph()` を呼ぶが、**これらは接続・グラフオブジェクトの再生成のみ**で、SQLiteに永続化済みの壊れた状態そのものは一切修復しない。そのため同じスレッドへの再送信は毎回同一箇所で即座に同じ `ValueError` を再現し続け、**自己修復しない無限ループ**になっていた。
4. `_checkpointer` はプロセス全体で1個の共有 `AsyncSqliteSaver`（[app.py:135](app.py#L135)）のため、壊れたセッションが自動リトライのたびにこの共有DB接続を閉じて再構築し、無関係な2つ目のセッション（`Task-156`）の進行中の aiosqlite 操作を巻き込んで `_CheckpointerConnectionClosed: no active connection` を誘発。プロセス全体がハングして両セッションとも復帰不可能になった。

この問題は既存の `issue.md` の **ISSUE-003**（`approve_plan` が WebSocket切断で同種の孤立tool_callを起こす件）と同一パターンが、別のトリガー経路（グローバルセマフォ待ち中の中断）で `update_task_progress` にも起きたもの。

## ログ引用

```
# セッションA: update_task_progress の tool_call が孤立し、ValueError 無限ループ
2026-08-04 23:44:44,xxx Task-605 ... ValueError: Found AIMessages with tool_calls that do not have a corresponding ToolMessage
2026-08-04 23:44:xx,xxx Task-36489 ... ValueError: Found AIMessages with tool_calls that do not have a corresponding ToolMessage
2026-08-04 23:44:xx,xxx Task-38388 ... ValueError: Found AIMessages with tool_calls that do not have a corresponding ToolMessage
# 同一 tool_call id (CtpwAjOKXuhObroiGoaA2LZrgbYMEcsc) で3回連続再現

# セッションB: 共有checkpointerの破壊により誘発エラー
2026-08-04 23:49:28,669 ... _CheckpointerConnectionClosed: no active connection
```

## エラー原文

```
ValueError: Found AIMessages with tool_calls that do not have a corresponding ToolMessage

_CheckpointerConnectionClosed: no active connection
```

## 推定原因

1. **孤立tool_callの発生**: `[graph] max_parallel=1` により全セッションが1本の `_TOOL_CALL_SEMAPHORE` を共有。`dispatch_agent`（画像OCR）が長時間スロットを占有している間、他のセッションのツール呼び出し（`update_task_progress`）がスロット待ちとなる。この待ち中に何らかの理由（タイムアウト、キャンセル、接続切れ等）でターンが中断され、`ToolMessage` が書き込まれないままチェックポイントにコミットされた。

2. **自己修復不能**: `_rebuild_graph()` / `_rebuild_checkpointer()` はグラフ・接続オブジェクトの再構築のみを行い、SQLiteに永続化済みの壊れたチェックポイント内容は変更しない。そのため次回ターン開始時に同じ `ValueError` が即座に再現し、修復不能の無限ループに陥った。

3. **プロセス全体ハング**: 壊れたセッションが自動リトライのたびに共有 `_checkpointer`（`AsyncSqliteSaver`）の接続を閉じて再構築するため、無関係な他セッションの aiosqlite 操作を巻き込んで `_CheckpointerConnectionClosed` を発生させた。ログの途絶え（23:49:28,669）の直後、プロセス全体がハングしたと推測される。

## 実装対応

- `_repair_orphaned_tool_calls()` 関数を [app.py](app.py) に追加。チェックポイント末尾に孤立tool_callがあれば、プレースホルダの `ToolMessage` を補完コミットして修復する。
- ThinkingLoopDetected リトライ経路（[app.py:1827](app.py#L1827)）と turn_broken_exc 経路（[app.py:1863](app.py#L1863)）の両方から `_repair_orphaned_tool_calls()` を呼ぶように変更。
- `_run_context_compaction()`（[app.py:1206](app.py#L1206)）に孤立tool_call残りの圧縮抑制ガードを追加。
- ISSUE-003 の修正案1（`_rebuild_graph()` に整合性修復処理を追加）の実際の適用例となった。

## 未解明点

- セマフォ待ち中に `update_task_progress` の tool_call が具体的にどのような経路で中断・孤立したか（タイムアウトか、キャンセルか、他の何らかの要因か）。`app_20260804_23.log` の該当時間帯のログが `_CheckpointerConnectionClosed` 直前で途絶えており、中断の直接原因を特定できなかった。
- 両セッションが同時に発生した原因（タイミングの偶然か、何かのトリガーで同時発生したか）。

## 追記

## ユーザー回答
