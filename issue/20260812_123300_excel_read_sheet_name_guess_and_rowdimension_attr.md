# excel-readで実在しない既定シート名"Sheet1"/"Sheet2"を指定して失敗、直後にopenpyxlのRowDimension属性名も誤って使用

- **区分**: 問題点
- **検知日時**: 2026-08-12 12:25:07, 12:29:32
- **対象ログファイル**: data/logs/app_20260812_113919.log

## 経緯

`annual_schedule.xlsx`の内容確認フェーズで、workerサブエージェントが`excel-read`スキルの`read_excel.py`をシート名`Sheet1`・`Sheet2`（openpyxlの既定シート名）を指定して2回連続実行したが、実際のシート名は`月間予定表`・`週間予定表`（作成時に明示的に付けた日本語シート名）であり、いずれも「シートが見つかりません」で失敗した。

その約4分後、グループ化（アウトライン）設定を確認するため`execute_python_code`でopenpyxlを直接操作した際、`RowDimension`オブジェクトに`.level`属性でアクセスして`AttributeError`が発生（正しくは`.outline_level`）。この回は同一実行内のstdoutで`Sheet2`側の一部データ確認までは成功していたが、グループ化レベルのprintでクラッシュした。直後（13秒後、12:29:45）に`.outline_level`へ修正した呼び出しで正常に確認できている。

いずれも実害（ファイル破損等）は無く、次の1〜2回の呼び出しで自己回復している。

## ログ引用

```
2026-08-12 12:25:07,316 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-read', 'script_filename': 'read_excel.py', 'script_args': ['E:\\user_b\\テスト\\annual_schedule.xlsx', '--sheet', 'Sheet2', '--offset', '0', '--limit', '50']} -> [終了コード] 1
2026-08-12 12:25:07,316 DEBUG src.subagent: subagent tool=run_script args={...} -> "[終了コード] 1\n[標準エラー]\nシートが見つかりません: Sheet2（存在するシート: ['月間予定表', '週間予定表']）"

2026-08-12 12:25:07,316 WARNING src.subagent: subagent tool=run_script args={'skill_name': 'excel-read', 'script_filename': 'read_excel.py', 'script_args': ['E:\\user_b\\テスト\\annual_schedule.xlsx', '--sheet', 'Sheet1', '--offset', '0', '--limit', '51']} -> [終了コード] 1
2026-08-12 12:25:07,316 DEBUG src.subagent: subagent tool=run_script args={...} -> "[終了コード] 1\n[標準エラー]\nシートが見つかりません: Sheet1（存在するシート: ['月間予定表', '週間予定表']）"

2026-08-12 12:29:32,185 DEBUG src.subagent: subagent tool=execute_python_code args={...print(f"  行{row_idx}: level={sheet2.row_dimensions[row_idx].level}")...} -> '[終了コード] 1\n[標準出力]\n...\n[標準エラー]\nTraceback (most recent call last):\n  File "...tmppv3h2twv.py", line 149, in <module>\n    print(f"  行{row_idx}: level={sheet2.row_dimensions[row_idx].level}")\nAttributeError: \'RowDimension\' object has no attribute \'level\''
```

## 推定原因

- シート名の件: 自分自身がこのセッションで既に作成した（月間予定表・週間予定表という日本語名で`add_sheet`した）ファイルにもかかわらず、読み込み時にopenpyxlの既定名`Sheet1`/`Sheet2`を推測で指定してしまった。直前のツール結果（`edit_excel.py`の`ops`引数等）に正しいシート名が含まれていたはずだが、それを参照せず一般的な既定値を使った点で、[issue/20260809_002501_glob_wrong_path_inference_error.md](20260809_002501_glob_wrong_path_inference_error.md)の「パス・識別子を推測で構築してしまう」問題と同根とみられる。
- `RowDimension.level`の件: openpyxlのAPIとして`level`ではなく`outline_level`が正しい属性名。LLMの学習データ内の誤情報またはうろ覚えによる単純なAPI名の取り違えとみられる。

## 追記（YYYY-MM-DD HH:MM）

（同一原因の問題が再検知されるたびに、ここに追記を積み重ねていく）

## 追記（2026-08-12 13:04）— シート名誤指定の詳細な発生経緯を特定

同一ログ（`app_20260812_113919.log`）を遡って追跡した結果、「サブエージェントへの情報引き継ぎ漏れ」でも「モデルの単純な物忘れ」でもなく、**メインエージェント自身がdispatch_agentのtask引数に架空の別名を書き込み、それをworkerサブエージェントが実在する別名として誤採用した**という経緯が判明した。

1. **12:24:00（line 2541）**: メインエージェントが`dispatch_agent`へ渡したtask文自体に、以下のように実名へ括弧書きで`Sheet1`/`Sheet2`という**openpyxl既定名（このファイルには存在しない）**を注記してしまっていた。
   ```
   週間予定表（Sheet2）のW48の後に...
   月間予定表（Sheet1）:
   ```
2. **12:24:48〜57（iter5〜7）**: workerサブエージェントは`read_excel.py`（シート指定なし）を実行し、結果を`Read`で読んで実名`月間予定表`/`週間予定表`を自分の会話履歴に取り込み済みだった。つまりこの時点で正しい情報は文脈内に存在していた。
3. **12:25:07（iter8）**: 直後の応答のreasoning_contentで`Sheet1 (月間予定表)`のように実名を括弧内に格下げし、外側にはtask文由来の架空の別名`Sheet1`/`Sheet2`を採用。実際のツール呼び出し引数にも`Sheet1`/`Sheet2`を使ってしまい失敗。
4. **12:25:12（iter9）**: エラーメッセージ（存在するシート一覧）を見て即座に実名へ自己修正。

この根本原因は[issue/20260809_002501_glob_wrong_path_inference_error.md](20260809_002501_glob_wrong_path_inference_error.md)と同根（正しい識別子が文脈内にあるにもかかわらず、学習データ上ありふれた汎用パターン＝`Sheet1`/`Sheet2`を優先してしまう）だが、今回は**引き金がメインエージェント自身の委譲タスク文にあった**点が新しい発見。メインエージェントが実名の説明として汎用別名を括弧書きで添えたことが、小型モデルに対して「その別名も有効な入力」という誤った手がかりを与えていた。

**対策**: `system_prompt.md`の「Task Delegation」節（実務上の注意）に、シート名・テーブル名・キー名等の識別子も直前のツール結果の実在文字列をそのまま使い、実名へ推測の別名（`Sheet1`等）を括弧書きで添えないよう明記した（path_memoryの`@N`必須ルールと同じ狙いの、識別子全般への拡張）。実害は無く1〜2回で自己回復する軽微な事象のため、ツール側のガード強化（エラー時のより踏み込んだ候補提示等）は見送り、発生源（メインエージェントのtask文生成）側でのプロンプト修正に留めた。

## ユーザー回答

ここにはユーザーの回答が記述される
