---
name: excel-tools
description: xlsx/xls/xlsmファイルの読み込み、既存xlsx/xlsmの編集（セル書式・行列操作・グラフ・条件付き書式・データ検証を含む）、数式の再計算を行う。ユーザーがExcelファイルの中身を確認したいとき、表データを新規に作成/編集したいとき、書式付きのレポートやグラフ入りのExcelを出力したいとき、数式の計算結果を確認したいときに使う。
license: MIT
metadata:
  author: local-agent-system
  version: "2.0"
---

# excel-tools

xlsx/xls/xlsm の読み込み・編集・数式再計算を行うスキルです。`scripts/` 配下の
3つのスクリプトを `run_script` ツールで実行して結果を得ます。

- `read_excel.py` … 読み込み専用（シート一覧・セルデータ取得）
- `edit_excel.py` … 新規作成・既存編集の両方（セル書き込み・書式・行列操作・
  グラフ・条件付き書式・データ検証）
- `recalc_excel.py` … Excel本体をバックグラウンド起動して数式を再計算し、
  エラーセルを検出する

読み込みは `.xlsx`/`.xlsm` を `openpyxl`、レガシー形式の `.xls` を `xlrd` で
処理します。編集・生成は `.xlsx`/`.xlsm` のみ対応します（`.xls` は生成・
編集不可、読み込みのみ）。

各スクリプトは正常系なら終了コード0でJSON1行を標準出力へ、異常系なら
終了コード非0でエラーメッセージを標準エラーへ出力します。

このプロジェクトには汎用のファイル書き込みツールが無いため、構造化データ
（ops等）は **LLMが組み立てたJSON文字列をそのまま `run_script` の
`script_args` の1要素として渡す**ことでスクリプトへ伝えます
（`run_script` はシェルを介さず引数リストをそのまま子プロセスに渡すため、
引用符のエスケープを気にする必要はありません）。ユーザーが既存のJSON
ファイルを持っている場合は `--ops-file`/`--data-file` 系の引数でパス
指定も可能です。**opsの要素数が多い・日本語の長い文字列を複数含む等で
`--ops-json` に直接渡すJSON文字列の構文エラー（引用符の閉じ忘れ等）を
2回以上繰り返してしまった場合は、無理に1行のJSON文字列として組み立て
直すことに固執せず、`execute_python_code` で ops を Python の list/dict
として組み立てて `json.dump` で作業ディレクトリ配下の一時ファイル
（例: `ops.json`）へ書き出し、`--ops-file <そのファイルの絶対パス>` で
渡す方法に切り替えること。** そちらのほうが構文エラーを起こしにくい。

## 1. read_excel.py — シート一覧・セルデータの読み込み

`run_script` の引数:
- `skill_name`: `excel-tools`
- `script`: `scripts/read_excel.py`
- `script_args`: `["<対象ファイルのパス>", "--sheet", "<省略可>", "--offset", "<省略可>", "--limit", "<省略可>", "--data-only"]`

### 手順（推奨フロー）

1. まず `--sheet` を付けずに実行し、シート名と行数・列数の一覧を確認する。
2. 読みたいシートが分かったら `--sheet` にシート名（またはシート一覧内での
   0始まりインデックス）を指定して実行し、セルデータを取得する。
3. シートの行数が多い場合は `--offset`/`--limit`（既定 offset=0, limit=200）
   で範囲を分けて読み進める。`total_rows` を見て続きが必要か判断する。
4. 数式が入ったセルは既定では数式文字列（例: `"=SUM(A1:A10)"`）が返る。
   Excelが最後に計算した値が欲しい場合は `--data-only` を付ける（xlsxのみ
   有効）。数式を新規に書き込んだ直後で最新の計算値が欲しい場合は、先に
   `recalc_excel.py` を実行してから読み直すこと。

### 出力例（シート一覧モード、`--sheet` 省略時）

```json
{"path": "C:\\foo\\book.xlsx", "mode": "sheets", "sheets": [{"name": "Sheet1", "max_row": 120, "max_column": 5}]}
```

