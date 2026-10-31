# execute_python_code: 切り詰めマーカーの日本語文字がSyntaxErrorを引き起こす

- **区分**: バグ
- **検知日時**: 2026-08-06 00:18:38
- **対象ログファイル**: data/logs/app_20260805_23_1.log

## 経緯

栄養情報追加バッチ処理中に、`execute_python_code` がLLMによって生成された
Pythonコードを実行した際、`SyntaxError` が発生して終了コード1で失敗した。
エラーの原因は、ツール結果の切り詰めマーカー文中の日本語文字（全角読点
`、` U+3001）がPythonコード内にそのまま含まれていたため。

## ログ引用

```
2026-08-06 00:18:38,682 WARNING src.tools: tool_result: name=execute_python_code args_code='...' content='[終了コード] 1
[標準エラー]
  File "E:\\user_a\\レシピ\\_tmp_76d18c38-d97f-4c04-9952-07af97355491\\tmpm80v0zw3.py", line 212
    ...(切り詰め: 元は2867文字中、先頭2000文字のみ表示しています。全文はこの会話の履歴に保存されていますが、入力容量の都合でモデルへは渡されていません。詳細が必要な場合は、同じ引数での再実行ではなく、別の範囲指定（例: Readツールのoffset/limit）で読み直してください)
                       ^
SyntaxError: invalid character \'、\' (U+3001)'
```

## エラー原文

```
  File "E:\user_a\レシピ\_tmp_76d18c38-d97f-4c04-9952-07af97355491\tmpm80v0zw3.py", line 212
    ...(切り詰め: 元は2867文字中、先頭2000文字のみ表示しています。全文はこの会話の履歴に保存されていますが、入力容量の都合でモデルへは渡されていません。詳細が必要な場合は、同じ引数での再実行ではなく、別の範囲指定（例: Readツールのoffset/limit）で読み直してください)
                       ^
SyntaxError: invalid character '、' (U+3001)
```

## 推定原因

`context_trim` によって切り詰められたToolMessageの内容が、LLMのプロンプト
入力として渡される際、切り詰めマーカー文（日本語）に含まれる全角文字
（`、`等）が、LLMが生成するPythonコード内にそのまま埋め込まれる可能性がある。

LLMは切り詰めマーカー文を「コメント」として認識し、Pythonコード中に
`...(切り詰め: ...)` のような形式で書き写しているものと推測される。

Pythonのソースコード中に全角読点は文字として許可されないため、
`SyntaxError: invalid character '、' (U+3001)` で失敗する。

## 修正内容（2026-08-06）

切り詰めマーカーを英語のみに変更し、LLMがPythonコード中に埋め込むリスクを
排除した。

### 変更ファイル

| ファイル | 変更内容 |
|---------|---------|
| `src/context_trim.py` | `_MARKER_TEMPLATE` を日本語→英語に変更 |
| `src/subagent.py` | `...(以下省略)` → `...[truncated]` に変更 |
| `src/subagent.py` | `(件数が多いため前半の結果は省略...)` → 英語に変更 |

### 新しいマーカー文言

**context_trim.py:**
```
...[truncated: {original_len} chars total, first {limit} chars shown. Full text preserved in conversation history. To read the rest, re-run the tool with different offset/limit parameters]
```

**subagent.py:**
```
...[truncated]
(too many results, first part omitted. showing recent results only)
```

### テスト結果

全10テスト通過（`test_context_trim_ai_messages.py` 4件 + `test_subagent_truncation.py` 6件）。

## ユーザー回答
