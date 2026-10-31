# excel-editスキルの`--new`で作った`.xlsm`はマクロ有効ブックにならず、後続のexcel-vba-editがExcelで開けない

- **区分**: バグ → SKILL.md修正済み（スクリプト本体は未修正、下記参照）
- **検知日時**: 2026-08-22 20:20:23
- **対象ログファイル**: data/logs/app_20260822_195744.log

## 経緯

excel-vba マクロブック作成タスクで、サブエージェントは以下の順で処理した。

1. `excel-edit`スキルの`edit_excel.py --new --overwrite` で
   `キャッシュフロー計算表.xlsm` を新規作成（シート・書式・データを設定）。
2. その後 `excel-vba-edit`スキルの`edit_vba.py`（`--new`なし、既存編集）で
   同じファイルにVBAモジュールを追加しようとした。

手順2で Excel COM が `Workbooks.Open` に失敗した。

## ログ引用

```
2026-08-22 20:20:21,136 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-vba-edit', 'script_filename': 'edit_vba.py', 'script_args': ['E:\\user_b\\vba-test\\キャッシュフロー計算表.xlsm', '--ops-file', 'C:\\DT_Python\\Locohane\\data\\path_memory\\vba_ops.json']} -> [終了コード] 1
```

## エラー原文

```
VBAの編集に失敗しました: (-2147352567, '例外が発生しました。', (0, 'Microsoft Excel', "Excel でファイル 'キャッシュフロー計算表.xlsm' を開くことができません。ファイル形式またはファイル拡張子が正しくありません。ファイルが破損しておらず、ファイル拡張子とファイル形式が一致していることを確認してください。", 'xlmain11.chm', 0, -2146827284), None)
Traceback (most recent call last):
  File "C:\DT_Python\Locohane\skills\excel-vba-edit\scripts\edit_vba.py", line 95, in _edit_vba
    workbook = excel.Workbooks.Open(str(path), UpdateLinks=0, IgnoreReadOnlyRecommended=True)
pywintypes.com_error: (-2147352567, ...)
```

続いてサブエージェントが`dispatch_agent`で調査した結果（20:20:34）も同じ結論に
達している：「`excel-vba-read`スキル（oletoolsで直接バイト列から抽出）では
開けたが `modules_count: 0`（VBAなし）、`edit_vba.py`はExcel COMでファイルを
開く必要があるためエラーになる。ファイルが壊れている可能性が高い」。

## 推定原因（特定済み・現物確認済み）

`skills/excel-edit/scripts/edit_excel.py` の`--new`処理（84-89行目）は
拡張子に関わらず`openpyxl.Workbook()`（プレーンなxlsxワークブック、
`vba_archive`なし）を生成し、`wb.save(str(output_path))`で保存する
（117行目）。openpyxlは`vba_archive`が無いworkbookを保存する際、
拡張子が`.xlsm`であってもマクロ有効コンテナ（`vbaProject.bin`・
`[Content_Types].xml`の`macroEnabled`宣言）を一切付与しない。

実際に生成された `キャッシュフロー計算表.xlsm` を確認したところ、
`xl/vbaProject.bin` が存在せず、`[Content_Types].xml`にも
`macroEnabled`の記述が無かった（中身は事実上のxlsx）。Excelはファイル
拡張子と内部フォーマットの不一致を検出して開くこと自体を拒否する
（今回のCOMエラーメッセージと一致）。

一方 `skills/excel-vba-edit/scripts/edit_vba.py` の`--new`（88-116行目）は
`excel.Workbooks.Add()` → `SaveAs(..., FileFormat=xlOpenXMLWorkbookMacroEnabled)`
と実際にExcel本体を経由して保存するため、こちらは正しくマクロ有効な
`.xlsm`を作成できる。

