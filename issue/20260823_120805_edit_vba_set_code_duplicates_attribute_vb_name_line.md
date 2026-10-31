# edit_vba.pyのset_code/add_moduleが読み込んだコードをそのまま書き戻すとAttribute VB_Name行が二重化する

- **区分**: バグ → 修正済み
- **検知日時**: 2026-08-23 12:08:05
- **対象ログファイル**: data/logs/app_20260823_104959.log

## 経緯

VBAマクロ修正タスクで、workerが`read_vba.py --module CSVImporter`で既存
コードを読み、その内容を元に修正したコードを`set_code`で書き戻した。直後に
確認のため再度`read_vba.py`で読み直したところ、モジュール冒頭が
`Attribute VB_Name = "CSVImporter"`という同じ行が2回連続する状態になって
いた（本来1行のはずが2行）。

## ログ引用

```
2026-08-23 12:06:36,627 WARNING src.subagent: subagent tool=Read args={'file_path': '@44'} -> {... "code": "Attribute VB_Name = \"CSVImporter\"\r\nOption Explicit\r\n..."}
（↑最初の読み込み時点では1行のみ）

2026-08-23 12:08:05,906 WARNING src.subagent: subagent tool=execute_python_code args={'code': 'import json\nops = [{"op": "set_code", "name": "CSVImporter", "code": \'Attribute VB_Name = "CSVImporter"\\r\\nOption Explicit\\r\\n...\'}]\n...'} -> [終了コード] 0

2026-08-23 12:08:20,751 WARNING src.subagent: subagent tool=Read args={'file_path': '@45'} -> {... "code": "Attribute VB_Name = \"CSVImporter\"\r\nAttribute VB_Name = \"CSVImporter\"\r\nOption Explicit\r\n..."}
（↑set_code適用後の再読み込みで2行に重複）
```

## エラー原文

（例外は発生していないため無し。データ破損型のバグ）

## 推定原因

`skills/excel-vba-read/scripts/read_vba.py`はoletools（`VBA_Parser.extract_all_macros()`）
でファイルのバイト列から直接ソースを抽出しており、モジュール冒頭の
`Attribute VB_Name = "..."`宣言行を`code`に含めて返す。

一方`skills/excel-vba-edit/scripts/_vba_ops.py`の`_replace_code()`は
COM経由の`CodeModule.AddFromString()`で全文書き込みを行うが、この関数は
Attribute行を特別扱いせず単なるテキストとして挿入する。VBEは`comp.Name`から
Attribute VB_Nameを内部的に自動管理しているため、書き込むコード文字列に
同じ内容のAttribute行が含まれていると、内部管理分＋テキスト由来分の2行に
重複してしまう。

`read_vba.py`の出力をそのまま`set_code`/`add_module`に渡す（読み込んだコードを
元に修正して書き戻す）というワークフローはごく自然な使い方であり、今回のように
毎回この往復を行うたびにAttribute行が増殖しうる構造的な欠陥だった。

## 対応（実装済み・2026-08-23）

`skills/excel-vba-edit/scripts/_vba_ops.py`に`_strip_leading_attribute_lines()`を
追加し、`_replace_code()`が`AddFromString()`を呼ぶ前にモジュール冒頭の
連続する`Attribute ... = ...`行を除去するよう修正した（`op_set_code`/
`op_add_module`はいずれも`_replace_code()`経由のため両方に効く）。

テスト: `tests/test_excel_vba_edit_attribute_line_duplication.py`を新規追加
（5件）。単一/複数のAttribute行除去、Attribute行が無い場合は無変更、
本文中の類似文字列は誤って除去しない、`_replace_code`が実際に除去後の
コードを`AddFromString`に渡すことを確認。`pytest tests/`435件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
