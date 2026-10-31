# excel-read: シート指定なしで終了コード1

- **区分**: 問題点
- **検知日時**: 2026-08-13 17:25:00
- **対象ログファイル**: data/logs/app_20260813_162817.log

## 経緯

サブエージェントが `excel-read` スキルの `read_excel.py` をシート名指定なしで実行したが、終了コード1で失敗した。

## ログ引用

```
2026-08-13 17:17:24,921 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-read', 'script_filename': 'read_excel.py', 'script_args': ['E:\\user_b\\テスト\\annual_schedule.xlsx']} -> [終了コード] 1
```

## 推定原因

シート名指定なしで実行した場合のデフォルト動作（全シート読み込み or 既定シート読み込み）でエラーが発生した可能性がある。既存issue（20260812_123300_excel_read_sheet_name_guess_and_rowdimension_attr.md）とは異なる事案（今回はシート指定なし）。

## 追記（2026-08-13 17:25）

- 初回検知

## 追記（2026-08-13 17:30）

- 訂正: 実際のスタックトレースを確認したところ、シート指定の有無は無関係で、
  `register_output_path()` → `path_memory.register()` が書き込みサンドボックス
  ガードにより `PermissionError` を送出しスクリプトがクラッシュしていたことが
  根本原因と判明。詳細は `20260813_173000_path_memory_permission_error_sandbox_guard.md`
  を参照。本issueはその重複として位置付ける。

## ユーザー回答
