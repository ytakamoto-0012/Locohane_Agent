# run_macroでButtons.Add/MsgBoxを含むマクロを実行すると300秒タイムアウト→Excelプロセス残留で以後の編集が読み取り専用エラーになる

- **区分**: バグ → SKILL.md修正済み
- **検知日時**: 2026-08-22 21:23:02〜21:28:02
- **対象ログファイル**: data/logs/app_20260822_203542.log

## 経緯

excel-vbaマクロブック作成タスクで、`excel-vba-edit`スキルの`edit_vba.py`を
`run_macro`を含むopsで呼び出したところ、`run_script`のスクリプトタイムアウト
（既定300秒）に達して失敗した。

```
2026-08-22 21:23:02,063 INFO src.tools: run_script: excel-vba-edit edit_vba.py cwd=E:\user_b\vba-test
2026-08-22 21:28:02,078 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-vba-edit', 'script_filename': 'edit_vba.py', 'script_args': ['E:\\user_b\\vba-test\\収支計算表.xlsm', '--ops-file', 'E:\\user_b\\vba-test\\_ops_vba.json']} -> エラー: スクリプトが 300 秒でタイムアウトしました。
```

直後のLLMの推論（21:28:05）で原因の見立てが示されている：

```
VBAマクロの実行がタイムアウトしました。おそらく`CreateButtons`マクロ内で
`SHEET_MONTHLY`や`SHEET_LONGTERM`という定数が定義されていない、または
`Buttons.Add`でエラーが発生している可能性があります。
```

## 推定原因

`excel-vba-edit`は`run_macro`を含む呼び出しのみマクロ実行許可設定
（`msoAutomationSecurityLow`）でExcelを開く（SKILL.md 66行目）。この状態で
実行されたマクロ（`CreateButtons`、シートにフォームコントロールのボタンを
配置する処理）が`Buttons.Add`のような図形・UIオブジェクト操作を行い、
対話セッションの無い自動化実行のため誰も応答できないダイアログや処理待ちで
ハングした可能性が高い。`MsgBox`/`InputBox`を含む場合も同様の理由で
ハングする（今回のタスクで以前生成されたVBAコードには
`MsgBox "CSVインポート完了！..."`のような呼び出しが複数含まれていた）。

さらに、この300秒タイムアウトでスクリプトプロセスが強制終了された後も
Excel本体（EXCEL.EXE）はファイルを開いたまま残留し、その後の同一ファイルへの
`edit_vba.py`/`edit_excel.py`呼び出しが以下のエラーで失敗し続ける状態に
なる（同日21:16:17に既に同種の事象を観測済み、[issue/20260822_202048_foreign_tmp_dir_guard_false_positive_on_file.md](20260822_202048_foreign_tmp_dir_guard_false_positive_on_file.md)とは別原因）：

```
'収支計算表.xlsm' は読み取り専用のため、上書き保存できません。
```

`excel-vba-edit`のSKILL.mdには「強制終了時はEXCEL.EXE残留の可能性あり
（タスクマネージャーでの手動終了が必要）」という記述は既にあったが、
`run_macro`のブロッキングUIが原因でこの状態に陥りうることは明記されて
いなかった。

## 対応（修正済み）

`skills/excel-vba-edit/SKILL.md`の`run_macro`opの説明に以下を追記した：
- `MsgBox`/`InputBox`等のブロッキングUIをマクロに含めない
- `Buttons.Add`等の図形・フォームコントロール操作も同様の理由で避ける
  （ボタン配置が必要ならコードだけ書いてユーザーに実行を委ねる）
- 進捗表示は`Debug.Print`か戻り値で行う
- タイムアウト時はExcelプロセス残留により以後の編集が読み取り専用エラーに
  なることを明記

スクリプト側（`edit_vba.py`）でのタイムアウト検出・自動リカバリ（例:
`run_macro`実行前に`Application.DisplayAlerts=False`だけでなく
`Application.ScreenUpdating=False`や`Buttons`系操作の事前検知で拒否する等）
は見送った。理由: `run_macro`は任意のユーザー定義マクロを実行する機能で
あり、ブロッキングUIを機械的に検出するのは困難（`MsgBox`という文字列が
コード中にあっても、実行パス次第で呼ばれない場合もある）。まずは
ドキュメントでの回避誘導を優先し、再発するようであれば
`run_macro`実行に個別のより短いタイムアウトを設ける等の対策を検討する。

