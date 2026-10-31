# LLMがrun_scriptの引数値にXML風のツール呼び出しトークンを混入させ、argparseエラーで3件失敗（自己修復）

- **区分**: 問題点
- **検知日時**: 2026-08-23 11:01:25

- **対象ログファイル**: data/logs/app_20260823_104959.log

## 経緯

excel-vbaマクロブック作成タスクで、workerが3シート分の内容を並列確認
しようと`run_script(read_excel.py)`を3件同時に呼んだところ、いずれも
`--limit`の値が`'](</parameter> 100</arg_value> <arg_value>['`という
XML風の断片文字列になっており、argparseの`invalid int value`で3件とも
終了コード2で失敗した。

6秒後、workerは自ら「JSON構文のエラー」と認識し、`--limit`を正しく
`'100'`に修正して再送、成功した。実害は無い（3件とも即座に成功する
別呼び出しへ置き換わった）。

## ログ引用

```
2026-08-23 11:01:25,186 DEBUG src.llm: LLM応答: content='シート構成確認完了。3シート存在。次に各シートの詳細を確認する。\n\n' reasoning_content='...' tool_calls=[{'name': 'run_script', 'args': {'skill_name': 'excel-read', 'script_filename': 'read_excel.py', 'script_args': ['E:\\user_b\\vba-test\\収支計算表.xlsm', '--sheet', '取引明細表', '--limit', '](</parameter> 100</arg_value> <arg_value>[']}, ...}, ...]
2026-08-23 11:01:25,494 DEBUG src.subagent: subagent tool=run_script args={...} -> "[終了コード] 2\n[標準エラー]\nusage: read_excel.py [-h] [--sheet SHEET] [--offset OFFSET] [--limit LIMIT]\n                     [--data-only] [--query-json QUERY_JSON]\n                     file_path\nread_excel.py: error: argument --limit: invalid int value: '](</parameter> 100</arg_value> <arg_value>['"
2026-08-23 11:01:31,389 DEBUG src.llm: LLM応答: content='' reasoning_content='JSON構文のエラー。script_args配列のJSONを正しく記述し直します。\n' tool_calls=[{'name': 'run_script', 'args': {..., 'script_args': [..., '--limit', '100']}, ...}, ...]
```

## エラー原文

```
read_excel.py: error: argument --limit: invalid int value: '](</parameter> 100</arg_value> <arg_value>['
```

## 推定原因

未検証（Locohane側のツール呼び出しパース処理を疑ったが、`src.llm`の
DEBUG行の時点で既にこの壊れた文字列が`tool_calls`の`args`値として
記録されており、Locohane側の後処理で混入した形跡は無い＝**LLM自身が
生成したテキストの時点で既にこの断片が混入していた**とみられる）。
`</parameter>`・`<arg_value>`という文字列は、この会話で使われている
tool-calling方式（JSON形式のtool_calls）とは異なるXMLベースのツール
呼び出し記法（他のツール呼び出しフレームワークで使われる形式）を彷彿と
させ、モデルが学習時に触れた別形式のツール呼び出しテンプレートを
誤って生成テキストに混入させた可能性がある（未検証、llama-server側の
プロンプトテンプレート/グラマー制約の設定次第で発生頻度が変わる可能性）。

6秒で自己修復しており実害は無いが、これが引数の型検証（argparseの
`int`変換等）で弾かれない文脈（例: 自由文字列を受け取る引数、または
`execute_python_code`のcode引数）で発生した場合、壊れた断片がそのまま
実行・保存されてしまうリスクがある。

## ユーザー回答

ここにはユーザーの回答が記述される
