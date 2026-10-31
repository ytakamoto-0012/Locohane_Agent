# web-search: `--include-answer`に値を付けて渡し3回連続失敗

- **区分**: バグ
- **検知日時**: 2026-08-23 00:08:00
- **対象ログファイル**: data/logs/app_20260822_235526.log

## 経緯

excel-vbaマクロブック作成タスク中、メインエージェントがWeb検索で参考情報を
集める際、最初の2件の`run_script`呼び出し（`--max-results 5`のみ）は成功
したが、続く3件の呼び出しで`script_args`に`'--include-answer', 'true'`を
追加したところ、いずれも終了コード2（argparseの引数エラー）で失敗した。

`search_web.py`の`--include-answer`は`action="store_true"`で値を取らない
フラグだが、`skills/web-search/SKILL.md`の説明文（「指定すると...取得する」）
は他の値ありオプション（`--max-results`等）と同じ書き方をしており、値を
渡さないフラグだと明示していなかった。そのためLLMが慣習的に`'true'`という
値を続けて渡してしまい、`argparse`が想定する位置引数`query`が1個のところ
`'true'`が余分な位置引数として解釈され`unrecognized arguments: true`と
なった。

## ログ引用

```
2026-08-22 23:56:36,501 DEBUG src.tools: tool_call: name=run_script args={'skill_name': 'web-search', 'script_filename': 'search_web.py', 'script_args': ['家計簿 収支計算表 支出 収入 項目 生活設計', '--max-results', '5', '--include-answer', 'true']} id=4j4tVoAG0u7gJg1VriCRcFvmFrbTo3th
2026-08-22 23:56:36,681 WARNING src.tools: tool_result: name=run_script content='[終了コード] 2\n[標準エラー]\nusage: search_web.py [-h] [--max-results MAX_RESULTS]\n                     [--topic {general,news,finance}] [--include-answer]\n                     [--time-range {day,week,month,year}]\n                     [--exclude-domains EXCLUDE_DOMAINS]\n                     [--include-domains INCLUDE_DOMAINS]\n                     query\nsearch_web.py: error: unrecognized arguments: true'
```
同様のエラーが23:56:36,864・23:56:37,064にも計3回連続で発生（別クエリの
並列呼び出し3件すべてで同じ誤用）。

## エラー原文

```
usage: search_web.py [-h] [--max-results MAX_RESULTS]
                     [--topic {general,news,finance}] [--include-answer]
                     [--time-range {day,week,month,year}]
                     [--exclude-domains EXCLUDE_DOMAINS]
                     [--include-domains INCLUDE_DOMAINS]
                     query
search_web.py: error: unrecognized arguments: true
```

## 推定原因

`skills/web-search/SKILL.md`が`--include-answer`を値なしフラグだと明示
していなかったため（`skills/excel-vba-edit/SKILL.md`の`--overwrite`等では
既に「（値なしフラグ）」という明記の慣習がある）。

## 対応

`skills/web-search/SKILL.md`の`--include-answer`の説明に「（値なしフラグ）」
の明記と、`"true"`等の値を続けて渡すと`unrecognized arguments`エラーになる
旨の注意書きを追加した（プロジェクト内の他スキルと同じ表記慣習に統一）。

## ユーザー回答

ここにはユーザーの回答が記述される