つまり「表データ・書式（excel-edit）とVBAマクロ（excel-vba-edit）の両方を
持つ新規`.xlsm`を作る」タスクでは、**excel-vba-editの`--new`を先に実行して
真のマクロ有効コンテナを作り、その後excel-editを`--new`なしで使って
シート・データを追記する**という順序が必須だが、この制約がどちらの
SKILL.mdにも明記されておらず、サブエージェントは自然な発想（データ構造から
先に作る）で逆順に実行し、20分以上（20:06〜20:20）試行錯誤の末に
「ファイルが壊れている」という誤った結論に達して調査を打ち切っていた。

## 対応

- `skills/excel-edit/SKILL.md`・`skills/excel-vba-edit/SKILL.md` の両方に
  相互参照の警告を追記（「excel-vba-editの`--new`を先に実行してから
  excel-editを`--new`なしで使う」順序を明記、逆順だとマクロ無効ファイルに
  なりExcelで開けなくなる旨も記載）。2026-08-22実施。
- スクリプト本体（`edit_excel.py`）側の恒久対応は見送った。理由:
  openpyxl単体では有効な`vbaProject.bin`を新規生成できず、`--new`時に
  `.xlsm`を指定されたら`edit_vba.py --new`相当（Excel COM経由）に処理を
  委譲する設計変更が必要で影響範囲が大きい。まずはドキュメントでの
  順序誘導により同種の逆順実行を防止し、再発するようであれば
  スクリプト側の作り分け（例:`--new`時に`.xlsm`ならエラーにして
  `edit_vba.py --new`の使用を促す）を検討する。

## 追記（2026-08-22 20:35）— 正しい順序に切り替えた後の新たなつまずき

SKILL.md修正後、サブエージェントは実際に順序を入れ替え、
`excel-vba-edit`の`--new`を先に実行するようになった（20:30:49）。
その過程で2件の新規事象を観測した（いずれも本件の対症療法の副作用に近く、
別バグというよりドキュメント不足の連鎖）。

1. `--ops-json`/`--ops-file`を付けずに`--new --overwrite`のみで呼び出し、
   argparseの必須排他グループ違反で終了コード2（使用法エラー）。
   ```
   edit_vba.py: error: one of the arguments --ops-json --ops-file is required
   ```
   スクリプトの仕様通り（ops無しでは新規ブックの中身を何も指定できない
   ため妥当な制約）で、バグではない。

2. その後、`add_module`等を含む正しい呼び出しで`.xlsm`の新規作成に成功した
   ものの、続けてexcel-editスキルで`rename_sheet`を3回（Sheet1→月別収支計算、
   Sheet2→取引明細表、Sheet3→長期キャッシュフロー）呼び出し、2回目で
   失敗した。
   ```
   ops[1]（op='rename_sheet'）の適用に失敗しました: シートが見つかりません: Sheet2（存在するシート: ['月別収支計算']）
   ```
   **原因**: `excel-vba-edit`の`--new`（`Workbooks.Add()`）が作る新規ブックは
   既定で **「Sheet1」1枚のみ**であり、旧来のExcelデフォルトだった
   「Sheet1〜Sheet3の3枚」ではない。サブエージェントが後者を前提に
   opsを組んだため存在しないシート名でエラーになった。
   `skills/excel-vba-edit/SKILL.md`に「`--new`直後はSheet1 1枚のみ」と
   明記して対応（2026-08-22実施）。

   なお、このエラー発生時に副次的に以下のZipFile例外がstderrへ出力されて
   いた（GC時の後始末失敗で、終了コード・エラーメッセージ自体には影響しない
   ノイズと判断。実害未確認のため今回は未対応）。
   ```
   Exception ignored in: <function ZipFile.__del__ at 0x...>
   Traceback (most recent call last):
     File "...\zipfile.py", line 1894, in __del__
       self.close()
     File "...\zipfile.py", line 1911, in close
       self.fp.seek(self.start_dir)
   ValueError: I/O operation on closed file.
   ```

## ユーザー回答

ここにはユーザーの回答が記述される
