# read_excel.pyにグラフ一覧クエリが無く、グラフ存在確認が画像頼みで長時間ループの一因になった

- **区分**: 改善点 → 対応済み
- **検知日時**: 2026-08-23 11:35:40

- **対象ログファイル**: data/logs/app_20260823_104959.log

## 経緯

VBAプロジェクト復旧作業の検証中、verifierがグラフの存在を
excel-renderの画像だけで判別しようとして長時間の推論ループ
（11:29:33のThinkingLoopDetected、直近テキストに「画像2と3には
グラフが写っていない」「もう少し詳しく調べる必要がある」の反復あり）に
陥った。その数分後、workerが
`read_excel.py --query-json '[{"op": "list_charts"}]'`でグラフ一覧を
取得しようとしたが、`未対応のqueryです`で失敗した（対応していたのは
`group_by`/`list_images`のみ）。

## ログ引用

```
2026-08-23 11:35:40,954 DEBUG src.subagent: subagent tool=run_script args={'skill_name': 'excel-read', 'script_filename': 'read_excel.py', 'script_args': ['E:\\user_b\\vba-test\\収支計算表.xlsm', '--sheet', '月別', '--query-json', '[{"op": "list_charts"}]']} -> "[終了コード] 1\n[標準エラー]\n未対応のqueryです: 'list_charts'（対応op: ['group_by', 'list_images']）"
```

## 推定原因

`read_excel.py`の`--query-json`は`group_by`（列の値グルーピング）と
`list_images`（画像一覧）のみ対応しており、グラフの存在・タイトル・種類を
テキスト/JSONで確認する手段が無かった。excel-editの`add_chart`/
`update_chart`/`delete_chart`が既に`chart_index`（0始まり通し番号）を
持つのに対応する読み取り手段が無い非対称な状態だった。そのため、グラフの
有無を確認したいときは常にexcel-renderで画像化しVLMで判定するしかなく、
今回のように画像から判別しづらいケースでLLMが長時間迷走する一因になった。

## 対応（実装済み・2026-08-23）

`skills/excel-read/scripts/read_excel.py`に`list_charts`クエリを追加した。
シート内の各グラフについて`chart_index`（excel-editの`update_chart`/
`delete_chart`と同じ基準）・`type`（`line`/`bar`/`pie`/`scatter`）・
`title`・`anchor`（セル参照文字列）を返す。`skills/excel-read/SKILL.md`に
使用例を追記し、`agents/verifier.md`のExcelチェックリストにも
「画像で判別しづらい場合は先に`list_charts`で確認する」旨を追加した。

テスト: `tests/test_excel_read_list_charts_query.py`を新規追加（3件、
タイトル・種類・位置の取得／グラフ無しシート／タイトル未設定グラフの
`title: null`）。`pytest tests/` 430件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