## 追記（2026-08-22 21:33）— 同一セッション内で2回目のタイムアウトが再発

SKILL.md修正後も、同一の対話セッション（既にSKILL.mdを読み込み済みで
コンテキストに残っている）で2回目の300秒タイムアウトが発生した。

```
2026-08-22 21:33:09,878 WARNING src.subagent: subagent tool=run_script args={'script_filename': 'edit_vba.py', 'skill_name': 'excel-vba-edit', 'script_args': ['E:\\user_b\\vba-test\\収支計算表.xlsm', '--ops-file', 'E:\\user_b\\vba-test\\_ops_vba.json']} -> エラー: スクリプトが 300 秒でタイムアウトしました。
```

直後にサブエージェントが`modMain`へ`insert_code`で追加しようとした
`CreateButtons`のコードを確認したところ、`ws.Buttons.Add(...)`を4回、
末尾に`MsgBox "ボタンが配置されました。", vbInformation`を含んでおり、
今回のSKILL.md追記で名指しで警告した2パターン（Buttons.Add・MsgBox）を
両方含んでいた。修正はSKILL.mdへの追記のみだったため、**既にこの
SKILL.mdを読み込み済みの実行中セッションには反映されない**（次にこの
スキルを新規に読み込むセッション・サブエージェントから効果が出る）。

Excelプロセスが今回も開いたまま残留している可能性が高い。ユーザーへの
確認・タスクマネージャーでのEXCEL.EXE手動終了が必要な可能性がある旨を
伝えた。

## 追記（2026-08-22 21:34）— 予測通りExcelプロセス残留を確認、実害が具体化

サブエージェント自身が`tasklist /FI "IMAGENAME eq EXCEL.EXE"`で確認した
結果、**EXCEL.EXEが2プロセス（PID: 26092, 29580）残留**していることが
判明した。これにより後続の`edit_excel.py`（罫線・書式適用）が
`Permission denied`で保存不可になり、実害（タスク進行不能）が確定した。

```
2026-08-22 21:34:28,256 WARNING src.tools: tool_result: name=dispatch_agent content='...
1. 作業1（VBAボタン配置）- run_macro部分: マクロ実行時、「マクロを実行できません。このブックでマクロが使用できないか、またはすべてのマクロが無効になっています」というExcelエラー。
2. 作業2（罫線・書式の適用）: Excelプロセス（PID: 26092, 29580）が収支計算表.xlsmファイルをロックしているため、Permission deniedで保存処理が失敗。
3. 作業3（グラフ作成）: 作業2が失敗したため未着手。
'
```

`run_macro`自体は今回タイムアウトではなく「マクロを実行できません」
エラーだった点は新情報（既に別プロセスがファイルを開いているため、
新しいCOMセッションが期待するマクロ実行許可設定で開けなかった可能性）。

ユーザーへPID 26092・29580の手動終了（タスクマネージャー、または
`taskkill /PID 26092 /F` 等）を依頼する必要がある旨を伝えた
（保存されていないExcel上の変更が失われる可能性があるため、
こちらから無断では終了しない）。

## 追記（2026-08-22 21:40）— EXCEL.EXE残留の根本メカニズムを特定（未実装）

なぜ`edit_vba.py`側の`try/finally`（`workbook.Close(SaveChanges=False)`→
`excel.Quit()`、70-141行目）があるにもかかわらずEXCEL.EXEが残留するのかを
コードで確認した。

`run_script`側のスクリプトタイムアウト（300秒）は、`edit_vba.py`の
Pythonプロセスを**外部から強制終了**する実装になっている（`run_macro`の
`Application.Run`呼び出しはCOM経由の同期ブロッキング呼び出しであり、
Python側のコード自体はまだ`Application.Run`の1行で停止したままなので、
通常のPython例外としては伝播しない）。プロセスが外部から強制終了される
ため、`finally`節が実行される機会が無く、`workbook.Close()`/`excel.Quit()`
が呼ばれないままEXCEL.EXEだけが取り残される。これが今回2プロセス
（PID 26092, 29580）残留した根本メカニズム。

