# xlsxの検証→修正ループが収束せず、サブエージェントのmax_iterationsを2度消費して自己回復できないまま終了

- **区分**: 問題点
- **検知日時**: 2026-08-12 16:26:17, 16:39:22, 16:40:01, 16:45:59, 16:46:10, 16:59:05, 17:09:05
- **対象ログファイル**: data/logs/app_20260812_154052.log

## 経緯

子供会の年間行事予定表（`E:\user_b\テスト\annual_schedule.xlsx`）へ6月・12月の行事を追加編集するタスクで、`excel-edit`の`insert_rows`を使った差分パッチ編集後、workerサブエージェントが内容検証（`read_excel.py`での再読込）を行った際、「行挿入の順序を誤ったため7月のデータが上書きされて壊れた」という自作自演のバグに気づいた。しかし、この診断の直後に修正を実行せず、代わりに`read_excel.py`を同一シート・ほぼ同一offset/limitで再実行するという行動を選択し、以降 約14分間（16:26:17〜16:39:22）にわたり「読み直す→行番号を手計算で再導出する→確信が持てず読み直す」を繰り返した。

この読み直しループは、書き込み系ツール呼び出しを一切挟まないまま**workerサブエージェントの`max_iterations=40`（`config.ini` [subagent]）を静かに消費し尽くして強制打ち切り**となった（16:39:22）。ほぼ同時刻、オーケストレーター（メインエージェント）側でも「専用スキルを使うかexecute_python_codeを使うか」で同様の逡巡が発生し、フレームワークのループ検知（`src.llm`のテキスト類似度チェック）が発火して生成が強制打ち切られた（16:40:01）。

オーケストレーターは検証用サブエージェントを再ディスパッチしたが、これも同じパターンで2回目の`max_iterations`到達により打ち切られた（16:45:59）。オーケストレーター自身が`execute_python_code`で近道しようとしたが、メインエージェントとしての呼び出しは`max_calls=0`でハードガード拒否された（16:46:10）。

最終的にオーケストレーターは「差分パッチではなく、目的の値を全てタスク文に明示した上でファイル全体を`--new --overwrite`で再構築する」方針に転換したことで、ようやく実際の書き込みに到達した（16:59:05〜06）。しかしこの再構築後の検証でも、新たに「2月の月見出しが欠落している」という別の不整合が発見され、その原因を特定しようとした際に**全く同じ「行マップをゼロから何度も手計算し直す」パターンが再発**し、17:09:05に2回目のループ検知が発火して生成が打ち切られた。ログはこの直後（3737行目、リトライ準備中）で途切れており、この時点でセッションが最終的に成功したか、2月見出し欠落が解消されたかは本ログからは確認できない。

## ログ引用

```
2026-08-12 16:26:17,751 DEBUG src.llm: LLM応答: content='I see a problem - the 7月 data got overwritten by the 6月 夏休み前最後の行事 data. I need to fix this. Let me reload and redo the insertions properly.\n\n' reasoning_content='...Let me fix this by reloading the file and redoing the insertions properly.\n' tool_calls=[{'name': 'run_script', 'args': {'skill_name': 'excel-read', 'script_filename': 'read_excel.py', 'script_args': ['E:\\user_b\\テスト\\annual_schedule.xlsx', '--sheet', '月間予定表', '--offset', '0', '--limit', '30']}, ...}]

2026-08-12 16:39:22,900 WARNING src.subagent: dispatch_agent: 最大反復回数(40)に到達したため打ち切り
2026-08-12 16:39:22,902 DEBUG src.tools: tool_result: name=dispatch_agent content='[サブエージェント: 最大反復回数(40)に達したため打ち切りました]\n\n[ここまでに収集できたツール実行結果（打ち切りにより要約や整理は未完了）...]\n...\n  → モデルの解釈: I see the problem - the 7月 data got corrupted because my script wrote values before inserting rows. Let me fix this properly by reloading and doing insertions first.\n...\n  → モデルの解釈: I see the problem clearly. The 7月 event has been corrupted. Let me fix this properly.'

2026-08-12 16:45:59,391 WARNING src.subagent: dispatch_agent: 最大反復回数(40)に到達したため打ち切り

2026-08-12 16:46:10,225 DEBUG src.tools: tool_result: name=execute_python_code content='エラー: execute_python_code はメインエージェントとして呼び出しを禁止されています（max_calls=0）。これ以上自分で実行せず、残りの調査・処理は dispatch_agent（agent_type: explore, analyze-docss, verifier, worker）へ委譲してください。'

2026-08-12 16:59:05,845 DEBUG src.llm: LLM応答: content='' reasoning_content='opsファイルが作成できました。次に、このファイルを使ってexcel-editを実行します。\n' tool_calls=[{'name': 'run_script', 'args': {'skill_name': 'excel-edit', 'script_filename': 'edit_excel.py', 'script_args': ['E:\\user_b\\テスト\\annual_schedule.xlsx', '--new', '--overwrite', '--ops-file', 'E:\\user_b\\テスト\\_ops_monthly.json']}, ...}]
2026-08-12 16:59:06,169 INFO src.subagent: subagent tool=run_script args={...} -> [終了コード] 0
[標準出力]
{"path": "E:\\user_b\\テスト\\annual_schedule.xlsx", "backup_path": null, "sheets": ["月間予定表", "週間予定表"], "applied_ops": 269, "path_memory": {"@262": "E:\\user_b\\テスト\\annual_schedule.xlsx"}}

2026-08-12 17:09:02,356 DEBUG src.llm: ループ検知チェック: buffer_len=3024 match_ratio=0.455 consecutive_hits=1 直近テキスト='...Row 16: 2月 (1行) - NEW\n- Row 17: 1月 (1行)\n- Row 18: null (役員選考会) - this was row 17, now row 18...'
2026-08-12 17:09:05,737 DEBUG src.llm: ループ検知チェック: buffer_len=3174 match_ratio=0.608 consecutive_hits=2 直近テキスト='...'
2026-08-12 17:09:05,738 WARNING src.llm: LLM応答のループを検知したため生成を打ち切ります（直近テキスト: '...'） [name='Task-615543' ...]
2026-08-12 17:09:05,748 WARNING src.subagent: subagent: LLM応答のループを検知（1回目の再試行）: 直近テキスト='...' [name='Task-585292' ...]
2026-08-12 17:09:06,073 WARNING src.subagent: subagent: リトライ前にLLMモデルを再構築しました（client_broken=False） [name='Task-585292' ...]
```
（3737行目でログファイル終了。これ以降の顛末は次のログファイルに続く可能性がある）

