# officecli batch: `--props` 単独指定で「Properties specified without --prop flag」エラー

- **区分**: 問題点
- **検知日時**: 2026-08-04 01:00:30
- **対象ログファイル**: data/logs/app_20260804_00_4.log

## 経緯

execute_python_code ツール（サブエージェント経由）で officecli のバッチ操作を実行。シート追加時に `--type sheet` と `--props` を引数として個別指定したが、officecli が `--prop` フラグでの指定を要求しエラーとなった。

## ログ引用

```
2026-08-04 01:00:30,711 WARNING src.subagent: subagent tool=execute_python_code args={'code': '...cli("add", file_path, "/月間予定", "--type", "sheet", "--props", \'{"name":"週間詳細","position":"after"}\')...'} -> '[終了コード] 1\n[標準出力]\nCreated: E:\\user_b\\テスト\\annual_schedule.xlsx\nRenamed sheet to 月間予定\nERROR on add E:\\user_b\\テスト\\annual_schedule.xlsx /月間予定 --type sheet...: WARNING: Properties specified without --prop flag.\n[標準エラー]\nTraceback (most recent call last):\n  File "...tmphf573hlz.py", line 161, in <module>\n    cli("add", file_path, "/月間予定", "--type", "sheet", "--props", \'{"name":"週間詳細","position":"after"}\')\n  File "...tmphf573hlz.py", line 146, in cli\n    raise RuntimeError(f"officecli failed: {result.stderr}")\nRuntimeError: officecli failed: WARNING: Properties specified without --prop flag.'
```

## 推定原因

officecli の `add` コマンドが `--props` (複数形) を受け付けない。`--prop` (単数形) を繰り返す形式、または `--prop` 単独での指定を要求している。LLM が生成したコードで `--props` を使用しているのが原因。

## 追記（2026-08-04 05:54）

同一事象が再発。officecli の `batch` コマンド経由でも同様のエラーが発生し、最終的に `--prop` を使ったバッチJSONファイル経由で回避された。

```
2026-08-04 05:54:39,645 WARNING src.subagent: subagent tool=execute_python_code args={'code': '...cli("add", file_path, "/月間予定", "--type", "sheet", "--props", \'{"name":"週間詳細","position":"after"}\')...'} -> '[終了コード] 1\n[標準エラー]\n...RuntimeError: officecli failed: WARNING: Properties specified without --prop flag.'
```

## ユーザー回答
