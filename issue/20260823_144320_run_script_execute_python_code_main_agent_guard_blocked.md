# run_script/execute_python_codeのメインエージェント直接呼び出しがmain_agent_tool_guardで拒否される

- **区分**: 問題点
- **検知日時**: 2026-08-23 14:06:03
- **対象ログファイル**: data/logs/app_20260823_135730.log

## 経緯

メインエージェントが `run_script`・`execute_python_code` を自ら直接呼び出そうと
した際、`[main_agent_tool_guard]` の設定（`config.ini`の`entries`で両ツールとも
`max_calls=0`＝完全ブロック）によりエラーで拒否され、`dispatch_agent`への
委譲を促された。ガードは設計通り動作しており（[main_agent_tool_guard]は
ホワイトリスト方式で0以下＝完全ブロックが意図的仕様）、既存の
`Glob`（[issue/20260813_163000_glob_main_agent_limit_reached.md](20260813_163000_glob_main_agent_limit_reached.md)）
と同種のパターン。いずれもこの直後にメインエージェントが`dispatch_agent`へ
委譲し直し、処理は継続できている（実害は軽微）。

## ログ引用

```
2026-08-23 14:06:03,317 WARNING src.tools: tool_result: name=run_script content='エラー: run_script はメインエージェントとして呼び出しを禁止されています（max_calls=0）。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: analyze-docs, explore, planner, verifier, worker）へ委譲してください。'
2026-08-23 14:22:51,337 WARNING src.tools: tool_result: name=execute_python_code content='エラー: execute_python_code はメインエージェントとして呼び出しを禁止されています（max_calls=0）。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: analyze-docs, explore, planner, verifier, worker）へ委譲してください。'
2026-08-23 14:42:39,918 WARNING src.tools: tool_result: name=run_script content='エラー: run_script はメインエージェントとして呼び出しを禁止されています（max_calls=0）。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: analyze-docs, explore, planner, verifier, worker）へ委譲してください。'
```

## 推定原因

`[main_agent_tool_guard].entries`で`["run_script", 0]`・`["execute_python_code", 0]`
相当の設定がされており、意図した通りの拒否。メインエージェント自身が
「一時ファイルをコピーする」等の細かい後始末を自分でやろうとして毎回
このガードに引っかかっており、システムプロンプトまたは各SKILL.mdで
「後始末的な作業も含めdispatch_agentへ委譲する」ことをより明確に示せば
このガード発火自体（＝1往復分のトークン浪費）を減らせる可能性がある。

## 追記（2026-08-23 18:08）

対象ログファイル: data/logs/app_20260823_175334.log

```
2026-08-23 18:07:22,977 WARNING src.tools: tool_result: name=run_script content='エラー: run_script はメインエージェントとして呼び出しを禁止されています（max_calls=0）。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: analyze-docs, explore, planner, verifier, worker）へ委譲してください。'
```

再発。原因・対応は上記から変わらず。

## 追記（2026-08-23 20:34）

対象ログファイル: data/logs/app_20260823_195217.log

```
2026-08-23 20:32:58,753 WARNING src.tools: tool_result: name=run_script content='エラー: run_script はメインエージェントとして呼び出しを禁止されています（max_calls=0）。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: analyze-docs, explore, planner, verifier, worker）へ委譲してください。'
```

再発。原因・対応は上記から変わらず。

## ユーザー回答

ここにはユーザーの回答が記述される
