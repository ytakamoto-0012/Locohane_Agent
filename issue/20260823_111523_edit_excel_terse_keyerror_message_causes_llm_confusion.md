# ops適用失敗時、KeyErrorの素のメッセージ（'cell'のみ等）だけではLLMが原因を特定できず迷走する

- **区分**: バグ → 修正済み
- **検知日時**: 2026-08-23 11:15:23〜11:16:47

- **対象ログファイル**: data/logs/app_20260823_104959.log

## 経緯

前サイクルのVBAプロジェクト消失（[issue/20260823_110242](20260823_110242_execute_python_code_raw_openpyxl_strips_vba_project.md)）
からの復旧作業中、workerが`set_cell`/`set_column_width`のopsに
`"row"/"col"`キー（`_ops.py`は`"cell"`/`"column"`のみ対応）を渡し続け、
`run_script(edit_excel.py)`が`ops[3]（op='set_cell'）の適用に失敗しました:
'cell'`という終了コード1を返した。

workerは`read_skill(excel-edit)`で正しいスキーマ（`cell`キー、"A1"形式）を
確認し、opsを正しく`"cell"`/`"column"`キーへ修正した（11:16:19）。ここまでは
正常な自己修復だったが、その直後の推論（`ループ検知チェック`のbuffer内容）で
「正しく`cell`キーを使っているのになぜ`'cell'`というエラーのままなのか」と
混乱し、約30秒間、無関係な原因（`rename_sheet`/`add_sheet`の失敗が誤帰属して
いる、シート名`Sheet1`が実は存在しない、等）を疑って迷走した末、修正済みの
opsを再送せず`read_excel.py`で現状確認するという遠回りの一手を選んだ
（11:16:47）。

## ログ引用

```
2026-08-23 11:15:23,642 DEBUG src.subagent: subagent tool=run_script args={...} -> "[終了コード] 1\n[標準エラー]\nops[3]（op='set_cell'）の適用に失敗しました: 'cell'\n..."
2026-08-23 11:16:19,654 DEBUG src.llm: LLM応答: content='`set_cell`のopスキーマを確認した。`sheet`と`cell`（"A1"形式）が必須。`row`/`col`ではない。修正して再実行する。\n\n' ...
2026-08-23 11:16:38,849 DEBUG src.llm: ループ検知チェック: ... 直近テキスト="...Let me reconsider whether the `cell` parameter is actually being recognized by the script at all. I'm wondering if the error is actually coming from a different op—maybe `rename_sheet` or `add_sheet` is failing with a 'cell' key error..."
2026-08-23 11:16:47,010 DEBUG src.llm: LLM応答: content='...`excel-edit`の`set_cell`は`cell`キー（"A1"形式）を使うが、`\'cell\'` KeyErrorが出ている。シート名の問題かもしれない。' tool_calls=[{'name': 'run_script', 'args': {'skill_name': 'excel-read', ...}}]
```

## 推定原因（コード確認済み）

`edit_excel.py`（106-108行目）・`edit_vba.py`（123-124行目）・
`edit_docx.py`（112-114行目）はいずれも
`except (KeyError, ValueError, TypeError) as e: f"ops[{idx}]（op=...）の適用に
失敗しました: {e}"`という共通実装で、`KeyError`の`str(e)`は欠落キー名の
repr（例`'cell'`）のみを返す。このため「`cell`という名前の何かが問題」と
しか伝わらず、「`cell`キー自体が渡されていない」のか「`cell`の値が不正」
なのかLLM側で区別できない。今回、正しく`cell`キーを使うよう修正した後も
同じ文言のエラーを想定してしまい、迷走の一因になった。

## 対応（実装済み・2026-08-23）

`edit_excel.py`/`edit_vba.py`/`edit_docx.py`の3ファイルで、`KeyError`のみ
`ValueError`/`TypeError`と分けて捕捉し、「ops[{idx}]（op=...）に必須キー{e}が
指定されていません」という明確なメッセージに変更した（`ValueError`/
`TypeError`は従来通り）。

テスト: `tests/test_office_edit_missing_op_key_error_message.py`を新規追加
（3件、excel-edit/excel-vba-edit/docx-edit各1件）。excel-edit・docx-edit は
スクリプトディレクトリ内に同名の`_ops.py`を持つため、同一pytestプロセス内
でのモジュールキャッシュ衝突を避けるヘルパー（`sys.modules`から明示的に
除去してから絶対パスでimportし直す）も併せて実装した。`pytest tests/`
427件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
