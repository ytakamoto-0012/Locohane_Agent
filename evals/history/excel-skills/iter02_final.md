---
name: excel-read
description: xlsx/xlsm/xlsファイルの読み込み専用スキル（シート一覧・セルデータの取得）。数式セルは数式文字列またはExcelが最後に計算した値のどちらかを選んで取得できる。xlsx/xlsmは既定でセルの値と数値表示形式のみを軽量取得し、`--style`を付けると書式（太字・背景色・罫線・結合セル・構造化テーブル）のフルstyle情報も取得できる。Excel本体は不要（openpyxlで直接読む）。ユーザーがExcelファイルの中身を確認・要約・検索したいとき、表データの値を読み取りたいとき、書き込んだ内容や書式が正しく反映されているか検証したいときに使う。VBAマクロコードを読みたい場合はexcel-vba-read、罫線・グラフ・レイアウトを画像として見たい場合はexcel-render、値の新規作成・編集はexcel-editを使う。
license: MIT
metadata:
  author: ytakamoto
  version: "1.0"
---

# excel-read

xlsx/xlsm/xls のシート一覧・セルデータを読み込み専用で取得するスキル。
`read_excel.py` を実行する。

## 呼び出し

`file_path`は必ず先頭の位置引数として渡す（フラグの前後関係は自由）。目的に応じて以下のように引数を組み合わせる。

```bash
# シート一覧のみ取得（--sheet省略、まず最初に実行する）
python read_excel.py "C:\Users\me\book.xlsx"

# 指定シートのセルデータを上から200行読む（既定のoffset/limit）
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1

# 201行目以降を続けて読む（分割読み込み）
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --offset 200 --limit 200

# 必要な列だけに絞り込んで読む（列数が多い表で一度に読む量を減らす）
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --columns "A,C,E"

# 列を絞った上で行も分割して読む（行数・列数どちらも多い表はこの組み合わせが基本）
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --columns "A,C,E" --offset 200 --limit 200

# 数式セルをExcelが最後に計算した値で取得する（数式文字列ではなく計算結果が欲しいとき）
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --data-only

# 太字・背景色・罫線等のフルstyle情報も取得する（既定は軽量モードでvalueと数値表示形式のみ）
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --style

# 列の値ごとの行範囲をグルーピング取得する（生のrowsを目で数えない）
python read_excel.py "C:\Users\me\book.xlsx" --sheet 月間予定表 --query-json '[{"op": "group_by", "column": "A"}]'
```

## 引数一覧

| 引数 | 必須/任意 | 値の型 | 既定値 | 説明 |
|---|---|---|---|---|
| `file_path`（位置引数、フラグ名なし） | 必須 | 文字列（絶対パス） | - | 読み込み対象の`.xlsx`/`.xlsm`/`.xls`ファイルパス。`script_args`配列の先頭要素として渡す |
| `--sheet` | 任意 | 文字列 | 省略＝シート一覧モード | シート名、または0始まりインデックス（例`"0"`＝先頭シート）。指定するとセルデータ取得モードになる |
| `--offset` | 任意 | 整数文字列 | `"0"` | 読み飛ばす行数（0始まり）。分割読み込み時に前回の`end_row`をそのまま次回`offset`に使う |
| `--limit` | 任意 | 整数文字列 | `"200"` | 読み込む最大行数。大きい表でも一度に全件読まず、`total_rows`を見ながら分割読み込みする |
| `--data-only` | 任意・値なしフラグ | - | 付けない＝数式文字列を返す | 数式セルを数式文字列ではなくExcelが最後に計算したキャッシュ値で返す。`.xlsx`/`.xlsm`のみ有効（`.xls`は常に値のみで無関係） |
| `--style` | 任意・値なしフラグ | - | 付けない＝軽量モード | `.xlsx`/`.xlsm`限定。付けると太字・背景色・罫線等のフルstyle情報と`column_widths`/`row_heights`/`warnings`も返す。既定（付けない）ではセルは`value`と（設定されていれば）`style.number_format`のみを返し、`column_widths`/`row_heights`/`warnings`は計算自体を省略する（`merged_cells`/`tables`は既定でも返す）。書式の細部（太字・背景色等）まで検証したいときのみ付ける。`.xls`指定時はエラー（元々styleを取得しないため） |
| `--query-json` | 任意 | 文字列（JSON配列を1行化） | なし | 構造化クエリ（詳細は下記「構造化クエリ」節）。`.xlsx`/`.xlsm`のみ対応かつ`--sheet`必須（`.xls`指定・`--sheet`省略はエラー） |
| `--columns` | 任意 | 文字列（カンマ区切り） | 省略＝全列 | 読み込む列を列アルファベット（`"A"`）または1始まりの列番号（`"3"`＝C列）のカンマ区切りで指定（例`"A,C,E"`）。`--sheet`必須。`.xlsx`/`.xlsm`/`.xls`いずれも対応。指定列が総列数を超えるとエラー |