### 出力例（セルデータモード、`--sheet` 指定時）

```json
{"path": "C:\\foo\\book.xlsx", "mode": "rows", "sheet": "Sheet1", "total_rows": 120, "total_columns": 5, "start_row": 1, "end_row": 120, "rows": [["名前", "点数"], ["太郎", 80]]}
```

`rows` は1行=1配列（セル値の配列）のリストです。日付・時刻のセルはISO8601
文字列（例: `"2026-07-13T00:00:00"`）に変換済みです。空セルは `null` です。

### エッジケース

- ファイルが存在しない、拡張子が `.xlsx`/`.xlsm`/`.xls` 以外、指定シートが
  見つからない、破損したファイル等はいずれもエラーメッセージ＋終了コード1。
  その内容をそのままユーザーに伝えること。
- `openpyxl`/`xlrd` が実行環境に無い場合は `ImportError` で終了コード1に
  なる。その場合は導入者へ `pip install openpyxl xlrd` の実施を促すこと。

## 2. edit_excel.py — 新規作成・既存編集

`run_script` の引数:
- `skill_name`: `excel-tools`
- `script`: `scripts/edit_excel.py`
- `script_args`: `["<対象パス.xlsx/.xlsm>", "--ops-json", "<JSON配列>", "--new（省略可）", "--overwrite（省略可）", "--output", "<省略可、別名保存先>"]`

- `--new` を付けると新規ワークブックを作成する（既存ファイルがある場合は
  `--overwrite` も付けないとエラーになる）。
- `--new` を付けない場合は対象パスを読み込んで編集する（存在しないと
  エラー）。編集で触れなかった既存のセル・書式・シートはそのまま保持
  される。
- `--output` を省略すると対象パスへ上書き保存する。別ファイルとして保存
  したい場合のみ指定する。
- `.xlsm` はマクロ（VBAプロジェクト）を保持したまま編集できるが、
  マクロ自体の追加・変更はこのスキルの対象外。

### ops（操作）の一覧

`ops` は適用したい操作を順番に並べたJSON配列。各要素は `"op"` キーで
種別を判別する。1回の呼び出しに複数opをまとめて渡してよい（例: シート追加
→データ書き込み→書式→グラフ追加、を1コールで実行できる）。

| op | 必須パラメータ | 任意パラメータ | 説明 |
|---|---|---|---|
| `add_sheet` | `name` | `index` | シート追加 |
| `delete_sheet` | `name` | - | シート削除 |
| `rename_sheet` | `name`, `new_name` | - | シート名変更 |
| `set_cell` | `sheet`, `cell` | `value`, `style` | 単一セルへ値・書式を設定 |
| `set_range` | `sheet`, `start_cell`, `rows` | `style`, `header_style` | 起点セルから複数行を一括書き込み。`header_style` は1行目のみに適用（見出し行用）。列幅は自動調整される |
| `set_style` | `sheet`, `range` | `style` | 値は変えずに既存セルへ書式のみ適用 |
| `insert_rows` | `sheet`, `index` | `count`(既定1) | 行挿入（1始まり行番号の位置に挿入） |
| `delete_rows` | `sheet`, `index` | `count`(既定1) | 行削除 |
| `insert_cols` | `sheet`, `index` | `count`(既定1) | 列挿入（1始まり列番号） |
| `delete_cols` | `sheet`, `index` | `count`(既定1) | 列削除 |
| `set_column_width` | `sheet`, `column`, `width` | - | 列幅を手動指定（例: `column: "A"`） |
| `merge_cells` | `sheet`, `range` | - | セル結合（例: `range: "A1:C1"`） |
| `freeze_panes` | `sheet`, `cell` | - | ウィンドウ枠の固定（例: `cell: "A2"` で1行目を固定） |
| `add_chart` | `sheet`, `type`, `data_range`, `anchor` | `title`, `categories_range`, `titles_from_data`(既定true) | グラフ追加。`type` は `bar`/`line`/`pie`/`scatter` |
| `add_conditional_format` | `sheet`, `range`, `rule_type`, `params` | - | 条件付き書式。下記参照 |
| `add_data_validation` | `sheet`, `range`, `type` | `formula1`, `formula2`, `prompt`, `prompt_title`, `error_message`, `error_title`, `allow_blank`(既定true) | データ検証（入力規則）。下記参照 |

