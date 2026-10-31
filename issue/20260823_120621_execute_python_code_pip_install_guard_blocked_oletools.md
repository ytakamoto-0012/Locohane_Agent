# execute_python_codeでのpip installがガードで拒否され1手ロス（oletoolsインストール指示）

- **区分**: 問題点
- **検知日時**: 2026-08-23 12:06:21
- **対象ログファイル**: data/logs/app_20260823_104959.log

## 経緯

VBAマクロ修正タスクの冒頭、ユーザー指示文に「oletoolsをインストールし」と
あったため、workerが`execute_python_code`で`pip install oletools`を実行
しようとし、`execute_python_codeガード`によって`PermissionError`で拒否された。
6秒後、workerは「pip installは実行環境で禁止されていますが、まずは既存VBA
コードを確認します」と判断し、`read_vba.py`をそのまま実行して成功（oletoolsは
既に導入済みだったため実害なし）。

## ログ引用

```
2026-08-23 12:06:21,135 WARNING src.subagent: subagent tool=execute_python_code args={'code': "import subprocess\nresult = subprocess.run(['pip', 'install', 'oletools'], capture_output=True, text=True)\nprint(result.stdout)\nprint(result.stderr)"} -> [終了コード] 1
2026-08-23 12:06:21,135 DEBUG src.subagent: subagent tool=execute_python_code args=... -> '[終了コード] 1\n[標準エラー]\n...\nPermissionError: [execute_python_codeガード] git/npm/pipコマンドの実行は禁止されています: [\'pip\', \'install\', \'oletools\']'
2026-08-23 12:06:27,636 INFO src.subagent: subagent iter=2 ai='pip installは実行環境で禁止されていますが、まずは既存VBAコードを確認します。\n\n'
2026-08-23 12:06:27,849 INFO src.subagent: subagent tool=run_script args={'skill_name': 'excel-vba-read', 'script_filename': 'read_vba.py', ...} -> [終了コード] 0
```

## 推定原因

`execute_python_codeガード`はCLAUDE.mdの「新規pipライブラリが必要な時は
requirements.txtを更新する」運用（依存追加は開発者側が管理し、LLMが実行時に
勝手にインストールしない設計）を守るための意図した制限であり、バグではない。
今回はユーザー指示文に「oletoolsをインストールし」という一文が含まれていた
ためLLMが素直に実行を試みたが、oletoolsは`excel-vba-read`スキル用に既に
導入済みで、実際には不要な手順だった。6秒で自己回復しており実害は無い。

## 追記（2026-08-23 12:57）

同一タスクの継続中、今度はplannerサブエージェントが`pip install oletools`を
再試行して同様にブロックされ、そこから「pipで入れられない＝インストールされて
いない」と誤って結論。実際には既にインストール済みで、同時刻に別の
サブエージェント（analyze-docs）が同じライブラリで正常にVBAコードを読み込めて
いたにもかかわらず、plannerは「oletoolsが未インストールのためVBAコードを
読めない」という誤った内容を計画のblockerとして書き、ユーザーに
`pip install oletools`を依頼するよう提案する草案を作成した。メインエージェントは
この矛盾（analyze-docsは成功しているのにplannerは失敗と主張）に気づいて
約70秒間の無駄な内部推論に陥り、最終的にThinkingLoopDetectedで自動回復した。

今回は前回と異なり自己回復せず、誤情報が計画草案という成果物に混入しかけた点で
実害が大きい。今回は今後の再発防止のため`## 対応`を実装した。

## 対応（実装済み・2026-08-23）

`src/tools.py`の`execute_python_codeガード`（pip/git/npm等ブロック時の
`PermissionError`メッセージ）に、「これはインストール済みかどうかとは無関係の
一律禁止であり、既存ライブラリは大抵インストール済みなので、まずimportや
スクリプトの実行を試すこと。それでも失敗する場合のみ新規ライブラリが必要と
ユーザーに報告すること」という一文を追加した。「pip installできない」＝
「ライブラリが無い」という誤った推論を防ぐのが狙い。

テスト: `tests/test_tools_python_fs_guard.py`に
`test_pip_install_blocked_message_hints_library_may_already_be_installed`を
追加。`pytest tests/`442件全通過。

## ユーザー回答

ここにはユーザーの回答が記述される