`--sheet`を省略した場合はシート一覧のみを返す（大きいファイルを誤って全件読み込まないための既定動作）。それ以外の引数はすべて省略可。

## 入出力の型

成功=終了コード0＋JSON1行を標準出力。失敗=終了コード非0＋エラーメッセージを標準エラー。エラー文はそのままユーザーへ伝える。未導入ライブラリは`ImportError`終了コード1（原因: `openpyxl`/`xlrd`）→該当する`pip install <パッケージ名>`をユーザーに促す。

このスクリプトは本文（シート一覧/rows）を標準出力に出さず、一時JSONへ書き出して`result_path`を返す。`result_path`を読む（分割読み込み可）。

## 手順

1. まず`--sheet`なしで実行しシート名・行数・列数を確認する。
2. `--sheet`にシート名（またはシート一覧内0始まりインデックス）を指定してセルデータ取得。
3. 行数・列数が多いときは引数を使い分ける：
   - `--offset`/`--limit`: セル値を上から順に読みたいときに使う。1回あたり`--limit`行ずつ`rows`に返る。`total_rows`を見て続きが要るか判断し、次回`--offset`に前回の`end_row`を渡して続きから読む。
   - `--columns`: 見たい列が分かっているとき（特定の列だけ値を確認したい、列数が多い表を一度に全部読みたくない等）に使う。`total_columns`が20を超えるような表では、まず`--sheet`なしの一覧やヘッダー行1行だけを読んで列の中身を把握してから、必要な列だけに絞って読む方が、全列を大きい`--limit`で一度に読むより安全（一度に読む量が多いと内容の見落としにつながる）。
   - `--query-json`: 列の値ごとの行範囲（グルーピング列の範囲確認、`insert_rows`/`merge_cells`後の検証など）を知りたいときに使う（下記「構造化クエリ」節）。生の`rows`を目で数えて行範囲を手計算しない。`--offset`/`--limit`/`--columns`の指定値には影響されず、常にシート全体（1行目〜`total_rows`）が対象になる。`rows`本体が不要なら`--limit`は既定`200`のままでよい（`query_results`は`--limit`の値に関係なく全件返る）。
4. 数式セルは既定で数式文字列（`"=SUM(A1:A10)"`）を返す。Excelが最後に計算した値が欲しければ`--data-only`（xlsxのみ、`rows`・`query_results`両方の値表示に影響する）。数式を書き込んだ直後の最新値が欲しい場合は先にexcel-recalcスキルの`recalc_excel.py`を実行してから読み直す。
5. `.xlsx`/`.xlsm`は`--sheet`指定時、既定では`value`と数値表示形式のみを返す。excel-editスキルの`edit_excel.py`で書いた書式（太字・背景色・結合・テーブル）が正しく反映されているか検証したいとき、**または表のデータがおかしくないか調べたいとき**は`--style`を付ける（`read_only=False`でファイル全体を読むため`.xls`や既定モードより低速）。既定（`--style`なし）では数式の循環参照・列幅超過などの構造的な異常は一切検出されない。`warnings`にこれらが自動で入るのは`--style`を付けた場合のみなので、「おかしいところがないか確認して」と言われたら必ず`--style`付きで読む。`--query-json`の`query_results`にはstyle情報は含まれない（`rows`側のみ）。

## 出力

`result_path`（JSONファイル）のトップレベルキーはどちらのモードでも`path`（読み込んだファイルパス）と`mode`（`"sheets"`または`"rows"`）を含む。それ以外は`--sheet`の有無・拡張子（`.xlsx`/`.xlsm`か`.xls`か）・`--query-json`の組み合わせで変わる。

**シート一覧モード**（`--sheet`省略時）: `sheets`＝各要素`{"name","max_row","max_column","print_area"}`のリストのみ。`print_area`は各シートに設定された印刷範囲（例`"'Sheet1'!$A$1:$B$20"`、複数範囲があるシートはカンマ区切りで連結された文字列）で、未設定なら`null`。`.xlsx`/`.xlsm`のみ値が入り、`.xls`は`print_area`キー自体が存在しない（xlrdでは印刷範囲を取得できないため非対応）。

