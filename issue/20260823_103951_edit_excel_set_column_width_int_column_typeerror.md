# edit_excel.py: set_column_widthのcolumnにint（列番号）を渡すとopenpyxlのTypeErrorで失敗する

- **区分**: バグ → 修正済み
- **検知日時**: 2026-08-23 10:37:24

- **対象ログファイル**: data/logs/app_20260823_102118.log

## 経緯

excel-vbaマクロブック作成タスク中、workerが取引明細表シートの列幅を
調整するため`set_column_width`を6件（A〜F列）呼び出そうとした。
`--ops-file`のパス解決で1往復（別issue [20260823_103204](20260823_103204_edit_vba_new_only_requires_dummy_empty_ops_json.md)は無関係、
今回は`@N`パスメモリー参照を使わず手でパスを構築して失敗→自己修正した
別件の迷走）試行錯誤した後、最終的に`--ops-json`で下記のopsを送った。

```json
[{"op": "set_column_width", "sheet": "取引明細表", "column": 1, "width": 10}, ...]
```

`column`に列文字（`"A"`）ではなく1始まりの列番号（`1`）を渡したところ、
`edit_excel.py`が例外で終了した。

## ログ引用

```
2026-08-23 10:37:24,593 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-edit', 'script_filename': 'edit_excel.py', 'script_args': ['E:\\user_b\\vba-test\\cashflow.xlsm', '--ops-json', '[{"op": "set_column_width", "sheet": "取引明細表", "column": 1, "width": 10}, {"op": "set_column_width", "sheet": "取引明細表", "column": 2, "width": 8}, ...]']} -> "[終了コード] 1\n[標準エラー]\nops[0]（op='set_column_width'）の適用に失敗しました: <class 'openpyxl.worksheet.dimensions.ColumnDimension'>.index should be <class 'str'> but value is <class 'int'>"
```

## エラー原文

```
ops[0]（op='set_column_width'）の適用に失敗しました: <class 'openpyxl.worksheet.dimensions.ColumnDimension'>.index should be <class 'str'> but value is <class 'int'>
```

## 推定原因（コード確認済み）

`skills/excel-edit/scripts/_ops.py`の`op_set_column_width`が
`op["column"]`をそのまま`ws.column_dimensions[...]`のキーへ渡していた
（修正前）:

```python
def op_set_column_width(wb, op: dict) -> None:
    width = min(max(float(op["width"]), 1), 60)
    _sheet(wb, op["sheet"]).column_dimensions[op["column"]].width = width
```

`column_dimensions`はopenpyxl側で列文字（`str`、例:`"A"`）のみを
インデックスとして受け付ける実装になっており、intを渡すと
`ColumnDimension.index should be str but value is int`という
openpyxl内部のバリデーションエラーがそのまま外へ漏れる。

`delete_cols`/`insert_cols`は同じファイル内で`op["index"]`（int、1始まり）
という別名のパラメータを使っており、こちらは元々int専用として設計されて
いる。しかし`set_column_width`は「column」という名前ゆえに列文字と
列番号のどちらでも自然に思えるにもかかわらず、実装は列文字専用だった。
SKILL.mdにも型の指定が無く、LLMが列番号（1,2,3...）で渡すのはごく自然な
選択だった。

## 対応（実装済み・2026-08-23）

`skills/excel-edit/scripts/_ops.py`の`op_set_column_width`を、`column`が
`int`なら`openpyxl.utils.get_column_letter()`で列文字へ変換し、`str`なら
そのまま使うよう修正した。`skills/excel-edit/SKILL.md`の`set_column_width`
説明にも「`column`は列文字（`"A"`等）と1始まり列番号（`1`等）のどちらでも
指定可」と明記した。

テスト: `tests/test_excel_edit_ops.py`に
`test_set_column_width_accepts_column_letter`・
`test_set_column_width_accepts_1indexed_column_number`を追加。
`pytest tests/` 412件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
