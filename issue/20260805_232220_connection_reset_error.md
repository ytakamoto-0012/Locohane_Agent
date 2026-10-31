# ConnectionResetError: 既存接続がリモートホストに強制的に切断されました

- **区分**: 問題点
- **検知日時**: 2026-08-05 23:22
- **対象ログファイル**: data/logs/app_20260805_23_3.log

## 経緯

LLMサーバー（llama.cpp）との通信中にConnection errorが発生し、チェックポイント
とグラフの両方が再構築された。その後、`asyncio` コールバックで
`ConnectionResetError`（WinError 10054）が検知された。

## ログ引用

```
2026-08-05 23:22:08,432 WARNING app.py: LLMサーバーとの通信エラーを検知しました: Connection error. [name='Task-67' id=2582029040272 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0]
2026-08-05 23:22:08,435 WARNING app.py: チェックポインタを再構築しました [name='Task-67' id=2582029040272 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0]
2026-08-05 23:22:08,764 WARNING app.py: エラーのためグラフを再構築しました: Connection error. [name='Task-67' id=2582029040272 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0]
2026-08-05 23:22:14,649 WARNING app.py: LLMサーバーとの通信エラーを検知しました: Connection error. [name='Task-126' id=2582028920144 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0]
2026-08-05 23:22:14,654 WARNING app.py: チェックポインタを再構築しました [name='Task-126' id=2582028920144 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0]
2026-08-05 23:22:14,964 WARNING app.py: エラーのためグラフを再構築しました: Connection error. [name='Task-126' id=2582028920144 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0]
2026-08-05 23:22:20,581 ERROR asyncio: Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)
...
ConnectionResetError: [WinError 10054] 既存の接続はリモート ホストに強制的に切断されました。
```

## エラー原文

```
ConnectionResetError: [WinError 10054] 既存の接続はリモート ホストに強制的に切断されました。
```

## 推定原因

llama-server（llama.cpp）側が接続を強制切断したものと推測される。
考えられる原因:

- llama-server のスロットが満杯で接続を拒否
- llama-server 自体が再起動またはクラッシュ
- ネットワーク（ローカルループバック）の一時的な障害
- llama-server の `--threads` / `--parallel` 設定と同時リクエスト数の不整合

`[WinError 10054]` は TCP RSTパケットの受信を意味し、サーバー側からの
強制切断である。

## 追記（2026-08-05 23:22）

初回検知。同一タスク（Task-67, Task-126）で連続してConnection errorが
発生している。

## 原因分析（2026-08-06 調査）

### タイムラインの再構成

```
23:22:08  Task-67: Connection error → チェックポイント再構築 → グラフ再構築
23:22:14  Task-126: Connection error → チェックポイント再構築 → グラフ再構築
23:22:20  asyncio callback: ConnectionResetError [WinError 10054]
```

### 「死んだセクション」の正体

Task-126 の再構築後、`aclose_active_llm_clients(thread_id)` で旧接続が
強制クローズされました（app.py:1932）。その後 `_rebuild_graph()` で新グラフが
構築されましたが、**新リクエストを送信する前に asyncio の低レベルコールバック
（`_ProactorBasePipeTransport._call_connection_lost`）が旧接続の後始末を
行おうとし、ソケットが既に RST 状態だったため `ConnectionResetError` が
発火しました。**

### 接続が復旧しなかった理由

1. **llama-server 側がスロットを解放しなかった**: `request_timeout_seconds = 300`
   （5分）の間、llama-server 側で生成が続き、クライアント側はストリーミング
   チャンクを待ち続けていました。5分後にサーバー側が接続を強制切断（TCP RST）
   しました。

2. **`aclose()` の後始末が不完全だった可能性**: `aclose_active_llm_clients()`
   （llm.py:187-197行目）では、`ConnectionResetError` は `except Exception` で
   捕捉されログのみ（`logger.debug`）で、**例外の伝播はしません**。これ自体は
   意図的な設計ですが、内部ソケットが RST 状態の場合、`aclose()` が完全に
   完了したかどうかの保証がありません。

3. **再構築後の新リクエストが失敗した**: `aclose` → `rebuild_graph` の処理は
   完了していますが、**その後の新しいリクエスト送信時点で、llama-server 側が
   まだ応答準備できていなかった** か、**再構築されたグラフの httpx.AsyncClient
   生成時点で何らかの状態が残っていた** 可能性があります。

### 根本原因

```
llama-server スロット満杯 → 5分後サーバー側がTCP RSTで強制切断 →
クライアント側がacloseで後始末 → 旧ソケットがRST状態 →
asyncio callbackでConnectionResetError発火 → 新接続も失敗
```

`[llm].max_concurrent_requests = 2` かつ `[llm].max_tokens = 32000` の環境で、
llama-server の `--parallel` 設定と同時リクエスト数のバランスが崩れていた
可能性があります。

## 対策案

1. **`aclose()` 後のソケット状態確認**: `aclose()` 完了後、httpx.AsyncClientの
   内部状態が健全かどうかを確認するチェックを追加する。

2. **再接続タイムアウトの導入**: 再構築後の新リクエスト送信前に、llama-server
   への単純HTTPコネクトテスト（`/models` エンドポイント等）を入れ、サーバー
   側が応答準備できていることを確認する。

3. **`max_concurrent_requests` の見直し**: 現在 `2` だが、llama-server の
   `--parallel` 設定と一致しているか確認する。一致していない場合、過剰な
   同時リクエストがスロット枯渇を誘発する。

4. **`request_timeout_seconds` の短縮**: 5分は長すぎる。スロットが満杯の場合、
   早期にタイムアウトさせてユーザーにフィードバックする方が効率的。

## ユーザー回答

## ユーザー回答
