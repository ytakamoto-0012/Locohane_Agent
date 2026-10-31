# LLM応答のループ検知（thinking暴走）

- **区分**: 問題点
- **検知日時**: 2026-08-13 16:30:00
- **対象ログファイル**: data/logs/app_20260812_205426.log

## 経緯

メインエージェントが年間行事予定表（Excel/PPTX）を作成する際、thinking（reasoning_content）内で同じ内容を繰り返し、ループ検知により生成が打ち切られた。`thinking_loop_guard` によりリトライが実行された。

## ログ引用

```
2026-08-12 21:04:37,970 WARNING src.llm: LLM応答のループを検知したため生成を打ち切ります（直近テキスト: '月: 新入生募集・準備\n- 4月: 新入生歓迎交流会\n- 5月: ○○まつり手伝い\n- 7月: ふれあい祭り\n- 8月: 夏休み行事\n- 9月: 敬老会手伝い\n- 10月: 宮ケ瀬神社秋季大祭\n- 11月: 準備月\n- 12月: クリスマス会\n\n週間ベースでは、各行事ごとに準備・段取り・片付け・打ち合わせの時期を詳細に示す。\n\nまず、月間ベースのExcelファイル を作成する。\n\n2026年度の年間行事予定表を作成する。\n\n過去4年のデータに基づい て設計：\n\n月間ベース（年表）\n- 1月: 6年生を送る会（あれば）\n- 2月: 役員選考 会・引継ぎ\n- 3月: 新入生募集・準備\n- 4月: 新入生歓迎交流会\n- 5月: ○○まつ り手伝い\n- 7月: ふれあい祭り\n- 8月: 夏休み行事\n- 9月: 敬老会手伝い\n- 10月:  宮ケ瀬神社秋季大祭\n- 11月: 準備月\n- 12月: クリスマス'） [name='Task-57203' id=2049204924176 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0]

2026-08-12 21:04:37,976 WARNING app.py: LLM応答のループを検知（1回目の再試行）: 直近テキスト='月: 新入生募集・準備\n- 4月: 新入生歓迎交流会\n- 5月: ○○まつり手伝い\n- 7月: ふれあい 祭り\n- 8月: 夏休み行事\n- 9月: 敬老会手伝い\n- 10月: 宮ケ瀬神社秋季大祭\n- 11月: 準備月\n- 12月: クリスマス会\n\n週間ベースでは、各行事ごとに準備・段取り・片付け・打ち合わせの時期を詳細に示す。\n\nまず、月間ベースのExcelファイルを作成する 。\n\n2026年度の年間行事予定表を作成する。\n\n過去4年のデータに基づいて設計：\n\n月間ベース（年表）\n- 1月: 6年生を送る会（あれば）\n- 2月: 役員選考会・引継ぎ\n- 3月: 新入生募集・準備\n- 4月: 新入生歓迎交流会\n- 5月: ○○まつり手伝い\n- 7月: ふれあい祭り\n- 8月: 夏休み行事\n- 9月: 敬老会手伝い\n- 10月: 宮ケ瀬神社秋季大祭\n- 11月: 準備月\n- 12月: クリスマス' [name='Task-103' id=2049195742352 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0]

2026-08-12 21:04:38,533 WARNING app.py: ThinkingLoopDetected: リトライ前にLLMグラフを再構築しました [name='Task-103' id=2049195742352 cancelling=0 cancelled=False must_cancel=False elapsed_ms=0]
```

## 推定原因

LLMが子供会の行事予定をthinking内で列挙する際、同じ月と行事の組み合わせを繰り返し、match_ratioが0.087まで上昇。`dry_multiplier` や `repeat_penalty` の効果が不十分だった可能性がある。`thinking_loop_guard` が正常に動作し、リトライによって回復した。

## 追記（2026-08-13 16:30）

- 初回検知。リトライにより回復。

## 追記（2026-08-13 17:25）

- 2件目検知。サブエージェントがpath_memory.pyのロックファイル処理を調査中にthinkingループ。
  直近テキスト: "eation. The lock file is created by `path_memory.register()`. If I could modify `path_memory.py` to not use lock files, that would fix the issue. But I don't have permission to edit files.\n\nLet me try one more thing..."
  リトライにより回復。

## 追記（2026-08-23 18:12）

対象ログファイル: data/logs/app_20260823_175334.log

3件目検知。explore サブエージェントが、`provide_download` の失敗を受けて
存在確認を依頼された際、`list_path_memory`・`Read`・`Glob`の3ツールを
「これから呼ぶ」という同じ英語の宣言を約40回近く繰り返し、ループ検知で
打ち切られた（その後モデル再構築でリトライし復旧）。

```
2026-08-23 18:12:37,299 WARNING src.llm: LLM応答のループを検知したため生成を打ち切ります（直近テキスト: '...I'll call them now.\n`list_path_memory`\n`Read(file_path="E:\\user_b\\テスト\\○○子ども会_過去実績報告書.docx")`\n`Glob(pattern="*", path="E:\\user_b\\テスト")`\n\nWait, I'm repeating myself. I'll just call them...'）
2026-08-23 18:12:37,309 WARNING src.subagent: subagent: LLM応答のループを検知（1回目の再試行）: ...
2026-08-23 18:12:37,819 WARNING src.subagent: subagent: リトライ前にLLMモデルを再構築しました（client_broken=False）
```

このケースの直接の引き金は、[glob_search_directory_not_found.md](20260813_163000_glob_search_directory_not_found.md)
に記載した「`E:\user_b\テスト（読み書き可能）`という実在しないパスへの
固執」。今回はツール呼び出し自体は正しいパス（`E:\user_b\テスト`）に
修正できていたが、"call them now" → "I'm repeating myself" という自己言及を
何十回も繰り返すだけでツール呼び出しの生成に踏み切れず、ループ検知に
救われる形になった。リトライにより最終的には回復している。

## 追記（2026-08-23 20:19）

対象ログファイル: data/logs/app_20260823_195217.log

4件目検知。魚図鑑PPTX作成タスクのworkerサブエージェントが、
「150711_trip」フォルダ内の画像ファイル一覧（10件のパス）を
列挙する思考を繰り返し、`consecutive_hits=2`でループ検知・打ち切りとなった。

```
2026-08-23 20:19:29,326 WARNING src.llm: LLM応答のループを検知したため生成を打ち切ります（直近テキスト: '...10. E:\\共有\\写真\\150711_trip\\DSC00270.JPG\n\nI will execute these calls.\nThen I will summarize...'）
2026-08-23 20:19:29,332 WARNING src.subagent: subagent: LLM応答のループを検知（1回目の再試行）: ...
2026-08-23 20:19:29,743 WARNING src.subagent: subagent: リトライ前にLLMモデルを再構築しました（client_broken=False）
```

リトライにより回復。今回は誤ったパス推測が引き金ではなく、10件の
ファイルパスを毎回律儀に全列挙してから同じ結論（「これから実行する」）を
繰り返す、という冗長な思考パターンが引き金だった。

## ユーザー回答
