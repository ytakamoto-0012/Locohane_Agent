# excel-editスキルのop_set_rangeがMergedCell（結合セルの非アンカー側）へ書き込もうとしてAttributeErrorでクラッシュ

- **区分**: バグ
- **検知日時**: 2026-08-12 12:06:35, 12:07:59
- **対象ログファイル**: data/logs/app_20260812_113919.log

## 経緯

子供会の年間行事予定表（`E:\user_b\テスト\annual_schedule.xlsx`）作成タスクで、`worker`サブエージェントがExcel編集を2回試み、いずれも`run_script(edit_excel.py)`が終了コード1で失敗した。

1. **1回目（12:06:35）**: `--new --overwrite --ops-file E:\user_b\テスト\ops.json`で実行。`execute_python_code`がops.jsonを書き出したのは`execute_python_code`専用の一時ディレクトリ（`_tmp_7115f2d5-39b6-47b1-ac88-c91025ff249f`配下）であり、指定した`E:\user_b\テスト\ops.json`には存在しなかったため「opsファイルが見つかりません」で即座に失敗（Excelへの書き込みは一切発生せず）。
2. サブエージェントは自己診断で一時ディレクトリのops.jsonを`execute_python_code`で読み直し（12:06:51）、`E:\user_b\テスト\ops.json`へコピー（12:07:03）して原因を解消。
3. **2回目（12:07:59）**: 今度は`--ops-json`にインライン指定（`insert_rows`→`set_cell`(A1にタイトル)→`merge_cells`(A1:H1)→`set_range`(A2からヘッダー行)の4ops、`--new`/`--overwrite`は付けていない＝既存の`annual_schedule.xlsx`への追記編集）を実行したところ、`_ops.py`の`op_set_range`が`AttributeError: 'MergedCell' object attribute 'value' is read-only`で未捕捉のままクラッシュした。

`annual_schedule.xlsx`は同日昼の別セッション（[issue/20260812_115000_explore_docs_batch_oversize_hard_token_cutoff.md](20260812_115000_explore_docs_batch_oversize_hard_token_cutoff.md)の元セッション、`app_20260812_023059.log`）で既に作成済み・月ごとにA列を結合済みのファイルであり、今回はそれを`insert_rows`で1行挿入してからヘッダー行を書き込もうとしたもの。挿入により既存の結合範囲がシフトし、`set_range`の書き込み先セルが結合セルの非アンカー（左上以外）側になった結果、openpyxlが例外を送出したとみられる。

## ログ引用

```
2026-08-12 12:06:35,976 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-edit', 'script_filename': 'edit_excel.py', 'script_args': ['E:\\user_b\\テスト\\annual_schedule.xlsx', '--new', '--overwrite', '--ops-file', 'E:\\user_b\\テスト\\ops.json']} -> [終了コード] 1
2026-08-12 12:06:35,976 DEBUG src.subagent: subagent tool=run_script args={...} -> '[終了コード] 1\n[標準エラー]\nopsファイルが見つかりません: E:\\user_b\\テスト\\ops.json'

2026-08-12 12:07:59,903 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-edit', 'script_filename': 'edit_excel.py', 'script_args': ['E:\\user_b\\テスト\\annual_schedule.xlsx', '--ops-json', '[{"op": "insert_rows", "sheet": "月間予定表", "index": 1, "count": 1}, {"op": "set_cell", ...}, {"op": "merge_cells", "sheet": "月間予定表", "range": "A1:H1"}, {"op": "set_range", "sheet": "月間予定表", "start_cell": "A2", "rows": [["月","週","日付","行事名","種別","詳細","場所","備考"]], "header_style": {...}}]']} -> [終了コード] 1
2026-08-12 12:07:59,903 DEBUG src.subagent: subagent tool=run_script args={...} -> '[終了コード] 1\n[標準エラー]\nTraceback (most recent call last):\n  File "C:\\DT_Python\\Locohane\\skills\\excel-edit\\scripts\\edit_excel.py", line 131, in <module>\n    sys.exit(main())\n  File "C:\\DT_Python\\Locohane\\skills\\excel-edit\\scripts\\edit_excel.py", line 100, in main\n    apply_op(wb, op)\n  File "C:\\DT_Python\\Locohane\\skills\\excel-edit\\scripts\\_ops.py", line 363, in apply_op\n    handler(wb, op)\n  File "C:\\DT_Python\\Locohane\\skills\\excel-edit\\scripts\\_ops.py", line 121, in op_set_range\n    cell = ws.cell(row=min_row + r_offset, column=min_col + c_offset, value=value)\n  File "C:\\DT_Python\\Python311\\env_local_agent_system\\Lib\\site-packages\\openpyxl\\worksheet\\worksheet.py", line 246, in cell\n    cell.value = value\nAttributeError: 'MergedCell' object attribute 'value' is read-only'
```