`sheet` はシート名または `read_excel.py` で得たシート一覧内の0始まり
インデックスで指定できる。

`rows`/`value` のセルは文字列・数値・真偽値・`null`（空セル）に対応する。
`"=SUM(B2:B3)"` のように `=` で始まる文字列はExcel上で数式として評価
される（このスクリプト自身は数式を評価しない。計算値を確認したい場合は
`recalc_excel.py` を使う）。

### style 共通スキーマ（`set_cell`/`set_range`/`set_style`/`header_style`で共通）

```json
{
  "bold": true, "italic": false,
  "font_color": "0000FF", "font_size": 11,
  "fill_color": "FFFF00",
  "number_format": "#,##0.00",
  "align": "center", "valign": "center", "wrap_text": false,
  "border": "thin",
  "role": "input"
}
```

すべて省略可。`font_color`/`fill_color` はRRGGBBの16進数（例: 赤 `FF0000`）。
`align` は `left`/`center`/`right`、`valign` は `top`/`center`/`bottom`。
`border` は文字列（`thin`/`medium`/`thick`。4辺すべてに適用）または
`{"top": "thin", "bottom": "thin"}` のように辺ごとの指定も可（辺キーは
`top`/`bottom`/`left`/`right`）。

**`role` によるセルの色分け規約（Anthropic公式スキルに準拠、推奨）**:
財務モデルやデータ集計シートを作る際、`font_color` を明示する代わりに
`role` を指定すると自動的に規約色になる。

- `role: "input"` → 青 `0000FF`（ハードコードした入力値のセル）
- `role: "formula"` → 黒 `000000`（同一シート内で完結する数式）
- `role: "link"` → 緑 `008000`（他シートを参照する数式）

表を作るときはこの規約に沿って `role` を使い分けると、後で人間が見ても
「どこが手入力でどこが計算か」が一目で分かるシートになる。

### 条件付き書式（`add_conditional_format`）

`rule_type` と `params` は openpyxl の `openpyxl.formatting.rule` の
コンストラクタ引数にそのまま渡される。

| rule_type | params例 |
|---|---|
| `color_scale` | `{"start_type": "min", "start_color": "FFFFFF", "end_type": "max", "end_color": "FF0000"}` |
| `cell_is` | `{"operator": "greaterThan", "formula": ["100"], "fill": null}`（`fill`はPatternFillオブジェクトが必要なため実質省略。色付けしたい場合は事前に`set_style`で該当セルへ書式を当てておく運用でもよい） |
| `formula` | `{"formula": ["$A1=\"NG\""]}` |
| `data_bar` | `{"start_type": "min", "end_type": "max", "color": "638EC6"}` |
| `icon_set` | `{"icon_style": "3TrafficLights1", "type": "percent"}` |

不正な `params` を渡すと `ops[N]の適用に失敗しました` として詳細な
Pythonの例外メッセージがそのまま返るので、それを手がかりに修正すること。

### データ検証（`add_data_validation`）

例: プルダウンリストを設定する場合

```json
{"op": "add_data_validation", "sheet": "Sheet1", "range": "C2:C100", "type": "list", "formula1": "\"OK,NG,保留\"", "prompt": "OK/NG/保留のいずれかを選択", "error_message": "リストにない値です"}
```

`type` は `list`/`whole`/`decimal`/`date`/`textLength`/`custom` に対応
（openpyxlの `DataValidation` の `type` 引数と同じ）。

### 手順