**恒久対策の方向性（今回は未実装）**: `run_macro`のCOM呼び出しを
別スレッドで実行し、run_script全体のタイムアウトより短い内部タイムアウトで
待機、超過時はそのスレッドが保持するExcelプロセスのPIDを直接
`TerminateProcess`する、という設計であれば、ハング時でも比較的短時間で
検知・自己クリーンアップできる可能性がある。ただし今回は見送った。理由:
(1) COMオブジェクトはアパートメントスレッドモデルのため別スレッドからの
呼び出しにはマーシャリングが必要でCOM設計の変更が大掛かりになる、
(2) マクロ実行の途中でプロセスを強制終了すると、ファイルへの書き込みが
中途半端な状態でExcelプロセスごと消える可能性があり、`.xlsm`破損の
新たなリスクを生みかねない、(3) ユーザーの実タスクが進行中のこの
タイミングで検証不十分な変更を書き込み系スクリプトに入れるのは
リスクが高いと判断。まずはSKILL.mdでの回避誘導（追記1・2）を優先し、
発生頻度・実害の大きさを見ながら改めて検討する。

## 追記（2026-08-22 21:46〜21:53）— ロック未解消のまま副次的な迷走が継続

ユーザーへの対応依頼後もEXCEL.EXEロックが解消されないまま
（21:43:32に再度`Permission denied`を確認）、サブエージェントは
`excel-edit`スキル経由の保存を諦め、`execute_python_code`で`openpyxl`を
直接叩いてグラフ作成を試みる方向へ迂回した。この過程で`openpyxl`の
チャート系列色設定API（`openpyxl.chart.series.Point`等の誤った
インポート）を繰り返し勘違いし、6回連続失敗→システム警告→
LLM応答ループ検知（2回目）という展開になった（21:46:10, 21:52:22）。

これは`openpyxl`ライブラリAPIに関するLLMの知識不足であり、
`excel-edit`スキル自体は`add_chart`/`update_chart`の`theme`オプションで
系列色を扱える（本件のバグではない）。ただし、そもそも`excel-edit`
経由で保存できていればこの迂回自体が発生しなかった可能性が高く、
根本原因はやはり本issueのEXCEL.EXEロック未解消。新規issueは起票せず
本issueへの追記に留める。

## 追記（2026-08-22 23:20）— 3回目の実機確認を受けコード側に静的チェックを実装

同一セッション内で3回目（`DeleteCharts`マクロ）も`run_macro`が300秒で
タイムアウトし、直後にサブエージェント自身が`tasklist`でEXCEL.EXE残留・
ファイルサイズを確認する行動を取っていた（自己診断の質は向上している）。

```
2026-08-22 23:12:45,690 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-vba-edit', 'script_filename': 'edit_vba.py', ...} （run_macro DeleteCharts）
2026-08-22 23:17:53,644 WARNING ... -> 300秒タイムアウト（推定）
```

SKILL.mdでの回避誘導（追記1）は同一セッション内で3回とも効果が無かった
（既に読み込み済みのSKILL.mdはセッション途中で更新されないため）。
MsgBox付きマクロをLLMが繰り返し生成する傾向自体は今後も発生しうると
判断し、**ドキュメントのみでの対応から、コード側の静的チェックへ格上げ**
した。

### 対応（実装済み）

`skills/excel-vba-edit/scripts/_vba_ops.py`の`op_run_macro()`に、
`Application.Run`を呼ぶ前の事前チェックを追加:

- `_find_procedure_code()`: `run_macro`の`name`（`"Module.Proc"`または
  `"Proc"`単体）から、COMの`CodeModule.ProcStartLine`/`ProcCountLines`/
  `Lines`（`replace_procedure`が既に使っている仕組みと同じ）で対象
  プロシージャの実ソースを取得する。モジュール・プロシージャが
  特定できない場合はNoneを返し、チェック自体をスキップする
  （フェイルオープン。lookup失敗で正当な実行までブロックしないため）。
- `_check_blocking_ui()`: 取得したコードに`MsgBox`/`InputBox`が含まれて
  いれば`ValueError`で実行前に拒否する。
- これにより、ハングそのものが発生しなくなった（`Application.Run`を
  スキップするだけでExcel自体は正常終了処理に入る）。

`Buttons.Add`等の図形操作は今回のチェック対象に含めていない（実際に
ハングを起こしたか確証が無く、正当な用途を誤ってブロックするリスクの
方が大きいと判断）。SKILL.mdでの回避誘導（追記1）は引き続き残す。