**補足（監視上の注意）**: `WARNING`レベルのログは`-> [終了コード] 1`のみで、実際のエラー内容（`[標準エラー]`以下のTraceback）は同一イベントの`DEBUG`レベル行にのみ記録されていた。`config.ini`の`[log].level=debug`によりファイルには残っているが、本スキルが基本方針とする「WARNING/ERROR/CRITICALのみ抽出」だとエラーの実体を見落とす。今回はWARNING行の直後にある同時刻・同内容のDEBUG行を突き合わせて発見した。

## エラー原文

```
Traceback (most recent call last):
  File "C:\DT_Python\Locohane\skills\excel-edit\scripts\edit_excel.py", line 131, in <module>
    sys.exit(main())
             ^^^^^^
  File "C:\DT_Python\Locohane\skills\excel-edit\scripts\edit_excel.py", line 100, in main
    apply_op(wb, op)
  File "C:\DT_Python\Locohane\skills\excel-edit\scripts\_ops.py", line 363, in apply_op
    handler(wb, op)
  File "C:\DT_Python\Locohane\skills\excel-edit\scripts\_ops.py", line 121, in op_set_range
    cell = ws.cell(row=min_row + r_offset, column=min_col + c_offset, value=value)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\DT_Python\Python311\env_local_agent_system\Lib\site-packages\openpyxl\worksheet\worksheet.py", line 246, in cell
    cell.value = value
    ^^^^^^^^^^
AttributeError: 'MergedCell' object attribute 'value' is read-only
```

## 推定原因

- `skills/excel-edit/scripts/_ops.py`の`op_set_range`（121行目）は`ws.cell(...).value = value`で直接書き込んでおり、書き込み先セルが既存の結合範囲内の非アンカーセル（`MergedCell`）である場合を考慮していない。openpyxlの仕様上、結合範囲の左上以外のセルは`MergedCell`となり`.value`への代入が`AttributeError`を送出する。
- 今回のケースでは、既存の`annual_schedule.xlsx`（前回セッションで月ごとにA列を結合済み）に対して`insert_rows(index=1, count=1)`で1行挿入した直後に`set_range`でA2からヘッダー行を書き込もうとしており、挿入によってシフトした結合範囲と書き込み先が衝突したとみられる（未検証: `insert_rows`実装が結合範囲の座標を正しくシフトしているか、シフト後にA2が実際に結合セル内に入っているかは、`_ops.py`のソース未確認のため断定できない）。
- いずれにせよ、`op_set_range`が結合セルとの衝突を検知せず未捕捉の例外でスクリプト全体をクラッシュさせる点は、LLM側にエラー内容が「Pythonの内部例外」としてしか伝わらず、原因（結合セルとの衝突）を自力で推測しにくい形になっている。

## 追記（YYYY-MM-DD HH:MM）

（同一原因の問題が再検知されるたびに、ここに追記を積み重ねていく）

## ユーザー回答

ここにはユーザーの回答が記述される
