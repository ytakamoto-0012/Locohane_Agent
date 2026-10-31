# Glob: メインエージェントの呼び出し上限（1回）に到達

- **区分**: 問題点
- **検知日時**: 2026-08-13 16:30:00
- **対象ログファイル**: data/logs/app_20260812_203024.log, app_20260812_205426.log

## 経緯

メインエージェントが `Glob` ツールを1回以上呼び出そうとした際、呼び出し上限に達しているためエラーが発生。以降の調査・処理は `dispatch_agent` へ委譲するよう指示されている。

## ログ引用

```
2026-08-12 20:31:47,702 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの 調査・処理は dispatch_agent（agent_type: explore, analyze-docss, verifier, worker）へ委譲してください。'
2026-08-12 20:35:42,030 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの 調査・処理は dispatch_agent（agent_type: explore, analyze-docss, verifier, worker）へ委譲してください。'
2026-08-12 20:55:07,023 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの 調査・処理は dispatch_agent（agent_type: explore, analyze-docss, verifier, worker）へ委譲してください。'
```

## 推定原因

`[main_agent_tool_guard].entries` で `["Glob", 1]` が設定されており、メインエージェントが1ターン以内に1回のみ呼び出しが許可されている。LLMが1ターン内で複数回 `Glob` を呼び出そうとしたため、2回目以降がブロックされている。これは意図したガードの動作だが、LLMが1回で済ませられていないのが根本原因。

## 追記（2026-08-13 16:30）

- 初回検知

## 追記（2026-08-23 13:58）

対象ログファイル: data/logs/app_20260823_135730.log

```
2026-08-23 13:58:11,740 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: analyze-docs, explore, planner, verifier, worker）へ委譲してください。'
```

前回（8/13時点）はエージェント種別が `explore, analyze-docss, verifier, worker`（`analyze-docss` はtypo）だったが、今回は `analyze-docs, explore, planner, verifier, worker` に変化している（`analyze-docss`のtypo修正・`planner`追加）。再発は引き続き「LLMが1ターンで複数回Globを呼ぼうとする」という同一パターン。

## ユーザー回答