`skills/excel-vba-edit/SKILL.md`の該当箇所を、静的チェックで機械的に
ブロックされる旨・エラーメッセージの内容に更新。

`tests/test_excel_vba_edit_run_macro_blocking_ui.py`を新規作成し、
COMオブジェクトをモックして以下を検証:
- MsgBox/InputBoxを含むプロシージャは`_check_blocking_ui`でエラー
- クリーンなコードはエラーにならない
- `_find_procedure_code`がdotted名／bare名の両方で正しくプロシージャを
  特定できる、未知のモジュール/プロシージャではNoneを返す
- `op_run_macro`はMsgBox検出時に`Application.Run`を一切呼ばずに
  拒否する（`run_called_with is None`で確認）
- クリーンなマクロは`Application.Run`まで到達する
- lookup失敗時（モジュールが1つも無い等）はチェックを諦めて
  `Application.Run`まで到達する（フェイルオープンの確認）

検証: `pytest tests/` 380件全通過。

なお、EXCEL.EXEロック自体（既に残留している分）は今回の修正では
解消されない。引き続きユーザーによる手動終了が必要。

## 追記（2026-08-22）— PID記録によるLLM自己回復手段を実装

ユーザーから「excel-vbaでデバッグをする際に、LLMがプロセスを落とせるように
できないか」という要望があった。23:20時点の対応（`run_macro`前の静的チェック）
はMsgBox/InputBoxによるハングの**発生自体**を防ぐが、それ以外の原因（今回の
`DeleteCharts`2回目実行のように、前段のハングで残留したEXCEL.EXEのファイル
ロック競合など）でタイムアウトした場合、Excelプロセスの後始末は依然ユーザーの
手動終了に依存していた。実機ログ（`data/logs/app_20260822_230818.log`）でも、
サブエージェントが`execute_python_code`経由の`tasklist`でEXCEL.EXE残留
（PID: 26092, 29580, 26868）を自ら確認していながら、`taskkill`を打つ許可・
手順がSKILL.mdに無かったためユーザーへ手動終了を依頼するだけに留まっていた。

ユーザーに実装レベルを確認したところ、「PID記録＋SKILL.md手順」
（軽量なドキュメントのみ対応と、コード側で自動検知・自動終了する恒久対策の
中間案）を選択。

### 対応（実装済み）

`skills/office_shared/excel_common.py`に`record_excel_pid`/`release_excel_pid`/
`excel_pid_file`を追加。`edit_vba.py`の`_edit_vba`が`win32.DispatchEx`で
Excelを起動した直後に`Application.Hwnd`→`GetWindowThreadProcessId`でPIDを
取得し、対象ファイルと同じフォルダの`<ファイル名>.vba_pid`（1行1PID）へ追記する。
正常終了時は`finally`節で自動的に取り除く。`run_macro`ハングでrun_scriptの
外部タイムアウトによりPythonプロセスが強制終了された場合はこの後始末が
実行されないため、PIDファイルだけが残る。

`skills/excel-vba-edit/SKILL.md`の`run_macro`説明に、タイムアウト時は
このPIDファイルを読んで記載PIDに対し`taskkill /PID <pid> /F`をLLM自身が
実行してよい旨を明記した。対象は`edit_vba.py`が専用に起動した
`Visible=False`の非表示プロセスのみで、`tasklist`で見える無関係な
EXCEL.EXEまでは終了しないよう明記した（ユーザーが対話的に使っている
Excelを誤って巻き込むリスクを避けるため）。

`tests/test_excel_vba_edit_pid_recovery.py`を新規作成し、PID記録・重複排除・
複数PID蓄積・Hwnd取得失敗時のフェイルオープン・release時の該当PIDのみ除去・
空になった場合のファイル削除を検証。`pytest tests/` 389件全通過。

なお、コード側での自動検知・自動終了（run_macro呼び出しを内部タイムアウト付き
監視スレッドで保護し、超過時に自動でTerminateProcessする案）は今回も見送った。
理由: PID記録の仕組みだけでも「ユーザーの手動終了待ち」という最大のボトル
ネックは解消でき、まずはこの効果を見てから判断するのが妥当と判断したため。

## ユーザー回答

「PID記録+SKILL.md手順（推奨）」を選択（2026-08-22）。
