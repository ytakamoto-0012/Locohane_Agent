# excel-tools調査タスクでのLLM暴走・応答停止（2件: analyze-docssのReadツール欠落／空応答nudgeの検知漏れ）

- **区分**: バグ
- **検知日時**: 2026-08-10 10:20〜10:33
- **対象ログファイル**: data/logs/app_20260810_100804.log, data/logs/app_20260810_102626.log

## 経緯

ユーザーから「`E:\user_b\test1\sample_macro.xlsm`（VBA付きExcel）の内容を確認・見た目を改善したい」という依頼があり、`analyze-docss`サブエージェントに調査が委譲された。このとき2件、別々の原因でLLMの挙動が異常になった。

1. **1件目（暴走・ループ）**: `analyze-docss`に一時ファイルを読む手段が無く、堂々巡りの推論を繰り返してループ検知が4回発動してもなお回復しなかった。
2. **2件目（無言に近い応答での停止）**: 1件目を修正して再実行したところ途中まで正常に進んだが、`run_script(read_vba.py)`成功直後にLLMが`content='…'`（三点リーダー1文字のみ）・`tool_calls=[]`という実質空の応答を返し、そのままターンが終了してユーザーには「…」しか表示されなかった。

## ログ引用

### 1件目: analyze-docssにReadツールが無く堂々巡り

