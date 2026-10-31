# dispatch_agent_background 失敗 → check_dispatch_agent_job エラー

- **区分**: バグ
- **検知日時**: 2026-08-08 02:25:00
- **対象ログファイル**: data/logs/app_20260808_020533.log

## 経緯

レシピ画像変換バッチタスク（images→md）において、メインエージェントが dispatch_agent_background を使って worker エージェントに大規模画像処理を委譲しようとしたが、サブエージェントの実行自体が失敗した。その後 check_dispatch_agent_job を呼んだ際にも「エラー: サブエージェントの実行に失敗しました: 」という空のエラーメッセージが返された。

## ログ引用

```
2026-08-08 02:24:38,145 ERROR src.tools: dispatch_agent_background 失敗 (run_id=56e798f5921c4264880bddd5bc6f45a2)
2026-08-08 02:24:38,177 WARNING app.py: on_message: リトライ4回目開始 [name='Task-98' id=1959051393680 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0, cancel_scope_breakage_last_60s=0]
2026-08-08 02:25:30,094 WARNING src.tools: tool_result: name=check_dispatch_agent_job content='エラー: サブエージェントの実行に失敗しました: '
```

## エラー原文

```
ERROR src.tools: dispatch_agent_background 失敗 (run_id=56e798f5921c4264880bddd5bc6f45a2)
```

## 推定原因

1. **前段の ThinkingLoopDetected 影響**: この失敗の約15分前に複数の ThinkingLoopDetected が発生しており、会話状態が壊れていた可能性。
2. **llama-serverスロット詰まり**: リトライ後のチャンク受信に15秒〜27秒要しており、リソース不足でサブエージェントの初期化が失敗した可能性。
3. **エラーメッセージが空**: `check_dispatch_agent_job` の content が `'エラー: サブエージェントの実行に失敗しました: '` と末尾が空であり、具体的な原因が伝わっていない。dispatch_agent_background 側の例外情報が失われている疑い。

## 追記（2026-08-08 02:25）

初回検知。dispatch_agent_background の失敗時に run_id は出力されるが、check_dispatch_agent_job 側で具体的なエラー原因が取得できていない。

## ユーザー回答
