# Chainlit起動時のasync_generator RuntimeError（GeneratorExit / cancel scope）

- **区分**: エラー（起動時）
- **検知日時**: 2026-08-02 10:24:15
- **発生フェーズ**: Chainlitサーバー起動直後

## 経緯

`chainlit run` によりアプリ起動時、サーバーURL表示直後に2つの例外が発生した。

1. `BaseChatOpenAI._astream` の async_generator が `GeneratorExit` を無視
2. `AsyncStream.__aexit__` 内で `RuntimeError: Attempted to exit cancel scope in a different task`

どちらも**サーバー起動直後**で、実際のユーザーメッセージ処理前の発生。
既存の issue `20260802_004307_thinking_loop_cancel_scope_runtimeerror.md` と同型だが、
トリガーが「LLM応答ループ検知後のリトライ」ではなく「起動時の初期化処理」。

## エラー原文

```
Exception ignored in: <async_generator object BaseChatOpenAI._astream at 0x00000248B520E5C0>
Traceback (most recent call last):
  File "C:\DT_Python\Python311\env_local_agent_system\Lib\site-packages\langchain_openai\chat_models\base.py", line 3513, in _astream
    yield chunk
RuntimeError: async generator ignored GeneratorExit

Exception ignored in: <coroutine object AsyncStream.__aexit__ at 0x00000248BCE52340>
Traceback (most recent call last):
  File "C:\DT_Python\Python311\env_local_agent_system\Lib\site-packages\openai\_streaming.py", line 231, in __aexit__
    await self.close()
  File "C:\DT_Python\Python311\env_local_agent_system\Lib\site-packages\openai\_streaming.py", line 239, in close
    await self.response.aclose()
  File "C:\DT_Python\Python311\env_local_agent_system\Lib\site-packages\httpx\_models.py", line 1076, in aclose
    await self.stream.aclose()
  File "C:\DT_Python\Python311\env_local_agent_system\Lib\site-packages\httpx\_client.py", line 182, in aclose
    await self._stream.aclose()
  File "C:\DT_Python\Python311\env_local_agent_system\Lib\site-packages\httpx\_transports\default.py", line 276, in aclose
    await self._httpcore_stream.aclose()
  File "C:\DT_Python\Python311\env_local_agent_system\Lib\site-packages\httpcore\_async\connection_pool.py", line 412, in aclose
    with AsyncShieldCancellation():
  File "C:\DT_Python\Python311\env_local_agent_system\Lib\site-packages\httpcore\_synchronization.py", line 226, in __exit__
    self._anyio_shield.__exit__(exc_type, exc_value, traceback)
  File "C:\DT_Python\Python311\env_local_agent_system\Lib\site-packages\anyio\_backends\_asyncio.py", line 464, in __exit__
    raise RuntimeError(
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

## 推定原因

### 1. `async generator ignored GeneratorExit`

`langchain_openai.chat_models.base.BaseChatOpenAI._astream` が async generator として実装されており、
ジェネレーターが破棄される際に送出される `GeneratorExit` を `try...finally` で捕捉せず、
そのまま無視しているために発生。

**影響**: 「Exception ignored in」は stderr へ出力されるだけで、Python 3.7+ では
**プロセス終了時に自動的に無視される**ため、実際の動作への影響は軽微と考えられる。

### 2. `Attempted to exit cancel scope in a different task`

`httpcore` の `AsyncShieldCancellation()` が、cancel scope を開いたタスクとは
**別のタスク**で close を実行しようとした際に発生。

**トリガーの候補**:
- Chainlit起動時の初期化処理内で、何らかの非同期処理がタスク分割された
- `openai` クライアントの `AsyncStream` が内部で `anyio` の cancel scope を使用
- 起動直後なので、接続プールの初期化やコネクションoliminationの過程で発生

**既存対策との関係**:
- `src/llm.py` の `ChatLlamaCpp._astream` には `asyncio.timeout(5.0)` で保護された
  `aclose()` 処理が実装されており、**同一task制約を破らないよう設計済み**
- ただし今回のエラーは `langchain_openai` および `openai` クライアント由来であり、
  `ChatLlamaCpp._astream` の外側で発生している可能性が高い

## 影響度評価

- **重大度**: **低**（起動時の一時的エラー、サーバーは正常起動している）
- **ユーザー影響**: なし（サーバー起動後、通常のチャット処理は正常に動作する）
- **ログの噪音**: あり（stderr に出力されるため、本番環境では鬱陶しい）

## 対策案

### 案1: 無視する（推奨）

Python 3.7+ では `GeneratorExit` を無視した async generator の例外は
自動的にクリーンアップされるため、**コード修正の必要はない**。
stderr への出力を抑制したい場合は、起動スクリプトで stderr をリダイレクト。

**メリット**: 修正コストゼロ、安全
**デメリット**: stderr へ出力され続ける

### 案2: ルートロガーで抑制する

`app.py` 起動時に `logging.captureWarnings()` や stderr filter で
「Exception ignored in」を抑制する。

```python
import sys
import logging

# 「Exception ignored in」を stderr から抑制
class SuppressIgnoredExceptionFilter(logging.Filter):
    def filter(self, record):
        return "Exception ignored in" not in record.getMessage()

logging.getLogger().addFilter(SuppressIgnoredExceptionFilter())
```

**メリット**: 明示的に制御可能
**デメリット**: 本来の原因解決ではない

### 案3: 依存パッケージの更新を確認する

`langchain-openai`, `openai`, `httpcore`, `anyio` の最新バージョンで
修正されている可能性を確認。

**メリット**: 根本解決
**デメリット**: バージョンアップによる他影響のリスク

## 関連issue

- [`20260802_004307_thinking_loop_cancel_scope_runtimeerror.md`](./20260802_004307_thinking_loop_cancel_scope_runtimeerror.md) - LLM応答ループ検知後のリトライで同型エラー
- [`20260802_005812_retry_first_chunk_delay_slot_congestion.md`](./20260802_005812_retry_first_chunk_delay_slot_congestion.md) - 初回チャンク受信遅延

## 追記（2026-08-02 10:25）

ユーザーが `chainlit run` で起動した直後の発生であり、**実際のチャット処理とは無関係**。
既存の `ChatLlamaCpp._astream` 対策（`asyncio.timeout()` による同一task保護）は
すでに実装済み。今回のエラーは依存パッケージ側の問題であり、
**実質的な影響は低く、当面は無視してよい**。

ただし、stderr への出力が噪音となる場合は、案2のfilter導入を検討。

## ユーザー回答

本件は無視します。