```
2026-08-10 10:09:49,455 WARNING src.subagent: subagent tool=read_memory args={'name': 'E:\\user_b\\test1\\_tmp_0b660c99-425b-4478-ac84-7275bbd7834c\\excel_vba_read\\c6983424_20260810_100942_938672.json'} -> エラー: name は英数字・ハイフン・アンダースコアのみ、64文字以内で指定してください: ...
2026-08-10 10:10:59,376 WARNING src.llm: LLM応答のループを検知したため生成を打ち切ります（直近テキスト: '...\n\nあ、待て。`read_skill_file` が skills ルート外でも動くようにアップデートされた可能性もある。試してみるしかない。\n\nいや、待て。`read_skill_file` の引数 `relative_path` は skills ルートからの相対パス。絶対パスを渡すと...'）
2026-08-10 10:10:59,814 WARNING src.subagent: subagent: リトライ前にLLMモデルを再構築しました（client_broken=False）
2026-08-10 10:19:39,411 DEBUG src.llm: ループ検知チェック: ... 直近テキスト='...「`Read` ツールで `result_path`（または `path_memory` の `@N`）を読む」とある。\n    *   しかし、この環境のツール一覧に `Read` が含まれていない。代わりに `read_skill_file` や `read_memory' ...
```

（10:10・10:13・10:15・10:16の計4回、同一パターンでループ検知→LLMクライアント再構築→リトライが発生。10:20台まで継続）

### 2件目: 「…」のみの応答でターン終了

```
2026-08-10 10:32:22,572 DEBUG src.tools: tool_result: name=run_script content='[終了コード] 0\n[標準出力]\n{"path": "E:\\\\user_b\\\\test1\\\\sample_macro.xlsm", "module": "Module1", ..., "result_path": "...\\excel_vba_read\\c6983424_20260810_103222_556076.json", "path_memory": {"@31": "..."}}'
2026-08-10 10:32:24,164 DEBUG src.llm: LLM応答: content='…' reasoning_content=None tool_calls=[]
（この後10:33:29までapp.py側の新規メッセージが無い＝ターンがここで終了）
```

## 推定原因

### 1件目: `analyze-docss`のツール一覧に`Read`が無い

- `excel-tools`/`docx-tools`等のSKILL.mdは、読み込み専用スクリプト（`read_excel.py`/`read_vba.py`/`read_docx.py`等）が本文を標準出力に出さず一時JSONへ書き出し、`result_path`（`path_memory`の`@N`）を`Read`ツールで読む設計になっている（`skills/excel-tools/SKILL.md`）。
- しかし調査専用サブエージェント`analyze-docss`（[agents/analyze-docss.md](agents/analyze-docss.md)）の`tools:`一覧には`Read`（および`Grep`/`json_query`/`list_path_memory`）が含まれておらず、`read_skill_file`（skillsルート限定）・`read_memory`（永続メモリー専用、名前は英数字/64文字以内）のいずれも一時ファイルの読み取りに使えない。
- そのため、excel-tools/docx-tools等の「読み込み専用スクリプト＋result_path」パターンを使うタスクを`analyze-docss`に委譲すると、**実際に手段が存在しない**問題にLLMがぶつかり、堂々巡りの推論を続ける。アプリのループ検知（`src/llm.py`）が打ち切ってリトライしても、根本のツール欠落は解消されないため同じ結論に戻り続ける。
- 同種の問題は`explore`/`worker`/`verifier`エージェントには無い（いずれも`Read`を保持）。`analyze-docss`のみの設定漏れ。

### 2件目: `is_empty_final_message`が近似空応答を検知できない

- アプリには「無言終了（tool_callsもcontentも空）」を検知して自動的に1回だけ最終回答を促すリトライ機構がある（[src/graph.py:224 `is_empty_final_message`](src/graph.py#L224)、`EMPTY_RESPONSE_NUDGE`）。
- 判定は`content.strip()`が空文字列かどうかのみを見ている:
  ```python
  content = last.content if isinstance(last.content, str) else str(last.content)
  return not content.strip()
  ```
- 今回LLMが返した`content='…'`（三点リーダー1文字）は`strip()`しても空文字列にならないため「空ではない＝正常な最終回答」と誤判定され、`EMPTY_RESPONSE_NUDGE`が発火しなかった。
- 結果、ツール呼び出しも実質的な回答文も無いままターンが終了し、ユーザーには「…」とだけ表示されて止まったように見えた。
- 直前（10:29:49〜10:30:29の約40秒間）にコンテキスト圧縮が6回連続発火し、累積トークンも数十万規模まで膨らんでいた。同時にリトライ後の初回チャンクまで33秒の異常遅延（`llama-server スロット詰まりの疑い`）も記録されており、高負荷下でローカルLLMが退化応答（「…」）を出しやすい状況だったと考えられる。

## 対応

### 1件目: 修正済み（2026-08-10）

[agents/analyze-docss.md](agents/analyze-docss.md)と[agents/no-websearch-version/analyze-docss.md](agents/no-websearch-version/analyze-docss.md)の`tools:`に`Read`・`Grep`・`json_query`・`list_path_memory`を追加し、`explore`/`worker`と同等のfile-tools一式を持たせた。

修正後の同一セッション再実行（10:33:46）で、`Read`ツールにより`result_path`のJSON（VBAコード全文）を正常に取得できたことを確認済み。

### 2件目: 修正済み（2026-08-10）

[src/graph.py:224 `is_empty_final_message`](src/graph.py#L224)の判定を、単純な空文字列チェックから「`strip()`後に英数字・各言語の文字（`str.isalnum()`。日本語の漢字・ひらがな・カタカナも含む）を一つも含まないか」に変更した。

```python
content = last.content if isinstance(last.content, str) else str(last.content)
stripped = content.strip()
if not stripped:
    return True
return not any(ch.isalnum() for ch in stripped)
```

これにより「…」「...」「。」「-」等の記号のみの退化応答も「空」として検知され、`EMPTY_RESPONSE_NUDGE`による自動リトライが発火するようになる。「OK」「完了しました。」等の意味のある短い応答は誤検知しない。

回帰テストを[tests/test_is_empty_final_message.py](tests/test_is_empty_final_message.py)に追加（「…」のみの応答が空と判定されるケースを含む）。既存の[tests/test_graph_retry_budget.py](tests/test_graph_retry_budget.py)と合わせて14件全て通過を確認済み。

## ユーザー回答

上記修正済み
