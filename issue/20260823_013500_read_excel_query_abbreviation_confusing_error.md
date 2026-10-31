# read_excel.py: 存在しない`--query`がargparseの省略補完で`--query-json`に化け、紛らわしいエラーで12回連続失敗

- **区分**: バグ → 修正済み
- **検知日時**: 2026-08-23 01:35:01
- **対象ログファイル**: data/logs/app_20260823_002537.log

## 経緯

excel-vbaマクロブック作成タスク（再起動後）で、`excel-read`スキルの
`read_excel.py`を`--query rows --sheet 月別収支`のように呼び出したところ、
以下のエラーで終了コード1になった。

```
--query-jsonのJSON解析に失敗しました: Expecting value: line 1 column 1 (char 0)
```

`read_excel.py`には`--query`という引数は存在せず、`--query-json`のみが
定義されている。Pythonの`argparse`は既定で**一意に定まる省略形を自動補完**
する（`allow_abbrev`が既定`True`）ため、`--query`は唯一該当する
`--query-json`として解釈され、続く`'rows'`（本来は"rows"という文字列
クエリのつもりだったとみられる）が`--query-json`の値として渡り、JSONとして
パースできず上記のエラーになっていた。

エラーメッセージが「引数`--query`は存在しません」ではなく
「`--query-json`のJSON解析に失敗」という形で出るため、LLMは`--query`が
誤りだと気づけず、`--sheet`の位置を変える・`--query-json`のop名を変える
（`"rows"`→`{"op":"rows"}`→`{"op":"read_vba",...}`など存在しないopまで
試す）等、的外れな修正を12回連続で試みて失敗し続けた（01:35:01〜01:35:17）。
最終的には`--query`系を諦め、`--sheet`+`--offset`+`--limit`という
別の（正しい）呼び出し方に切り替えて解決した（01:52:52、成功）。

## ログ引用

```
2026-08-23 01:35:01,496 WARNING src.subagent: subagent tool=run_script args={'script_filename': 'read_excel.py', 'skill_name': 'excel-read', 'script_args': ['E:\\user_b\\vba-test\\収支計算表.xlsm', '--query', 'rows', '--sheet', '月別収支']} -> [終了コード] 1
2026-08-23 01:35:01,496 DEBUG src.subagent: ... -> '[終了コード] 1\n[標準エラー]\n--query-jsonのJSON解析に失敗しました: Expecting value: line 1 column 1 (char 0)'
```
（同一パターンで`--sheet`位置違い・`{"op":"rows"}`・存在しない`{"op":"read_vba",...}`など計12回、01:35:01〜01:35:17の間に連続発生）

## エラー原文

```
--query-jsonのJSON解析に失敗しました: Expecting value: line 1 column 1 (char 0)
```

## 推定原因

`read_excel.py`（`skills/excel-read/scripts/read_excel.py`）の
`argparse.ArgumentParser()`が`allow_abbrev`の既定値（`True`）のままだった
ため、未定義の`--query`が一意に一致する`--query-json`へ暗黙補完され、
本来出るべき「unrecognized arguments」エラーの代わりに紛らわしい
JSON解析エラーが出ていた。

## 対応（修正済み）

`skills/excel-read/scripts/read_excel.py`の`ArgumentParser()`に
`allow_abbrev=False`を追加。他の定義済みオプション（`--sheet`/`--offset`/
`--limit`/`--data-only`/`--query-json`）同士に接頭辞衝突は無く、この
スクリプト単体の`ArgumentParser`インスタンスのみに影響するため
他スキルへの副作用は無い。

修正後、`--query rows --sheet X`は以下の明確なエラーになることを確認した：
```
read_excel.py: error: unrecognized arguments: --query rows
```

`pytest tests/`389件全通過（既存テストへの影響なし）。

他スキルスクリプトについても同種の未定義オプション補完リスクがないか
包括的な監査はしていない（今回は実際に踏んだ`read_excel.py`のみ対応）。

## ユーザー回答

ここにはユーザーの回答が記述される
