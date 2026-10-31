# json_query ツールで file_path/json_text 未指定エラーが発生

- **区分**: 問題点
- **検知日時**: 2026-08-02 09:34:48
- **対象ログファイル**: data/logs/app_20260802_04.log

## 経緯

サブエージェント（`dispatch_agent`）が `json_query` ツールを呼び出した際、
`query` のみ指定し、`file_path` または `json_text` のいずれかを指定しなかった。
`json_query` ツールは両者のいずれかが必須であるため、エラーで失敗した。

## ログ引用

```
2026-08-02 09:34:48,668 WARNING src.subagent: subagent tool=json_query args={'query': '$'} -> エラー: file_path と json_text のどちらか一方を指定してください
```

## 推定原因

LLMが `json_query` の呼び出しで `query` のみ指定し、対象ファイル/JSONを
指定し忘れた。`json_query` は `file_path` または `json_text` のいずれかが
必須引数であり、この組み合わせではツール側がエラーを返す仕様。

## 追記（YYYY-MM-DD HH:MM）

（同一原因の問題が再検知されるたびに、ここに追記を積み重ねていく）

## ユーザー回答

ここにはユーザーの回答が記述される
