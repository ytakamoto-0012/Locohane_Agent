# workerサブエージェントが「計画未承認」ブロックで行き詰まり、無関係なcreate_memoryをテスト値で2回誤呼び出し

- **区分**: 問題点 → 対応済み
- **検知日時**: 2026-08-23 12:17:38
- **対象ログファイル**: data/logs/app_20260823_104959.log

## 経緯

メインエージェントが、既に完了済みのVBAマクロ修正計画（全項目`[x]`）に加えて
未承認の追加修正（通貨形式・グラフ再生成）をworkerサブエージェントへ直接
`dispatch_agent`した。workerが`run_script(edit_excel.py)`を実行しようとして
「計画が未承認のため実行できません」でブロックされた。workerは
`create_plan`/`approve_plan`を自分のtoolsに持たない（サブエージェントには
無い）ため計画承認できないが、それに気づく前に`create_memory`を
`content`/`description`空・`name`="test"という無関係かつ無効な引数で
2回連続（12:17:38, 12:17:54）誤って呼び出し、2回とも同一エラーで失敗した。
その後の内部推論（約30秒）でようやく「サブエージェントは計画承認できない」
「委譲元に差し戻すべき」と自己修正し、9イテレーションかけて
「0件処理・計画未承認のため失敗」という結果をメインエージェントへ正しく
報告した。実害（データ破損等）は無いが、無駄な試行錯誤とトークン消費が
発生した。

## ログ引用

```
2026-08-23 12:17:36,001 WARNING src.subagent: subagent tool=run_script args={...edit_excel.py...} -> エラー: 計画が未承認のため実行できません（skill=excel-edit, script=edit_excel.py）。create_plan で計画を作成し、approve_plan でユーザーの承認を得てから実行してください。
2026-08-23 12:17:38,875 WARNING src.subagent: subagent tool=create_memory args={'content': '', 'description': '', 'name': 'test', 'memory_type': 'reference'} -> エラー: description が空です
2026-08-23 12:17:54,119 WARNING src.subagent: subagent tool=create_memory args={'content': '', 'description': '', 'name': 'test', 'memory_type': 'reference'} -> エラー: description が空です
2026-08-23 12:18:15,383 DEBUG src.llm: LLM応答: content='最終回答：\n\n1. 処理対象として受け取った件数: 3件...\n2. 実際に書き出したファイルの件数: 0件\n3. 失敗した対象と理由:\n   - 通貨形式の修正（set_number_format）: 計画未承認のため`run_script`がブロックされた\n...'
```

## 推定原因

`src/tools.py`の`run_script`/`execute_python_code`/`execute_python_code_background`
が返す「計画が未承認のため実行できません」エラーメッセージは、
`create_plan`/`approve_plan`の呼び出しを一律に促す文面だったが、これらの
ツールはメインエージェントにしか無くサブエージェント（worker等）には
無い。workerはこのメッセージに従おうとして行き詰まり、無関係な
`create_memory`をテスト値で誤って試すという回り道をした後、ようやく
自力で「サブエージェントには計画承認手段が無い」と気づいて委譲元へ
差し戻した。

## 対応（実装済み・2026-08-23）

`src/tools.py`の3箇所（`run_script`/`execute_python_code`/
`execute_python_code_background`の計画未承認エラーメッセージ）に、
「自分のtoolsにcreate_plan/approve_planが無い（サブエージェントである）
場合は、それ以上試行せずこのエラーをそのまま最終回答として委譲元へ
報告してください」という一文を追加した。低パラメータモデル向けに、
次に取るべき具体的な行動（試行を止めて報告する）を直接指示する形にした。

既存テストにこのメッセージ文言への依存は無く、`pytest tests/`435件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
