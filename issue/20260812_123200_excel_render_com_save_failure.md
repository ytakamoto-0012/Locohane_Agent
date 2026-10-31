# excel-renderがExcel COM経由の保存エラーで失敗（約3分後の再試行で成功）

- **区分**: 問題点
- **検知日時**: 2026-08-12 12:18:52
- **対象ログファイル**: data/logs/app_20260812_113919.log

## 経緯

`annual_schedule.xlsx`の見た目確認のため、workerサブエージェントが`excel-render`スキルの`render_excel.py`を実行したところ、Excel COM自動化層で「ドキュメントを保存できませんでした」というエラーが発生し終了コード1で失敗した。

この直後は失敗のまま先に進み、他の作業（`read_skill`での再確認、`run_script`直接呼び出しの禁止ガード等）を挟んだのち、約3分後（12:22:00）に同じ`render_excel.py`を`--max-pages 5`で再試行したところ**終了コード0で成功**している。

## ログ引用

```
2026-08-12 12:18:52,007 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-render', 'script_filename': 'render_excel.py', 'script_args': ['E:\\user_b\\テスト\\annual_schedule.xlsx', '--start-page', '1', '--max-pages', '1']} -> [終了コード] 1
2026-08-12 12:18:52,007 DEBUG src.subagent: subagent tool=run_script args={...} -> "[終了コード] 1\n[標準エラー]\nExcelでのレンダリングに失敗しました: (-2147352567, '例外が発生しました。', (0, 'Microsoft Excel', 'ドキュメントを保存できませんでした。ドキュメントが開いているか、保存時にエラーが発生した可能性があります。', 'xlmain11.chm', 0, -2146827284), None)"

2026-08-12 12:22:00,143 INFO src.subagent: subagent tool=run_script args={'skill_name': 'excel-render', 'script_filename': 'render_excel.py', 'script_args': ['E:\\user_b\\テスト\\annual_schedule.xlsx', '--start-page', '1', '--max-pages', '5']} -> [終了コード] 0
```

## 推定原因

未検証。エラーメッセージ「ドキュメントが開いているか、保存時にエラーが発生した可能性があります」から、`render_excel.py`が内部でExcel COM経由にファイルを開いて画像化のために一時保存する処理を行っており、その時点で同じファイルが別プロセス（直前に成功していた`excel-edit`の`edit_excel.py`実行や、別のCOMインスタンスの後始末漏れ等）によってロックされていた可能性がある。約3分の間隔を空けた再試行で成功していることから、一過性のファイルロック競合とみられるが、`skills/excel-render/scripts/render_excel.py`のCOM解放処理（`wb.Close()`/`app.Quit()`のタイミングや例外時のクリーンアップ有無）は未確認。

## 追記（YYYY-MM-DD HH:MM）

（同一原因の問題が再検知されるたびに、ここに追記を積み重ねていく）

## ユーザー回答

ここにはユーザーの回答が記述される
