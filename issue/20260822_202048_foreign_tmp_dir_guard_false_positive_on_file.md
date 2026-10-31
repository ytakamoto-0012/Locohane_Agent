# _foreign_tmp_dir_error() が `_tmp_` で始まる名前の「ファイル」まで他セッションの一時ディレクトリ誤検知でブロック

- **区分**: バグ → 修正済み
- **検知日時**: 2026-08-22 20:20:48
- **対象ログファイル**: data/logs/app_20260822_195744.log

## 経緯

excel-vba マクロブック作成タスク中、サブエージェントが `execute_python_code`
で作業ディレクトリ（`E:\user_b\vba-test\`）直下に `_tmp_ops.json` という
名前で ops 定義を書き出し（20:10:31）、直後に `run_script` で
`edit_excel.py --ops-file E:\user_b\vba-test\_tmp_ops.json` を実行
（20:10:35）したところ、`_tmp_ops.json` を読み込もうとした箇所で
`PermissionError` が発生しスクリプトが終了コード1でクラッシュした。

## ログ引用

```
2026-08-22 20:10:35,064 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-edit', 'script_filename': 'edit_excel.py', 'script_args': [..., '--ops-file', 'E:\\user_b\\vba-test\\_tmp_ops.json']} -> [終了コード] 1
```

## エラー原文

```
Traceback (most recent call last):
  File "C:\DT_Python\Locohane\skills\excel-edit\scripts\edit_excel.py", line 136, in <module>
    sys.exit(main())
  File "C:\DT_Python\Locohane\skills\excel-edit\scripts\edit_excel.py", line 68, in main
    raw = _load_json_arg(args)
  File "C:\DT_Python\Locohane\skills\excel-edit\scripts\edit_excel.py", line 42, in _load_json_arg
    return data_path.read_text(encoding="utf-8")
  File "...\agent_fs_guard_6w431nmu\sitecustomize.py", line 53, in _guard_open
    _guard_check_foreign_tmp(_file)
  File "...\agent_fs_guard_6w431nmu\sitecustomize.py", line 22, in _guard_check_foreign_tmp
    raise PermissionError(
PermissionError: [一時ディレクトリガード] 他セッションの一時ディレクトリへはアクセスできません: E:\user_b\vba-test\_tmp_ops.json
```

## 推定原因（特定済み）

`src/tools.py` の `_foreign_tmp_dir_error()`（856行目付近）は、
`_resolve_exec_workdir()` が作る `_tmp_<thread_id>`（execute_python_code系の
セッション作業フォルダ、常にディレクトリ）を「他セッションの残留物」として
読み取り拒否するためのガード。判定条件は「作業ディレクトリ直下の最初の階層が
`_tmp_` で始まる名前で、かつ自セッションの名前と一致しない」の2つのみで、
**その階層が実際にディレクトリかどうかを見ていなかった**。

そのため、LLMが自分で作業ディレクトリ直下に `_tmp_ops.json` のような
`_tmp_` で始まる名前の**ファイル**（`_tmp_<thread_id>` ディレクトリとは
無関係）を作成すると、同じ経路で「他セッションの一時ディレクトリ」と
誤判定され読み取りを拒否されていた。

同じ目的の姉妹関数 `_foreign_tmp_dir_names()`（896行目、Glob/Grep の
除外リスト生成用）は既に `entry.is_dir()` 判定を持っており、
`_foreign_tmp_dir_error()` 側だけがこのチェックを欠いた非対称な実装に
なっていた（実装時期・経緯は未確認）。

## 対応（修正済み）

`src/tools.py` の `_foreign_tmp_dir_error()` に `(parent / first).is_dir()`
判定を追加し、ディレクトリの場合のみ拒否するよう修正（`_foreign_tmp_dir_names()`
と同じ条件に統一）。docstringにも経緯を追記。

`tests/test_tools_file_tools_wrappers.py` の `TestForeignTmpDirGuard` に
`test_read_file_named_with_tmp_prefix_is_not_blocked` を追加し、
`_tmp_` で始まる名前のファイルが読み取れることを検証。

検証: `pytest tests/` 352件全通過（修正前は対象ケース未カバーだったため
既存テストへの影響なし、新規テストは修正後に通過を確認）。

## 追記（2026-08-22 20:35）

`edit_excel.py` の別呼び出し（20:11:07, `--ops-file @3`）でも同一エラーが
再発しているが、これは `_tmp_ops.json` を再度指定しようとして
`path_memory` の `@3` 参照解決を試みた別試行であり、根本原因は同一。
今回の修正で解消見込み。

`--new --overwrite` で再実行した際に `シートが見つかりません: Sheet1
（存在するシート: []）`（20:11:07 直後）が発生しているが、これは
本ガード修正後の実際の動作を見て別途要観察（`--new` 時の初期シート構成の
挙動が意図通りか、LLM側の呼び出し順序の問題かは未検証）。

## 追記（2026-08-22 21:00）— 修正は未デプロイのため実行中プロセスでは再発中

修正コミット（`src/tools.py`）は 2026-08-22 20:35:47 に作成されたが、
現在実行中のアプリプロセスは同日 20:35:42（コミットの5秒前）に起動して
おり、**修正前のコードのままメモリに載っている**（Pythonはソース変更を
自動リロードしないため）。そのため、同一の
`PermissionError: [一時ディレクトリガード] 他セッションの一時ディレクトリへは
アクセスできません: E:\user_b\vba-test\_tmp_ops_monthly.json` /
`..._tmp_ops_trans.json` が20:58:57・21:00:17の2回、`edit_excel.py`の
`--ops-file`呼び出しで再発した（いずれもファイル名は違うが同一原因）。

**アプリの再起動が必要**（コード修正自体は完了しており、再起動後は
発生しなくなる見込み）。excel-vbaタスクが進行中のため、ユーザーへの
確認なしにこちらから再起動は行わない。

## ユーザー回答

ここにはユーザーの回答が記述される