**セルデータモード**（`--sheet`指定時）共通キー: `sheet`（解決後のシート名）、`total_rows`/`total_columns`（シート全体の行数・列数。`--columns`で絞り込んでも変わらない）、`start_row`/`end_row`（今回`rows`に含まれる1始まり行番号の範囲。1行も返らなければ両方`null`。次回`--offset`にはこの`end_row`をそのまま渡せる）、`columns`（今回`rows`に含まれる列アルファベットの配列、例`["A","C","E"]`。`--columns`省略時は全列）、`rows`（1行1配列のリスト、`--offset`/`--limit`/`--columns`の範囲のみ）。**`rows`の各行の`j`番目の要素は、必ず`columns[j]`列の値**（`--columns`で列を間引いても、この対応関係で元の列が分かる。行内の位置だけで列を数えない）。

- `.xls`のとき: `rows`のセル値は日付/時刻はISO8601文字列、空セルは`null`（style情報は非対応）。
- `.xlsx`/`.xlsm`のとき: `rows`の各セルが`{"value":..., "style":{...}}`（`style`キーは値がある場合のみ）。`value`は日付/時刻はISO8601文字列、空セルは`null`、数式セルは既定で数式文字列（例`"=SUM(A1:A10)"`）、`--data-only`を付けるとExcelが最後に計算したキャッシュ値（例`15`）に変わる。加えてトップレベルに`merged_cells`（シート全体、offset/limit範囲に関わらず全件）と`tables`（構造化テーブル一覧）が付く。
  - **既定（`--style`なし、軽量モード）**: `rows`の各セルは`value`と（`number_format`が設定されている場合のみ）`style.number_format`だけを返す（`bold`/`fill_color`等は省略）。トップレベルの`merged_cells`/`tables`は返るが、`column_widths`/`row_heights`/`warnings`はキー自体省略（計算をスキップする）。
  - **`--style`ありのとき**: `style`のキー体系はフルで返る:
```json
{"bold": true, "italic": false, "font_color": "0000FF", "font_size": 11,
 "fill_color": "FFFF00", "number_format": "#,##0.00",
 "align": "center", "valign": "center", "wrap_text": false,
 "border": "thin"}
```
    既定値と一致する項目は省略、書式なしセルは`style`キー自体省略。加えて以下の追加フィールドが付く:
    - `column_widths`: `columns`に含まれる列（＝返却範囲の列。`--columns`指定時はその列のみ）の幅（`{"A": 12.3, "B": null, ...}`）。未設定列は`null`。
    - `row_heights`: 返却範囲（`start_row`〜`end_row`）の各行の高さ（`{"3": 15.0, ...}`）。未設定行は省略。
    - `warnings`: 構造的な不備の警告配列（該当なしならキー省略）。`wrap_text`が有効でないセルで、推定表示幅が列幅を超えている場合に文字切れの可能性を指摘。加えて、数式セル（`--data-only`未指定時のみ判定可能）の参照範囲が自身のセルを含む場合（例: `N8`セルの`=SUM(B8:N8)`）はExcelの循環参照エラーの可能性を指摘する（シート修飾された参照`Sheet2!A1:B2`は判定対象外）。
- `--query-json`ありのとき: 上記`rows`一式に加え、トップレベルに`query_results`が付く。**`query_results`は`--offset`/`--limit`の範囲に関わらずシート全体が対象**（詳細・例は次の「構造化クエリ」節）。`query_results`にはstyle情報は含まれない（`--style`の有無に関わらず）。

## 構造化クエリ（`--query-json`、必須ルール）

**`insert_rows`/`merge_cells`後の検証や、グルーピングされた列（月・区分など、結合セル化される列）の範囲確認では、生の`rows`を目で数えて行範囲を手計算しない。** 代わりに`--query-json`でクエリを渡すと、列の値ごとに連続する行範囲へグルーピングした結果を1回の呼び出しで返す。

