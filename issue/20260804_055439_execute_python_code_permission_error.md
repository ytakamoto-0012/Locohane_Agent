# execute_python_code: Excel ファイル削除時に PermissionError [WinError 32]

- **区分**: 問題点
- **検知日時**: 2026-08-04 05:54:39
- **対象ログファイル**: data/logs/app_20260804_00_4.log

## 経緯

execute_python_code ツール（サブエージェント経由）で `os.remove(file_path)` を実行し、既存の Excel ファイルを削除しようとしたが、ファイルが別のプロセス（Excel 等）によってロックされていたため `PermissionError` が発生した。

## ログ引用

```
2026-08-04 05:54:39,645 WARNING src.subagent: subagent tool=execute_python_code args={'code': '...if os.path.exists(file_path):\n    os.remove(file_path)\n...'} -> '[終了コード] 1\n[標準エラー]\nTraceback (most recent call last):\n  File "E:\\user_b\\テスト\\_tmp_8f37ed67-1f68-47f2-91c5-9ec9e773d970\\tmpt0sgq02y.py", line 151, in <module>\n    os.remove(file_path)\n  File "E:\\user_b\\テスト\\_tmp_8f37ed67-1f68-47f2-91c5-9ec9e773d970\\tmpt0sgq02y.py", line 44, in _fn\n    return _orig(_path, *_args, **_kwargs)\n           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n  File "E:\\user_b\\テスト\\_tmp_8f37ed67-1f68-47f2-91c5-9ec9e773d970\\tmpt0sgq02y.py", line 44, in _fn\n    return _orig(_path, *_args, **_kwargs)\n           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\nPermissionError: [WinError 32] プロセスはファイルにアクセスできません。別のプロセスが使用中です。: \'E:\\\\user_b\\\\テスト\\\\annual_schedule.xlsx\''
```

## エラー原文

```
PermissionError: [WinError 32] プロセスはファイルにアクセスできません。別のプロセスが使用中です。: 'E:\\user_b\\テスト\\annual_schedule.xlsx'
```

## 推定原因

ユーザーが annual_schedule.xlsx を Excel で開いている状態で、execute_python_code から同一ファイルの削除を試みた。Windows では Excel が開いているファイルの削除を許可しないため、WinError 32 が発生した。

回避策として、サブエージェントは既存ファイルを削除せずにそのまま使い続ける形にフォールバックした（次の LLM 応答で「既存ファイルをそのまま使う形で進める」と判断）。

## 追記（2026-08-04 05:54）

同一事象が再発（ファイルロック解除後にもう1度削除を試みたが、まだロックが残っていた）。

```
2026-08-04 05:54:39,645 WARNING src.subagent: subagent tool=execute_python_code args={'code': '...if os.path.exists(file_path):\n    os.remove(file_path)\n...'} -> '[終了コード] 1\n[標準エラー]\n...PermissionError: [WinError 32] プロセスはファイルにアクセスできません。別のプロセスが使用中です。'
```

## ユーザー回答
