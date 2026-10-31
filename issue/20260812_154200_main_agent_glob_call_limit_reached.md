# メインエージェントがGlobを複数回直接呼び出しし呼び出し上限に到達

- **区分**: 改善点
- **検知日時**: 2026-08-12 16:00:00
- **対象ログファイル**: data/logs/app_20260812_154052.log

## 経緯

メインエージェントが年間行事予定表の作成タスクを処理中、作業ディレクトリ構造の確認のためにGlobツールを1回呼び出した後、さらに4回のGlobを同一ターンで並列呼び出ししようとした。しかし、`[main_agent_tool_guard]` の設定によりGlobの呼び出し上限が1回に制限されていたため、初回呼び出し以降の4回がすべてエラーで拒否された。

エージェントはGlobによる直接調査を諦め、dispatch_agent（exploreエージェント）へ委譲して調査を続行した。

## ログ引用

```
2026-08-12 15:42:04,340 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: explore, analyze-docss, verifier, worker）へ委譲してください。'
2026-08-12 15:42:04,340 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: explore, analyze-docss, verifier, worker）へ委譲してください。'
2026-08-12 15:42:04,340 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: explore, analyze-docss, verifier, worker）へ委譲してください。'
2026-08-12 15:42:04,340 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: explore, analyze-docss, verifier, worker）へ委譲してください。'
```

## 推定原因

`[main_agent_tool_guard]` セクションの `entries` で `["Glob", 1]` が設定されており、メインエージェントが1ターン内でGlobを呼び出せる回数が1回に制限されている。これは小型ローカルモデルが進捗の無いまま同じ呼び出しを繰り返し行う事例への対策だが、今回のように並列呼び出しが必要なケースでは制限が厳しすぎる可能性がある。

エージェントは制限に達した後、適切にdispatch_agentへ委譲しているので、バグではなく「並列呼び出しが必要なケースの扱い」が改善点として残る。

## 追記（2026-08-12 18:30）

同一事象が別セッションで再発。18:25のセッションでも、メインエージェントがGlobを自分で実行しようとして呼び出し上限に達し、その後dispatch_agent(explore)へ委譲した。

```
2026-08-12 18:25:27,368 WARNING src.tools: tool_result: name=Glob content='エラー: 検索起点ディレクトリが見つかりません: E:\\user_b\\テスト（読み書き可能）'
2026-08-12 18:25:32,117 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: explore, analyze-docss, verifier, worker）へ委譲してください。'
```

1つ目の「検索起点ディレクトリが見つかりません」は、LLMが`テスト（読み書き可能）`という存在しないパスを推測したケース。2つ目が今回のGlob呼び出し上限到達。

いずれもエージェントは制限後に適切にexploreエージェントへ委譲して処理を続行している。

## 追記（2026-08-23 17:59）

対象ログファイル: data/logs/app_20260823_175334.log

```
2026-08-23 17:56:14,003 WARNING src.tools: tool_result: name=Glob content='エラー: 検索起点ディレクトリが見つかりません: E:\\user_b\\テスト（読み書き可能）\\Datas'
2026-08-23 17:56:16,187 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: analyze-docs, explore, planner, verifier, worker）へ委譲してください。'
```

再発。同じ「存在しないパス推測→呼び出し上限到達→explore委譲」の流れ。
「テスト（読み書き可能）」パス推測側の詳しい原因調査結果は
[glob_search_directory_not_found.md](20260813_163000_glob_search_directory_not_found.md)
に記載（括弧のエスケープ問題ではなく、ユーザーの注釈文言をLLMがパスの
一部として解釈したことが原因と判明）。

## 追記（2026-08-23 20:01）

対象ログファイル: data/logs/app_20260823_195217.log（新スレッド）

```
2026-08-23 19:59:11,488 WARNING src.tools: tool_result: name=Glob content='エラー: Glob はメインエージェントとして既に呼び出し上限（1回）に達しています。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: analyze-docs, explore, planner, verifier, worker）へ委譲してください。'
```

再発。ガードは意図通り機能しており、実害なし。
