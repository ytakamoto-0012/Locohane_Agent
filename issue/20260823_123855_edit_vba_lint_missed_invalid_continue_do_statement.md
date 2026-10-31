# LLMが生成したVBAコードの`Continue Do`（VBAに存在しない構文）がlintをすり抜けて書き込まれていた

- **区分**: バグ → 修正済み
- **検知日時**: 2026-08-23 12:38:55
- **対象ログファイル**: data/logs/app_20260823_123006.log

## 経緯

VBAマクロ修正タスクで、`CSVImporter`モジュールの`ImportCSV`プロシージャに
`Continue Do`という文が含まれていた。これはVB.NET等には存在するがVBAには
存在しない構文で、`_lint_vba_syntax`のブロック対応チェック（Sub/End Sub、
Do/Loop等の対応関係のみを見る簡易lint）はキーワード単位の妥当性を検証しない
ため、そのまま`set_code`で書き込まれてしまった。別セッションの
analyze-docsサブエージェントが原因を特定し、`GoTo`+ラベルへの置換計画を
作成していた。

なお、このコードが生成された時点（同一セッション前半）のLLM自身の内部推論
ログには「`Continue Do`はVBA7（Office 2010+）で有効」という誤った自己正当化
があり、モデルが他言語の構文と混同したことが根本原因と見られる。

## ログ引用

```
2026-08-23 12:39:07,856 WARNING src.subagent: subagent tool=Read args={'file_path': '@6'} -> {... "code": "Attribute VB_Name = \"CSVImporter\"\r\nAttribute VB_Name = \"CSVImporter\"\r\nOption Explicit\r\n\r\n' CSVファイルを取り込んで取引明細表に追記する...
2026-08-23 12:39:45,642 WARNING src.tools: tool_result: name=dispatch_agent content='調査結果を基に、実行計画の草案を作成します。\n\n## 1. steps候補\n\n```json\n[\n  {\n    "content": "CSVImporterモジュールのImportCSVプロシージャ内にある「Continue Do」を「GoTo HeaderCheck」に置換し、Do Whileループ直前にHeaderCheck:ラベルを追加する"...
```

## 推定原因

`_lint_vba_syntax`（`skills/excel-vba-edit/scripts/_vba_ops.py`）はブロック
構文（Sub/End Sub、Do/Loop、If/End If等）の対応関係のみを検証しており、
個々のキーワード・文の妥当性は見ていない（実コンパイルチェックはCOM経由では
実現できないため意図的に対象外としている）。`Continue Do`はブロック対応上は
問題なく通過してしまうため、書き込み前に検知できなかった。

## 対応（実装済み・2026-08-23）

低パラメータモデルが他言語との混同でよく生成する既知のハマりどころとして、
`_lint_vba_syntax`に`Continue Do`/`Continue For`/`Continue While`の検出を
追加した（文字列リテラル・コメントは既存の`_iter_logical_lines`によるサニタイズで
除外される）。検出時はGoTo+ラベルへの置換を促すエラーメッセージを返す。
`set_code`/`add_module`/`find_replace`/`replace_procedure`/`insert_code`の
全書き込み経路で`_lint_vba_syntax`を呼んでいるため、いずれの操作でも効く。

テスト: `tests/test_excel_vba_edit_lint_invalid_continue_statement.py`を
新規追加（6件）。Continue Do/For/Whileの検出、文字列リテラル・コメント内は
誤検知しないこと、通常コードでは発火しないことを確認。`pytest tests/`
441件全通過。

なお、今回既に書き込まれてしまった`E:\user_b\vba-test\収支計算表.xlsm`の
`Continue Do`自体は、別セッションが自力で原因調査・修正計画を進めているため、
本監視スキルの範囲（ユーザーファイルの直接操作はしない）としては対応せず
見守っている。

## ユーザー回答

ここにはユーザーの回答が記述される