1. ユーザーの依頼内容から ops の配列を組み立てる（表データ投入は
   `set_range`、既存表への追記は `insert_rows`+`set_range`、見た目の調整は
   `set_style`、グラフが要る場合は `add_chart`、というように必要なopを
   組み合わせる）。
2. `run_script` を上記引数で呼び出す。
3. 既存ファイルを上書きしてよいか不明な場合（`--new`使用時に対象が既に
   存在する場合）は `--overwrite` を付けずに一度実行し、エラーが出たら
   ユーザーに上書き可否を確認する。
4. 成功時は `{"path": ..., "sheets": [...], "applied_ops": N}` が返るので、
   保存先パスをユーザーへ伝える。

### エッジケース

- 拡張子が `.xlsx`/`.xlsm` でない、`ops` がJSON配列でない、各opに `op`
  キーが無い、存在しないシートやopを指定した、`--new`なしでファイル不在、
  `--new`ありで`--overwrite`無しに既存ファイルを指定、などはいずれも
  エラーメッセージ＋終了コード1。どのop（何番目・どの種別）が失敗したか
  がメッセージに含まれるので、そのまま報告するかopを修正して再実行する。
- `openpyxl` が無い場合は `ImportError` で終了コード1。

## 3. recalc_excel.py — 数式の再計算・エラーセル検出

`run_script` の引数:
- `skill_name`: `excel-tools`
- `script`: `scripts/recalc_excel.py`
- `script_args`: `["<対象ファイルのパス>"]`

`edit_excel.py` で書き込んだ数式はそのままでは評価されない（openpyxlは
数式を評価しない）ため、LLMが計算結果を確認したい場合はこのスクリプトを
実行してから `read_excel.py --data-only` で読み直す。ローカルに
インストール済みのMicrosoft Excelを画面に表示せず起動し、実際に
計算・上書き保存する。

### 出力例

```json
{"path": "C:\\foo\\book.xlsx", "recalculated": true, "errors": [{"sheet": "Sheet1", "cell": "B5", "value": "#DIV/0!"}]}
```

`errors` が空配列であれば数式エラーは無い。1件でもあればユーザーへ
「どのシートのどのセルがエラーか」を報告し、必要なら該当セルを
`edit_excel.py` の `set_cell` で修正する。

### 制約・注意点（実行前にユーザーへ伝えるべき情報）

- **ローカルにMicrosoft Excelがインストールされ、対話セッションから
  呼び出されている必要がある**（サーバーサービスとしての実行では動かない
  可能性が高い）。このプロジェクトはネイティブWindows環境前提のため通常は
  問題ないが、Excel未導入の環境ではこのスクリプトだけ使えない。
- 内部で実際にExcelプロセス（EXCEL.EXE）を一時的に起動する。処理完了時は
  必ず終了させるが、**万一スクリプトが強制終了された場合はEXCEL.EXEが
  残留する可能性がある**（その場合はタスクマネージャーで手動終了が必要な
  旨をユーザーに伝えること）。
- 大きなワークブックは計算に時間がかかり、`run_script` のタイムアウト
  （既定60秒、`config.ini` の `script_timeout`）を超える可能性がある。
  超過した場合はタイムアウトを増やすようユーザーに提案すること。
- インターネットからダウンロードした（Mark of the Web が付いた）ファイルは
  Excelが保護されたビューで開いてしまい正しく再計算できない場合がある。
  このスキルで生成・編集したファイルであれば通常は問題ない。

### エッジケース

- ファイル不在、拡張子が `.xlsx`/`.xlsm`/`.xls` 以外はエラーメッセージ＋
  終了コード1。
- `pywin32`（`win32com.client`）が実行環境に無い場合は `ImportError` で
  終了コード1。その場合は導入者へ `pip install pywin32` の実施を促すこと。
- Excel側のCOMエラー（未インストール、ライセンス未認証、ファイルが他
  プロセスで開かれている等）は "Excelでの再計算に失敗しました: ..." として
  終了コード1。エラーメッセージの内容をそのままユーザーに伝えること。
