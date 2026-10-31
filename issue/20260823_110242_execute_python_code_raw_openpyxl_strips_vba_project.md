# execute_python_codeで生のopenpyxl.load_workbook()を使いタブ色・グラフを調整し、既存.xlsmのVBAプロジェクトを無言で破壊

- **区分**: バグ → 対応済み
- **検知日時**: 2026-08-23 10:59:26〜11:03:23

- **対象ログファイル**: data/logs/app_20260823_104959.log

## 経緯

excel-vbaマクロブック作成タスク（2回目の再起動）で、`edit_vba.py`により
`収支計算表.xlsm`へVBAモジュール`CSVImporter`を追加した直後、workerが
シートタブの色・アクティブシート・グラフの位置/サイズ/タイトルを最終調整
するため、`excel-edit`スキルのopsではなく`execute_python_code`で
**生の`openpyxl.load_workbook(path)`を`keep_vba=True`を付けずに直接呼び、
`wb.save(path)`で上書き保存**した（10:59:26〜11:00:06、4回試行し
最終的に成功、途中`chart_collection`/`chart_objects`等の存在しない属性名を
試すtrial-and-errorもあった）。

保存自体は成功（終了コード0）したが、`keep_vba=True`無しでの
`load_workbook`→`save`はVBAプロジェクトを保持しない。この時点でファイルは
無言で壊れた状態になった。

その後`excel-render`スキルの`render_excel.py`でグラフを画像確認しようと
したところ、Excel COMが`収支計算表.xlsm`を開けず
「ファイル形式またはファイル拡張子が正しくありません」で失敗（11:02:42）。
最終的に`verifier`サブエージェントが`has_vba: false`
（`CSVImporter`モジュール・3プロシージャすべて未存在）と報告し、
`edit_vba.py`で追加したVBAマクロが完全に失われていたことが判明した
（11:03:23）。

なお同じ検証で、simple Chinese文字混入issue
（[20260823_105754](20260823_105754_execute_python_code_cp932_unicodeencodeerror_and_simplified_chinese_leak.md)）
の「实际額」もそのまま最終ファイルに残っていることが確認された。

## ログ引用

```
2026-08-23 10:59:26,296 WARNING src.subagent: subagent tool=execute_python_code args={'code': 'import openpyxl\n...\nwb = openpyxl.load_workbook(r"E:\\user_b\\vba-test\\収支計算表.xlsm")\n\n# Set tab colors...\nwb.save(r"E:\\user_b\\vba-test\\収支計算表.xlsm")\n...'} -> [終了コード] 1
2026-08-23 11:00:06,546 WARNING src.subagent: subagent tool=execute_python_code args={'code': 'import openpyxl\n\nwb = openpyxl.load_workbook(r"E:\\user_b\\vba-test\\収支計算表.xlsm")\n...\nwb.save(r"E:\\user_b\\vba-test\\収支計算表.xlsm")\nprint("Final adjustments completed successfully")\n'} -> [終了コード] 0
2026-08-23 11:02:42,921 DEBUG src.subagent: subagent tool=run_script args={'skill_name': 'excel-render', 'script_filename': 'render_excel.py', 'script_args': ['E:\\user_b\\vba-test\\収支計算表.xlsm']} -> '[終了コード] 1\n[標準エラー]\nExcelでのレンダリングに失敗しました: ...Excel でファイル \'収支計算表.xlsm\' を開くことができません。ファイル形式またはファイル拡張子が正しくありません。...'
2026-08-23 11:03:23,868 DEBUG src.tools: tool_result: name=dispatch_agent content='# 検証結果報告\n...\n## 5. VBAマクロ: **不合格**\n- 標準モジュール`CSVImporter`: **未存在**（`has_vba: false`, `modules_count: 0`）\n...'
```

## エラー原文

```
Excel でファイル '収支計算表.xlsm' を開くことができません。ファイル形式またはファイル拡張子が正しくありません。ファイルが破損しておらず、ファイル拡張子とファイル形式が一致していることを確認してください。
```

## 推定原因（コード確認済み）

`skills/excel-edit/scripts/edit_excel.py`（95行目）は
`openpyxl.load_workbook(str(path), keep_vba=(ext == ".xlsm"))`と、
拡張子が`.xlsm`なら常に`keep_vba=True`で読み込んでいる。しかしworkerは
シートタブの色（`set_tab_color`相当）・アクティブシート指定
（`set_active_sheet`相当）・グラフのタイトル/位置調整（`update_chart`で
既に対応可能）という操作のうち、**タブ色・アクティブシートに対応する
opがexcel-editに存在しなかった**ため、`execute_python_code`で生の
openpyxlを直接使う判断をした。その際`keep_vba=True`を付け忘れ、既存の
VBAプロジェクトを保持しないまま上書き保存してしまった。

グラフの位置/サイズ/タイトル変更自体は既に`update_chart`opでカバー
されていたが、workerはこれに気づかず（`chart_collection`/
`chart_objects`という存在しない属性名を試すtrial-and-errorも発生して
おり、openpyxlのグラフAPIへの理解も不確かだった）、生openpyxlで
代用しようとした。

## 対応（実装済み・2026-08-23）

1. `skills/excel-edit/scripts/_ops.py`に`set_tab_color`
   （`sheet`,`color`）・`set_active_sheet`（`sheet`）の2opを新設した。
   これによりタブ色・アクティブシート変更のためだけに生openpyxlへ
   迂回する必要が無くなった（グラフ調整は既存の`update_chart`で対応可能）。
2. `skills/excel-edit/SKILL.md`のopsテーブルに2op追加。加えて「呼び出しと
   前提条件」節に、既存`.xlsm`へ`execute_python_code`で生の
   `openpyxl.load_workbook()`を直接呼ばないこと（`keep_vba=True`忘れで
   VBAプロジェクトが無言で消失する）・該当opが本当に無ければユーザーへ
   相談することを明記した。

テスト: `tests/test_excel_edit_ops.py`に
`test_set_tab_color_sets_sheet_properties_tab_color`・
`test_set_active_sheet_updates_workbook_active_index`を追加。
`pytest tests/` 422件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