## 推定原因

- **`run_script`（read_excel.py等）は重複呼び出しガードの対象外**: `src/tools.py`の`_check_file_tools_duplicate`はRead/Glob/Grep/json_query/read_skill/read_skill_file/get_tool_sourceのみを対象としており、`run_script`経由の呼び出し（`excel-read`スキル等）は監視されていない。そのため同一シートを微妙に異なるoffsetで7回以上読み直しても、フレームワーク側の重複検知は一切発火しなかった。
- **既存のループ検知はテキスト類似度ベースで、この失敗モードには鈍感**: `src.llm`の`ループ検知チェック`は直近の生成テキストの字面の一致率（`match_ratio`）を見ているが、今回のworkerは毎回「行番号の言い回し」を変えながら同じ再計算を繰り返していたため、`match_ratio`が低いまま（0.0〜0.05程度）14分間検知をすり抜けた。実際に発火したのは、全く別の失敗（オーケストレーターの方針逡巡、および17:09の2月見出し調査での再発）でテキストがほぼ同一のフレーズを繰り返した場合のみだった。
- **サブエージェントの`max_iterations`到達に予兆となる警告がない**: `[subagent].token_guard_soft_threshold`はトークン超過時に「まとめて回答せよ」という注意メッセージを1回注入する仕組みがあるが、iteration回数側には同等の事前警告がなく、40回に達した瞬間に無言で打ち切られる。今回は書き込み系ツールを1回も呼ばないまま予算を使い切っており、「読むだけで進捗が無い」ことをモデル自身にもフレームワークにも気づかせる手段がなかった。
- **根本には`excel-edit`のAPI設計がある**: `insert_rows`後に絶対行番号で`set_range`/`set_cell`する現行方式は、挿入によって生じる行ズレの計算をLLM側の暗算に依存している。今回はこの暗算を誤ったことが最初のバグ原因であり、かつ検証時も同じ暗算をやり直すしかないためループが起きた。この設計上の弱点は[issue/20260812_121000_excel_edit_mergedcell_attributeerror.md](20260812_121000_excel_edit_mergedcell_attributeerror.md)（`insert_rows`後の`set_range`が結合セルの座標シフトを考慮していない）と同根。
- 最終的に収束したのは「差分パッチをやめてファイル全体を`--new --overwrite`で再構築し、目的値をタスク文に全て明示する」という、LLMに行ズレ計算をさせないアプローチに切り替えたときのみだった。ただしこれは今回のセッション内でオーケストレーターが試行錯誤の末にたまたま選んだ回避策であり、恒常的な対策（プロンプト・ツール設計側の変更）にはなっていない。

## 追記（YYYY-MM-DD HH:MM）

（同一原因の問題が再検知されるたびに、ここに追記を積み重ねていく）

## ユーザー回答

ここにはユーザーの回答が記述される