```bash
python read_excel.py "C:\Users\me\book.xlsx" --sheet 月間予定表 --query-json '[{"op": "group_by", "column": "A"}]'
```
出力（`result_path`内、`query_results`キー）:
```json
"query_results": [
  {"op": "group_by", "column": "A",
   "items": [{"value": "4月", "start_row": 2, "end_row": 2, "row_count": 1},
             {"value": "5月", "start_row": 3, "end_row": 4, "row_count": 2}]}
]
```
- `group_by`の必須キー: `"op": "group_by"`と`"column"`（列アルファベット`"A"`または1始まりの列番号、例`"3"`＝C列）。`column`欠落はエラー＋終了コード1。
- 挙動: 指定列を**1行目から**`total_rows`まで上から走査し、非null値ごとに連続する行範囲（結合セルの非アンカー行・空欄継続行を含む）を`{value,start_row,end_row,row_count}`としてまとめる。
- **見出し行も除外されない。** 見出しセル（例`A1`の"月"）に値があれば、それ自体が`start_row:1,end_row:1`の独立した1件として`items`の先頭に混ざる。上記の出力例で`start_row`が`2`から始まっているのは見出しセルがたまたま空だったケースであり、一般には先頭itemが見出しかどうかを`start_row`で判定して除外する必要がある。
- `queries`は配列なので、`[{"op":"group_by","column":"A"}, {"op":"group_by","column":"C"}]`のように複数クエリを1回の呼び出しにまとめられる（列を変えて範囲確認したいときに個別呼び出しを繰り返さなくてよい）。
- `--sheet`必須（省略時・`.xls`拡張子ではエラー）。未対応のop名もエラー＋終了コード1（対応opの一覧をメッセージに含む）。
- excel-editスキルで`insert_row_group`を使う際の`anchor`（どの値の前/後に挿入するか）の確認にもそのまま使える。

### `list_images`（画像一覧）

```bash
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --query-json '[{"op": "list_images"}]'
```
出力（`result_path`内、`query_results`キー）:
```json
"query_results": [
  {"op": "list_images",
   "items": [{"image_index": 0, "anchor_type": "absolute",
              "left_cm": 1.0, "top_cm": 1.0, "width_cm": 5.0, "height_cm": 3.0}]}
]
```
`image_index`はそのシート内での画像の0始まり通し番号（追加順）で、excel-editの
`set_image_position`の`image_index`と完全に一致する。`anchor_type`が`"absolute"`
以外（テンプレートに元から入っていた、セルに追従する配置の画像）の場合は
`left_cm`等が`null`になる（`set_image_position`で一度動かすと絶対座標に
置き換わり、以降は座標が取得できるようになる）。

### `list_charts`（グラフ一覧）

グラフの存在・タイトル・種類・位置を、画像化せず一覧確認できる。

```bash
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --query-json '[{"op": "list_charts"}]'
```
出力（`result_path`内、`query_results`キー）:
```json
"query_results": [
  {"op": "list_charts",
   "items": [{"chart_index": 0, "type": "line", "title": "月別収支推移", "anchor": "F1"}]}
]
```
`chart_index`はそのシート内でのグラフの0始まり通し番号で、excel-editの
`update_chart`/`delete_chart`の`chart_index`と完全に一致する。`type`は
`line`/`bar`/`pie`/`scatter`のいずれか（対応外の種類はopenpyxlのクラス名を
そのまま返す）。`title`は未設定なら`null`。グラフが実在するか・タイトルが
意図通りかを確認したいだけなら、excel-renderで画像化するよりこちらが速い。

### `print_area`（印刷範囲のみ）

指定した1シートの印刷範囲だけを軽量に取得したいとき（`--sheet`省略時のシート一覧モードでも`print_area`は返るが、全シート分の`max_row`/`max_column`や`rows`を含む重い応答を避けたい場合に使う）。

```bash
python read_excel.py "C:\Users\me\book.xlsx" --sheet Sheet1 --query-json '[{"op": "print_area"}]'
```
出力（`result_path`内、`query_results`キー）:
```json
"query_results": [
  {"op": "print_area", "value": "'Sheet1'!$A$1:$H$45"}
]
```
`value`は未設定なら`null`。複数範囲が設定されているシートはカンマ区切りで連結された文字列になる（シート一覧モードの`print_area`と同じ形式）。`.xls`はそもそも`--query-json`非対応。

## エッジケース

ファイル不在／拡張子がxlsx・xlsm・xls以外／シート未検出／破損ファイルはエラー＋終了コード1。`.xls`はstyle情報・`--query-json`・`--style`非対応（`--query-json`・`--style`は指定時のみエラー、style情報・印刷範囲はそもそも`.xlsx`/`.xlsm`限定で自動的に付かない）。`--columns`は`--query-json`・`--style`と異なり`.xls`でも使える（`.xls`限定のエラーはない）。指定列が総列数を超える場合はエラー文言に総列数を含めて終了コード1（例「指定列が範囲外です: 'Z'（このシートの列数: 10）」）。

## 見た目の確認について

罫線・書式・グラフ・レイアウトなど、セル値・style情報だけでは判断できない見た目の確認は、このスキルではなくexcel-renderスキル（`render_excel.py`＋`analyze_image`）で画像として確認すること。
