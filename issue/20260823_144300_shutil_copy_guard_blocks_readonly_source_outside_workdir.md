# execute_python_code内のshutil.copy2/copy/move等のサンドボックスガードが、書き込み先だけでなく読み取り専用のコピー元パスまでブロックしてしまう

- **区分**: バグ → 修正済み（2026-08-23）
- **検知日時**: 2026-08-23 14:39:33
- **対象ログファイル**: data/logs/app_20260823_135730.log

## 経緯

pptx-createでの図鑑作成タスク中、サブエージェントが `execute_python_code` で
ユーザーの写真フォルダ（`E:\共有\写真\釣り\*.JPG`、作業ディレクトリ外）から
一時フォルダ内の `images/` へ `shutil.copy2(src_path, dst_path)` でコピーしようと
したところ、コピー先ではなく**コピー元**のパスが作業ディレクトリ外であることを
理由に `PermissionError` で拒否された。

同じファイルを読み取り専用で扱う `analyze_image` ツールは同一パス
（`E:\共有\写真\釣り\DSC_5188.JPG` 等）を問題なく読み込めており
（14:16:09のログで確認）、`open()` の読み取りモードもガード対象外
（`_guard_open` は書き込みモード時のみ `_guard_check` を呼ぶ）。
一方 `shutil.copy2` 等はコピー元・コピー先の両方を `_guard_check` に通しており、
読み取り専用のはずのコピー元にまで書き込みガードが誤って適用されている。

## ログ引用

```
2026-08-23 14:39:33,214 WARNING src.subagent: subagent tool=execute_python_code args={'code': '...shutil.copy2(src_path, dst_path)...'} -> '[終了コード] 1\n[標準エラー]\nTraceback (most recent call last):\n  File "...\\tmp4k0t_nno.py", line 192, in <module>\n    shutil.copy2(src_path, dst_path)\n  File "...\\tmp4k0t_nno.py", line 80, in _fn\n    _guard_check(_src, _name)\n  File "...\\tmp4k0t_nno.py", line 38, in _guard_check\n    raise PermissionError(\nPermissionError: [書き込みサンドボックスガード] 作業ディレクトリ配下以外はcopy2できません: E:\\共有\\写真\\釣り\\DSC_5172.JPG\n作業ディレクトリ（またはセッション専用の一時フォルダ _tmp_<thread_id>）配下のみ書き込み・削除可能です。default_workdir直下など共有フォルダへは直接書き込めません。\n\n\n【システム警告】execute_python_code が直近4回連続で失敗しています。同じコード・引数を少しずつ書き直す対症療法をやめ、根本的に別の書き方・別の手段に切り替えるか、この手段にこだわらず代替アプローチを検討してください。'
```

## エラー原文

```
PermissionError: [書き込みサンドボックスガード] 作業ディレクトリ配下以外はcopy2できません: E:\共有\写真\釣り\DSC_5172.JPG
作業ディレクトリ（またはセッション専用の一時フォルダ _tmp_<thread_id>）配下のみ書き込み・削除可能です。default_workdir直下など共有フォルダへは直接書き込めません。
```

## 推定原因

`src/tools.py:3043` 付近、`for _guard_name in ("rmtree", "move", "copy", "copy2", "copyfile", "copytree"):` で
生成される `_guard_make_shutil`（`src/tools.py:3044-3052`）が原因。

```python
def _fn(_src, *_args, **_kwargs):
    if _name == "rmtree":
        _guard_check(_src, _name)
    else:
        _guard_check(_src, _name)      # ← コピー元にもガードをかけている
        if _args:
            _guard_check(_args[0], _name)  # コピー先
    return _orig(_src, *_args, **_kwargs)
```

`move`（元ファイルを削除する）は元・先とも書き込み対象になるため妥当だが、
`copy`/`copy2`/`copyfile`/`copytree` は**コピー元は読み取り専用**であり、
`open()` の読み取りモードや `analyze_image` と同様に作業ディレクトリ外からの
読み取りを許可すべきところ、`move` と同じ扱いで `_src` にまで書き込みガードを
適用してしまっている。これにより「ユーザー指定フォルダの画像を一時フォルダへ
取り込んで加工する」という一般的なワークフローが `shutil.copy2` では実行不能になり、
LLMが代替手段を試行錯誤する原因になっていた（本タスクでは4回連続失敗の
システム警告が発生し、その前後で `src.context_compaction` の要約失敗も複数回
発生している。[issue/20260802_104012_context_compaction_summary_failed.md](20260802_104012_context_compaction_summary_failed.md)
に追記した2026-08-23分を参照）。

`copy`系関数のコピー元チェックを `_guard_check` ではなく読み取り専用相当の
チェック（またはチェック省略）に変更し、コピー先（`_args[0]`、`copytree`の場合は
第2引数）のみを書き込みガード対象にすることが根本対応と考えられる。

## 追記（2026-08-23 15:10）— 修正済み

`src/tools.py`の`_python_fs_guard_preamble()`内、`_guard_make_shutil`のループを
`("rmtree", "move")`（従来通り第1引数・第2引数の両方をガード）と
`("copy", "copy2", "copyfile", "copytree")`（新設の`_guard_make_shutil_copy`。
コピー元は`_guard_check_foreign_tmp`のみ＝他セッションの一時ディレクトリ
以外は無制限に読める、コピー先のみ`_guard_check`で書き込みガード）に分離。

`tests/test_tools_python_fs_guard.py`に回帰テストを2件追加:
- `test_shutil_copy2_from_outside_allowed_root_succeeds`
- `test_shutil_copy2_to_outside_allowed_root_is_still_blocked`

検証: `pytest tests/` 444件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
